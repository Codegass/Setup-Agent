"""Engine-owned, append-only evidence state for a single SAG run."""

from __future__ import annotations

import copy
import json
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from sag.evidence import EvidenceFinding, EvidenceStatus, OperationOutcome
from sag.tools.base import ToolResult


class StateScope(str, Enum):
    ENVIRONMENT = "environment"
    DEPENDENCIES = "dependencies"
    ARTIFACTS = "artifacts"
    TEST_RUNTIME = "test_runtime"
    PROJECT_ANALYSIS = "project_analysis"


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


class ToolObservation(BaseModel):
    """A preserved WS0 result observed by the engine."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    observation_id: str
    scope: StateScope
    tool_name: str
    result: ToolResult
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
    _tool_observations: list[ToolObservation] = PrivateAttr(default_factory=list)
    _validator_findings: list[EvidenceFinding] = PrivateAttr(default_factory=list)
    _conflicts: list[str] = PrivateAttr(default_factory=list)
    _finalized_at: str | None = PrivateAttr(default=None)
    _canonical_values: set[tuple[StateScope, str, str]] = PrivateAttr(default_factory=set)

    @property
    def state_epochs(self) -> Mapping[StateScope, int]:
        return MappingProxyType(dict(self._state_epochs))

    @property
    def facts(self) -> tuple[EvidenceFact, ...]:
        return tuple(_snapshot(fact) for fact in self._facts)

    @property
    def blockers(self) -> tuple[BlockerRecord, ...]:
        return tuple(_snapshot(blocker) for blocker in self._blockers)

    @property
    def blocker_events(self) -> tuple[BlockerRecord, ...]:
        return tuple(_snapshot(event) for event in self._blocker_events)

    @property
    def action_attempts(self) -> tuple[ActionAttempt, ...]:
        return tuple(_snapshot(attempt) for attempt in self._action_attempts)

    @property
    def tool_observations(self) -> tuple[ToolObservation, ...]:
        return tuple(_snapshot(observation) for observation in self._tool_observations)

    @property
    def validator_findings(self) -> tuple[EvidenceFinding, ...]:
        return tuple(_snapshot(finding) for finding in self._validator_findings)

    @property
    def conflicts(self) -> tuple[str, ...]:
        return tuple(self._conflicts)

    @property
    def finalized_at(self) -> str | None:
        return self._finalized_at

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

    def ingest_tool_result(
        self,
        scope: StateScope,
        tool_name: str,
        result: ToolResult,
        provenance: str | None = None,
    ) -> StateEpochDelta:
        """Record a WS0 result and promote only its verified facts into epochs."""
        self._require_mutable()
        scope = StateScope(scope)
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
            scope=scope,
            tool_name=tool_name,
            result=result.model_copy(deep=True),
            provenance=source,
        )
        self._tool_observations.append(observation)
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

    def state_vector(self, scopes: Iterable[StateScope]) -> dict[str, int]:
        """Return selected epoch counters in the declaration order of StateScope."""
        selected = {StateScope(scope) for scope in scopes}
        return {scope.value: self._state_epochs[scope] for scope in StateScope if scope in selected}

    def seal(self, *, finalized_at: str) -> None:
        """Close evidence collection permanently at the evidence-close boundary."""
        self._require_mutable()
        if not finalized_at.strip():
            raise ValueError("finalized_at must be nonblank")
        self._finalized_at = finalized_at
