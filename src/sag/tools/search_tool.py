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

# `find` reports 0 when it walked everything it was given, >0 when some part of
# the walk failed — it says nothing about whether anything matched, so for the
# name form an empty stdout under exit 0 is the found-nothing answer.
_FIND_TRAVERSED = 0
_FIND_CAPPED_BY_HEAD = 141
# Shallow on purpose: build files sit at the root or one or two modules down,
# and an unbounded walk of a checked-out repo is the kind of command that hangs.
NAME_SEARCH_MAX_DEPTH = 4
_NAME_GLOB_SEPARATOR = "|"
# Metacharacters that mean something in an extended regex and are LITERAL in a
# name glob. `.` is excluded: it is literal in both and is in half the filenames
# anyone would look for.
_REGEX_ONLY_METACHARACTERS = "^$()+\\"


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
                "target: ref id (e.g. 'output_5b9a') | 'file:<path>' | 'name:<dir>' "
                "| 'job:<id>' | 'web:<query>'. "
                "'file:' greps file CONTENTS (a file, or a directory searched "
                "recursively): it matches text INSIDE files, so it cannot report "
                "whether a file exists. "
                "'name:' matches file and directory NAMES under <dir>, to depth "
                f"{NAME_SEARCH_MAX_DEPTH}, and never reads file content: this is the "
                "form that establishes whether a path such as gradlew or pom.xml is "
                "on disk. "
                "pattern: for 'file:' and ref ids an extended regular expression "
                "(grep -E). Set ignore_case=true for case-insensitive file-content "
                "searches instead of using PCRE-only inline flags such as (?i); "
                "for 'name:' one or more shell globs separated by "
                f"'{_NAME_GLOB_SEPARATOR}' (e.g. 'pom.xml|build.gradle|gradlew'), "
                "matched against the name alone; ignored for web; "
                "for a ref id, omit pattern to read the stored output itself."
            ),
        )
        self.docker_orchestrator = docker_orchestrator
        self.output_search = output_search
        self.web_search = web_search
        self.command_tracker = command_tracker

    def execute(
        self,
        target: str,
        pattern: str = "",
        max_results: int = 50,
        ignore_case: bool = False,
    ) -> ToolResult:
        target = (target or "").strip()
        if target.startswith("file:"):
            return self._grep_container(
                target[5:], pattern, max_results, ignore_case=ignore_case
            )
        if target.startswith("name:"):
            return self._find_by_name(target[5:], pattern, max_results)
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
                "Use 'file:/workspace/...' to grep the CONTENTS of a file in the container",
                "Use 'name:/workspace/...' to find a file by NAME under a directory",
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

    def _grep_container(
        self,
        path: str,
        pattern: str,
        max_results: int,
        *,
        ignore_case: bool = False,
    ) -> ToolResult:
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
        grep_flags = "E" + ("i" if ignore_case else "")
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
            f"grep -rn{grep_flags} -e {quoted_pattern} -- {quoted_path} | head -{limit}; "
            f"else grep -n{grep_flags} -e {quoted_pattern} -- {quoted_path} | head -{limit}; fi"
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
        if exit_code == _GREP_FOUND_NOTHING and diagnostic:
            return self._search_failed(path, pattern, exit_code, diagnostic)
        if exit_code == _GREP_FOUND_NOTHING:
            return ToolResult.completed_success(
                output=f"No matches for {pattern!r} in {path}",
                facts={"target": path, "pattern": pattern, "matched": False},
            )
        return self._search_failed(path, pattern, exit_code, diagnostic or stdout)

    def _find_by_name(self, path: str, pattern: str, max_results: int) -> ToolResult:
        """Find files by NAME under a directory — the question `file:` cannot answer.

        `file:` greps CONTENTS, so a filename it never sees written inside a
        file does not exist as far as it is concerned.  In D2 a model asked
        which build system a project used by grepping the project directory for
        `(^|/)build\\.gradle$|...|(^|/)gradlew$`, got a truthful "no matches",
        and concluded tapestry-5 had no build files while `build.gradle` and
        `gradlew` sat on disk.  Nothing on this tool could have answered it.

        Carried on the `target=` prefix rather than a separate `by_name=`
        parameter: the tool already routes on that prefix, so the forms stay
        mutually exclusive by construction — no `web:` query can be asked to
        match names, and the model has one thing to get right (which prefix)
        instead of two (prefix and flag).  The tool list stays the size it is,
        which is why a new registered tool was not an option either (#19).

        Bounded three ways, like `_grep_container`: a shallow `-maxdepth`, a
        `head` cap, and the transport timeout.  There is no `-exec`, so nothing
        here can outlive the walk.
        """

        limit = max(1, int(max_results))
        globs = [
            glob.strip() for glob in (pattern or "*").split(_NAME_GLOB_SEPARATOR) if glob.strip()
        ] or ["*"]
        # `-name` matches the NAME component only, which is the whole point;
        # alternation is spelled `-o` because a glob has no `|`.
        expression = " -o ".join(f"-name {shlex.quote(glob)}" for glob in globs)
        # Same pipeline discipline as the grep path: `head` is last, so without
        # `pipefail` the pipeline reports head's 0 and a failed walk reads as a
        # successful one — and for THIS form an exit 0 with no output is the
        # found-nothing verdict, so that mistake would manufacture exactly the
        # false "it is not there" this form exists to prevent.
        command = (
            "set -o pipefail; "
            f"find {shlex.quote(path)} -maxdepth {NAME_SEARCH_MAX_DEPTH} "
            f"{shlex.quote('(')} {expression} {shlex.quote(')')} -print "
            f"| head -{limit}"
        )
        result = self.docker_orchestrator.execute_command(command, workdir=None, timeout=60)

        exit_code = result.get("exit_code")
        stdout = self._stream(result, "stdout")
        diagnostic = self._stream(result, "stderr")
        suggestions = [
            f"Confirm the directory exists and is readable: {path}",
            "pattern is one or more shell globs separated by "
            f"'{_NAME_GLOB_SEPARATOR}' (e.g. 'pom.xml|build.gradle|gradlew'); "
            "use 'file:<path>' to search file CONTENTS instead",
        ]
        if command_did_not_run(result) or exit_code not in (
            _FIND_TRAVERSED,
            _FIND_CAPPED_BY_HEAD,
        ):
            return self._search_failed(
                path, pattern, exit_code, diagnostic or stdout, suggestions=suggestions
            )

        lines = [line for line in stdout.splitlines() if line.strip()][:limit]
        facts = {
            "target": path,
            "pattern": pattern,
            "matched": bool(lines),
            "max_depth": NAME_SEARCH_MAX_DEPTH,
        }
        if not lines:
            # The walk succeeded and it was BOUNDED: "not within this depth" is
            # the honest claim, "not in this tree" is not one this can make.
            return ToolResult.completed_success(
                output=(
                    f"No file or directory named {pattern!r} under {path} "
                    f"(searched to depth {NAME_SEARCH_MAX_DEPTH})"
                    + self._glob_vocabulary_note(pattern)
                ),
                facts=facts,
            )
        capped = len(lines) >= limit
        facts["capped_at_max_results"] = capped
        return ToolResult.completed_success(
            output="\n".join(lines)
            + (f"\n... [capped at {limit} results; more may exist]" if capped else ""),
            facts=facts,
        )

    @staticmethod
    def _glob_vocabulary_note(pattern: str) -> str:
        """Say which vocabulary just failed to match, when the shape suggests a regex.

        A regular expression handed to this form yields a TRUE no-match — no
        file is named `(^|/)gradlew$` — that answers a different question than
        the one asked.  Truthful and misleading is the failure mode this tool
        was fixed for once already; the verdict stands, the reason goes with it.
        """

        if not any(char in (pattern or "") for char in _REGEX_ONLY_METACHARACTERS):
            return ""
        return (
            "\n[pattern was matched as a shell glob against the name alone: "
            f"{' '.join(_REGEX_ONLY_METACHARACTERS)} are literal characters here, "
            "and a '/' can never match. 'file:<path>' is the form that takes an "
            "extended regular expression]"
        )

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
        suggestions: list[str] | None = None,
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
            suggestions=suggestions
            or [
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
                        "ref id "
                        "| file:<path> greps file CONTENTS (file, or directory searched "
                        "recursively) and cannot report whether a file exists "
                        f"| name:<dir> matches file and directory NAMES under <dir> to "
                        f"depth {NAME_SEARCH_MAX_DEPTH}, reading no content "
                        "| job:<id> | web:<query>"
                    ),
                },
                "pattern": {
                    "type": "string",
                    "description": (
                        "file:/ref id -> extended regular expression, grep -E; "
                        "use ignore_case=true instead of inline PCRE flags such as (?i); "
                        "name: -> shell globs separated by "
                        f"'{_NAME_GLOB_SEPARATOR}' (e.g. 'pom.xml|build.gradle|gradlew') "
                        "matched against the name alone; ignored for web"
                    ),
                },
                "ignore_case": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Case-insensitive matching for file: content searches; use this "
                        "instead of PCRE-only inline flags."
                    ),
                },
                "max_results": {"type": "integer", "default": 50},
            },
            "required": ["target"],
        }
