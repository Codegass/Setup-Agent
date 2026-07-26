# tests/test_evidence_triggered_enrichment.py
"""Physical-evidence enrichment fires on evidence, not on words (Plan 3 Task 2).

The enrichment payload is Gen 2; its trigger was the last Gen 1 survivor — a
keyword substring scan of the observation text. That scan misfires in both
directions: a bash observation that merely says "build success" ran the
Java-artifact probe (loud nonsense on a Python repo), and a build tool that
reported neutrally got no enrichment at all. The trigger is now the tool whose
execution produced the observation, threaded from `_execute_action_step`.
"""

from types import SimpleNamespace

import pytest

from sag.agent.react_engine import ReActEngine
from sag.agent.react_types import ReActStep, StepType

BUILD_FAMILY = ("build", "maven", "gradle", "python")
NON_BUILD_TOOLS = ("bash", "search", "phase", "file_io", "project", "report", "advisor")

PHYSICAL_STATE = {"class_files": 3, "jar_files": 1}
ENRICHMENT_MARK = "[PHYSICAL EVIDENCE: 3 .class files exist]"


def _scripted_result():
    """A plain stand-in for ToolResult carrying only what the ACTION path reads."""
    return SimpleNamespace(
        succeeded=True,
        error_code=None,
        error=None,
        output="ok",
        output_ref=None,
        evidence_refs=[],
        refs=[],
        metadata={},
        invocation_status=SimpleNamespace(value="completed"),
        operation_outcome=SimpleNamespace(value="succeeded"),
        evidence_status=SimpleNamespace(value="present"),
        evidence_assessment=SimpleNamespace(value="supported"),
        failure_signature=None,
        error_tail_preview=None,
    )


@pytest.fixture
def enrichment_engine():
    """A `ReActEngine` whose tool execution is scripted and whose physical
    probe is the real one, wrapped so the test can see every invocation.

    House fixture pattern from `tests/test_native_dispatch.py`.
    """

    def _build():
        engine = ReActEngine.__new__(ReActEngine)
        engine.steps = []
        engine.current_iteration = 2
        engine.config = SimpleNamespace(verbose=False)
        engine.agent_logger = SimpleNamespace(
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
        )
        engine.context_manager = SimpleNamespace(current_task_id=None, project_name="demo")
        engine.token_tracker = SimpleNamespace(update_last_tool_name=lambda name: None)
        engine.emit = lambda *a, **k: None
        # No scheduler and no sealed evidence: the real refusal guards run and
        # decline to refuse, so calls reach the scripted executor.
        engine.run_evidence_state = None
        engine.phase_machine = None
        engine.loop_memory = None
        engine.output_storage = None
        engine.command_tracker = None
        engine.physical_validator = SimpleNamespace(
            validate_build_artifacts=lambda project_name=None: dict(PHYSICAL_STATE)
        )
        engine.executed_calls = []
        engine.probed = []
        engine.next_observation_text = "observed"

        real_probe = engine._get_physical_validation_state

        def spying_probe(observation):
            engine.probed.append(observation)
            return real_probe(observation)

        engine._get_physical_validation_state = spying_probe

        def fake_execute_tool_call(call):
            engine.executed_calls.append(call)
            return SimpleNamespace(
                call=call,
                result=_scripted_result(),
                status="success",
                raw_params=call.raw_params,
                validated_params=dict(call.raw_params),
                observation_text=engine.next_observation_text,
                attempted_execution=True,
                metadata={},
                actual_executions=[],
            )

        engine._execute_tool_call = fake_execute_tool_call
        engine._record_execution_bundle = lambda execution, call: (
            execution.result,
            "execution-1",
            [],
        )
        engine._emit_control_action_envelope = lambda tool, params: None
        engine._emit_control_tool_result = lambda **kwargs: None
        engine._apply_tool_execution_loop_effects = lambda execution: None
        engine._missing_required_test_attempt = lambda: None
        return engine

    return _build


def _execute(engine, tool_name, observation_text, params=None):
    """Run one ACTION step through the real dispatch path; return its observation."""
    engine.next_observation_text = observation_text
    step = ReActStep(
        step_type=StepType.ACTION,
        content=tool_name,
        tool_name=tool_name,
        tool_params=params or {},
        timestamp="scripted",
        tool_call_id=f"call_{len(engine.executed_calls) + 1}",
    )
    engine.steps.append(step)
    engine._execute_action_step(step)
    observations = [s for s in engine.steps if s.step_type == StepType.OBSERVATION]
    return observations[-1]


def test_build_words_in_a_bash_observation_do_not_trigger_the_probe(enrichment_engine):
    """The Java-artifact probe must not fire because bash output said "build"."""
    engine = enrichment_engine()

    observation = _execute(engine, "bash", "build success: 42 tests passed, compile clean")

    assert engine.probed == [], "a non-build tool must never reach the physical probe"
    assert "PHYSICAL EVIDENCE" not in observation.content


def test_a_build_tool_observation_triggers_the_probe_on_neutral_text(enrichment_engine):
    """Evidence, not wording: a build execution is enriched however it reads."""
    engine = enrichment_engine()

    observation = _execute(engine, "build", "the requested action finished")

    assert engine.probed == ["the requested action finished"]
    assert ENRICHMENT_MARK in observation.content


@pytest.mark.parametrize("tool_name", BUILD_FAMILY)
def test_every_build_family_tool_triggers_enrichment(enrichment_engine, tool_name):
    engine = enrichment_engine()

    observation = _execute(engine, tool_name, "the requested action finished")

    assert len(engine.probed) == 1, f"{tool_name} belongs to the build family"
    assert ENRICHMENT_MARK in observation.content


@pytest.mark.parametrize("tool_name", NON_BUILD_TOOLS)
def test_non_build_tools_never_trigger_enrichment(enrichment_engine, tool_name):
    engine = enrichment_engine()

    observation = _execute(engine, tool_name, "build failed to compile the maven test target")

    assert engine.probed == [], f"{tool_name} produces no physical build evidence"
    assert "PHYSICAL EVIDENCE" not in observation.content


def test_add_observation_step_takes_the_source_tool_explicitly(enrichment_engine):
    """The trigger is a parameter of the observation append, not a text scan."""
    engine = enrichment_engine()

    enriched = engine._add_observation_step("the requested action finished", source_tool="maven")
    assert ENRICHMENT_MARK in enriched.content

    engine.probed.clear()
    unenriched = engine._add_observation_step("build success: compile finished", source_tool="bash")
    assert engine.probed == []
    assert "PHYSICAL EVIDENCE" not in unenriched.content

    plain = engine._add_observation_step("build success: compile finished")
    assert engine.probed == [], "no source tool means no physical evidence to enrich with"
    assert "PHYSICAL EVIDENCE" not in plain.content


def test_the_probe_itself_no_longer_screens_the_observation_text(enrichment_engine):
    """The keyword scan is deleted, not merely bypassed: a gated probe would
    still starve a neutrally-worded build observation."""
    engine = enrichment_engine()

    assert engine._get_physical_validation_state("the requested action finished") == PHYSICAL_STATE
