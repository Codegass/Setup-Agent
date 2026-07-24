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


def _direct_read(orchestrator: Any, path: str) -> tuple[bool, Optional[str]]:
    reader = getattr(orchestrator, "read_file", None)
    if not callable(reader):
        return False, None
    result = reader(path)
    if isinstance(result, Mapping):
        if not _command_succeeded(result):
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

    ``None`` means the file is absent.  Malformed transport or invalid UTF-8
    raises ``ContainerFileReadError`` so callers can fail closed.

    ``exact_bytes`` additionally preserves terminal newlines and every other
    UTF-8 byte via base64 transport.  It is required for transactional
    readback.  XML/JSON parsers normally need only the untruncated path.
    """
    handled, direct = _direct_read(orchestrator, path)
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
        # Compatibility for a small orchestrator that does not understand the
        # transport probe: fall through to an untruncated cat. Production
        # always emits one of the trusted markers above.

    result = _execute_untruncated(orchestrator, f"cat -- {shlex.quote(path)}")
    if not _command_succeeded(result):
        return None
    return str(result.get("output") or "")
