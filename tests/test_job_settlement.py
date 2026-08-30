# tests/test_job_settlement.py
"""Plan 8 Stage 1 — settling the books a detached job left open (spec §3.2).

p7d polaris (`logs/session_20260729_111737_22356`) dispatched its Gradle test
job, polled it patiently, watched 321 tests run and pass — and counted none of
them, because the job produced no terminal receipt inside the call that
started it. The exit code was written atomically to
`/tmp/sag_jobs/<job_id>.log.exit` by the launcher
(docker_orch/orch.py:979-984), the complete log was on disk, and the
before-snapshot had been taken at dispatch. Nothing returned to look.

Settlement returns to look. It reads the exit code, snapshots the same roots
again, runs the SAME parsers the synchronous path runs, and writes an ORDINARY
receipt through `record_invocation`. One schema, one writer, no second
bookkeeping system — the only thing that moved is WHEN (acceptance §6.5).

Attribution does not move at all. The settling receipt's window is its own
`before` against its own `after`, and receipts are ordered: a path claimed by a
receipt written INSIDE that window — between the dispatch and its settlement —
is EXCLUDED here and the exclusion is counted on the receipt. A receipt from
BEFORE the dispatch is outside the window and takes nothing: the whole point of
Stage 1 is that a detached retry finally gets to claim its own rewrite of a
report an earlier, already-closed attempt had produced. An untouched file
nobody vouched for stays unclaimed — the Bigtop rule is not negotiable.
"""

import json
import shlex

import pytest
from container_evidence_fakes import ContainerFS, ScriptedOrchestrator, ok

from sag.agent.control_events import ControlEventSink
from sag.agent.evidence_publications import (
    EvidencePublicationAuthority,
    install_evidence_publication_authority,
    reset_evidence_publication_authority,
)
from sag.agent.invocation_receipts import (
    RECEIPT_DIR,
    RECEIPT_MODULE_OUTCOMES_MAX_ITEMS,
    build_receipt,
    next_sequence,
    write_receipt,
)
from sag.agent.job_obligations import (
    MAX_SETTLEMENT_PERSIST_ATTEMPTS,
    OBLIGATION_DIR,
    blocks_model,
    build_obligation,
    observe_exit_marker,
    open_job_ids,
    reconcile_job_obligations,
    settle_open_obligations,
    write_obligation,
)
from sag.tools.internal.gradle_tool import (
    GradleTool,
    _gradle_cached_report_dirs,
    _gradle_module_outcomes,
    _gradle_report_identity,
)

ROOT = "/workspace/polaris"
JOB = "373f63e5a0a4"
LOG_PATH = f"/tmp/sag_jobs/{JOB}.log"
EXIT_PATH = f"{LOG_PATH}.exit"
TERMINAL_AUTHORITY = "docker_exec_inspect_v1"
DOCKER_EXEC_ID = "e" * 64
CONTAINER_ID = "c" * 64
TERMINAL_IDENTITY = {
    "terminal_authority": TERMINAL_AUTHORITY,
    "docker_exec_id": DOCKER_EXEC_ID,
    "container_id": CONTAINER_ID,
    "start_accepted": True,
    "startup_identity_verified": True,
    "runner_dispatch_state": "accepted",
    "pid": 4711,
    "pgid": 4711,
    "pid_path": "/tmp/sag_jobs/fixture.pid",
    "pgid_path": "/tmp/sag_jobs/fixture.pgid",
    "identity_path": "/tmp/sag_jobs/fixture.identity",
    "process_identity_token": "a" * 64,
}

# The tail of a Gradle run that served one module's tests from the build cache
# and ran the other's. Both parsers read THIS text, on both paths.
POLARIS_LOG = """> Task :polaris-core:compileJava
> Task :polaris-core:test
> Task :polaris-api:test FROM-CACHE
BUILD SUCCESSFUL in 41m 3s
"""

CORE_REPORT = f"{ROOT}/polaris-core/build/test-results/test/TEST-Core.xml"
API_REPORT = f"{ROOT}/polaris-api/build/test-results/test/TEST-Api.xml"
STRANGER_REPORT = f"{ROOT}/other/build/test-results/test/TEST-Stranger.xml"

# `sha256sum`-shaped digests: the snapshot parser keeps a line only when the
# digest is 64 hex characters, exactly as the container prints it.
SHA_CORE = "cc" * 32
SHA_API = "bb" * 32
SHA_STRANGER = "ab" * 32

# What was on disk when the job was dispatched: the api report from an earlier
# attempt, and a report from a module this job never touches.
BEFORE = {API_REPORT: SHA_API, STRANGER_REPORT: SHA_STRANGER}
# What is on disk when the job finishes: core wrote one, api's is byte-identical
# (Gradle served it FROM-CACHE), the stranger's never moved.
AFTER = {CORE_REPORT: SHA_CORE, API_REPORT: SHA_API, STRANGER_REPORT: SHA_STRANGER}

# The survey manifest the dispatch was decided on. Settlement must state the
# same pins and the same domain the synchronous receipt would have stated.
REQUIREMENTS = {
    "survey_fingerprint": "sf-8f21",
    "config_fingerprint": "cf-04ab",
    "build_domains": [{"root": ROOT}],
}


class JobContainer(ContainerFS):
    """`ContainerFS` plus the probes an invocation receipt makes.

    The report snapshot (`find … -exec sha256sum`), the tree's sha, the
    runner's toolchain line and the per-testcase tag read are the four round
    trips `record_invocation` owns; everything else is the ordinary evidence
    file layer.
    """

    def __init__(self, files=None, reports=None):
        super().__init__(files=files)
        self.reports = dict(reports or {})

    def __call__(self, command, **kwargs):
        # The strict snapshot reader accepts only a PROVEN clean transport
        # (exit_code == 0); a double that omits the code is an incomplete
        # bracket and would honestly claim nothing.
        if "###sag-gradle-xml-count###" in command:
            # The post-dispatch report harvest's discovery pass: the bounded
            # path list, then its own count marker and the scan's own exit
            # status. Settlement runs the SAME harvest the synchronous runner
            # does.
            self.commands.append(command)
            paths = sorted(self.reports)
            return {
                **ok("\n".join([*paths, f"###sag-gradle-xml-count###{len(paths)} 0"])),
                "exit_code": 0,
            }
        if "ATTRIBUTE = re.compile" in command:
            self.commands.append(command)
            paths = json.loads(self.files[shlex.split(command)[-1]])
            return {
                **ok(
                    json.dumps(
                        {
                            "status": "complete",
                            "suites": [
                                {
                                    "path": path,
                                    "tests": 3,
                                    "failures": 0,
                                    "errors": 0,
                                    "skipped": 0,
                                }
                                for path in paths
                            ],
                        }
                    )
                ),
                "exit_code": 0,
            }
        if command.startswith("find "):
            self.commands.append(command)
            return {
                **ok(
                    "\n".join(f"{digest}  {path}" for path, digest in sorted(self.reports.items()))
                ),
                "exit_code": 0,
            }
        if command.startswith("git -C "):
            self.commands.append(command)
            return {**ok("9f1a2b3c4d5e6f708192a3b4c5d6e7f809111213"), "exit_code": 0}
        if "SAGTOOLCHAIN" in command:
            self.commands.append(command)
            return {**ok(f"{ROOT}/gradlew\nSAGTOOLCHAIN\nGradle 8.7"), "exit_code": 0}
        if command.startswith("grep -oE") or command.startswith("ls "):
            self.commands.append(command)
            return {**ok(""), "exit_code": 0}
        return super().__call__(command, **kwargs)


class ReceiptFailingJobContainer(JobContainer):
    """Fail selected receipt chunks while keeping the small lifecycle ledger writable."""

    def __init__(self, *, receipt_failures, files=None, reports=None):
        super().__init__(files=files, reports=reports)
        self.receipt_failures = receipt_failures

    def __call__(self, command, **kwargs):
        if (
            self.receipt_failures > 0
            and RECEIPT_DIR in command
            and command.startswith("printf '%s' ")
        ):
            self.receipt_failures -= 1
            self.commands.append(command)
            return {"success": False, "exit_code": 1, "output": "injected receipt failure"}
        return super().__call__(command, **kwargs)


class ReceiptLedgerReadFailingJobContainer(JobContainer):
    """Return a valid prefix but fail the complete receipt record stream."""

    def __call__(self, command, **kwargs):
        if "for file in " in command and RECEIPT_DIR in command:
            self.commands.append(command)
            return {
                "success": False,
                "exit_code": 70,
                "output": json.dumps({"receipt_id": "partial-only"}) + "\n",
            }
        return super().__call__(command, **kwargs)


class SettlementMarkFailingJobContainer(JobContainer):
    """Lose the first terminal receipt's ledger mark after the receipt lands."""

    def __init__(self, *, files=None, reports=None):
        super().__init__(files=files, reports=reports)
        self.obligation_publishes = 0

    def __call__(self, command, **kwargs):
        tokens = shlex.split(command) if "\n" not in command else []
        if (
            tokens[:3] == ["mv", "-f", "--"]
            and len(tokens) >= 5
            and tokens[4].startswith(f"{OBLIGATION_DIR}/")
        ):
            self.obligation_publishes += 1
            # initial, terminal observation, frozen receipt id, attempt count,
            # then the settlement mark.
            if self.obligation_publishes == 5:
                self.commands.append(command)
                return {"success": False, "exit_code": 1, "output": "lost mark"}
        return super().__call__(command, **kwargs)


class HostTerminalOrchestrator(ScriptedOrchestrator):
    """Container I/O plus Docker-daemon-owned detached terminal truth."""

    def __init__(self, *, terminal_state="finished", terminal_exit_code=0):
        super().__init__()
        self.terminal_state = terminal_state
        self.terminal_exit_code = terminal_exit_code
        self.detached_terminal_states = {
            DOCKER_EXEC_ID: (terminal_state, terminal_exit_code),
        }
        self.terminal_inspections = []

    def set_detached_terminal_state(self, exec_id, state, exit_code=None):
        self.detached_terminal_states[exec_id] = (state, exit_code)

    def inspect_detached_terminal(self, handle):
        self.terminal_inspections.append(dict(handle))
        if (
            handle.get("terminal_authority") != TERMINAL_AUTHORITY
            or handle.get("container_id") != CONTAINER_ID
        ):
            return {
                "probe_success": False,
                "state": "unknown",
                "probe_error": "terminal_identity_mismatch",
            }
        state, exit_code = self.detached_terminal_states.get(
            handle.get("docker_exec_id"),
            ("unknown", None),
        )
        if state == "running":
            return {
                "probe_success": True,
                "state": "running",
                "running": True,
                "finished": False,
                "exit_code": None,
            }
        if state == "finished":
            return {
                "probe_success": True,
                "state": "finished",
                "running": False,
                "finished": True,
                "exit_code": exit_code,
            }
        return {
            "probe_success": False,
            "state": "unknown",
            "probe_error": "terminal_state_unknown",
        }


def _orchestrator(
    *,
    exit_code="0",
    reports=None,
    files=None,
    forensic_exit_marker=False,
):
    try:
        terminal_exit_code = int(exit_code) if exit_code is not None else None
    except (TypeError, ValueError):
        terminal_exit_code = None
    terminal_state = (
        "running"
        if exit_code is None
        else "finished" if terminal_exit_code is not None else "unknown"
    )
    orchestrator = HostTerminalOrchestrator(
        terminal_state=terminal_state,
        terminal_exit_code=terminal_exit_code,
    )
    orchestrator.filesystem = JobContainer(
        files={
            LOG_PATH: POLARIS_LOG,
            **(
                {EXIT_PATH: f"{exit_code}\n"}
                if forensic_exit_marker and exit_code is not None
                else {}
            ),
            **(files or {}),
        },
        reports=AFTER if reports is None else reports,
    )
    return orchestrator


def _receipt_failing_orchestrator(*, exit_code="0", receipt_failures=1):
    orchestrator = HostTerminalOrchestrator(
        terminal_state="finished",
        terminal_exit_code=int(exit_code),
    )
    orchestrator.filesystem = ReceiptFailingJobContainer(
        receipt_failures=receipt_failures,
        files={LOG_PATH: POLARIS_LOG},
        reports=AFTER,
    )
    return orchestrator


def _obligation(**overrides):
    """One obligation in the shape the real detach seam writes it.

    `dispatch_sequence` is part of that shape: it is the receipt ordinal in
    hand at dispatch, and it is the only thing that lets settlement tell a
    receipt written inside its window from one written before it. Tests that
    need two obligations, or that rewrite one body twice, keep the body they
    built rather than calling this again — a second call is a second dispatch.
    """
    body = build_obligation(
        job_id=JOB,
        tool="gradle",
        attempt=1,
        requested_action="test",
        effective_action="test",
        argv=f"{ROOT}/gradlew --continue test",
        working_directory=ROOT,
        before=BEFORE,
        log_path=LOG_PATH,
        exit_code_path=EXIT_PATH,
        **TERMINAL_IDENTITY,
        requirements_pins={
            "survey_fingerprint": "sf-8f21",
            "config_fingerprint": "cf-04ab",
        },
        domain_id=ROOT,
        dispatch_sequence=next_sequence(),
    )
    body.update(overrides)
    return body


def _settled_receipt(orchestrator, job_id=JOB):
    """The receipt THIS obligation settled into, named by the ledger itself.

    Picking it out by id would guess; the ledger states it.
    """
    receipt_id = _ledger(orchestrator, job_id)["settled_receipt_id"]
    return json.loads(orchestrator.filesystem.files[f"{RECEIPT_DIR}/{receipt_id}.json"])


def _with_obligation(orchestrator, **overrides):
    write_obligation(orchestrator.execute_command, _obligation(**overrides))
    return orchestrator


def _receipts(orchestrator):
    return [
        json.loads(body)
        for path, body in sorted(orchestrator.filesystem.files.items())
        if path.startswith(f"{RECEIPT_DIR}/")
    ]


def _ledger(orchestrator, job_id=JOB):
    return json.loads(orchestrator.filesystem.files[f"{OBLIGATION_DIR}/{job_id}.json"])


# ---------------------------------------------------------------------------
# the receipt polaris never got
# ---------------------------------------------------------------------------


def test_a_terminated_job_settles_into_an_ordinary_receipt():
    orchestrator = _with_obligation(_orchestrator())

    settlements = settle_open_obligations(orchestrator)

    (receipt,) = _receipts(orchestrator)
    assert receipt["tool"] == "gradle"
    assert receipt["exit_code"] == 0
    assert receipt["outcome"] == "completed"
    assert receipt["argv"] == f"{ROOT}/gradlew --continue test"
    assert receipt["working_directory"] == ROOT
    assert [entry["path"] for entry in receipt["report_delta"]["new"]] == [CORE_REPORT]
    assert [settlement.job_id for settlement in settlements] == [JOB]
    assert settlements[0].receipt_id == receipt["receipt_id"]


def test_an_incomplete_receipt_ledger_blocks_settlement_instead_of_double_claiming():
    orchestrator = HostTerminalOrchestrator()
    orchestrator.filesystem = ReceiptLedgerReadFailingJobContainer(
        files={LOG_PATH: POLARIS_LOG},
        reports=AFTER,
    )
    _with_obligation(orchestrator)

    reconciliation = reconcile_job_obligations(orchestrator)

    assert [item.job_id for item in reconciliation.terminal_observations] == [JOB]
    assert reconciliation.settlements == ()
    assert reconciliation.integrity_failures == (f"{JOB}:receipt_ledger_unreadable",)
    assert _receipts(orchestrator) == []
    assert _ledger(orchestrator)["process_state"] == "terminal"
    assert _ledger(orchestrator)["settlement_state"] == "pending"


def test_a_malformed_receipt_ledger_blocks_settlement_instead_of_hiding_a_claim():
    orchestrator = _orchestrator(
        files={f"{RECEIPT_DIR}/broken.json": "{not json"},
    )
    _with_obligation(orchestrator)

    reconciliation = reconcile_job_obligations(orchestrator)

    assert [item.job_id for item in reconciliation.terminal_observations] == [JOB]
    assert reconciliation.settlements == ()
    assert reconciliation.integrity_failures == (f"{JOB}:receipt_ledger_unreadable",)
    assert _ledger(orchestrator)["settlement_state"] == "pending"


def test_settlement_marks_the_obligation_and_nothing_else():
    orchestrator = _with_obligation(_orchestrator())
    before_body = _ledger(orchestrator)

    settle_open_obligations(orchestrator)

    after_body = _ledger(orchestrator)
    assert after_body["settled_receipt_id"] == _receipts(orchestrator)[0]["receipt_id"]
    lifecycle = {
        "process_state",
        "settlement_state",
        "terminal_exit_code",
        "terminal_marker_ref",
        "terminal_observed_at",
        "settlement_attempts",
        "attempted_receipt_id",
        "receipt_persistence_code",
        "settled_receipt_id",
    }
    assert {key: value for key, value in after_body.items() if key not in lifecycle} == {
        key: value for key, value in before_body.items() if key not in lifecycle
    }
    assert after_body["process_state"] == "terminal"
    assert after_body["settlement_state"] == "settled"
    assert after_body["terminal_exit_code"] == 0
    assert open_job_ids(orchestrator) == ()


def test_a_job_that_has_not_terminated_stays_open():
    """Nothing may be guessed from a partial log. No exit file, no receipt."""
    orchestrator = _with_obligation(_orchestrator(exit_code=None))

    assert settle_open_obligations(orchestrator) == []
    assert _receipts(orchestrator) == []
    assert open_job_ids(orchestrator) == (JOB,)


def test_reconciliation_never_falls_back_to_the_runtime_runner_for_control_io():
    class UnsafeOnly:
        def __init__(self):
            self.normal_calls = 0

        def execute_command(self, command, **kwargs):
            self.normal_calls += 1
            return {"success": True, "exit_code": 0, "output": ""}

    owner = UnsafeOnly()
    orchestrator = owner.execute_command

    reconciliation = reconcile_job_obligations(orchestrator)

    assert reconciliation.integrity_failures == ("orchestrator_unavailable",)
    assert owner.normal_calls == 0


def test_settlement_is_idempotent():
    """Every trigger sweeps the whole ledger; a settled obligation is skipped."""
    orchestrator = _with_obligation(_orchestrator())

    settle_open_obligations(orchestrator)
    again = settle_open_obligations(orchestrator)

    assert again == []
    assert len(_receipts(orchestrator)) == 1


def test_restart_reconciliation_rejects_a_missing_settled_receipt_without_resettling():
    orchestrator = _with_obligation(_orchestrator())
    settle_open_obligations(orchestrator)
    ledger = _ledger(orchestrator)
    receipt_id = ledger["settled_receipt_id"]
    orchestrator.filesystem.files.pop(f"{RECEIPT_DIR}/{receipt_id}.json")

    reconciliation = reconcile_job_obligations(orchestrator)

    assert reconciliation.settlements == ()
    # The host expected set still names the deleted receipt, so the complete
    # live ledger is unavailable rather than a trustworthy set with one
    # semantically "missing" member.
    assert reconciliation.integrity_failures == (f"{JOB}:receipt_ledger_unreadable",)
    assert reconciliation.barrier_active is True
    assert _ledger(orchestrator) == ledger
    assert _receipts(orchestrator) == []


def test_restart_reconciliation_rejects_a_corrupt_settled_receipt_without_resettling():
    orchestrator = _with_obligation(_orchestrator())
    settle_open_obligations(orchestrator)
    ledger = _ledger(orchestrator)
    receipt_id = ledger["settled_receipt_id"]
    orchestrator.filesystem.files[f"{RECEIPT_DIR}/{receipt_id}.json"] = "{not json"

    reconciliation = reconcile_job_obligations(orchestrator)

    assert reconciliation.settlements == ()
    assert reconciliation.integrity_failures == (f"{JOB}:receipt_ledger_unreadable",)
    assert reconciliation.barrier_active is True
    assert _ledger(orchestrator) == ledger


def test_restart_reconciliation_rejects_settled_receipt_identity_mismatch():
    orchestrator = _with_obligation(_orchestrator())
    settle_open_obligations(orchestrator)
    ledger = _ledger(orchestrator)
    receipt_id = ledger["settled_receipt_id"]
    receipt_path = f"{RECEIPT_DIR}/{receipt_id}.json"
    receipt = json.loads(orchestrator.filesystem.files[receipt_path])
    receipt["run_id"] = "different-run-epoch"
    orchestrator.filesystem.files[receipt_path] = json.dumps(receipt, sort_keys=True)

    reconciliation = reconcile_job_obligations(orchestrator)

    assert reconciliation.settlements == ()
    assert reconciliation.integrity_failures == (f"{JOB}:receipt_ledger_unreadable",)
    assert reconciliation.barrier_active is True
    assert _ledger(orchestrator) == ledger


def test_a_failing_job_settles_as_a_failed_receipt():
    """A settled failure is an ordinary failure: same field, same meaning."""
    orchestrator = _with_obligation(_orchestrator(exit_code="1"))

    settle_open_obligations(orchestrator)

    (receipt,) = _receipts(orchestrator)
    assert receipt["exit_code"] == 1
    assert receipt["outcome"] == "failed"


def test_exit_marker_observation_distinguishes_absent_malformed_and_unreadable():
    absent = observe_exit_marker(
        _orchestrator(exit_code=None, forensic_exit_marker=True).execute_command,
        EXIT_PATH,
    )
    malformed = observe_exit_marker(
        _orchestrator(
            exit_code="not-an-int",
            forensic_exit_marker=True,
        ).execute_command,
        EXIT_PATH,
    )

    def unreadable(_command, **_kwargs):
        return {"success": False, "exit_code": -1, "output": "container gone"}

    transport = observe_exit_marker(unreadable, EXIT_PATH)

    assert absent.state == "absent"
    assert malformed.state == "malformed"
    assert transport.state == "unreadable"


def test_a_transient_receipt_failure_retries_the_same_frozen_identity():
    orchestrator = _with_obligation(_receipt_failing_orchestrator(receipt_failures=1))

    first = reconcile_job_obligations(orchestrator, observed_at="2026-08-08T00:00:00Z")
    pending = _ledger(orchestrator)

    assert [item.job_id for item in first.terminal_observations] == [JOB]
    assert first.settlement_pending_job_ids == (JOB,)
    assert pending["process_state"] == "terminal"
    assert pending["settlement_state"] == "pending"
    assert pending["settlement_attempts"] == 1
    assert pending["receipt_persistence_code"] == "transport_write_failed"
    frozen_id = pending["attempted_receipt_id"]
    assert frozen_id

    second = reconcile_job_obligations(orchestrator)

    assert second.terminal_observations == ()
    assert [item.receipt_id for item in second.settlements] == [frozen_id]
    assert _ledger(orchestrator)["settled_receipt_id"] == frozen_id
    assert [receipt["receipt_id"] for receipt in _receipts(orchestrator)] == [frozen_id]


def test_restart_after_receipt_publish_reuses_it_instead_of_minting_a_second_receipt():
    orchestrator = HostTerminalOrchestrator()
    orchestrator.filesystem = SettlementMarkFailingJobContainer(
        files={LOG_PATH: POLARIS_LOG},
        reports=AFTER,
    )
    _with_obligation(orchestrator)

    first = reconcile_job_obligations(orchestrator)
    pending = _ledger(orchestrator)
    receipts_after_first = _receipts(orchestrator)

    assert first.settlements == ()
    assert first.integrity_failures == (f"{JOB}:settlement_mark_unpersisted",)
    assert pending["settlement_state"] == "pending"
    assert len(receipts_after_first) == 1
    frozen_id = pending["attempted_receipt_id"]
    assert receipts_after_first[0]["receipt_id"] == frozen_id

    replayed = reconcile_job_obligations(orchestrator)

    assert [settlement.receipt_id for settlement in replayed.settlements] == [frozen_id]
    assert len(_receipts(orchestrator)) == 1
    assert _ledger(orchestrator)["settled_receipt_id"] == frozen_id


@pytest.mark.parametrize("exit_code", [0, 1, 137])
def test_a_terminal_job_with_two_receipt_failures_closes_as_unpersisted(exit_code):
    orchestrator = _with_obligation(
        _receipt_failing_orchestrator(
            exit_code=str(exit_code),
            receipt_failures=2,
        )
    )

    first = reconcile_job_obligations(orchestrator)
    second = reconcile_job_obligations(orchestrator)
    terminal = _ledger(orchestrator)

    assert first.settlement_pending_job_ids == (JOB,)
    assert second.settlement_pending_job_ids == ()
    assert len(second.terminal_unpersisted) == 1
    assert second.terminal_unpersisted[0].exit_code == exit_code
    assert second.terminal_unpersisted[0].persistence_code == "transport_write_failed"
    assert terminal["process_state"] == "terminal"
    assert terminal["settlement_state"] == "unpersisted"
    assert terminal["terminal_exit_code"] == exit_code
    assert terminal["settlement_attempts"] == 2
    assert blocks_model(terminal) is False
    assert open_job_ids(orchestrator) == ()

    repeated = reconcile_job_obligations(orchestrator)
    assert len(repeated.terminal_observations) == 1
    assert repeated.terminal_observations[0].job_id == JOB
    assert len(repeated.terminal_unpersisted) == 1
    assert repeated.terminal_unpersisted[0].job_id == JOB
    assert repeated.terminal_unpersisted[0].exit_code == exit_code


@pytest.mark.parametrize("attempts", [-1, MAX_SETTLEMENT_PERSIST_ATTEMPTS + 1])
def test_invalid_settlement_attempt_count_is_integrity_not_an_retry_budget(attempts):
    orchestrator = _orchestrator(exit_code="0")
    invalid = _obligation(
        process_state="terminal",
        settlement_state="pending",
        terminal_exit_code=0,
        terminal_marker_ref=f"docker-exec:{DOCKER_EXEC_ID}",
        terminal_observed_at="2026-08-08T12:00:00Z",
        settlement_attempts=attempts,
        attempted_receipt_id="inv-gradle-1-invalid-attempts",
    )

    # The strict writer refuses this shape.  The reconciliation seam still
    # treats an explicitly supplied corrupt snapshot as integrity failure;
    # it never turns an over-budget value into retry authority.
    assert write_obligation(orchestrator.execute_command, invalid) is False
    reconciliation = reconcile_job_obligations(orchestrator, obligations=[invalid])

    assert reconciliation.settlements == ()
    assert reconciliation.terminal_unpersisted == ()
    assert reconciliation.settlement_pending_job_ids == ()
    assert reconciliation.integrity_failures == (f"{JOB}:invalid_settlement_attempts",)
    assert f"{OBLIGATION_DIR}/{JOB}.json" not in orchestrator.filesystem.files
    assert _receipts(orchestrator) == []


def test_the_module_outcomes_are_the_synchronous_parsers_output():
    """`_gradle_module_outcomes` over the job's own complete log — the same
    call the synchronous path makes at gradle_tool.py:404."""
    orchestrator = _with_obligation(_orchestrator())

    settle_open_obligations(orchestrator)

    (receipt,) = _receipts(orchestrator)
    parsed = _gradle_module_outcomes(POLARIS_LOG)
    assert [
        {"module": entry["module"], "status": entry["status"]}
        for entry in receipt["module_outcomes"]
    ] == parsed
    # The list itself is the parser's; what the settled harvest adds is the
    # executed witness for the modules whose reports declared their own totals.
    # Gradle's task stream can never say a module ran tests, and `attempted`
    # had to stand in for both that and a module that produced nothing.
    assert [entry.get("tests_reported") for entry in receipt["module_outcomes"]] == [3, 3]


def test_a_settled_receipt_states_the_suite_totals_a_synchronous_one_would():
    """Settlement parity. kafka died on the DETACHED path: a harvest wired only
    into the runner would have left that run reporting nothing all over again.

    And the totals are the CLAIMED reports', not the tree's. `TEST-Stranger.xml`
    sits under this build root, byte-identical before and after, vouched for by
    no cache hit — `report_delta` puts it in no bucket precisely because it is
    not this invocation's evidence. A settled harvest scans the whole tree,
    which at settlement may also hold reports a LATER dispatch wrote, so the
    scan finds it and the delta is what refuses it.
    """
    orchestrator = _with_obligation(_orchestrator())

    settle_open_obligations(orchestrator)

    (receipt,) = _receipts(orchestrator)
    assert receipt["gradle_suite_summaries"]["suites"] == [
        {
            "module": ":polaris-api",
            "task": "test",
            "xml_files": 1,
            "tests": 3,
            "failures": 0,
            "errors": 0,
            "skipped": 0,
        },
        {
            "module": ":polaris-core",
            "task": "test",
            "xml_files": 1,
            "tests": 3,
            "failures": 0,
            "errors": 0,
            "skipped": 0,
        },
    ]
    # The stranger is on disk and in no bucket, so it is in no total either:
    # summing it would state another dispatch's three tests as this one's.
    assert _gradle_report_identity(STRANGER_REPORT, ROOT) == (":other", "test")
    assert STRANGER_REPORT not in {
        entry["path"]
        for bucket in ("new", "changed", "cached")
        for entry in receipt["report_delta"].get(bucket) or ()
    }
    assert ":other" not in {
        suite["module"] for suite in receipt["gradle_suite_summaries"]["suites"]
    }
    assert receipt["gradle_row_disclosure"]["rows_source"] == "gradle_xml"
    assert "evidence_omissions" not in receipt


def test_a_cache_hit_the_job_vouched_for_is_claimed():
    """`_gradle_cached_report_dirs` runs on the settled path too, so kafka's
    4,686-test lesson survives detachment."""
    orchestrator = _with_obligation(_orchestrator())

    settle_open_obligations(orchestrator)

    (receipt,) = _receipts(orchestrator)
    assert [entry["path"] for entry in receipt["report_delta"]["cached"]] == [API_REPORT]


def test_an_untouched_report_nobody_vouched_for_stays_unclaimed():
    """The Bigtop rule. Settlement lets completed work claim its OWN write
    window, and not one file more."""
    orchestrator = _with_obligation(_orchestrator())

    settle_open_obligations(orchestrator)

    (receipt,) = _receipts(orchestrator)
    claimed = {
        entry["path"]
        for bucket in ("new", "changed", "cached")
        for entry in receipt["report_delta"].get(bucket, ())
    }
    assert STRANGER_REPORT not in claimed


# ---------------------------------------------------------------------------
# attribution: an intervening receipt claimed it first
# ---------------------------------------------------------------------------


def _other_receipt(receipt_id, paths, digest=SHA_CORE, exit_code=0):
    """One receipt some OTHER dispatch wrote, claiming `paths` at `digest`."""
    return build_receipt(
        receipt_id=receipt_id,
        tool="gradle",
        requested_action="test",
        effective_action="test",
        argv=f"{ROOT}/gradlew :polaris-core:test",
        working_directory=ROOT,
        exit_code=exit_code,
        before={},
        after={path: digest for path in paths},
    )


def _publish_other_receipt(orchestrator, receipt_id, paths, digest=SHA_CORE, exit_code=0):
    receipt = _other_receipt(receipt_id, paths, digest=digest, exit_code=exit_code)
    assert write_receipt(orchestrator.execute_command, receipt) is True
    return receipt


def _with_intervening(*paths):
    """This job's obligation, plus a receipt written AFTER its dispatch.

    The order is the fact under test: the obligation's `dispatch_sequence` is
    taken first, the other receipt's id second, so the receipt is provably
    inside the window between dispatch and settlement.
    """
    orchestrator = _with_obligation(_orchestrator())
    receipt_id = f"inv-gradle-3-{next_sequence():04d}"
    _publish_other_receipt(orchestrator, receipt_id, paths)
    return orchestrator


def test_a_path_an_intervening_receipt_already_claimed_is_excluded():
    """First claim wins inside the window. Two receipts counting one report
    file is how a passing suite gets counted twice."""
    orchestrator = _with_intervening(CORE_REPORT)

    settle_open_obligations(orchestrator)

    settled = _settled_receipt(orchestrator)
    assert settled["report_delta"]["new"] == []
    assert settled["excluded_claimed_paths"] == 1


def test_the_exclusion_never_touches_what_nobody_claimed():
    """Only the intervening receipt's own paths move; this job keeps the rest
    of its window."""
    orchestrator = _with_intervening(CORE_REPORT)

    settle_open_obligations(orchestrator)

    settled = _settled_receipt(orchestrator)
    assert [entry["path"] for entry in settled["report_delta"]["cached"]] == [API_REPORT]


def test_a_rolled_back_settlement_mark_cannot_mint_a_second_receipt():
    """The host revision head makes a restored pre-settlement body forensic.

    A second sweep cannot accept the rolled-back mutable slot, so it neither
    repeats settlement nor mints a second receipt.
    """
    body = _obligation()
    orchestrator = _orchestrator()
    write_obligation(orchestrator.execute_command, body)
    settle_open_obligations(orchestrator)
    # The mark that never reached disk.
    orchestrator.filesystem.files[f"{OBLIGATION_DIR}/{JOB}.json"] = json.dumps(body, sort_keys=True)

    reconciliation = reconcile_job_obligations(orchestrator)

    assert reconciliation.integrity_failures == ("ledger_unreadable",)
    assert len(_receipts(orchestrator)) == 1


# ---------------------------------------------------------------------------
# attribution: a receipt from BEFORE the dispatch is outside the window
# ---------------------------------------------------------------------------

SHA_CORE_ATTEMPT_1 = "dd" * 32


def test_a_receipt_from_before_this_dispatch_does_not_take_the_rewrite():
    """The camel case Stage 1 exists to retire, in miniature.

    Attempt 1 ran synchronously, exited 1, and wrote a receipt claiming
    TEST-Core.xml at sha A. Attempt 2 dispatched the same command and detached,
    holding A in its `before`. The job finished and REWROTE the report at sha B.

    B is attempt 2's own evidence. The earlier receipt vouched for a version
    that is no longer on disk, and it was closed before this dispatch existed —
    first claim applies to the window between dispatch and settlement (spec
    §3.2), not to the whole history of the run. Excluding it here is how 11,492
    passing tests stayed unclaimable: the path leaves `after`, the settled
    receipt claims nothing, and the file on disk hashes to a sha only the stale
    earlier claim names.
    """
    earlier = f"inv-gradle-1-{next_sequence():04d}"
    orchestrator = _orchestrator()
    _publish_other_receipt(
        orchestrator,
        earlier,
        [CORE_REPORT],
        digest=SHA_CORE_ATTEMPT_1,
        exit_code=1,
    )
    _with_obligation(orchestrator, before={CORE_REPORT: SHA_CORE_ATTEMPT_1, **BEFORE})

    settle_open_obligations(orchestrator)

    settled = _settled_receipt(orchestrator)
    assert settled["report_delta"]["changed"] == [{"path": CORE_REPORT, "sha256": SHA_CORE}]
    assert "excluded_claimed_paths" not in settled


def test_the_pre_dispatch_receipt_is_still_the_only_claim_it_ever_made():
    """Scoping the exclusion adds no claim to anyone else's books."""
    earlier = f"inv-gradle-1-{next_sequence():04d}"
    orchestrator = _orchestrator()
    _publish_other_receipt(
        orchestrator,
        earlier,
        [CORE_REPORT],
        digest=SHA_CORE_ATTEMPT_1,
        exit_code=1,
    )
    _with_obligation(orchestrator, before={CORE_REPORT: SHA_CORE_ATTEMPT_1, **BEFORE})

    settle_open_obligations(orchestrator)

    untouched = json.loads(orchestrator.filesystem.files[f"{RECEIPT_DIR}/{earlier}.json"])
    assert untouched["report_delta"]["new"] == [{"path": CORE_REPORT, "sha256": SHA_CORE_ATTEMPT_1}]


def test_an_obligation_that_cannot_order_itself_excludes_every_claim():
    """A ledger entry with no dispatch ordinal cannot tell a window from a
    history, so it refuses to claim a path any receipt already vouched for.
    Conservative, and stated: the notice counts what it gave up."""
    receipt_id = f"inv-gradle-3-{next_sequence():04d}"
    orchestrator = _orchestrator()
    _publish_other_receipt(orchestrator, receipt_id, [CORE_REPORT])
    body = _obligation()
    body.pop("dispatch_sequence")
    write_obligation(orchestrator.execute_command, body)

    settle_open_obligations(orchestrator)

    assert _settled_receipt(orchestrator)["excluded_claimed_paths"] == 1


def _delta_paths(receipt):
    return [
        entry["path"]
        for bucket in ("new", "changed", "cached")
        for entry in receipt["report_delta"].get(bucket, ())
    ]


def test_no_interleaving_records_no_exclusion():
    """An absent fact stays an absent key: `excluded_claimed_paths` is not a
    zero every receipt carries."""
    orchestrator = _with_obligation(_orchestrator())

    settle_open_obligations(orchestrator)

    assert "excluded_claimed_paths" not in _receipts(orchestrator)[0]


# ---------------------------------------------------------------------------
# acceptance §6.5: the settled path IS the synchronous path
# ---------------------------------------------------------------------------


def _synchronous_receipt(tmp_path):
    """What the runner writes when the same job finishes inside the call.

    Mirrors gradle_tool.execute()'s own call site (the parsers at
    gradle_tool.py:404-413), because that is the receipt settlement claims to
    reproduce.
    """
    orchestrator = _orchestrator()
    authority = EvidencePublicationAuthority.for_live_run(
        run_id="run-pytest",
        sink=ControlEventSink(tmp_path / "sync-control-events.jsonl"),
    )
    token = install_evidence_publication_authority(authority, orchestrator=orchestrator)
    tool = GradleTool.__new__(GradleTool)
    tool.orchestrator = orchestrator
    tool._pending_invocation_receipt = None
    try:
        tool._record_invocation_receipt(
            requested_action="test",
            argv=f"{ROOT}/gradlew --continue test",
            working_directory=ROOT,
            attempt=1,
            result={"exit_code": 0, "output": POLARIS_LOG, "full_output": POLARIS_LOG},
            before=BEFORE,
            requirements=REQUIREMENTS,
            module_outcomes=_gradle_module_outcomes(POLARIS_LOG),
            cached_report_roots=_gradle_cached_report_dirs(POLARIS_LOG, ROOT),
        )
        return _receipts(orchestrator)[0]
    finally:
        reset_evidence_publication_authority(token)


def test_the_settled_receipt_is_field_for_field_the_synchronous_one(tmp_path):
    """One schema, one writer. Only the id — which is a sequence, not a fact —
    is allowed to differ."""
    orchestrator = _with_obligation(_orchestrator())

    settle_open_obligations(orchestrator)

    settled = _receipts(orchestrator)[0]
    synchronous = _synchronous_receipt(tmp_path)
    assert {key: value for key, value in settled.items() if key != "receipt_id"} == {
        key: value for key, value in synchronous.items() if key != "receipt_id"
    }


def test_the_settled_receipt_states_the_pins_the_dispatch_was_decided_on():
    """Settlement happens turns later; the survey manifest it would re-read is
    not the one this dispatch was decided on. The obligation carries them."""
    orchestrator = _with_obligation(_orchestrator())

    settle_open_obligations(orchestrator)

    receipt = _receipts(orchestrator)[0]
    assert receipt["survey_fingerprint"] == "sf-8f21"
    assert receipt["config_fingerprint"] == "cf-04ab"
    assert receipt["domain_id"] == ROOT


# ---------------------------------------------------------------------------
# the bounded notice
# ---------------------------------------------------------------------------


def test_the_notice_is_one_bounded_line():
    """Risk answer (spec §7): settlement must not surprise the model with a
    receipt from nowhere, and must not fabricate a tool result either."""
    orchestrator = _with_obligation(_orchestrator())

    (settlement,) = settle_open_obligations(orchestrator)

    receipt_id = settlement.receipt_id
    expected = f"[settled] job {JOB}: exit 0 — receipt {receipt_id}, 2 report paths claimed"
    assert settlement.notice() == expected
    assert "\n" not in settlement.notice()


def test_the_notice_states_the_exclusion_when_there_was_one():
    """ "Earlier" would be false: the receipt that took the path was written
    AFTER this dispatch, which is precisely why it took it."""
    orchestrator = _with_intervening(CORE_REPORT)

    (settlement,) = settle_open_obligations(orchestrator)

    assert settlement.notice().endswith("1 already claimed by an intervening receipt)")


# ---------------------------------------------------------------------------
# the notice states an evidence field the receipt could not carry
# ---------------------------------------------------------------------------

# One task row per module, past the receipt's reactor bound: a real (if huge)
# gradle log whose module list the receipt cannot represent.
OVERSIZED_REACTOR_LOG = (
    "\n".join(
        f"> Task :module{index:05d}:test" for index in range(RECEIPT_MODULE_OUTCOMES_MAX_ITEMS + 1)
    )
    + "\nBUILD SUCCESSFUL in 41m 3s\n"
)


def test_the_notice_names_an_evidence_field_the_receipt_had_to_drop():
    """The receipt's own declaration is the model's observation too.

    `build_receipt` drops an unrepresentable observability field alone rather
    than voiding the exit code, the argv and the report delta with it — and
    the model, whose next turn reads only this line, was told nothing. A
    settled dispatch that could not carry its module list looked exactly like
    one that ran a single module.
    """
    orchestrator = _with_obligation(_orchestrator(files={LOG_PATH: OVERSIZED_REACTOR_LOG}))

    (settlement,) = settle_open_obligations(orchestrator)

    receipt = _settled_receipt(orchestrator)
    assert "module_outcomes" not in receipt
    assert [entry["field"] for entry in receipt["evidence_omissions"]] == ["module_outcomes"]
    notice = settlement.notice()
    assert "module_outcomes" in notice
    assert "receipt module_outcomes is invalid" in notice
    assert "\n" not in notice


def test_a_settlement_that_carried_everything_says_nothing_extra():
    orchestrator = _with_obligation(_orchestrator())

    (settlement,) = settle_open_obligations(orchestrator)

    assert settlement.evidence_omissions == ()
    assert settlement.notice().endswith("2 report paths claimed")
