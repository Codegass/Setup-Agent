"""
UI Module for Setup Agent

Provides enhanced CLI UI with Rich components, live displays, and event-driven updates.
"""

from ui.events import UIEvent, EventType, PhaseType
from ui.ui_manager import UIManager

__all__ = ["UIEvent", "EventType", "PhaseType", "UIManager"]
