"""Unified retrieval tool: search refs, container files, job logs, the web.

The envelope's `refs` are handles for THIS tool ("links, not dumps").
Stage 1 delegates ref/web searches to the existing OutputSearchTool /
WebSearchTool internals; file/job targets grep inside the container.
"""

import shlex
from typing import Any, Dict

from sag.evidence import EvidenceStatus, InvocationStatus, OperationOutcome

from .base import BaseTool, ToolResult
from .internal.build_utils import classify_detached_completion


class SearchTool(BaseTool):
    def __init__(self, docker_orchestrator, output_search=None, web_search=None):
        super().__init__(
            name="search",
            description=(
                "Search stored outputs, files, background-job logs, or the web. "
                "target: ref id (e.g. 'output_5b9a') | 'file:<path>' | 'job:<id>' | 'web:<query>'. "
                "pattern: grep pattern (ignored for web)."
            ),
        )
        self.docker_orchestrator = docker_orchestrator
        self.output_search = output_search
        self.web_search = web_search

    def execute(self, target: str, pattern: str = "", max_results: int = 50) -> ToolResult:
        target = (target or "").strip()
        if target.startswith("file:"):
            return self._grep_container(target[5:], pattern, max_results)
        if target.startswith("job:"):
            return self._poll_job(target[4:])
        if target.startswith("web:"):
            return self._web(target[4:], max_results)
        if target.startswith("output_") and self.output_search is not None:
            return self.output_search.execute(
                action="grep", ref_id=target, grep_pattern=pattern or ".", limit=max_results
            )
        return ToolResult.completed_failure(
            output=f"Unrecognized search target: {target!r}",
            error="unknown target",
            suggestions=[
                "Use a ref id from a tool result (e.g. 'output_5b9a')",
                "Use 'file:/workspace/...' to grep a file in the container",
                "Use 'job:<id>' to grep a background job log",
                "Use 'web:<query>' for a web search",
            ],
        )

    def _poll_job(self, job_id: str) -> ToolResult:
        poll_ref = f"job:{job_id}"
        try:
            handle = self.docker_orchestrator.detached_handle(job_id)
        except ValueError as exc:
            return ToolResult.completed_failure(
                output=f"Invalid background job reference: {poll_ref}",
                error=str(exc),
                error_code="INVALID_DETACHED_JOB_REF",
            )

        poll = self.docker_orchestrator.poll_detached_command(handle, tail_lines=50)
        tail = str(poll.get("tail") or "")
        if poll.get("running") or poll.get("state") == "running":
            return ToolResult(
                invocation_status=InvocationStatus.PENDING,
                operation_outcome=OperationOutcome.UNKNOWN,
                evidence_status=EvidenceStatus.UNKNOWN,
                poll_ref=poll_ref,
                output=tail or "Background operation is still running.",
                refs=[poll_ref],
                metadata={
                    "dispatch_status": "running_detached",
                    "job_id": job_id,
                    "log_path": handle["log_path"],
                    "log_size": poll.get("log_size", 0),
                },
            )

        if poll.get("finished") or poll.get("state") in {"finished", "vanished"}:
            completed = self.docker_orchestrator.collect_detached_result(handle, poll)
            full_output = str(completed.get("full_output") or completed.get("output") or tail)
            result = classify_detached_completion(
                completed.get("exit_code"),
                str(completed.get("output") or tail),
                full_output=full_output,
                poll_ref=poll_ref,
                invocation_status=(
                    InvocationStatus.CRASHED
                    if poll.get("state") == "vanished"
                    else InvocationStatus.COMPLETED
                ),
            )
            result.metadata.update(
                {
                    "dispatch_status": "completed_detached",
                    "job_id": job_id,
                    "log_path": handle["log_path"],
                    "log_size": poll.get("log_size", 0),
                }
            )
            result.refs.append(poll_ref)
            return result

        return classify_detached_completion(
            None,
            tail or "Detached process state could not be established.",
            poll_ref=poll_ref,
        )

    def _grep_container(self, path: str, pattern: str, max_results: int) -> ToolResult:
        cmd = (
            f"grep -n {shlex.quote(pattern or '.')} {shlex.quote(path)} 2>/dev/null "
            f"| head -{int(max_results)}"
        )
        result = self.docker_orchestrator.execute_command(cmd, workdir=None, timeout=60)
        output = (result.get("output") or "").strip()
        matched = bool(output)
        return ToolResult.completed_success(
            output=output if matched else f"No matches for {pattern!r} in {path}",
            facts={"target": path, "pattern": pattern, "matched": matched},
        )

    def _web(self, query: str, max_results: int) -> ToolResult:
        if self.web_search is None:
            return ToolResult.completed_failure(
                output="web search unavailable",
                error="web search unavailable",
            )
        return self.web_search.execute(query=query, max_results=min(max_results, 5))

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "ref id | file:<path> | job:<id> | web:<query>",
                },
                "pattern": {"type": "string", "description": "grep pattern (ignored for web)"},
                "max_results": {"type": "integer", "default": 50},
            },
            "required": ["target"],
        }
