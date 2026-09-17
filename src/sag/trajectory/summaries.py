"""One line for what a call asked, one for what it answered.

Pure functions over the payload dicts the ledger already carries. They add no
facts: every value here is copied out of an `action_envelope`'s exact params or
a `tool_result`'s projected result. Where a shape is not recognised the answer
is `None`, so a reader is never shown a confident-looking blank.
"""

from __future__ import annotations

from typing import Any, Mapping

from sag.trajectory.schema import SUMMARY_MAX_CHARS, ObservationOutcome

_SHA_CHARS = 7
_MAX_SCALAR_PARAMS = 3
_PENDING_STATUSES = frozenset({"dispatched", "running", "polling"})
_CANCELLED_REFUSAL_CODES = frozenset({"CALL_NOT_EXECUTED"})
#: The build tool's own verbs — the enum of its `action` parameter. A command
#: is prefixed only with a verb this set names, so an unfamiliar runner reads
#: as itself rather than acquiring a verb the tool never had.
_BUILD_ACTIONS = frozenset({"deps", "compile", "test", "verify", "package", "install", "native"})


def _clip(text: str | None) -> str | None:
    if text is None:
        return None
    collapsed = " ".join(str(text).split())
    if not collapsed:
        return None
    if len(collapsed) <= SUMMARY_MAX_CHARS:
        return collapsed
    return collapsed[: SUMMARY_MAX_CHARS - 1] + "…"


def _first_line(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return " ".join(value.splitlines()[0].split()) or None


def _join(*parts: str | None) -> str | None:
    kept = [part for part in parts if part]
    return " · ".join(kept) if kept else None


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _repo_slug(url: Any) -> str | None:
    if not isinstance(url, str) or "/" not in url:
        return None
    trimmed = url.rstrip("/")
    if trimmed.endswith(".git"):
        trimmed = trimmed[: -len(".git")]
    parts = [part for part in trimmed.split("/") if part]
    if len(parts) < 2:
        return None
    return "/".join(parts[-2:])


def _short_sha(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) < _SHA_CHARS:
        return None
    return value[:_SHA_CHARS]


def _command_action(command: str | None) -> str | None:
    """The build verb a command names, when it names one of the tool's own."""

    if not command:
        return None
    named = [token for token in command.split() if token in _BUILD_ACTIONS]
    return named[-1] if named else None


def _build_call(params: dict[str, Any]) -> str | None:
    command = _first_line(params.get("command")) or _first_line(params.get("source_command"))
    action = params.get("action") or params.get("effective_action") or _command_action(command)
    if command and isinstance(action, str) and action:
        return f"{action} {command}"
    return command


def _project_call(params: dict[str, Any]) -> str | None:
    action = params.get("action")
    if action == "clone":
        slug = _repo_slug(params.get("repo_url")) or "repository"
        sha = _short_sha(params.get("ref"))
        return f"clone {slug}@{sha}" if sha else f"clone {slug}"
    if action == "provision":
        maven = params.get("maven_version")
        if maven:
            return f"provision maven {maven}"
        java = params.get("java_version")
        if java:
            distribution = params.get("java_distribution") or "jdk"
            return f"provision {distribution} {java}"
        return "provision"
    if action == "env":
        keys = params.get("keys") or params.get("variables")
        if isinstance(keys, (list, tuple)) and keys:
            return "env " + ", ".join(str(key) for key in keys[:3])
        return "env"
    return str(action) if action else None


def _search_call(params: dict[str, Any]) -> str | None:
    target = params.get("target")
    pattern = params.get("pattern")
    if target:
        return f"{target} /{pattern}/" if pattern else str(target)
    return _first_line(params.get("query"))


def _scalar_params(params: dict[str, Any]) -> str | None:
    pairs = [
        f"{key}={value}"
        for key, value in params.items()
        if isinstance(value, (str, int, float, bool))
    ]
    return " ".join(pairs[:_MAX_SCALAR_PARAMS]) or None


def call_summary(tool: str, params: Mapping[str, Any] | None) -> str | None:
    """What this call asked for, in one line."""

    if tool == "advisor":
        return "consult"
    if tool == "report":
        return "generate"
    values = _mapping(params)
    if not values:
        return None
    if tool == "build":
        return _clip(_build_call(values))
    if tool == "bash":
        return _clip(_first_line(values.get("command")))
    if tool == "project":
        return _clip(_project_call(values))
    if tool == "files":
        action = values.get("action")
        path = values.get("path") or values.get("file_path")
        return _clip(f"{action} {path}" if action and path else (action or path))
    if tool == "search":
        return _clip(_search_call(values))
    if tool == "phase":
        signal = values.get("signal")
        phase = values.get("phase")
        return _clip(f"{signal} {phase}" if signal and phase else (signal or phase))
    return _clip(_scalar_params(values))


def observation_outcome(result: Mapping[str, Any] | None) -> ObservationOutcome | None:
    """How the call came out, as one of five words."""

    if result is None:
        return None
    values = _mapping(result)
    status = str(values.get("invocation_status") or "").strip().lower()
    if status in _PENDING_STATUSES:
        return "pending"
    if str(values.get("operation_outcome") or "").strip().lower() == "success":
        return "ok"
    return "failed"


def _analysis(result: dict[str, Any]) -> dict[str, Any]:
    return _mapping(_mapping(result.get("metadata")).get("analysis"))


def _build_observation(result: dict[str, Any], outcome: ObservationOutcome) -> str | None:
    analysis = _analysis(result)
    facts = _mapping(result.get("facts"))
    exit_code = analysis.get("exit_code")
    exit_text = f"exit {exit_code}" if isinstance(exit_code, int) else None
    if outcome != "ok":
        reason = result.get("error_code") or _first_line(result.get("failure_signature"))
        return _join(exit_text, str(reason) if reason else None)
    executed = facts.get("executed")
    counts = None
    if isinstance(executed, int):
        errors = facts.get("errors")
        if not isinstance(errors, int):
            errors = _mapping(analysis.get("log_tests_run")).get("errors")
        counts = (
            f"{executed:,} tests · {facts.get('failed', 0)} F · "
            f"{errors if isinstance(errors, int) else 0} E · {facts.get('skipped', 0)} S"
        )
    artifacts = analysis.get("artifacts_created")
    jars = f"{len(artifacts)} jars" if isinstance(artifacts, list) and artifacts else None
    return _join(exit_text, counts, jars)


def _project_observation(result: dict[str, Any]) -> str | None:
    metadata = _mapping(result.get("metadata"))
    commit = _short_sha(metadata.get("resolved_commit"))
    if commit:
        path = metadata.get("clone_path")
        return f"{commit} → {path}" if path else commit
    java = metadata.get("verified_java_version")
    if java:
        return f"java {java}"
    maven = metadata.get("verified_maven_version") or metadata.get("maven_version")
    if maven:
        return f"maven {maven}"
    return None


def observation_summary(tool: str, result: Mapping[str, Any] | None) -> str | None:
    """What came back, in one line."""

    if result is None:
        return None
    values = _mapping(result)
    outcome = observation_outcome(values)
    if outcome == "pending":
        job = _mapping(values.get("metadata")).get("job_id")
        return _clip(f"running · job {job}" if job else "running")
    if tool == "build":
        return _clip(_build_observation(values, outcome))
    if tool == "project":
        return _clip(
            _project_observation(values)
            or (None if outcome == "ok" else str(values.get("error_code") or ""))
        )
    if tool == "phase":
        facts = _mapping(values.get("facts"))
        gate = facts.get("gate") or facts.get("signal")
        reason = _first_line(facts.get("reason"))
        return _clip(_join(f"gate {gate}" if gate else None, reason))
    if tool == "advisor":
        return "advice delivered" if outcome == "ok" else _clip(str(values.get("error_code") or ""))
    if outcome == "ok":
        return None
    return _clip(
        str(values.get("error_code") or "")
        or _first_line(values.get("failure_signature"))
        or "failed"
    )


def refusal_summary(payload: Mapping[str, Any]) -> tuple[ObservationOutcome, str | None]:
    """How a refused call reads: cancelled before dispatch, or refused outright."""

    values = _mapping(payload)
    code = str(values.get("refusal_code") or "").strip()
    reason = _first_line(values.get("reason"))
    if code in _CANCELLED_REFUSAL_CODES:
        return "cancelled", _clip(f"cancelled: {reason}" if reason else "cancelled")
    return "refused", _clip(_join(code or None, reason))


__all__ = [
    "call_summary",
    "observation_outcome",
    "observation_summary",
    "refusal_summary",
]
