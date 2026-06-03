"""Build final CLI diagnosis from typed run state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sag.ui.state import UIEvidenceRecord, UIRunState, UITimelineEntry


@dataclass(frozen=True, slots=True)
class FinalDiagnosis:
    status: str
    outcome: str
    completed_phases: tuple[str, ...] = ()
    failed_phases: tuple[str, ...] = ()
    skipped_phases: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    recovery: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    failure_classifications: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()


def build_final_diagnosis(state: UIRunState) -> FinalDiagnosis:
    status = state.final_status or ("success" if state.is_complete else "unknown")
    completed_phases = _phase_names_by_status(state, "success")
    failed_phases = _phase_names_by_status(state, "error")
    skipped_phases = _phase_names_by_status(state, "skipped")

    failures = tuple(
        _format_timeline_entry(entry)
        for entry in state.timeline
        if entry.kind == "error" or entry.level == "error"
    )
    warnings = tuple(
        _format_timeline_entry(entry)
        for entry in state.timeline
        if entry.kind == "warning" or entry.level == "warning"
    )
    recovery = tuple(
        _format_recovery_entry(entry) for entry in state.timeline if entry.kind == "recovery"
    )
    if not recovery:
        recovery = warnings

    evidence = tuple(_format_evidence_record(item) for item in state.evidence)
    failure_classifications = _failure_classifications(state.timeline)
    next_actions = (
        ()
        if status == "success"
        else ("Review the latest failure and rerun the failed phase after addressing it.",)
    )

    return FinalDiagnosis(
        status=status,
        outcome=_build_outcome(status, completed_phases, failed_phases, skipped_phases),
        completed_phases=completed_phases,
        failed_phases=failed_phases,
        skipped_phases=skipped_phases,
        failures=failures,
        warnings=warnings,
        recovery=recovery,
        evidence=evidence,
        failure_classifications=failure_classifications,
        next_actions=next_actions,
    )


def _build_outcome(
    status: str,
    completed_phases: tuple[str, ...],
    failed_phases: tuple[str, ...],
    skipped_phases: tuple[str, ...],
) -> str:
    parts = [
        (
            "Project setup completed successfully."
            if status == "success"
            else f"Project setup finished with status: {status}."
        )
    ]
    if completed_phases:
        parts.append(f"Completed phases: {', '.join(completed_phases)}.")
    if failed_phases:
        parts.append(f"Failed phases: {', '.join(failed_phases)}.")
    if skipped_phases:
        parts.append(f"Skipped phases: {', '.join(skipped_phases)}.")
    return " ".join(parts)


def _phase_names_by_status(state: UIRunState, status: str) -> tuple[str, ...]:
    return tuple(
        _display_phase_name(snapshot.phase)
        for snapshot in state.phases
        if snapshot.status == status
    )


def _display_phase_name(phase: Any) -> str:
    value = getattr(phase, "value", str(phase))
    return str(value).replace("_", " ").title()


def _format_timeline_entry(entry: UITimelineEntry) -> str:
    if entry.details:
        return f"{entry.message}: {entry.details}"
    return entry.message


def _format_evidence_record(record: UIEvidenceRecord) -> str:
    parts = [record.summary]
    if record.path:
        parts.append(record.path)
    return " | ".join(parts)


def _failure_classifications(
    timeline: tuple[UITimelineEntry, ...],
) -> tuple[str, ...]:
    classifications: list[str] = []
    seen: set[str] = set()
    for entry in timeline:
        if not entry.failure_classification:
            continue
        classification = str(entry.failure_classification)
        if classification in seen:
            continue
        seen.add(classification)
        classifications.append(classification)
    return tuple(classifications)


def _format_recovery_entry(entry: UITimelineEntry) -> str:
    metadata = entry.metadata
    parts = []

    strategy = metadata.get("recovery_strategy") or metadata.get("strategy")
    if strategy:
        parts.append(f"strategy={_stringify(strategy)}")

    guidance = metadata.get("guidance") or entry.message
    if guidance:
        parts.append(f"guidance={_stringify(guidance)}")

    for key in ("recovery_params", "parameter_diff", "recovery"):
        value = metadata.get(key)
        if value:
            parts.append(f"{key}={_stringify(value)}")

    return " | ".join(parts)


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    return repr(value)
