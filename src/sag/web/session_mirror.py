"""Host mirror of a container's result files, so the web UI can serve finished
sessions from disk instead of re-`docker exec`-ing into every container on every
5s dashboard poll (which also revived stopped containers).

`ensure_mirror` copies the result paths out with `container.get_archive` — no exec,
works on a stopped container, never revives it. `MirrorReader` then answers the
exact `cat`/`find` shapes `session_registry`'s read helpers emit, reading from the
mirror, so those helpers stay unchanged.
"""

from __future__ import annotations

import fnmatch
import io
import re
import shlex
import tarfile
import time
from pathlib import Path
from typing import Any, Callable

from loguru import logger

# Result paths mirrored from the container (all under /workspace). `.setup_agent`
# holds sessions/index.json, contexts/, report_metrics.json, module_metrics.json.
_ARCHIVE_PATHS = ("/workspace/.setup_agent", "/workspace/.sag_last_comment.json")
_REPORT_RE = re.compile(r"setup-report(?:-\d{8}-\d{6})?\.md")
_STORE_IDENTITY_FILE = ".evidence-store-identity"
RUNNING_TTL_SECONDS = 10.0

_last_fetch: dict[str, float] = {}


def mirror_root(logs_root: Path) -> Path:
    return logs_root / "web_mirror"


def ensure_mirror(
    client: Any,
    container_name: str,
    running: bool,
    logs_root: Path,
    now: Callable[[], float] = time.monotonic,
) -> Path | None:
    """Return the host mirror dir for a container, refreshing it when needed.

    Stopped container -> mirrored once (results are immutable). Running -> refetched
    when older than RUNNING_TTL_SECONDS. Returns None only when nothing was ever
    mirrored and the fetch fails.
    """
    dest = mirror_root(logs_root) / container_name
    fetched = _last_fetch.get(container_name)
    if dest.exists() and fetched is not None and (not running or now() - fetched < RUNNING_TTL_SECONDS):
        return dest

    try:
        container = client.containers.get(container_name)
    except Exception:
        return dest if dest.exists() else None

    dest.mkdir(parents=True, exist_ok=True)
    for path in _ARCHIVE_PATHS:
        _extract(container, path, dest)
    _extract_report(container, dest)
    _write_store_identity(container, dest)
    _last_fetch[container_name] = now()
    return dest


def _write_store_identity(container: Any, dest: Path) -> None:
    """Persist Docker's host-observed immutable id beside the mirror.

    The mirrored result files are container bytes. Live evidence readers must
    still prove those bytes came from the store the host control ledger bound
    to this run, so the reader cannot identify itself by its Python object id.
    This sidecar is written from the Docker API and is not part of the archive.
    """

    container_id = str(getattr(container, "id", "") or "").strip()
    if not container_id:
        return
    try:
        (dest / _STORE_IDENTITY_FILE).write_text(
            f"docker:{container_id}\n",
            encoding="utf-8",
        )
    except OSError as exc:
        # A missing identity makes authority checks fail closed. The mirror is
        # still useful for non-authoritative context, logs, and trajectories.
        logger.debug("mirror store identity write failed for {}: {}", dest, exc)


class _ChunkReader(io.RawIOBase):
    """File-like over get_archive's chunk generator, so tarfile streams the archive
    instead of us materializing the whole thing in RAM (a session's
    full_outputs.jsonl can be large, times every container on a dashboard load)."""

    def __init__(self, chunks: Any):
        self._chunks = iter(chunks)
        self._buf = b""

    def readable(self) -> bool:
        return True

    def readinto(self, b: bytearray) -> int:
        while not self._buf:
            try:
                self._buf = next(self._chunks)
            except StopIteration:
                return 0
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n


def _extract(container: Any, container_path: str, dest: Path) -> None:
    try:
        stream, _ = container.get_archive(container_path)
    except Exception:
        return  # missing path (NotFound) or unreachable container — skip
    try:
        with tarfile.open(fileobj=io.BufferedReader(_ChunkReader(stream)), mode="r|") as tar:
            tar.extractall(dest, filter="data")
    except Exception as exc:
        logger.debug("mirror extract failed for {}: {}", container_path, exc)


def _extract_report(container: Any, dest: Path) -> None:
    """Fetch reports named by any JSON context, with the generic report as fallback.

    A completed run can record its report only in ``phase_report.json`` rather
    than the trunk.  The generic ``setup-report.md`` name is also still emitted
    by older report flows, so try it even when no context names it.
    """
    contexts = dest / ".setup_agent" / "contexts"
    names: set[str] = {"setup-report.md"}
    if contexts.is_dir():
        for f in contexts.rglob("*.json"):
            try:
                names.update(_REPORT_RE.findall(f.read_text(encoding="utf-8", errors="ignore")))
            except OSError:
                continue
    for name in names:
        _extract(container, f"/workspace/{name}", dest)


class MirrorReader:
    """Answers the `cat`/`find` commands session_registry issues, from the mirror.

    ponytail: recognizes only the exact command shapes SAG's own read helpers emit
    (cat <path>, the contexts find, the setup-report find) — it's an internal read
    path, not a general shell. Add a branch here if a helper grows a new shape.
    """

    _CONTEXT_GLOBS = ("trunk*.json", "phase_*.json", "full_outputs.jsonl")

    def __init__(self, mirror: Path):
        self.mirror = mirror

    def evidence_store_identity(self) -> str:
        """Return the immutable store identity observed by the host mirror."""

        try:
            value = (self.mirror / _STORE_IDENTITY_FILE).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        return value if value.startswith("docker:") and len(value) > len("docker:") else ""

    def execute_command(self, command: str, timeout: int | None = None, **_: Any) -> dict[str, Any]:
        if command.startswith("file="):
            return self._named_json_file_stream(command)
        if command.startswith("cat "):
            return self._cat(command)
        if "setup-report-*.md" in command:
            return self._latest_timestamped_report()
        if "setup-report.md" in command:
            return self._generic_report()
        if "/contexts" in command and command.startswith("find "):
            return self._context_files()
        return {"output": "", "exit_code": 1, "success": False}

    def _named_json_file_stream(self, command: str) -> dict[str, Any]:
        """Answer the exact bounded transport used by live artifact readers."""

        from sag.agent.evidence_records import (
            frame_named_json_record_stream,
            named_json_file_stream_command,
        )

        assignment, separator, _ = command.partition("; count=0;")
        if not separator or not assignment.startswith("file="):
            return {"output": "", "exit_code": 1, "success": False}
        try:
            paths = shlex.split(assignment.removeprefix("file="))
        except ValueError:
            return {"output": "", "exit_code": 1, "success": False}
        if len(paths) != 1 or command != named_json_file_stream_command(paths[0]):
            return {"output": "", "exit_code": 1, "success": False}

        target = self._host(paths[0])
        records: list[tuple[str, bytes]] = []
        if target.is_file():
            try:
                records.append((target.name, target.read_bytes()))
            except OSError:
                return {"output": "", "exit_code": 1, "success": False}
        return {
            "output": frame_named_json_record_stream(records),
            "exit_code": 0,
            "success": True,
        }

    def _host(self, container_path: str) -> Path:
        return self.mirror / container_path.removeprefix("/workspace/").lstrip("/")

    def _cat(self, command: str) -> dict[str, Any]:
        parts = shlex.split(command.replace(" 2>/dev/null", ""))
        target = self._host(parts[1]) if len(parts) > 1 else None
        if target and target.is_file():
            try:
                return {"output": target.read_text(encoding="utf-8", errors="ignore"),
                        "exit_code": 0, "success": True}
            except OSError:
                pass
        return {"output": "", "exit_code": 1, "success": False}

    def _latest_timestamped_report(self) -> dict[str, Any]:
        reports = sorted(self.mirror.glob("setup-report-*.md"))
        out = f"/workspace/{reports[-1].name}" if reports else ""
        return {"output": out, "exit_code": 0, "success": True}

    def _generic_report(self) -> dict[str, Any]:
        report = self.mirror / "setup-report.md"
        out = "/workspace/setup-report.md" if report.is_file() else ""
        return {"output": out, "exit_code": 0, "success": True}

    def _context_files(self) -> dict[str, Any]:
        base = self.mirror / ".setup_agent" / "contexts"
        found: list[str] = []
        if base.is_dir():
            for p in base.rglob("*"):
                if not p.is_file():
                    continue
                rel = p.relative_to(base)
                if len(rel.parts) > 2:
                    continue  # -maxdepth 2
                name = p.name
                journal = p.match("journal/phase_*.journal.jsonl")
                if any(fnmatch.fnmatch(name, g) for g in self._CONTEXT_GLOBS) or journal:
                    found.append(str(rel))
        return {"output": "\n".join(found), "exit_code": 0, "success": True}
