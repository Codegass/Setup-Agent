"""Dependency-aware phase routing and bounded repair decisions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from .evidence_state import RunEvidenceState, StateScope
from .phase_machine import (
    PhaseAttemptRecord,
    PhaseOutcome,
    PhaseSkipRecord,
    _policy_skip_record,
)

_ROUTE_KINDS = {"advance", "repair", "evidence_close", "report", "flow_close"}
_REPAIR_EDGES = {("test", "build"), ("build", "analyze")}
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]*$")


def _normalized_signature(value: str) -> str:
    return " ".join(str(value).strip().lower().split())


@dataclass(frozen=True)
class RepairRequest:
    from_phase: str
    target_phase: str
    reason_code: str
    failure_signature: str
    hypothesis: str
    evidence_refs: tuple[str, ...]
    source_attempt_id: str = ""

    def __post_init__(self) -> None:
        from_phase = str(self.from_phase).strip().lower()
        target_phase = str(self.target_phase).strip().lower()
        reason_code = str(self.reason_code).strip().lower()
        signature = _normalized_signature(self.failure_signature)
        hypothesis = str(self.hypothesis).strip()
        refs = tuple(
            dict.fromkeys(
                str(ref).strip()
                for ref in self.evidence_refs
                if ref is not None and str(ref).strip()
            )
        )
        if not from_phase or not target_phase:
            raise ValueError("repair request requires source and target phases")
        if not _REASON_CODE.fullmatch(reason_code):
            raise ValueError("repair reason_code must be typed snake_case")
        if not signature:
            raise ValueError("repair request requires a normalized failure signature")
        if not hypothesis:
            raise ValueError("repair request requires an explicit hypothesis")
        if not refs:
            raise ValueError("repair request requires current-attempt evidence refs")
        object.__setattr__(self, "from_phase", from_phase)
        object.__setattr__(self, "target_phase", target_phase)
        object.__setattr__(self, "reason_code", reason_code)
        object.__setattr__(self, "failure_signature", signature)
        object.__setattr__(self, "hypothesis", hypothesis)
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(self, "source_attempt_id", str(self.source_attempt_id).strip())

    def to_metadata(self) -> dict[str, Any]:
        return {
            "from_phase": self.from_phase,
            "target_phase": self.target_phase,
            "source_attempt_id": self.source_attempt_id,
            "reason_code": self.reason_code,
            "failure_signature": self.failure_signature,
            "hypothesis": self.hypothesis,
            "evidence_refs": list(self.evidence_refs),
        }

    @classmethod
    def from_metadata(cls, value: Mapping[str, Any]) -> "RepairRequest":
        if not isinstance(value, Mapping):
            raise TypeError("repair request metadata must be a mapping")
        evidence_refs = value.get("evidence_refs") or ()
        if isinstance(evidence_refs, str) or not isinstance(evidence_refs, (list, tuple)):
            raise TypeError("repair evidence refs must be a list")
        return cls(
            from_phase=str(value.get("from_phase") or ""),
            target_phase=str(value.get("target_phase") or ""),
            source_attempt_id=str(value.get("source_attempt_id") or ""),
            reason_code=str(value.get("reason_code") or ""),
            failure_signature=str(value.get("failure_signature") or ""),
            hypothesis=str(value.get("hypothesis") or ""),
            evidence_refs=tuple(evidence_refs),
        )


@dataclass(frozen=True)
class RepairBudgets:
    global_remaining: int
    phase_remaining: Mapping[str, int] = field(default_factory=dict)

    @classmethod
    def available(cls) -> "RepairBudgets":
        return cls(global_remaining=2, phase_remaining={"test": 1, "build": 1})

    @classmethod
    def none(cls) -> "RepairBudgets":
        return cls(global_remaining=0, phase_remaining={})

    def allows(self, request: RepairRequest) -> bool:
        return self.global_remaining > 0 and self.phase_remaining.get(request.from_phase, 0) > 0


@dataclass(frozen=True)
class PhaseRoute:
    kind: str
    source_attempt_id: str
    target: str | None = None
    new_attempt: bool = False
    reason_code: str = ""

    def __post_init__(self) -> None:
        if self.kind not in _ROUTE_KINDS:
            raise ValueError(f"unknown phase route kind: {self.kind!r}")
        if not _REASON_CODE.fullmatch(str(self.reason_code).strip()):
            raise ValueError("phase route requires a typed reason code")
        if self.kind in {"advance", "repair", "report"} and not self.target:
            raise ValueError(f"{self.kind} route requires a target")
        if self.kind in {"evidence_close", "flow_close"} and self.target is not None:
            raise ValueError(f"{self.kind} route cannot name a phase target")
        if self.kind == "repair" and not self.new_attempt:
            raise ValueError("repair route must open a new target attempt")
        if self.kind != "repair" and self.new_attempt:
            raise ValueError("only a repair route may flag a new attempt")


@dataclass(frozen=True)
class TransitionDecision:
    route: PhaseRoute
    skips: tuple[PhaseSkipRecord, ...] = ()
    reason_code: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "skips", tuple(self.skips))
        if self.skips and self.route.kind != "evidence_close":
            raise ValueError("downstream skips are valid only at evidence-close")
        if not self.reason_code:
            object.__setattr__(self, "reason_code", self.route.reason_code)
        elif self.reason_code != self.route.reason_code:
            raise ValueError("decision and route reason codes must match")


class RepairRecurrenceGuard(Protocol):
    def check(
        self,
        request: RepairRequest,
        relevant_state_vector: Mapping[str, int],
    ) -> str | None: ...


class StateBackedRepairGuard:
    """Reject an accepted repair signature repeated without relevant progress."""

    def __init__(self, state: RunEvidenceState):
        self.state = state

    def check(
        self,
        request: RepairRequest,
        relevant_state_vector: Mapping[str, int],
    ) -> str | None:
        vector = dict(relevant_state_vector)
        for record in reversed(self.state.repair_records):
            if not record.accepted:
                continue
            if (
                record.from_phase == request.from_phase
                and record.target_phase == request.target_phase
                and record.failure_signature == request.failure_signature
                and record.state_vector == vector
            ):
                return "repair_without_progress"
        return None


class PhaseTransitionPolicy:
    def __init__(self, repair_guard: RepairRecurrenceGuard | None = None):
        self.repair_guard = repair_guard

    @staticmethod
    def _route(
        kind: str,
        record: PhaseAttemptRecord,
        *,
        target: str | None = None,
        new_attempt: bool = False,
        reason_code: str,
    ) -> PhaseRoute:
        return PhaseRoute(
            kind=kind,
            source_attempt_id=record.attempt_id,
            target=target,
            new_attempt=new_attempt,
            reason_code=reason_code,
        )

    @staticmethod
    def _skip(
        phase: str,
        *,
        state: RunEvidenceState,
        reason: str,
        prerequisite_ref: str,
    ) -> PhaseSkipRecord:
        ordinal = 1 + sum(record.phase == phase for record in state.phase_records)
        refs = (prerequisite_ref,) if prerequisite_ref else ()
        return _policy_skip_record(
            phase=phase,
            attempt_id=f"{phase}-skip-{ordinal}",
            reason=reason,
            evidence_refs=refs,
            prerequisite_ref=prerequisite_ref,
        )

    def _evidence_close(
        self,
        record: PhaseAttemptRecord,
        *,
        state: RunEvidenceState,
        reason_code: str,
        skipped_phases: tuple[str, ...] = (),
        prerequisite_ref: str = "",
    ) -> TransitionDecision:
        skips = tuple(
            self._skip(
                phase,
                state=state,
                reason=reason_code,
                prerequisite_ref=prerequisite_ref,
            )
            for phase in skipped_phases
        )
        return TransitionDecision(
            route=self._route("evidence_close", record, reason_code=reason_code),
            skips=skips,
            reason_code=reason_code,
        )

    def decide(
        self,
        record: PhaseAttemptRecord,
        *,
        state: RunEvidenceState,
        budgets: RepairBudgets,
    ) -> TransitionDecision:
        """Route a validated terminal record; never invent a repair proposal."""
        del budgets  # A budget permits proposals; it never creates one.
        phase = record.phase
        if phase == "provision":
            if state.fact_value("provision.workspace_ready") is True:
                return TransitionDecision(
                    self._route(
                        "advance", record, target="analyze", reason_code="workspace_ready"
                    )
                )
            ref = state.fact_provenance("provision.workspace_ready") or _first_ref(record)
            return self._evidence_close(
                record,
                state=state,
                reason_code="workspace_not_ready",
                skipped_phases=("analyze", "build", "test"),
                prerequisite_ref=ref,
            )
        if phase == "analyze":
            if state.fact_value("analysis.build_entry_ready") is True:
                return TransitionDecision(
                    self._route(
                        "advance", record, target="build", reason_code="analysis_ready"
                    )
                )
            ref = state.fact_provenance("analysis.build_entry_ready") or _first_ref(record)
            return self._evidence_close(
                record,
                state=state,
                reason_code="analysis_not_ready",
                skipped_phases=("build", "test"),
                prerequisite_ref=ref,
            )
        if phase == "build":
            test_ready = state.fact_value("build.test_entry_ready") is True
            if record.outcome in {PhaseOutcome.SUCCESS, PhaseOutcome.PARTIAL} and test_ready:
                return TransitionDecision(
                    self._route("advance", record, target="test", reason_code="test_entry_ready")
                )
            ref = state.fact_provenance("build.test_entry_ready") or _first_ref(record)
            return self._evidence_close(
                record,
                state=state,
                reason_code="build_not_ready",
                skipped_phases=("test",),
                prerequisite_ref=ref,
            )
        if phase == "test":
            return self._evidence_close(
                record,
                state=state,
                reason_code="test_terminal",
            )
        if phase == "report":
            return TransitionDecision(
                self._route("flow_close", record, reason_code="report_terminal")
            )
        raise ValueError(f"no transition policy for phase {phase!r}")

    def request_repair(
        self,
        request: RepairRequest,
        *,
        state: RunEvidenceState,
        budgets: RepairBudgets,
        current_state_vector: Mapping[str, int] | None = None,
        source_outcome: PhaseOutcome | str | None = None,
        source_record: PhaseAttemptRecord | None = None,
    ) -> TransitionDecision:
        if source_record is not None:
            if (
                source_record.phase != request.from_phase
                or source_record.attempt_id != request.source_attempt_id
            ):
                raise ValueError("repair source record does not match the request")
            source_outcome = source_record.outcome
        vector = dict(
            self._repair_state_vector(request, state)
            if current_state_vector is None
            else current_state_vector
        )
        rejection = self._repair_rejection(
            request,
            state=state,
            budgets=budgets,
            vector=vector,
            source_outcome=source_outcome,
        )
        if rejection is not None and not _REASON_CODE.fullmatch(str(rejection)):
            raise ValueError("repair guard must return a typed reason code")
        accepted = rejection is None
        reason = "repair_accepted" if accepted else rejection
        state.record_repair(
            request,
            state_vector=vector,
            accepted=accepted,
            decision_reason=reason,
        )
        if accepted:
            route = PhaseRoute(
                kind="repair",
                source_attempt_id=request.source_attempt_id,
                target=request.target_phase,
                new_attempt=True,
                reason_code="repair_accepted",
            )
            return TransitionDecision(route=route, reason_code="repair_accepted")
        if source_record is not None:
            # Rejection denies only the rollback. Route the already validated
            # source attempt by its ordinary prerequisites; a bad proposal
            # must not suppress a runnable downstream phase.
            return self.decide(source_record, state=state, budgets=budgets)
        route = PhaseRoute(
            kind="evidence_close",
            source_attempt_id=request.source_attempt_id,
            reason_code=reason,
        )
        skips = ()
        if request.from_phase == "build":
            prerequisite_ref = request.evidence_refs[0]
            skips = (
                self._skip(
                    "test",
                    state=state,
                    reason=reason,
                    prerequisite_ref=prerequisite_ref,
                ),
            )
        return TransitionDecision(route=route, skips=skips, reason_code=reason)

    def _repair_rejection(
        self,
        request: RepairRequest,
        *,
        state: RunEvidenceState,
        budgets: RepairBudgets,
        vector: Mapping[str, int],
        source_outcome: PhaseOutcome | str | None,
    ) -> str | None:
        if source_outcome is not None and PhaseOutcome(source_outcome) is PhaseOutcome.SUCCESS:
            return "repair_source_green"
        if (request.from_phase, request.target_phase) not in _REPAIR_EDGES:
            return "illegal_edge"
        if not request.source_attempt_id:
            return "missing_source_attempt"
        if not budgets.allows(request):
            return "repair_budget_exhausted"
        current_refs = set(state.evidence_refs_for_attempt(request.source_attempt_id))
        if not current_refs.intersection(request.evidence_refs):
            return "stale_repair_evidence"
        guard = self.repair_guard or StateBackedRepairGuard(state)
        return guard.check(request, vector)

    @staticmethod
    def _repair_state_vector(
        request: RepairRequest,
        state: RunEvidenceState,
    ) -> dict[str, int]:
        scopes = (
            (StateScope.ENVIRONMENT, StateScope.DEPENDENCIES, StateScope.ARTIFACTS)
            if request.target_phase == "build"
            else (StateScope.PROJECT_ANALYSIS, StateScope.DEPENDENCIES)
        )
        return state.state_vector(scopes)


def _first_ref(record: PhaseAttemptRecord) -> str:
    return record.evidence_refs[0] if record.evidence_refs else ""
