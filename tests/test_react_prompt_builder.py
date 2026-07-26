from sag.agent.react_prompt_builder import ReActPromptBuilder
from sag.agent.react_types import ReactModelMode, ReActStep, StepType
from sag.config.prompt_loader import load_react_engine_prompts
from sag.tools.base import BaseTool, ToolResult


class DummyContextManager:
    contexts_dir = "/workspace/.setup_agent/contexts"
    orchestrator = None

    def get_current_context_info(self):
        return {
            "context_type": "trunk",
            "context_id": "trunk",
            "goal": "Set up the repository",
            "progress": "0/1",
            "next_task": "task_1",
        }

    def load_trunk_context(self):
        return None


class DummyTool(BaseTool):
    def __init__(self):
        super().__init__("dummy", "Dummy tool for prompt tests")

    def execute(self) -> ToolResult:
        return ToolResult.completed_success(output="ok")

    def get_usage_example(self):
        return "dummy()"


def make_builder():
    return ReActPromptBuilder(
        prompts=load_react_engine_prompts(),
        context_manager=DummyContextManager(),
        tools={"dummy": DummyTool()},
    )


def test_initial_prompt_preserves_repository_and_tool_markers():
    prompt = make_builder().build_initial_system_prompt(
        repository_url="https://example.test/repo.git",
        repository_ref=None,
    )

    assert "You are SAG (Setup-Agent)" in prompt
    assert "https://example.test/repo.git" in prompt
    assert "dummy: Dummy tool for prompt tests" in prompt
    assert "Usage: dummy()" in prompt
    assert "HOW YOU ACT" in prompt


def test_initial_prompt_explains_evidence_status_rules():
    prompt = make_builder().build_initial_system_prompt(
        repository_url="https://example.test/repo.git",
        repository_ref=None,
    )

    assert "done means the phase flow ended" in prompt
    assert "BUILD SUCCESS cannot override validator findings" in prompt
    assert "partial, conflict, or unknown" in prompt
    assert "read evidence refs or raw output refs" in prompt


def test_initial_prompt_includes_repository_ref_when_present():
    prompt = make_builder().build_initial_system_prompt(
        repository_url="https://example.test/repo.git",
        repository_ref="rel/commons-cli-1.11.0",
    )

    assert "Repository ref: rel/commons-cli-1.11.0" in prompt
    assert 'ref="rel/commons-cli-1.11.0"' in prompt


def test_initial_prompt_omits_repository_ref_when_absent():
    prompt = make_builder().build_initial_system_prompt(
        repository_url="https://example.test/repo.git",
        repository_ref=None,
    )

    assert "Repository ref:" not in prompt
    assert 'ref="' not in prompt


def test_system_prompt_requires_reading_evidence_refs_for_uncertain_states():
    prompt = make_builder().build_initial_system_prompt(
        repository_url="https://example.test/repo.git",
        repository_ref=None,
    )

    assert "partial, conflict, or unknown" in prompt
    assert "read evidence refs or raw output refs" in prompt


# Plan 2 Task 8: old protocol removed
