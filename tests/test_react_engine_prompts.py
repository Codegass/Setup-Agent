from sag.agent.react_engine import ReActEngine
from sag.agent.react_prompt_builder import ReActPromptBuilder
from sag.agent.react_types import ReactModelMode, ReActStep, StepType
from sag.config.prompt_loader import PromptConfig, load_react_engine_prompts
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
        return ToolResult(success=True, output="ok")

    def get_usage_example(self):
        return "dummy()"


def make_engine(repository_url=None, supports_function_calling=True):
    engine = ReActEngine.__new__(ReActEngine)
    engine.context_manager = DummyContextManager()
    engine.tools = {"dummy": DummyTool()}
    engine.repository_url = repository_url
    engine.supports_function_calling = supports_function_calling
    engine.prompts = load_react_engine_prompts()
    engine.prompt_builder = ReActPromptBuilder(
        prompts=engine.prompts,
        context_manager=engine.context_manager,
        tools=engine.tools,
    )
    engine.steps = []
    engine.successful_states = {
        "working_directory": None,
        "cloned_repos": set(),
        "project_type": None,
        "maven_success": False,
        "excluded_modules": set(),
        "excluded_tests": set(),
        "report_snapshot": None,
    }
    return engine


def test_react_engine_initialization_loads_prompt_config(monkeypatch):
    monkeypatch.setattr(ReActEngine, "_setup_litellm", lambda self: None)
    monkeypatch.setattr(ReActEngine, "_check_function_calling_support", lambda self: None)

    engine = ReActEngine(DummyContextManager(), [])

    assert isinstance(engine.prompts, PromptConfig)
    assert isinstance(engine.prompt_builder, ReActPromptBuilder)


def test_initial_system_prompt_preserves_core_markers_with_repository_url():
    engine = make_engine(repository_url="https://example.test/repo.git")

    prompt = engine.prompt_builder.build_initial_system_prompt(
        repository_url=engine.repository_url,
        tool_calling_enabled=engine.supports_function_calling,
    )

    assert "You are SAG (Setup-Agent)" in prompt
    assert "https://example.test/repo.git" in prompt
    assert "CRITICAL CONTEXT MANAGEMENT RULES" in prompt
    assert "AVAILABLE TOOLS" in prompt
    assert "dummy: Dummy tool for prompt tests" in prompt
    assert "Usage: dummy()" in prompt
    assert "Handling Maven POM Parsing Errors" in prompt
    assert "Handling Multi-Module Maven Test Execution" in prompt
    assert "RESPONSE FORMAT" in prompt
    assert "REMEMBER THE CONTINUOUS CYCLE" in prompt


def test_initial_system_prompt_uses_prompt_based_branch_when_function_calling_disabled():
    engine = make_engine(supports_function_calling=False)

    prompt = engine.prompt_builder.build_initial_system_prompt(
        repository_url=engine.repository_url,
        tool_calling_enabled=engine.supports_function_calling,
    )

    assert "Always respond in this exact format" in prompt
    assert "ACTION: [tool_name]" in prompt


def test_next_prompt_preserves_history_and_stuck_guidance():
    engine = make_engine(repository_url="https://example.test/repo.git")
    engine.steps = [
        ReActStep(step_type=StepType.THOUGHT, content="thought 1", timestamp="t1"),
        ReActStep(step_type=StepType.THOUGHT, content="thought 2", timestamp="t2"),
        ReActStep(step_type=StepType.THOUGHT, content="thought 3", timestamp="t3"),
    ]

    prompt = engine.prompt_builder.build_next_prompt(
        steps=engine.steps,
        repository_url=engine.repository_url,
        tool_calling_enabled=engine.supports_function_calling,
        successful_states=engine.successful_states,
    )

    assert "CONVERSATION HISTORY" in prompt
    assert "THOUGHT: thought 1" in prompt
    assert "IMPORTANT: You have been thinking without taking action" in prompt
    assert "https://example.test/repo.git" in prompt
    assert "Continue with your next THOUGHT and ACTION" in prompt


def test_mode_prompts_preserve_markers_and_base_prompt():
    engine = make_engine()

    thinking_prompt = engine.prompt_builder.build_mode_prompt(
        "base prompt", ReactModelMode.THINKING
    )
    action_prompt = engine.prompt_builder.build_mode_prompt("base prompt", ReactModelMode.ACTION)

    assert "THINKING MODEL INSTRUCTIONS" in thinking_prompt
    assert "CURRENT SITUATION TO ANALYZE" in thinking_prompt
    assert thinking_prompt.endswith("base prompt")
    assert "ACTION MODEL INSTRUCTIONS" in action_prompt
    assert "RESPONSE FORMAT (when function calling not supported)" in action_prompt
    assert action_prompt.endswith("base prompt")
