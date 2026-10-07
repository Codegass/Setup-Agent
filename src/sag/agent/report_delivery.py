"""Bind report delivery to exact persisted bytes and the authoritative seal.

This is delivery evidence, not a second setup verdict. The engine receives the
binding from ReportTool, never from the model or a workspace filename search.
"""

import hashlib
import re
from collections.abc import Mapping

from sag.runtime.container_io import read_container_text

from .control_events import canonical_sha256
from .verdict_finalizer import read_live_verdict_snapshot


def report_delivery_binding(snapshot, path: str, content: str) -> dict[str, str]:
    return {
        "run_id": snapshot.run_id,
        "snapshot_sha256": canonical_sha256(snapshot.model_dump(mode="json")),
        "path": path,
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def verify_report_delivery(orchestrator, binding: Mapping) -> None:
    """Raise on missing, stale, tampered or unpersisted delivery evidence."""
    if not isinstance(binding, Mapping):
        raise ValueError("report delivery binding is missing")
    path = binding.get("path")
    if (
        not isinstance(path, str)
        or re.fullmatch(r"/workspace/setup-report-[\w.-]+\.md", path) is None
    ):
        raise ValueError("report delivery path is invalid")
    snapshot = read_live_verdict_snapshot(orchestrator)
    if binding.get("run_id") != snapshot.run_id or binding.get(
        "snapshot_sha256"
    ) != canonical_sha256(snapshot.model_dump(mode="json")):
        raise ValueError("report belongs to a different sealed snapshot")
    content = read_container_text(orchestrator, path, exact_bytes=True)
    if not content or hashlib.sha256(content.encode("utf-8")).hexdigest() != binding.get("sha256"):
        raise ValueError("report bytes are missing or differ from the delivered artifact")
