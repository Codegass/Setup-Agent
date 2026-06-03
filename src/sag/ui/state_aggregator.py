"""Aggregate UI events into typed CLI/TUI run state."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from typing import Any, Callable, Optional

from sag.ui.events import EventType, PhaseType, UIEvent
from sag.ui.state import (
    ActiveOperation,
    PhaseSnapshot,
    UIEvidenceRecord,
    UIRunState,
    UITimelineEntry,
    initial_run_state,
)

_MISSING = object()


class UIStateAggregator:
    def __init__(self, project_name: str, clock: Callable[[], datetime] | None = None):
        self._clock = clock or datetime.now
        self._state = initial_run_state(project_name, self._clock())

    def snapshot(self) -> UIRunState:
        return self._state

    def handle(self, event: Any) -> UIRunState:
        event = self._normalize_event(event)
        if event is None:
            return self._state

        if not isinstance(event.event_type, EventType):
            self._state = self._append_warning(
                f"Unknown UI event ignored: {event.event_type}: {event.message}",
                details=event.details,
            )
            return self._state

        handler = {
            EventType.PHASE_START: self._handle_phase_start,
            EventType.PHASE_COMPLETE: self._handle_phase_complete,
            EventType.PHASE_ERROR: self._handle_phase_error,
            EventType.STEP_START: self._handle_step_start,
            EventType.STEP_COMPLETE: self._handle_step_complete,
            EventType.STEP_ERROR: self._handle_step_error,
            EventType.STATUS_UPDATE: self._handle_status_update,
            EventType.AGENT_THOUGHT: self._handle_agent_thought,
            EventType.AGENT_ACTION: self._handle_agent_action,
            EventType.AGENT_OBSERVATION: self._handle_agent_observation,
            EventType.VALIDATION_START: self._handle_validation_event,
            EventType.VALIDATION_CHECK: self._handle_validation_event,
            EventType.VALIDATION_COMPLETE: self._handle_validation_event,
            EventType.WARNING: self._handle_warning,
            EventType.ERROR: self._handle_error,
            EventType.REPORT_GENERATED: self._handle_report_generated,
            EventType.SUCCESS: self._handle_success,
            EventType.FAILURE: self._handle_failure,
        }.get(event.event_type)

        if handler is None:
            self._state = self._append_warning(
                f"Unhandled UI event ignored: {event.event_type.value}: {event.message}",
                details=event.details,
            )
            return self._state

        handler(event)
        return self._state

    def _normalize_event(self, event: Any) -> UIEvent | None:
        if not isinstance(event, UIEvent):
            self._state = self._append_warning(
                self._malformed_event_message(event, "not a UIEvent")
            )
            return None

        event_type = getattr(event, "event_type", _MISSING)
        message = getattr(event, "message", _MISSING)
        if event_type is _MISSING or message is _MISSING or message is None:
            self._state = self._append_warning(
                self._malformed_event_message(event, "missing required fields")
            )
            return None
        event_type_label = (
            event_type.value if isinstance(event_type, EventType) else str(event_type)
        )
        if not isinstance(message, str):
            self._state = self._append_warning(
                f"Malformed UI event ignored: {event_type_label}: non-string message {message!r}"
            )
            return None

        details = getattr(event, "details", None)
        if details is not None and not isinstance(details, str):
            self._state = self._append_warning(
                f"Malformed UI event ignored: {event_type_label}: "
                f"non-string details {details!r}: {message}"
            )
            return None

        level = getattr(event, "level", "info") or "info"
        if not isinstance(level, str):
            self._state = self._append_warning(
                f"Malformed UI event ignored: {event_type_label}: "
                f"non-string level {level!r}: {message}"
            )
            return None

        phase = getattr(event, "phase", None)
        if phase is not None and not isinstance(phase, PhaseType):
            self._state = self._append_warning(
                f"Malformed UI event ignored: {event_type_label}: invalid phase {phase}: {message}",
                details=details,
            )
            return None

        metadata = getattr(event, "metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}

        return UIEvent(
            event_type=event_type,
            message=message,
            phase=phase,
            details=details,
            level=level,
            metadata=metadata,
        )

    def _malformed_event_message(self, event: Any, reason: str) -> str:
        parts = [f"Malformed UI event ignored: {type(event).__name__}", reason]
        event_type = getattr(event, "event_type", None)
        message = getattr(event, "message", None)
        if event_type is not None:
            parts.append(f"event_type={event_type}")
        if message is not None:
            parts.append(f"message={message}")
        return "; ".join(parts)

    def _handle_phase_start(self, event: UIEvent) -> None:
        if event.phase:
            self._state = self._replace_phase(event.phase, status="running")
            self._state = replace(self._state, current_phase=event.phase)
        self._state = replace(self._state, current_status=event.message)
        self._state = self._append_timeline(event, kind="phase")

    def _handle_phase_complete(self, event: UIEvent) -> None:
        if event.phase:
            self._state = self._replace_phase(event.phase, status="success")
        self._state = replace(self._state, current_status=event.message)
        self._state = self._append_timeline(event, kind="phase")

    def _handle_phase_error(self, event: UIEvent) -> None:
        if event.phase:
            self._state = self._replace_phase(event.phase, status="error")
        classification = (
            "verification_failure" if event.phase == PhaseType.VERIFICATION else "tool_failure"
        )
        entry = self._build_timeline(
            event,
            kind="error",
            level="error",
            failure_classification=classification,
        )
        self._state = replace(
            self._state,
            latest_error=entry,
            timeline=self._state.timeline + (entry,),
        )

    def _handle_step_start(self, event: UIEvent) -> None:
        if event.phase:
            phase = self._phase_snapshot(event.phase)
            step = {
                "name": event.message,
                "status": "running",
                "details": event.details,
            }
            self._state = self._replace_phase(event.phase, steps=phase.steps + (step,))
        self._state = replace(self._state, current_status=event.message)
        self._state = self._append_timeline(event, kind="step")

    def _handle_step_complete(self, event: UIEvent) -> None:
        if event.phase:
            self._state = self._update_running_or_named_step(
                event.phase, event.message, "success", event.details
            )
        self._state = self._append_timeline(event, kind="step")

    def _handle_step_error(self, event: UIEvent) -> None:
        if event.phase:
            self._state = self._update_running_or_named_step(
                event.phase, event.message, "error", event.details
            )
        entry = self._build_timeline(event, kind="error", level="error")
        self._state = replace(
            self._state,
            latest_error=entry,
            timeline=self._state.timeline + (entry,),
        )

    def _handle_status_update(self, event: UIEvent) -> None:
        self._state = replace(self._state, current_status=event.message)
        self._state = self._append_timeline(event, kind="status")

    def _handle_agent_thought(self, event: UIEvent) -> None:
        summary = self._summarize_thought(event.message)
        self._state = replace(self._state, current_status=summary)
        self._state = self._append_timeline(event, kind="thought", message=summary)

    def _handle_agent_action(self, event: UIEvent) -> None:
        tool_name = str(event.metadata.get("tool_name", "unknown"))
        tool_params = self._copy_metadata(event.metadata.get("tool_params", {}))
        if not isinstance(tool_params, dict):
            tool_params = {}

        detected_phase = self._detect_phase_from_action(tool_name, tool_params)
        if detected_phase:
            phase = self._phase_snapshot(detected_phase)
            status = "running" if phase.status == "pending" else phase.status
            self._state = self._replace_phase(detected_phase, status=status)
            self._state = replace(self._state, current_phase=detected_phase)

        action = self._format_tool_params(tool_name, tool_params)
        visible_params = f"({action})" if action else ""
        current_status = f"Using {tool_name} {visible_params}".strip()
        operation = ActiveOperation(
            tool_name=tool_name,
            action=action,
            workdir=self._extract_workdir(tool_params),
            visible_params=visible_params,
            started_at=self._clock(),
            detail=event.message,
        )
        self._state = replace(
            self._state,
            active_operation=operation,
            current_status=current_status,
        )
        self._state = self._append_timeline(event, kind="tool", message=current_status)

    def _handle_agent_observation(self, event: UIEvent) -> None:
        summary = self._summarize_observation(event.message)
        operation = replace(self._state.active_operation, detail=summary)
        self._state = replace(
            self._state,
            active_operation=operation,
            current_status=summary,
        )
        self._state = self._append_timeline(event, kind="observation", message=summary)

    def _handle_validation_event(self, event: UIEvent) -> None:
        summary = event.metadata.get("summary") or event.metadata.get("result")
        path = event.metadata.get("path")
        if summary or path:
            self._state = self._append_evidence(
                "validation",
                str(summary or event.message),
                details=event.details,
                path=str(path) if path else None,
                metadata=event.metadata,
            )

        kind = "error" if event.level == "error" else "evidence"
        entry = self._build_timeline(
            event,
            kind=kind,
            level=event.level,
            failure_classification="verification_failure" if kind == "error" else None,
        )
        if kind == "error":
            self._state = replace(self._state, latest_error=entry)
        self._state = replace(self._state, timeline=self._state.timeline + (entry,))

    def _handle_warning(self, event: UIEvent) -> None:
        entry = self._build_timeline(
            event,
            kind="warning",
            level="warning",
            failure_classification="warning",
        )
        self._state = replace(
            self._state,
            latest_warning=entry,
            timeline=self._state.timeline + (entry,),
        )

    def _handle_error(self, event: UIEvent) -> None:
        entry = self._build_timeline(event, kind="error", level="error")
        self._state = replace(
            self._state,
            latest_error=entry,
            timeline=self._state.timeline + (entry,),
        )

    def _handle_report_generated(self, event: UIEvent) -> None:
        report_data = self._copy_metadata(event.metadata)
        self._state = replace(
            self._state,
            report_data=report_data,
            current_status=event.message,
        )
        self._state = self._append_evidence(
            "report",
            event.message,
            details=event.details,
            path=str(report_data.get("report_path")) if report_data.get("report_path") else None,
            metadata=report_data,
        )
        self._state = self._append_timeline(event, kind="report")

    def _handle_success(self, event: UIEvent) -> None:
        self._state = replace(
            self._state,
            is_complete=True,
            final_status="success",
            current_status=event.message,
        )
        self._state = self._append_timeline(event, kind="completion", level="success")

    def _handle_failure(self, event: UIEvent) -> None:
        self._state = replace(
            self._state,
            is_complete=True,
            final_status="failure",
            current_status=event.message,
        )
        self._state = self._append_timeline(
            event,
            kind="completion",
            level="error",
            failure_classification="final_failure",
        )

    def _append_timeline(
        self,
        event: UIEvent,
        kind: str,
        message: Optional[str] = None,
        level: Optional[str] = None,
        failure_classification: Optional[str] = None,
    ) -> UIRunState:
        entry = self._build_timeline(
            event,
            kind,
            message=message,
            level=level,
            failure_classification=failure_classification,
        )
        return replace(self._state, timeline=self._state.timeline + (entry,))

    def _append_warning(self, message: str, details: Optional[str] = None) -> UIRunState:
        event = UIEvent(EventType.WARNING, message, details=details, level="warning")
        entry = self._build_timeline(
            event,
            kind="warning",
            level="warning",
            failure_classification="warning",
        )
        return replace(
            self._state,
            latest_warning=entry,
            timeline=self._state.timeline + (entry,),
        )

    def _classify_failure(self, event: UIEvent) -> str | None:
        failure_type = event.metadata.get("failure_type")
        if failure_type:
            return str(failure_type)

        event_name = (
            event.event_type.value
            if isinstance(event.event_type, EventType)
            else str(event.event_type)
        ).lower()
        searchable_parts = [
            event_name,
            event.message,
            event.details or "",
            str(event.metadata.get("error_code", "")),
        ]
        searchable = " ".join(searchable_parts).lower()
        if "timeout" in searchable:
            return "command_timeout"
        has_parameter_normalization_text = "parameter" in searchable and any(
            token in searchable for token in ("fix", "normal", "normalize", "normalization")
        )
        if (
            event.metadata.get("parameter_fix")
            or event.metadata.get("parameter_fixes")
            or event.metadata.get("parameter_normalization")
            or has_parameter_normalization_text
        ):
            return "parameter_normalization"
        has_recovery_attempt_text = any(
            token in searchable for token in ("recovery", "recover", "fallback", "retry")
        )
        if (
            event.metadata.get("recovery_attempted")
            or event.metadata.get("recovery_strategy")
            or event.metadata.get("fallback")
            or event.metadata.get("retry")
            or has_recovery_attempt_text
        ):
            return "recovery_attempt"
        if (
            event.event_type
            in {
                EventType.VALIDATION_START,
                EventType.VALIDATION_CHECK,
                EventType.VALIDATION_COMPLETE,
            }
            or event.phase == PhaseType.VERIFICATION
        ):
            return "verification_failure"
        if event.event_type == EventType.WARNING:
            return "warning"
        if event.event_type == EventType.FAILURE:
            return "final_failure"
        return "tool_failure"

    def _append_evidence(
        self,
        kind: str,
        summary: str,
        details: Optional[str] = None,
        path: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> UIRunState:
        evidence = UIEvidenceRecord(
            timestamp=self._clock(),
            kind=kind,
            summary=summary,
            details=details,
            path=path,
            metadata=self._copy_metadata(metadata or {}),
        )
        return replace(self._state, evidence=self._state.evidence + (evidence,))

    def _replace_phase(
        self,
        phase: PhaseType,
        status: Optional[str] = None,
        steps: Optional[tuple[dict[str, Any], ...]] = None,
    ) -> UIRunState:
        phases = tuple(
            (
                replace(
                    snapshot,
                    status=status or snapshot.status,
                    steps=self._copy_steps(steps if steps is not None else snapshot.steps),
                )
                if snapshot.phase == phase
                else snapshot
            )
            for snapshot in self._state.phases
        )
        return replace(self._state, phases=phases, current_phase=phase)

    def _update_running_or_named_step(
        self,
        phase: PhaseType,
        name: str,
        status: str,
        details: Optional[str],
    ) -> UIRunState:
        snapshot = self._phase_snapshot(phase)
        updated_steps = [self._copy_metadata(step) for step in snapshot.steps]
        target_index = next(
            (index for index, step in enumerate(updated_steps) if step.get("name") == name),
            None,
        )
        if target_index is None:
            target_index = next(
                (
                    index
                    for index, step in enumerate(updated_steps)
                    if step.get("status") == "running"
                ),
                None,
            )
        if target_index is not None:
            updated_steps[target_index]["status"] = status
            if details:
                updated_steps[target_index]["details"] = details
        return self._replace_phase(phase, steps=tuple(updated_steps))

    def _format_tool_params(self, tool_name: str, params: dict[str, Any]) -> str:
        if not params:
            return ""

        important_params = {
            "bash": ["command"],
            "manage_context": ["action"],
            "file_io": ["action", "path"],
            "maven": ["goal", "action"],
            "gradle": ["task", "action"],
            "project_setup": ["action"],
            "project_analyzer": ["action"],
            "report": ["action"],
        }
        params_to_show = important_params.get(tool_name, list(params.keys())[:2])

        formatted_parts = []
        for param in params_to_show:
            if param not in params:
                continue
            value_str = str(params[param])
            if len(value_str) > 50:
                value_str = value_str[:47] + "..."
            formatted_parts.append(f"{param}='{value_str}'")

        return ", ".join(formatted_parts)

    def _detect_phase_from_action(
        self, tool_name: str, params: dict[str, Any]
    ) -> Optional[PhaseType]:
        if self._state.current_phase == PhaseType.VERIFICATION:
            return None
        if tool_name == "report":
            return PhaseType.VERIFICATION

        if tool_name in {"maven", "gradle"}:
            goal = str(params.get("goal", ""))
            task = str(params.get("task", ""))
            action = str(params.get("action", ""))
            test_text = " ".join([goal, task, action]).lower()
            if "test" in test_text:
                return PhaseType.TEST
            if any(
                keyword in test_text
                for keyword in ["compile", "package", "install", "build", "assemble"]
            ):
                return PhaseType.BUILD

        if tool_name == "bash":
            command = str(params.get("command", "")).lower()
            if any(
                keyword in command
                for keyword in ["mvn test", "gradle test", "pytest", "npm test", "test"]
            ):
                return PhaseType.TEST
            if any(
                keyword in command
                for keyword in [
                    "mvn compile",
                    "mvn package",
                    "mvn install",
                    "gradle build",
                    "gradle assemble",
                    "make",
                    "npm run build",
                ]
            ):
                return PhaseType.BUILD

        return None

    def _extract_workdir(self, params: dict[str, Any]) -> Optional[str]:
        for key in ("working_directory", "workdir", "cwd", "directory"):
            if params.get(key):
                return str(params[key])
        return None

    def _summarize_thought(self, text: str) -> str:
        thought = text.strip()
        for prefix in ("I need to ", "I should ", "I will "):
            if thought.startswith(prefix):
                thought = thought.removeprefix(prefix)
                break
        sentences = thought.split(". ")
        summary = sentences[0].strip() if sentences else thought
        if len(summary) < 20 and len(sentences) > 1:
            second = sentences[1].strip()
            summary = f"{summary}. {second[:37]}..." if len(second) > 40 else f"{summary}. {second}"
        return summary[:77] + "..." if len(summary) > 80 else summary

    def _summarize_observation(self, text: str) -> str:
        observation = text.strip()
        lines = [line.strip() for line in observation.splitlines() if line.strip()]
        lowered = observation.lower()
        if "success" in lowered:
            for line in lines:
                if "success" in line.lower():
                    return self._truncate(line, 100)
        if "error" in lowered or "failed" in lowered:
            for line in lines:
                if "error" in line.lower() or "failed" in line.lower():
                    return self._truncate(line, 100)
        for line in lines:
            if len(line) > 10:
                return self._truncate(line, 100)
        return self._truncate(observation, 100)

    def _build_timeline(
        self,
        event: UIEvent,
        kind: str,
        message: Optional[str] = None,
        level: Optional[str] = None,
        failure_classification: Optional[str] = None,
    ) -> UITimelineEntry:
        classification = failure_classification
        entry_level = level or event.level
        if classification is None and (
            kind in {"warning", "error"} or entry_level in {"warning", "error"}
        ):
            classification = self._classify_failure(event)
        return UITimelineEntry(
            timestamp=self._clock(),
            kind=kind,
            message=message or event.message,
            level=entry_level,
            details=event.details,
            failure_classification=classification,
            metadata=self._copy_metadata(event.metadata),
        )

    def _phase_snapshot(self, phase: PhaseType) -> PhaseSnapshot:
        for snapshot in self._state.phases:
            if snapshot.phase == phase:
                return snapshot
        raise ValueError(f"Unknown phase: {phase}")

    def _copy_steps(self, steps: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
        return tuple(self._copy_metadata(step) for step in steps)

    def _copy_metadata(self, metadata: Any) -> Any:
        return deepcopy(metadata)

    def _truncate(self, text: str, limit: int) -> str:
        return text[: limit - 3] + "..." if len(text) > limit else text
