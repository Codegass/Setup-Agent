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
from build_requirements_fakes import complete_build_requirements_v1
from test_container_io import FakeContainer
from test_invocation_receipts import receipts_written as atomic_receipts_written

from sag.agent.action_intents import action_fingerprint
from sag.agent.evidence_records import frame_named_json_record_stream
from sag.agent.evidence_assessments import (
    ASSESSMENT_DIR,
    BLOCKED_CLASS_CODES,
    CAPABILITY_PATTERNS,
    CAPABILITY_PREFIX,
    CONTROL_STAGES,
    FALSIFIER_PREFIX,
    ControlAssessment,
    ReceiptAssessment,
    assess_dispatch,
    assess_receipt,
    assessment_id,
    capability_absences,
    read_assessments,
    read_live_assessment_ledger,
    read_receipt,
    validate_assessment_v2,
    write_assessment,
)
from sag.agent.evidence_publications import (
    BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
    EVIDENCE_PUBLICATION_GENESIS_SHA256,
    evidence_publication_authority_for,
    install_evidence_publication_authority,
    reset_evidence_publication_authority,
    unavailable_evidence_publication_authority,
)
from sag.agent.invocation_contracts import (
    ARGV_EXECUTION_BINDING,
    CONTRACT_DIR,
    DIRECT_FALSIFIERS,
    action_context,
    build_contract,
    clear_action_context,
    contract_receipt_fields,
    direct_falsifiers,
    expected_observations,
    freeze_contract,
)
from sag.agent.invocation_receipts import RECEIPT_DIR, record_invocation, write_receipt_result
from sag.tools.base import ToolResult
from sag.tools.build.build_tool import BuildTool
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH

SHA = "9f2b1c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b"
OTHER_SHA = "0a1b2c3d4e5f60718293a4b5c6d7e8f901112131"
CURRENT = {
    "target_sha": SHA,
    "survey_fingerprint": "survey-7",
    "config_fingerprint": "cfg-7",
    "document_map_fingerprint": "map-7",
    "domain_id": "/workspace/proj",
    "fact_epoch": 7,
}
FALSIFIED = f"{FALSIFIER_PREFIX}empty_delta_despite_success"
# The wired facade path reads only the strict host-published v1 record, so the
# wired fixture is the complete schema rather than the legacy pin sketch; the
# same record feeds the contract, the receipt, and the current pins, keeping
# every stated pin consistent by construction.
WIRED_REQUIREMENTS = complete_build_requirements_v1(
    project_root="/workspace/proj",
    build_system="maven",
    target_sha=SHA,
    config_fingerprint="cfg-7",
)

# Marks a key the fixture must LEAVE OUT, so a test can state "the receipt
# knows nothing about this" instead of "it knows None".
ABSENT = object()


def _atomic_write_tokens(command):
    """The shared writer's bounded command shape, or ``None`` otherwise."""
    tokens = (
        shlex.split(command) if "\n" not in command or command.startswith("python3 -c ") else []
    )
    if (
        tokens[:3] == ["mkdir", "-p", "--"]
        or tokens[:2] in ([":", ">"], ["printf", "%s"], ["rm", "-f"])
        or tokens[:2] == ["base64", "--decode"]
        or tokens[:3] == ["mv", "-f", "--"]
        or (
            tokens[:2] == ["python3", "-c"]
            and (
                "hashlib.sha256" in tokens[2]
                or "json.load" in tokens[2]
                or "fcntl.flock" in tokens[2]
            )
        )
    ):
        return tokens
    return None


def contract_for(action="test", **overrides):
    """One frozen contract for `action`, with the Stage C typed expectations."""
    params = {"action": action, "working_directory": "/workspace/proj"}
    domain_id = "test:/workspace/proj"
    values = {
        "run_id": "run-pytest",
        "envelope_id": "envelope-000001",
        "tool": "build",
        "params": params,
        "effective_tool": "maven",
        "effective_action": action,
        "expected_cwd": "/workspace/proj",
        "expected_argv": "--fail-at-end verify",
        "execution_binding": ARGV_EXECUTION_BINDING,
        "intent_source": "model",
        "intent_id": f"intent-receipt-assessor-{action}",
        "intent_domain_id": domain_id,
        "intent_exact_params": params,
        "action_fingerprint": action_fingerprint(
            domain_id=domain_id,
            tool="build",
            params=params,
        ),
        "target_sha": SHA,
        "survey_fingerprint": "survey-7",
        "config_fingerprint": "cfg-7",
        "document_map_fingerprint": "map-7",
        "domain_id": "/workspace/proj",
        "fact_epoch": 7,
        "expected_observations": expected_observations(action),
        "direct_falsifiers": direct_falsifiers(action),
    }
    for key, value in overrides.items():
        if value is ABSENT:
            values.pop(key, None)
        else:
            values[key] = value
    return build_contract(**values)


def receipt_for(*, action="test", **overrides):
    """One finalized receipt: exit 0, no report delta, compliant, fresh."""
    contract = contract_for(action)
    receipt = {
        "schema_version": 2,
        "receipt_id": "inv-maven-1-0001",
        "run_id": contract["run_id"],
        "tool": "maven",
        "requested_action": action,
        "effective_action": action,
        "argv": "mvn --fail-at-end verify",
        "working_directory": "/workspace/proj",
        "actual_cwd": "/workspace/proj",
        "exit_code": 0,
        "outcome": "completed",
        "report_delta": {"new": [], "changed": []},
        "target_sha": SHA,
        "survey_fingerprint": "survey-7",
        "config_fingerprint": "cfg-7",
        "document_map_fingerprint": "map-7",
        "domain_id": "/workspace/proj",
        "fact_epoch": 7,
        "contract_id": contract["contract_id"],
        "contract_hash": contract["contract_hash"],
        "execution_binding": ARGV_EXECUTION_BINDING,
        "compliance": "exact",
    }
    for key, value in overrides.items():
        if value is ABSENT:
            receipt.pop(key, None)
        else:
            receipt[key] = value
    return receipt


def build_action_context(envelope_id, *, action):
    params = {"action": action, "working_directory": "/workspace/proj"}
    domain_id = "test:/workspace/proj"
    return action_context(
        envelope_id=envelope_id,
        intent_source="model",
        intent_id=f"intent-{envelope_id}",
        intent_domain_id=domain_id,
        intent_exact_params=params,
        action_fingerprint=action_fingerprint(
            domain_id=domain_id,
            tool="build",
            params=params,
        ),
    )


def wrote_reports(paths=("/workspace/proj/target/surefire-reports/TEST-a.xml",)):
    return {"new": [{"path": path, "sha256": "a" * 64} for path in paths], "changed": []}


def skipped(reason, node_id="suite#case"):
    node = {"node_id": node_id, "status": "skipped", "reason": reason}
    return {"testcase_outcomes": {"nodes": [node]}}


def _written(commands, directory):
    filesystem = FakeContainer()
    payloads = []
    for command in commands:
        tokens = _atomic_write_tokens(command)
        if tokens is None:
            continue
        filesystem.execute_command(command)
        if tokens[:3] == ["mv", "-f", "--"] and tokens[4].startswith(f"{directory}/"):
            payloads.append(json.loads(filesystem.files[tokens[4]]))
    return payloads


def test_gate_is_a_typed_control_assessment_stage():
    assessment = ControlAssessment(
        event_or_intent_id="ctl-phase-claim-0001",
        stage="gate",
        typed_code="phase_claim_refused",
    )

    assert "gate" in CONTROL_STAGES
    assert assessment.payload()["stage"] == "gate"
    assert write_assessment(ContainerFS(), assessment) is True


def test_assessment_writer_requires_host_publication_and_replay_repairs_it(
    bind_host_evidence_publication_authority,
):
    execute = ContainerFS()
    assessment = ReceiptAssessment(
        receipt_id="inv-maven-host-publication-0001",
        typed_code="expectation_unobserved",
    )
    token = install_evidence_publication_authority(
        unavailable_evidence_publication_authority("publication probe")
    )
    try:
        assert write_assessment(execute, assessment) is False
    finally:
        reset_evidence_publication_authority(token)

    identifier = assessment.assessment_id
    body = json.dumps(assessment.payload(), sort_keys=True)
    assert execute.files[f"{ASSESSMENT_DIR}/{identifier}.json"] == body
    assert write_assessment(execute, assessment) is True
    assert bind_host_evidence_publication_authority.verify_bytes(
        record_kind="receipt_assessment",
        record_id=identifier,
        raw=body.encode("utf-8"),
    ).authorized


@pytest.mark.parametrize(
    "existing_body",
    (
        lambda payload: json.dumps(payload, indent=2, sort_keys=True),
        lambda payload: json.dumps(payload, sort_keys=True)[:-1]
        + ',"typed_code":"expectation_unobserved"}',
    ),
)
def test_assessment_replay_never_publishes_noncanonical_equal_semantics(
    existing_body,
    bind_host_evidence_publication_authority,
):
    assessment = ReceiptAssessment(
        receipt_id="inv-maven-noncanonical-assessment-0001",
        typed_code="expectation_unobserved",
    )
    payload = assessment.payload()
    body = existing_body(payload)
    execute = ContainerFS(
        {f"{ASSESSMENT_DIR}/{assessment.assessment_id}.json": body}
    )

    assert write_assessment(execute, assessment) is False
    assert not bind_host_evidence_publication_authority.verify_bytes(
        record_kind="receipt_assessment",
        record_id=assessment.assessment_id,
        raw=body.encode("utf-8"),
    ).authorized


def test_persisted_assessment_union_recomputes_identity_shape_and_filename():
    receipt = ReceiptAssessment(
        receipt_id="inv-maven-strict-assessment-0001",
        typed_code="expectation_unobserved",
        detail="typed evidence missing",
    ).payload()
    control = ControlAssessment(
        event_or_intent_id="gate-strict-assessment-0001",
        stage="gate",
        typed_code="phase_claim_refused",
        blocker_owner="harness",
        observed_facts={"receipt_count": 0},
        evidence_refs=("control-gate-0001",),
    ).payload()

    assert validate_assessment_v2(
        receipt,
        expected_id=receipt["assessment_id"],
    ) == receipt
    # A gate may carry more precise ownership than the generic typed-code
    # fallback; reconstruction must preserve that closed-enum value exactly.
    assert validate_assessment_v2(
        control,
        expected_id=control["assessment_id"],
    ) == control

    invalid = []
    for mutation in (
        lambda body: body.update(schema_version=1),
        lambda body: body.update(assessment_id="asm-forged-00000000"),
        lambda body: body.update(blocker_owner="operator"),
        lambda body: body.update(extra="unknown"),
        lambda body: body.update(event_or_intent_id="gate-mixed-union"),
        lambda body: body.update(detail=" typed evidence missing "),
    ):
        body = json.loads(json.dumps(receipt))
        mutation(body)
        invalid.append(body)
    bad_control_stage = json.loads(json.dumps(control))
    bad_control_stage["stage"] = "future-stage"
    invalid.append(bad_control_stage)
    duplicate_refs = json.loads(json.dumps(control))
    duplicate_refs["evidence_refs"] = ["control-gate-0001", "control-gate-0001"]
    invalid.append(duplicate_refs)

    for body in invalid:
        with pytest.raises((TypeError, ValueError)):
            validate_assessment_v2(body)
    with pytest.raises(ValueError, match="filename"):
        validate_assessment_v2(receipt, expected_id="asm-other-00000000")


def test_live_assessment_reader_requires_exact_host_publication_and_complete_set(
    bind_host_evidence_publication_authority,
):
    execute = ContainerFS()
    assessment = ReceiptAssessment(
        receipt_id="inv-maven-live-assessment-0001",
        typed_code="expectation_unobserved",
    )
    assert write_assessment(execute, assessment) is True
    expected = assessment.payload()

    ledger = read_live_assessment_ledger(execute)
    assert ledger.complete is True
    assert ledger.conflict is None
    assert [record.payload for record in ledger.records] == [expected]
    assert read_assessments(execute) == [expected]

    path = f"{ASSESSMENT_DIR}/{assessment.assessment_id}.json"
    exact = execute.files[path]
    execute.files[path] = exact.replace("expectation_unobserved", "expectation_unmet")
    tampered = read_live_assessment_ledger(execute)
    assert tampered.complete is False
    assert tampered.conflict is not None
    assert read_assessments(execute) == []

    execute.files[path] = exact
    del execute.files[path]
    deleted = read_live_assessment_ledger(execute)
    assert deleted.complete is False
    assert deleted.conflict is not None
    assert read_assessments(execute) == []


def test_runtime_postcondition_is_a_typed_control_assessment_stage():
    assessment = ControlAssessment(
        event_or_intent_id="ctl-jdk-runtime-0001",
        stage="postcondition",
        typed_code="java_runtime_requirement_conflict",
    )

    assert "postcondition" in CONTROL_STAGES
    assert assessment.payload()["stage"] == "postcondition"
    assert write_assessment(ContainerFS(), assessment) is True


def test_gate_control_assessment_persists_observed_facts_and_provenance():
    assessment = ControlAssessment(
        event_or_intent_id="gate-build-1",
        stage="gate",
        typed_code="BUILD_ATTEMPT_REQUIRED",
        blocker_owner="unknown",
        observed_facts={"terminal_build_receipts": 0},
        evidence_refs=("control:gate:build-1:BUILD_ATTEMPT_REQUIRED",),
    )

    assert assessment.payload()["observed_facts"] == {"terminal_build_receipts": 0}
    assert assessment.payload()["evidence_refs"] == ["control:gate:build-1:BUILD_ATTEMPT_REQUIRED"]


def assessments_written(commands):
    return _written(commands, ASSESSMENT_DIR)


def contracts_written(commands):
    return _written(commands, CONTRACT_DIR)


def receipts_written(commands):
    return atomic_receipts_written(commands)


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
        self.atomic = FakeContainer()
        self.atomic.files = self.files

    def __call__(self, command, **kwargs):
        return self.execute_command(command, **kwargs)

    def execute_control_command(self, command, **kwargs):
        # Evidence writers require the explicit clean host-control channel.
        return self.execute_command(command, **kwargs)

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        if command.startswith("file=") and "SAG_NAMED_JSON_RECORD_V1" in command:
            target = shlex.split(command.partition(";")[0][len("file=") :])[0]
            records = (
                [(target.rsplit("/", 1)[-1], self.files[target])]
                if target in self.files
                else []
            )
            return {
                "success": True,
                "exit_code": 0,
                "output": frame_named_json_record_stream(records),
            }
        if "for file in " in command and "SAG_NAMED_JSON_RECORD_V1" in command:
            quoted_glob = command.partition(" in ")[2].partition("; do")[0]
            target = shlex.split(quoted_glob)[0]
            prefix = target[: -len("*.json")]
            records = [
                (path.rsplit("/", 1)[-1], body)
                for path, body in sorted(self.files.items())
                if path.startswith(prefix) and path.endswith(".json")
            ]
            return {"success": True, "exit_code": 0, "output": frame_named_json_record_stream(records)}
        if "__SAG_FILE_MISSING__" in command:
            return {"success": False, "exit_code": 44, "output": "__SAG_FILE_MISSING__"}
        tokens = _atomic_write_tokens(command)
        if tokens is not None:
            if not self.writable and tokens[:2] != ["rm", "-f"]:
                return {"success": False, "exit_code": 1, "output": "Read-only file system"}
            return self.atomic.execute_command(command)
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
    params = {"action": "test", "working_directory": "/workspace/proj"}
    domain_id = "test:/workspace/proj"

    contract = freeze_contract(
        orchestrator.execute_command,
        run_id="run-pytest",
        envelope_id="envelope-000004",
        tool="build",
        params=params,
        effective_tool="maven",
        effective_action="verify",
        expected_cwd="/workspace/proj",
        expected_argv="--fail-at-end verify",
        execution_binding=ARGV_EXECUTION_BINDING,
        intent_source="model",
        intent_id="intent-receipt-assessor-freeze",
        intent_domain_id=domain_id,
        intent_exact_params=params,
        action_fingerprint=action_fingerprint(
            domain_id=domain_id,
            tool="build",
            params=params,
        ),
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
        receipt_for(),
        current_fingerprints={**CURRENT, "target_sha": OTHER_SHA},
    )

    assert assessment.typed_code == "stale_fingerprint"
    assert assessment.fingerprints["target_sha"] == SHA


def test_a_pin_only_one_side_states_an_unknown_contract_binding():
    """A partial pin tuple cannot be current execution authority."""
    assessment = assess_receipt(
        contract_for(document_map_fingerprint="map-9"),
        receipt_for(report_delta=wrote_reports()),
        current_fingerprints=CURRENT,
    )

    assert assessment.typed_code == "contract_binding_unknown"


def test_a_stale_contract_cannot_contradict():
    assessment = assess_receipt(
        contract_for(), receipt_for(), current_fingerprints={**CURRENT, "target_sha": OTHER_SHA}
    )

    assert assessment.typed_code == "stale_fingerprint"


def test_fact_epoch_is_an_absent_preserving_contract_receipt_pin():
    assessment = assess_receipt(
        contract_for(),
        receipt_for(fact_epoch=8),
        current_fingerprints=CURRENT,
    )

    assert assessment.typed_code == "contract_binding_unknown"


def test_model_project_authority_requires_every_current_pin():
    current = dict(CURRENT)
    current.pop("fact_epoch")

    assessment = assess_receipt(
        contract_for(),
        receipt_for(report_delta=wrote_reports()),
        current_fingerprints=current,
    )

    assert assessment.typed_code == "contract_binding_unknown"
    assert "current fact_epoch" in assessment.detail


def test_changed_current_fact_epoch_is_stale_not_current_authority():
    assessment = assess_receipt(
        contract_for(),
        receipt_for(report_delta=wrote_reports()),
        current_fingerprints={**CURRENT, "fact_epoch": 8},
    )

    assert assessment.typed_code == "stale_fingerprint"
    assert assessment.fingerprints["fact_epoch"] == "7"


def test_explicit_controller_bash_contract_needs_no_project_pin_tuple():
    params = {"action": "test", "working_directory": "/workspace/proj"}
    domain_id = "controller:/workspace/proj"
    contract = build_contract(
        run_id="run-controller-bash",
        envelope_id="envelope-controller-bash",
        tool="build",
        params=params,
        effective_tool="bash",
        effective_action="test",
        expected_cwd="/workspace/proj",
        expected_argv="-lc true",
        execution_binding=ARGV_EXECUTION_BINDING,
        intent_source="controller",
        intent_id="intent-controller-bash",
        intent_domain_id=domain_id,
        intent_exact_params=params,
        action_fingerprint=action_fingerprint(
            domain_id=domain_id,
            tool="build",
            params=params,
        ),
    )
    receipt = {
        "schema_version": 2,
        "receipt_id": "inv-bash-test-0001",
        "run_id": contract["run_id"],
        "tool": "bash",
        "requested_action": "test",
        "effective_action": "test",
        "argv": "bash -lc true",
        "working_directory": "/workspace/proj",
        "actual_cwd": "/workspace/proj",
        "exit_code": 0,
        "outcome": "completed",
        "report_delta": wrote_reports(),
        "contract_id": contract["contract_id"],
        "contract_hash": contract["contract_hash"],
        "execution_binding": ARGV_EXECUTION_BINDING,
        "compliance": "exact",
    }

    assert assess_receipt(contract, receipt).typed_code == "expectation_met"


def test_build_tool_projects_the_complete_current_authority_tuple():
    requirements = {
        "survey": {
            "survey_fingerprint": "survey-7",
            "config_fingerprint": "cfg-7",
            "document_map_fingerprint": "map-7",
        },
        "build_domains": [{"root": "/workspace/proj"}],
        "domain_facts": [
            {
                "domain_id": "dom-proj",
                "root": "/workspace/proj",
                "fact_epoch": 7,
            }
        ],
    }

    assert BuildTool._current_fingerprints(
        requirements,
        {"target_sha": SHA, "actual_cwd": "/workspace/proj/sub"},
    ) == CURRENT


def test_a_dispatch_that_left_the_frozen_vector_is_a_deviated_receipt():
    assessment = assess_receipt(
        contract_for(),
        receipt_for(
            argv="mvn --fail-at-end package",
            compliance="deviated",
            report_delta=wrote_reports(),
        ),
        current_fingerprints=CURRENT,
    )

    assert assessment.typed_code == "deviated_receipt"


def test_a_deviated_receipt_can_never_contradict_the_contract_it_ignored():
    """Every falsifier precondition except compliance holds. A dispatch that
    did not honour the contract cannot be evidence against it (spec §C5)."""
    assessment = assess_receipt(
        contract_for(),
        receipt_for(argv="mvn --fail-at-end package", compliance="deviated"),
        current_fingerprints=CURRENT,
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
        contract_for("test"),
        receipt_for(
            argv="mvn --batch-mode --fail-at-end verify",
            compliance="equivalent",
        ),
        current_fingerprints=CURRENT,
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


def test_exit_zero_compile_without_positive_artifact_evidence_is_unobserved():
    """A build contract expects an artifact OR a report delta, and a schema-v2
    receipt states nothing about artifacts. Unknown artifacts are not absent
    artifacts, so the predicate is not established (spec §C5)."""
    assessment = assess_receipt(
        contract_for("compile"), receipt_for(action="compile"), current_fingerprints=CURRENT
    )

    assert assessment.typed_code == "expectation_unobserved"


def test_a_build_that_states_it_produced_nothing_at_all_is_falsified():
    assessment = assess_receipt(
        contract_for("compile"),
        receipt_for(action="compile", artifact_delta={"new": [], "changed": []}),
        current_fingerprints=CURRENT,
    )

    assert assessment.typed_code == FALSIFIED


def test_a_build_that_states_an_artifact_is_not_falsified():
    assessment = assess_receipt(
        contract_for("compile"),
        receipt_for(
            action="compile",
            artifact_delta={"new": [{"path": "/workspace/proj/target/a.jar"}], "changed": []}
        ),
        current_fingerprints=CURRENT,
    )

    assert assessment.typed_code == "expectation_met"


def test_a_contract_that_named_no_falsifier_cannot_be_falsified():
    assessment = assess_receipt(
        contract_for("deps"), receipt_for(action="deps"), current_fingerprints=CURRENT
    )

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
    execute = ContainerFS()
    assert write_receipt_result(execute, receipt).persisted is True

    assert read_receipt(execute, "inv-maven-1-0001") == receipt
    assert read_receipt(execute, "inv-maven-1-9999") is None
    assert read_receipt(execute, "") is None


def test_live_receipt_reader_ignores_only_strict_foreign_and_historical_siblings():
    current = receipt_for()
    foreign = {
        **receipt_for(),
        "receipt_id": "inv-maven-1-0099",
        "run_id": "run-previous",
    }
    historical = {
        "schema_version": 1,
        "receipt_id": "inv-maven-1-0000",
        "tool": "maven",
        "requested_action": "test",
        "effective_action": "test",
        "argv": "mvn --fail-at-end verify",
        "working_directory": "/workspace/proj",
        "exit_code": 0,
        "outcome": "completed",
        "report_delta": {"new": [], "changed": []},
    }
    execute = ContainerFS()
    assert write_receipt_result(execute, current).persisted is True
    execute.files[f"{RECEIPT_DIR}/{foreign['receipt_id']}.json"] = json.dumps(
        foreign,
        sort_keys=True,
    )
    execute.files[f"{RECEIPT_DIR}/{historical['receipt_id']}.json"] = json.dumps(
        historical,
        sort_keys=True,
    )

    assert read_receipt(execute, current["receipt_id"]) == current
    assert read_receipt(execute, foreign["receipt_id"]) is None

    unknown = {**foreign, "schema_version": 3}
    execute.files[f"{RECEIPT_DIR}/{foreign['receipt_id']}.json"] = json.dumps(
        unknown,
        sort_keys=True,
    )
    assert read_receipt(execute, current["receipt_id"]) is None


def test_historical_receipt_scope_rejects_unknown_v1_shape():
    current = receipt_for()
    forged_v1 = {
        "schema_version": 1,
        "receipt_id": "inv-maven-1-0000",
        "tool": "maven",
        "requested_action": "test",
        "effective_action": "test",
        "argv": "mvn --fail-at-end verify",
        "working_directory": "/workspace/proj",
        "exit_code": 0,
        "outcome": "completed",
        "report_delta": {"new": [], "changed": []},
        "future_claim": "must not be classified forensic",
    }
    execute = ContainerFS()
    assert write_receipt_result(execute, current).persisted is True
    execute.files[f"{RECEIPT_DIR}/{forged_v1['receipt_id']}.json"] = json.dumps(
        forged_v1,
        sort_keys=True,
    )

    assert read_receipt(execute, current["receipt_id"]) is None


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
        argv = "mvn --fail-at-end test"
        metadata = record_invocation(
            self.orchestrator.execute_command,
            tool="maven",
            attempt=1,
            requested_action="test",
            effective_action="test",
            argv=argv,
            working_directory=kwargs.get("working_directory") or "/workspace/proj",
            exit_code=self.exit_code,
            before={},
            after=self.after,
            requirements=WIRED_REQUIREMENTS,
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
    _publish_requirements(orchestrator)
    maven_tool = ReceiptWritingMavenTool(orchestrator, **runner)
    return BuildTool(orchestrator, maven_tool=maven_tool), orchestrator


def _publish_requirements(orchestrator):
    raw = json.dumps(WIRED_REQUIREMENTS, sort_keys=True, separators=(",", ":"))
    orchestrator.files[REQUIREMENTS_PATH] = raw
    authority = evidence_publication_authority_for(orchestrator)
    prior = authority.latest_head(BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID)
    authority.publish_revision(
        record_kind="build_requirements",
        record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
        logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
        raw=raw.encode("utf-8"),
        expected_previous_raw_sha256=(
            prior.raw_sha256 if prior else EVIDENCE_PUBLICATION_GENESIS_SHA256
        ),
    )


def test_the_facade_assesses_the_receipt_its_own_dispatch_minted():
    tool, orchestrator = _wired_build_tool()

    with build_action_context("envelope-000031", action="test"):
        result = tool.execute(action="test", working_directory="/workspace/proj")

    (contract,) = contracts_written(orchestrator.commands)
    (receipt,) = receipts_written(orchestrator.commands)
    (assessment,) = assessments_written(orchestrator.commands)
    assert result.succeeded
    assert contract["expected_observations"] == ["report_delta"]
    assert receipt["contract_id"] == contract["contract_id"]
    assert assessment["receipt_id"] == receipt["receipt_id"]
    assert assessment["typed_code"] == FALSIFIED
    assert f"{ASSESSMENT_DIR}/{assessment['assessment_id']}.json" in orchestrator.files


def test_the_facade_records_a_capability_absence_the_receipt_carries():
    orchestrator = ContainerFS(markers={"pom.xml"})
    report = "/workspace/proj/target/surefire-reports/TEST-a.xml"
    orchestrator.files[report] = (
        '<testcase classname="a" name="one"><skipped message="need llvm"/></testcase>'
    )
    tool, orchestrator = _wired_build_tool(orchestrator, after={report: "c" * 64})

    with build_action_context("envelope-000032", action="test"):
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

    _publish_requirements(orchestrator)
    tool = BuildTool(orchestrator, maven_tool=SilentTool())
    with build_action_context("envelope-000033", action="compile"):
        result = tool.execute(action="compile", working_directory="/workspace/proj")

    assert result.succeeded
    assert assessments_written(orchestrator.commands) == []


def test_an_assessment_failure_never_breaks_the_result():
    """Evidence collection owes the model a build result, not an exception."""

    class ExplodingReader(ContainerFS):
        def execute_command(self, command, **kwargs):
            if RECEIPT_DIR in command and (
                command.startswith("cat ") or "SAG_NAMED_JSON_RECORD_V1" in command
            ):
                raise RuntimeError("container is gone")
            return super().execute_command(command, **kwargs)

    tool, orchestrator = _wired_build_tool(ExplodingReader(markers={"pom.xml"}))

    with build_action_context("envelope-000034", action="test"):
        result = tool.execute(action="test", working_directory="/workspace/proj")

    assert result.succeeded
    assert assessments_written(orchestrator.commands) == []


def test_the_assessment_is_never_a_receipt_rewrite():
    """Spec §C4: the receipt is finalized once. The assessor writes NEXT to it."""
    tool, orchestrator = _wired_build_tool()

    with build_action_context("envelope-000035", action="test"):
        tool.execute(action="test", working_directory="/workspace/proj")

    assert len(receipts_written(orchestrator.commands)) == 1
    assert isinstance(
        assess_receipt(contract_for(), receipt_for(), current_fingerprints=CURRENT),
        ReceiptAssessment,
    )


def test_backstop_assesses_a_facade_external_receipt_once(
    bind_host_evidence_publication_authority,
):
    """A valid facade-external receipt with no verdict gets one backstop."""
    import json as _json

    from sag.agent.evidence_assessments import ensure_receipt_assessed
    from sag.agent.invocation_contracts import build_contract

    params = {
        "action": "compile",
        "working_directory": "/workspace/bigtop/bigtop-data-generators",
    }
    domain_id = "test:/workspace/bigtop/bigtop-data-generators"
    contract = build_contract(
        run_id="run-pytest",
        envelope_id="envelope-000009",
        tool="build",
        params=params,
        effective_tool="gradle",
        effective_action="compileJava",
        expected_cwd="/workspace/bigtop/bigtop-data-generators",
        expected_argv="--build-cache compileJava",
        execution_binding=ARGV_EXECUTION_BINDING,
        intent_source="controller",
        intent_id="intent-backstop-gradle",
        intent_domain_id=domain_id,
        intent_exact_params=params,
        action_fingerprint=action_fingerprint(
            domain_id=domain_id,
            tool="build",
            params=params,
        ),
        expected_observations=["artifact_or_report_delta"],
        direct_falsifiers=[
            {
                "predicate_id": "empty_delta_despite_success",
                "kind": "delta_empty_on_exit0",
            }
        ],
    )

    store = {
        "/workspace/.setup_agent/invocation_receipts/inv-gradle-1-0004.json": _json.dumps(
            {
                "schema_version": 2,
                "receipt_id": "inv-gradle-1-0004",
                "run_id": contract["run_id"],
                "contract_id": contract["contract_id"],
                "contract_hash": contract["contract_hash"],
                "execution_binding": ARGV_EXECUTION_BINDING,
                "tool": "gradle",
                "requested_action": "compileJava",
                "effective_action": "compileJava",
                "argv": "/workspace/bigtop/gradlew --build-cache compileJava",
                "working_directory": "/workspace/bigtop/bigtop-data-generators",
                "actual_cwd": "/workspace/bigtop/bigtop-data-generators",
                "exit_code": 0,
                "outcome": "completed",
                "compliance": "exact",
                "report_delta": {"new": [{"path": "/r.xml", "sha256": "ab" * 32}], "changed": []},
            }
        ),
        f"/workspace/.setup_agent/invocation_contracts/{contract['contract_id']}.json": _json.dumps(
            contract
        ),
    }
    written = {}
    atomic = FakeContainer()
    # Publication requires the run's one durable store binding first; the
    # atomic file layer is this test's container store.
    token = install_evidence_publication_authority(
        bind_host_evidence_publication_authority,
        orchestrator=atomic,
    )
    reset_evidence_publication_authority(token)

    def execute(command, **_kwargs):
        if "for file in " in command and "SAG_NAMED_JSON_RECORD_V1" in command:
            quoted_glob = command.partition(" in ")[2].partition("; do")[0]
            target = shlex.split(quoted_glob)[0]
            prefix = target[: -len("*.json")]
            records = [
                (path.rsplit("/", 1)[-1], body)
                for path, body in sorted(store.items())
                if path.startswith(prefix) and path.endswith(".json")
            ]
            return {
                "success": True,
                "exit_code": 0,
                "output": frame_named_json_record_stream(records),
            }
        if command.startswith("ls "):
            return {"success": True, "exit_code": 0, "output": "\n".join(written)}
        if command.startswith("cat "):
            for path, body in store.items():
                if path in command:
                    return {"success": True, "exit_code": 0, "output": body}
            return {"success": False, "exit_code": 1, "output": ""}
        tokens = _atomic_write_tokens(command)
        if tokens is not None:
            result = atomic.execute_command(command)
            if tokens[:3] == ["mv", "-f", "--"]:
                body = atomic.files[tokens[4]]
                payload = _json.loads(body)
                written[payload["assessment_id"] + ".json"] = body
            return result
        return {"success": True, "exit_code": 0, "output": ""}

    contract_raw = store[
        f"/workspace/.setup_agent/invocation_contracts/{contract['contract_id']}.json"
    ].encode("utf-8")
    receipt_raw = store[
        "/workspace/.setup_agent/invocation_receipts/inv-gradle-1-0004.json"
    ].encode("utf-8")
    bind_host_evidence_publication_authority.publish_bytes(
        record_kind="invocation_contract",
        record_id=contract["contract_id"],
        raw=contract_raw,
        contract_id=contract["contract_id"],
        contract_hash=contract["contract_hash"],
    )
    bind_host_evidence_publication_authority.publish_bytes(
        record_kind="invocation_receipt",
        record_id="inv-gradle-1-0004",
        raw=receipt_raw,
        contract_id=contract["contract_id"],
        contract_hash=contract["contract_hash"],
    )

    assert ensure_receipt_assessed(execute, "inv-gradle-1-0004") is True
    assert len(written) == 1
    body = _json.loads(next(iter(written.values())))
    # A facade-external project receipt has no harness-current pin tuple at
    # this backstop seam. Preserve it as evidence, but never promote it green.
    assert body["typed_code"] == "contract_binding_unknown"

    # second pass: already assessed => no-op
    assert ensure_receipt_assessed(execute, "inv-gradle-1-0004") is False
    assert len(written) == 1


def test_a_failed_node_with_a_dependency_reason_emits_the_distinct_code():
    """Spec §5 S2: after the LLVM rebuild made execution real, the NumPy
    failure must emit its OWN typed code so R2 can start from dependency
    metadata."""
    from sag.agent.evidence_assessments import dependency_incompatibilities

    receipt = {
        "receipt_id": "inv-python-3-0005",
        "testcase_outcomes": {
            "nodes": [
                {
                    "node_id": "tests.python.x.test_minimal_target_codegen_llvm#test_llvm_add_pipeline",
                    "status": "failed",
                    "reason": "ValueError: Could not convert T.float32 to a NumPy dtype",
                },
                {"node_id": "tests.python.x#test_cuda", "status": "skipped", "reason": "CUDA"},
            ]
        },
    }

    findings = dependency_incompatibilities(receipt)

    assert [f.typed_code for f in findings] == ["dependency_incompatible_numpy"]
    assert "test_llvm_add_pipeline" in findings[0].detail


def test_a_failed_node_without_a_matching_reason_emits_nothing():
    from sag.agent.evidence_assessments import dependency_incompatibilities

    receipt = {
        "receipt_id": "inv-x",
        "testcase_outcomes": {
            "nodes": [{"node_id": "a#b", "status": "failed", "reason": "assert 1 == 2"}]
        },
    }

    assert dependency_incompatibilities(receipt) == []
