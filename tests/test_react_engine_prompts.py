from sag.agent.react_engine import ReActEngine
from sag.agent.react_llm import ReactLLMClient
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


class DummyTask:
    def __init__(self, task_id, description, status, key_results=""):
        self.id = task_id
        self.description = description
        self.status = status
        self.key_results = key_results


class DummyStatus:
    def __init__(self, value):
        self.value = value


class DummyTrunkContext:
    def __init__(self):
        self.todo_list = [
            DummyTask("task_1", "Clone repository", DummyStatus("completed"), "cloned"),
            DummyTask("task_2", "Compile project", DummyStatus("in_progress")),
        ]


class DummyContextManagerWithTodo(DummyContextManager):
    def load_trunk_context(self):
        return DummyTrunkContext()


class DummyTool(BaseTool):
    def __init__(self):
        super().__init__("dummy", "Dummy tool for prompt tests")

    def execute(self) -> ToolResult:
        return ToolResult.completed_success(output="ok")

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


def make_prompt_builder_with_todo():
    prompts = load_react_engine_prompts()
    return ReActPromptBuilder(
        prompts=prompts,
        context_manager=DummyContextManagerWithTodo(),
        tools={"dummy": DummyTool()},
    )


def test_react_engine_initialization_loads_prompt_config(monkeypatch):
    monkeypatch.setattr(ReactLLMClient, "setup", lambda self: None)

    engine = ReActEngine(DummyContextManager(), [])

    assert isinstance(engine.prompts, PromptConfig)
    assert isinstance(engine.prompt_builder, ReActPromptBuilder)
    assert isinstance(engine.llm_client, ReactLLMClient)


def test_initial_system_prompt_preserves_core_markers_with_repository_url():
    engine = make_engine(repository_url="https://example.test/repo.git")

    prompt = engine.prompt_builder.build_initial_system_prompt(
        repository_url=engine.repository_url,
    )

    assert "You are SAG (Setup-Agent)" in prompt
    assert "https://example.test/repo.git" in prompt
    assert "CRITICAL PHASE WORKFLOW RULES" in prompt
    assert "AVAILABLE TOOLS" in prompt
    assert "dummy: Dummy tool for prompt tests" in prompt
    assert "Usage: dummy()" in prompt
    assert "Handling Maven POM Parsing Errors" in prompt
    assert "Handling Multi-Module Maven Test Execution" in prompt
    assert "HOW YOU ACT" in prompt
    assert "REMEMBER THE PHASE CYCLE" in prompt


def test_initial_system_prompt_includes_env_overlay_runtime_guidance():
    engine = make_engine(repository_url="https://example.test/repo.git")

    prompt = engine.prompt_builder.build_initial_system_prompt(
        repository_url=engine.repository_url,
    )

    assert "Use bash to install missing runtimes" in prompt
    assert "project(action='env'" in prompt
    assert "env: Validate, register, and activate" in prompt
    assert "build, bash, validation, and report flows" in prompt
    assert "exact executable/version" in prompt
    assert "Do not use project(action='env') to rewrite project build configuration" in prompt
    assert "Runtime Recovery Guardrails" not in prompt


def test_initial_system_prompt_uses_run_task_contract_without_setup_workflow():
    engine = make_engine(repository_url="https://example.test/repo.git")

    prompt = engine.prompt_builder.build_initial_system_prompt(
        repository_url=engine.repository_url,
        workflow_mode="run_task",
    )

    assert "RUN TASK MODE" in prompt
    assert "TASK COMPLETE:" in prompt
    assert "BUILD SUCCESS cannot override validator findings" in prompt
    assert "partial, conflict, or unknown evidence" in prompt
    assert "read evidence refs or raw output refs" in prompt
    assert "Do not start, continue, or complete setup TODO tasks" in prompt
    assert "INTELLIGENT SETUP WORKFLOW" not in prompt
    assert "MANDATORY WORKFLOW FOR PROJECT SETUP" not in prompt
    assert "REMEMBER THE PHASE CYCLE" not in prompt
    assert "phase(action=" not in prompt
    assert "first action should be to clone" not in prompt


# Plan 2 Task 8: old protocol removed
