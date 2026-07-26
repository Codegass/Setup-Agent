"""Render the phase step window into a native tool-calling messages array.

``self.steps`` remains the engine's single source of truth (phase signals,
compaction, archive, journal, and reporting all consume it unchanged); this
module derives the provider conversation from it every iteration.

Rendering rules (Plan 2 Task 2; each is covered by ``tests/test_native_messages.py``):

1. ``system_prompt`` becomes one leading ``{"role": "system"}`` message.
2. SYSTEM_GUIDANCE steps (including the phase intro and the compaction
   ledger) become ``{"role": "user", "content": step.content}``.
3. THOUGHT steps become ``{"role": "assistant", "content": step.content}``.
   The native loop produces none; legacy steps render harmlessly.
4. A run of consecutive ACTION steps sharing ``native_text`` provenance
   renders as ONE assistant message whose ``content`` is that shared prose
   ("" renders as ``None``) and whose ``tool_calls`` hold one entry per
   ACTION step. ACTION steps with no ``tool_call_id`` get ``synthetic-<n>``.
5. Each OBSERVATION step carrying a ``tool_call_id`` becomes
   ``{"role": "tool", "tool_call_id": ..., "content": <clamped>}``.
6. Pairing repair (defense in depth): every emitted ``tool_call.id`` ends up
   with exactly one ``role="tool"`` reply placed immediately after the
   assistant message that opened it. Missing replies are synthesized as
   cancellations, duplicates collapse to the first, orphans are dropped, and
   a reply separated from its call by an interleaved guidance message is
   hoisted back into place. Anthropic hard-400s on any of those, and the
   engine can interleave guidance between an ACTION step and its
   OBSERVATION, so the renderer guarantees the invariant rather than
   trusting the dispatcher to.
7. Clamping is tail-preserving: observation content over 5000 chars renders
   as the first 2000 chars, an omission marker, and the LAST 3000 chars. The
   tail is where the real failure lives, so head-only truncation is the
   exact bug this replaces.
8. Legacy OBSERVATION steps without a ``tool_call_id`` become
   ``{"role": "user", "content": "[observation] " + <clamped>}``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

from .react_types import StepType

_CLAMP_TOTAL = 5000
_CLAMP_HEAD = 2000
_CLAMP_TAIL = 3000

_CANCELLED = "[cancelled by harness: no result was produced for this call]"


def _clamp(content: Optional[str]) -> str:
    """Truncate long tool output through the middle, keeping head and tail."""
    text = content or ""
    if len(text) <= _CLAMP_TOTAL:
        return text
    omitted = len(text) - _CLAMP_HEAD - _CLAMP_TAIL
    return text[:_CLAMP_HEAD] + f"\n…[{omitted} chars omitted]…\n" + text[-_CLAMP_TAIL:]


def _cancellation(tool_call_id: str) -> Dict[str, Any]:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": _CANCELLED}


def render_messages(system_prompt: str, steps: Iterable[Any]) -> List[Dict[str, Any]]:
    """Render ``steps`` (one phase window) as an OpenAI-normalized, pairing-safe
    messages array prefixed by ``system_prompt``."""
    messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    pending_assistant: Optional[Dict[str, Any]] = None
    pending_text: Optional[str] = None
    synthetic = 0

    def flush_assistant() -> None:
        nonlocal pending_assistant, pending_text
        if pending_assistant is not None:
            messages.append(pending_assistant)
            pending_assistant = None
            pending_text = None

    for step in steps:
        step_type = getattr(step, "step_type", None)

        if step_type == StepType.ACTION:
            call_id = getattr(step, "tool_call_id", None)
            if not call_id:
                synthetic += 1
                call_id = f"synthetic-{synthetic}"
            entry = {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": getattr(step, "tool_name", "") or "",
                    "arguments": json.dumps(getattr(step, "tool_params", None) or {}),
                },
            }
            text = getattr(step, "native_text", None) or ""
            # Only calls from the same assistant turn (same prose provenance)
            # may share one assistant message.
            if pending_assistant is not None and text != pending_text:
                flush_assistant()
            if pending_assistant is None:
                pending_text = text
                pending_assistant = {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [entry],
                }
            else:
                pending_assistant["tool_calls"].append(entry)
            continue

        flush_assistant()

        if step_type == StepType.OBSERVATION:
            call_id = getattr(step, "tool_call_id", None)
            clamped = _clamp(getattr(step, "content", ""))
            if call_id:
                messages.append({"role": "tool", "tool_call_id": call_id, "content": clamped})
            else:
                messages.append({"role": "user", "content": "[observation] " + clamped})
        elif step_type == StepType.THOUGHT:
            messages.append({"role": "assistant", "content": getattr(step, "content", "")})
        else:  # SYSTEM_GUIDANCE and anything else
            messages.append({"role": "user", "content": getattr(step, "content", "")})

    flush_assistant()
    return _repair_pairing(messages)


def _repair_pairing(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Guarantee exactly one ``role="tool"`` reply per emitted tool_call id,
    directly after the assistant message that opened it."""
    replies: Dict[str, Dict[str, Any]] = {}
    for message in messages:
        if message["role"] == "tool":
            replies.setdefault(message["tool_call_id"], message)

    repaired: List[Dict[str, Any]] = []
    answered: set[str] = set()
    for message in messages:
        if message["role"] == "tool":
            continue  # re-emitted below next to its call, or dropped as an orphan
        repaired.append(message)
        if message["role"] != "assistant":
            continue
        for call in message.get("tool_calls") or ():
            call_id = call["id"]
            reply = None if call_id in answered else replies.get(call_id)
            answered.add(call_id)
            repaired.append(reply if reply is not None else _cancellation(call_id))
    return repaired
