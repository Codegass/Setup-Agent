"""WS2: phase gates consume the orthogonal job-obligation lifecycle."""

import json

import pytest

from test_job_settlement import DOCKER_EXEC_ID, _obligation, _orchestrator, _with_obligation

from sag.agent import phase_gates
from sag.agent.job_obligations import (
    PROCESS_TERMINAL,
    SETTLEMENT_PENDING,
    SETTLEMENT_SETTLED,
    SETTLEMENT_UNPERSISTED,
    OBLIGATION_DIR,
    read_obligations,
    reconcile_job_obligations,
)
from sag.agent.invocation_receipts import RECEIPT_DIR
from sag.agent.phase_gates import (
    JOB_BARRIER_FACT,
    JOB_INTEGRITY_FACT,
    TERMINAL_UNPERSISTED_FACT,
    ClaimDisposition,
    GateControlDisposition,
    GateResult,
    ValidatorState,
    _ValidatorObservation,
    check_phase_claim,
    check_phase_done,
)
from sag.agent.phase_machine import PhaseClaim, PhaseOutcome


def _claim(outcome: str = "success") -> PhaseClaim:
    return PhaseClaim(
        phase="build",
        signal="done",
        claimed_outcome=PhaseOutcome(outcome),
        key_results="bounded test claim",
    )


def _green_inspection(monkeypatch):
    calls = []

    def inspect(phase, validator, orchestrator, project_name):
        calls.append((phase, project_name))
        return _ValidatorObservation(
            ValidatorState.GREEN,
            reason="physical build evidence is green",
            code="build_green",
        )

    monkeypatch.setattr(phase_gates, "_inspect_phase_evidence", inspect)
    return calls


def test_running_job_returns_wait_without_inspecting_project_evidence(monkeypatch):
    calls = _green_inspection(monkeypatch)
    orchestrator = _with_obligation(_orchestrator(exit_code=None))

    gate = check_phase_claim("build", _claim(), None, orchestrator, "demo")

    assert calls == []
    assert gate.accepted is False
    assert gate.validator_state is ValidatorState.UNAVAILABLE
    assert gate.validated_outcome is PhaseOutcome.UNKNOWN
    assert gate.disposition is ClaimDisposition.CONTRADICTED
    assert gate.control_disposition is GateControlDisposition.WAIT_REQUIRED
    assert gate.code == "job_controller_barrier"
    assert gate.validated_facts[JOB_BARRIER_FACT] == [
        {"job_id": "373f63e5a0a4", "state": "running"}
    ]
    assert "project claim was graded" in gate.reason


def test_settlement_pending_is_a_controller_barrier(monkeypatch):
    calls = _green_inspection(monkeypatch)
    orchestrator = _with_obligation(
        _orchestrator(exit_code=None),
        process_state=PROCESS_TERMINAL,
        settlement_state=SETTLEMENT_PENDING,
        terminal_exit_code=0,
        terminal_marker_ref=f"docker-exec:{DOCKER_EXEC_ID}",
        terminal_observed_at="2026-08-08T12:00:00Z",
    )

    probe = check_phase_done("build", None, orchestrator, "demo", sealed=True)

    assert calls == []
    assert probe["ok"] is False
    assert probe["validator_state"] == "unavailable"
    assert probe["control_disposition"] == "wait_required"
    assert probe["validated_facts"][JOB_BARRIER_FACT] == [
        {"job_id": "373f63e5a0a4", "state": "settlement_pending"}
    ]


def test_durable_terminal_unpersisted_survives_restart_and_caps_success(monkeypatch):
    calls = _green_inspection(monkeypatch)
    original = _with_obligation(
        _orchestrator(exit_code="0"),
        process_state=PROCESS_TERMINAL,
        settlement_state=SETTLEMENT_UNPERSISTED,
        terminal_exit_code=0,
        terminal_marker_ref=f"docker-exec:{DOCKER_EXEC_ID}",
        terminal_observed_at="2026-08-08T12:00:00Z",
        settlement_attempts=2,
        attempted_receipt_id="inv-gradle-1-0042",
        receipt_persistence_code="transport_publish_failed",
    )
    # A fresh controller has no in-memory settlement state, but it reconnects
    # to the same one-run/one-container evidence store.  Reuse that store
    # identity while exercising recovery solely from the durable ledger.
    orchestrator = original

    gate = check_phase_claim("build", _claim("success"), None, orchestrator, "demo")

    assert calls == [("build", "demo")]
    assert gate.accepted is False
    assert gate.validated_outcome is PhaseOutcome.PARTIAL
    assert gate.validator_state is ValidatorState.PARTIAL
    assert gate.control_disposition is GateControlDisposition.HARNESS_RECOVERY_REQUIRED
    assert gate.code == "evidence_unpersisted"
    assert gate.validated_facts[TERMINAL_UNPERSISTED_FACT][0] == {
        "job_id": "373f63e5a0a4",
        "exit_code": 0,
        "attempted_receipt_id": "inv-gradle-1-0042",
        "persistence_code": "transport_publish_failed",
        "attempt_count": 2,
        "obligation_ref": ("/workspace/.setup_agent/job_obligations/373f63e5a0a4.json"),
        "log_ref": "/tmp/sag_jobs/373f63e5a0a4.log",
    }
    assert "terminal but its receipt was not persisted" in gate.reason
    assert "wait for the terminal result" not in " ".join(gate.suggestions)
    assert gate.suggestions == ()
    assert "phase(" not in " ".join(gate.suggestions)


def test_terminal_unpersisted_allows_an_honest_partial_close(monkeypatch):
    _green_inspection(monkeypatch)
    orchestrator = _with_obligation(
        _orchestrator(exit_code="0"),
        process_state=PROCESS_TERMINAL,
        settlement_state=SETTLEMENT_UNPERSISTED,
        terminal_exit_code=0,
        terminal_marker_ref=f"docker-exec:{DOCKER_EXEC_ID}",
        terminal_observed_at="2026-08-08T12:00:00Z",
        settlement_attempts=2,
        attempted_receipt_id="inv-gradle-1-0042",
        receipt_persistence_code="transport_publish_failed",
    )

    gate = check_phase_claim("build", _claim("partial"), None, orchestrator, "demo")

    assert gate.accepted is True
    assert gate.validated_outcome is PhaseOutcome.PARTIAL
    assert gate.disposition is ClaimDisposition.CONFIRMED
    assert gate.control_disposition is GateControlDisposition.HARNESS_RECOVERY_REQUIRED
    assert gate.code == "evidence_unpersisted"


def test_settled_terminal_job_does_not_change_the_gate(monkeypatch):
    calls = _green_inspection(monkeypatch)
    orchestrator = _with_obligation(_orchestrator(exit_code="0"))
    settlement = reconcile_job_obligations(orchestrator)

    assert len(settlement.settlements) == 1
    assert settlement.integrity_failures == ()

    gate = check_phase_claim("build", _claim("success"), None, orchestrator, "demo")

    assert calls == [("build", "demo")]
    assert gate.accepted is True
    assert gate.validated_outcome is PhaseOutcome.SUCCESS
    assert gate.control_disposition is GateControlDisposition.TERMINAL_CLAIMABLE
    assert JOB_BARRIER_FACT not in gate.validated_facts
    assert TERMINAL_UNPERSISTED_FACT not in gate.validated_facts


@pytest.mark.parametrize("mutation", ["missing", "malformed", "mismatch"])
def test_sealed_gate_requires_the_settled_receipt_identity_witness(monkeypatch, mutation):
    calls = _green_inspection(monkeypatch)
    orchestrator = _with_obligation(_orchestrator(exit_code="0"))
    settlement = reconcile_job_obligations(orchestrator)
    receipt_id = settlement.settlements[0].receipt_id
    receipt_path = f"{RECEIPT_DIR}/{receipt_id}.json"
    if mutation == "missing":
        orchestrator.filesystem.files.pop(receipt_path)
    elif mutation == "malformed":
        orchestrator.filesystem.files[receipt_path] = "{not json"
    else:
        receipt = json.loads(orchestrator.filesystem.files[receipt_path])
        receipt["run_id"] = "forged-run"
        orchestrator.filesystem.files[receipt_path] = json.dumps(receipt, sort_keys=True)
    expected = f"373f63e5a0a4:receipt_ledger_unreadable"

    gate = check_phase_claim("build", _claim("failed"), None, orchestrator, "demo", sealed=True)

    assert calls == []
    assert gate.accepted is False
    assert gate.code == "job_evidence_integrity"
    assert gate.control_disposition is GateControlDisposition.HARNESS_RECOVERY_REQUIRED
    assert gate.validated_facts[JOB_INTEGRITY_FACT] == [expected]


def test_terminal_unpersisted_does_not_mask_missing_analysis_facts(monkeypatch):
    orchestrator = _with_obligation(
        _orchestrator(exit_code="0"),
        process_state=PROCESS_TERMINAL,
        settlement_state=SETTLEMENT_UNPERSISTED,
        terminal_exit_code=0,
        terminal_marker_ref=f"docker-exec:{DOCKER_EXEC_ID}",
        terminal_observed_at="2026-08-08T12:00:00Z",
        settlement_attempts=2,
        attempted_receipt_id="inv-gradle-1-0042",
        receipt_persistence_code="transport_publish_failed",
    )
    monkeypatch.setattr(
        phase_gates,
        "_inspect_phase_evidence",
        lambda *_args, **_kwargs: _ValidatorObservation(
            ValidatorState.UNAVAILABLE,
            reason="survey manifest missing",
            code="analysis_facts_missing",
            control_disposition=GateControlDisposition.HARNESS_RECOVERY_REQUIRED,
            blocker_owner="harness",
        ),
    )
    claim = PhaseClaim(
        phase="analyze",
        signal="done",
        claimed_outcome=PhaseOutcome.FAILED,
        key_results="facts unavailable",
    )

    gate = check_phase_claim("analyze", claim, None, orchestrator, "demo")

    assert gate.accepted is False
    assert gate.code == "analysis_facts_missing"
    assert gate.validator_state is ValidatorState.UNAVAILABLE
    assert gate.control_disposition is GateControlDisposition.HARNESS_RECOVERY_REQUIRED
    assert TERMINAL_UNPERSISTED_FACT in gate.validated_facts
    assert "run.physical_validator_state" not in gate.validated_facts


def test_schema_v1_unsettled_record_is_forensic_not_a_live_barrier():
    source = _obligation()
    legacy = {
        key: source[key]
        for key in (
            "schema_version",
            "job_id",
            "tool",
            "attempt",
            "requested_action",
            "effective_action",
            "argv",
            "working_directory",
            "before",
            "log_path",
            "exit_code_path",
            "settled_receipt_id",
            "dispatch_sequence",
        )
    }
    legacy["schema_version"] = 1
    orchestrator = _orchestrator(exit_code=None)
    from sag.agent.job_obligations import write_obligation

    assert write_obligation(orchestrator.execute_command, legacy) is False
    orchestrator.filesystem.files[f"{OBLIGATION_DIR}/{legacy['job_id']}.json"] = json.dumps(
        legacy,
        sort_keys=True,
    )
    assert read_obligations(orchestrator) == []


def test_malformed_daemon_terminal_is_harness_integrity_not_project_failure(monkeypatch):
    calls = _green_inspection(monkeypatch)
    orchestrator = _with_obligation(_orchestrator(exit_code=None))
    orchestrator.set_detached_terminal_state(
        DOCKER_EXEC_ID,
        "finished",
        "not-an-integer",
    )

    gate = check_phase_claim("build", _claim("failed"), None, orchestrator, "demo")

    assert calls == []
    assert gate.accepted is False
    assert gate.validated_outcome is PhaseOutcome.UNKNOWN
    assert gate.validator_state is ValidatorState.UNAVAILABLE
    assert gate.control_disposition is GateControlDisposition.HARNESS_RECOVERY_REQUIRED
    assert gate.code == "job_evidence_integrity"
    assert gate.validated_facts[JOB_INTEGRITY_FACT] == ["373f63e5a0a4:terminal_authority_malformed"]
    assert "project evidence was not inspected" in gate.reason
    assert "project failure" in gate.suggestions[0]


def test_unreadable_ledger_requires_harness_recovery_without_project_probe(monkeypatch):
    calls = _green_inspection(monkeypatch)

    class UnreadableLedger:
        @staticmethod
        def execute_command(command, **kwargs):
            return {
                "success": False,
                "exit_code": -1,
                "dispatch_status": "container_unavailable",
                "output": "ledger transport unavailable",
            }

    gate = check_phase_claim("build", _claim("failed"), None, UnreadableLedger(), "demo")

    assert calls == []
    assert gate.accepted is False
    assert gate.validated_outcome is PhaseOutcome.UNKNOWN
    assert gate.control_disposition is GateControlDisposition.HARNESS_RECOVERY_REQUIRED
    assert gate.code == "job_evidence_integrity"
    assert gate.validated_facts[JOB_INTEGRITY_FACT] == ["ledger_unreadable"]


def test_old_gate_metadata_defaults_to_terminal_claimable():
    metadata = GateResult(
        accepted=True,
        validated_outcome=PhaseOutcome.SUCCESS,
        claim_disposition=ClaimDisposition.CONFIRMED,
        validator_state=ValidatorState.GREEN,
    ).to_metadata()
    metadata.pop("control_disposition")

    restored = GateResult.from_metadata(metadata)

    assert restored.control_disposition is GateControlDisposition.TERMINAL_CLAIMABLE
