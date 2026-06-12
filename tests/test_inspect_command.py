"""sag inspect: render journals + phase history for debugging what the model
saw (spec §7). Helpers are pure; sources (container/session-dir) are injected."""

import json

from sag.main import _inspect_render_timeline, _inspect_render_iteration


RECORDS = [
    {"iteration": 1, "phase": "build", "segments": {}, "delta": {"added": 1, "compacted": 0},
     "total_chars": 4000, "intro_text": "=== PHASE: BUILD ===\nobjective", "step_span": 1},
    {"iteration": 2, "phase": "build", "segments": {}, "delta": {"added": 2, "compacted": 0},
     "total_chars": 5200, "step_span": 3},
    {"iteration": 3, "phase": "build", "segments": {}, "delta": {"added": 1, "compacted": 9},
     "total_chars": 4100, "ledger_text": "ATTEMPT LEDGER:\n✗ build: enforcer → output_a",
     "step_span": 2},
]


def test_timeline_one_line_per_iteration_with_markers():
    out = _inspect_render_timeline(RECORDS)
    lines = [l for l in out.splitlines() if l.strip().startswith("iter")]
    assert len(lines) == 3
    assert "INTRO" in out and "LEDGER" in out
    assert "compacted=9" in out


def test_iteration_view_shows_window_composition():
    out = _inspect_render_iteration(RECORDS, 3, history_entries=[
        {"type": "action", "tool_name": "build", "success": False, "output": "BUILD FAILED: enforcer"},
    ])
    assert "=== PHASE: BUILD ===" in out, "nearest intro at-or-before the iteration"
    assert "ATTEMPT LEDGER" in out
    assert "BUILD FAILED" in out


def test_iteration_view_missing_iter_says_so():
    out = _inspect_render_iteration(RECORDS, 99, history_entries=[])
    assert "no journal record" in out.lower()
