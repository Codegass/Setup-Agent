from types import SimpleNamespace

import sag.agent.react_engine as react_engine_module
from sag.agent.phase_machine import PhaseMachine, PhaseTermination
from sag.agent.react_engine import ReActEngine


class _PromptBuilder:
    def invalidate_trunk_cache(self):
        pass

    def build_initial_system_prompt(self, **kwargs):
        return "system prompt"

    def build_mode_prompt(self, prompt, mode, **kwargs):
        return prompt


class _LLMClient:
    def __init__(self, response="unparseable response", error=None):
        self.response = response
        self.error = error

    def capabilities_for(self, mode):
        return SimpleNamespace(supports_function_calling=False, model="test-model")

    def get_response(self, prompt, mode):
        if self.error is not None:
            raise self.error
        return self.response


class _ResponseParser:
    def parse(self, response, **kwargs):
        return []


def _engine(*, response="unparseable response", error=None, wall_clock_cap=0):
    engine = ReActEngine.__new__(ReActEngine)
    engine.max_iterations = 3
    engine.phase_machine = PhaseMachine()
    engine.config = SimpleNamespace(max_wall_clock_seconds=wall_clock_cap)
    engine.agent_logger = SimpleNamespace(info=lambda *args, **kwargs: None)
    engine.prompt_builder = _PromptBuilder()
    engine.repository_url = "https://example.test/repo.git"
    engine.repository_ref = None
    engine.llm_client = _LLMClient(response=response, error=error)
    engine.state_evaluator = SimpleNamespace(completion_mode="previous")
    engine.token_tracker = SimpleNamespace(set_iteration=lambda iteration: None)
    engine.response_parser = _ResponseParser()
    engine._phase_intro_step = lambda: SimpleNamespace(content="phase intro")
    engine._start_phase_branch = lambda: None
    engine._enforce_phase_floors = lambda: False
    engine._should_use_thinking_model = lambda: True
    engine._export_token_usage_csv = lambda: None
    return engine


def _assert_setup_abort(engine, reason):
    assert len(engine.phase_machine.records) == 1
    record = engine.phase_machine.records[0]
    assert record.termination is PhaseTermination.ABORTED
    assert record.reason == reason
    assert engine.phase_machine.current_phase == "provision"
    assert engine.phase_machine.termination_state() == "aborted"
    assert any(f"ABORTED: {reason}" in line for line in engine.phase_machine.digest_lines())


def test_setup_wall_clock_exhaustion_records_abort_without_advancing(monkeypatch):
    engine = _engine(wall_clock_cap=1)
    clock = iter([100.0, 102.0, 102.0])
    monkeypatch.setattr(react_engine_module.time, "time", lambda: next(clock))

    succeeded = engine.run_react_loop("set up project", max_iterations=3)

    assert succeeded is False
    _assert_setup_abort(engine, "wall clock cap exceeded")


def test_setup_empty_llm_response_records_abort_without_advancing():
    engine = _engine(response="")

    succeeded = engine.run_react_loop("set up project", max_iterations=3)

    assert succeeded is False
    _assert_setup_abort(engine, "LLM response unavailable")


def test_setup_iteration_exhaustion_records_abort_without_advancing():
    engine = _engine(response="unparseable response")

    succeeded = engine.run_react_loop("set up project", max_iterations=2)

    assert succeeded is False
    assert engine.current_iteration == 2
    _assert_setup_abort(engine, "iteration budget exhausted")


def test_setup_engine_exception_records_abort_without_advancing():
    engine = _engine(error=RuntimeError("LLM transport failed"))

    succeeded = engine.run_react_loop("set up project", max_iterations=3)

    assert succeeded is False
    _assert_setup_abort(engine, "engine exception: RuntimeError")


def test_setup_duplicate_cleanup_keeps_first_abort_record():
    engine = _engine(response="")
    engine.run_react_loop("set up project", max_iterations=3)
    first_record = engine.phase_machine.records[0]

    engine._record_setup_abort(True, "duplicate cleanup")

    assert engine.phase_machine.records == (first_record,)
    _assert_setup_abort(engine, "LLM response unavailable")


def test_run_task_abnormal_exit_does_not_record_setup_abort():
    engine = _engine(response="")

    succeeded = engine.run_react_loop(
        "perform one task",
        max_iterations=3,
        completion_mode="run_task",
    )

    assert succeeded is False
    assert engine.phase_machine.records == ()
    assert engine.phase_machine.current_phase == "provision"
    assert engine.phase_machine.termination_state() == "open"
