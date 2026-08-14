"""Deterministic control-layer replay over production policy components.

CONTRACT CHANGE (Plan 2). Replay used to prove determinism by re-executing the
production `ReasoningScheduler`: it drove a live scheduler alongside the
transcript and rejected any run whose recorded mode, reasoning triggers, or
plan index differed. That verifier died with the protocol it verified — there
is no scheduler, no plan, and no actor byte-match left to re-execute.

What replay verifies now is the event stream itself:

1. **Envelope resolution (dual key).** Every `tool_result` names an
   `action_envelope` whose `envelope_sha256` recomputes byte-identically from
   `{tool, exact_params, tool_call_id | plan_index}`. Native turns key on the
   tool_call id; transcripts recorded before Plan 2 key on the plan index and
   hash exactly as they did when they were written.
2. **Pairing.** Exactly one `tool_result` answers each envelope — no envelope
   left unanswered, no envelope answered twice. This is the same invariant the
   live dispatcher owes Anthropic, checked offline.
3. **Attempt ordering.** `tool_result` lineage is monotone per phase attempt:
   once results for a later attempt appear, an earlier attempt cannot receive
   another one.
4. **Gate lineage.** Every `gate_decision` carries a claim for the phase
   attempt that is actually open, and production `validate_phase_claim`
   reproduces its recorded acceptance and outcome.

Everything else the transcript proves is unchanged: phase transitions re-run
through the real `PhaseTransitionPolicy`, loop decisions through the real
`LoopMemory`, and the verdict through the real `VerdictFinalizer`.

Rows of a kind this walk no longer models (`scheduler_decision`,
`planner_response`) are **skipped and counted**, never fatal: an old transcript
must still verify, and a silently ignored row would be indistinguishable from
a row the walk forgot to check. The counts are reported as
`ReplayResult.skipped_event_kinds`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sag.tools.base import ToolResult, bind_tool_result_output_storage
from sag.verdict_rates import UNBOUNDED_CONFLICT

from .attempt_policy import (
    TestCandidateResolution,
    forced_test_refusal_receipts,
    has_test_candidate_refresh_receipt,
    required_test_attempt,
    terminal_test_receipts,
)
from .action_intents import (
    action_fingerprint,
    canonical_params,
    validate_repair_action_affordance,
)
from .control_events import (
    CONTROL_EVENT_SCHEMA_VERSION,
    EVIDENCE_MUTABLE_RECORD_KINDS,
    EVIDENCE_PUBLICATION_GENESIS_SHA256,
    ControlEvent,
    EvidencePublicationPayload,
    EvidenceStoreBoundPayload,
    GateOutcomeRevisedPayload,
    SourceFileManifest,
    action_envelope_sha256,
    canonical_json,
    canonical_sha256,
    job_stall_transition,
    forced_action_sha256,
)
from .evidence_state import EvidenceRole, RunEvidenceState, StateScope
from .evidence_assessments import assessment_id
from .evidence_publications import (
    EvidencePublicationAuthority,
    install_evidence_publication_authority,
    reset_evidence_publication_authority,
)
from .evidence_records import frame_named_json_record_stream
from .loop_memory import CompletionClaimEvent, LoopDecision, LoopEvent, LoopMemory
from .phase_gates import (
    ANALYSIS_FACTS_RECOVERY_CODES,
    ANALYSIS_RECOVERY_FACT,
    OPEN_OBLIGATIONS_FACT,
    ValidatorState,
    validate_phase_claim,
)
from .phase_machine import PhaseAttemptRecord, PhaseClaim, PhaseMachine
from .phase_transitions import (
    PhaseTransitionPolicy,
    RepairBudgets,
    RepairRequest,
)
from .repair_contexts import (
    RepairContext,
    repair_context_reference_set,
    repair_context_sha256,
)
from .verdict_finalizer import (
    EvidenceCloseReason,
    RunVerdictSnapshot,
    VerdictFinalizer,
    _snapshot_verdict,
)


class ReplayValidationError(ValueError):
    """The transcript is malformed or impossible under production policy."""


class ReplayMismatchError(ReplayValidationError):
    """A valid transcript no longer reproduces its frozen expectations."""


_V4_RATE_CONFLICTS = frozenset({UNBOUNDED_CONFLICT})


def _legacy_v3_snapshot_projection(snapshot: RunVerdictSnapshot) -> dict[str, Any]:
    """Project today's replay into the immutable verdict-v3 comparison shape.

    The replay still executes and returns the v4 finalizer result. This view is
    comparison-only: it preserves old transcript bytes without granting live
    authority to v3 or rewriting the recorded expectation.
    """

    payload = snapshot.model_dump(mode="json")
    payload["schema_version"] = 3
    # Rate-vocabulary conflicts are v4-only: they name a band relation this
    # comparison shape does not carry. Letting one through would retroactively
    # rewrite a frozen v3 record (cassandra-java-driver really did execute
    # 4,928 of 3,349 discovered) instead of reproducing it. No detection is
    # lost — the projection still compares test_stats.discovered and .executed
    # verbatim, which are the counts the conflict is derived from.
    conflicts = tuple(
        conflict for conflict in snapshot.conflicts if conflict not in _V4_RATE_CONFLICTS
    )
    payload["conflicts"] = list(conflicts)
    payload["verdict"] = _snapshot_verdict(
        snapshot.build_evidence,
        snapshot.test_stats,
        conflicts,
    )
    payload.pop("rates", None)
    return payload


#: Event kinds the engine stopped emitting when Plan 2 deleted the scheduler.
#: The walk reads and counts them so pre-Plan-2 transcripts still verify.
_HISTORICAL_EVENT_KINDS = frozenset({"scheduler_decision", "planner_response"})
_POST_CLOSE_PUBLICATION_KINDS = frozenset({"run_pin", "report_metrics"})


def _observe_evidence_publication(
    payload: Mapping[str, Any],
    *,
    expected_run_id: str | None,
    seen: dict[tuple[str, str, str], EvidencePublicationPayload],
    evidence_closed: bool,
) -> None:
    """Validate one publication without projecting it into replay state."""

    try:
        publication = EvidencePublicationPayload.model_validate(payload)
    except (TypeError, ValueError, ValidationError) as exc:
        raise ReplayValidationError("evidence publication payload is invalid") from exc
    if expected_run_id is not None and publication.run_id != expected_run_id:
        raise ReplayValidationError("evidence publication belongs to a foreign run")
    if evidence_closed and publication.record_kind not in _POST_CLOSE_PUBLICATION_KINDS:
        raise ReplayValidationError(
            "only final run-pin or report-metrics publication may follow evidence_close"
        )
    mutable = publication.record_kind in EVIDENCE_MUTABLE_RECORD_KINDS
    key = (
        publication.run_id,
        "logical" if mutable else publication.record_kind,
        str(publication.logical_artifact_id if mutable else publication.record_id),
    )
    prior = seen.get(key)
    if prior is None:
        if mutable and (
            publication.revision != 1
            or publication.previous_raw_sha256 != EVIDENCE_PUBLICATION_GENESIS_SHA256
            or publication.previous_publication_sha256 != EVIDENCE_PUBLICATION_GENESIS_SHA256
        ):
            raise ReplayValidationError("mutable evidence publication does not begin at genesis")
        seen[key] = publication
    elif prior == publication:
        return
    elif mutable:
        previous_state = canonical_sha256(prior.model_dump(mode="json", exclude_unset=True))
        if (
            publication.revision != int(prior.revision or 0) + 1
            or publication.previous_raw_sha256 != prior.raw_sha256
            or publication.previous_publication_sha256 != previous_state
            or publication.record_id != prior.record_id
            or prior.publication_state == "revoked"
            or (
                publication.publication_state == "present"
                and (
                    publication.contract_id != prior.contract_id
                    or publication.contract_hash != prior.contract_hash
                )
            )
            or (
                publication.record_kind not in {"build_requirements", "receipt_structure"}
                and publication.record_kind != prior.record_kind
            )
            or (
                publication.record_kind in {"build_requirements", "receipt_structure"}
                and prior.record_kind not in {"build_requirements", "receipt_structure"}
            )
        ):
            raise ReplayValidationError(
                "mutable evidence publication revision chain is discontinuous"
            )
        seen[key] = publication
    else:
        raise ReplayValidationError(
            "evidence publication identity commits conflicting record bytes"
        )


def _validate_analysis_recovery_audit(
    value: Any,
    *,
    phase: str,
    control_disposition: Any,
) -> dict[str, Any]:
    """Validate the final-gate witness for the sole controller survey retry."""

    if not isinstance(value, Mapping):
        raise ReplayValidationError("analysis recovery audit must be an object")
    audit = dict(value)
    expected_fields = {
        "kind",
        "attempt",
        "before_code",
        "survey_status",
        "after_code",
        "resolved",
    }
    if set(audit) != expected_fields:
        raise ReplayValidationError("analysis recovery audit fields are not canonical")
    if phase != "analyze":
        raise ReplayValidationError("analysis recovery audit requires the analyze phase")
    if not isinstance(audit["kind"], str) or audit["kind"] != "framework_survey":
        raise ReplayValidationError("analysis recovery audit identity is invalid")
    if type(audit["attempt"]) is not int or audit["attempt"] != 1:
        raise ReplayValidationError("analysis recovery attempt must be an integer")
    if not isinstance(audit["before_code"], str):
        raise ReplayValidationError("analysis recovery before_code must be a string")
    if audit["before_code"] not in ANALYSIS_FACTS_RECOVERY_CODES:
        raise ReplayValidationError("analysis recovery before_code is not recoverable")
    if not isinstance(audit["survey_status"], str):
        raise ReplayValidationError("analysis recovery survey_status must be a string")
    if audit["survey_status"] not in {"created", "present", "failed"}:
        raise ReplayValidationError("analysis recovery survey_status is invalid")
    if not isinstance(audit["after_code"], str) or not audit["after_code"].strip():
        raise ReplayValidationError("analysis recovery after_code is required")
    after_code = audit["after_code"].strip()
    if not isinstance(audit["resolved"], bool):
        raise ReplayValidationError("analysis recovery resolved must be boolean")
    expected_resolved = bool(
        after_code not in ANALYSIS_FACTS_RECOVERY_CODES
        and (
            str(control_disposition or "") != "harness_recovery_required"
            or after_code == "evidence_unpersisted"
        )
    )
    if audit["resolved"] is not expected_resolved:
        raise ReplayValidationError("analysis recovery resolved disagrees with final gate")
    return audit


def _recorded_test_dispatch(tool: str, params: Mapping[str, Any]) -> bool:
    if tool == "build":
        return str(params.get("action") or "").strip().lower() == "test"
    if tool == "python":
        return str(params.get("operation") or "").strip().lower() == "test"
    if tool == "gradle":
        tasks = str(params.get("tasks") or "").strip().lower().split()
        return any(task == "test" or task.endswith(":test") for task in tasks)
    if tool == "maven":
        command = str(params.get("command") or "").strip().lower().split()
        return any(
            token in {"test", "verify"} or token.endswith(":test") or token.endswith(":verify")
            for token in command
        )
    return False


def _delivered_gate_word(result: Mapping[str, Any]) -> dict[str, Any] | None:
    """The graded word a tool result handed the model, when it carried one."""

    metadata = result.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    body = metadata.get("gate_result")
    if not isinstance(body, Mapping):
        return None
    decision_id = str(body.get("decision_id") or "").strip()
    if not decision_id or not str(metadata.get("phase_signal") or "").strip():
        return None
    return {
        "decision_id": decision_id,
        "accepted": bool(body.get("accepted")),
        "validated_outcome": str(body.get("validated_outcome") or ""),
    }


def _verify_gate_word_lineage(
    payload: Mapping[str, Any],
    *,
    delivered: Mapping[str, Any] | None,
    seen_decision_ids: set[str],
    revisions: set[tuple[str, str]],
) -> None:
    """The sealed word is the delivered word, or it names the one it replaced.

    Eight archived sessions replayed clean while their `gate_decision`
    contradicted the `gate_result` the model had been handed seconds earlier:
    the walk paired the two events and never compared their words. Archived
    transcripts carry no `decision_id`, so they are read exactly as before.
    """

    decision_id = str(payload.get("decision_id") or "").strip()
    supersedes = str(payload.get("supersedes") or "").strip()
    if supersedes:
        known = set(seen_decision_ids)
        if delivered is not None:
            known.add(str(delivered["decision_id"]))
        if supersedes not in known:
            raise ReplayValidationError(
                "gate decision supersedes a grading that was never recorded"
            )
    if delivered is None or not decision_id:
        return
    sealed_word = (
        bool(payload.get("expected_accepted")),
        str(payload.get("expected_outcome") or ""),
    )
    delivered_word = (bool(delivered["accepted"]), str(delivered["validated_outcome"]))
    if decision_id == str(delivered["decision_id"]):
        if sealed_word != delivered_word:
            raise ReplayValidationError(
                "one grading cannot carry two words: the delivered copy and the "
                "sealed gate decision disagree"
            )
        return
    if supersedes != str(delivered["decision_id"]):
        raise ReplayValidationError(
            "gate decision replaces the delivered grading without superseding it"
        )
    if sealed_word == delivered_word:
        return
    if (str(delivered["decision_id"]), decision_id) not in revisions:
        raise ReplayValidationError(
            "a sealed word that differs from the delivered one has no "
            "gate_outcome_revised observation"
        )


def _repair_assessment_id_for_gate(
    payload: Mapping[str, Any],
    *,
    phase_attempt_id: str,
) -> str:
    """Re-derive the live gate ControlAssessment identity from event facts."""

    code = str(payload.get("code") or "").strip()
    if not code:
        raise ReplayValidationError("repair source gate has no typed code")
    subject_material = {
        "phase_attempt_id": str(phase_attempt_id or ""),
        "phase": str(payload.get("phase") or ""),
        "validator_state": str(payload.get("validator_state") or ""),
        "validated_outcome": str(payload.get("expected_outcome") or ""),
        "control_disposition": str(payload.get("control_disposition") or ""),
        "blocker_owner": str(payload.get("blocker_owner") or ""),
        "code": code,
        "validated_facts": dict(payload.get("validated_facts") or {}),
        "evidence_refs": sorted(
            {str(ref).strip() for ref in payload.get("evidence_refs") or () if str(ref).strip()}
        ),
    }
    subject_id = "gate-" + canonical_sha256(subject_material)[:16]
    return assessment_id(subject_id, code)


def _terminal_claim_exception(payload: Mapping[str, Any]) -> bool:
    params = payload.get("exact_params")
    return bool(
        str(payload.get("tool") or "").strip().lower() == "phase"
        and isinstance(params, Mapping)
        and str(params.get("action") or "").strip().lower() in {"done", "blocked"}
        and payload.get("intent_source") == "model"
        and str(payload.get("intent_id") or "").strip()
        and str(payload.get("action_fingerprint") or "").strip()
        and payload.get("repair_context_id") is None
        and payload.get("trigger_assessment_id") is None
        and payload.get("repair_context_sha256") is None
    )


_REPAIR_PREDISPATCH_REFUSAL_CODES = frozenset(
    {
        "ACTION_INTENT_INVALID",
        "ACTION_INTENT_BINDING_MISMATCH",
        "ACTION_ENVELOPE_IDENTITY_MISSING",
        "ACTION_ENVELOPE_PERSIST_FAILED",
        "ACTION_PARAMS_UNRECORDABLE",
        "CONTRACT_PERSIST_FAILED",
        "CONTRACT_AUTHORITY_MISSING",
        "REPAIR_ACTION_AFFORDANCE_MISMATCH",
        "REPAIR_CONTEXT_NOT_ACTIVE",
        "REPAIR_INTENT_DOMAIN_MISMATCH",
        "REPAIR_INTENT_INVALID_OBSERVATION",
        "REPAIR_INTENT_INVALID_REFS",
        "REPAIR_INTENT_LINEAGE_MISMATCH",
        "REPAIR_INTENT_REQUIRED",
        "REPAIR_LINEAGE_RECORDING_REQUIRED",
        "REPAIR_TOOL_NOT_ALLOWED",
    }
)


def _repair_action_dispatch_outcome(
    *,
    result: Mapping[str, Any],
    actual_executions: Iterable[Mapping[str, Any]] = (),
) -> bool:
    """Return whether a repair action physically dispatched, or fail closed.

    ``True`` is grounded only by a recorded physical execution or an explicit
    runner marker. ``False`` is grounded by an explicit pre-dispatch marker or
    one of the typed contract refusals. Missing/contradictory telemetry is a
    harness-integrity failure; replay must never guess that it dispatched.
    """

    executions = tuple(actual_executions)
    metadata = result.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    marker = metadata.get("runner_dispatched", None)
    if marker is not None and not isinstance(marker, bool):
        raise ReplayValidationError("repair action runner_dispatched must be boolean")
    refusal_code = str(result.get("error_code") or "") in (_REPAIR_PREDISPATCH_REFUSAL_CODES)
    actual_states: list[bool | None] = []
    for actual in executions:
        actual_result = actual.get("result")
        actual_result = actual_result if isinstance(actual_result, Mapping) else {}
        actual_metadata = actual_result.get("metadata")
        actual_metadata = actual_metadata if isinstance(actual_metadata, Mapping) else {}
        actual_marker = actual_metadata.get("runner_dispatched", None)
        if actual_marker is not None and not isinstance(actual_marker, bool):
            raise ReplayValidationError("repair actual execution runner_dispatched must be boolean")
        actual_refusal = str(actual_result.get("error_code") or "") in (
            _REPAIR_PREDISPATCH_REFUSAL_CODES
        )
        if actual_marker is True and actual_refusal:
            raise ReplayValidationError(
                "repair actual execution contradicts its pre-dispatch refusal"
            )
        if actual_marker is True:
            actual_states.append(True)
        elif actual_marker is False or actual_refusal:
            actual_states.append(False)
        elif str(actual.get("tool") or "").strip().lower() == "build":
            actual_states.append(None)
        else:
            actual_states.append(True)
    if any(state is None for state in actual_states):
        raise ReplayValidationError(
            "repair action has no strict physical-dispatch or pre-dispatch-refusal evidence"
        )
    dispatched = any(state is True for state in actual_states) or marker is True
    refused = marker is False or refusal_code
    if dispatched and refused:
        raise ReplayValidationError(
            "repair action dispatch evidence contradicts its pre-dispatch refusal"
        )
    if dispatched:
        return True
    if refused:
        return False
    raise ReplayValidationError(
        "repair action has no strict physical-dispatch or pre-dispatch-refusal evidence"
    )


def _validate_repair_action_envelope(
    payload: Mapping[str, Any],
    *,
    context: RepairContext,
    context_sha256: str,
) -> None:
    """Validate one model action against the full active context projection."""

    if payload.get("intent_source") != "model":
        raise ReplayValidationError("repair action must be model-owned")
    if payload.get("repair_context_id") != context.repair_context_id:
        raise ReplayValidationError("repair action names a different active context")
    if payload.get("repair_context_sha256") != context_sha256:
        raise ReplayValidationError("repair action context hash differs from the opened context")
    if payload.get("trigger_assessment_id") != context.trigger_assessment_id:
        raise ReplayValidationError("repair action trigger differs from the opened context")
    domain_id = str(payload.get("domain_id") or "").strip()
    if context.domain_id is not None and domain_id != context.domain_id:
        raise ReplayValidationError("repair action domain differs from the opened context")
    tool = str(payload.get("tool") or "").strip().lower()
    params = payload.get("exact_params")
    if not isinstance(params, Mapping):
        raise ReplayValidationError("repair action exact_params must be an object")
    expected_fingerprint = action_fingerprint(
        domain_id=domain_id,
        tool=tool,
        params=canonical_params(params),
    )
    if payload.get("action_fingerprint") != expected_fingerprint:
        raise ReplayValidationError("repair action fingerprint differs from its outer call")

    try:
        affordance = validate_repair_action_affordance(
            tool=tool,
            params=params,
            next_action_kind=payload.get("next_action_kind"),
            context=context,
        )
    except (TypeError, ValueError) as exc:
        raise ReplayValidationError(str(exc)) from exc
    submitted_refs = {
        str(ref).strip() for ref in payload.get("blocking_fact_refs") or () if str(ref).strip()
    }
    allowed_refs = repair_context_reference_set(context, affordance=affordance)
    if not submitted_refs or not submitted_refs.issubset(allowed_refs):
        raise ReplayValidationError("repair action cites refs outside the opened context")
    expected_observations = {
        str(value).strip()
        for value in payload.get("expected_observation") or ()
        if str(value).strip()
    }
    if not expected_observations or not expected_observations.issubset(
        set(context.admissible_observation_types)
    ):
        raise ReplayValidationError("repair action expects observations outside the opened context")


def _validated_opened_repair_context(
    payload: Mapping[str, Any],
    *,
    source_gate: Mapping[str, Any],
) -> RepairContext:
    """Re-derive one opened context exclusively from its recorded gate."""

    attempt_id = str(payload.get("source_phase_attempt_id") or "")
    if attempt_id != str(source_gate.get("source_attempt_id") or ""):
        raise ReplayValidationError("repair context phase attempt differs from its source gate")
    if source_gate.get("expected_accepted") is not False or (
        source_gate.get("control_disposition") != "repair_required"
    ):
        raise ReplayValidationError("repair context source is not a rejected repair-required gate")
    context = RepairContext.model_validate(payload.get("context"))
    context_digest = str(payload.get("context_sha256") or "").strip().lower()
    if repair_context_sha256(context) != context_digest:
        raise ReplayValidationError("repair context opened hash mismatch")
    expected_trigger = _repair_assessment_id_for_gate(
        source_gate,
        phase_attempt_id=attempt_id,
    )
    if context.trigger_assessment_id != expected_trigger:
        raise ReplayValidationError("repair context trigger is not derived from its source gate")
    if context.typed_blocker != source_gate.get("code"):
        raise ReplayValidationError("repair context blocker differs from its source gate")
    if context.blocker_owner != source_gate.get("blocker_owner"):
        raise ReplayValidationError("repair context owner differs from its source gate")
    expected_refs = {
        expected_trigger,
        *(str(ref).strip() for ref in source_gate.get("evidence_refs") or () if str(ref).strip()),
    }
    if set(context.observed_fact_refs) != expected_refs:
        raise ReplayValidationError("repair context observed refs differ from its source gate")
    return context


@dataclass(frozen=True)
class ActiveRepairContextState:
    """Restart-safe projection derived only from the append-only event stream."""

    context: RepairContext | None
    context_sha256: str | None
    source_gate_sequence: int | None
    source_phase_attempt_id: str | None
    consumed_context_ids: tuple[str, ...]


def recover_active_repair_context(
    events: Iterable[ControlEvent | Mapping[str, Any]],
) -> ActiveRepairContextState:
    """Replay repair open/use/consume state without invoking any producer.

    A process restart calls this over the existing strict control stream. An
    unanswered envelope, forged/missing context, partial lineage, reopened
    consumed context, or evidence close with an active repair is fatal. A
    pre-dispatch contract refusal leaves the context active; an actual
    dispatch consumes it exactly once. A later legal rejected gate may replace
    the active context, and an honest terminal phase claim is the sole unlinked
    model action allowed while a context is active.
    """

    gates: dict[int, dict[str, Any]] = {}
    opened_gate_sequences: set[int] = set()
    consumed: set[str] = set()
    answered_envelope_ids: set[str] = set()
    active: dict[str, Any] | None = None
    envelope: dict[str, Any] | None = None
    answered_terminal_claim: dict[str, str] | None = None
    pending_rejected_terminal_gate: dict[str, str] | None = None
    pending_finalizer_gate: dict[str, str] | None = None
    pending_repair_gate_sequence: int | None = None
    pending_phase_gate_sequence: int | None = None
    evidence_closed = False
    publications: dict[tuple[str, str, str], EvidencePublicationPayload] = {}
    evidence_store_binding: EvidenceStoreBoundPayload | None = None
    for expected_sequence, raw_event in enumerate(events, 1):
        # Revalidate model instances as well as mappings. Frozen Pydantic
        # objects can still be corrupted through ``object.__setattr__``; a
        # restart boundary must not trust an in-memory producer more than the
        # JSON path trusts disk.
        event = ControlEvent.model_validate(raw_event)
        if event.sequence != expected_sequence:
            raise ReplayValidationError(
                "event sequence must be monotonic: "
                f"expected {expected_sequence}, got {event.sequence}"
            )
        payload = event.payload
        if evidence_closed and event.kind != "evidence_publication":
            raise ReplayValidationError("control stream continues after evidence_close")
        if event.kind == "evidence_store_bound":
            if evidence_store_binding is not None or publications:
                raise ReplayValidationError(
                    "evidence store must bind exactly once before any publication"
                )
            evidence_store_binding = EvidenceStoreBoundPayload.model_validate(payload)
            continue
        if event.kind == "evidence_publication":
            if evidence_store_binding is None:
                raise ReplayValidationError(
                    "evidence publication precedes the immutable store binding"
                )
            _observe_evidence_publication(
                payload,
                expected_run_id=evidence_store_binding.run_id,
                seen=publications,
                evidence_closed=evidence_closed,
            )
            continue
        if pending_repair_gate_sequence is not None and event.kind in {
            "action_envelope",
            "forced_action",
            "gate_decision",
            "phase_transition",
            "evidence_close",
        }:
            raise ReplayValidationError("repair-required gate has no repair_context_opened event")
        if answered_terminal_claim is not None and event.kind in {
            "action_envelope",
            "forced_action",
            "phase_transition",
            "evidence_close",
        }:
            raise ReplayValidationError("model terminal action has no matching gate_decision event")
        if pending_rejected_terminal_gate is not None and event.kind in {
            "action_envelope",
            "forced_action",
            "gate_decision",
            "phase_transition",
            "evidence_close",
        }:
            raise ReplayValidationError(
                "rejected model terminal gate has no completion_claim_decision event"
            )
        if event.kind == "gate_decision":
            if active is not None:
                finalizer_gate = bool(
                    pending_finalizer_gate is not None
                    and payload.get("expected_accepted") is True
                    and str(payload.get("code") or "") == "agent_no_progress"
                    and str(payload.get("phase") or "") == pending_finalizer_gate["phase"]
                    and str(payload.get("signal") or "") == "done"
                )
                if not finalizer_gate and answered_terminal_claim is None:
                    raise ReplayValidationError(
                        "gate decision cannot bypass an active repair context; "
                        "a complete model terminal action must precede it"
                    )
                if not finalizer_gate and (
                    str(payload.get("phase") or "") != answered_terminal_claim["phase"]
                    or str(payload.get("signal") or "") != answered_terminal_claim["signal"]
                ):
                    raise ReplayValidationError(
                        "gate decision differs from its model terminal action"
                    )
                source_attempt_id = str(payload.get("source_attempt_id") or "")
                if source_attempt_id and source_attempt_id != str(
                    active["source_phase_attempt_id"]
                ):
                    raise ReplayValidationError(
                        "gate decision targets a different active repair attempt"
                    )
                if finalizer_gate:
                    pending_finalizer_gate = None
                else:
                    answered_terminal_claim = None
                    if payload.get("expected_accepted") is False:
                        pending_rejected_terminal_gate = {
                            "phase": str(payload.get("phase") or ""),
                            "signal": str(payload.get("signal") or ""),
                            "phase_attempt_id": source_attempt_id,
                            "judge_disposition": str(payload.get("control_disposition") or ""),
                            "blocker_id": str(payload.get("code") or ""),
                        }
            gates[event.sequence] = dict(payload)
            if (
                payload.get("expected_accepted") is False
                and payload.get("control_disposition") == "repair_required"
            ):
                pending_repair_gate_sequence = event.sequence
            if payload.get("expected_accepted") is True:
                pending_phase_gate_sequence = event.sequence
            continue
        if event.kind == "repair_context_opened":
            if envelope is not None:
                raise ReplayValidationError(
                    "repair context cannot replace an unanswered action envelope"
                )
            gate_sequence = int(payload["source_gate_sequence"])
            if pending_repair_gate_sequence is None:
                raise ReplayValidationError(
                    "repair_context_opened has no pending repair-required gate"
                )
            if gate_sequence != pending_repair_gate_sequence:
                raise ReplayValidationError(
                    "repair_context_opened references a different repair-required gate"
                )
            source_gate = gates.get(gate_sequence)
            if source_gate is None:
                raise ReplayValidationError(
                    "repair context does not reference a prior gate_decision"
                )
            if gate_sequence in opened_gate_sequences:
                raise ReplayValidationError("one gate_decision cannot open two repair contexts")
            context = _validated_opened_repair_context(
                payload,
                source_gate=source_gate,
            )
            if context.repair_context_id in consumed:
                raise ReplayValidationError("a consumed repair context cannot be reopened")
            opened_gate_sequences.add(gate_sequence)
            active = {
                "context": context,
                "context_sha256": payload["context_sha256"],
                "source_gate_sequence": gate_sequence,
                "source_phase_attempt_id": payload["source_phase_attempt_id"],
            }
            pending_repair_gate_sequence = None
            continue
        if event.kind == "completion_claim_decision":
            if pending_rejected_terminal_gate is not None:
                if (
                    str(payload.get("phase_attempt_id") or "")
                    != pending_rejected_terminal_gate["phase_attempt_id"]
                    or str(payload.get("claim_kind") or "")
                    != pending_rejected_terminal_gate["signal"]
                    or str(payload.get("judge_disposition") or "")
                    != pending_rejected_terminal_gate["judge_disposition"]
                    or str(payload.get("blocker_id") or "")
                    != pending_rejected_terminal_gate["blocker_id"]
                ):
                    raise ReplayValidationError(
                        "completion claim decision differs from its rejected terminal gate"
                    )
                if (
                    payload.get("expected_decision") == "agent_no_progress"
                    and payload.get("expected_close_phase") is True
                ):
                    pending_finalizer_gate = {
                        "phase": pending_rejected_terminal_gate["phase"],
                        "phase_attempt_id": pending_rejected_terminal_gate["phase_attempt_id"],
                    }
                pending_rejected_terminal_gate = None
            continue
        if event.kind in {"action_envelope", "forced_action"}:
            if envelope is not None:
                raise ReplayValidationError("an action envelope is still unanswered")
            candidate = dict(payload)
            candidate["_repair_context_to_consume"] = None
            candidate["_terminal_claim_exception"] = False
            envelope_id = str(payload.get("envelope_id") or "")
            if envelope_id in answered_envelope_ids:
                raise ReplayValidationError(f"envelope {envelope_id!r} already has a tool_result")
            if event.kind == "action_envelope":
                if active is not None:
                    if _terminal_claim_exception(payload):
                        candidate["_terminal_claim_exception"] = True
                    else:
                        _validate_repair_action_envelope(
                            payload,
                            context=active["context"],
                            context_sha256=active["context_sha256"],
                        )
                        candidate["_repair_context_to_consume"] = active[
                            "context"
                        ].repair_context_id
                elif payload.get("repair_context_id") is not None:
                    raise ReplayValidationError(
                        "repair action has no active repair_context_opened event"
                    )
            envelope = candidate
            continue
        if event.kind == "tool_result":
            if envelope is None:
                raise ReplayValidationError("tool result has no action envelope to answer")
            if payload.get("envelope_id") != envelope.get("envelope_id"):
                raise ReplayValidationError("tool result references a different envelope")
            if payload.get("tool") != envelope.get("tool") or payload.get("params") != envelope.get(
                "exact_params"
            ):
                raise ReplayValidationError("tool result differs from its exact action envelope")
            context_id = envelope.get("_repair_context_to_consume")
            if context_id:
                result = payload.get("result")
                result = result if isinstance(result, Mapping) else {}
                dispatched = _repair_action_dispatch_outcome(
                    result=result,
                    actual_executions=tuple(payload.get("actual_executions") or ()),
                )
                if dispatched:
                    if active is None or active["context"].repair_context_id != context_id:
                        raise ReplayValidationError(
                            "repair context changed before its action completed"
                        )
                    consumed.add(context_id)
                    active = None
            if envelope.get("_terminal_claim_exception"):
                params = envelope.get("exact_params")
                params = params if isinstance(params, Mapping) else {}
                source_gate = gates.get(int(active["source_gate_sequence"])) if active else None
                answered_terminal_claim = {
                    "phase": str((source_gate or {}).get("phase") or ""),
                    "signal": str(params.get("action") or "").strip().lower(),
                }
            answered_envelope_ids.add(str(payload.get("envelope_id") or ""))
            envelope = None
            continue
        if event.kind == "phase_transition":
            if envelope is not None:
                raise ReplayValidationError("phase transitioned with an unanswered action envelope")
            if pending_phase_gate_sequence is None:
                if active is not None:
                    raise ReplayValidationError(
                        "phase transition cannot clear an active repair context "
                        "without an accepted model terminal gate"
                    )
                raise ReplayValidationError("phase transition has no accepted gate decision")
            active = None
            pending_phase_gate_sequence = None
            continue
        if event.kind == "evidence_close":
            if active is not None or pending_phase_gate_sequence is not None:
                raise ReplayValidationError("evidence closed with unconsumed control state")
            evidence_closed = True
    if envelope is not None:
        raise ReplayValidationError("control stream ended with an unanswered action envelope")
    if pending_repair_gate_sequence is not None:
        raise ReplayValidationError(
            "control stream ended before repair_context_opened followed a repair-required gate"
        )
    if pending_phase_gate_sequence is not None:
        raise ReplayValidationError(
            "control stream ended before phase_transition followed an accepted gate"
        )
    if answered_terminal_claim is not None:
        raise ReplayValidationError(
            "control stream ended before gate_decision followed a model terminal action"
        )
    if pending_rejected_terminal_gate is not None:
        raise ReplayValidationError(
            "control stream ended before completion_claim_decision followed a rejected gate"
        )
    if pending_finalizer_gate is not None:
        raise ReplayValidationError(
            "control stream ended before finalizer gate followed agent_no_progress"
        )
    return ActiveRepairContextState(
        context=active["context"] if active is not None else None,
        context_sha256=active["context_sha256"] if active is not None else None,
        source_gate_sequence=active["source_gate_sequence"] if active is not None else None,
        source_phase_attempt_id=(active["source_phase_attempt_id"] if active is not None else None),
        consumed_context_ids=tuple(sorted(consumed)),
    )


def recover_active_repair_context_from_path(
    path: str | Path,
) -> ActiveRepairContextState:
    """Read a live JSONL stream strictly and reconstruct restart authority."""

    events: list[ControlEvent] = []
    target = Path(path)
    if not target.exists():
        return ActiveRepairContextState(None, None, None, None, ())
    try:
        with target.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    events.append(ControlEvent.model_validate_json(line))
    except (OSError, TypeError, ValueError, ValidationError) as exc:
        raise ReplayValidationError("active repair context stream is invalid") from exc
    return recover_active_repair_context(events)


class InitialFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1)
    value: Any
    evidence_ref: str = Field(min_length=1)
    scope: StateScope | None = None


class ReplayInitialState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start_phase: Literal["provision", "analyze", "build", "test", "report"]
    heartbeat_actions: int | None = Field(default=5, ge=1)
    available_tools: tuple[str, ...] = Field(min_length=1)
    facts: tuple[InitialFact, ...] = ()
    conflicts: tuple[str, ...] = ()
    repair_global_remaining: int = Field(default=2, ge=0)
    repair_phase_remaining: dict[str, int] = Field(default_factory=lambda: {"test": 1, "build": 1})


class ReplayHeader(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1, 2, 3]
    fixture_kind: Literal["recorded_tool_transcript"]
    probe: Literal["tvm", "bigtop", "paramiko", "cassandra-java-driver"]
    run_id: str = Field(min_length=1)
    source_manifest: tuple[SourceFileManifest, ...] = Field(min_length=1)
    initial_state: ReplayInitialState
    finalized_at: str = Field(min_length=1)
    expected_snapshot: dict[str, Any]
    expected_event_digest: str


class ReplayTranscript(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    header: ReplayHeader
    events: tuple[ControlEvent, ...]

    @classmethod
    def read(cls, path: str | Path) -> "ReplayTranscript":
        transcript_path = Path(path)
        try:
            lines = [
                line
                for line in transcript_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except OSError as exc:
            raise ReplayValidationError(f"cannot read replay transcript: {exc}") from exc
        if not lines:
            raise ReplayValidationError("replay transcript is empty")
        try:
            header = ReplayHeader.model_validate_json(lines[0])
            events = tuple(ControlEvent.model_validate_json(line) for line in lines[1:])
            if header.schema_version < 3:
                for event in events:
                    if event.kind == "repair_context_opened" or any(
                        event.payload.get(key) is not None
                        for key in (
                            "trigger_assessment_id",
                            "repair_context_id",
                            "repair_context_sha256",
                        )
                    ):
                        raise ReplayValidationError(
                            "repair-linked replay schema 1/2 is explicitly unverifiable; "
                            "a schema-3 repair_context_opened event is required"
                        )
        except (TypeError, ValueError, ValidationError) as exc:
            raise ReplayValidationError(str(exc)) from exc
        for expected, event in enumerate(events, 1):
            if event.sequence != expected:
                raise ReplayValidationError(
                    f"event sequence must be monotonic: expected {expected}, got {event.sequence}"
                )
            if header.schema_version == 1 and event.kind == "forced_action":
                raise ReplayValidationError("forced_action requires control-event schema version 2")
            if header.schema_version < 3 and event.kind == "repair_context_opened":
                raise ReplayValidationError("repair_context_opened requires replay schema 3")
            if (
                header.schema_version >= 3
                and event.kind == "gate_decision"
                and not str(event.payload.get("code") or "").strip()
            ):
                raise ReplayValidationError("replay schema 3 requires gate_decision.code")
        manifest_paths = {item.path for item in header.source_manifest}
        for event in events:
            if event.source is None:
                raise ReplayValidationError("recorded fixtures require source provenance per event")
            if event.source.path not in manifest_paths:
                raise ReplayValidationError(
                    f"event source {event.source.path!r} is absent from source_manifest"
                )
        return cls(header=header, events=events)


@dataclass(frozen=True)
class RepairRouteResult:
    edge: tuple[str, str]
    accepted: bool
    reason_code: str


@dataclass(frozen=True)
class ReplayResult:
    header: ReplayHeader
    snapshot: RunVerdictSnapshot
    expected_snapshot: dict[str, Any]
    produced_event_digest: str
    expected_event_digest: str
    unconsumed_events: tuple[str, ...]
    phase_records: tuple[PhaseAttemptRecord, ...]
    loop_decisions: tuple[LoopDecision, ...]
    repair_routes: tuple[RepairRouteResult, ...]
    planner_response_count: int
    executed_envelope_count: int
    #: envelopes that received exactly one tool_result (== executed_envelope_count
    #: for a well-formed transcript; the walk raises before it can differ)
    paired_envelope_count: int = 0
    gate_decision_count: int = 0
    #: {kind: rows} for pre-Plan-2 event kinds this walk no longer models
    skipped_event_kinds: Mapping[str, int] = field(default_factory=dict)
    compatibility_action_model_calls: int = 0

    def phase(self, phase: str) -> PhaseAttemptRecord:
        matches = self.phase_attempts(phase)
        if not matches:
            raise KeyError(phase)
        return matches[-1]

    def phase_attempts(self, phase: str) -> tuple[PhaseAttemptRecord, ...]:
        return tuple(record for record in self.phase_records if record.phase == phase)


class _ReplayOutputStorage:
    def __init__(self) -> None:
        self._refs: dict[str, str] = {}

    def register(self, ref: str, value: str = "recorded output") -> None:
        self._refs[ref] = value

    def has_output_ref(self, ref: str) -> bool:
        return ref in self._refs

    def retrieve_output(self, ref: str) -> str | None:
        return self._refs.get(ref)


class _ReplayVerdictOrchestrator:
    """In-memory persistence seam used by the production VerdictFinalizer."""

    class _PublicationSink:
        path = Path("/host/replay-control-events.jsonl")

        def emit(self, kind, payload, *, source=None) -> None:
            del kind, payload, source

    def __init__(self, *, run_id: str) -> None:
        self.files: dict[str, str] = {}
        authority = EvidencePublicationAuthority(
            run_id=run_id,
            sink=self._PublicationSink(),
        )
        token = install_evidence_publication_authority(authority, orchestrator=self)
        reset_evidence_publication_authority(token)

    @staticmethod
    def _ok(output: str = "") -> dict[str, Any]:
        return {"success": True, "exit_code": 0, "output": output}

    @staticmethod
    def _fail(output: str, *, exit_code: int = 1) -> dict[str, Any]:
        return {"success": False, "exit_code": exit_code, "output": output}

    def execute_command(self, command: str, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        if command.startswith("file=") and "SAG_NAMED_JSON_RECORD_V1" in command:
            assignment = command.partition(";")[0]
            target = shlex.split(assignment[len("file=") :])[0]
            records = (
                [(target.rsplit("/", 1)[-1], self.files[target])] if target in self.files else []
            )
            return self._ok(frame_named_json_record_stream(records))

        tokens = shlex.split(command)
        if tokens[:3] == ["mkdir", "-p", "--"]:
            return {"success": True, "exit_code": 0, "output": ""}
        if len(tokens) == 3 and tokens[:2] == [":", ">"]:
            self.files[tokens[2]] = ""
            return self._ok()
        if len(tokens) == 5 and tokens[:2] == ["printf", "%s"] and tokens[3] == ">>":
            self.files[tokens[4]] = self.files.get(tokens[4], "") + tokens[2]
            return self._ok()
        if tokens[:2] == ["base64", "--decode"] and tokens[-2:-1] == [">"]:
            self.files[tokens[-1]] = base64.b64decode(self.files.get(tokens[2], "")).decode("utf-8")
            return self._ok()
        if tokens[:2] == ["python3", "-c"] and "fcntl.flock" in tokens[2]:
            target, candidate, _lock_path, expected, expected_bytes, expected_sha = tokens[3:9]
            candidate_bytes = self.files.get(candidate, "").encode("utf-8")
            if (
                len(candidate_bytes) != int(expected_bytes)
                or hashlib.sha256(candidate_bytes).hexdigest() != expected_sha
            ):
                return self._fail("SAG_CAS_CANDIDATE_INVALID\n", exit_code=76)
            current = self.files.get(target)
            actual = (
                "absent"
                if current is None
                else "sha256:" + hashlib.sha256(current.encode("utf-8")).hexdigest()
            )
            if actual != expected:
                return self._fail("SAG_CAS_CONFLICT\n", exit_code=75)
            self.files[target] = self.files.pop(candidate)
            return self._ok()
        if tokens[:2] == ["python3", "-c"] and "hashlib.sha256" in tokens[2]:
            path, expected_bytes, expected_sha = tokens[3:6]
            payload = self.files.get(path, "").encode("utf-8")
            if (
                len(payload) != int(expected_bytes)
                or hashlib.sha256(payload).hexdigest() != expected_sha
            ):
                return self._fail("validation failed")
            return self._ok()
        if tokens[:2] == ["python3", "-c"] and "json.load" in tokens[2]:
            try:
                json.loads(self.files.get(tokens[3], ""))
            except (TypeError, json.JSONDecodeError):
                return self._fail("invalid json")
            return self._ok()
        if tokens[:3] == ["mv", "-f", "--"]:
            source, target = tokens[3:5]
            self.files[target] = self.files.pop(source)
            return self._ok()
        if tokens[:2] == ["rm", "-f"]:
            targets = tokens[3:] if tokens[2:3] == ["--"] else tokens[2:]
            for target in targets:
                self.files.pop(target, None)
            return self._ok()
        return self._ok()

    def execute_control_command(self, command: str, **kwargs: Any) -> dict[str, Any]:
        """Replay persistence is an in-memory clean-control transport."""

        return self.execute_command(command, **kwargs)


class ControlReplayRunner:
    """Consume recorded inputs through the shipped gates/policy/finalizer."""

    def __init__(
        self,
        *,
        llm_factory: Callable[[], Any] | None = None,
        orchestrator_factory: Callable[[], Any] | None = None,
        verify_expected: bool = True,
    ) -> None:
        # Factories are deliberate tripwires. Replays never invoke either one.
        self.llm_factory = llm_factory
        self.orchestrator_factory = orchestrator_factory
        self.verify_expected = verify_expected

    @classmethod
    def offline(cls, *, verify_expected: bool = True) -> "ControlReplayRunner":
        def _forbidden() -> Any:
            raise AssertionError("offline replay attempted an external call")

        return cls(
            llm_factory=_forbidden,
            orchestrator_factory=_forbidden,
            verify_expected=verify_expected,
        )

    def run(self, path: str | Path) -> ReplayResult:
        transcript = ReplayTranscript.read(path)
        header = transcript.header
        initial = header.initial_state
        machine = PhaseMachine(start_phase=initial.start_phase)
        state = RunEvidenceState(run_id=header.run_id)
        for fact in initial.facts:
            state.set_fact(
                fact.key,
                fact.value,
                evidence_ref=fact.evidence_ref,
                scope=fact.scope,
            )
        for conflict in initial.conflicts:
            state.record_conflict(conflict)
        loop_memory = LoopMemory()
        transition_policy = PhaseTransitionPolicy(repair_guard=loop_memory)
        budgets = RepairBudgets(
            global_remaining=initial.repair_global_remaining,
            phase_remaining=dict(initial.repair_phase_remaining),
        )
        output_storage = _ReplayOutputStorage()
        verdict_orchestrator = _ReplayVerdictOrchestrator(run_id=header.run_id)
        finalizer = VerdictFinalizer(verdict_orchestrator)

        active_envelope: dict[str, Any] | None = None
        active_repair_context: dict[str, Any] | None = None
        pending_active_terminal_claim: dict[str, str] | None = None
        pending_rejected_terminal_gate: dict[str, str] | None = None
        pending_finalizer_gate: dict[str, str] | None = None
        repair_gate_events: dict[int, dict[str, Any]] = {}
        opened_repair_gate_sequences: set[int] = set()
        pending_repair_gate_sequence: int | None = None
        consumed_repair_context_ids: set[str] = set()
        pending_record: PhaseAttemptRecord | None = None
        produced: list[dict[str, Any]] = []
        loop_decisions: list[LoopDecision] = []
        repairs: list[RepairRouteResult] = []
        planner_response_count = 0
        envelope_count = 0
        paired_envelope_count = 0
        gate_decision_count = 0
        analysis_recovery_count = 0
        pending_analysis_recovery_observation: dict[str, Any] | None = None
        skipped_kinds: dict[str, int] = {}
        answered_envelope_ids: set[str] = set()
        # Ordered lineage of the phase attempts tool results have been seen for;
        # a result for anything but the last entry is out of order.
        attempt_order: list[str] = []
        forced_attempt_ids: set[str] = set()
        terminal_exit_codes: dict[str, int] = {}
        terminal_unpersisted_jobs: set[str] = set()
        live_at_close_jobs: set[str] = set()
        legacy_unsettled_jobs: set[str] = set()
        settled_jobs: set[str] = set()
        settled_job_events: dict[str, tuple[str, int]] = {}
        stall_event_fingerprints: set[str] = set()
        # Gate truth (spec 2026-08-14 §3): the word a tool result handed the
        # model, the gradings already named, and the revisions spoken aloud.
        delivered_gate_word: dict[str, Any] | None = None
        seen_decision_ids: set[str] = set()
        gate_outcome_revisions: set[tuple[str, str]] = set()
        evidence_publications: dict[tuple[str, str, str], EvidencePublicationPayload] = {}
        evidence_store_binding: EvidenceStoreBoundPayload | None = None
        barrier_integrity_failure_seen = False
        snapshot: RunVerdictSnapshot | None = None

        def open_envelope(payload: dict[str, Any], *, forced: bool) -> dict[str, Any]:
            """Claim the pairing slot; the previous envelope must be answered."""
            nonlocal active_envelope, envelope_count
            if active_envelope is not None:
                raise ReplayValidationError(
                    f"action envelope {active_envelope['envelope_id']!r} must be "
                    "answered by exactly one tool_result before another action "
                    "is recorded"
                )
            envelope = dict(payload)
            envelope["_harness_forced"] = forced
            active_envelope = envelope
            envelope_count += 1
            return envelope

        for event in transcript.events:
            payload = event.payload
            try:
                if pending_repair_gate_sequence is not None and event.kind in {
                    "action_envelope",
                    "forced_action",
                    "gate_decision",
                    "phase_transition",
                    "evidence_close",
                }:
                    raise ReplayValidationError(
                        "repair-required gate has no repair_context_opened event"
                    )
                if pending_active_terminal_claim is not None and event.kind in {
                    "action_envelope",
                    "forced_action",
                    "phase_transition",
                    "evidence_close",
                }:
                    raise ReplayValidationError(
                        "model terminal action has no matching gate_decision event"
                    )
                if pending_rejected_terminal_gate is not None and event.kind in {
                    "action_envelope",
                    "forced_action",
                    "gate_decision",
                    "phase_transition",
                    "evidence_close",
                }:
                    raise ReplayValidationError(
                        "rejected model terminal gate has no " "completion_claim_decision event"
                    )
                if event.kind == "evidence_store_bound":
                    if evidence_store_binding is not None or evidence_publications:
                        raise ReplayValidationError(
                            "evidence store must bind exactly once before any publication"
                        )
                    binding = EvidenceStoreBoundPayload.model_validate(payload)
                    if binding.run_id != header.run_id:
                        raise ReplayValidationError(
                            "evidence store binding belongs to a foreign run"
                        )
                    evidence_store_binding = binding
                elif event.kind == "evidence_publication":
                    if evidence_store_binding is None:
                        raise ReplayValidationError(
                            "evidence publication precedes the immutable store binding"
                        )
                    _observe_evidence_publication(
                        payload,
                        expected_run_id=header.run_id,
                        seen=evidence_publications,
                        evidence_closed=snapshot is not None,
                    )
                elif event.kind in _HISTORICAL_EVENT_KINDS:
                    # Recorded before Plan 2 by machinery this walk no longer
                    # models. Skipped, but counted so the notice is visible.
                    skipped_kinds[event.kind] = skipped_kinds.get(event.kind, 0) + 1
                    if event.kind == "planner_response":
                        planner_response_count += 1
                elif event.kind == "action_envelope":
                    calculated_hash = action_envelope_sha256(
                        plan_index=payload.get("plan_index"),
                        tool_call_id=payload.get("tool_call_id"),
                        tool=payload["tool"],
                        exact_params=payload["exact_params"],
                        intent_id=payload.get("intent_id"),
                        intent_source=payload.get("intent_source"),
                        action_fingerprint=payload.get("action_fingerprint"),
                        trigger_assessment_id=payload.get("trigger_assessment_id"),
                        repair_context_id=payload.get("repair_context_id"),
                        repair_context_sha256=payload.get("repair_context_sha256"),
                        domain_id=payload.get("domain_id"),
                        blocking_fact_refs=payload.get("blocking_fact_refs"),
                        repair_hypothesis=payload.get("repair_hypothesis"),
                        next_action_kind=payload.get("next_action_kind"),
                        expected_observation=payload.get("expected_observation"),
                        stop_condition=payload.get("stop_condition"),
                    )
                    if calculated_hash != payload["envelope_sha256"]:
                        raise ReplayValidationError("action envelope hash mismatch")
                    consumes_repair_context = None
                    active_terminal_exception = False
                    if active_repair_context is not None:
                        if _terminal_claim_exception(payload):
                            active_terminal_exception = True
                        else:
                            opened_context = active_repair_context["context"]
                            _validate_repair_action_envelope(
                                payload,
                                context=opened_context,
                                context_sha256=active_repair_context["context_sha256"],
                            )
                            consumes_repair_context = opened_context.repair_context_id
                    elif payload.get("repair_context_id") is not None:
                        context_id = str(payload.get("repair_context_id") or "")
                        suffix = (
                            " (already consumed)"
                            if context_id in consumed_repair_context_ids
                            else ""
                        )
                        raise ReplayValidationError(
                            "repair action has no active repair_context_opened event" + suffix
                        )
                    envelope = open_envelope(payload, forced=False)
                    if active_terminal_exception:
                        envelope["_active_terminal_exception"] = True
                    if consumes_repair_context is not None:
                        envelope["_repair_context_to_consume"] = consumes_repair_context
                elif event.kind == "repair_context_opened":
                    if header.schema_version < 3:
                        raise ReplayValidationError(
                            "repair_context_opened requires replay schema 3"
                        )
                    if snapshot is not None:
                        raise ReplayValidationError(
                            "repair context cannot open after evidence_close"
                        )
                    source_sequence = int(payload["source_gate_sequence"])
                    if pending_repair_gate_sequence is None:
                        raise ReplayValidationError(
                            "repair_context_opened has no pending repair-required gate"
                        )
                    if source_sequence != pending_repair_gate_sequence:
                        raise ReplayValidationError(
                            "repair_context_opened references a different repair-required gate"
                        )
                    gate_payload = repair_gate_events.get(source_sequence)
                    if gate_payload is None:
                        raise ReplayValidationError(
                            "repair context does not reference a prior gate_decision"
                        )
                    if source_sequence in opened_repair_gate_sequences:
                        raise ReplayValidationError(
                            "one gate_decision cannot open two repair contexts"
                        )
                    attempt_id = str(payload["source_phase_attempt_id"])
                    if attempt_id != machine.current_attempt_id:
                        raise ReplayValidationError("repair context targets a stale phase attempt")
                    context = _validated_opened_repair_context(
                        payload,
                        source_gate=gate_payload,
                    )
                    if context.repair_context_id in consumed_repair_context_ids:
                        raise ReplayValidationError("a consumed repair context cannot be reopened")
                    opened_repair_gate_sequences.add(source_sequence)
                    # A later legal rejected gate replaces an older active map;
                    # it does not need the model to consume a stale diagnosis.
                    active_repair_context = {
                        "context": context,
                        "context_sha256": payload["context_sha256"],
                        "source_gate_sequence": source_sequence,
                    }
                    pending_repair_gate_sequence = None
                elif event.kind == "forced_action":
                    if machine.current_phase != payload["phase"]:
                        raise ReplayValidationError(
                            "forced action targets a phase that is not open"
                        )
                    if machine.current_attempt_id != payload["source_attempt_id"]:
                        raise ReplayValidationError("forced action targets a stale phase attempt")
                    calculated_hash = forced_action_sha256(
                        policy=payload["policy"],
                        trigger=payload["trigger"],
                        phase=payload["phase"],
                        source_attempt_id=payload["source_attempt_id"],
                        reason_code=payload["reason_code"],
                        tool=payload["tool"],
                        exact_params=payload["exact_params"],
                        candidate_root=payload.get("candidate_root"),
                        candidate_system=payload.get("candidate_system"),
                        parent_execution_id=payload.get("parent_execution_id"),
                        candidate_resolution=payload["candidate_resolution"],
                        intent_id=payload.get("intent_id"),
                        intent_source=payload.get("intent_source"),
                        action_fingerprint=payload.get("action_fingerprint"),
                    )
                    if calculated_hash != payload["action_sha256"]:
                        raise ReplayValidationError("forced action hash mismatch")
                    resolution = TestCandidateResolution.from_snapshot(
                        payload["candidate_resolution"]
                    )
                    expected = required_test_attempt(
                        state,
                        None,
                        phase=machine.current_phase,
                        attempt_id=machine.current_attempt_id,
                        resolution=resolution,
                    )
                    if expected is None:
                        raise ReplayValidationError(
                            "forced action has no missing must-attempt requirement"
                        )
                    if (
                        payload["tool"] != expected.required_action["tool"]
                        or payload["exact_params"] != expected.required_action["params"]
                        or payload.get("candidate_root") != expected.root
                        or payload.get("candidate_system") != expected.system
                        or payload.get("parent_execution_id") != expected.parent_execution_id
                        or payload["reason_code"] != expected.reason_code
                    ):
                        raise ReplayValidationError(
                            "forced action differs from the production must-attempt policy"
                        )
                    open_envelope(payload, forced=True)
                    forced_attempt_ids.add(payload["source_attempt_id"])
                elif event.kind == "tool_result":
                    if payload["envelope_id"] in answered_envelope_ids:
                        raise ReplayValidationError(
                            f"envelope {payload['envelope_id']!r} already has a "
                            "tool_result; the pairing invariant allows exactly one"
                        )
                    if active_envelope is None:
                        raise ReplayValidationError(
                            "tool result has no action envelope to answer "
                            f"(envelope {payload['envelope_id']!r})"
                        )
                    if payload["envelope_id"] != active_envelope["envelope_id"]:
                        raise ReplayValidationError("tool result references a different envelope")
                    if (
                        payload["tool"] != active_envelope["tool"]
                        or payload["params"] != active_envelope["exact_params"]
                    ):
                        raise ReplayValidationError(
                            "tool result differs from its normalized action envelope"
                        )
                    forced_result = bool(active_envelope["_harness_forced"])
                    source_phase = str(payload.get("source_phase") or "")
                    source_attempt_id = str(payload.get("source_attempt_id") or "")
                    if forced_result and (
                        source_phase != active_envelope["phase"]
                        or source_attempt_id != active_envelope["source_attempt_id"]
                    ):
                        raise ReplayValidationError(
                            "forced result requires the exact forced phase attempt lineage"
                        )
                    if source_attempt_id:
                        if source_attempt_id in attempt_order:
                            if attempt_order[-1] != source_attempt_id:
                                raise ReplayValidationError(
                                    "tool result ordering is not monotone per phase "
                                    f"attempt: {source_attempt_id!r} was already closed "
                                    f"by {attempt_order[-1]!r}"
                                )
                        else:
                            attempt_order.append(source_attempt_id)
                    current_schema = header.schema_version >= 2
                    test_dispatch = _recorded_test_dispatch(
                        payload["tool"],
                        payload["params"],
                    )
                    if (
                        not forced_result
                        and test_dispatch
                        and current_schema
                        and (
                            source_phase != machine.current_phase
                            or source_attempt_id != machine.current_attempt_id
                        )
                    ):
                        raise ReplayValidationError(
                            "schema-v2 test result requires the open phase attempt lineage"
                        )
                    if (
                        not forced_result
                        and test_dispatch
                        and not current_schema
                        and source_attempt_id
                        and source_attempt_id != machine.current_attempt_id
                    ):
                        raise ReplayValidationError("tool result targets a stale phase attempt")
                    result_payload = dict(payload["result"])
                    delivered_gate_word = (
                        _delivered_gate_word(result_payload) or delivered_gate_word
                    )
                    output_ref = result_payload.get("output_ref")
                    if output_ref:
                        output_storage.register(
                            str(output_ref), str(result_payload.get("output", ""))
                        )
                    with bind_tool_result_output_storage(output_storage):
                        result = ToolResult.model_validate(result_payload)
                    actual_executions = tuple(payload.get("actual_executions") or ())
                    facade_only_forced_build = bool(
                        forced_result
                        and active_envelope["tool"] == "build"
                        and not actual_executions
                    )
                    if actual_executions:
                        for actual in actual_executions:
                            actual_payload = dict(actual["result"])
                            actual_output_ref = actual_payload.get("output_ref")
                            if actual_output_ref:
                                output_storage.register(
                                    str(actual_output_ref),
                                    str(actual_payload.get("output", "")),
                                )
                            with bind_tool_result_output_storage(output_storage):
                                actual_result = ToolResult.model_validate(actual_payload)
                            state.ingest_tool_result(
                                StateScope(actual["scope"]),
                                actual["tool"],
                                actual_result,
                                provenance=actual_result.output_ref
                                or next(
                                    iter(actual_result.evidence_refs or actual_result.refs),
                                    None,
                                ),
                                roles=tuple(EvidenceRole(role) for role in actual["roles"]),
                                execution_id=actual["execution_id"],
                                params=actual["params"],
                                source_phase=payload.get("source_phase") or None,
                                source_attempt_id=payload.get("source_attempt_id") or None,
                            )
                    else:
                        state.ingest_tool_result(
                            StateScope(payload["scope"]),
                            payload["tool"],
                            result,
                            provenance=result.output_ref
                            or next(iter(result.evidence_refs or result.refs), None),
                            roles=tuple(EvidenceRole(role) for role in payload["roles"]),
                            execution_id=payload["execution_id"],
                            params=payload["params"],
                            source_phase=payload.get("source_phase") or None,
                            source_attempt_id=payload.get("source_attempt_id") or None,
                        )
                    if facade_only_forced_build:
                        resolution = TestCandidateResolution.from_snapshot(
                            active_envelope["candidate_resolution"]
                        )
                        refusals = forced_test_refusal_receipts(
                            state,
                            attempt_id=machine.current_attempt_id,
                            candidates=resolution.candidates,
                        )
                        if (
                            not result.is_terminal
                            or (result.metadata or {}).get("runner_dispatched") is True
                            or not refusals
                        ):
                            raise ReplayValidationError(
                                "facade-only forced build is allowed only for a "
                                "complete terminal no-runner control receipt"
                            )
                    action_fingerprint = str(
                        active_envelope.get("action_fingerprint") or ""
                    ).strip()
                    if action_fingerprint and payload["tool"] not in {
                        "advisor",
                        "manage_context",
                        "phase",
                        "report",
                    }:
                        result_metadata = dict(result.metadata or {})
                        loop_memory.observe_material_action(
                            action_fingerprint,
                            schema_valid=True,
                            passed_freeze=result.error_code != "CONTRACT_PERSIST_FAILED",
                            dispatched=(result_metadata.get("runner_dispatched") is not False),
                        )
                        if payload.get("output_sha256") and (
                            payload.get("roles")
                            or result.evidence_refs
                            or result.refs
                            or result.facts
                            or result.test_stats is not None
                            or actual_executions
                        ):
                            loop_memory.observe_evidence(
                                payload["output_sha256"],
                                kind=f"tool:{payload['tool']}",
                            )
                    answered_envelope_ids.add(payload["envelope_id"])
                    paired_envelope_count += 1
                    if active_envelope.get("_active_terminal_exception"):
                        exact_params = active_envelope.get("exact_params")
                        exact_params = exact_params if isinstance(exact_params, Mapping) else {}
                        pending_active_terminal_claim = {
                            "phase": str(machine.current_phase or ""),
                            "signal": str(exact_params.get("action") or "").strip().lower(),
                        }
                    repair_context_to_consume = active_envelope.get("_repair_context_to_consume")
                    if repair_context_to_consume:
                        dispatched = _repair_action_dispatch_outcome(
                            result=result.model_dump(mode="python"),
                            actual_executions=actual_executions,
                        )
                        if dispatched:
                            if (
                                active_repair_context is None
                                or active_repair_context["context"].repair_context_id
                                != repair_context_to_consume
                            ):
                                raise ReplayValidationError(
                                    "repair context changed before its action completed"
                                )
                            consumed_repair_context_ids.add(repair_context_to_consume)
                            active_repair_context = None
                    active_envelope = None
                elif event.kind == "validator_observation":
                    evidence_ref = next(iter(payload["evidence_refs"]), None)
                    if payload["validated_facts"] and not evidence_ref:
                        raise ReplayValidationError("validated facts require evidence provenance")
                    recovery = payload["validated_facts"].get(ANALYSIS_RECOVERY_FACT)
                    if recovery is not None:
                        if pending_analysis_recovery_observation is not None:
                            raise ReplayValidationError(
                                "analysis recovery observation has no final gate"
                            )
                        pending_analysis_recovery_observation = _validate_analysis_recovery_audit(
                            recovery,
                            phase=payload["phase"],
                            control_disposition=payload.get("control_disposition"),
                        )
                    # Production records these facts only after the paired gate
                    # decision is accepted. The observation event is audit data,
                    # not a second state mutation.
                elif event.kind == "gate_outcome_revised":
                    revision = GateOutcomeRevisedPayload.model_validate(payload)
                    if (
                        delivered_gate_word is None
                        or str(delivered_gate_word["decision_id"]) != revision.delivered_decision_id
                    ):
                        raise ReplayValidationError(
                            "gate_outcome_revised names a word that was never delivered"
                        )
                    gate_outcome_revisions.add(
                        (revision.delivered_decision_id, revision.revised_decision_id)
                    )
                elif event.kind == "gate_decision":
                    gate_decision_count += 1
                    _verify_gate_word_lineage(
                        payload,
                        delivered=delivered_gate_word,
                        seen_decision_ids=seen_decision_ids,
                        revisions=gate_outcome_revisions,
                    )
                    if str(payload.get("decision_id") or "").strip():
                        seen_decision_ids.add(str(payload["decision_id"]).strip())
                    delivered_gate_word = None
                    if active_repair_context is not None:
                        finalizer_gate = bool(
                            pending_finalizer_gate is not None
                            and payload.get("expected_accepted") is True
                            and str(payload.get("code") or "") == "agent_no_progress"
                            and str(payload.get("phase") or "") == pending_finalizer_gate["phase"]
                            and str(payload.get("signal") or "") == "done"
                        )
                        if not finalizer_gate and pending_active_terminal_claim is None:
                            raise ReplayValidationError(
                                "gate decision cannot bypass an active repair context; "
                                "a complete model terminal action must precede it"
                            )
                        if not finalizer_gate and (
                            str(payload.get("phase") or "")
                            != pending_active_terminal_claim["phase"]
                            or str(payload.get("signal") or "")
                            != pending_active_terminal_claim["signal"]
                        ):
                            raise ReplayValidationError(
                                "gate decision differs from its model terminal action"
                            )
                        if finalizer_gate:
                            pending_finalizer_gate = None
                        else:
                            pending_active_terminal_claim = None
                            if payload.get("expected_accepted") is False:
                                pending_rejected_terminal_gate = {
                                    "phase": str(payload.get("phase") or ""),
                                    "signal": str(payload.get("signal") or ""),
                                    "phase_attempt_id": str(payload.get("source_attempt_id") or ""),
                                    "judge_disposition": str(
                                        payload.get("control_disposition") or ""
                                    ),
                                    "blocker_id": str(payload.get("code") or ""),
                                }
                    recovery = payload["validated_facts"].get(ANALYSIS_RECOVERY_FACT)
                    if recovery is None and pending_analysis_recovery_observation is not None:
                        raise ReplayValidationError(
                            "analysis recovery observation differs from final gate"
                        )
                    if recovery is not None:
                        validated_recovery = _validate_analysis_recovery_audit(
                            recovery,
                            phase=payload["phase"],
                            control_disposition=payload.get("control_disposition"),
                        )
                        if pending_analysis_recovery_observation != validated_recovery:
                            raise ReplayValidationError(
                                "analysis recovery observation differs from final gate"
                            )
                        analysis_recovery_count += 1
                        if analysis_recovery_count > 1:
                            raise ReplayValidationError(
                                "analysis facts recovery may occur at most once per run"
                            )
                        pending_analysis_recovery_observation = None
                    if payload["phase"] != machine.current_phase:
                        raise ReplayValidationError("gate decision targets the wrong open phase")
                    source_attempt_id = payload.get("source_attempt_id")
                    current_forced_contract = str(machine.current_attempt_id) in forced_attempt_ids
                    if (
                        payload["phase"] == "test"
                        and payload["expected_accepted"]
                        and (current_forced_contract or header.schema_version >= 2)
                        and source_attempt_id != machine.current_attempt_id
                    ):
                        raise ReplayValidationError(
                            "accepted test gate requires the current attempt id"
                        )
                    if (
                        source_attempt_id is not None
                        and source_attempt_id != machine.current_attempt_id
                    ):
                        raise ReplayValidationError("gate decision targets a stale phase attempt")
                    resolution_payload = payload.get("test_candidate_resolution")
                    if (
                        payload["phase"] == "test"
                        and payload["expected_accepted"]
                        and (current_forced_contract or header.schema_version >= 2)
                        and resolution_payload is None
                    ):
                        raise ReplayValidationError(
                            "accepted test gate requires candidate resolution"
                        )
                    if (
                        payload["phase"] == "test"
                        and payload["expected_accepted"]
                        and resolution_payload is not None
                    ):
                        resolution = TestCandidateResolution.from_snapshot(resolution_payload)
                        requirement = required_test_attempt(
                            state,
                            None,
                            phase=machine.current_phase,
                            attempt_id=machine.current_attempt_id,
                            resolution=resolution,
                        )
                        if requirement is not None:
                            raise ReplayValidationError(
                                "accepted test gate has no candidate-bound terminal receipt"
                            )
                        validator_state = ValidatorState(payload["validator_state"])
                        if (
                            resolution.status != "available"
                            and has_test_candidate_refresh_receipt(
                                state,
                                attempt_id=machine.current_attempt_id,
                            )
                            and validator_state is not ValidatorState.UNAVAILABLE
                        ):
                            raise ReplayValidationError(
                                "unresolved refreshed test coordinates require a non-green gate"
                            )
                        if resolution.status == "available":
                            receipts = terminal_test_receipts(
                                state,
                                attempt_id=machine.current_attempt_id,
                                candidates=resolution.candidates,
                            )
                            refusals = forced_test_refusal_receipts(
                                state,
                                attempt_id=machine.current_attempt_id,
                                candidates=resolution.candidates,
                            )
                            if (
                                refusals
                                and not receipts
                                and validator_state
                                not in {ValidatorState.RED, ValidatorState.UNAVAILABLE}
                            ):
                                raise ReplayValidationError(
                                    "forced pre-execution refusal requires a non-green gate"
                                )
                    claim = PhaseClaim(
                        phase=payload["phase"],
                        signal=payload["signal"],
                        claimed_outcome=payload["claimed_outcome"],
                        key_results=payload["key_results"],
                        reason=payload["reason"],
                        evidence_refs=tuple(payload["evidence_refs"]),
                    )
                    ownership = {}
                    if payload.get("control_disposition") is not None:
                        ownership["control_disposition"] = payload["control_disposition"]
                    if payload.get("blocker_owner") is not None:
                        ownership["blocker_owner"] = payload["blocker_owner"]
                    gate = validate_phase_claim(
                        claim,
                        ValidatorState(payload["validator_state"]),
                        reason=payload["reason"],
                        evidence_refs=tuple(payload["evidence_refs"]),
                        validated_facts=payload["validated_facts"],
                        code=payload.get("code"),
                        **ownership,
                    )
                    if gate.accepted is not payload["expected_accepted"]:
                        raise ReplayMismatchError("gate acceptance differs from transcript")
                    if gate.validated_outcome.value != payload["expected_outcome"]:
                        raise ReplayMismatchError("gate outcome differs from transcript")
                    if (
                        payload.get("control_disposition") is not None
                        and gate.control_disposition.value != payload["control_disposition"]
                    ):
                        raise ReplayMismatchError(
                            "gate control disposition differs from transcript"
                        )
                    if (
                        payload.get("blocker_owner") is not None
                        and gate.blocker_owner.value != payload["blocker_owner"]
                    ):
                        raise ReplayMismatchError("gate blocker owner differs from transcript")
                    if not gate.accepted:
                        pending_record = None
                    else:
                        for key, value in gate.validated_facts.items():
                            if str(key).startswith("run."):
                                # Mirror of _record_gate_facts (#28): control
                                # facts never become run facts, in production
                                # and replay alike.
                                continue
                            evidence_ref = next(
                                iter(gate.evidence_refs), f"validator://{claim.phase}"
                            )
                            state.set_fact(
                                key,
                                value,
                                evidence_ref=evidence_ref,
                                source_phase=claim.phase,
                                source_attempt_id=machine.current_attempt_id,
                            )
                        if gate.evidence_refs:
                            state.record_phase_evidence(
                                str(machine.current_attempt_id), gate.evidence_refs
                            )
                        pending_record = machine.close_attempt(gate)
                    repair_gate_events[event.sequence] = dict(payload)
                    if not gate.accepted and gate.control_disposition.value == "repair_required":
                        pending_repair_gate_sequence = event.sequence
                elif event.kind == "phase_transition":
                    if pending_record is None:
                        raise ReplayValidationError("phase transition has no validated attempt")
                    repair_payload = payload.get("repair_request")
                    if repair_payload is not None:
                        request = RepairRequest.from_metadata(repair_payload)
                        decision = transition_policy.request_repair(
                            request,
                            state=state,
                            budgets=budgets,
                            source_record=pending_record,
                        )
                        accepted = decision.route.kind == "repair"
                        repairs.append(
                            RepairRouteResult(
                                edge=(request.from_phase, request.target_phase),
                                accepted=accepted,
                                reason_code=decision.reason_code,
                            )
                        )
                        if accepted:
                            budgets = RepairBudgets(
                                global_remaining=max(0, budgets.global_remaining - 1),
                                phase_remaining={
                                    **dict(budgets.phase_remaining),
                                    request.from_phase: max(
                                        0, budgets.phase_remaining.get(request.from_phase, 0) - 1
                                    ),
                                },
                            )
                    else:
                        decision = transition_policy.decide(
                            pending_record,
                            state=state,
                            budgets=budgets,
                        )
                    if (
                        decision.route.kind != payload["expected_kind"]
                        or decision.route.target != payload["expected_target"]
                        or decision.reason_code != payload["expected_reason_code"]
                    ):
                        raise ReplayMismatchError("phase transition differs from production policy")
                    appended = machine.apply(decision)
                    for record in appended:
                        state.record_phase_record(record)
                    pending_record = None
                    active_repair_context = None
                elif event.kind == "loop_decision":
                    event_payload = dict(payload["event"])
                    recorded_recurrence_count = event_payload.pop("recurrence_count", None)
                    decision = loop_memory.observe(LoopEvent(**event_payload))
                    if (
                        recorded_recurrence_count is not None
                        and int(recorded_recurrence_count) != decision.recurrence_count
                    ):
                        raise ReplayMismatchError(
                            "loop recurrence count differs from production LoopMemory"
                        )
                    if (
                        decision.decision != payload["expected_decision"]
                        or decision.reason_code != payload["expected_reason_code"]
                    ):
                        raise ReplayMismatchError(
                            "loop decision differs from production LoopMemory"
                        )
                    loop_decisions.append(decision)
                elif event.kind == "completion_claim_decision":
                    if pending_rejected_terminal_gate is not None:
                        if (
                            str(payload.get("phase_attempt_id") or "")
                            != pending_rejected_terminal_gate["phase_attempt_id"]
                            or str(payload.get("claim_kind") or "")
                            != pending_rejected_terminal_gate["signal"]
                            or str(payload.get("judge_disposition") or "")
                            != pending_rejected_terminal_gate["judge_disposition"]
                            or str(payload.get("blocker_id") or "")
                            != pending_rejected_terminal_gate["blocker_id"]
                        ):
                            raise ReplayValidationError(
                                "completion claim decision differs from its "
                                "rejected terminal gate"
                            )
                    completion_event = CompletionClaimEvent(
                        phase_attempt_id=payload["phase_attempt_id"],
                        claim_kind=payload["claim_kind"],
                        judge_disposition=payload["judge_disposition"],
                        blocker_id=payload["blocker_id"],
                        mechanical_evidence_digest=payload["mechanical_evidence_digest"],
                        assessment_fingerprints=tuple(payload["assessment_fingerprints"]),
                        open_job_fingerprints=tuple(payload["open_job_fingerprints"]),
                        target_fingerprint=payload["target_fingerprint"],
                        config_fingerprint=payload["config_fingerprint"],
                        fact_fingerprint=payload["fact_fingerprint"],
                        evidence_refs=tuple(payload["evidence_refs"]),
                    )
                    completion = loop_memory.observe_completion_claim(completion_event)
                    if (
                        completion.decision != payload["expected_decision"]
                        or completion.recurrence_count != payload["expected_recurrence_count"]
                        or completion.reason_code != payload["expected_reason_code"]
                        or completion.close_phase is not payload["expected_close_phase"]
                    ):
                        raise ReplayMismatchError(
                            "completion-claim decision differs from production LoopMemory"
                        )
                    if pending_rejected_terminal_gate is not None:
                        if completion.decision == "agent_no_progress" and completion.close_phase:
                            pending_finalizer_gate = {
                                "phase": pending_rejected_terminal_gate["phase"],
                                "phase_attempt_id": pending_rejected_terminal_gate[
                                    "phase_attempt_id"
                                ],
                            }
                        pending_rejected_terminal_gate = None
                elif event.kind == "evidence_close":
                    if (
                        active_envelope is not None
                        or pending_record is not None
                        or active_repair_context is not None
                        or pending_active_terminal_claim is not None
                        or pending_rejected_terminal_gate is not None
                        or pending_finalizer_gate is not None
                    ):
                        raise ReplayValidationError("evidence closed with unconsumed control state")
                    unresolved_terminal_jobs = (
                        set(terminal_exit_codes) - terminal_unpersisted_jobs - settled_jobs
                    )
                    if unresolved_terminal_jobs and not barrier_integrity_failure_seen:
                        raise ReplayValidationError(
                            "evidence closed with unresolved terminal settlement: "
                            + ", ".join(sorted(unresolved_terminal_jobs))
                        )
                    state.seal(
                        finalized_at=header.finalized_at,
                        close_reason=payload["reason"],
                    )
                    snapshot = finalizer.finalize(
                        state,
                        EvidenceCloseReason(payload["reason"]),
                    )
                elif event.kind == "claim_transition":
                    # Plan 6 Stage C: claim-graph transitions are replayed by
                    # claim_graph.load(), not by this walker — pass through so
                    # a transcript that carries them still verifies end-to-end.
                    pass
                elif event.kind == "job_terminal_observed":
                    if snapshot is not None:
                        raise ReplayValidationError(
                            "job terminal observation cannot follow evidence_close"
                        )
                    job_id = payload["job_id"]
                    exit_code = int(payload["exit_code"])
                    if job_id in legacy_unsettled_jobs:
                        raise ReplayValidationError(
                            "legacy job_unsettled cannot precede terminal observation"
                        )
                    if job_id in settled_jobs:
                        raise ReplayValidationError(
                            "terminal observation must precede job settlement"
                        )
                    if job_id in live_at_close_jobs:
                        raise ReplayValidationError(
                            "a job cannot be both terminal and live at close"
                        )
                    if job_id in terminal_exit_codes:
                        raise ReplayValidationError(
                            f"duplicate terminal observation for job {job_id!r}"
                        )
                    terminal_exit_codes[job_id] = exit_code
                    loop_memory.observe_job_transition(
                        job_id,
                        "terminal",
                        progress_fingerprint=canonical_sha256(payload),
                    )
                elif event.kind == "job_terminal_unpersisted":
                    if snapshot is not None:
                        raise ReplayValidationError(
                            "terminal persistence failure cannot follow evidence_close"
                        )
                    job_id = payload["job_id"]
                    exit_code = int(payload["exit_code"])
                    if job_id not in terminal_exit_codes:
                        raise ReplayValidationError(
                            "terminal-unpersisted requires a prior terminal observation"
                        )
                    if terminal_exit_codes[job_id] != exit_code:
                        raise ReplayValidationError(
                            "terminal-unpersisted exit code differs from its observation"
                        )
                    if job_id in terminal_unpersisted_jobs:
                        raise ReplayValidationError(
                            f"duplicate terminal persistence failure for job {job_id!r}"
                        )
                    if job_id in settled_jobs:
                        raise ReplayValidationError(
                            "a settled job cannot become terminal-unpersisted"
                        )
                    terminal_unpersisted_jobs.add(job_id)
                    loop_memory.observe_job_transition(
                        job_id,
                        "terminal_unpersisted",
                        progress_fingerprint=canonical_sha256(payload),
                    )
                    evidence_ref = payload["obligation_ref"]
                    if evidence_ref == "unpersisted":
                        evidence_ref = payload.get("log_ref") or evidence_ref
                    state.set_fact(
                        f"job_terminal_unpersisted.{job_id}",
                        payload,
                        evidence_ref=evidence_ref,
                    )
                    state.record_conflict(f"job_terminal_unpersisted:{job_id}")
                elif event.kind == "job_live_at_close":
                    if snapshot is not None:
                        raise ReplayValidationError(
                            "live-at-close observation cannot follow evidence_close"
                        )
                    job_id = payload["job_id"]
                    if job_id in terminal_exit_codes:
                        raise ReplayValidationError(
                            "a terminal job cannot be recorded as live at close"
                        )
                    if job_id in live_at_close_jobs:
                        raise ReplayValidationError(
                            f"duplicate live-at-close observation for job {job_id!r}"
                        )
                    if job_id in settled_jobs or job_id in legacy_unsettled_jobs:
                        raise ReplayValidationError("a closed job cannot become live at close")
                    live_at_close_jobs.add(job_id)
                    loop_memory.observe_job_transition(
                        job_id,
                        "live_at_close",
                        progress_fingerprint=canonical_sha256(payload),
                    )
                    state.set_fact(
                        f"job_live_at_close.{job_id}",
                        payload,
                        evidence_ref=payload["obligation_ref"],
                    )
                    state.record_conflict(f"job_live_at_close:{job_id}")
                elif event.kind == "job_unsettled":
                    # Legacy Plan-8 vocabulary, retained only for archived
                    # streams. New runs distinguish terminal persistence
                    # failure from a process that was physically live at close.
                    unsettled_job = payload["job_id"]
                    if unsettled_job in terminal_exit_codes:
                        raise ReplayValidationError(
                            "legacy job_unsettled cannot describe a terminal job"
                        )
                    if unsettled_job in settled_jobs or unsettled_job in live_at_close_jobs:
                        raise ReplayValidationError(
                            "legacy job_unsettled contradicts the recorded job lifecycle"
                        )
                    if unsettled_job in legacy_unsettled_jobs:
                        raise ReplayValidationError(
                            f"duplicate legacy job_unsettled for job {unsettled_job!r}"
                        )
                    legacy_unsettled_jobs.add(unsettled_job)
                    state.set_fact(
                        f"{OPEN_OBLIGATIONS_FACT}.{unsettled_job}",
                        payload.get("obligation") or {},
                        evidence_ref=payload["evidence_ref"],
                    )
                    state.record_conflict(f"job_unsettled:{unsettled_job}")
                elif event.kind == "job_settled":
                    # Plan 8 Stage 1: a detached job's books closing is an
                    # evidence-layer fact (a receipt on disk), not a control
                    # decision this walk re-derives. It carries no envelope, no
                    # claim and no attempt lineage, so it consumes nothing and
                    # blocks nothing — including between a gate_decision and
                    # the phase_transition that must follow it.
                    if snapshot is not None:
                        raise ReplayValidationError("job settlement cannot follow evidence_close")
                    settled_job = payload["job_id"]
                    if settled_job in settled_job_events:
                        prior_receipt, prior_exit = settled_job_events[settled_job]
                        raise ReplayValidationError(
                            "duplicate job_settled for job "
                            f"{settled_job!r}; first receipt/exit was "
                            f"{prior_receipt!r}/{prior_exit}"
                        )
                    if settled_job in terminal_unpersisted_jobs:
                        raise ReplayValidationError(
                            "a terminal-unpersisted job cannot also be settled"
                        )
                    if settled_job in live_at_close_jobs:
                        raise ReplayValidationError("a job live at close cannot also be settled")
                    if settled_job in legacy_unsettled_jobs:
                        raise ReplayValidationError("a legacy-unsettled job cannot also be settled")
                    if settled_job in terminal_exit_codes and terminal_exit_codes[
                        settled_job
                    ] != int(payload["exit_code"]):
                        raise ReplayValidationError(
                            "job settlement exit code differs from its observation"
                        )
                    settled_job_events[settled_job] = (
                        str(payload["receipt_id"]),
                        int(payload["exit_code"]),
                    )
                    settled_jobs.add(settled_job)
                    loop_memory.observe_job_transition(
                        settled_job,
                        "settled",
                        progress_fingerprint=canonical_sha256(payload),
                    )
                elif event.kind == "job_barrier_integrity_failure":
                    if snapshot is not None:
                        raise ReplayValidationError(
                            "job barrier integrity failure cannot follow evidence_close"
                        )
                    if barrier_integrity_failure_seen:
                        raise ReplayValidationError("duplicate job barrier integrity failure")
                    barrier_integrity_failure_seen = True
                    state.set_fact(
                        "job_barrier_integrity_failure",
                        payload,
                        evidence_ref="control:job_barrier_integrity_failure",
                    )
                    state.record_conflict("job_barrier_integrity_failure")
                elif event.kind == "job_stall_observed":
                    if snapshot is not None:
                        raise ReplayValidationError(
                            "job stall observation cannot follow evidence_close"
                        )
                    fingerprint = canonical_sha256(payload)
                    if fingerprint in stall_event_fingerprints:
                        raise ReplayValidationError("duplicate job stall observation")
                    stall_event_fingerprints.add(fingerprint)
                    job_id = payload["job_id"]
                    if job_id in terminal_exit_codes:
                        raise ReplayValidationError(
                            "job stall observation cannot follow terminal observation"
                        )
                    fact_key = f"job_stall_observed.{job_id}.{fingerprint[:12]}"
                    state.set_fact(
                        fact_key,
                        payload,
                        evidence_ref=payload["diagnostic_ref"],
                    )
                    transition = job_stall_transition(payload)
                    if transition is not None:
                        loop_memory.observe_job_transition(
                            job_id,
                            transition,
                            progress_fingerprint=fingerprint,
                        )
                else:  # pragma: no cover - ControlEvent validation owns this
                    raise ReplayValidationError(f"unsupported event kind: {event.kind}")
            except ReplayValidationError:
                raise
            except Exception as exc:
                raise ReplayValidationError(
                    f"event {event.sequence} ({event.kind}) is impossible: {exc}"
                ) from exc
            produced.append(
                {"sequence": event.sequence, "kind": event.kind, "payload": event.payload}
            )

        if pending_repair_gate_sequence is not None:
            raise ReplayValidationError(
                "transcript ended before repair_context_opened followed a repair-required gate"
            )
        if snapshot is None:
            raise ReplayValidationError("transcript did not reach evidence_close")
        if pending_analysis_recovery_observation is not None:
            raise ReplayValidationError("analysis recovery observation has no final gate")
        if paired_envelope_count != envelope_count:
            raise ReplayValidationError(
                "the pairing invariant requires exactly one tool_result per "
                f"envelope: {envelope_count} recorded, {paired_envelope_count} answered"
            )
        unconsumed = tuple(
            name
            for name, present in (
                ("active_envelope", active_envelope is not None),
                ("pending_phase_record", pending_record is not None),
                ("active_repair_context", active_repair_context is not None),
            )
            if present
        )
        digest = canonical_sha256(produced)
        actual_snapshot = snapshot.model_dump(mode="json")
        if self.verify_expected:
            if header.expected_snapshot.get("schema_version") == 3:
                actual_snapshot = _legacy_v3_snapshot_projection(snapshot)
            if actual_snapshot != header.expected_snapshot:
                raise ReplayMismatchError("replayed snapshot differs from frozen expectation")
            if digest != header.expected_event_digest:
                raise ReplayMismatchError("produced event digest differs from frozen expectation")
            if unconsumed:
                raise ReplayMismatchError(f"unconsumed replay state: {unconsumed!r}")
        return ReplayResult(
            header=header,
            snapshot=snapshot,
            expected_snapshot=header.expected_snapshot,
            produced_event_digest=digest,
            expected_event_digest=header.expected_event_digest,
            unconsumed_events=unconsumed,
            phase_records=machine.records,
            loop_decisions=tuple(loop_decisions),
            repair_routes=tuple(repairs),
            planner_response_count=planner_response_count,
            executed_envelope_count=envelope_count,
            paired_envelope_count=paired_envelope_count,
            gate_decision_count=gate_decision_count,
            skipped_event_kinds=dict(skipped_kinds),
        )


__all__ = [
    "ControlReplayRunner",
    "ReplayHeader",
    "ReplayMismatchError",
    "ReplayResult",
    "ReplayTranscript",
    "ReplayValidationError",
]
