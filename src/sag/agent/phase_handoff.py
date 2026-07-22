"""Cumulative, provenance-preserving phase handoff projections.

``RunEvidenceState`` remains the only mutable truth store.  This module reads
that state, materializes its complete handoff atomically, and renders a bounded
typed projection for phase-start prompts without deleting canonical history.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .attempt_ledger import failure_preview
from .evidence_state import RunEvidenceState
from .output_storage import atomic_write_container_text

PHASE_HANDOFF_PATH = "/workspace/.setup_agent/phase-handoff.json"

_INLINE_CAPS = {
    "facts": 20,
    "blockers": 12,
    "attempts": 12,
    "failures": 8,
    "repairs": 8,
}

_TARGET_FACT_PRIORITIES = {
    "provision": ("provision.", "environment.", "java."),
    "analyze": ("provision.workspace_ready", "analysis.", "project.", "java."),
    "build": (
        "analysis.build_entry_ready",
        "java.required_version",
        "project.",
        "dependencies.",
        "build.",
        "artifacts.",
    ),
    "test": (
        "build.test_entry_ready",
        "java.required_version",
        "native.",
        "build.",
        "artifacts.",
        "test.",
    ),
    "report": ("build.", "test.", "artifacts.", "native.", "java."),
}


def _dedupe(values) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(value).strip()
            for value in values
            if value is not None and str(value).strip()
        )
    )


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _short_json(value: Any, limit: int = 240) -> str:
    rendered = json.dumps(value, sort_keys=True, ensure_ascii=True, default=str)
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 1] + "…"


def _phase_from_attempt(attempt_id: str | None) -> str | None:
    if not attempt_id:
        return None
    phase, separator, suffix = str(attempt_id).rpartition("-")
    return phase if separator and suffix.isdigit() else None


class HandoffFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    value: Any
    status: Literal["verified", "claimed"]
    scope: str
    source_phase: str | None = None
    source_attempt_id: str | None = None
    source_ref: str
    evidence_ref: str
    last_updated_epoch: int


class HandoffBlocker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blocker_id: str
    category: str
    status: Literal["active", "resolved"]
    error_code: str
    failure_signature: str
    evidence_refs: tuple[str, ...] = ()
    remediation: str | None = None
    frequency: int = 1
    first_attempt_id: str | None = None
    last_attempt_id: str | None = None
    first_seen_epoch: int
    last_updated_epoch: int

    @property
    def signature(self) -> str:
        return self.failure_signature


class HandoffAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: str
    source_kind: Literal["phase", "action"]
    phase: str | None = None
    action: str | None = None
    termination: str | None = None
    outcome: str
    state_vector: dict[str, int] = Field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    last_updated_epoch: int


class HandoffFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str
    error_code: str
    failure_signature: str
    error_tail_preview: str
    output_ref: str
    occurrence_count: int = 1
    source_phase: str | None = None
    source_attempt_id: str | None = None
    last_updated_epoch: int

    @property
    def preview(self) -> str:
        return self.error_tail_preview


class HandoffRepairRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repair_id: str
    source_attempt_id: str
    from_phase: str
    target_phase: str
    accepted: bool
    reason_code: str
    failure_signature: str
    hypothesis: str
    evidence_refs: tuple[str, ...] = ()
    state_vector: dict[str, int] = Field(default_factory=dict)
    last_updated_epoch: int


class HandoffProjection(BaseModel):
    """A detached prompt or persistence projection of canonical run state."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    target_phase: str
    facts: tuple[HandoffFact, ...] = ()
    blockers: tuple[HandoffBlocker, ...] = ()
    attempts: tuple[HandoffAttempt, ...] = ()
    repair_routes: tuple[HandoffRepairRoute, ...] = ()
    last_failures: tuple[HandoffFailure, ...] = ()
    next_hypothesis: str | None = None
    omitted_fact_count: int = 0
    omitted_blocker_count: int = 0
    omitted_attempt_count: int = 0
    omitted_failure_count: int = 0
    omitted_repair_count: int = 0
    full_state_ref: str

    def fact(self, key: str) -> HandoffFact | None:
        return next((fact for fact in self.facts if fact.key == key), None)

    def blocker(self, identity: str) -> HandoffBlocker | None:
        return next(
            (
                blocker
                for blocker in self.blockers
                if blocker.blocker_id == identity or blocker.failure_signature == identity
            ),
            None,
        )

    @property
    def has_omissions(self) -> bool:
        return any(
            (
                self.omitted_fact_count,
                self.omitted_blocker_count,
                self.omitted_attempt_count,
                self.omitted_failure_count,
                self.omitted_repair_count,
            )
        )

    def to_prompt_text(self) -> str:
        lines = [
            "=== CUMULATIVE PHASE HANDOFF ===",
            f"Target phase: {self.target_phase}",
            "[BEGIN UNTRUSTED TOOL/PROJECT EVIDENCE]",
        ]
        if self.facts:
            lines.append("FACTS:")
            for fact in self.facts:
                source = fact.evidence_ref or "unverified-claim"
                phase = f" phase={fact.source_phase}" if fact.source_phase else ""
                lines.append(
                    f"- {fact.key} [{fact.status}]={_short_json(fact.value)}"
                    f"{phase} ref={source}"
                )

        active_blockers = [blocker for blocker in self.blockers if blocker.status == "active"]
        if active_blockers:
            lines.append("ACTIVE BLOCKERS:")
            for blocker in active_blockers:
                refs = ",".join(blocker.evidence_refs) or "none"
                lines.append(
                    f"- {blocker.failure_signature} code={blocker.error_code} "
                    f"frequency={blocker.frequency} refs={refs}"
                )

        if self.last_failures:
            lines.append("LAST RELEVANT FAILURES:")
            for failure in self.last_failures:
                preview = failure.error_tail_preview.replace("\n", " ⏎ ")
                lines.append(
                    f"- command={_short_json(failure.command, 120)} "
                    f"code={failure.error_code} signature={failure.failure_signature} "
                    f"tail={_short_json(preview, 420)} ref={failure.output_ref}"
                )

        if self.attempts:
            lines.append("ATTEMPTS:")
            for attempt in self.attempts:
                identity = attempt.phase or attempt.action or attempt.attempt_id
                refs = ",".join(attempt.evidence_refs) or "none"
                lines.append(
                    f"- {attempt.attempt_id} {identity} "
                    f"outcome={attempt.outcome} refs={refs}"
                )

        if self.repair_routes:
            lines.append("REPAIR ROUTES:")
            for repair in self.repair_routes:
                decision = "accepted" if repair.accepted else "rejected"
                lines.append(
                    f"- {repair.source_attempt_id}: {repair.from_phase}->{repair.target_phase} "
                    f"{decision} signature={repair.failure_signature}"
                )

        if self.next_hypothesis:
            lines.append(f"NEXT HYPOTHESIS: {_short_json(self.next_hypothesis, 300)}")
        lines.append("[END UNTRUSTED TOOL/PROJECT EVIDENCE]")
        if self.has_omissions:
            lines.append(
                "omitted: "
                f"facts={self.omitted_fact_count}, "
                f"blockers={self.omitted_blocker_count}, "
                f"attempts={self.omitted_attempt_count}, "
                f"failures={self.omitted_failure_count}, "
                f"repairs={self.omitted_repair_count}; "
                f"full handoff: {self.full_state_ref}"
            )
        return "\n".join(lines)


class PhaseHandoff:
    """Read-only cumulative view over one ``RunEvidenceState``."""

    def __init__(
        self,
        state: RunEvidenceState,
        *,
        storage_path: str | Path | None = None,
        orchestrator=None,
    ) -> None:
        if not isinstance(state, RunEvidenceState):
            raise TypeError("PhaseHandoff requires RunEvidenceState")
        self._state = state
        self.orchestrator = orchestrator
        self.storage_path = Path(storage_path or PHASE_HANDOFF_PATH)
        self.full_state_ref = str(self.storage_path)
        self._persistence_enabled = orchestrator is not None or storage_path is not None
        self._materializing = False
        state.add_change_listener(self._on_state_change)
        if self._persistence_enabled:
            self.materialize()

    def _attempt_for_ref(self, evidence_ref: str) -> str | None:
        for event in reversed(self._state.phase_evidence_events):
            if evidence_ref in event.evidence_refs:
                return event.attempt_id
        return None

    def _facts(self, target_phase: str) -> list[HandoffFact]:
        projected = []
        for epoch, fact in enumerate(self._state.facts, 1):
            evidence_ref = str(fact.provenance or "")
            attempt_id = getattr(fact, "source_attempt_id", None) or self._attempt_for_ref(
                evidence_ref
            )
            projected.append(
                HandoffFact(
                    key=fact.key,
                    value=fact.value,
                    status=_enum_value(fact.status),
                    scope=_enum_value(fact.scope),
                    source_phase=getattr(fact, "source_phase", None)
                    or _phase_from_attempt(attempt_id),
                    source_attempt_id=attempt_id,
                    source_ref=evidence_ref,
                    evidence_ref=evidence_ref,
                    last_updated_epoch=epoch,
                )
            )

        priorities = _TARGET_FACT_PRIORITIES.get(target_phase, ())

        def priority(fact: HandoffFact) -> tuple[int, int, str]:
            target_rank = len(priorities) + 1
            for index, required in enumerate(priorities):
                if fact.key == required or fact.key.startswith(required):
                    target_rank = index
                    break
            return target_rank, -fact.last_updated_epoch, fact.key

        return sorted(projected, key=priority)

    def _blockers(self, facts: list[HandoffFact]) -> list[HandoffBlocker]:
        grouped: dict[str, list[tuple[int, Any]]] = {}
        for epoch, blocker in enumerate(self._state.blockers, 1):
            signature = blocker.failure_signature or blocker.blocker_id
            grouped.setdefault(signature, []).append((epoch, blocker))

        projected = []
        for signature, occurrences in grouped.items():
            first_epoch, first = occurrences[0]
            last_epoch, last = occurrences[-1]
            active = [item for _, item in occurrences if item.status == "active"]
            status = "active" if active else "resolved"
            representative = active[-1] if active else last
            refs = _dedupe(
                ref for _, item in occurrences for ref in item.evidence_refs
            )
            attempt_ids = []
            for _, item in occurrences:
                attempt_id = getattr(item, "source_attempt_id", None)
                if attempt_id is None:
                    attempt_id = next(
                        (
                            matched
                            for ref in item.evidence_refs
                            if (matched := self._attempt_for_ref(ref)) is not None
                        ),
                        None,
                    )
                attempt_ids.append(attempt_id)
            attempt_ids = [attempt for attempt in attempt_ids if attempt]
            projected.append(
                HandoffBlocker(
                    blocker_id=first.blocker_id,
                    category=representative.category,
                    status=status,
                    error_code=representative.error_code,
                    failure_signature=signature,
                    evidence_refs=refs,
                    remediation=last.resolution,
                    frequency=len(occurrences),
                    first_attempt_id=attempt_ids[0] if attempt_ids else None,
                    last_attempt_id=attempt_ids[-1] if attempt_ids else None,
                    first_seen_epoch=first_epoch,
                    last_updated_epoch=last_epoch,
                )
            )

        known_signatures = {blocker.failure_signature for blocker in projected}
        next_epoch = len(self._state.blockers)
        claimed_by_key: dict[str, list[HandoffFact]] = {}
        for fact in facts:
            if fact.status == "claimed":
                claimed_by_key.setdefault(fact.key, []).append(fact)
        for key, claims in claimed_by_key.items():
            signature = f"unverified_claim:{key}"
            if signature in known_signatures:
                continue
            fact = max(claims, key=lambda claim: claim.last_updated_epoch)
            next_epoch += 1
            refs = (fact.evidence_ref,) if fact.evidence_ref and not fact.evidence_ref.startswith(
                "claim://"
            ) else ()
            projected.append(
                HandoffBlocker(
                    blocker_id=signature,
                    category="unverified_claim",
                    status="active",
                    error_code="UNVERIFIED_CLAIM",
                    failure_signature=signature,
                    evidence_refs=refs,
                    frequency=len(claims),
                    first_attempt_id=fact.source_attempt_id,
                    last_attempt_id=fact.source_attempt_id,
                    first_seen_epoch=next_epoch,
                    last_updated_epoch=next_epoch,
                )
            )
            known_signatures.add(signature)

        return sorted(
            projected,
            key=lambda blocker: (
                0 if blocker.status == "active" else 1,
                -blocker.last_updated_epoch,
                -blocker.frequency,
                blocker.failure_signature,
            ),
        )

    def _attempts(self) -> list[HandoffAttempt]:
        projected = []
        epoch = 0
        for record in self._state.phase_records:
            epoch += 1
            projected.append(
                HandoffAttempt(
                    attempt_id=record.attempt_id,
                    source_kind="phase",
                    phase=record.phase,
                    termination=_enum_value(record.termination),
                    outcome=_enum_value(record.outcome),
                    evidence_refs=_dedupe(record.evidence_refs),
                    last_updated_epoch=epoch,
                )
            )
        for attempt in self._state.action_attempts:
            epoch += 1
            projected.append(
                HandoffAttempt(
                    attempt_id=attempt.attempt_id,
                    source_kind="action",
                    action=attempt.action,
                    outcome=_enum_value(attempt.outcome),
                    state_vector=dict(attempt.state_vector),
                    evidence_refs=_dedupe(attempt.evidence_refs),
                    last_updated_epoch=epoch,
                )
            )
        return sorted(
            projected,
            key=lambda attempt: (-attempt.last_updated_epoch, attempt.attempt_id),
        )

    def _failures(self) -> list[HandoffFailure]:
        grouped: dict[tuple[str, str, str], HandoffFailure] = {}
        for epoch, observation in enumerate(self._state.tool_observations, 1):
            result = observation.result
            outcome = _enum_value(result.operation_outcome)
            if outcome not in {"failed", "partial", "unknown"}:
                continue
            error_code = str(result.error_code or "")
            signature = str(result.failure_signature or "")
            if not (error_code or signature or result.error or result.error_tail_preview):
                continue
            params = observation.params or {}
            command = str(
                params.get("command")
                or params.get("action")
                or f"{observation.tool_name}:{json.dumps(params, sort_keys=True, default=str)}"
            )
            source = result.raw_output or result.output or result.error or ""
            preview = failure_preview(
                source,
                explicit_tail=result.error_tail_preview or "",
            )
            output_ref = str(result.output_ref or observation.provenance or "")
            attempt_id = self._attempt_for_ref(output_ref) or self._attempt_for_ref(
                observation.provenance
            )
            identity = (command, error_code, signature)
            prior = grouped.get(identity)
            grouped[identity] = HandoffFailure(
                command=command,
                error_code=error_code,
                failure_signature=signature,
                error_tail_preview=preview,
                output_ref=output_ref,
                occurrence_count=(prior.occurrence_count + 1) if prior else 1,
                source_phase=_phase_from_attempt(attempt_id),
                source_attempt_id=attempt_id,
                last_updated_epoch=epoch,
            )
        return sorted(
            grouped.values(),
            key=lambda failure: (-failure.last_updated_epoch, failure.failure_signature),
        )

    def _repairs(self) -> list[HandoffRepairRoute]:
        repairs = [
            HandoffRepairRoute(
                repair_id=record.repair_id,
                source_attempt_id=record.source_attempt_id,
                from_phase=record.from_phase,
                target_phase=record.target_phase,
                accepted=record.accepted,
                reason_code=record.reason_code,
                failure_signature=record.failure_signature,
                hypothesis=record.hypothesis,
                evidence_refs=_dedupe(record.evidence_refs),
                state_vector=dict(record.state_vector),
                last_updated_epoch=epoch,
            )
            for epoch, record in enumerate(self._state.repair_records, 1)
        ]
        return sorted(repairs, key=lambda repair: -repair.last_updated_epoch)

    def _complete_projection(self, target_phase: str) -> HandoffProjection:
        facts = self._facts(target_phase)
        blockers = self._blockers(facts)
        repairs = self._repairs()
        next_hypothesis = next(
            (repair.hypothesis for repair in repairs if repair.accepted and repair.evidence_refs),
            None,
        )
        return HandoffProjection(
            run_id=self._state.run_id,
            target_phase=target_phase,
            facts=tuple(facts),
            blockers=tuple(blockers),
            attempts=tuple(self._attempts()),
            repair_routes=tuple(repairs),
            last_failures=tuple(self._failures()),
            next_hypothesis=next_hypothesis,
            full_state_ref=self.full_state_ref,
        )

    @staticmethod
    def _with_omission_counts(
        projection: HandoffProjection,
        *,
        totals: dict[str, int],
    ) -> HandoffProjection:
        return projection.model_copy(
            update={
                "omitted_fact_count": totals["facts"] - len(projection.facts),
                "omitted_blocker_count": totals["blockers"] - len(projection.blockers),
                "omitted_attempt_count": totals["attempts"] - len(projection.attempts),
                "omitted_failure_count": totals["failures"]
                - len(projection.last_failures),
                "omitted_repair_count": totals["repairs"] - len(projection.repair_routes),
            }
        )

    def project_for(self, target_phase: str, *, char_budget: int) -> HandoffProjection:
        if char_budget <= 0:
            raise ValueError("phase handoff character budget must be positive")
        complete = self._complete_projection(str(target_phase))
        active_blockers = tuple(
            blocker for blocker in complete.blockers if blocker.status == "active"
        )
        totals = {
            "facts": len(complete.facts),
            "blockers": len(active_blockers),
            "attempts": len(complete.attempts),
            "failures": len(complete.last_failures),
            "repairs": len(complete.repair_routes),
        }
        selected = HandoffProjection(
            run_id=complete.run_id,
            target_phase=complete.target_phase,
            next_hypothesis=complete.next_hypothesis,
            full_state_ref=complete.full_state_ref,
        )
        selected = self._with_omission_counts(selected, totals=totals)

        categories = (
            ("blockers", active_blockers, "blockers"),
            ("last_failures", complete.last_failures, "failures"),
            ("facts", complete.facts, "facts"),
            ("attempts", complete.attempts, "attempts"),
            ("repair_routes", complete.repair_routes, "repairs"),
        )
        budget_exhausted = False
        for field_name, entries, cap_name in categories:
            for entry in entries[: _INLINE_CAPS[cap_name]]:
                current = tuple(getattr(selected, field_name))
                candidate = selected.model_copy(update={field_name: (*current, entry)})
                candidate = self._with_omission_counts(candidate, totals=totals)
                if len(candidate.to_prompt_text()) <= char_budget:
                    selected = candidate
                    continue
                budget_exhausted = True
                break
            if budget_exhausted:
                break
        return selected

    def materialize(self) -> HandoffProjection:
        """Atomically write the complete, untrimmed canonical projection."""

        projection = self._complete_projection("all")
        if not self._persistence_enabled or self._materializing:
            return projection
        self._materializing = True
        try:
            payload = projection.model_dump_json(indent=2)
            if self.orchestrator is not None:
                parent = str(self.storage_path.parent)
                result = self.orchestrator.execute_command(f"mkdir -p {parent}")
                if not (result.get("exit_code") == 0 or result.get("success")):
                    raise OSError("failed to create phase handoff directory")
                atomic_write_container_text(
                    self.orchestrator,
                    str(self.storage_path),
                    payload,
                )
            else:
                self.storage_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.storage_path.with_name(self.storage_path.name + ".tmp")
                temporary.write_text(payload, encoding="utf-8")
                temporary.replace(self.storage_path)
        finally:
            self._materializing = False
        return projection

    def _on_state_change(self, state: RunEvidenceState, _event: str) -> None:
        if state is not self._state:
            raise RuntimeError("phase handoff listener received a different run state")
        self.materialize()
