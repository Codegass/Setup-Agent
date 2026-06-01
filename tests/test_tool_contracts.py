from sag.tools.base import BaseTool, ToolResult
from sag.agent.react_engine import ReActEngine
from sag.tools.report_tool import ReportTool


class ExampleTool(BaseTool):
    def __init__(self):
        super().__init__("example", "Example tool")

    def execute(self, command: str) -> ToolResult:
        return ToolResult(success=True, output=command)


def test_base_tool_exposes_public_parameter_schema():
    tool = ExampleTool()
    schema = tool.get_parameter_schema()

    assert schema["type"] == "object"
    assert "command" in schema["properties"]
    assert schema["required"] == ["command"]


def test_react_engine_uses_public_tool_schema_without_empty_fallback():
    tool = ExampleTool()
    engine = ReActEngine.__new__(ReActEngine)
    engine.tools = {"example": tool}
    engine.is_claude_model = False

    schema = ReActEngine._build_tools_schema(engine)

    assert schema[0]["function"]["name"] == "example"
    assert "command" in schema[0]["function"]["parameters"]["properties"]


def test_real_tool_custom_schema_is_preserved():
    schema = ReportTool().get_parameter_schema()

    assert schema["type"] == "object"
    assert schema["properties"]["action"]["enum"] == ["generate"]
    assert "status" in schema["properties"]
    assert "details" in schema["properties"]
