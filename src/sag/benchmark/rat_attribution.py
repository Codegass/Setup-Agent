"""Conservative attribution of RAT-rejected untracked files.

This explains an already established quality-check failure; it never changes its
status. A new filename alone does not identify its author. The current recorders
without trusted write events therefore return unknown, as historical runs do.
"""

from __future__ import annotations

import posixpath
import re
from datetime import datetime
import xml.etree.ElementTree as ET

from .requirements import bound_file, load_json


def _time(value):
    if not isinstance(value, str):
        raise ValueError("Write provenance timestamp unavailable")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Write provenance needs timezone-aware timestamps")
    return parsed


def _snapshot(base, ref, run_id):
    snapshot = load_json(bound_file(base, ref))
    if snapshot.get("run_id") != run_id or not snapshot.get("project_root"):
        raise ValueError("Worktree snapshot subject differs")
    raw = {}
    expected = {
        "head": ["rev-parse", "HEAD"],
        "tracked": ["diff", "--name-only", "-z", "HEAD", "--"],
        "untracked": ["ls-files", "--others", "--exclude-standard", "-z"],
    }
    for key, args in expected.items():
        probe = snapshot["probes"][key]
        if probe.get("argv") != ["git", "-C", snapshot["project_root"], *args]:
            raise ValueError("Worktree probe is not bound to the checkout")
        if type(probe.get("exit_code")) is not int or probe["exit_code"] != 0:
            raise ValueError("Worktree probe failed")
        raw[key] = bound_file(base, probe["stdout"]).read_bytes()
        bound_file(base, probe["stderr"])
    head = raw["head"].decode("ascii").strip()
    if re.fullmatch(r"[0-9a-f]{40}", head) is None:
        raise ValueError("Invalid checkout revision")
    if any(raw[key] and not raw[key].endswith(b"\0") for key in ("tracked", "untracked")):
        raise ValueError("Incomplete worktree path listing")
    paths = {p.decode("utf-8", "surrogateescape") for p in raw["untracked"].split(b"\0") if p}
    return snapshot, head, paths


def _relative(path, root):
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ValueError("Invalid rejected path")
    if posixpath.isabs(path):
        path = posixpath.relpath(path, root)
    if path == ".." or path.startswith("../") or posixpath.normpath(path) != path:
        raise ValueError("RAT path escapes checkout")
    return path


def attribute_rat_failure(base, run_id, before_ref, after_ref, write_events_ref, rat_report_ref):
    """Return origin plus paths and references, requiring every attribution link.

    ``rat_report_ref`` names a hashed JSON envelope with run_id, project_root,
    invocation_id, a byte-bound ``report`` (RAT XML), and ``file_contents`` entries
    {project_relative_path, content: {path, sha256}} retained at RAT execution.
    ``write_events_ref`` names trusted_write_observer JSON, coverage=complete,
    run_id/project_root and events with path/origin/observed_at/content refs.
    Never reconstruct rejected content from the current, possibly changed tree.
    """
    output = {
        "origin": "unknown",
        "reason": "attribution_evidence_unavailable",
        "paths": [],
        "evidence_refs": [
            r for r in (before_ref, after_ref, write_events_ref, rat_report_ref) if r
        ],
    }
    try:
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("Run identity unavailable")
        before, before_head, initial = _snapshot(base, before_ref, run_id)
        after, after_head, final = _snapshot(base, after_ref, run_id)
        root = before["project_root"]
        if after["project_root"] != root or before_head != after_head:
            raise ValueError("Worktree boundary subject changed")
        if before.get("boundary") not in {"task_start", "acceptance_before"} or after.get(
            "boundary"
        ) not in {"acceptance_after", "evidence_close"}:
            raise ValueError("Not before/after execution boundaries")
        started, ended = _time(before["observed_at"]), _time(after["observed_at"])
        if started > ended:
            raise ValueError("Reversed worktree boundaries")
        report = load_json(bound_file(base, rat_report_ref))
        if report.get("run_id") != run_id or report.get("project_root") != root:
            raise ValueError("RAT report belongs to another execution")
        for snapshot in (before, after):
            identity = snapshot.get("invocation_id")
            if identity and identity != report.get("invocation_id"):
                raise ValueError("RAT report invocation mismatch")
        rejected = set()
        xml = ET.parse(bound_file(base, report["report"]))
        for resource in xml.iter():
            if resource.tag.rsplit("}", 1)[-1] != "resource":
                continue
            if any(
                c.tag.rsplit("}", 1)[-1] == "license-approval" and c.get("name") == "false"
                for c in resource
            ):
                rejected.add(_relative(resource.get("name"), root))
        if not rejected:
            raise ValueError("RAT report does not identify rejected files")
        if not write_events_ref:
            output.update(
                reason="write_origin_unavailable",
                paths=[{"path": p, "origin": "unknown"} for p in sorted(rejected)],
            )
            return output
        writes = load_json(bound_file(base, write_events_ref))
        if (
            writes.get("run_id") != run_id
            or writes.get("project_root") != root
            or writes.get("source") != "trusted_write_observer"
            or writes.get("coverage") != "complete"
            or not isinstance(writes.get("events"), list)
        ):
            raise ValueError("Write attribution is not a complete trusted observation")
        evidence_contents = {}
        for item in report.get("file_contents", []):
            path = _relative(item["project_relative_path"], root)
            if path in evidence_contents:
                raise ValueError("Ambiguous rejected file content")
            bound_file(base, item["content"])
            evidence_contents[path] = item["content"]["sha256"]
        for path in sorted(rejected):
            item = {
                "path": path,
                "origin": "unknown",
                "reason": "file_or_write_provenance_unavailable",
            }
            if path in initial:
                item["reason"] = "present_before_execution"
            elif path not in final:
                item["reason"] = "not_observed_untracked_after_execution"
            else:
                events = [e for e in writes["events"] if _relative(e.get("path"), root) == path]
                if events and path in evidence_contents:
                    timed = [(_time(e["observed_at"]), e) for e in events]
                    timed.sort(key=lambda pair: pair[0])
                    if any(t < started or t > ended for t, _ in timed):
                        raise ValueError("Write event falls outside observed interval")
                    last_time, last = timed[-1]
                    if sum(t == last_time for t, _ in timed) != 1:
                        raise ValueError("Ambiguous final write order")
                    bound_file(base, last["content"])
                    if (
                        last.get("origin") in {"agent", "harness"}
                        and last["content"]["sha256"] == evidence_contents[path]
                    ):
                        item.update(
                            origin=last["origin"], reason="new_file_write_and_rat_content_bound"
                        )
            output["paths"].append(item)
        origins = {p["origin"] for p in output["paths"]}
        if len(origins) == 1 and "unknown" not in origins:
            output.update(origin=origins.pop(), reason="rejected_files_attributed")
        else:
            output["reason"] = "mixed_or_unattributed_rejected_files"
    except (
        KeyError,
        ValueError,
        TypeError,
        AttributeError,
        OSError,
        UnicodeError,
        ET.ParseError,
    ) as exc:
        output.update(origin="unknown", reason=str(exc), paths=[])
    return output
