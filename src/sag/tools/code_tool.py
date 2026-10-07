"""Optional orchestration tool; all child execution stays in ReActEngine."""
from typing import Any, Callable

from .base import BaseTool, ToolResult


class CodeTool(BaseTool):
    def __init__(self, execute_program: Callable[..., ToolResult] | None = None):
        super().__init__("code", (
            "Run a short JavaScript program to compose existing build/test tools. "
            "Use await tools.NAME({...}) with the SAME parameters shown in each tool's schema. "
            "Child calls run serially even with Promise.all. Prefer native calls for simple actions; "
            "code is useful when a later read depends on an earlier result, or to select relevant fields. "
            "Each result has text, invocation_status, operation_outcome, error_code, poll_ref, "
            "output_ref, output_path, page, facts and receipt_view. Null means unavailable. "
            "text is the supplied read page or tool preview, not necessarily the whole file; "
            "use page.next or the full ref/path to continue. JSON.parse(text) requires a complete JSON page. "
            "Use text(value) to show selected results; only these selections plus the harness's "
            "status summary reach the next model turn. Full child results remain archived. "
            "Ordinary tool failures return their real status so you can inspect or branch; "
            "successful script execution never proves build/test success. "
            "phase, report, advisor, manage_context and nested code are unavailable inside code. "
            "Use those as separate native calls. A pending job, budget limit, cancellation or "
            "phase closure ends the program and stops remaining calls, even inside try/catch. "
            "Already executed effects are retained. Do not rerun the whole program to retry one failed action. "
            "Await every child call. Variables last only for this program; no Node, filesystem, "
            "network or process APIs are exposed. Use the existing tools to access the container. "
            "The same active repair_intent requirements apply to child calls. "
            "In a phase execution plan, name the real child tool and parameters, not this wrapper."
        ))
        self.execute_program = execute_program

    def get_parameter_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"program": {
            "type": "string", "minLength": 1, "maxLength": 16000,
            "description": "JavaScript body. Call await tools.NAME(params), inspect real status, and text only the evidence needed next.",
        }}, "required": ["program"], "additionalProperties": False}

    def get_usage_example(self) -> str:
        return 'code(program=\'const r = await tools.file_io({action:"read",path:"/workspace/project/pom.xml"}); text(r.text);\')'

    def execute(self, program: str) -> ToolResult:
        if not isinstance(program, str) or not 1 <= len(program) <= 16000:
            return ToolResult.completed_failure(output="", error="program must contain 1 to 16000 characters", error_code="CODE_PARAMETERS_INVALID")
        if self.execute_program is None:
            return ToolResult.completed_failure(output="", error="code runtime is not bound; use native tools", error_code="CODE_NOT_WIRED")
        return self.execute_program(program)
