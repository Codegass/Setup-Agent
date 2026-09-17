"""
UI event types shared by the agent's tools.

The live terminal display this package once painted has been retired; the run
is now shown one line per turn, derived from the control ledger.
"""

from sag.ui.events import EventType, PhaseType, UIEvent

__all__ = ["UIEvent", "EventType", "PhaseType"]
