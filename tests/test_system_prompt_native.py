# tests/test_system_prompt_native.py
"""The system prompt teaches the tools and the phase workflow — not a protocol.

Under the native executor loop the conversation IS the protocol: assistant
tool_calls and their tool results are structured wire fields, rendered from
`self.steps`. Any surviving "reply with THOUGHT:/ACTION:/PARAMETERS:" or
CURRENT_PLAN instruction is now a lie that invites the model to emit prose
where a tool call belongs (Plan 2 Task 8)."""

from pathlib import Path

from sag.agent.react_prompt_builder import ReActPromptBuilder
from sag.config.prompt_loader import load_react_engine_prompts

YAML_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "sag"
    / "config"
    / "prompts"
    / "react_engine.yaml"
)

BANNED = (
    "THOUGHT:",
    "ACTION:",
    "PARAMETERS:",
    "CURRENT_PLAN",
    "THINKING MODE",
    "ACTION MODE",
    "SCHEDULER",
    "exact_params",
    "next THOUGHT",
)


class _CM:
    def get_current_context_info(self):
        return {"context_type": "trunk", "context_id": "trunk"}

    def load_trunk_context(self):
        return None


def _prompt(workflow_mode="setup"):
    builder = ReActPromptBuilder(
        prompts=load_react_engine_prompts(),
        context_manager=_CM(),
        tools={},
    )
    return builder.build_initial_system_prompt(
        repository_url="https://example.test/repo.git",
        repository_ref=None,
        workflow_mode=workflow_mode,
    )


def test_setup_prompt_has_no_text_protocol_instructions():
    prompt = _prompt("setup")
    offenders = [pattern for pattern in BANNED if pattern in prompt]
    assert offenders == [], f"setup system prompt still teaches the text protocol: {offenders}"


def test_run_task_prompt_has_no_text_protocol_instructions():
    prompt = _prompt("run_task")
    offenders = [pattern for pattern in BANNED if pattern in prompt]
    assert offenders == [], f"run-task system prompt still teaches the text protocol: {offenders}"


def test_prompt_yaml_carries_no_text_protocol_sections():
    text = YAML_PATH.read_text()
    offenders = [pattern for pattern in BANNED if pattern in text]
    assert offenders == [], f"react_engine.yaml still carries the text protocol: {offenders}"
    assert "mode_prompts:" not in text
    assert "next_prompt:" not in text


def test_setup_prompt_still_names_the_tools_and_the_phase_workflow():
    prompt = _prompt("setup")
    for tool in ("bash", "file_io", "search", "phase", "build", "project"):
        assert tool in prompt, f"system prompt no longer names the {tool} tool"
    assert "provision" in prompt and "report" in prompt, "phase order must stay visible"
    assert 'phase(action="done"' in prompt or "phase(action='done'" in prompt
    assert 'phase(action="blocked"' in prompt or "phase(action='blocked'" in prompt


def test_setup_prompt_names_the_advisor_and_when_to_consult_it():
    """The mechanical guarantees are a floor; the prompt is what lets the model
    consult BEFORE a redirect has to make it (spec §3.2)."""
    prompt = _prompt("setup")
    assert "advisor()" in prompt
    assert "when stuck" in prompt
    assert "before closing a phase" in prompt


def test_setup_prompt_states_that_tool_calls_are_how_work_happens():
    prompt = _prompt("setup")
    assert "USE THE TOOLS" in prompt
    assert "tool result" in prompt
