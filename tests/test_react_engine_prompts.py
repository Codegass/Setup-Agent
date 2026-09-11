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
    assert "dummy: Dummy tool for prompt tests" not in prompt
    assert "Usage: dummy()" not in prompt
    assert "engine mechanically inventories checkout files and creates a fact sheet" in prompt
    assert "broad evidence and gap-checking inputs" in prompt
    assert "not a harness-authored project plan" in prompt
    assert "structured execution_plan is optional" in prompt
    assert "no fixed post-clone action sequence is required" in prompt
    assert "Handling Maven POM Parsing Errors" not in prompt
    assert "Handling Multi-Module Maven Test Execution" not in prompt
    assert "HOW YOU ACT" in prompt
    assert "REMEMBER THE PHASE CYCLE" in prompt


def test_initial_system_prompt_describes_runtime_capabilities_without_selecting_calls():
    engine = make_engine(repository_url="https://example.test/repo.git")

    prompt = engine.prompt_builder.build_initial_system_prompt(
        repository_url=engine.repository_url,
    )

    assert "Use bash to install missing runtimes" not in prompt
    assert "project(action='env'" not in prompt
    assert "env: Runtime validation, registration, and activation" in prompt
    assert "build, bash, validation, and report flows" in prompt
    assert "does not rewrite project build configuration" in prompt
    assert "Runtime Recovery Guardrails" not in prompt


def test_real_setup_tool_bundle_has_no_pre_evidence_routing_or_usage_examples():
    """Exercise actual production tool classes, not a tools={} prompt.

    Tool descriptions remain available in the structured schemas; the initial
    setup prompt carries lifecycle syntax and factual capability boundaries,
    never examples or prose that chooses a tool/order for the model.
    """
    from types import SimpleNamespace

    from sag.agent.advisor import AdvisorTool
    from sag.tools.bash import BashTool
    from sag.tools.build.build_tool import BuildTool
    from sag.tools.file_io import FileIOTool
    from sag.tools.phase_tool import PhaseTool
    from sag.tools.project_tool import ProjectTool
    from sag.tools.report_tool import ReportTool
    from sag.tools.search_tool import SearchTool

    machine = SimpleNamespace(
        current_phase="provision",
        current_attempt_id="provision-1",
        is_complete=False,
    )
    tools = [
        BashTool(None),
        FileIOTool(None),
        PhaseTool(machine, None, None, "demo"),
        BuildTool(None),
        ProjectTool(),
        SearchTool(None),
        ReportTool(None, workflow_mode="setup"),
        AdvisorTool(),
    ]
    builder = ReActPromptBuilder(
        prompts=load_react_engine_prompts(),
        context_manager=DummyContextManager(),
        tools={tool.name: tool for tool in tools},
    )

    prompt = builder.build_initial_system_prompt(
        repository_url="https://example.test/repo.git",
        workflow_mode="setup",
    )

    forbidden = (
        "PREFERRED",
        "USE THIS",
        "Use bash to install",
        "use build(action=...) instead",
        "npm install",
        "Use this tool when all main tasks are finished",
        "Usage:",
    )
    assert [text for text in forbidden if text in prompt] == []
    assert 'command="<complete runner command>"' in prompt
    assert "on-demand diagnostic, not a routine prerequisite" in prompt
    assert "Valid actions: done, blocked, note" in prompt
    assert "Project build runner dispatches are recorded only by the build facade" in prompt


def test_build_schema_description_is_a_factual_boundary_not_a_router():
    from sag.tools.build.build_tool import BuildTool

    description = BuildTool(None).description

    assert "pass command and working_directory once" in description
    assert "deps is an on-demand diagnostic, not a routine prerequisite" in description
    assert "durable invocation receipt" in description
    assert "bash mvn/gradle" not in description
    assert "wrong version" not in description


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
