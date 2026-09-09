"""Lossless machine-facing reads from the managed container.

DockerOrchestrator intentionally strips and may truncate ordinary command
output before it reaches the model.  XML/JSON parsers and persistence
readbacks are machine consumers: they must bypass that presentation layer.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import shlex
from collections.abc import Callable
from typing import Any, Mapping, Optional, cast

_PAYLOAD_MARKER = "__SAG_FILE_BASE64__"
_MISSING_MARKER = "__SAG_FILE_MISSING__"
_PREFIX_MARKER = "SAG_FILE_PREFIX_V1"
_PREFIX_END_MARKER = "SAG_FILE_PREFIX_END_V1"


class ContainerFileReadError(RuntimeError):
    """The file existed (or its state was unknown) but could not be read safely."""


def resolve_control_execute(source: Any) -> Callable[..., Any] | None:
    """Resolve the clean host-control executor, retaining a narrow fake fallback.

    A strict evidence transport must not run through the project runtime
    environment.  Production orchestrators expose ``execute_control_command``;
    older test doubles intentionally fall back to ``execute_command``.  Writers
    often receive a bound ``execute_command`` callback, so its owner is checked
    before the callback itself is accepted.
    """

    clean = getattr(source, "execute_control_command", None)
    if callable(clean):
        return cast(Callable[..., Any], clean)
    owner = getattr(source, "__self__", None) if callable(source) else None
    owner_clean = getattr(owner, "execute_control_command", None)
    if callable(owner_clean):
        return cast(Callable[..., Any], owner_clean)
    if owner is not None:
        # A bound normal executor is not a free-standing compatibility fake.
        # Falling back to it would re-enter the project-controlled runtime
        # overlay for evidence reads/writes merely because a caller passed the
        # method instead of its owner.  Production owners must expose the
        # clean control channel explicitly.
        return None
    execute = getattr(source, "execute_command", None)
    if callable(execute):
        return cast(Callable[..., Any], execute)
    return cast(Callable[..., Any], source) if callable(source) else None


def _command_succeeded(result: Mapping[str, Any]) -> bool:
    return result.get("success") is not False and result.get("exit_code", 0) == 0


def command_did_not_run(result: Any) -> bool:
    """The two unambiguous did-not-run signatures, in ONE place (§3.9, P3).

    `DockerOrchestrator.execute_command` converts every within-command failure
    into `{"success": False, "exit_code": -1, "dispatch_status": ...}` — it
    does not raise for those. A command the CONTAINER ran and that exited
    nonzero (an empty glob, an absent file) is not this: exit 1 keeps meaning
    what the command said. Four independent implementation rounds each wrote a
    per-site `except` for a raise that never comes; every consumer of this
    distinction reads it from here.
    """
    return isinstance(result, Mapping) and (
        result.get("exit_code") == -1 or bool(result.get("dispatch_status"))
    )


def _execute_untruncated(orchestrator: Any, command: str) -> Mapping[str, Any]:
    """Use the production no-truncation API, with a narrow test-double fallback."""
    execute = resolve_control_execute(orchestrator)
    if execute is None:
        raise ContainerFileReadError("container read source is not executable")
    try:
        result = execute(command, truncate_output=False)
    except TypeError as exc:
        # Several small unit-test orchestrators predate the presentation flag.
        # Production DockerOrchestrator accepts it; only fall back when Python
        # explicitly rejected that keyword.
        if "truncate_output" not in str(exc):
            raise
        result = execute(command)
    if not isinstance(result, Mapping):
        raise ContainerFileReadError("container read returned a non-mapping result")
    return result


def read_container_prefix(source: Any, path: str, *, max_bytes: int) -> tuple[bytes, bool]:
    """Read an exact byte prefix and whether the file extends beyond it.

    Only ``max_bytes + 1`` bytes are read in the container. The extra byte
    distinguishes an exactly full budget from a truncated file; it is never
    returned or hashed. Framing survives presentation whitespace stripping,
    while its length and digest reject clipped or changed transport data.
    Missing files and unverified reads raise ``ContainerFileReadError``.
    """
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("max_bytes must be a nonnegative integer")
    script = (
        "import base64,hashlib,sys\n"
        "limit=int(sys.argv[2])\n"
        "with open(sys.argv[1], 'rb') as source:\n"
        " data=source.read(limit+1)\n"
        "raw=data[:limit]\n"
        f"print('{_PREFIX_MARKER}\\t'+str(len(raw))+'\\t'+"
        "hashlib.sha256(raw).hexdigest()+'\\t'+str(int(len(data)>limit))+'\\t'+"
        "base64.b64encode(raw).decode('ascii'))\n"
        f"print('{_PREFIX_END_MARKER}')\n"
    )
    command = f"python3 -c {shlex.quote(script)} {shlex.quote(path)} {max_bytes}"
    result = _execute_untruncated(source, command)
    if (
        result.get("success") is False
        or type(result.get("exit_code")) is not int
        or result["exit_code"] != 0
        or result.get("dispatch_status")
    ):
        raise ContainerFileReadError(f"bounded container read did not succeed for {path}")
    output = result.get("output")
    # Bound the encoded reply before decoding. This is a single prefix frame,
    # not an arbitrary successful command whose text may be accepted as data.
    max_output_bytes = ((max_bytes + 2) // 3 * 4) + len(str(max_bytes)) + 128
    if not isinstance(output, str) or len(output) > max_output_bytes:
        raise ContainerFileReadError(f"bounded container read returned invalid frame for {path}")
    lines = output.removesuffix("\n").split("\n")
    if len(lines) != 2 or lines[1] != _PREFIX_END_MARKER:
        raise ContainerFileReadError(f"bounded container read returned invalid frame for {path}")
    fields = lines[0].split("\t")
    if (
        len(fields) != 5
        or fields[0] != _PREFIX_MARKER
        or re.fullmatch(r"0|[1-9][0-9]*", fields[1]) is None
        or len(fields[1]) > len(str(max_bytes))
        or re.fullmatch(r"[0-9a-f]{64}", fields[2]) is None
        or fields[3] not in ("0", "1")
    ):
        raise ContainerFileReadError(f"bounded container read returned invalid frame for {path}")
    byte_count = int(fields[1])
    has_more = fields[3] == "1"
    if byte_count > max_bytes or (has_more and byte_count != max_bytes):
        raise ContainerFileReadError(f"bounded container read exceeded its budget for {path}")
    try:
        raw = base64.b64decode(fields[4], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ContainerFileReadError(
            f"bounded container read returned invalid payload for {path}"
        ) from exc
    if (
        len(raw) != byte_count
        or hashlib.sha256(raw).hexdigest() != fields[2]
        or base64.b64encode(raw).decode("ascii") != fields[4]
    ):
        raise ContainerFileReadError(f"bounded container read returned invalid payload for {path}")
    return raw, has_more


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
        dispatch_status = result.get("dispatch_status")
        exit_code = result.get("exit_code")
        if (
            type(exit_code) is int
            and exit_code == 44
            and result.get("success") is False
            and not dispatch_status
            and output == _MISSING_MARKER
        ):
            return None
        if output.startswith(_PAYLOAD_MARKER):
            if not _command_succeeded(result) or dispatch_status:
                raise ContainerFileReadError(
                    f"lossless container read did not succeed for {path}: "
                    f"{str(result.get('output') or '')[:120]}"
                )
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
