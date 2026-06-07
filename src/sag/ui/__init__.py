"""
UI Module for Setup Agent

Provides enhanced CLI UI with Rich components, live displays, and event-driven updates.
"""

from sag.ui.events import EventType, PhaseType, UIEvent
from sag.ui.ui_manager import UIManager

__all__ = ["UIEvent", "EventType", "PhaseType", "UIManager"]
