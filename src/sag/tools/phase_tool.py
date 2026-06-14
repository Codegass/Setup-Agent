# src/sag/tools/phase_tool.py
"""phase(action: done | blocked | note) — the model's lifecycle surface.

`done` is a CLAIM, validated by the phase gate; `blocked` is the
always-accepted escape valve (recorded honestly, verdict degrades);
`note` is a free-form record. The ENGINE advances the machine when it
sees metadata.phase_signal — the tool never mutates phase state."""

from typing import Any, Dict, List, Optional

from sag.agent.phase_gates import check_phase_done

from .base import BaseTool, ToolResult


class PhaseTool(BaseTool):
    def __init__(self, machine, validator, orchestrator, project_name, gate_fn=check_phase_done):
        super().__init__(
            name="phase",
            description=(
                "Phase lifecycle: action='done' (key_results, evidence refs) claims the current "
                "phase is finished — it is checked against physical evidence; action='blocked' "
                "(reason, evidence) honestly records the phase cannot finish — always accepted; "
                "action='note' (text) records a working note. The engine advances phases; "
                "you never pick or reorder them."
            ),
        )
        self.machine = machine
        self.validator = validator
        self.orchestrator = orchestrator
        self.project_name = project_name
        self.gate_fn = gate_fn

    def execute(
        self,
        action: str,
        key_results: str = "",
        reason: str = "",
        evidence: Optional[List[str]] = None,
        text: str = "",
    ) -> ToolResult:
        if self.machine.is_complete:
            return ToolResult(
                success=False,
                verdict="failed",
                output="All phases already complete.",
                error="machine complete",
            )
        verb = (action or "").strip().lower()
        phase = self.machine.current_phase

        if verb == "note":
            if not text:
                return ToolResult(
                    success=False,
                    verdict="failed",
                    output="note requires text",
                    error="missing text",
                )
            return ToolResult(
                success=True,
                output=f"Noted ({phase}): {text}",
                facts={"phase": phase},
                metadata={"phase_signal": "note", "text": text},
            )

        if verb == "blocked":
            if not (reason or "").strip():
                return ToolResult(
                    success=False,
                    verdict="failed",
                    output="blocked requires a concrete reason (and evidence refs if available)",
                    error="missing reason",
                )
            return ToolResult(
                success=True,
                output=f"Phase '{phase}' recorded as BLOCKED: {reason}. The run will reflect "
                "this honestly; moving to the next phase.",
                verdict="partial",
                facts={"phase": phase},
                metadata={
                    "phase_signal": "blocked",
                    "reason": reason,
                    "evidence": list(evidence or []),
                },
            )

        if verb == "done":
            gate = self.gate_fn(phase, self.validator, self.orchestrator, self.project_name)
            if not gate["ok"]:
                return ToolResult(
                    success=False,
                    verdict="failed",
                    output=f"Phase '{phase}' done-claim rejected: {gate['reason']}",
                    error=gate["reason"],
                    suggestions=list(gate["suggestions"])
                    + [
                        "If the phase truly cannot finish, use phase(action='blocked', "
                        "reason=..., evidence=[...]) — that is always accepted and recorded honestly",
                    ],
                )
            return ToolResult(
                success=True,
                output=f"Phase '{phase}' complete. Advancing.",
                facts={"phase": phase},
                metadata={
                    "phase_signal": "done",
                    "key_results": key_results,
                    "evidence": list(evidence or []),
                },
            )

        return ToolResult(
            success=False,
            verdict="failed",
            output=f"Unknown phase action: {action!r}",
            error="invalid action",
            suggestions=["Use action= done | blocked | note"],
        )

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["done", "blocked", "note"]},
                "key_results": {
                    "type": "string",
                    "description": "done: lasting record of this phase (facts, versions, paths)",
                },
                "reason": {"type": "string", "description": "blocked: why the phase cannot finish"},
                "evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "refs supporting the claim (output_*, job:*, file:*)",
                },
                "text": {"type": "string", "description": "note: working note"},
            },
            "required": ["action"],
        }
