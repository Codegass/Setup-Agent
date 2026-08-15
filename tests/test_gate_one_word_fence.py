# tests/test_gate_one_word_fence.py
"""Fix C fences — spec 2026-08-14 §3: one grader, one word.

camel §0.7 delivered *"validated outcome 'failed'"* to the model and sealed
`unknown`; polaris S10 embedded `success`/`green` in a `tool_result` and sealed
`unknown`/`unavailable` two events later. Both are the same statement seen from
two angles: `phase_tool` renders and embeds its grade, the engine emits the
`tool_result` and the observation, and only then does `_cap_unresolved_test_gate`
re-grade and seal a different object.

These fences reconstruct both shapes from a SYNTHETIC gate pair rather than from
the cap's trigger condition — Fix A already made several of the corpus triggers
unreachable, and the divergence is a property of the seal, not of the cap.
"""

import inspect
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from sag.agent.control_events import (
    CONTROL_EVENT_KINDS,
    ControlEvent,
    ControlEventSink,
    compact_control_value,
)
from sag.agent.evidence_state import RunEvidenceState
from sag.agent.loop_memory import LoopMemory
from sag.agent.phase_gates import (
    ClaimDisposition,
    GateControlDisposition,
    GateResult,
    ValidatorState,
    claim_identity,
    gate_observation_text,
)
from sag.agent.phase_machine import PhaseClaim, PhaseMachine, PhaseOutcome
from sag.agent.phase_transitions import PhaseTransitionPolicy
from sag.agent.react_engine import ReActEngine
from sag.agent.react_types import StepType
from sag.agent.replay import ControlReplayRunner, ReplayValidationError

FIXTURES = Path(__file__).parent / "fixtures" / "control_layer"

_STATES = {
    PhaseOutcome.SUCCESS: ValidatorState.GREEN,
    PhaseOutcome.PARTIAL: ValidatorState.PARTIAL,
    PhaseOutcome.FAILED: ValidatorState.RED,
    PhaseOutcome.UNKNOWN: ValidatorState.UNAVAILABLE,
}


def _claim(outcome=PhaseOutcome.FAILED, *, phase="build", signal="done"):
    return PhaseClaim(
        phase=phase,
        signal=signal,
        claimed_outcome=outcome,
        key_results="41/41 compiled",
    )


def _gate(outcome, *, claim=None, accepted=True, reason="scripted gate", code="", facts=None):
    return GateResult(
        accepted=accepted,
        validated_outcome=outcome,
        claim_disposition=(
            ClaimDisposition.CONFIRMED if accepted else ClaimDisposition.CONTRADICTED
        ),
        validator_state=_STATES[outcome],
        reason=reason,
        code=code,
        validated_facts=dict(facts or {}),
        claim=claim,
    )


def _sink(tmp_path):
    return ControlEventSink(
        tmp_path / "control_events.jsonl",
        clock=lambda: "2026-08-14T12:00:00Z",
        id_factory=lambda sequence: f"live-{sequence}",
    )


def _events(tmp_path):
    text = (tmp_path / "control_events.jsonl").read_text(encoding="utf-8")
    return [ControlEvent.model_validate_json(line) for line in text.splitlines()]


def _last(tmp_path, kind):
    rows = [event for event in _events(tmp_path) if event.kind == kind]
    assert rows, f"no {kind} event was emitted"
    return rows[-1]


def _engine(tmp_path, *, start_phase="build"):
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = PhaseMachine(start_phase=start_phase)
    engine.run_evidence_state = RunEvidenceState(run_id="gate-one-word")
    engine.transition_policy = PhaseTransitionPolicy()
    engine.control_event_sink = _sink(tmp_path)
    engine._repair_global_remaining = 2
    engine._repair_phase_remaining = {"test": 1, "build": 1}
    engine.steps = []
    engine.context_journal = None
    engine._phase_iterations = 12
    engine.config = SimpleNamespace(
        phase_min_floors={"analyze": 4, "build": 10, "test": 12, "report": 8},
        max_iterations=150,
        verbose=False,
    )
    engine.current_iteration = 10
    engine.context_manager = SimpleNamespace(
        update_task_status=lambda *a, **k: True,
        current_task_id=None,
    )
    engine.agent_logger = SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
    )
    engine.finalized_reasons = []
    engine._finalize_evidence = lambda reason: engine.finalized_reasons.append(reason)
    engine.loop_memory = LoopMemory()
    return engine


def _terminal_step(claim, gate):
    return SimpleNamespace(
        step_type=SimpleNamespace(value="action"),
        tool_name="phase",
        tool_result=SimpleNamespace(
            success=True,
            metadata={
                "phase_signal": claim.signal,
                "phase_claim": claim.to_metadata(),
                "gate_result": gate.to_metadata(),
            },
        ),
    )


def _rejected_phase_execution(claim, *, code, reason, disposition, owner="harness"):
    """A phase-tool rejection as `_execute_tool_step` hands it to the engine."""
    from sag.tools.phase_tool import PhaseTool

    return SimpleNamespace(
        call=SimpleNamespace(name="phase"),
        result=PhaseTool._rejected_claim_result(
            claim,
            code=code,
            reason=reason,
            control_disposition=disposition,
            blocker_owner=owner,
        ),
        observation_text="",
    )


def _guidance(engine):
    return [step.content for step in engine.steps if step.step_type is StepType.SYSTEM_GUIDANCE]


# ---------------------------------------------------------------------------
# §3 item 3 — one identity per grading
# ---------------------------------------------------------------------------
def test_the_same_grading_carries_the_same_decision_id():
    """The id is derived from the graded content, so replay and the live run
    name one statement identically without a random seed to record."""
    first = _gate(PhaseOutcome.FAILED)
    second = _gate(PhaseOutcome.FAILED)

    assert first.decision_id
    assert first.decision_id == second.decision_id


def test_a_different_word_is_a_different_decision():
    assert _gate(PhaseOutcome.FAILED).decision_id != _gate(PhaseOutcome.UNKNOWN).decision_id


def test_a_superseding_grading_names_the_one_it_replaces():
    delivered = _gate(PhaseOutcome.FAILED)
    sealed = _gate(PhaseOutcome.UNKNOWN).superseding(delivered)

    assert sealed.supersedes == delivered.decision_id
    assert sealed.decision_id != delivered.decision_id
    assert sealed.to_metadata()["supersedes"] == delivered.decision_id


def test_an_assessment_subject_is_not_mistakable_for_a_decision(tmp_path):
    """Two identity namespaces shared one `gate-` prefix.

    `decision_id` names a grading; the repair-assessment subject names the
    thing an assessment is ABOUT. Only their digest lengths told them apart, so
    any reader — or any future fence keyed on the prefix — read one as the
    other. Now the prefixes are disjoint in both directions, and replay derives
    the same subject the live mint does.
    """
    from sag.agent.phase_gates import GATE_ASSESSMENT_SUBJECT_PREFIX, GATE_DECISION_ID_PREFIX
    from sag.agent.replay import _repair_assessment_id_for_gate

    assert not GATE_ASSESSMENT_SUBJECT_PREFIX.startswith(GATE_DECISION_ID_PREFIX)
    assert not GATE_DECISION_ID_PREFIX.startswith(GATE_ASSESSMENT_SUBJECT_PREFIX)

    engine = _engine(tmp_path)
    claim = _claim(PhaseOutcome.FAILED)
    gate = _gate(
        PhaseOutcome.FAILED,
        claim=claim,
        accepted=False,
        reason="the build did not compile",
        code="build_red",
    )
    assessment = engine._gate_control_assessment(claim, gate)

    assert gate.decision_id.startswith(GATE_DECISION_ID_PREFIX)
    assert assessment.subject_id.startswith(GATE_ASSESSMENT_SUBJECT_PREFIX)
    assert not assessment.subject_id.startswith(GATE_DECISION_ID_PREFIX)

    event = engine._emit_control_gate(claim, gate)
    assert (
        _repair_assessment_id_for_gate(
            event.payload,
            phase_attempt_id=engine.phase_machine.current_attempt_id,
        )
        == assessment.assessment_id
    )


def test_a_gate_with_nothing_to_supersede_stays_absent_in_the_record():
    body = _gate(PhaseOutcome.FAILED).to_metadata()

    assert body["decision_id"]
    assert "supersedes" not in body


def test_a_mutated_grading_cannot_keep_the_identity_of_the_old_one():
    """`_prepare_rejected_completion` rewrites a gate's reason and disposition
    after grading. The id is derived, so the rewritten statement is renamed by
    construction rather than by whoever remembered to rename it."""
    gate = _gate(PhaseOutcome.FAILED, reason="the build did not compile")
    rewritten = replace(gate, reason="the build did not compile; context unpersisted")

    assert rewritten.decision_id != gate.decision_id
    assert rewritten.decision_id == GateResult.from_metadata(rewritten.to_metadata()).decision_id


def test_the_identity_survives_the_metadata_round_trip():
    gate = _gate(PhaseOutcome.PARTIAL, facts={"build.test_entry_ready": True})

    assert GateResult.from_metadata(gate.to_metadata()).decision_id == gate.decision_id


def test_an_archived_gate_without_an_id_rehydrates_to_the_same_one():
    """v1/v2 transcripts recorded no identity; deriving it from content means a
    replay of those bytes names the same decision the live run named."""
    gate = _gate(PhaseOutcome.FAILED)
    archived = gate.to_metadata()
    archived.pop("decision_id")

    assert GateResult.from_metadata(archived).decision_id == gate.decision_id


def test_the_sealed_event_carries_the_embedded_copy_byte_for_byte(tmp_path):
    """polaris S10: the embedded `gate_result` and the adjacent `gate_decision`
    said different things. They are now one serialization written to two sinks."""
    engine = _engine(tmp_path)
    claim = _claim(PhaseOutcome.SUCCESS)
    gate = _gate(PhaseOutcome.SUCCESS, claim=claim, code="test_execution_observed")

    engine._emit_control_gate(claim, gate)

    event = _last(tmp_path, "gate_decision")
    embedded = compact_control_value({"gate_result": gate.to_metadata()})["gate_result"]
    assert event.payload["gate_result"] == embedded
    assert event.payload["decision_id"] == gate.decision_id
    assert event.payload["expected_outcome"] == embedded["validated_outcome"]
    assert event.payload["expected_accepted"] == embedded["accepted"]
    assert event.payload["reason"] == embedded["reason"]
    assert event.payload["code"] == embedded["code"]


# ---------------------------------------------------------------------------
# §3 item 1 — the accepted path seals what it delivered
# ---------------------------------------------------------------------------
def test_the_camel_shape_cannot_be_sealed_without_the_revision(tmp_path):
    """A word already delivered to the model cannot be replaced by a silent
    second grading: the emitter refuses rather than seal the divergence."""
    engine = _engine(tmp_path)
    claim = _claim(PhaseOutcome.FAILED)
    delivered = _gate(PhaseOutcome.FAILED, claim=claim)
    engine._register_delivered_gate(claim, delivered)

    with pytest.raises(ValueError, match="delivered"):
        engine._emit_control_gate(claim, _gate(PhaseOutcome.UNKNOWN, claim=claim))


def test_a_revision_chain_without_the_observation_is_still_refused(tmp_path):
    """`supersedes` alone is bookkeeping. The model has to be told."""
    engine = _engine(tmp_path)
    claim = _claim(PhaseOutcome.FAILED)
    delivered = _gate(PhaseOutcome.FAILED, claim=claim)
    engine._register_delivered_gate(claim, delivered)
    revised = _gate(PhaseOutcome.UNKNOWN, claim=claim).superseding(delivered)

    with pytest.raises(ValueError, match="gate_outcome_revised"):
        engine._emit_control_gate(claim, revised)


def test_the_same_word_re_sealed_needs_no_revision(tmp_path):
    engine = _engine(tmp_path)
    claim = _claim(PhaseOutcome.FAILED)
    delivered = _gate(PhaseOutcome.FAILED, claim=claim)
    engine._register_delivered_gate(claim, delivered)

    engine._emit_control_gate(claim, delivered)

    assert _last(tmp_path, "gate_decision").payload["decision_id"] == delivered.decision_id


def test_a_capped_word_names_both_words_to_the_model_before_it_seals(tmp_path):
    """camel's shape driven through the real routing path: the cap may change
    the word, but only out loud."""
    engine = _engine(tmp_path)
    claim = _claim(PhaseOutcome.FAILED)
    delivered = _gate(PhaseOutcome.FAILED, claim=claim, code="tests_not_executed")
    capped = _gate(
        PhaseOutcome.UNKNOWN,
        claim=claim,
        reason="test coordinates remained unavailable after the one bounded survey refresh",
        code="test_candidate_resolution_unavailable",
    )
    engine._cap_unresolved_test_gate = lambda claim_, gate_, **_: capped

    engine._handle_phase_signals([_terminal_step(claim, delivered)])

    revision = _last(tmp_path, "gate_outcome_revised")
    assert revision.payload["delivered_outcome"] == "failed"
    assert revision.payload["revised_outcome"] == "unknown"
    assert revision.payload["delivered_decision_id"] == delivered.decision_id
    assert revision.payload["code"] == "test_candidate_resolution_unavailable"

    text = revision.payload["observation_text"]
    assert "failed" in text and "unknown" in text
    assert any(text in message for message in _guidance(engine))

    sealed = _last(tmp_path, "gate_decision")
    assert sealed.payload["supersedes"] == delivered.decision_id
    assert sealed.payload["decision_id"] == revision.payload["revised_decision_id"]
    assert sealed.payload["expected_outcome"] == "unknown"


def test_an_acceptance_capped_to_a_rejection_is_not_a_logger_warning(tmp_path):
    """react_engine.py:2811 sealed a REJECTION for a claim the model had been
    told was ACCEPTED, behind a bare warning and `return None`. Never fired live;
    constructible today; the strictly worse form of the camel shape."""
    engine = _engine(tmp_path)
    claim = _claim(PhaseOutcome.SUCCESS)
    delivered = _gate(PhaseOutcome.SUCCESS, claim=claim)
    rejected = _gate(
        PhaseOutcome.UNKNOWN,
        claim=claim,
        accepted=False,
        reason="test coordinates remained unavailable",
        code="test_candidate_resolution_unavailable",
    )
    engine._cap_unresolved_test_gate = lambda claim_, gate_, **_: rejected

    engine._handle_phase_signals([_terminal_step(claim, delivered)])

    revision = _last(tmp_path, "gate_outcome_revised")
    assert revision.payload["delivered_accepted"] is True
    assert revision.payload["revised_accepted"] is False
    assert any(revision.payload["observation_text"] in message for message in _guidance(engine))
    assert _last(tmp_path, "gate_decision").payload["expected_accepted"] is False


def test_a_cap_that_only_refines_the_reason_chains_without_shouting(tmp_path):
    """Two gradings in sequence carry distinct ids and the later names the
    earlier (§3 item 3) — but the model is only interrupted when the WORD moved."""
    engine = _engine(tmp_path)
    claim = _claim(PhaseOutcome.FAILED)
    delivered = _gate(PhaseOutcome.FAILED, claim=claim, code="tests_not_executed")
    refined = _gate(
        PhaseOutcome.FAILED,
        claim=claim,
        reason="the harness-owned test action produced no candidate-bound runner receipt",
        code="forced_test_attempt_nonreceipt",
    )
    engine._cap_unresolved_test_gate = lambda claim_, gate_, **_: refined

    engine._handle_phase_signals([_terminal_step(claim, delivered)])

    sealed = _last(tmp_path, "gate_decision")
    assert sealed.payload["supersedes"] == delivered.decision_id
    assert sealed.payload["decision_id"] != delivered.decision_id
    assert not [event for event in _events(tmp_path) if event.kind == "gate_outcome_revised"]


def test_a_retry_of_the_same_claim_is_a_second_grading_not_a_persist_failure(tmp_path):
    """The cap seals a revision and leaves the attempt OPEN, so the model is
    free to dispatch a test and re-issue the identical claim. The phase tool
    rejects the retry, and that rejection is a word the model reads — not a
    grading slipped in behind the delivered one. `gate_decision_persist_failed`
    is reserved for a decision that could not be made durable (spec §4)."""
    engine = _engine(tmp_path, start_phase="test")
    claim = _claim(PhaseOutcome.SUCCESS, phase="test")
    delivered = _gate(PhaseOutcome.SUCCESS, claim=claim, code="test_execution_observed")
    capped = _gate(
        PhaseOutcome.UNKNOWN,
        claim=claim,
        accepted=False,
        reason="test coordinates remained unavailable after the one bounded survey refresh",
        code="test_candidate_resolution_unavailable",
    )
    engine._cap_unresolved_test_gate = lambda claim_, gate_, **_: capped
    engine._handle_phase_signals([_terminal_step(claim, delivered)])
    assert not engine.phase_machine.is_complete

    execution = _rejected_phase_execution(
        claim,
        code="WAIT_REQUIRED",
        reason="a dispatched runner job is still live; its receipt is not settled",
        disposition=GateControlDisposition.WAIT_REQUIRED,
    )
    prepared = engine._prepare_rejected_completion(execution)
    engine._apply_rejected_completion_control(prepared)

    assert getattr(engine, "_fatal_harness_control_failure", None) is None
    assert _last(tmp_path, "gate_decision").payload["decision_id"] == prepared.gate.decision_id


def test_a_declining_branch_states_the_revision_now_and_parks_nothing(tmp_path):
    """The cap speaks in the window the rejection lands in.

    `_deliver_gate_observation` also PARKS its text so a window reset cannot
    swallow it — but the cap branch routes no phase decision at all, so nothing
    resets and the parked copy has no window of its own to reach. Left there it
    waits for the next transition, which is how a superseded `unknown` was
    re-stated as CRITICAL GUIDANCE inside the phase that followed a sealed
    `success`.
    """
    engine = _engine(tmp_path, start_phase="test")
    claim = _claim(PhaseOutcome.SUCCESS, phase="test")
    delivered = _gate(PhaseOutcome.SUCCESS, claim=claim, code="test_execution_observed")
    capped = _gate(
        PhaseOutcome.UNKNOWN,
        claim=claim,
        accepted=False,
        reason="test coordinates remained unavailable after the one bounded survey refresh",
        code="test_candidate_resolution_unavailable",
    )
    engine._cap_unresolved_test_gate = lambda claim_, gate_, **_: capped

    engine._handle_phase_signals([_terminal_step(claim, delivered)])

    revision = _last(tmp_path, "gate_outcome_revised").payload["observation_text"]
    assert any(revision in message for message in _guidance(engine))
    assert engine._pending_window_observation() is None


def test_a_superseded_word_cannot_be_re_stated_in_the_window_that_follows(tmp_path):
    """A parked word survives the reset only while it is still what the record
    says. The park names its decision, so a later grading of the same attempt
    retires it instead of letting it speak after it was replaced."""
    engine = _engine(tmp_path, start_phase="test")
    claim = _claim(PhaseOutcome.SUCCESS, phase="test")
    first = _gate(PhaseOutcome.FAILED, claim=claim, code="tests_not_executed")
    engine._seal_engine_gate(claim, first)
    assert engine._pending_window_observation() is not None

    engine._seal_engine_gate(claim, first)
    later = _gate(
        PhaseOutcome.SUCCESS,
        claim=claim,
        code="test_execution_observed",
        reason="executed 12 of 12 discovered · 12 passed, 0 failed, 0 skipped",
    ).superseding(first)
    engine._emit_gate_outcome_revised(claim, first, later)
    engine._emit_control_gate(claim, later)

    assert engine._pending_window_observation() is None


def test_the_record_names_the_claim_by_the_key_the_live_registry_used(tmp_path):
    """Delivered word and sealed decision carry ONE name for the claim, so an
    offline reader pairs them exactly as `_delivered_gates` does. The name is
    taken before the record is bounded: `phase_claim` is truncated like every
    other recorded string, and a digest of the truncated copy would name a
    claim the live run never graded."""
    engine = _engine(tmp_path, start_phase="test")
    claim = replace(_claim(PhaseOutcome.SUCCESS, phase="test"), key_results="compiled. " * 90)
    execution = _rejected_phase_execution(
        claim,
        code="WAIT_REQUIRED",
        reason="a dispatched runner job is still live; its receipt is not settled",
        disposition=GateControlDisposition.WAIT_REQUIRED,
    )
    prepared = engine._prepare_rejected_completion(execution)
    engine._apply_rejected_completion_control(prepared)

    recorded = ReActEngine._control_result_projection(execution.result)["metadata"]
    assert len(recorded["phase_claim"]["key_results"]) < len(claim.key_results)
    assert recorded["phase_claim_sha256"] == claim_identity(claim)
    assert _last(tmp_path, "gate_decision").payload["claim_sha256"] == claim_identity(claim)
    assert claim_identity(claim) in engine._delivered_gates()


def test_the_rejection_the_model_read_is_the_word_a_later_seal_must_supersede(tmp_path):
    """Registering the rejection keeps the fence pointed at the same target:
    an engine re-grading of THAT claim still has to name the word it replaces."""
    engine = _engine(tmp_path, start_phase="test")
    claim = _claim(PhaseOutcome.SUCCESS, phase="test")
    execution = _rejected_phase_execution(
        claim,
        code="WAIT_REQUIRED",
        reason="a dispatched runner job is still live; its receipt is not settled",
        disposition=GateControlDisposition.WAIT_REQUIRED,
    )
    prepared = engine._prepare_rejected_completion(execution)
    engine._apply_rejected_completion_control(prepared)

    with pytest.raises(ValueError, match="delivered"):
        engine._emit_control_gate(claim, _gate(PhaseOutcome.FAILED, claim=claim))


# ---------------------------------------------------------------------------
# §3 item 2 — engine-generated decisions render their own observation
# ---------------------------------------------------------------------------
def test_every_engine_generated_close_seals_through_the_delivering_sink():
    """`_enforce_phase_floors`, the control-persist close, the no-progress close
    and the loop close each sealed a graded outcome behind a `logger.warning`.
    One sink now emits AND delivers, so the discipline is structural."""
    for name in (
        "_enforce_phase_floors",
        "_close_phase_for_control_persist_exhaustion",
        "_close_phase_for_agent_no_progress",
        "_close_phase_for_loop",
    ):
        source = inspect.getsource(getattr(ReActEngine, name))
        assert "_seal_engine_gate(" in source, name
        assert "self._emit_control_gate(" not in source, name


def test_the_loop_close_states_the_word_it_seals(tmp_path):
    engine = _engine(tmp_path)
    engine._missing_required_test_attempt = lambda *_a, **_k: None
    execution = SimpleNamespace(
        result=SimpleNamespace(
            output_ref="output_build_1",
            evidence_refs=("output_build_1",),
            refs=(),
        )
    )
    decision = SimpleNamespace(failure_ref="output_build_1")

    assert engine._close_phase_for_loop(decision, execution) is True

    sealed = _last(tmp_path, "gate_decision")
    assert sealed.payload["expected_outcome"] == "failed"
    spoken = [message for message in _guidance(engine) if "PHASE_CLOSED" in message]
    assert spoken, "the harness closed the phase without telling the model"
    assert "'failed'" in spoken[-1]
    assert sealed.payload["reason"] in spoken[-1]


# ---------------------------------------------------------------------------
# §3 — one renderer
# ---------------------------------------------------------------------------
def test_the_accepted_observation_is_rendered_from_the_gate_not_assembled():
    gate = _gate(PhaseOutcome.PARTIAL)

    assert gate_observation_text(gate, phase="build", origin="terminal_claim") == (
        "Phase 'build' terminal claim accepted with validated outcome "
        "'partial'. Awaiting engine routing."
    )


def test_a_revision_render_must_name_the_word_it_replaces():
    with pytest.raises(ValueError, match="supersede"):
        gate_observation_text(
            _gate(PhaseOutcome.UNKNOWN),
            phase="test",
            origin="outcome_revision",
        )


def test_the_phase_tool_renders_the_accepted_word_through_the_shared_renderer():
    from sag.tools.phase_tool import PhaseTool

    source = inspect.getsource(PhaseTool.execute)
    assert "gate_observation_text(" in source
    assert "terminal claim accepted with validated outcome" not in source


# ---------------------------------------------------------------------------
# §3 — schema ripple
# ---------------------------------------------------------------------------
def test_the_revision_kind_is_appended_never_inserted():
    # An absolute position, not a tail slice: kinds are appended after this
    # one (`turn_record`), and a `[-1]` assertion would read every honest
    # append as an insertion.
    assert CONTROL_EVENT_KINDS[22] == "gate_outcome_revised"
    assert CONTROL_EVENT_KINDS[:10] == (
        "planner_response",
        "scheduler_decision",
        "action_envelope",
        "forced_action",
        "tool_result",
        "validator_observation",
        "gate_decision",
        "phase_transition",
        "loop_decision",
        "evidence_close",
    )


def test_a_lost_revision_fails_closed_like_a_lost_gate():
    from sag.agent.react_engine import _STRICT_LINEAGE_CONTROL_KINDS

    assert "gate_outcome_revised" in _STRICT_LINEAGE_CONTROL_KINDS


# ---------------------------------------------------------------------------
# §3 item 4 — replay refuses the divergence it used to walk past
# ---------------------------------------------------------------------------
def _paramiko_rows():
    return [
        json.loads(line)
        for line in (FIXTURES / "paramiko.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def _write(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


_BUILD_CLAIM = claim_identity(
    PhaseClaim(
        phase="build",
        signal="done",
        claimed_outcome=PhaseOutcome.SUCCESS,
        key_results="41/41 Paramiko source files compiled.",
    )
)
_OTHER_CLAIM = claim_identity(
    PhaseClaim(
        phase="build",
        signal="blocked",
        claimed_outcome=PhaseOutcome.UNKNOWN,
        reason="the engine closed this attempt at the floor",
    )
)


def _delivered_body():
    return {
        "decision_id": "gate-delivered",
        "accepted": True,
        "validated_outcome": "failed",
        "claim_disposition": "confirmed",
        "validator_state": "red",
        "control_disposition": "terminal_claimable",
        "blocker_owner": "none",
        "reason": "tests were not executed",
        "evidence_refs": [],
        "suggestions": [],
        "code": "tests_not_executed",
        "validated_facts": {},
    }


def _deliver_word(rows, *, claim_sha256, signal="done"):
    """Hand the model a graded word on the first tool result of the stream."""
    for row in rows:
        if row.get("kind") == "tool_result":
            metadata = row["payload"]["result"]["metadata"]
            metadata["gate_result"] = _delivered_body()
            metadata["phase_claim_sha256"] = claim_sha256
            if signal:
                metadata["phase_signal"] = signal
            return row
    raise AssertionError("the fixture carries no tool result")


def _seal(rows, *, claim_sha256, decision_id="gate-sealed"):
    for row in rows:
        if row.get("kind") == "gate_decision":
            row["payload"]["decision_id"] = decision_id
            row["payload"]["claim_sha256"] = claim_sha256
            return row
    raise AssertionError("the fixture carries no gate decision")


def test_replay_refuses_a_supersedes_that_names_no_earlier_decision(tmp_path):
    rows = _paramiko_rows()
    for row in rows:
        if row.get("kind") == "gate_decision":
            row["payload"]["decision_id"] = "gate-second"
            row["payload"]["supersedes"] = "gate-nobody-emitted-this"
            break
    transcript = _write(tmp_path / "paramiko.jsonl", rows)

    with pytest.raises(ReplayValidationError, match="supersedes"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_replay_refuses_a_seal_that_diverges_from_the_delivered_word(tmp_path):
    """The eight corpus sessions replayed clean because replay never compared
    the sealed word with the one the model was handed."""
    rows = _paramiko_rows()
    _deliver_word(rows, claim_sha256=_BUILD_CLAIM)
    _seal(rows, claim_sha256=_BUILD_CLAIM)
    transcript = _write(tmp_path / "paramiko.jsonl", rows)

    with pytest.raises(ReplayValidationError, match="delivered"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_replay_lets_a_close_of_another_claim_stand(tmp_path):
    """The live refusal is keyed by (open attempt, claim): an engine close that
    grades its OWN claim owes nothing to a word delivered for a different one.
    Pairing the two by adjacency condemned honest transcripts — the delivered
    word here is simply never claimed by any decision."""
    rows = _paramiko_rows()
    _deliver_word(rows, claim_sha256=_BUILD_CLAIM)
    _seal(rows, claim_sha256=_OTHER_CLAIM)
    transcript = _write(tmp_path / "paramiko.jsonl", rows)

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert result.gate_decision_count == 2


def test_replay_reads_a_rejected_grading_as_a_delivered_word(tmp_path):
    """A rejection carries its gate into the observation the model reads, so it
    is delivered even though it carries no `phase_signal` (phase_tool.py:192)."""
    rows = _paramiko_rows()
    _deliver_word(rows, claim_sha256=_BUILD_CLAIM, signal="")
    _seal(rows, claim_sha256=_BUILD_CLAIM)
    transcript = _write(tmp_path / "paramiko.jsonl", rows)

    with pytest.raises(ReplayValidationError, match="delivered"):
        ControlReplayRunner.offline(verify_expected=False).run(transcript)


def test_replay_accepts_the_named_revision(tmp_path):
    rows = _paramiko_rows()
    _deliver_word(rows, claim_sha256=_BUILD_CLAIM)
    revised = None
    for index, row in enumerate(rows):
        if row.get("kind") == "gate_decision":
            row["payload"]["decision_id"] = "gate-sealed"
            row["payload"]["claim_sha256"] = _BUILD_CLAIM
            row["payload"]["supersedes"] = "gate-delivered"
            revised = (
                index,
                {
                    "sequence": row["sequence"],
                    "kind": "gate_outcome_revised",
                    "payload": {
                        "phase": row["payload"]["phase"],
                        "delivered_decision_id": "gate-delivered",
                        "revised_decision_id": "gate-sealed",
                        "delivered_outcome": "failed",
                        "revised_outcome": row["payload"]["expected_outcome"],
                        "delivered_accepted": True,
                        "revised_accepted": row["payload"]["expected_accepted"],
                        "reason": row["payload"]["reason"],
                        "code": row["payload"].get("code") or "gate_outcome_revised",
                        "observation_text": "the sealed word changed",
                    },
                    "source": row["source"],
                },
            )
            break
    rows.insert(revised[0], revised[1])
    for sequence, row in enumerate(rows[1:], 1):
        row["sequence"] = sequence
    transcript = _write(tmp_path / "paramiko.jsonl", rows)

    result = ControlReplayRunner.offline(verify_expected=False).run(transcript)

    assert result.gate_decision_count == 2
