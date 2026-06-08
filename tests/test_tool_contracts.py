from types import SimpleNamespace
from typing import Optional

from sag.agent.react_llm import ReactLLMClient
from sag.agent.react_types import ReactModelMode
from sag.config.models import LogLevel
from sag.config.settings import Config
from sag.tools.base import BaseTool, ToolResult
from sag.tools.bash import BashTool
from sag.tools.context_tool import ContextTool
from sag.tools.report_tool import ReportTool


class ExampleTool(BaseTool):
    def __init__(self):
        super().__init__("example", "Example tool")

    def execute(self, command: str) -> ToolResult:
        return ToolResult(success=True, output=command)


class OptionalIntTool(BaseTool):
    def __init__(self):
        super().__init__("optional_int", "Optional integer tool")

    def execute(self, end_line: Optional[int] = None) -> ToolResult:
        return ToolResult(success=True, output=str(end_line))


class DerivedReportTool(ReportTool):
    pass


class FakeTokenTracker:
    def track_token_usage(self, response, model, step_type):
        pass


def make_llm_client(tools):
    config = Config(
        action_model="gpt-4o",
        action_provider="openai",
        log_level=LogLevel.INFO,
    )
    return ReactLLMClient(
        config=config,
        tools={tool.name: tool for tool in tools},
        token_tracker=FakeTokenTracker(),
    )


def test_base_tool_exposes_public_parameter_schema():
    tool = ExampleTool()
    schema = tool.get_parameter_schema()

    assert schema["type"] == "object"
    assert "command" in schema["properties"]
    assert schema["required"] == ["command"]


def test_base_tool_infers_optional_integer_schema():
    schema = OptionalIntTool().get_parameter_schema()

    assert schema["properties"]["end_line"]["type"] == "integer"
    assert schema["properties"]["end_line"]["default"] is None


def test_react_llm_client_uses_public_tool_schema_without_empty_fallback():
    client = make_llm_client([ExampleTool()])

    schema = client.build_tools_schema(ReactModelMode.ACTION)

    assert schema[0]["function"]["name"] == "example"
    assert "command" in schema[0]["function"]["parameters"]["properties"]


def test_real_tool_custom_schema_is_preserved():
    schema = ReportTool().get_parameter_schema()

    assert schema["type"] == "object"
    assert schema["properties"]["action"]["enum"] == ["generate"]
    assert "status" in schema["properties"]
    assert "details" in schema["properties"]


def test_bash_tool_schema_exposes_timeout_for_agent_use():
    schema = BashTool().get_parameter_schema()

    assert schema["properties"]["timeout"]["type"] == "integer"
    assert schema["properties"]["timeout"]["default"] == 60
    assert "maximum total execution time" in schema["properties"]["timeout"]["description"].lower()


def test_react_llm_client_preserves_bash_timeout_in_tool_schema():
    client = make_llm_client([BashTool()])

    schema = client.build_tools_schema(ReactModelMode.ACTION)

    timeout_schema = schema[0]["function"]["parameters"]["properties"]["timeout"]
    assert timeout_schema["type"] == "integer"
    assert timeout_schema["default"] == 60


def test_inherited_custom_schema_is_preserved():
    schema = DerivedReportTool().get_parameter_schema()

    assert schema["properties"]["action"]["enum"] == ["generate"]
    assert "status" in schema["properties"]
    assert "details" in schema["properties"]


def test_context_tool_schema_exposes_force_and_evidence_fields():
    schema = ContextTool(SimpleNamespace()).get_parameter_schema()
    properties = schema["properties"]

    assert "force" in properties
    assert properties["force"]["type"] == "boolean"
    assert properties["force"]["default"] is False
    assert "evidence_refs" in properties
    assert "evidence_status" in properties
    assert "conflicts" in properties


def test_context_tool_schema_keeps_action_specific_required_semantics():
    schema = ContextTool(SimpleNamespace()).get_parameter_schema()
    properties = schema["properties"]

    assert schema["required"] == ["action"]
    assert "summary" not in schema["required"]
    assert "key_results" not in schema["required"]
    assert "required for complete_task" in properties["summary"]["description"]
    assert "required for complete_with_results" in properties["key_results"]["description"]
