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
    # Tool capabilities are carried by the structured function schemas.  The
    # setup system prompt must not duplicate free-form descriptions/examples,
    # which can silently select a call or an ordering before evidence exists.
    assert "dummy: Dummy tool for prompt tests" not in prompt
    assert "Usage: dummy()" not in prompt
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


def test_compact_setup_prompt_removes_repetition_and_preserves_operational_contract():
    builder = make_builder()
    kwargs = {"repository_url": "https://example.test/repo.git", "repository_ref": "abc123"}
    full = builder.build_initial_system_prompt(**kwargs)
    compact = builder.build_initial_system_prompt(**kwargs, compact_setup_prompt=True)
    assert len(compact) < len(full)
    for required in (
        "Repository ref: abc123",
        "CURRENT phase",
        "BUILD SUCCESS cannot override validator findings",
        "partial, conflict, or unknown",
        "evidence refs or raw output refs",
        "raw shell",
        "Do not add skips",
        "actual receipt and reports",
        "unresolved requirements",
        "native toolchains",
        "search",
        "env",
        "report",
        "phase(action=",
    ):
        assert required in compact
    assert compact.count("Repository URL: https://example.test/repo.git") == 1
    assert builder.build_initial_system_prompt(
        **kwargs, workflow_mode="run_task"
    ) == builder.build_initial_system_prompt(
        **kwargs, workflow_mode="run_task", compact_setup_prompt=True
    )


def test_initial_prompt_includes_repository_ref_when_present():
    prompt = make_builder().build_initial_system_prompt(
        repository_url="https://example.test/repo.git",
        repository_ref="rel/commons-cli-1.11.0",
    )

    assert "Repository ref: rel/commons-cli-1.11.0" in prompt


def test_compact_setup_retains_meaningful_context_values():
    builder = make_builder()
    prompt = builder.build_initial_system_prompt(
        repository_url="https://example.test/repo.git", compact_setup_prompt=True
    )
    assert "Goal: Set up the repository" in prompt
    assert "Progress: 0/1" in prompt
    assert "Next Task: task_1" in prompt
    assert "Context ID:" not in prompt


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
