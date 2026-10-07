"""The advisor tool: an optional question over a harness-side consultation.

The model calls `advisor(question, context)` (both optional); the engine assembles the task, current facts and
a view of recorded evidence within the advisor's context budget, then consults
a fresh-context reviewer. Context selection is the harness's job: rank task
requirements and current blockers first, then automatically compress lower
priority material into labeled summaries or excerpts when space is tight.

The tool itself is inert: it owns no provider call and no state. Everything
that could fail lives behind `consult_fn`, which is
`ReActEngine.consult_advisor` and never raises.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from sag.evidence import OperationOutcome

from ..tools.base import BaseTool, ToolResult

ADVISOR_TOOL_DESCRIPTION = (
    "Consult a reviewer when a build/test diagnosis or next step is uncertain. "
    "Optionally give a short question and context (your hypothesis and intended next action). "
    "These are claims to check, not facts. The harness supplies the task, execution facts "
    "and evidence references independently. Depending on configuration, the reviewer can "
    "search and page through archived evidence; it cannot inspect unobserved live files, "
    "execute commands, modify the project, or certify success. Wait for relevant tool results "
    "before asking about them; do not pair this call with an unfinished build in the same batch. "
    "A normal phase transition or successful command alone does not require consultation."
)


class AdvisorTool(BaseTool):
    """A short optional question, one delegated consultation."""

    def __init__(self, consult_fn: Optional[Callable[..., ToolResult]] = None):
        super().__init__(name="advisor", description=ADVISOR_TOOL_DESCRIPTION)
        # Bound after the engine exists (the engine owns the consult); an
        # unbound advisor is a wiring bug and says so instead of pretending.
        self.consult_fn = consult_fn

    def get_parameter_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "maxLength": 1000,
                    "description": "The specific build/test decision or uncertainty to review; optional.",
                },
                "context": {
                    "type": "string",
                    "maxLength": 2000,
                    "description": "Optional short hypothesis and proposed next step. Unverified actor interpretation; do not restate whole logs.",
                },
            },
            "additionalProperties": False,
        }

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
        return self.consult_fn(**{k: v for k, v in params.items() if k in {"question", "context"}})
