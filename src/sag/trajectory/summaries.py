"""One line for what a call asked, one for what it answered.

Pure functions over the payload dicts the ledger already carries. They add no
facts: every value here is copied out of an `action_envelope`'s exact params or
a `tool_result`'s projected result, and every key read below is a key the ledger
really writes.

One rule governs the whole module: **the derivation states only what the payload
carries.** No defaulted counts, no invented zeros, no verdict where the record
declined to state one. Where a clause's source is missing the clause is omitted;
where no clause survives the answer is `None`. A reader is never shown a
confident-looking number nobody measured, and never an empty string standing in
for an answer.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sag.agent.control_events import CANCELLED_CALL_REFUSAL_CODE
from sag.evidence import InvocationStatus, OperationOutcome
from sag.trajectory.schema import SUMMARY_MAX_CHARS, ObservationOutcome

#: A git sha is 40 hex characters, and only such a value may be shortened. A
#: `ref` is usually a tag (`releases/lucene/10.4.0`), and half a tag names
#: nothing at all.
_SHA_CHARS = 7
_FULL_SHA_CHARS = 40
_HEX_DIGITS = frozenset("0123456789abcdef")

_MAX_SCALAR_PARAMS = 3

#: The build tool's own verbs — the enum of its `action` parameter, mirrored
#: because this package is a read-only derivation and must not import a
#: Docker-bound tool. `tests/test_trajectory_summaries.py` pins the two equal.
_BUILD_ACTIONS = frozenset({"deps", "compile", "test", "verify", "package", "install", "native"})

#: A dispatched call with no terminal exit yet. The engine's word is `pending`;
#: a status it never writes would leave every running job reading as a failure.
_PENDING_STATUS = InvocationStatus.PENDING.value
_CANCELLED_STATUS = InvocationStatus.CANCELLED.value
_FAILED_STATUSES = frozenset({InvocationStatus.TIMEOUT.value, InvocationStatus.CRASHED.value})


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


def _count(value: Any) -> int | None:
    """A number the payload actually carried, or `None`.

    A count is printed only when it is an integer in the record. `bool` is an
    `int` in Python and is not a count, and a `None` the payload wrote in place
    of a number is an absent count, not a zero.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


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


def _is_sha(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _FULL_SHA_CHARS
        and set(value.lower()) <= _HEX_DIGITS
    )


def _short_sha(value: Any) -> str | None:
    """The readable head of a commit sha, and nothing else."""

    return value[:_SHA_CHARS] if _is_sha(value) else None


def _ref_label(value: Any) -> str | None:
    """A git ref as the record holds it — shortened only when it is a sha."""

    ref = _first_line(value)
    if ref is None:
        return None
    return ref[:_SHA_CHARS] if _is_sha(ref) else ref


def _command_action(command: str | None) -> str | None:
    """The build verb a command names, when it names one of the tool's own."""

    if not command:
        return None
    named = [token for token in command.split() if token in _BUILD_ACTIONS]
    return named[-1] if named else None


def _build_call(params: dict[str, Any]) -> str | None:
    command = _first_line(params.get("command")) or _first_line(params.get("source_command"))
    action = _first_line(params.get("action")) or _command_action(command)
    if command:
        # A command spells out its own options; the args are not appended twice.
        return f"{action} {command}" if action else command
    if not action:
        return None
    # Only text is rendered, so a container never reaches the line raw; the
    # build tool declares `args` a string and every real envelope states one.
    args = _first_line(params.get("args"))
    return f"{action} {args}" if args else action


def _project_call(params: dict[str, Any]) -> str | None:
    action = _first_line(params.get("action"))
    if action == "clone":
        slug = _repo_slug(params.get("repo_url")) or "repository"
        ref = _ref_label(params.get("ref"))
        return f"clone {slug}@{ref}" if ref else f"clone {slug}"
    if action == "provision":
        maven = _first_line(params.get("maven_version"))
        if maven:
            return f"provision maven {maven}"
        java = _first_line(params.get("java_version"))
        if java:
            # `java` names the key the number came from; a distribution word
            # would be a fact the envelope did not carry.
            distribution = _first_line(params.get("java_distribution")) or "java"
            return f"provision {distribution} {java}"
        return "provision"
    if action == "env":
        runtime = _first_line(params.get("tool"))
        return f"env {runtime}" if runtime else "env"
    return action


def _search_call(params: dict[str, Any]) -> str | None:
    target = _first_line(params.get("target"))
    if target is None:
        return None
    pattern = _first_line(params.get("pattern"))
    return f"{target} /{pattern}/" if pattern else target


def _scalar_params(params: dict[str, Any]) -> str | None:
    pairs = [
        f"{key}={value}"
        for key, value in params.items()
        if isinstance(value, (str, int, float, bool))
    ]
    return " ".join(pairs[:_MAX_SCALAR_PARAMS]) or None


def call_summary(tool: str, params: Mapping[str, Any] | None) -> str | None:
    """What this call asked for, in one line."""

    values = _mapping(params)
    # Two tools answer from their name: the advisor is always consulted, and the
    # report is always generated, whether or not any params were recorded.
    if tool == "advisor":
        return "consult"
    if tool == "report":
        return _clip(_first_line(values.get("action"))) or "generate"
    if not values:
        return None
    if tool == "build":
        return _clip(_build_call(values))
    if tool == "bash":
        return _clip(_first_line(values.get("command")))
    if tool == "project":
        return _clip(_project_call(values))
    if tool == "file_io":
        action = _first_line(values.get("action"))
        path = _first_line(values.get("path"))
        return _clip(f"{action} {path}" if action and path else (action or path))
    if tool == "search":
        return _clip(_search_call(values))
    if tool == "phase":
        action = _first_line(values.get("action"))
        outcome = _first_line(values.get("outcome"))
        return _clip(f"{action} {outcome}" if action and outcome else (action or outcome))
    return _clip(_scalar_params(values))


def observation_outcome(result: Mapping[str, Any] | None) -> ObservationOutcome | None:
    """How the call came out, as one of the engine's own words.

    `unknown` is the record explicitly declining to state an outcome, and so are
    `partial` and `skipped` as far as this vocabulary goes — none of them is a
    failure, and answering `failed` would be inventing a verdict against a
    payload that refused to give one.
    """

    if result is None:
        return None
    values = _mapping(result)
    status = str(values.get("invocation_status") or "").strip().lower()
    if status == _PENDING_STATUS:
        return "pending"
    if status == _CANCELLED_STATUS:
        return "cancelled"
    if status in _FAILED_STATUSES:
        return "failed"
    outcome = str(values.get("operation_outcome") or "").strip().lower()
    if outcome == OperationOutcome.SUCCESS.value:
        return "ok"
    if outcome == OperationOutcome.FAILED.value:
        return "failed"
    return None


def _analysis(result: dict[str, Any]) -> dict[str, Any]:
    return _mapping(_mapping(result.get("metadata")).get("analysis"))


def _build_observation(result: dict[str, Any], outcome: ObservationOutcome | None) -> str | None:
    metadata = _mapping(result.get("metadata"))
    analysis = _analysis(result)
    facts = _mapping(result.get("facts"))
    exit_code = _count(metadata.get("exit_code"))
    if exit_code is None:
        exit_code = _count(analysis.get("exit_code"))
    exit_text = f"exit {exit_code}" if exit_code is not None else None
    counts = None
    executed = _count(facts.get("executed"))
    if executed is not None:
        # Each term is stated only when its own number was taken. The error
        # count is a measurement of its own — a test that could not run at all,
        # counted apart from one that ran and failed — and it is kept in the log
        # analysis rather than in `facts`.
        terms = [f"{executed:,} tests"]
        failed = _count(facts.get("failed"))
        if failed is not None:
            terms.append(f"{failed} F")
        errors = _count(analysis.get("test_error_count"))
        if errors is not None:
            terms.append(f"{errors} E")
        skipped = _count(facts.get("skipped"))
        if skipped is not None:
            terms.append(f"{skipped} S")
        counts = " · ".join(terms)
    artifacts = analysis.get("artifacts_created")
    made = (
        f"{len(artifacts)} artifact{'s' if len(artifacts) > 1 else ''}"
        if isinstance(artifacts, list) and artifacts
        else None
    )
    # A run that failed still ran what it ran: the reason is appended to the
    # counts, never substituted for them.
    reason = None
    if outcome != "ok":
        reason = _first_line(result.get("error_code")) or _first_line(analysis.get("error_type"))
    return _join(exit_text, counts, made, reason)


def _project_observation(result: dict[str, Any]) -> str | None:
    metadata = _mapping(result.get("metadata"))
    commit = _short_sha(metadata.get("resolved_commit"))
    path = _first_line(metadata.get("clone_path"))
    if commit and path:
        return f"{commit} → {path}"
    if commit or path:
        return commit or path
    java = _first_line(metadata.get("verified_java_version")) or _first_line(
        metadata.get("java_version")
    )
    return f"java {java}" if java else None


def _search_observation(result: dict[str, Any]) -> str | None:
    """How a search came out: a count when one was taken, else whether it hit.

    `metadata.total_matches` is a real count and is preferred when it is stated.
    `facts["matched"]` is not a count — `search_tool.py` writes it as `True`,
    `False` or `None` — so it answers "did the pattern hit" and nothing more.
    """

    total = _count(_mapping(result.get("metadata")).get("total_matches"))
    if total is not None:
        return f"{total:,} matches"
    matched = _mapping(result.get("facts")).get("matched")
    if isinstance(matched, bool):
        return "matched" if matched else "no match"
    return None


def _phase_observation(result: dict[str, Any]) -> str | None:
    metadata = _mapping(result.get("metadata"))
    gate = _mapping(metadata.get("gate_result"))
    word = _first_line(gate.get("code"))
    if word:
        # The reason code, bare, like every other reason code this module
        # renders. Labelling it `gate` put the word over two different facts —
        # the gate's NAME here and the word a gate DELIVERED on the row beside
        # it — and one word over two facts makes both unreadable.
        return _join(word, _first_line(gate.get("reason")))
    return _first_line(metadata.get("phase_signal"))


def observation_summary(tool: str, result: Mapping[str, Any] | None) -> str | None:
    """What came back, in one line."""

    if result is None:
        return None
    values = _mapping(result)
    outcome = observation_outcome(values)
    if outcome == "pending":
        job = _first_line(_mapping(values.get("metadata")).get("job_id"))
        return _clip(f"running · job {job}" if job else "running")
    if tool == "build":
        return _clip(_build_observation(values, outcome))
    if tool == "project":
        if outcome == "ok":
            return _clip(_project_observation(values))
        return _clip(_first_line(values.get("error_code")))
    if tool == "phase":
        return _clip(_phase_observation(values))
    if tool == "search":
        if outcome == "ok":
            return _clip(_search_observation(values))
        return _clip(_first_line(values.get("error_code")))
    if tool == "advisor":
        if outcome == "ok":
            return "advice delivered"
        return _clip(_first_line(values.get("error_code")))
    if outcome == "ok":
        return None
    # The verdict is the outcome's job. This line carries the code the payload
    # stated, or says nothing at all.
    return _clip(_first_line(values.get("error_code")))


def refusal_summary(payload: Mapping[str, Any]) -> tuple[ObservationOutcome, str | None]:
    """How a refused call reads: cancelled before dispatch, or refused outright."""

    values = _mapping(payload)
    code = str(values.get("refusal_code") or "").strip()
    reason = _first_line(values.get("reason"))
    if code == CANCELLED_CALL_REFUSAL_CODE:
        return "cancelled", _clip(f"cancelled: {reason}" if reason else "cancelled")
    return "refused", _clip(_join(code or None, reason))


__all__ = [
    "call_summary",
    "observation_outcome",
    "observation_summary",
    "refusal_summary",
]
