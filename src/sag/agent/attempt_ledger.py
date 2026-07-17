"""Rolling compaction of phase history into an attempt ledger (spec §3.2).

Old ACTION steps become one lifecycle-aware line each so a
long debugging phase stays inside a small window while every attempt
(especially failures) remains visible. Old thoughts drop: the lasting
record of judgment is the ledger line + notes, not the musing.

The ledger ACCUMULATES across waves: when a prior wave's ledger step itself
ages into the compacted slice, its lines merge into the new ledger instead
of vanishing — failed approaches stay visible for the whole phase. The size
cap sheds terminal non-failures first and never sheds failed or pending lines."""

import re
from dataclasses import dataclass, replace
from typing import List, Optional, Tuple

LEDGER_HEADER = (
    "ATTEMPT LEDGER (older work, compacted — do NOT retry ✗ entries the same way; "
    "… pending handoffs require polling unless a later terminal entry closes the same ref):"
)
MAX_LEDGER_LINES = 60
_JOB_REF_PATTERN = re.compile(r"\bjob:[A-Za-z0-9_-]+\b")
_LEDGER_CODE_PATTERN = re.compile(r"(?:^| )code=([^ ]+)")
_LEDGER_SIGNATURE_PATTERN = re.compile(r"(?:^| )signature=([^ ]+)")
_LEDGER_COUNT_PATTERN = re.compile(r" ×(\d+)$")
_FAILURE_HEAD_LINES = 30
_FAILURE_TAIL_LINES = 80
_FAILURE_PREVIEW_CHARS = 400


def failure_preview(output: str, *, explicit_tail: str = "") -> str:
    """Render a bounded failure preview while preserving the fatal tail.

    The retained source window is at most the first 30 and last 80 lines.  The
    prompt-facing 400-character preview then favors the end of that window, so
    a late compiler/CMake traceback cannot be replaced by startup chatter.
    """

    source = str(output or "")
    tail = str(explicit_tail or "").strip()
    lines = source.splitlines()
    if len(lines) > _FAILURE_HEAD_LINES + _FAILURE_TAIL_LINES:
        retained = [
            *lines[:_FAILURE_HEAD_LINES],
            "…",
            *lines[-_FAILURE_TAIL_LINES:],
        ]
    else:
        retained = lines
    retained_text = "\n".join(retained).strip()
    if tail and tail not in retained_text:
        retained_text = f"{retained_text}\n{tail}".strip()
    if len(retained_text) <= _FAILURE_PREVIEW_CHARS:
        return retained_text

    fatal_source = tail or "\n".join(lines[-_FAILURE_TAIL_LINES:]).strip()
    if len(fatal_source) >= _FAILURE_PREVIEW_CHARS:
        return fatal_source[-_FAILURE_PREVIEW_CHARS:]
    remaining = _FAILURE_PREVIEW_CHARS - len(fatal_source) - 3
    head = retained_text[: max(0, remaining)].rstrip()
    return f"{head}\n…\n{fatal_source}"[-_FAILURE_PREVIEW_CHARS:]


@dataclass(frozen=True)
class AttemptLedgerEntry:
    """One outcome-aware, prompt-safe action history entry."""

    action_key: str
    outcome: str
    preview: str
    output_ref: str
    error_code: str = ""
    failure_signature: str = ""
    occurrence_count: int = 1


class AttemptLedger:
    """Structured failure ledger used by handoff and focused unit tests.

    Identical failure identities collapse to one entry, but retain the newest
    full-output reference and an occurrence count.  This is deliberately not a
    second evidence store: the engine's canonical observations remain in
    ``RunEvidenceState``.
    """

    def __init__(self) -> None:
        self._entries: list[AttemptLedgerEntry] = []
        self._failure_indexes: dict[tuple[str, str, str], int] = {}

    def record_failed_action(
        self,
        *,
        action_key: str,
        output: str,
        output_ref: str,
        error_code: str,
        failure_signature: str = "",
        error_tail_preview: str = "",
    ) -> AttemptLedgerEntry:
        identity = (str(action_key), str(error_code), str(failure_signature))
        entry = AttemptLedgerEntry(
            action_key=str(action_key),
            outcome="failed",
            preview=failure_preview(output, explicit_tail=error_tail_preview),
            output_ref=str(output_ref),
            error_code=str(error_code),
            failure_signature=str(failure_signature),
        )
        existing_index = self._failure_indexes.get(identity) if failure_signature else None
        if existing_index is None:
            if failure_signature:
                self._failure_indexes[identity] = len(self._entries)
            self._entries.append(entry)
            return entry
        previous = self._entries[existing_index]
        updated = replace(entry, occurrence_count=previous.occurrence_count + 1)
        self._entries[existing_index] = updated
        return updated

    def prompt_entries(self) -> tuple[AttemptLedgerEntry, ...]:
        return tuple(self._entries)


def _step_kind(step) -> str:
    kind = getattr(getattr(step, "step_type", None), "value", None)
    return kind or str(getattr(step, "step_type", ""))


def _previous_ledger_lines(step) -> List[str]:
    """Lines carried by a prior wave's ledger step ([] for any other step)."""
    content = getattr(step, "content", "") or ""
    if "ATTEMPT LEDGER" not in content:
        return []
    return [line for line in content.split("\n")[1:] if line.strip()]


def _failure_line_identity(line: str) -> tuple[str, str, str] | None:
    if not line.startswith(("✗ ", "~ ", "? ")):
        return None
    parts = line.split(" ", 2)
    code = _LEDGER_CODE_PATTERN.search(line)
    signature = _LEDGER_SIGNATURE_PATTERN.search(line)
    if len(parts) < 2 or signature is None:
        return None
    return parts[1], code.group(1) if code else "", signature.group(1)


def _failure_line_count(line: str) -> int:
    match = _LEDGER_COUNT_PATTERN.search(line)
    return int(match.group(1)) if match else 1


def _job_refs(line: str) -> set[str]:
    _, separator, ref_text = line.rpartition(" → ")
    return set(_JOB_REF_PATTERN.findall(ref_text)) if separator else set()


def _terminal_poll_refs(steps: List) -> set[str]:
    refs = set()
    for step in steps:
        result = getattr(step, "tool_result", None)
        poll_ref = getattr(result, "poll_ref", None)
        invocation = getattr(getattr(result, "invocation_status", None), "value", None)
        if poll_ref and invocation != "pending":
            refs.add(poll_ref)
    return refs


def _reconcile_job_lifecycle(
    lines: List[str], terminal_refs: set[str]
) -> Tuple[List[str], set[str]]:
    pending_refs = {
        ref for line in lines if line.startswith("…") for ref in _job_refs(line)
    }
    terminal_line_refs = {
        ref for line in lines if not line.startswith("…") for ref in _job_refs(line)
    }
    closed_refs = pending_refs & (terminal_refs | terminal_line_refs)
    reconciled = [
        line
        for line in lines
        if not (line.startswith("…") and bool(_job_refs(line) & closed_refs))
    ]
    return reconciled, pending_refs & terminal_line_refs


def _cap_lines(lines: List[str], protected_job_refs: set[str]) -> List[str]:
    """Drop oldest terminal non-failures first; failed/pending lines survive."""
    overflow = len(lines) - MAX_LEDGER_LINES
    if overflow <= 0:
        return lines
    kept = []
    for line in lines:
        protected = line.startswith(("✗", "…")) or bool(
            _job_refs(line) & protected_job_refs
        )
        if overflow > 0 and not protected:
            overflow -= 1
            continue
        kept.append(line)
    return kept


def _result_marker(result) -> Tuple[str, str]:
    invocation = getattr(getattr(result, "invocation_status", None), "value", None)
    outcome = getattr(getattr(result, "operation_outcome", None), "value", None)
    if invocation == "pending":
        return "…", "PENDING"
    return {
        "success": ("✓", "SUCCESS"),
        "failed": ("✗", "FAILED"),
        "partial": ("~", "PARTIAL"),
        "unknown": ("?", "UNKNOWN"),
        "skipped": ("-", "SKIPPED"),
    }.get(outcome, ("?", "UNKNOWN"))


def compact_steps(steps: List, keep_recent: int = 30) -> Tuple[Optional[str], List]:
    """Returns (ledger_text or None, remaining_steps)."""
    if len(steps) <= keep_recent:
        return None, list(steps)

    old, recent = steps[:-keep_recent], steps[-keep_recent:]
    lines = []
    failure_indexes: dict[tuple[str, str, str], tuple[int, int]] = {}
    for step in old:
        if "action" not in _step_kind(step).lower():
            # A prior wave's ledger ages out like any old step — merge its
            # lines (it sits first in the window, so chronology is preserved).
            for previous_line in _previous_ledger_lines(step):
                identity = _failure_line_identity(previous_line)
                if identity is not None:
                    prior = failure_indexes.get(identity)
                    count = _failure_line_count(previous_line)
                    if prior is not None:
                        index, prior_count = prior
                        merged_count = prior_count + count
                        base = _LEDGER_COUNT_PATTERN.sub("", previous_line)
                        lines[index] = f"{base} ×{merged_count}"
                        failure_indexes[identity] = (index, merged_count)
                        continue
                    failure_indexes[identity] = (len(lines), count)
                lines.append(previous_line)
            continue
        result = getattr(step, "tool_result", None)
        marker, state = _result_marker(result)
        error_code = str(getattr(result, "error_code", "") or "")
        signature = str(getattr(result, "failure_signature", "") or "")
        if marker in {"✗", "~", "?"}:
            source = (
                getattr(result, "raw_output", "")
                or getattr(result, "output", "")
                or getattr(result, "error", "")
                or ""
            )
            summary = failure_preview(
                source,
                explicit_tail=getattr(result, "error_tail_preview", "") or "",
            )
        else:
            summary = (getattr(result, "output", "") or "")[:90]
        summary = summary.replace("\n", " ⏎ ").replace("→", "->")
        metadata = getattr(result, "metadata", None) or {}
        refs = []
        for ref_id in (
            getattr(result, "poll_ref", None),
            metadata.get("output_ref_id"),
            getattr(result, "output_ref", None),
        ):
            if ref_id and ref_id not in refs:
                refs.append(ref_id)
        ref_text = f" → {', '.join(refs)}" if refs else ""
        identity_text = ""
        if error_code:
            identity_text += f" code={error_code}"
        if signature:
            identity_text += f" signature={signature}"
        line = (
            f"{marker} {getattr(step, 'tool_name', '?')} [{state}]: "
            f"{summary}{identity_text}{ref_text}"
        )
        if marker in {"✗", "~", "?"} and signature:
            identity = (str(getattr(step, "tool_name", "?")), error_code, signature)
            previous = failure_indexes.get(identity)
            if previous is not None:
                index, count = previous
                count += 1
                lines[index] = f"{line} ×{count}"
                failure_indexes[identity] = (index, count)
                continue
            failure_indexes[identity] = (len(lines), 1)
        lines.append(line)

    lines, protected_job_refs = _reconcile_job_lifecycle(
        lines,
        _terminal_poll_refs(steps),
    )
    if not lines:
        return None, recent
    ledger = LEDGER_HEADER + "\n" + "\n".join(_cap_lines(lines, protected_job_refs))
    return ledger, recent
