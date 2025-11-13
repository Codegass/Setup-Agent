"""
Event system for UI updates

Defines event types and event classes for communicating UI state changes
between components and the UIManager.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class PhaseType(str, Enum):
    """High-level phases of the setup process"""
    SETUP = "setup"
    BUILD = "build"
    TEST = "test"
    VERIFICATION = "verification"


class EventType(str, Enum):
    """Types of UI events"""
    # Phase events
    PHASE_START = "phase_start"
    PHASE_COMPLETE = "phase_complete"
    PHASE_ERROR = "phase_error"

    # Step events (within a phase)
    STEP_START = "step_start"
    STEP_UPDATE = "step_update"
    STEP_COMPLETE = "step_complete"
    STEP_ERROR = "step_error"

    # Status events
    STATUS_UPDATE = "status_update"

    # Agent events
    AGENT_THOUGHT = "agent_thought"
    AGENT_ACTION = "agent_action"
    AGENT_OBSERVATION = "agent_observation"

    # Docker events
    DOCKER_INIT = "docker_init"
    DOCKER_CONTAINER_CREATE = "docker_container_create"
    DOCKER_COMMAND = "docker_command"
    DOCKER_READY = "docker_ready"

    # Validation events
    VALIDATION_START = "validation_start"
    VALIDATION_CHECK = "validation_check"
    VALIDATION_COMPLETE = "validation_complete"

    # Project events
    PROJECT_ANALYSIS = "project_analysis"

    # Report events
    REPORT_GENERATED = "report_generated"

    # Error events
    ERROR = "error"
    WARNING = "warning"

    # Completion events
    SUCCESS = "success"
    FAILURE = "failure"


@dataclass
class UIEvent:
    """
    Base event class for UI updates

    Attributes:
        event_type: Type of the event
        message: Human-readable message
        phase: Current phase (optional)
        details: Additional details (optional)
        timestamp: Event timestamp
        level: Importance level (info, warning, error)
        metadata: Additional metadata for the event
    """
    event_type: EventType
    message: str
    phase: Optional[PhaseType] = None
    details: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)
    level: str = "info"  # info, warning, error, success
    metadata: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"[{self.timestamp.strftime('%H:%M:%S')}] {self.event_type.value}: {self.message}"


class UIEventEmitter:
    """
    Event emitter mixin for components that emit UI events

    Components should inherit from this class and call emit_event()
    to send updates to the UIManager.
    """

    def __init__(self):
        self._ui_manager = None

    def set_ui_manager(self, ui_manager):
        """Set the UIManager instance to receive events"""
        self._ui_manager = ui_manager

    def emit_event(self, event: UIEvent):
        """Emit a UI event to the UIManager"""
        if self._ui_manager is not None:
            self._ui_manager.handle_event(event)

    def emit(
        self,
        event_type: EventType,
        message: str,
        phase: Optional[PhaseType] = None,
        details: Optional[str] = None,
        level: str = "info",
        **metadata
    ):
        """
        Convenience method to create and emit an event

        Args:
            event_type: Type of the event
            message: Human-readable message
            phase: Current phase (optional)
            details: Additional details (optional)
            level: Importance level (info, warning, error, success)
            **metadata: Additional metadata
        """
        event = UIEvent(
            event_type=event_type,
            message=message,
            phase=phase,
            details=details,
            level=level,
            metadata=metadata
        )
        self.emit_event(event)
