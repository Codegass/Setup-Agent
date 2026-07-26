# tests/test_invocation_receipts.py
"""Plan 5 Stage B Task B1 (P0-A): every runner call leaves a scoped receipt.

Ground-truth review 2026-07-26 (§"Evidence is snapshot-global instead of
receipt-scoped"): the validator scanned the whole filesystem after several
invocations and could not answer which invocation produced which report —
the direct cause of Bigtop's 54/54. A receipt makes that answerable: one
atomic JSON file per physical runner invocation carrying the requested and
effective action, the exact argv, the exit status, and the before/after
content-hash delta of the test reports the invocation actually touched.

Persistence is best effort at the tool layer: a failed write NEVER raises
and never blocks the command result — it only flips `receipt_persisted` to
false in the ToolResult metadata (the phase gate is lane b2's business).

Scripted-orchestrator style (house pattern, shared with
tests/test_python_tool.py and tests/test_maven_gradle_tool_contracts.py).
"""

import json

from test_maven_gradle_tool_contracts import FakeBuildToolOrchestrator
from test_python_tool import MANIFEST, Orch, ok

from sag.agent.invocation_receipts import (
    RECEIPT_DIR,
    RECEIPT_HEREDOC,
    RECEIPT_SCHEMA_VERSION,
    report_delta,
    snapshot_reports,
    write_receipt,
)
from sag.tools.internal.gradle_tool import GradleTool
from sag.tools.internal.maven_tool import MavenTool
from sag.tools.internal.python_tool import PYTEST_REPORT_DIR, PythonTool

SUREFIRE = "/workspace/proj/target/surefire-reports/TEST-a.xml"
FAILSAFE = "/workspace/proj/target/failsafe-reports/TEST-b.xml"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def sha256sum_output(*pairs):
    """The container's `sha256sum` transport format: `<hash>  <path>`."""
    return "".join(f"{digest}  {path}\n" for path, digest in pairs)


class FakeExecute:
    """Scriptable stand-in for orchestrator.execute_command."""

    def __init__(self, rules=None, default=None, raises=False):
        self.rules = list(rules or [])
        self.default = default or ok("")
        self.raises = raises
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if self.raises:
            raise RuntimeError("container is gone")
        for substring, result in self.rules:
            if substring in command:
                return result(command) if callable(result) else dict(result)
        return dict(self.default)


def receipts_written(commands):
    """Every schema-v1 receipt body persisted through the recorded commands."""
    payloads = []
    for command in commands:
        if RECEIPT_DIR not in command or RECEIPT_HEREDOC not in command:
            continue
        _, _, rest = command.partition("\n")
        body, _, _ = rest.partition(f"\n{RECEIPT_HEREDOC}")
        payloads.append(json.loads(body))
    return payloads


# ---------------------------------------------------------------------------
# snapshot_reports
# ---------------------------------------------------------------------------


def test_snapshot_reports_parses_one_shell_round_trip_into_path_hashes():
    execute = FakeExecute(
        rules=[("sha256sum", ok(sha256sum_output((SUREFIRE, HASH_A), (FAILSAFE, HASH_B))))]
    )

    snapshot = snapshot_reports(execute, ["/workspace/proj"])

    assert snapshot == {SUREFIRE: HASH_A, FAILSAFE: HASH_B}
    assert len(execute.commands) == 1


def test_snapshot_reports_scans_every_root_for_the_validators_report_shapes():
    """The scan must find exactly what physical_validator.is_report_file
    accepts: surefire, failsafe, gradle test-results and pytest junit XML."""
    execute = FakeExecute()

    snapshot_reports(execute, ["/workspace/proj", PYTEST_REPORT_DIR])

    command = execute.commands[0]
    assert "/workspace/proj" in command
    assert PYTEST_REPORT_DIR in command
    for marker in (
        "/target/surefire-reports/",
        "/target/failsafe-reports/",
        "/build/test-results/",
        "/.setup_agent/pytest-reports/",
    ):
        assert marker in command
    assert "*.xml" in command


def test_snapshot_reports_ignores_output_lines_that_are_not_hash_path_pairs():
    execute = FakeExecute(
        rules=[
            (
                "sha256sum",
                ok(
                    "sha256sum: /workspace/proj/gone.xml: No such file or directory\n"
                    "\n"
                    f"{HASH_A}  {SUREFIRE}\n"
                ),
            )
        ]
    )

    assert snapshot_reports(execute, ["/workspace/proj"]) == {SUREFIRE: HASH_A}


def test_snapshot_reports_returns_nothing_and_never_raises_when_the_call_fails():
    assert snapshot_reports(FakeExecute(raises=True), ["/workspace/proj"]) == {}


def test_snapshot_reports_without_scan_roots_does_not_touch_the_container():
    execute = FakeExecute()

    assert snapshot_reports(execute, []) == {}
    assert execute.commands == []


# ---------------------------------------------------------------------------
# report_delta
# ---------------------------------------------------------------------------


def test_report_delta_separates_new_from_changed_and_drops_unchanged():
    before = {SUREFIRE: HASH_A, FAILSAFE: HASH_B}
    after = {SUREFIRE: HASH_A, FAILSAFE: HASH_C, "/workspace/proj/build/test-results/x.xml": HASH_B}

    delta = report_delta(before, after)

    assert delta == {
        "new": [{"path": "/workspace/proj/build/test-results/x.xml", "sha256": HASH_B}],
        "changed": [{"path": FAILSAFE, "sha256": HASH_C}],
    }


def test_report_delta_of_an_untouched_tree_claims_no_reports():
    """A same-path overwrite is 'changed'; a byte-identical file is neither.
    An empty delta is a stated fact, not a missing one."""
    snapshot = {SUREFIRE: HASH_A}

    assert report_delta(snapshot, dict(snapshot)) == {"new": [], "changed": []}


def test_report_delta_ignores_reports_that_vanished():
    assert report_delta({SUREFIRE: HASH_A}, {}) == {"new": [], "changed": []}


# ---------------------------------------------------------------------------
# write_receipt
# ---------------------------------------------------------------------------


def test_write_receipt_persists_atomically_under_the_session_receipt_dir():
    execute = FakeExecute()
    receipt = {"schema_version": RECEIPT_SCHEMA_VERSION, "receipt_id": "inv-maven-1-0001"}

    assert write_receipt(execute, receipt) is True

    command = execute.commands[0]
    final = f"{RECEIPT_DIR}/inv-maven-1-0001.json"
    assert f"mkdir -p {RECEIPT_DIR}" in command
    assert f"{final}.tmp" in command
    assert f"mv -f {final}.tmp {final}" in command
    assert receipts_written(execute.commands) == [receipt]


def test_write_receipt_returns_false_when_the_atomic_write_fails():
    execute = FakeExecute(default={"success": False, "exit_code": 1, "output": "Read-only"})

    assert write_receipt(execute, {"receipt_id": "inv-maven-1-0002"}) is False


def test_write_receipt_never_raises_when_the_container_call_raises():
    assert write_receipt(FakeExecute(raises=True), {"receipt_id": "inv-maven-1-0003"}) is False


def test_write_receipt_refuses_a_receipt_without_an_id():
    execute = FakeExecute()

    assert write_receipt(execute, {"schema_version": RECEIPT_SCHEMA_VERSION}) is False
    assert execute.commands == []


# ---------------------------------------------------------------------------
# maven integration
# ---------------------------------------------------------------------------


class ReceiptOrchestrator(FakeBuildToolOrchestrator):
    """Build-tool orchestrator double that scripts the report snapshots."""

    def __init__(self, snapshots=(), monitored_result=None, receipt_write=None):
        super().__init__(monitored_result)
        self.snapshots = list(snapshots)
        self.receipt_write = receipt_write
        self.receipt_commands = []

    def execute_command(self, command, workdir=None, timeout=None):
        if "sha256sum" in command:
            self.commands.append((command, workdir, timeout))
            return ok(self.snapshots.pop(0) if self.snapshots else "")
        if RECEIPT_DIR in command:
            self.commands.append((command, workdir, timeout))
            self.receipt_commands.append(command)
            return dict(self.receipt_write or ok(""))
        return super().execute_command(command, workdir=workdir, timeout=timeout)


MAVEN_TEST_OUTPUT = "\n".join(
    [
        "[INFO] --- maven-surefire-plugin:3.5.5:test (default-test) @ demo ---",
        "[INFO] Tests run: 4, Failures: 0, Errors: 0, Skipped: 0",
        "[INFO] BUILD SUCCESS",
    ]
)


def maven_tool(orchestrator):
    tool = MavenTool(orchestrator)
    tool._record_test_summary = lambda *args, **kwargs: None
    return tool


def test_maven_test_run_lands_a_schema_v1_receipt_for_its_own_reports():
    orchestrator = ReceiptOrchestrator(
        snapshots=["", sha256sum_output((SUREFIRE, HASH_A))],
        monitored_result={"output": MAVEN_TEST_OUTPUT, "exit_code": 0},
    )

    result = maven_tool(orchestrator).execute(
        command="verify",
        fail_at_end=True,
        working_directory="/workspace/proj",
    )

    (receipt,) = receipts_written(orchestrator.receipt_commands)
    assert receipt["schema_version"] == RECEIPT_SCHEMA_VERSION
    assert receipt["tool"] == "maven"
    assert receipt["requested_action"] == "verify"
    assert receipt["effective_action"] == "verify"
    assert receipt["argv"] == orchestrator.monitored_commands[0][0]
    assert receipt["working_directory"] == "/workspace/proj"
    assert receipt["exit_code"] == 0
    assert receipt["outcome"] == "completed"
    assert receipt["report_delta"] == {
        "new": [{"path": SUREFIRE, "sha256": HASH_A}],
        "changed": [],
    }
    assert result.metadata["receipt_id"] == receipt["receipt_id"]
    assert "receipt_persisted" not in result.metadata


def test_maven_receipt_brackets_the_run_so_pre_existing_reports_stay_out():
    """The Bigtop case: auxiliary XML already on disk is NOT this
    invocation's evidence — only the same-path overwrite is."""
    orchestrator = ReceiptOrchestrator(
        snapshots=[
            sha256sum_output((SUREFIRE, HASH_A), (FAILSAFE, HASH_B)),
            sha256sum_output((SUREFIRE, HASH_C), (FAILSAFE, HASH_B)),
        ],
        monitored_result={"output": MAVEN_TEST_OUTPUT, "exit_code": 0},
    )

    maven_tool(orchestrator).execute(command="verify", working_directory="/workspace/proj")

    (receipt,) = receipts_written(orchestrator.receipt_commands)
    assert receipt["report_delta"] == {
        "new": [],
        "changed": [{"path": SUREFIRE, "sha256": HASH_C}],
    }


def test_maven_failed_build_still_leaves_a_receipt_recording_the_failure():
    orchestrator = ReceiptOrchestrator(
        monitored_result={"output": "[ERROR] BUILD FAILURE", "exit_code": 1},
    )

    result = maven_tool(orchestrator).execute(command="verify", working_directory="/workspace/proj")

    (receipt,) = receipts_written(orchestrator.receipt_commands)
    assert receipt["outcome"] == "failed"
    assert receipt["exit_code"] == 1
    assert result.succeeded is False
    assert result.metadata["receipt_id"] == receipt["receipt_id"]


def test_maven_receipt_persistence_failure_is_visible_and_never_blocks():
    orchestrator = ReceiptOrchestrator(
        monitored_result={"output": MAVEN_TEST_OUTPUT, "exit_code": 0},
        receipt_write={"success": False, "exit_code": 1, "output": "Read-only file system"},
    )

    result = maven_tool(orchestrator).execute(command="verify", working_directory="/workspace/proj")

    assert result.succeeded is True
    assert result.metadata["receipt_persisted"] is False
    assert "receipt_id" not in result.metadata


# ---------------------------------------------------------------------------
# gradle integration
# ---------------------------------------------------------------------------


def test_gradle_test_run_lands_a_schema_v1_receipt():
    gradle_report = "/workspace/proj/build/test-results/test/TEST-a.xml"
    orchestrator = ReceiptOrchestrator(
        snapshots=["", sha256sum_output((gradle_report, HASH_A))],
        monitored_result={"output": "BUILD SUCCESSFUL in 3s", "exit_code": 0},
    )

    result = GradleTool(orchestrator).execute(tasks="test", working_directory="/workspace/proj")

    (receipt,) = receipts_written(orchestrator.receipt_commands)
    assert receipt["tool"] == "gradle"
    assert receipt["requested_action"] == "test"
    assert receipt["effective_action"] == "test"
    assert receipt["argv"] == orchestrator.monitored_commands[0][0]
    assert receipt["working_directory"] == "/workspace/proj"
    assert receipt["outcome"] == "completed"
    assert receipt["report_delta"]["new"] == [{"path": gradle_report, "sha256": HASH_A}]
    assert result.metadata["receipt_id"] == receipt["receipt_id"]


def test_gradle_receipt_records_the_substituted_default_task():
    """No task named means gradle_tool runs `build` — a requested/effective
    divergence the receipt must state rather than smooth over."""
    orchestrator = ReceiptOrchestrator(
        monitored_result={"output": "BUILD SUCCESSFUL in 3s", "exit_code": 0},
    )

    GradleTool(orchestrator).execute(working_directory="/workspace/proj")

    (receipt,) = receipts_written(orchestrator.receipt_commands)
    assert receipt["requested_action"] == ""
    assert receipt["effective_action"] == "build"


def test_maven_receipt_records_extra_goals_as_the_effective_action():
    orchestrator = ReceiptOrchestrator(
        monitored_result={"output": MAVEN_TEST_OUTPUT, "exit_code": 0},
    )

    maven_tool(orchestrator).execute(
        command="clean verify",
        goals="jacoco:report",
        working_directory="/workspace/proj",
    )

    (receipt,) = receipts_written(orchestrator.receipt_commands)
    assert receipt["requested_action"] == "clean verify"
    assert receipt["effective_action"] == "clean verify jacoco:report"


# ---------------------------------------------------------------------------
# python (pytest) integration
# ---------------------------------------------------------------------------


class PytestReceiptOrch(Orch):
    """Scripted python orchestrator that also answers the report snapshots."""

    def __init__(self, snapshots=(), **kwargs):
        super().__init__(**kwargs)
        self.snapshots = list(snapshots)

    def execute_command(self, cmd, workdir=None, **kwargs):
        if "sha256sum" in cmd:
            self.commands.append(cmd)
            return ok(self.snapshots.pop(0) if self.snapshots else "")
        return super().execute_command(cmd, workdir=workdir, **kwargs)


def test_pytest_run_lands_a_schema_v1_receipt_for_its_junit_report():
    junit = f"{PYTEST_REPORT_DIR}/pytest-attempt-000001.xml"
    orch = PytestReceiptOrch(
        snapshots=["", sha256sum_output((junit, HASH_A))],
        manifest=dict(MANIFEST),
        rules=[("--collect-only", ok("42 tests collected in 0.12s"))],
    )

    result = PythonTool(orch).execute("test", working_directory="/workspace/proj")

    (receipt,) = receipts_written(orch.commands)
    assert receipt["schema_version"] == RECEIPT_SCHEMA_VERSION
    assert receipt["tool"] == "python"
    assert receipt["requested_action"] == "test"
    assert receipt["effective_action"] == "test"
    assert receipt["argv"] == result.metadata["command"]
    assert receipt["working_directory"] == "/workspace/proj"
    assert receipt["exit_code"] == 0
    assert receipt["outcome"] == "completed"
    assert receipt["report_delta"] == {
        "new": [{"path": junit, "sha256": HASH_A}],
        "changed": [],
    }
    assert result.metadata["receipt_id"] == receipt["receipt_id"]
    assert "receipt_persisted" not in result.metadata


def test_pytest_receipt_hashes_the_report_after_the_attempt_tagger_rewrote_it():
    """The tagger rewrites the JUnit XML in place to persist sag.attempt_id.
    Hashing before it would bind the receipt to bytes that no longer exist,
    and every consumer would read this attempt's own report as stale."""
    orch = PytestReceiptOrch(
        manifest=dict(MANIFEST),
        rules=[("--collect-only", ok("42 tests collected in 0.12s"))],
    )

    PythonTool(orch).execute("test", working_directory="/workspace/proj")

    snapshots = [index for index, command in enumerate(orch.commands) if "sha256sum" in command]
    run = next(
        index
        for index, command in enumerate(orch.commands)
        if "--junitxml" in command and "-m pytest" in command
    )
    tagged = next(
        index for index, command in enumerate(orch.commands) if "SAG_ATTEMPT_TAGGED" in command
    )
    assert len(snapshots) == 2
    assert snapshots[0] < run < tagged < snapshots[1]


def test_pytest_receipt_ids_are_unique_across_invocations_of_one_process():
    orch = PytestReceiptOrch(
        manifest=dict(MANIFEST),
        rules=[("--collect-only", ok("42 tests collected in 0.12s"))],
    )
    tool = PythonTool(orch)

    first = tool.execute("test", working_directory="/workspace/proj")
    second = tool.execute("test", working_directory="/workspace/proj")

    ids = [receipt["receipt_id"] for receipt in receipts_written(orch.commands)]
    assert len(ids) == 2
    assert len(set(ids)) == 2
    assert first.metadata["receipt_id"] != second.metadata["receipt_id"]


def test_pytest_receipt_persistence_failure_never_masks_the_test_result():
    orch = PytestReceiptOrch(
        manifest=dict(MANIFEST),
        rules=[
            ("--collect-only", ok("42 tests collected in 0.12s")),
            (RECEIPT_DIR, {"success": False, "exit_code": 1, "output": "Read-only"}),
        ],
    )

    result = PythonTool(orch).execute("test", working_directory="/workspace/proj")

    assert result.succeeded is True
    assert result.metadata["receipt_persisted"] is False
    assert "receipt_id" not in result.metadata
