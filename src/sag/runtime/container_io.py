"""Lossless machine-facing reads from the managed container.

DockerOrchestrator intentionally strips and may truncate ordinary command
output before it reaches the model.  XML/JSON parsers and persistence
readbacks are machine consumers: they must bypass that presentation layer.
"""

from __future__ import annotations

import base64
import binascii
import shlex
from typing import Any, Mapping, Optional

_PAYLOAD_MARKER = "__SAG_FILE_BASE64__"
_MISSING_MARKER = "__SAG_FILE_MISSING__"


class ContainerFileReadError(RuntimeError):
    """The file existed (or its state was unknown) but could not be read safely."""


def _command_succeeded(result: Mapping[str, Any]) -> bool:
    return result.get("success") is not False and result.get("exit_code", 0) == 0


def _execute_untruncated(orchestrator: Any, command: str) -> Mapping[str, Any]:
    """Use the production no-truncation API, with a narrow test-double fallback."""
    try:
        result = orchestrator.execute_command(command, truncate_output=False)
    except TypeError as exc:
        # Several small unit-test orchestrators predate the presentation flag.
        # Production DockerOrchestrator accepts it; only fall back when Python
        # explicitly rejected that keyword.
        if "truncate_output" not in str(exc):
            raise
        result = orchestrator.execute_command(command)
    if not isinstance(result, Mapping):
        raise ContainerFileReadError("container read returned a non-mapping result")
    return result


def _direct_read(
    orchestrator: Any, path: str, *, exact_bytes: bool = False
) -> tuple[bool, Optional[str]]:
    """`read_file` exists only on test doubles; production has no such method.

    The doubles' protocol states the same three answers as the transport
    (§3.9): ``None`` = absent, a mapping that did not succeed = a failed READ
    — which raises on the exact path, exactly as a failed transport does —
    and anything else is content. Before this, a double returning
    ``{"success": False}`` read as "absent", so every test written against it
    exercised a conflation production does not have.
    """
    reader = getattr(orchestrator, "read_file", None)
    if not callable(reader):
        return False, None
    result = reader(path)
    if isinstance(result, Mapping):
        if not _command_succeeded(result):
            if exact_bytes:
                raise ContainerFileReadError(
                    f"direct read did not succeed for {path}: "
                    f"{str(result.get('output') or result.get('content') or '')[:120]}"
                )
            return True, None
        content = result.get("content")
        if content is None:
            content = result.get("output", "")
        return True, str(content or "")
    if result is None:
        return True, None
    return True, str(result)


def read_container_text(
    orchestrator: Any,
    path: str,
    *,
    exact_bytes: bool = False,
) -> Optional[str]:
    """Read UTF-8 text without presentation truncation.

    ``None`` means the file is absent.  On the ``exact_bytes`` path, absent
    means MARKER-VERIFIED absent (``__SAG_FILE_MISSING__`` / exit 44): a
    command that did not succeed raises ``ContainerFileReadError`` instead of
    masquerading as absence, because "could not look" and "looked and found
    nothing" license opposite actions — the first caps, the second may create.
    Malformed transport or invalid UTF-8 raises the same error.

    Scope (Plan 8 §3.9, first half): ``_direct_read`` and the non-exact
    readers still return ``None`` on failure; their callers and test doubles
    migrate separately.

    ``exact_bytes`` additionally preserves terminal newlines and every other
    UTF-8 byte via base64 transport.  It is required for transactional
    readback.  XML/JSON parsers normally need only the untruncated path.
    """
    handled, direct = _direct_read(orchestrator, path, exact_bytes=exact_bytes)
    if handled:
        return direct

    # In-memory file maps are an explicit test-double API and preserve bytes.
    # Limit this shortcut to exact readback; parser tests still exercise the
    # production truncate_output=False call.
    if exact_bytes:
        files = getattr(orchestrator, "files", None)
        if isinstance(files, dict):
            value = files.get(path)
            return None if value is None else str(value)

        quoted = shlex.quote(path)
        transport = (
            f"if test -f {quoted}; then "
            f"printf '{_PAYLOAD_MARKER}'; base64 -w 0 -- {quoted}; "
            f"else printf '{_MISSING_MARKER}'; exit 44; fi"
        )
        result = _execute_untruncated(orchestrator, transport)
        output = str(result.get("output") or "")
        if output.startswith(_MISSING_MARKER) or result.get("exit_code") == 44:
            return None
        if output.startswith(_PAYLOAD_MARKER):
            encoded = output[len(_PAYLOAD_MARKER) :]
            try:
                raw = base64.b64decode(encoded, validate=True)
                return raw.decode("utf-8")
            except (binascii.Error, UnicodeDecodeError) as exc:
                raise ContainerFileReadError(
                    f"lossless container read returned invalid payload for {path}"
                ) from exc
        # Plan 8 §3.9: a probe that DID NOT SUCCEED proves nothing about the
        # file, and returning None here reported it as absent. Live shape:
        # DockerOrchestrator converts every within-command failure into
        # {"success": False, ...} without raising, so this branch — not an
        # exception — is how a transient docker failure arrives. Reading it
        # as absence is what let a settlement-time hiccup replace the whole
        # survey manifest with one structure key ("absent → create").
        if not _command_succeeded(result):
            raise ContainerFileReadError(
                f"lossless container read did not succeed for {path}: "
                f"{str(result.get('output') or '')[:120]}"
            )
        # Compatibility for a small orchestrator that answered the transport
        # probe successfully but without a marker: fall through to an
        # untruncated cat. Production always emits one of the trusted markers.

    result = _execute_untruncated(orchestrator, f"cat -- {shlex.quote(path)}")
    if not _command_succeeded(result):
        if exact_bytes:
            # A plain `cat` cannot prove absence — only the marker probe can —
            # so on the transactional path its failure is a failed READ. The
            # non-exact parsers keep None-on-failure until their doubles
            # migrate to an explicit absence protocol (measured: 17 doubles
            # express "absent" as a plain failure today).
            raise ContainerFileReadError(
                f"container read did not succeed for {path}: "
                f"{str(result.get('output') or '')[:120]}"
            )
        return None
    return str(result.get("output") or "")
