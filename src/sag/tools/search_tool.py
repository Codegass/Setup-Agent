"""Unified retrieval tool: search refs, container files, job logs, the web.

The envelope's `refs` are handles for THIS tool ("links, not dumps").
Stage 1 delegates ref/web searches to the existing OutputSearchTool /
WebSearchTool internals; file/job targets grep inside the container.
"""

import shlex
from typing import Any, Dict

from .base import BaseTool, ToolResult


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
            return self._grep_container(f"/tmp/sag_jobs/{target[4:]}.log", pattern, max_results)
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
