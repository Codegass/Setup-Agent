"""Engine-owned, append-only evidence state for a single SAG run."""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Iterable

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


class RunEvidenceState(BaseModel):
    """Mutable run evidence owned and written exclusively by the engine."""

    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    run_id: str
    state_epochs: dict[StateScope, int] = Field(
        default_factory=lambda: {scope: 0 for scope in StateScope}
    )
    facts: list[EvidenceFact] = Field(default_factory=list)
    blockers: list[BlockerRecord] = Field(default_factory=list)
    blocker_events: list[BlockerRecord] = Field(default_factory=list)
    action_attempts: list[ActionAttempt] = Field(default_factory=list)
    tool_observations: list[ToolObservation] = Field(default_factory=list)
    validator_findings: list[EvidenceFinding] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    finalized_at: str | None = None

    _canonical_values: set[tuple[StateScope, str, str]] = PrivateAttr(default_factory=set)

    def model_post_init(self, __context: Any) -> None:
        for scope in StateScope:
            self.state_epochs.setdefault(scope, 0)
        self._canonical_values = {
            (fact.scope, fact.key, fact.canonical_value)
            for fact in self.facts
            if fact.status is FactStatus.VERIFIED
        }

    def _require_mutable(self) -> None:
        if self.finalized_at is not None:
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
            value=value,
            canonical_value=canonical_value,
            status=FactStatus.VERIFIED,
            provenance=provenance,
        )
        before = self.state_epochs[scope]
        identity = (scope, key, canonical_value)
        changed = identity not in self._canonical_values
        if changed:
            self._canonical_values.add(identity)
            self.state_epochs[scope] = before + 1
        self.facts.append(fact)
        return StateEpochDelta(
            scope=scope,
            before=before,
            after=self.state_epochs[scope],
            changed=changed,
            fact=fact,
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
            value=value,
            canonical_value=_canonicalize(value),
            status=FactStatus.CLAIMED,
            provenance=provenance,
        )
        before = self.state_epochs[scope]
        self.facts.append(fact)
        return StateEpochDelta(
            scope=scope,
            before=before,
            after=before,
            changed=False,
            fact=fact,
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
            blocker_id=f"blocker_{len(self.blockers) + 1}",
            category=category,
            error_code=error_code,
            failure_signature=failure_signature,
            evidence_refs=list(evidence_refs),
        )
        self.blockers.append(blocker)
        self.blocker_events.append(blocker.model_copy(deep=True))
        return blocker

    def resolve_blocker(
        self,
        blocker_id: str,
        *,
        resolution: str = "",
    ) -> BlockerRecord:
        self._require_mutable()
        blocker = next((item for item in self.blockers if item.blocker_id == blocker_id), None)
        if blocker is None:
            raise KeyError(f"Unknown blocker: {blocker_id}")
        if blocker.status == "resolved":
            raise ValueError(f"Blocker already resolved: {blocker_id}")
        blocker.status = "resolved"
        blocker.resolution = resolution or None
        resolution_event = blocker.model_copy(deep=True)
        resolution_event.event = "resolved"
        self.blocker_events.append(resolution_event)
        return resolution_event

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
            attempt_id=f"attempt_{len(self.action_attempts) + 1}",
            action=action,
            relevant_scopes=scopes,
            state_vector=self.state_vector(scopes),
            outcome=outcome,
            evidence_refs=list(evidence_refs),
        )
        self.action_attempts.append(attempt)
        return attempt

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
                f"tool:{tool_name}:{len(self.tool_observations) + 1}",
            )
        )
        observation = ToolObservation(
            observation_id=f"observation_{len(self.tool_observations) + 1}",
            scope=scope,
            tool_name=tool_name,
            result=result.model_copy(deep=True),
            provenance=source,
        )
        self.tool_observations.append(observation)
        self.validator_findings.extend(
            finding.model_copy(deep=True) for finding in result.validator_findings
        )
        self.conflicts.extend(result.conflicts)

        before = self.state_epochs[scope]
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
            after=self.state_epochs[scope],
            changed=changed,
            fact=latest_fact,
        )

    def state_vector(self, scopes: Iterable[StateScope]) -> dict[str, int]:
        """Return selected epoch counters in the declaration order of StateScope."""
        selected = {StateScope(scope) for scope in scopes}
        return {scope.value: self.state_epochs[scope] for scope in StateScope if scope in selected}

    def seal(self, *, finalized_at: str) -> None:
        """Close evidence collection permanently at the evidence-close boundary."""
        self._require_mutable()
        if not finalized_at.strip():
            raise ValueError("finalized_at must be nonblank")
        self.finalized_at = finalized_at
