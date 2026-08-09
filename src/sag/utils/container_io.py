"""Robust text-file writes into the container.

Writing a file by embedding its whole contents in a single shell command
(`cat > f << 'EOF' ... EOF`) breaks once the payload is large: Linux caps a
single argv element at MAX_ARG_STRLEN (~128 KB), so big build logs and
accumulated branch-history JSON fail with ``exec /bin/bash: argument list too
long``. This helper keeps the fast single-command heredoc for small content and
streams large content as length-bounded base64 chunks. It only needs the
orchestrator's ``execute_command`` so the same code works under the test fakes.
"""

import base64
import hashlib
import posixpath
import shlex
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Optional, cast

from loguru import logger
from sag.runtime.container_io import resolve_control_execute

# Stay well under the kernel's per-arg limit (MAX_ARG_STRLEN ~= 131072).
DEFAULT_MAX_CMD_CHARS = 60000
# Task 1.1's hard transport ceiling includes the shell syntax around a base64
# chunk, not only the chunk itself.
MAX_CONTAINER_COMMAND_CHARS = 60200

WRITE_PERSISTED = "persisted"
WRITE_INVALID_ARGUMENTS = "invalid_arguments"
WRITE_TRANSPORT_FAILED = "transport_write_failed"
WRITE_VALIDATION_FAILED = "transport_validation_failed"
WRITE_PUBLISH_FAILED = "transport_publish_failed"
WRITE_COMPARE_CONFLICT = "compare_conflict"


@dataclass(frozen=True)
class ContainerWriteResult:
    """Outcome of one exact, atomic container write."""

    persisted: bool
    code: str
    bytes_written: int = 0
    sha256: str = ""


def _ok(result) -> bool:
    if not isinstance(result, Mapping) or result.get("dispatch_status"):
        return False
    exit_code = result.get("exit_code")
    if exit_code is not None:
        return bool(
            isinstance(exit_code, int)
            and not isinstance(exit_code, bool)
            and exit_code == 0
            and result.get("success") is not False
        )
    return result.get("success") is True


def _resolve_execute(target: Any) -> Optional[Callable[..., Any]]:
    return cast(Optional[Callable[..., Any]], resolve_control_execute(target))


def _run_atomic_command(execute: Callable[..., Any], command: str) -> bool:
    """Run one bounded command without allowing transport errors to escape."""
    if len(command) > MAX_CONTAINER_COMMAND_CHARS:
        logger.error(
            "Refusing oversized container write command "
            f"({len(command)} > {MAX_CONTAINER_COMMAND_CHARS})"
        )
        return False
    try:
        return _ok(execute(command) or {})
    except Exception as exc:
        logger.debug(f"Container write command failed: {type(exc).__name__}: {exc}")
        return False


def write_container_text_atomic(
    execute,
    path: str,
    content: str,
    *,
    max_cmd_chars: int = DEFAULT_MAX_CMD_CHARS,
    validate_json: bool = False,
) -> ContainerWriteResult:
    """Persist exact UTF-8 text through a validated atomic rename.

    ``execute`` may be either an execute callback or an orchestrator exposing
    ``execute_command``. The final path is never opened for writing: all bytes
    are assembled and validated under unique temporary names, then published
    with one ``mv -f``.
    """
    executor = _resolve_execute(execute)
    if (
        executor is None
        or not isinstance(path, str)
        or not path
        or "\x00" in path
        or not isinstance(content, str)
        or not isinstance(max_cmd_chars, int)
        or isinstance(max_cmd_chars, bool)
        or max_cmd_chars <= 0
    ):
        return ContainerWriteResult(False, WRITE_INVALID_ARGUMENTS)

    # This is the only host serialization of caller-owned content. Every
    # subsequent operation consumes these exact bytes or their base64 form.
    try:
        payload = content.encode("utf-8")
    except UnicodeEncodeError:
        return ContainerWriteResult(False, WRITE_INVALID_ARGUMENTS)
    expected_bytes = len(payload)
    expected_sha256 = hashlib.sha256(payload).hexdigest()
    encoded = base64.b64encode(payload).decode("ascii")

    # Include fresh entropy in the one filename component so two concurrent
    # writes of identical content cannot share either staging file.
    temp_digest = f"{expected_sha256[:16]}{uuid.uuid4().hex[:16]}"
    encoded_tmp = f"{path}.b64.{temp_digest}.tmp"
    decoded_tmp = f"{path}.{temp_digest}.tmp"
    parent = posixpath.dirname(path) or "."

    quoted_parent = shlex.quote(parent)
    quoted_encoded = shlex.quote(encoded_tmp)
    quoted_decoded = shlex.quote(decoded_tmp)
    quoted_final = shlex.quote(path)

    mkdir_command = f"mkdir -p -- {quoted_parent}"
    initialize_command = f": > {quoted_encoded}"
    decode_command = f"base64 --decode {quoted_encoded} > {quoted_decoded}"
    byte_sha_program = (
        'import hashlib,sys;data=open(sys.argv[1],"rb").read();'
        "raise SystemExit(len(data)!=int(sys.argv[2]) or "
        "hashlib.sha256(data).hexdigest()!=sys.argv[3])"
    )
    validate_command = (
        f"python3 -c {shlex.quote(byte_sha_program)} {quoted_decoded} "
        f"{expected_bytes} {expected_sha256}"
    )
    json_program = 'import json,sys;json.load(open(sys.argv[1],encoding="utf-8"))'
    json_command = f"python3 -c {shlex.quote(json_program)} {quoted_decoded}"
    remove_encoded_command = f"rm -f -- {quoted_encoded}"
    cleanup_command = f"rm -f -- {quoted_encoded} {quoted_decoded}"
    publish_command = f"mv -f -- {quoted_decoded} {quoted_final}"

    fixed_commands = [
        mkdir_command,
        initialize_command,
        decode_command,
        validate_command,
        remove_encoded_command,
        cleanup_command,
        publish_command,
    ]
    if validate_json:
        fixed_commands.append(json_command)
    if any(len(command) > MAX_CONTAINER_COMMAND_CHARS for command in fixed_commands):
        return ContainerWriteResult(False, WRITE_INVALID_ARGUMENTS)

    append_prefix = "printf '%s' '"
    append_suffix = f"' >> {quoted_encoded}"
    chunk_chars = min(
        max_cmd_chars,
        MAX_CONTAINER_COMMAND_CHARS - len(append_prefix) - len(append_suffix),
    )
    if chunk_chars <= 0:
        return ContainerWriteResult(False, WRITE_INVALID_ARGUMENTS)

    def cleanup() -> None:
        _run_atomic_command(executor, cleanup_command)

    if not _run_atomic_command(executor, mkdir_command):
        return ContainerWriteResult(False, WRITE_TRANSPORT_FAILED)
    if not _run_atomic_command(executor, initialize_command):
        cleanup()
        return ContainerWriteResult(False, WRITE_TRANSPORT_FAILED)

    for offset in range(0, len(encoded), chunk_chars):
        chunk = encoded[offset : offset + chunk_chars]
        # Base64's alphabet is safe inside a single-quoted shell word.
        command = f"{append_prefix}{chunk}{append_suffix}"
        if not _run_atomic_command(executor, command):
            cleanup()
            return ContainerWriteResult(False, WRITE_TRANSPORT_FAILED)

    if not _run_atomic_command(executor, decode_command):
        cleanup()
        return ContainerWriteResult(False, WRITE_TRANSPORT_FAILED)
    if not _run_atomic_command(executor, validate_command):
        cleanup()
        return ContainerWriteResult(False, WRITE_VALIDATION_FAILED)
    if validate_json and not _run_atomic_command(executor, json_command):
        cleanup()
        return ContainerWriteResult(False, WRITE_VALIDATION_FAILED)

    # Dispose of the encoded transport before publication. If cleanup itself
    # fails, the validated decoded file is removed and the old final survives.
    if not _run_atomic_command(executor, remove_encoded_command):
        cleanup()
        return ContainerWriteResult(False, WRITE_TRANSPORT_FAILED)
    if not _run_atomic_command(executor, publish_command):
        cleanup()
        return ContainerWriteResult(False, WRITE_PUBLISH_FAILED)

    return ContainerWriteResult(
        True,
        WRITE_PERSISTED,
        bytes_written=expected_bytes,
        sha256=expected_sha256,
    )


def compare_publish_container_text_atomic(
    execute,
    path: str,
    content: str,
    *,
    expected_content: Optional[str],
    max_cmd_chars: int = DEFAULT_MAX_CMD_CHARS,
    validate_json: bool = False,
) -> ContainerWriteResult:
    """Publish exact text only if ``path`` still has the bytes the caller read.

    Whole-file read/modify/write users cannot be made safe by an atomic
    ``rename`` alone: two writers may both read the same old body and the later
    rename then loses the first update.  This helper first stages and validates
    the candidate with :func:`write_container_text_atomic`, then takes an
    advisory lock and performs the compare plus ``os.replace`` inside one
    container process.  Every caller for a given ``path`` uses the same lock;
    a crashed process releases it in the kernel.

    ``expected_content=None`` means the caller proved the target absent.  A
    compare conflict is a normal optimistic-concurrency outcome and is
    reported distinctly so the caller can re-read, re-derive, and retry.
    """

    executor = _resolve_execute(execute)
    if (
        executor is None
        or not isinstance(path, str)
        or not path
        or "\x00" in path
        or not isinstance(content, str)
        or (expected_content is not None and not isinstance(expected_content, str))
    ):
        return ContainerWriteResult(False, WRITE_INVALID_ARGUMENTS)
    try:
        expected_token = (
            "absent"
            if expected_content is None
            else "sha256:" + hashlib.sha256(expected_content.encode("utf-8")).hexdigest()
        )
    except UnicodeEncodeError:
        return ContainerWriteResult(False, WRITE_INVALID_ARGUMENTS)

    candidate = f"{path}.candidate.{uuid.uuid4().hex}.tmp"
    staged = write_container_text_atomic(
        # Preserve the original source classification. Passing the already
        # resolved bound method back through the resolver would make a narrow
        # object-level test-double fallback look like an unsafe bound normal
        # callback and reject it on the second pass.
        execute,
        candidate,
        content,
        max_cmd_chars=max_cmd_chars,
        validate_json=validate_json,
    )
    if not staged.persisted:
        return staged

    lock_path = f"{path}.update.lock"
    program = """import fcntl,hashlib,os,sys
target,candidate,lock_path,expected,expected_bytes,expected_sha=sys.argv[1:7]
with open(lock_path,"a+b") as lock:
    fcntl.flock(lock,fcntl.LOCK_EX)
    candidate_data=open(candidate,"rb").read()
    if len(candidate_data) != int(expected_bytes) or hashlib.sha256(candidate_data).hexdigest() != expected_sha:
        print("SAG_CAS_CANDIDATE_INVALID")
        raise SystemExit(76)
    try:
        with open(target,"rb") as source:
            actual="sha256:"+hashlib.sha256(source.read()).hexdigest()
    except FileNotFoundError:
        actual="absent"
    if actual != expected:
        print("SAG_CAS_CONFLICT")
        raise SystemExit(75)
    os.replace(candidate,target)
"""
    command = (
        f"python3 -c {shlex.quote(program)} {shlex.quote(path)} "
        f"{shlex.quote(candidate)} {shlex.quote(lock_path)} {shlex.quote(expected_token)} "
        f"{staged.bytes_written} {staged.sha256}"
    )
    cleanup = f"rm -f -- {shlex.quote(candidate)}"
    if len(command) > MAX_CONTAINER_COMMAND_CHARS or len(cleanup) > MAX_CONTAINER_COMMAND_CHARS:
        _run_atomic_command(executor, cleanup)
        return ContainerWriteResult(False, WRITE_INVALID_ARGUMENTS)
    try:
        result = executor(command) or {}
    except Exception as exc:
        logger.debug(f"Container compare-publish failed: {type(exc).__name__}: {exc}")
        _run_atomic_command(executor, cleanup)
        return ContainerWriteResult(False, WRITE_PUBLISH_FAILED)
    if _ok(result):
        return staged

    _run_atomic_command(executor, cleanup)
    exit_code = result.get("exit_code") if isinstance(result, Mapping) else None
    output = str(result.get("output") or "") if isinstance(result, Mapping) else ""
    if (
        exit_code == 75
        and output.strip() == "SAG_CAS_CONFLICT"
        and not result.get("dispatch_status")
    ):
        return ContainerWriteResult(False, WRITE_COMPARE_CONFLICT)
    return ContainerWriteResult(False, WRITE_PUBLISH_FAILED)


def _heredoc_delimiter(content: str) -> str:
    digest = hashlib.md5(content.encode()).hexdigest()[:12]
    delimiter = f"SAG_FILE_EOF_{digest}"
    while f"\n{delimiter}\n" in f"\n{content}\n":
        digest = hashlib.md5(f"{content}{delimiter}".encode()).hexdigest()[:12]
        delimiter = f"SAG_FILE_EOF_{digest}"
    return delimiter


def write_container_text(
    orchestrator,
    path: str,
    content: str,
    *,
    append: bool = False,
    max_cmd_chars: int = DEFAULT_MAX_CMD_CHARS,
) -> bool:
    """Write ``content`` to container file ``path`` (a trusted internal path).

    A trailing newline is always written (so JSONL records stay one line each).
    Content over ``max_cmd_chars`` is streamed as base64 chunks to avoid the
    kernel per-arg limit. Returns True on success.
    """
    executor = _resolve_execute(orchestrator)
    if executor is None:
        logger.error(f"No clean container transport is available for {path}")
        return False
    if len(content) <= max_cmd_chars:
        delimiter = _heredoc_delimiter(content)
        operator = ">>" if append else ">"
        command = f"cat {operator} {path} <<'{delimiter}'\n{content}\n{delimiter}"
        result = executor(command)
        if _ok(result):
            return True
        logger.error(f"Failed to write container file {path}: {result.get('output')}")
        return False

    encoded = base64.b64encode(content.encode("utf-8", errors="replace")).decode("ascii")
    tmp = f"{path}.b64.{hashlib.md5(encoded.encode()).hexdigest()[:8]}.tmp"

    if not _ok(executor(f"rm -f {tmp}")):
        logger.error(f"Failed to reset temp file {tmp}")
        return False

    for i in range(0, len(encoded), max_cmd_chars):
        chunk = encoded[i : i + max_cmd_chars]
        # base64's alphabet ([A-Za-z0-9+/=]) is safe inside single quotes.
        if not _ok(executor(f"printf '%s' '{chunk}' >> {tmp}")):
            logger.error(f"Failed to append chunk to {tmp}")
            executor(f"rm -f {tmp}")
            return False

    operator = ">>" if append else ">"
    finalize = f"base64 -d {tmp} {operator} {path} && printf '\\n' >> {path} && rm -f {tmp}"
    if _ok(executor(finalize)):
        return True
    logger.error(f"Failed to finalize chunked write to {path}")
    executor(f"rm -f {tmp}")
    return False
