"""Unified retrieval tool: search refs, container files, job logs, the web.

The envelope's `refs` are handles for THIS tool ("links, not dumps").
Stage 1 delegates ref/web searches to the existing OutputSearchTool /
WebSearchTool internals; file/job targets grep inside the container.
"""

import shlex
from typing import Any, Dict, Mapping

from sag.evidence import EvidenceStatus, InvocationStatus, OperationOutcome
from sag.runtime.container_io import command_did_not_run

from .base import BaseTool, ToolResult
from .internal.build_utils import classify_detached_completion

SEARCH_FAILED = "SEARCH_FAILED"

# grep's own vocabulary: 0 = matched, 1 = looked and found nothing, 2 = the
# search itself failed (unreadable path, invalid regex, ...).
_GREP_MATCHED = 0
_GREP_FOUND_NOTHING = 1
# 128 + SIGPIPE(13): `head` closed the pipe after max_results lines and grep
# died writing into it. That is a CAPPED read, not a failed one.
_GREP_CAPPED_BY_HEAD = 141
_DIAGNOSTIC_QUOTE_LIMIT = 400


class SearchTool(BaseTool):
    def __init__(
        self,
        docker_orchestrator,
        output_search=None,
        web_search=None,
        command_tracker=None,
    ):
        super().__init__(
            name="search",
            description=(
                "Search stored outputs, files, background-job logs, or the web. "
                "target: ref id (e.g. 'output_5b9a') | 'file:<path>' | 'job:<id>' | 'web:<query>'. "
                "A 'file:' path may be a file or a directory (searched recursively). "
                "pattern: extended regular expression, grep -E (ignored for web); "
                "for a ref id, omit pattern to read the stored output itself."
            ),
        )
        self.docker_orchestrator = docker_orchestrator
        self.output_search = output_search
        self.web_search = web_search
        self.command_tracker = command_tracker

    def execute(self, target: str, pattern: str = "", max_results: int = 50) -> ToolResult:
        target = (target or "").strip()
        if target.startswith("file:"):
            return self._grep_container(target[5:], pattern, max_results)
        if target.startswith("job:"):
            return self._poll_job(target[4:])
        if target.startswith("web:"):
            return self._web(target[4:], max_results)
        if target.startswith("output_") and self.output_search is not None:
            if not pattern:
                # No pattern = read the stored output (auto-truncated by the
                # delegate). Grep-with-a-guessed-pattern must not be the only
                # reachable action: guessing what to look for in a log it has
                # never seen is exactly what a weak model cannot do (#30).
                return self.output_search.execute(action="retrieve", ref_id=target)
            return self.output_search.execute(
                action="grep", ref_id=target, grep_pattern=pattern, limit=max_results
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
            runner = None
            try:
                from sag.agent.job_obligations import read_obligations

                runner = next(
                    (
                        str(record.get("tool") or "")
                        for record in (read_obligations(self.docker_orchestrator) or ())
                        if str(record.get("job_id") or "") == job_id
                    ),
                    None,
                )
            except Exception:
                # The exit status remains usable when historical jobs predate
                # the obligation ledger; runner identity is then honestly
                # unknown and the generic high-confidence fallback applies.
                runner = None
            result = classify_detached_completion(
                completed.get("exit_code"),
                str(completed.get("output") or tail),
                runner=runner,
                full_output=full_output,
                poll_ref=poll_ref,
                invocation_status=(
                    InvocationStatus.CRASHED
                    if poll.get("state") == "vanished"
                    else InvocationStatus.COMPLETED
                ),
                terminal_observation=True,
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
            update_receipt = getattr(
                self.command_tracker,
                "update_execution_receipt",
                None,
            )
            if callable(update_receipt):
                update_receipt(
                    poll_ref,
                    invocation_status=result.invocation_status.value,
                    dispatch_status="completed_detached",
                    exit_code=completed.get("exit_code"),
                    operation_outcome=result.operation_outcome.value,
                    lifecycle_state=poll.get("state"),
                )
            return result

        return classify_detached_completion(
            None,
            tail or "Detached process state could not be established.",
            poll_ref=poll_ref,
        )

    def _grep_container(self, path: str, pattern: str, max_results: int) -> ToolResult:
        """Grep inside the container, keeping "did not run" apart from "found nothing".

        A search that did not succeed is not a search that found nothing
        (a41109d).  Three things made this tool state the opposite: stderr went
        to /dev/null, grep's exit code was never read, and every answer came
        back as `completed_success`.  A directory target, an absent path and an
        invalid regex therefore all arrived at the model as a confident "this
        does not exist" — tapestry-5 was told it had no build files while
        `build.gradle` and `gradlew` sat on disk.
        """

        limit = max(1, int(max_results))
        quoted_path = shlex.quote(path)
        # Extended regex: plain grep is BRE, where `|` is a literal character,
        # so an alternation like `(^|/)build\.gradle$|(^|/)gradlew$` can never
        # match anything.
        quoted_pattern = shlex.quote(pattern or ".")
        # `head` is the LAST command in the pipeline, so an unguarded `$?` is
        # HEAD's status: an erroring grep reads as exit 0. `pipefail` hands the
        # pipeline grep's status instead, which is the only way exit 2 (the
        # search failed) stays distinguishable from exit 1 (the search looked).
        # A directory is searched recursively rather than answered with
        # "Is a directory", and stderr is kept because those diagnostics ARE
        # the answer whenever the search did not run.
        command = (
            "set -o pipefail; "
            f"if test -d {quoted_path}; then "
            f"grep -rnE -e {quoted_pattern} -- {quoted_path} | head -{limit}; "
            f"else grep -nE -e {quoted_pattern} -- {quoted_path} | head -{limit}; fi"
        )
        result = self.docker_orchestrator.execute_command(command, workdir=None, timeout=60)

        exit_code = result.get("exit_code")
        stdout = self._stream(result, "stdout")
        diagnostic = self._stream(result, "stderr")
        if command_did_not_run(result):
            return self._search_failed(path, pattern, exit_code, diagnostic or stdout)
        if exit_code in (_GREP_MATCHED, _GREP_CAPPED_BY_HEAD):
            lines = stdout.splitlines()[:limit]
            capped = len(lines) >= limit
            return ToolResult.completed_success(
                output="\n".join(lines)
                + (f"\n... [capped at {limit} results; more may exist]" if capped else ""),
                facts={
                    "target": path,
                    "pattern": pattern,
                    "matched": True,
                    "capped_at_max_results": capped,
                },
            )
        if exit_code == _GREP_FOUND_NOTHING:
            return ToolResult.completed_success(
                output=f"No matches for {pattern!r} in {path}",
                facts={"target": path, "pattern": pattern, "matched": False},
            )
        return self._search_failed(path, pattern, exit_code, diagnostic or stdout)

    @staticmethod
    def _stream(result: Any, name: str) -> str:
        """Read one demuxed stream, falling back to the combined output.

        `DockerOrchestrator` returns stdout and stderr separately and also
        concatenates them into `output`; smaller transports return `output`
        alone, in which case it is the only text there is.
        """

        if isinstance(result, Mapping) and name in result:
            return str(result.get(name) or "").strip()
        return str((result or {}).get("output") or "").strip()

    def _search_failed(
        self,
        path: str,
        pattern: str,
        exit_code: Any,
        diagnostic: str,
    ) -> ToolResult:
        """The search itself failed: say what failed, and claim nothing about matches."""

        quoted = diagnostic[:_DIAGNOSTIC_QUOTE_LIMIT] or "no diagnostic was captured"
        reason = f"search failed in {path} (exit {exit_code}): {quoted}"
        return ToolResult.completed_failure(
            output=(
                f"Search for {pattern!r} in {path} FAILED (exit {exit_code}). "
                f"This is NOT a 'no matches' answer — nothing was established "
                f"about {pattern!r}.\n{quoted}"
            ),
            error=reason,
            error_code=SEARCH_FAILED,
            # No `matched` verdict exists: the search never produced one.
            facts={"target": path, "pattern": pattern, "matched": None},
            suggestions=[
                f"Confirm the path exists (a directory target is searched recursively): {path}",
                "pattern is an extended regular expression: escape ( ) | + ? { } "
                "to match them literally",
            ],
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
                    "description": (
                        "ref id | file:<path> (file or directory) | job:<id> | web:<query>"
                    ),
                },
                "pattern": {
                    "type": "string",
                    "description": "extended regular expression, grep -E (ignored for web)",
                },
                "max_results": {"type": "integer", "default": 50},
            },
            "required": ["target"],
        }
