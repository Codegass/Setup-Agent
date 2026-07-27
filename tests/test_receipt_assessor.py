# tests/test_receipt_assessor.py
"""Plan 6 Stage C Task C2 — the contract-vs-receipt assessor.

Spec §C5: a mismatch between what the contract expected and what the receipt
records is NOT automatically a contradiction. Plan 5 had no such distinction —
every unexpected outcome was "the build failed", so a proxy timeout, a stale
fingerprint and a genuinely empty compile all landed as the same fact and the
loop retried all three the same way.

The assessor separates them by CAUSE:

* no dispatch, network, timeout, permission and unmet preconditions are
  BLOCKED-class codes — they say nothing about the project, so they can never
  contradict a claim;
* a fingerprint the harness has since moved past is `stale_fingerprint`;
* a dispatch that left the frozen vector is `deviated_receipt` — an extra
  observation, never a falsification of a contract it did not honour;
* only an exact/equivalent, fingerprint-fresh receipt whose typed direct
  falsifier predicate actually fires may contradict, as `falsifier_<id>`;
* a clean success is `expectation_met`.

Capability absences ride ALONGSIDE the primary verdict: a skipped testcase
whose reason matches a named capability pattern adds `capability_absent_<name>`
without changing what the run meant.

Scripted-orchestrator style (house pattern, shared with
tests/test_invocation_contracts.py and tests/test_receipt_v2_and_assessments.py).
"""

import json
import shlex

import pytest

from sag.agent.evidence_assessments import (
    ASSESSMENT_DIR,
    ASSESSMENT_HEREDOC,
    BLOCKED_CLASS_CODES,
    CAPABILITY_PATTERNS,
    CAPABILITY_PREFIX,
    FALSIFIER_PREFIX,
    ReceiptAssessment,
    assess_dispatch,
    assess_receipt,
    assessment_id,
    capability_absences,
    read_receipt,
)
from sag.agent.invocation_contracts import (
    CONTRACT_DIR,
    CONTRACT_HEREDOC,
    DIRECT_FALSIFIERS,
    action_context,
    build_contract,
    clear_action_context,
    contract_receipt_fields,
    direct_falsifiers,
    expected_observations,
    freeze_contract,
)
from sag.agent.invocation_receipts import RECEIPT_DIR, RECEIPT_HEREDOC, record_invocation
from sag.tools.base import ToolResult
from sag.tools.build.build_tool import BuildTool

SHA = "9f2b1c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b"
OTHER_SHA = "0a1b2c3d4e5f60718293a4b5c6d7e8f901112131"
CURRENT = {"target_sha": SHA, "config_fingerprint": "cfg-7"}
FALSIFIED = f"{FALSIFIER_PREFIX}empty_delta_despite_success"

# Marks a key the fixture must LEAVE OUT, so a test can state "the receipt
# knows nothing about this" instead of "it knows None".
ABSENT = object()


def contract_for(action="test", **overrides):
    """One frozen contract for `action`, with the Stage C typed expectations."""
    contract = build_contract(
        envelope_id="envelope-000001",
        tool="build",
        params={"action": action, "working_directory": "/workspace/proj"},
        effective_action=action,
        expected_cwd="/workspace/proj",
        expected_argv="--fail-at-end verify",
        target_sha=SHA,
        config_fingerprint="cfg-7",
        expected_observations=expected_observations(action),
        direct_falsifiers=direct_falsifiers(action),
    )
    for key, value in overrides.items():
        if value is ABSENT:
            contract.pop(key, None)
        else:
            contract[key] = value
    return contract


def receipt_for(**overrides):
    """One finalized receipt: exit 0, no report delta, compliant, fresh."""
    receipt = {
        "schema_version": 2,
        "receipt_id": "inv-maven-1-0001",
        "tool": "maven",
        "requested_action": "test",
        "effective_action": "verify",
        "argv": "mvn --fail-at-end verify",
        "working_directory": "/workspace/proj",
        "actual_cwd": "/workspace/proj",
        "exit_code": 0,
        "outcome": "completed",
        "report_delta": {"new": [], "changed": []},
        "target_sha": SHA,
        "config_fingerprint": "cfg-7",
        "compliance": "exact",
    }
    for key, value in overrides.items():
        if value is ABSENT:
            receipt.pop(key, None)
        else:
            receipt[key] = value
    return receipt


def wrote_reports(paths=("/workspace/proj/target/surefire-reports/TEST-a.xml",)):
    return {"new": [{"path": path, "sha256": "a" * 64} for path in paths], "changed": []}


def skipped(reason, node_id="suite#case"):
    node = {"node_id": node_id, "status": "skipped", "reason": reason}
    return {"testcase_outcomes": {"nodes": [node]}}


def _written(commands, directory, heredoc):
    payloads = []
    for command in commands:
        if directory not in command or heredoc not in command:
            continue
        _, _, rest = command.partition("\n")
        body, _, _ = rest.partition(f"\n{heredoc}")
        payloads.append(json.loads(body))
    return payloads


def assessments_written(commands):
    return _written(commands, ASSESSMENT_DIR, ASSESSMENT_HEREDOC)


def contracts_written(commands):
    return _written(commands, CONTRACT_DIR, CONTRACT_HEREDOC)


def receipts_written(commands):
    return _written(commands, RECEIPT_DIR, RECEIPT_HEREDOC)


class ContainerFS:
    """Execute double with a file layer, so a write is readable afterwards.

    Only the shapes the evidence writers use are modelled: a single-path `cat`
    read and the `mkdir -p … && cat > tmp <<HEREDOC && mv -f tmp final` write.
    Everything else answers like an empty success.
    """

    def __init__(self, files=None, writable=True, sha=SHA, markers=()):
        self.files = dict(files or {})
        self.writable = writable
        self.sha = sha
        self.markers = set(markers)
        self.commands = []

    def __call__(self, command, **kwargs):
        return self.execute_command(command, **kwargs)

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        if "test -f" in command:
            tokens = shlex.split(command)
            probed = tokens[tokens.index("-f") + 1] if "-f" in tokens else ""
            hit = any(marker in probed for marker in self.markers)
            return {"success": True, "output": "exists" if hit else "missing"}
        if "rev-parse HEAD" in command:
            return {"success": True, "output": f"{self.sha}\n"}
        if command.startswith("cat ") and "\n" not in command:
            path = shlex.split(command)[-1]
            if path in self.files:
                return {"success": True, "output": self.files[path]}
            return {"success": False, "output": f"cat: {path}: No such file or directory"}
        if command.startswith("grep -oE "):
            # The receipt's per-testcase parse reads report TAGS; the parser
            # picks them out of whatever the container prints, so the file
            # itself is a faithful stand-in for the grep output.
            hits = [body for path, body in self.files.items() if path in command]
            return {"success": bool(hits), "output": "\n".join(hits)}
        if "mv -f " in command and "\n" in command:
            if not self.writable:
                return {"success": False, "output": "Read-only file system"}
            header, _, rest = command.partition("\n")
            heredoc = header.rsplit("<<'", 1)[1].split("'", 1)[0]
            body, _, _ = rest.partition(f"\n{heredoc}")
            final = header.rsplit("mv -f ", 1)[1].split()[1]
            self.files[final] = body
            return {"success": True, "output": ""}
        return {"success": True, "output": ""}


@pytest.fixture(autouse=True)
def _no_leaked_action_context():
    clear_action_context()
    yield
    clear_action_context()


# ---------------------------------------------------------------------------
# the freeze gains typed expectations (plan §Stage C, Task C2)
# ---------------------------------------------------------------------------


def test_build_verbs_expect_an_artifact_or_a_report_delta():
    for action in ("compile", "package", "install", "build"):
        assert expected_observations(action) == ["artifact_or_report_delta"]


def test_test_contracts_expect_a_report_delta():
    assert expected_observations("test") == ["report_delta"]


def test_every_expecting_contract_carries_the_v1_direct_falsifier():
    assert direct_falsifiers("test") == [
        {"predicate_id": "empty_delta_despite_success", "kind": "delta_empty_on_exit0"}
    ]
    assert direct_falsifiers("compile") == list(DIRECT_FALSIFIERS)


def test_a_verb_that_promises_no_observation_names_no_falsifier():
    """`deps` resolves coordinates; exit 0 with no delta is its NORMAL outcome,
    so it states no expectation and nothing may be falsified against it."""
    assert expected_observations("deps") == []
    assert direct_falsifiers("deps") == []
    assert expected_observations("survey") == []


def test_a_contract_states_no_expectation_it_does_not_have():
    """Absent facts are absent keys — a deps contract carries neither field."""
    contract = contract_for("deps")

    assert "expected_observations" not in contract
    assert "direct_falsifiers" not in contract


def test_the_freeze_derives_the_expectations_from_the_public_verb():
    orchestrator = ContainerFS()

    contract = freeze_contract(
        orchestrator.execute_command,
        envelope_id="envelope-000004",
        tool="build",
        params={"action": "test", "working_directory": "/workspace/proj"},
        effective_action="verify",
        expected_cwd="/workspace/proj",
        expected_argv="--fail-at-end verify",
        intent_source="model",
        requirements={},
    )

    assert contract["expected_observations"] == ["report_delta"]
    assert contract["direct_falsifiers"] == list(DIRECT_FALSIFIERS)


# ---------------------------------------------------------------------------
# taxonomy: the blocked class (spec §C5 — these can never contradict)
# ---------------------------------------------------------------------------


def test_a_receipt_with_no_exit_state_is_no_dispatch():
    assessment = assess_receipt(
        contract_for(), receipt_for(exit_code=ABSENT), current_fingerprints=CURRENT
    )

    assert assessment.typed_code == "no_dispatch"
    assert assessment.receipt_id == "inv-maven-1-0001"


def test_a_cancelled_dispatch_is_no_dispatch():
    assessment = assess_receipt(
        contract_for(), receipt_for(), current_fingerprints=CURRENT, dispatch_status="cancelled"
    )

    assert assessment.typed_code == "no_dispatch"


def test_a_timed_out_dispatch_is_a_timeout():
    assessment = assess_receipt(
        contract_for(),
        receipt_for(exit_code=1, outcome="failed"),
        current_fingerprints=CURRENT,
        dispatch_status="timeout",
    )

    assert assessment.typed_code == "timeout"


def test_a_network_error_is_a_transient_network_block():
    assessment = assess_receipt(
        contract_for(),
        receipt_for(exit_code=1, outcome="failed"),
        current_fingerprints=CURRENT,
        error_code="NETWORK_ERROR",
    )

    assert assessment.typed_code == "transient_network"


def test_a_permission_error_is_a_permission_block():
    assessment = assess_receipt(
        contract_for(),
        receipt_for(exit_code=1, outcome="failed"),
        current_fingerprints=CURRENT,
        error_code="PERMISSION_ERROR",
    )

    assert assessment.typed_code == "permission_denied"


def test_an_unmet_environment_precondition_is_a_precondition_block():
    assessment = assess_receipt(
        contract_for(),
        receipt_for(exit_code=1, outcome="failed"),
        current_fingerprints=CURRENT,
        error_code="PREREQUISITE_INCOMPLETE",
    )

    assert assessment.typed_code == "precondition_unmet"


def test_every_blocked_class_code_is_the_bound_vocabulary():
    assert BLOCKED_CLASS_CODES == (
        "no_dispatch",
        "transient_network",
        "timeout",
        "permission_denied",
        "precondition_unmet",
    )


def test_a_blocked_dispatch_never_contradicts_even_with_an_empty_delta():
    """The falsifier's own preconditions are met — exit 0, empty delta, exact
    compliance — but the runner reported a network block, so the run says
    nothing about the project (spec §C5)."""
    assessment = assess_receipt(
        contract_for(), receipt_for(), current_fingerprints=CURRENT, error_code="NETWORK_ERROR"
    )

    assert assessment.typed_code == "transient_network"
    assert not assessment.typed_code.startswith(FALSIFIER_PREFIX)


# ---------------------------------------------------------------------------
# taxonomy: staleness and deviation (observations, not contradictions)
# ---------------------------------------------------------------------------


def test_a_contract_pinned_to_a_tree_the_harness_moved_past_is_stale():
    assessment = assess_receipt(
        contract_for(),
        receipt_for(target_sha=OTHER_SHA),
        current_fingerprints={"target_sha": OTHER_SHA, "config_fingerprint": "cfg-7"},
    )

    assert assessment.typed_code == "stale_fingerprint"
    assert assessment.fingerprints["target_sha"] == SHA


def test_a_pin_only_one_side_states_is_unknown_and_never_a_mismatch():
    """Absent on either side is UNKNOWN; calling that stale would invent a
    fact the harness never held."""
    assessment = assess_receipt(
        contract_for(document_map_fingerprint="map-9"),
        receipt_for(report_delta=wrote_reports()),
        current_fingerprints={"target_sha": SHA},
    )

    assert assessment.typed_code == "expectation_met"


def test_a_stale_contract_cannot_contradict():
    assessment = assess_receipt(
        contract_for(), receipt_for(), current_fingerprints={"target_sha": OTHER_SHA}
    )

    assert assessment.typed_code == "stale_fingerprint"


def test_a_dispatch_that_left_the_frozen_vector_is_a_deviated_receipt():
    assessment = assess_receipt(
        contract_for(),
        receipt_for(compliance="deviated", report_delta=wrote_reports()),
        current_fingerprints=CURRENT,
    )

    assert assessment.typed_code == "deviated_receipt"


def test_a_deviated_receipt_can_never_contradict_the_contract_it_ignored():
    """Every falsifier precondition except compliance holds. A dispatch that
    did not honour the contract cannot be evidence against it (spec §C5)."""
    assessment = assess_receipt(
        contract_for(), receipt_for(compliance="deviated"), current_fingerprints=CURRENT
    )

    assert assessment.typed_code == "deviated_receipt"
    assert not assessment.typed_code.startswith(FALSIFIER_PREFIX)


# ---------------------------------------------------------------------------
# taxonomy: the one predicate licensed to contradict
# ---------------------------------------------------------------------------


def test_an_exit_zero_test_run_that_wrote_no_report_is_falsified():
    assessment = assess_receipt(contract_for("test"), receipt_for(), current_fingerprints=CURRENT)

    assert assessment.typed_code == FALSIFIED


def test_an_equivalent_dispatch_may_also_falsify():
    assessment = assess_receipt(
        contract_for("test"), receipt_for(compliance="equivalent"), current_fingerprints=CURRENT
    )

    assert assessment.typed_code == FALSIFIED


def test_a_run_that_wrote_reports_is_not_falsified():
    assessment = assess_receipt(
        contract_for("test"),
        receipt_for(report_delta=wrote_reports()),
        current_fingerprints=CURRENT,
    )

    assert assessment.typed_code == "expectation_met"


def test_a_changed_report_counts_as_a_delta():
    delta = {"new": [], "changed": [{"path": "/workspace/proj/t.xml", "sha256": "b" * 64}]}
    assessment = assess_receipt(
        contract_for("test"), receipt_for(report_delta=delta), current_fingerprints=CURRENT
    )

    assert assessment.typed_code == "expectation_met"


def test_an_unknowable_compliance_never_contradicts():
    """A python dispatch materializes no argv the facade could freeze, so the
    receipt states no compliance. Unknown is not compliant."""
    assessment = assess_receipt(
        contract_for("test"), receipt_for(compliance=ABSENT), current_fingerprints=CURRENT
    )

    assert assessment.typed_code != FALSIFIED


def test_a_green_compile_is_never_contradicted_for_lacking_a_test_report():
    """A build contract expects an artifact OR a report delta, and a schema-v2
    receipt states nothing about artifacts. Unknown artifacts are not absent
    artifacts, so the predicate is not established (spec §C5)."""
    assessment = assess_receipt(
        contract_for("compile"), receipt_for(), current_fingerprints=CURRENT
    )

    assert assessment.typed_code == "expectation_met"


def test_a_build_that_states_it_produced_nothing_at_all_is_falsified():
    assessment = assess_receipt(
        contract_for("compile"),
        receipt_for(artifact_delta={"new": [], "changed": []}),
        current_fingerprints=CURRENT,
    )

    assert assessment.typed_code == FALSIFIED


def test_a_build_that_states_an_artifact_is_not_falsified():
    assessment = assess_receipt(
        contract_for("compile"),
        receipt_for(
            artifact_delta={"new": [{"path": "/workspace/proj/target/a.jar"}], "changed": []}
        ),
        current_fingerprints=CURRENT,
    )

    assert assessment.typed_code == "expectation_met"


def test_a_contract_that_named_no_falsifier_cannot_be_falsified():
    assessment = assess_receipt(contract_for("deps"), receipt_for(), current_fingerprints=CURRENT)

    assert assessment.typed_code == "expectation_met"


# ---------------------------------------------------------------------------
# taxonomy: the two ordinary outcomes
# ---------------------------------------------------------------------------


def test_a_clean_success_is_expectation_met():
    assessment = assess_receipt(
        contract_for("test"),
        receipt_for(report_delta=wrote_reports()),
        current_fingerprints=CURRENT,
    )

    assert assessment.typed_code == "expectation_met"


def test_an_honest_failure_is_unmet_and_is_not_a_contradiction():
    """A compiler error is a real, typed failure of the expectation; it is not
    a falsification of a claim, and it is not a blocked-class excuse either."""
    assessment = assess_receipt(
        contract_for("test"),
        receipt_for(exit_code=1, outcome="failed"),
        current_fingerprints=CURRENT,
    )

    assert assessment.typed_code == "expectation_unmet"


# ---------------------------------------------------------------------------
# capability absence rides alongside the primary verdict
# ---------------------------------------------------------------------------


def test_a_skip_reason_naming_a_capability_pattern_reports_it_absent():
    (absence,) = capability_absences(receipt_for(**skipped("skipped: need llvm to run")))

    assert absence.typed_code == f"{CAPABILITY_PREFIX}llvm"
    assert absence.receipt_id == "inv-maven-1-0001"


def test_the_capability_table_is_data_and_matches_every_named_pattern():
    assert [entry["name"] for entry in CAPABILITY_PATTERNS] == ["llvm", "cuda"]

    (absence,) = capability_absences(receipt_for(**skipped("requires CUDA device")))

    assert absence.typed_code == f"{CAPABILITY_PREFIX}cuda"


def test_two_capabilities_are_two_absences_in_the_tables_own_order():
    receipt = receipt_for(
        testcase_outcomes={
            "nodes": [
                {"node_id": "b#two", "status": "skipped", "reason": "no CUDA device present"},
                {"node_id": "a#one", "status": "skipped", "reason": "LLVM not enabled"},
            ]
        }
    )

    assert [absence.typed_code for absence in capability_absences(receipt)] == [
        f"{CAPABILITY_PREFIX}llvm",
        f"{CAPABILITY_PREFIX}cuda",
    ]


def test_a_skip_for_an_unrelated_reason_names_no_capability():
    assert capability_absences(receipt_for(**skipped("temporarily disabled, see #42"))) == []


def test_a_passing_testcase_never_reports_an_absent_capability():
    receipt = receipt_for(
        testcase_outcomes={"nodes": [{"node_id": "a#one", "status": "passed", "reason": "LLVM"}]}
    )

    assert capability_absences(receipt) == []


def test_a_receipt_with_no_testcase_outcomes_states_no_capability_at_all():
    assert capability_absences(receipt_for()) == []


def test_the_capability_absence_does_not_replace_the_primary_verdict():
    execute = ContainerFS()
    receipt = receipt_for(report_delta=wrote_reports(), **skipped("skipped: need llvm"))

    landed = assess_dispatch(
        execute, contract=contract_for("test"), receipt=receipt, current_fingerprints=CURRENT
    )

    assert [assessment.typed_code for assessment in landed] == [
        "expectation_met",
        f"{CAPABILITY_PREFIX}llvm",
    ]


# ---------------------------------------------------------------------------
# persistence: idempotent, and derived exactly as Stage 0 derived it
# ---------------------------------------------------------------------------


def test_the_assessment_id_derivation_is_unchanged():
    assessment = assess_receipt(contract_for("test"), receipt_for(), current_fingerprints=CURRENT)

    assert assessment.assessment_id == assessment_id("inv-maven-1-0001", FALSIFIED)


def test_assess_dispatch_persists_the_verdict_next_to_the_receipt():
    execute = ContainerFS()

    assess_dispatch(
        execute, contract=contract_for("test"), receipt=receipt_for(), current_fingerprints=CURRENT
    )

    (payload,) = assessments_written(execute.commands)
    assert payload["receipt_id"] == "inv-maven-1-0001"
    assert payload["typed_code"] == FALSIFIED
    assert payload["fingerprints"]["target_sha"] == SHA


def test_assessing_the_same_dispatch_twice_writes_the_file_once():
    execute = ContainerFS()
    arguments = {
        "contract": contract_for("test"),
        "receipt": receipt_for(),
        "current_fingerprints": CURRENT,
    }

    first = assess_dispatch(execute, **arguments)
    writes_after_first = len(assessments_written(execute.commands))
    second = assess_dispatch(execute, **arguments)

    assert [a.typed_code for a in first] == [a.typed_code for a in second]
    assert writes_after_first == 1
    assert len(assessments_written(execute.commands)) == 1


def test_a_receiptless_dispatch_is_assessed_as_nothing_at_all():
    execute = ContainerFS()

    assert assess_dispatch(execute, contract=contract_for(), receipt=None) == []
    assert assessments_written(execute.commands) == []


def test_a_failed_write_is_reported_rather_than_raised():
    execute = ContainerFS(writable=False)

    assert (
        assess_dispatch(
            execute,
            contract=contract_for("test"),
            receipt=receipt_for(),
            current_fingerprints=CURRENT,
        )
        == []
    )


def test_read_receipt_returns_the_persisted_receipt():
    receipt = receipt_for()
    execute = ContainerFS(
        files={f"{RECEIPT_DIR}/inv-maven-1-0001.json": json.dumps(receipt, sort_keys=True)}
    )

    assert read_receipt(execute, "inv-maven-1-0001") == receipt
    assert read_receipt(execute, "inv-maven-1-9999") is None
    assert read_receipt(execute, "") is None


# ---------------------------------------------------------------------------
# wiring: contract frozen -> dispatch -> receipt -> assessment on disk
# ---------------------------------------------------------------------------


class ReceiptWritingMavenTool:
    """Maven runner double that writes a real receipt for its own dispatch."""

    def __init__(self, orchestrator, exit_code=0, after=None):
        self.orchestrator = orchestrator
        self.exit_code = exit_code
        self.after = dict(after or {})
        self.calls = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        argv = "mvn --fail-at-end verify"
        metadata = record_invocation(
            self.orchestrator.execute_command,
            tool="maven",
            attempt=1,
            requested_action="verify",
            effective_action="verify",
            argv=argv,
            working_directory=kwargs.get("working_directory") or "/workspace/proj",
            exit_code=self.exit_code,
            before={},
            after=self.after,
            **contract_receipt_fields(argv),
        )
        result = (
            ToolResult.completed_success(output="BUILD SUCCESS")
            if self.exit_code == 0
            else ToolResult.completed_failure(output="BUILD FAILURE", error="failed")
        )
        result.metadata.update(metadata)
        return result


def _wired_build_tool(orchestrator=None, **runner):
    orchestrator = orchestrator or ContainerFS(markers={"pom.xml"})
    maven_tool = ReceiptWritingMavenTool(orchestrator, **runner)
    return BuildTool(orchestrator, maven_tool=maven_tool), orchestrator


def test_the_facade_assesses_the_receipt_its_own_dispatch_minted():
    tool, orchestrator = _wired_build_tool()

    with action_context(envelope_id="envelope-000031"):
        result = tool.execute(action="test", working_directory="/workspace/proj")

    (contract,) = contracts_written(orchestrator.commands)
    (receipt,) = receipts_written(orchestrator.commands)
    (assessment,) = assessments_written(orchestrator.commands)
    assert result.succeeded
    assert contract["expected_observations"] == ["report_delta"]
    assert receipt["contract_id"] == contract["contract_id"]
    assert assessment["receipt_id"] == receipt["receipt_id"]
    # exit 0 with no report delta is exactly the falsifier this contract named.
    assert assessment["typed_code"] == FALSIFIED
    assert f"{ASSESSMENT_DIR}/{assessment['assessment_id']}.json" in orchestrator.files


def test_the_facade_records_a_capability_absence_the_receipt_carries():
    orchestrator = ContainerFS(markers={"pom.xml"})
    report = "/workspace/proj/target/surefire-reports/TEST-a.xml"
    orchestrator.files[report] = (
        '<testcase classname="a" name="one"><skipped message="need llvm"/></testcase>'
    )
    tool, orchestrator = _wired_build_tool(orchestrator, after={report: "c" * 64})

    with action_context(envelope_id="envelope-000032"):
        tool.execute(action="test", working_directory="/workspace/proj")

    codes = [payload["typed_code"] for payload in assessments_written(orchestrator.commands)]
    assert codes == ["expectation_met", f"{CAPABILITY_PREFIX}llvm"]


def test_a_dispatch_that_minted_no_receipt_is_assessed_as_nothing():
    orchestrator = ContainerFS(markers={"pom.xml"})

    class SilentTool:
        def __init__(self):
            self.calls = []

        def execute(self, **kwargs):
            self.calls.append(kwargs)
            return ToolResult.completed_success(output="BUILD SUCCESS")

    tool = BuildTool(orchestrator, maven_tool=SilentTool())
    with action_context(envelope_id="envelope-000033"):
        result = tool.execute(action="compile", working_directory="/workspace/proj")

    assert result.succeeded
    assert assessments_written(orchestrator.commands) == []


def test_an_assessment_failure_never_breaks_the_result():
    """Evidence collection owes the model a build result, not an exception."""

    class ExplodingReader(ContainerFS):
        def execute_command(self, command, **kwargs):
            if command.startswith("cat ") and RECEIPT_DIR in command:
                raise RuntimeError("container is gone")
            return super().execute_command(command, **kwargs)

    tool, orchestrator = _wired_build_tool(ExplodingReader(markers={"pom.xml"}))

    with action_context(envelope_id="envelope-000034"):
        result = tool.execute(action="test", working_directory="/workspace/proj")

    assert result.succeeded
    assert assessments_written(orchestrator.commands) == []


def test_the_assessment_is_never_a_receipt_rewrite():
    """Spec §C4: the receipt is finalized once. The assessor writes NEXT to it."""
    tool, orchestrator = _wired_build_tool()

    with action_context(envelope_id="envelope-000035"):
        tool.execute(action="test", working_directory="/workspace/proj")

    assert len(receipts_written(orchestrator.commands)) == 1
    assert isinstance(
        assess_receipt(contract_for(), receipt_for(), current_fingerprints=CURRENT),
        ReceiptAssessment,
    )
