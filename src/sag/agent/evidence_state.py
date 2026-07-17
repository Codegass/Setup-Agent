"""Engine-owned, append-only evidence state for a single SAG run."""

from __future__ import annotations

import copy
import json
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, computed_field, field_serializer

from sag.evidence import EvidenceFinding, EvidenceStatus, OperationOutcome
from sag.tools.base import ToolResult, UnpersistedToolResult, new_execution_id

from .phase_machine import PhaseAttemptRecord


class StateScope(str, Enum):
    ENVIRONMENT = "environment"
    DEPENDENCIES = "dependencies"
    ARTIFACTS = "artifacts"
    TEST_RUNTIME = "test_runtime"
    PROJECT_ANALYSIS = "project_analysis"


class EvidenceRole(str, Enum):
    BUILD = "build"
    TEST = "test"


class FactStatus(str, Enum):
    VERIFIED = "verified"
    CLAIMED = "claimed"


class EvidenceFact(BaseModel):
    """One observed or claimed fact, kept even when it is a duplicate."""

    model_config = ConfigDict(extra="forbid")

    scope: StateScope
    key: str
    value: Any
    canonical_value: str
    status: FactStatus
    provenance: str


class BlockerRecord(BaseModel):
    """A blocker and its latest status; event snapshots preserve its history."""

    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    blocker_id: str
    category: str
    error_code: str
    failure_signature: str
    status: str = "active"
    event: str = "recorded"
    resolution: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)


class ActionAttempt(BaseModel):
    """An append-only action record with the relevant state vector at dispatch."""

    model_config = ConfigDict(extra="forbid")

    attempt_id: str
    action: str
    relevant_scopes: list[StateScope] = Field(default_factory=list)
    state_vector: dict[str, int] = Field(default_factory=dict)
    outcome: OperationOutcome = OperationOutcome.UNKNOWN
    evidence_refs: list[str] = Field(default_factory=list)


class PhaseEvidenceEvent(BaseModel):
    """Evidence refs observed while one concrete phase attempt was open."""

    model_config = ConfigDict(extra="forbid")

    attempt_id: str
    evidence_refs: tuple[str, ...] = ()


class RepairRecord(BaseModel):
    """Append-only repair proposal and the state vector used to decide it."""

    model_config = ConfigDict(extra="forbid")

    repair_id: str
    from_phase: str
    target_phase: str
    source_attempt_id: str
    reason_code: str
    failure_signature: str
    hypothesis: str
    evidence_refs: tuple[str, ...] = ()
    state_vector: dict[str, int] = Field(default_factory=dict)
    accepted: bool
    decision_reason: str = ""


class ToolObservation(BaseModel):
    """A preserved WS0 result observed by the engine."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    observation_id: str
    execution_id: str
    scope: StateScope
    tool_name: str
    params: dict[str, Any] = Field(default_factory=dict)
    roles: tuple[EvidenceRole, ...] = ()
    result: ToolResult | UnpersistedToolResult
    provenance: str


class StateEpochDelta(BaseModel):
    """The scoped progress effect of a state registration or tool ingestion."""

    model_config = ConfigDict(extra="forbid")

    scope: StateScope
    before: int
    after: int
    changed: bool
    fact: EvidenceFact | None = None


def _canonicalize(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _snapshot(record: Any) -> Any:
    """Return a detached copy suitable for the public read surface."""
    if isinstance(record, BaseModel):
        return record.model_copy(deep=True)
    return copy.deepcopy(record)


class RunEvidenceState(BaseModel):
    """Mutable run evidence owned and written exclusively by the engine."""

    model_config = ConfigDict(extra="forbid")

    run_id: str

    _state_epochs: dict[StateScope, int] = PrivateAttr(
        default_factory=lambda: {scope: 0 for scope in StateScope}
    )
    _facts: list[EvidenceFact] = PrivateAttr(default_factory=list)
    _blockers: list[BlockerRecord] = PrivateAttr(default_factory=list)
    _blocker_events: list[BlockerRecord] = PrivateAttr(default_factory=list)
    _action_attempts: list[ActionAttempt] = PrivateAttr(default_factory=list)
    _phase_evidence_events: list[PhaseEvidenceEvent] = PrivateAttr(default_factory=list)
    _phase_evidence_refs: dict[str, set[str]] = PrivateAttr(default_factory=dict)
    _repair_records: list[RepairRecord] = PrivateAttr(default_factory=list)
    _tool_observations: list[ToolObservation] = PrivateAttr(default_factory=list)
    _observation_execution_ids: set[str] = PrivateAttr(default_factory=set)
    _validator_findings: list[EvidenceFinding] = PrivateAttr(default_factory=list)
    _conflicts: list[str] = PrivateAttr(default_factory=list)
    _phase_records: list[PhaseAttemptRecord] = PrivateAttr(default_factory=list)
    _phase_record_ids: set[str] = PrivateAttr(default_factory=set)
    _finalized_at: str | None = PrivateAttr(default=None)
    _close_reason: str | None = PrivateAttr(default=None)
    _canonical_values: set[tuple[StateScope, str, str]] = PrivateAttr(default_factory=set)

    @computed_field
    @property
    def state_epochs(self) -> Mapping[StateScope, int]:
        return MappingProxyType(dict(self._state_epochs))

    @field_serializer("state_epochs")
    def _serialize_state_epochs(self, epochs: Mapping[StateScope, int]) -> dict[str, int]:
        return {scope.value: epochs[scope] for scope in StateScope}

    @computed_field
    @property
    def facts(self) -> tuple[EvidenceFact, ...]:
        return tuple(_snapshot(fact) for fact in self._facts)

    @computed_field
    @property
    def blockers(self) -> tuple[BlockerRecord, ...]:
        return tuple(_snapshot(blocker) for blocker in self._blockers)

    @computed_field
    @property
    def blocker_events(self) -> tuple[BlockerRecord, ...]:
        return tuple(_snapshot(event) for event in self._blocker_events)

    @computed_field
    @property
    def action_attempts(self) -> tuple[ActionAttempt, ...]:
        return tuple(_snapshot(attempt) for attempt in self._action_attempts)

    @computed_field
    @property
    def phase_evidence_events(self) -> tuple[PhaseEvidenceEvent, ...]:
        return tuple(_snapshot(event) for event in self._phase_evidence_events)

    @computed_field
    @property
    def repair_records(self) -> tuple[RepairRecord, ...]:
        return tuple(_snapshot(record) for record in self._repair_records)

    @computed_field
    @property
    def tool_observations(self) -> tuple[ToolObservation, ...]:
        return tuple(_snapshot(observation) for observation in self._tool_observations)

    @computed_field
    @property
    def validator_findings(self) -> tuple[EvidenceFinding, ...]:
        return tuple(_snapshot(finding) for finding in self._validator_findings)

    @computed_field
    @property
    def conflicts(self) -> tuple[str, ...]:
        return tuple(self._conflicts)

    @computed_field
    @property
    def phase_records(self) -> tuple[PhaseAttemptRecord, ...]:
        return tuple(_snapshot(record) for record in self._phase_records)

    @computed_field
    @property
    def sealed(self) -> bool:
        return self._finalized_at is not None

    @computed_field
    @property
    def finalized_at(self) -> str | None:
        return self._finalized_at

    @property
    def close_reason(self) -> str | None:
        """Evidence-close metadata; deliberately absent from snapshot serialization."""
        return self._close_reason

    def _require_mutable(self) -> None:
        if self._finalized_at is not None:
            raise RuntimeError("RunEvidenceState is sealed")

    def register_fact(
        self,
        scope: StateScope,
        key: str,
        value: Any,
        provenance: str,
    ) -> StateEpochDelta:
        """Append verified evidence and advance only for new canonical content."""
        self._require_mutable()
        scope = StateScope(scope)
        canonical_value = _canonicalize(value)
        fact = EvidenceFact(
            scope=scope,
            key=key,
            value=copy.deepcopy(value),
            canonical_value=canonical_value,
            status=FactStatus.VERIFIED,
            provenance=provenance,
        )
        before = self._state_epochs[scope]
        identity = (scope, key, canonical_value)
        changed = identity not in self._canonical_values
        if changed:
            self._canonical_values.add(identity)
            self._state_epochs[scope] = before + 1
        self._facts.append(fact)
        return StateEpochDelta(
            scope=scope,
            before=before,
            after=self._state_epochs[scope],
            changed=changed,
            fact=_snapshot(fact),
        )

    def register_claim(
        self,
        scope: StateScope,
        key: str,
        value: Any,
        provenance: str,
    ) -> StateEpochDelta:
        """Append an unverified claim without allowing it to create progress."""
        self._require_mutable()
        scope = StateScope(scope)
        fact = EvidenceFact(
            scope=scope,
            key=key,
            value=copy.deepcopy(value),
            canonical_value=_canonicalize(value),
            status=FactStatus.CLAIMED,
            provenance=provenance,
        )
        before = self._state_epochs[scope]
        self._facts.append(fact)
        return StateEpochDelta(
            scope=scope,
            before=before,
            after=before,
            changed=False,
            fact=_snapshot(fact),
        )

    def set_fact(
        self,
        key: str,
        value: Any,
        *,
        evidence_ref: str,
        scope: StateScope | None = None,
    ) -> StateEpochDelta:
        """Register an engine/validator fact through a stable dotted-key seam."""
        if not str(evidence_ref).strip():
            raise ValueError("validated facts require a nonblank evidence ref")
        if scope is None:
            prefix = str(key).partition(".")[0]
            scope = {
                "provision": StateScope.ENVIRONMENT,
                "environment": StateScope.ENVIRONMENT,
                "dependencies": StateScope.DEPENDENCIES,
                "build": StateScope.ARTIFACTS,
                "artifacts": StateScope.ARTIFACTS,
                "test": StateScope.TEST_RUNTIME,
                "analysis": StateScope.PROJECT_ANALYSIS,
                "project": StateScope.PROJECT_ANALYSIS,
            }.get(prefix, StateScope.PROJECT_ANALYSIS)
        return self.register_fact(StateScope(scope), str(key), value, str(evidence_ref))

    def fact_value(self, key: str, default: Any = None) -> Any:
        """Return the latest verified canonical fact with this key."""
        for fact in reversed(self._facts):
            if fact.key == key and fact.status is FactStatus.VERIFIED:
                return copy.deepcopy(fact.value)
        return copy.deepcopy(default)

    def fact_provenance(self, key: str) -> str | None:
        for fact in reversed(self._facts):
            if fact.key == key and fact.status is FactStatus.VERIFIED:
                return fact.provenance
        return None

    def record_blocker(
        self,
        *,
        category: str,
        error_code: str,
        failure_signature: str,
        evidence_refs: Iterable[str] = (),
    ) -> BlockerRecord:
        self._require_mutable()
        blocker = BlockerRecord(
            blocker_id=f"blocker_{len(self._blockers) + 1}",
            category=category,
            error_code=error_code,
            failure_signature=failure_signature,
            evidence_refs=list(evidence_refs),
        )
        self._blockers.append(blocker)
        self._blocker_events.append(_snapshot(blocker))
        return _snapshot(blocker)

    def resolve_blocker(
        self,
        blocker_id: str,
        *,
        resolution: str = "",
    ) -> BlockerRecord:
        self._require_mutable()
        blocker = next((item for item in self._blockers if item.blocker_id == blocker_id), None)
        if blocker is None:
            raise KeyError(f"Unknown blocker: {blocker_id}")
        if blocker.status == "resolved":
            raise ValueError(f"Blocker already resolved: {blocker_id}")
        blocker.status = "resolved"
        blocker.resolution = resolution or None
        resolution_event = _snapshot(blocker)
        resolution_event.event = "resolved"
        self._blocker_events.append(resolution_event)
        return _snapshot(resolution_event)

    def record_attempt(
        self,
        *,
        action: str,
        relevant_scopes: Iterable[StateScope] = (),
        outcome: OperationOutcome = OperationOutcome.UNKNOWN,
        evidence_refs: Iterable[str] = (),
    ) -> ActionAttempt:
        self._require_mutable()
        scopes = [StateScope(scope) for scope in relevant_scopes]
        attempt = ActionAttempt(
            attempt_id=f"attempt_{len(self._action_attempts) + 1}",
            action=action,
            relevant_scopes=scopes,
            state_vector=self.state_vector(scopes),
            outcome=outcome,
            evidence_refs=list(evidence_refs),
        )
        self._action_attempts.append(attempt)
        return _snapshot(attempt)

    def record_phase_evidence(
        self,
        attempt_id: str,
        evidence_refs: Iterable[str],
    ) -> PhaseEvidenceEvent:
        """Append refs produced while an attempt is open; never reassign old refs."""
        self._require_mutable()
        normalized_attempt = str(attempt_id).strip()
        if not normalized_attempt:
            raise ValueError("phase evidence event requires an attempt id")
        normalized = tuple(
            dict.fromkeys(
                str(ref).strip()
                for ref in evidence_refs
                if ref is not None and str(ref).strip()
            )
        )
        if not normalized:
            raise ValueError("phase evidence event requires at least one evidence ref")
        known = self._phase_evidence_refs.setdefault(normalized_attempt, set())
        fresh = tuple(ref for ref in normalized if ref not in known)
        known.update(normalized)
        event = PhaseEvidenceEvent(
            attempt_id=normalized_attempt,
            evidence_refs=fresh or normalized,
        )
        self._phase_evidence_events.append(event)
        return _snapshot(event)

    def evidence_refs_for_attempt(self, attempt_id: str) -> tuple[str, ...]:
        return tuple(sorted(self._phase_evidence_refs.get(str(attempt_id), set())))

    def record_repair(
        self,
        request: Any,
        *,
        state_vector: Mapping[str, int],
        accepted: bool = True,
        decision_reason: str = "",
    ) -> RepairRecord:
        self._require_mutable()
        record = RepairRecord(
            repair_id=f"repair_{len(self._repair_records) + 1}",
            from_phase=str(request.from_phase),
            target_phase=str(request.target_phase),
            source_attempt_id=str(request.source_attempt_id),
            reason_code=str(request.reason_code),
            failure_signature=str(request.failure_signature),
            hypothesis=str(request.hypothesis),
            evidence_refs=tuple(request.evidence_refs),
            state_vector={str(key): int(value) for key, value in state_vector.items()},
            accepted=bool(accepted),
            decision_reason=str(decision_reason),
        )
        self._repair_records.append(record)
        return _snapshot(record)

    def record_conflict(self, conflict: str) -> None:
        """Record an engine-observed conflict without fabricating tool evidence."""
        self._require_mutable()
        normalized = str(conflict).strip()
        if not normalized:
            raise ValueError("conflict must be nonblank")
        if normalized not in self._conflicts:
            self._conflicts.append(normalized)

    def has_execution_id(self, execution_id: str) -> bool:
        return execution_id in self._observation_execution_ids

    def ingest_tool_result(
        self,
        scope: StateScope,
        tool_name: str,
        result: ToolResult | UnpersistedToolResult,
        provenance: str | None = None,
        *,
        roles: Iterable[EvidenceRole] = (),
        execution_id: str | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> StateEpochDelta:
        """Record a WS0 result and promote only its verified facts into epochs."""
        self._require_mutable()
        scope = StateScope(scope)
        execution_id = execution_id or new_execution_id()
        normalized_roles = tuple(dict.fromkeys(EvidenceRole(role) for role in roles))
        normalized_params = copy.deepcopy(dict(params or {}))
        existing = next(
            (
                observation
                for observation in self._tool_observations
                if observation.execution_id == execution_id
            ),
            None,
        )
        if existing is not None:
            if (
                existing.scope is not scope
                or existing.tool_name != tool_name
                or existing.params != normalized_params
                or existing.roles != normalized_roles
                or existing.result != result
            ):
                raise ValueError(f"conflicting observation for execution_id {execution_id}")
            before = self._state_epochs[scope]
            return StateEpochDelta(
                scope=scope,
                before=before,
                after=before,
                changed=False,
            )
        source = (
            provenance
            or result.output_ref
            or next(
                iter(result.evidence_refs or result.refs),
                f"tool:{tool_name}:{len(self._tool_observations) + 1}",
            )
        )
        observation = ToolObservation(
            observation_id=f"observation_{len(self._tool_observations) + 1}",
            execution_id=execution_id,
            scope=scope,
            tool_name=tool_name,
            params=normalized_params,
            roles=normalized_roles,
            result=result.model_copy(deep=True),
            provenance=source,
        )
        self._tool_observations.append(observation)
        self._observation_execution_ids.add(execution_id)
        self._validator_findings.extend(_snapshot(finding) for finding in result.validator_findings)
        self._conflicts.extend(result.conflicts)

        before = self._state_epochs[scope]
        latest_fact: EvidenceFact | None = None
        changed = False
        if result.evidence_status is EvidenceStatus.VERIFIED:
            for key, value in result.facts.items():
                delta = self.register_fact(scope, key, value, source)
                latest_fact = delta.fact
                changed = changed or delta.changed
        return StateEpochDelta(
            scope=scope,
            before=before,
            after=self._state_epochs[scope],
            changed=changed,
            fact=latest_fact,
        )

    def ingest_unpersisted_result(
        self,
        scope: StateScope,
        tool_name: str,
        result: UnpersistedToolResult,
        provenance: str,
        *,
        roles: Iterable[EvidenceRole] = (),
        execution_id: str | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> StateEpochDelta:
        """Record bounded evidence from a result-construction persistence failure."""
        if not isinstance(result, UnpersistedToolResult):
            raise TypeError("unpersisted evidence requires UnpersistedToolResult")
        return self.ingest_tool_result(
            scope,
            tool_name,
            result,
            provenance,
            roles=roles,
            execution_id=execution_id or result.execution_id,
            params=params,
        )

    def state_vector(self, scopes: Iterable[StateScope]) -> dict[str, int]:
        """Return selected epoch counters in the declaration order of StateScope."""
        selected = {StateScope(scope) for scope in scopes}
        return {scope.value: self._state_epochs[scope] for scope in StateScope if scope in selected}

    def record_phase_record(self, record: PhaseAttemptRecord) -> PhaseAttemptRecord:
        """Append one detached phase record without using it as verdict evidence."""
        self._require_mutable()
        if not isinstance(record, PhaseAttemptRecord):
            raise TypeError("phase records must be PhaseAttemptRecord instances")
        if record.attempt_id in self._phase_record_ids:
            existing = next(
                item for item in self._phase_records if item.attempt_id == record.attempt_id
            )
            if existing != record:
                raise ValueError(f"conflicting phase record for {record.attempt_id}")
            return _snapshot(existing)
        detached = _snapshot(record)
        self._phase_records.append(detached)
        self._phase_record_ids.add(detached.attempt_id)
        return _snapshot(detached)

    def record_phase_attempt(self, record: PhaseAttemptRecord) -> PhaseAttemptRecord:
        """Named WS3 seam; phase attempts and skips share append-only storage."""
        return self.record_phase_record(record)

    def seal(self, *, finalized_at: str, close_reason: str | None = None) -> None:
        """Close evidence collection permanently at the evidence-close boundary."""
        self._require_mutable()
        if not finalized_at.strip():
            raise ValueError("finalized_at must be nonblank")
        self._finalized_at = finalized_at
        self._close_reason = close_reason
