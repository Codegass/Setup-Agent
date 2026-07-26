"""The advisor tool: a no-parameter client surface over a harness-side consult.

The model calls `advisor()`; the engine assembles the full phase transcript
plus a deterministic evidence digest and consults a fresh-context reviewer
(spec §3.2). Nothing about the consult is a model parameter — forwarding the
transcript is the harness's job, so the model cannot narrow, bias, or forget
what the reviewer sees.

The tool itself is inert: it owns no provider call and no state. Everything
that could fail lives behind `consult_fn`, which is
`ReActEngine.consult_advisor` and never raises.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from sag.evidence import OperationOutcome

from ..tools.base import BaseTool, ToolResult

ADVISOR_TOOL_DESCRIPTION = (
    "Consult a senior reviewer about strategy. Takes NO parameters: the harness "
    "forwards your entire phase transcript and the run's evidence digest "
    "automatically. Consult it before substantive work on a complex task, when "
    "you are stuck (a recurring error, an approach that is not converging), and "
    "before claiming a phase done or blocked after failures. The reviewer cannot "
    "call tools; it returns strategic guidance about what to do next and why."
)


class AdvisorTool(BaseTool):
    """`advisor()` — zero parameters, one delegated consult."""

    def __init__(self, consult_fn: Optional[Callable[[], ToolResult]] = None):
        super().__init__(name="advisor", description=ADVISOR_TOOL_DESCRIPTION)
        # Bound after the engine exists (the engine owns the consult); an
        # unbound advisor is a wiring bug and says so instead of pretending.
        self.consult_fn = consult_fn

    def get_parameter_schema(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    def get_usage_example(self) -> str:
        return "advisor()"

    def execute(self, **params: Any) -> ToolResult:
        if self.consult_fn is None:
            return ToolResult.completed(
                output=(
                    "advisor not wired: this run has no consult channel, so no advice "
                    "is available. Continue with the tools you have."
                ),
                operation_outcome=OperationOutcome.FAILED,
                error="advisor consult channel is not bound",
                error_code="ADVISOR_NOT_WIRED",
                metadata={"advisor": "unwired"},
            )
        return self.consult_fn()
