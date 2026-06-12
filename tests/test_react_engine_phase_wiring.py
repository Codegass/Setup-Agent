# tests/test_react_engine_phase_wiring.py
"""Phase-machine wiring seams: signal handling, window reset, budget forcing.

These test the helper methods the loop calls, with a minimal fake engine
state — not the full LLM loop."""

from types import SimpleNamespace

from sag.agent.phase_machine import PhaseMachine
from sag.agent.react_engine import ReActEngine


def _engine_with_machine():
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = PhaseMachine()
    engine.steps = [SimpleNamespace(step_type=None, content="old")] * 7
    engine.context_journal = None
    engine._phase_iterations = 12
    engine.config = SimpleNamespace(
        phase_min_floors={"analyze": 4, "build": 10, "test": 12, "report": 8},
        max_iterations=150,
    )
    engine.current_iteration = 10
    engine.context_manager = SimpleNamespace(
        update_task_status=lambda *a, **k: True,
        current_task_id=None,
    )
    engine.agent_logger = SimpleNamespace(info=lambda *a, **k: None)
    return engine


def test_phase_done_signal_advances_and_resets_window():
    engine = _engine_with_machine()
    step = SimpleNamespace(
        step_type=SimpleNamespace(value="action"), tool_name="phase",
        tool_result=SimpleNamespace(
            success=True,
            metadata={"phase_signal": "done", "key_results": "cloned + JDK", "evidence": []},
        ),
    )

    engine._handle_phase_signals([step])

    assert engine.phase_machine.current_phase == "analyze"
    assert len(engine.steps) == 1, "window must reset to the phase intro"
    intro = engine.steps[0].content
    assert "analyze" in intro.lower()
    assert "cloned + JDK" in intro, "prior key results carried into the digest"
    assert engine._phase_iterations == 0


def test_phase_blocked_signal_records_and_advances():
    engine = _engine_with_machine()
    step = SimpleNamespace(
        step_type=SimpleNamespace(value="action"), tool_name="phase",
        tool_result=SimpleNamespace(
            success=True,
            metadata={"phase_signal": "blocked", "reason": "no network", "evidence": []},
        ),
    )

    engine._handle_phase_signals([step])

    assert engine.phase_machine.records[0].status == "blocked"
    assert engine.phase_machine.current_phase == "analyze"


def test_floor_starvation_forces_blocked():
    engine = _engine_with_machine()
    # 150-iteration run, still in provision, but only 29 iterations remain:
    # analyze+build+test+report floors (4+10+12+8=34) would starve.
    engine.current_iteration = 121

    forced = engine._enforce_phase_floors()

    assert forced is True
    assert engine.phase_machine.records[0].status == "blocked"
    assert "reserved" in engine.phase_machine.records[0].reason.lower()
    assert engine.phase_machine.current_phase == "analyze"


def test_hard_phase_may_consume_savings():
    engine = _engine_with_machine()
    # Deep into the run but only test+report remain after build: floors 12+8=20.
    engine.phase_machine.mark_done("ok", [])
    engine.phase_machine.mark_done("ok", [])
    assert engine.phase_machine.current_phase == "build"
    engine.current_iteration = 100  # 50 remain > 20 reserved -> build keeps going

    assert engine._enforce_phase_floors() is False


def test_no_machine_means_no_phase_behavior():
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = None
    assert engine._handle_phase_signals([]) is None
    assert engine._enforce_phase_floors() is False


def test_phase_transition_resets_context_switch_counter():
    """No manage_context actions exist in phase mode, so the legacy reset
    never fires; phase transitions are the context switches now."""
    engine = _engine_with_machine()
    engine.steps_since_context_switch = 23
    step = SimpleNamespace(
        step_type=SimpleNamespace(value="action"), tool_name="phase",
        tool_result=SimpleNamespace(
            success=True,
            metadata={"phase_signal": "done", "key_results": "ok", "evidence": []},
        ),
    )

    engine._handle_phase_signals([step])

    assert engine.steps_since_context_switch == 0

    forced = _engine_with_machine()
    forced.steps_since_context_switch = 23
    forced.current_iteration = 121  # floors force-block (see test above)

    assert forced._enforce_phase_floors() is True
    assert forced.steps_since_context_switch == 0


def test_persist_phase_record_warns_when_trunk_task_missing():
    """A missing phase_<name> trunk task must at least warn — silent False
    returns hid the analyzer-rewrite defect for an entire run."""
    from loguru import logger as loguru_logger

    from sag.agent.context_manager import TrunkContext

    class _CM:
        current_task_id = None

        def __init__(self, trunk):
            self.trunk = trunk

        def load_trunk_context(self):
            return self.trunk

        def _save_trunk_context(self, trunk):
            pass

    trunk = TrunkContext(context_id="t", goal="g", project_url="u", project_name="p")
    trunk.add_task("some unrelated task")  # no phase_build entry
    engine = ReActEngine.__new__(ReActEngine)
    engine.context_manager = _CM(trunk)

    messages = []
    handler_id = loguru_logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        engine._persist_phase_record("build", "completed", "compiled fine")
    finally:
        loguru_logger.remove(handler_id)

    assert any("phase_build" in m for m in messages), (
        "missing trunk phase task must produce a warning, not a silent no-op"
    )
