# tests/test_transient_failure_containment.py
"""A blip is not a verdict (spec 2026-08-13, task #45).

D2's sealed phase records convict two run-abort paths. tapestry-5: one
`litellm.InternalServerError` aborted the run in analyze, two turns after the
model had recovered from a poisoned search result on its own. rocketmq:
`repair_assessment_persist_failed` aborted the run while the model's blocked
claim ("no usable Maven toolchain, no wrapper") was factually correct.

Contract under test: transient provider errors retry 3 times with injected
backoff and unknown classes stay deterministic (fail-closed); persist
exhaustion converts to an honest blocked phase close with report delivery,
never a run abort. The two integrity-failure families keep today's abort
semantics untouched (their fences live elsewhere and stay green).
"""

import litellm
import pytest

from sag.agent.react_engine import ReActEngine

# ---------------------------------------------------------------------------
# Classification: which provider errors earn a retry
# ---------------------------------------------------------------------------


def _classify(exc) -> bool:
    return ReActEngine._is_transient_provider_error(exc)


def _provider_error(cls, message="boom"):
    """litellm exception constructors vary; build defensively."""
    try:
        return cls(message=message, llm_provider="openai", model="gpt-5.4-mini")
    except TypeError:
        try:
            return cls(message)
        except TypeError:
            return cls.__new__(cls)


@pytest.mark.parametrize(
    "cls",
    [
        litellm.InternalServerError,
        litellm.ServiceUnavailableError,
        litellm.RateLimitError,
        litellm.APIConnectionError,
        litellm.Timeout,
    ],
)
def test_transient_provider_errors_are_retryable(cls):
    assert _classify(_provider_error(cls)) is True


@pytest.mark.parametrize(
    "exc",
    [
        # The wire-schema incident (2026-08-09) proved BadRequestError
        # reproduces byte-for-byte: retrying it is pure waste.
        _provider_error(litellm.BadRequestError),
        ValueError("not a provider error at all"),
        RuntimeError("unknown class stays deterministic, fail-closed"),
    ],
)
def test_deterministic_and_unknown_errors_never_retry(exc):
    assert _classify(exc) is False


# ---------------------------------------------------------------------------
# The retry loop: bounded, backoff-injected, honest on exhaustion
# ---------------------------------------------------------------------------


class _FlakyClient:
    def __init__(self, failures, turn="TURN"):
        self.failures = list(failures)
        self.turn = turn
        self.calls = 0

    def get_native_turn(self, messages, **kwargs):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return self.turn


def _engine_with(client):
    engine = ReActEngine.__new__(ReActEngine)
    engine.llm_client = client
    engine.agent_logger = __import__("loguru").logger
    return engine


def test_a_transient_error_is_retried_and_the_run_continues():
    client = _FlakyClient(
        [
            _provider_error(litellm.InternalServerError),
            _provider_error(litellm.InternalServerError),
        ]
    )
    engine = _engine_with(client)
    sleeps: list[float] = []

    turn = engine._native_turn_with_retry(["messages"], sleep=sleeps.append)

    assert turn == "TURN"
    assert client.calls == 3
    assert sleeps == [5.0, 15.0], "backoff is 5/15/45, injected, never real"


def test_exhaustion_reraises_with_the_attempt_count_named():
    client = _FlakyClient([_provider_error(litellm.InternalServerError)] * 4)
    engine = _engine_with(client)
    sleeps: list[float] = []

    with pytest.raises(litellm.InternalServerError):
        engine._native_turn_with_retry(["messages"], sleep=sleeps.append)

    assert client.calls == 4, "one original attempt plus exactly three retries"
    assert sleeps == [5.0, 15.0, 45.0]


def test_a_deterministic_error_aborts_at_once_with_zero_retries():
    client = _FlakyClient([_provider_error(litellm.BadRequestError)])
    engine = _engine_with(client)
    sleeps: list[float] = []

    with pytest.raises(litellm.BadRequestError):
        engine._native_turn_with_retry(["messages"], sleep=sleeps.append)

    assert client.calls == 1
    assert sleeps == []


# ---------------------------------------------------------------------------
# Persist exhaustion: an honest phase close, not a run abort
# ---------------------------------------------------------------------------


def _containment_engine():
    """The convergence harness, minus a control transport: exactly the state
    in which _install_repair_context fails with the transport-family code."""
    import sys

    sys.path.insert(0, "tests")
    from test_terminal_claim_convergence import _engine

    engine = _engine()
    engine.orchestrator = None  # resolve_control_execute -> None
    return engine


def _repair_required_rejection(engine):
    from sag.agent.phase_gates import (
        BlockerOwner,
        GateControlDisposition,
        PhaseClaim,
        PhaseOutcome,
        ValidatorState,
        validate_phase_claim,
    )
    from sag.tools.base import ToolResult
    from sag.agent.tool_orchestration import ToolCall, ToolExecution

    # Claimed SUCCESS against a RED validator: a genuinely rejected claim,
    # which is what routes through REPAIR_REQUIRED into the install attempt.
    claim = PhaseClaim(
        phase="build",
        signal="blocked",
        claimed_outcome=PhaseOutcome.SUCCESS,
        reason="no usable toolchain",
        evidence_refs=("output_probe",),
    )
    gate = validate_phase_claim(
        claim,
        ValidatorState.RED,
        reason="no terminal build attempt receipt",
        evidence_refs=("output_probe",),
        code="BUILD_ATTEMPT_REQUIRED",
        control_disposition=GateControlDisposition.REPAIR_REQUIRED,
        blocker_owner=BlockerOwner.UNKNOWN,
    )
    result = ToolResult.completed_failure(
        output=gate.reason,
        error=gate.reason,
        metadata={"phase_claim": claim.to_metadata(), "gate_result": gate.to_metadata()},
    )
    call = ToolCall(name="phase", raw_params={"action": "blocked"})
    return ToolExecution(
        call=call,
        result=result,
        status="failure",
        raw_params=call.raw_params,
        attempted_execution=True,
        observation_text=result.output,
    )


def test_persist_exhaustion_closes_the_phase_instead_of_aborting_the_run():
    """D2 rocketmq: the controller failed to persist its own repair context
    and aborted the RUN while the model's blocked claim was factually correct.
    Spec §3: the phase closes blocked through the normal gate path — the risk
    window is zero because the phase ends in the same breath — and the run
    proceeds to evidence close and report delivery, never `aborted`."""
    engine = _containment_engine()
    execution = _repair_required_rejection(engine)

    prepared = engine._prepare_rejected_completion(execution)
    assert prepared is not None
    # The real rocketmq shape: the install failed with the transport family.
    assert prepared.event.blocker_id in {
        "repair_assessment_persist_failed",
        "repair_context_transport_unavailable",
    }

    # The routing cascade (dependents skip -> evidence close -> report) is
    # covered by the transition-policy suites; this fence pins the containment
    # DECISION, so the cascade is captured, not executed (house pattern from
    # the no-op convergence fences in test_terminal_claim_convergence).
    routed = {}
    engine._apply_phase_decision = lambda record, route: routed.update(
        record=record, route=route
    )

    consumed = engine._apply_rejected_completion_control(prepared)

    assert consumed is True
    assert not str(getattr(engine, "_fatal_harness_control_failure", "") or ""), (
        "a transport-persist failure is contained, never fatal to the run"
    )
    assert routed["record"].termination.value == "blocked", (
        "the phase ends BLOCKED — the honest word, not aborted"
    )
    # The outcome follows the judge: this fixture's validator is RED, so the
    # judge-supported reading is failed (rocketmq's UNAVAILABLE would give
    # unknown). The containment never invents an outcome of its own.
    assert routed["record"].outcome.value == "failed"
    blockers = [
        b for b in engine.run_evidence_state.blockers if b.error_code == prepared.event.blocker_id
    ]
    assert len(blockers) == 1, "the containment is a recorded fact, once"


def test_projection_invalid_stays_fatal():
    """The integrity boundary holds: a projection the harness itself computed
    wrong is a logic failure, not weather. Spec §1: untouched."""
    from sag.agent.react_engine import _CONTAINED_CONTROL_PERSIST_CODES

    assert "repair_context_projection_invalid" not in _CONTAINED_CONTROL_PERSIST_CODES
    assert "gate_decision_persist_failed" not in _CONTAINED_CONTROL_PERSIST_CODES
