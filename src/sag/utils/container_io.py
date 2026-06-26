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

from loguru import logger

# Stay well under the kernel's per-arg limit (MAX_ARG_STRLEN ~= 131072).
DEFAULT_MAX_CMD_CHARS = 60000


def _ok(result) -> bool:
    return bool(result.get("exit_code") == 0 or result.get("success"))


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
    if len(content) <= max_cmd_chars:
        delimiter = _heredoc_delimiter(content)
        operator = ">>" if append else ">"
        command = f"cat {operator} {path} <<'{delimiter}'\n{content}\n{delimiter}"
        result = orchestrator.execute_command(command)
        if _ok(result):
            return True
        logger.error(f"Failed to write container file {path}: {result.get('output')}")
        return False

    encoded = base64.b64encode(content.encode("utf-8", errors="replace")).decode("ascii")
    tmp = f"{path}.b64.{hashlib.md5(encoded.encode()).hexdigest()[:8]}.tmp"

    if not _ok(orchestrator.execute_command(f"rm -f {tmp}")):
        logger.error(f"Failed to reset temp file {tmp}")
        return False

    for i in range(0, len(encoded), max_cmd_chars):
        chunk = encoded[i : i + max_cmd_chars]
        # base64's alphabet ([A-Za-z0-9+/=]) is safe inside single quotes.
        if not _ok(orchestrator.execute_command(f"printf '%s' '{chunk}' >> {tmp}")):
            logger.error(f"Failed to append chunk to {tmp}")
            orchestrator.execute_command(f"rm -f {tmp}")
            return False

    operator = ">>" if append else ">"
    finalize = f"base64 -d {tmp} {operator} {path} && printf '\\n' >> {path} && rm -f {tmp}"
    if _ok(orchestrator.execute_command(finalize)):
        return True
    logger.error(f"Failed to finalize chunked write to {path}")
    orchestrator.execute_command(f"rm -f {tmp}")
    return False
