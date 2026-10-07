"""Acquire only frozen source-gap evidence; never alter old archives or run code."""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any

from scripts.benchmark_source_audit import blob_sha, compare_tree, digest, safe_ref, write_json


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_ref(path: Path, base: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(base).as_posix(),
        "sha256": digest(path),
        "bytes": path.stat().st_size,
    }


def verify_ref(base: Path, value: dict[str, Any]) -> Path:
    path = safe_ref(base, value["path"])
    if path.stat().st_size != value["bytes"] or digest(path) != value["sha256"]:
        raise ValueError("Evidence reference bytes changed: " + value["path"])
    return path


def split_http(raw: bytes) -> tuple[int | None, bytes]:
    match = re.match(rb"HTTP/\S+\s+(\d{3})[^\r\n]*\r?\n", raw)
    if not match:
        return None, raw
    separator = b"\r\n\r\n" if b"\r\n\r\n" in raw else b"\n\n"
    pieces = raw.split(separator, 1)
    return int(match[1]), pieces[1] if len(pieces) == 2 else b""


def capture(endpoint: str, directory: Path, root: Path, fetch: bool) -> tuple[dict[str, Any], Any]:
    """Capture CLI-authenticated public GET, preserving raw bytes and HTTP status.

    Credentials stay in gh's existing credential provider. Debug logging is
    disabled; no environment, request Authorization headers or prompts are saved.
    """
    url = "https://api.github.com/" + endpoint
    directory.mkdir(parents=True, exist_ok=True)
    previous = sorted(directory.glob("attempt-*/receipt.json"))
    for receipt_path in previous:
        receipt = json.loads(receipt_path.read_bytes())
        if receipt.get("url") != url:
            raise ValueError("Capture cache endpoint changed")
        body_path = verify_ref(root, receipt["body"])
        raw_path = verify_ref(root, receipt["raw_response"])
        verify_ref(root, receipt["stderr"])
        recorded_status, recorded_body = split_http(raw_path.read_bytes())
        if (
            receipt.get("method") != "GET"
            or recorded_status != receipt.get("http_status")
            or recorded_body != body_path.read_bytes()
        ):
            raise ValueError("Capture receipt contradicts its raw HTTP response")
        if receipt.get("http_status") == 200 and receipt.get("returncode") == 0:
            return {"receipt": file_ref(receipt_path, root), **receipt}, json.loads(
                body_path.read_bytes()
            )
    if not fetch:
        raise ValueError("Successful cached capture unavailable: " + endpoint)
    attempt = directory / f"attempt-{len(previous) + 1:03d}"
    attempt.mkdir()
    started, monotonic = now(), time.monotonic()
    argv = [
        "gh",
        "api",
        "--hostname",
        "github.com",
        "--method",
        "GET",
        "--include",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2022-11-28",
        endpoint,
    ]
    env = dict(os.environ)
    env.pop("GH_DEBUG", None)
    env.pop("DEBUG", None)
    try:
        proc = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=60, check=False
        )
        raw, stderr, returncode = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        raw, stderr, returncode = exc.stdout or b"", exc.stderr or b"", None
    status, body = split_http(raw)
    (attempt / "response.http").write_bytes(raw)
    (attempt / "body.json").write_bytes(body)
    (attempt / "stderr.txt").write_bytes(stderr)
    receipt = {
        "url": url,
        "method": "GET",
        "requested_at": started,
        "completed_at": now(),
        "elapsed_seconds": time.monotonic() - monotonic,
        "http_status": status,
        "returncode": returncode,
        "body": file_ref(attempt / "body.json", root),
        "raw_response": file_ref(attempt / "response.http", root),
        "stderr": file_ref(attempt / "stderr.txt", root),
    }
    write_json(attempt / "receipt.json", receipt)
    result = {"receipt": file_ref(attempt / "receipt.json", root), **receipt}
    if status != 200 or returncode != 0:
        raise ValueError(
            f"GET failed: {endpoint}; HTTP={status}, exit={returncode}; receipt={result['receipt']['path']}"
        )
    return result, json.loads(body)


def validate_tree(commit: dict[str, Any], tree: dict[str, Any], sha: str) -> None:
    if commit.get("sha") != sha or not re.fullmatch(
        r"[0-9a-f]{40}", commit.get("tree", {}).get("sha", "")
    ):
        raise ValueError("Commit object is not bound to frozen SHA/tree")
    if (
        tree.get("sha") != commit["tree"]["sha"]
        or tree.get("truncated") is not False
        or not isinstance(tree.get("tree"), list)
    ):
        raise ValueError("Tree is truncated or not the frozen commit's tree")
    seen = set()
    for row in tree["tree"]:
        path = row.get("path")
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or "\0" in path
            or any(p in ("", ".", "..") for p in path.split("/"))
            or path in seen
        ):
            raise ValueError("Unsafe or duplicate Git tree path")
        seen.add(path)
        if row.get("type") == "blob" and (
            not re.fullmatch(r"[0-9a-f]{40}", row.get("sha", ""))
            or type(row.get("size")) is not int
            or row["size"] < 0
        ):
            raise ValueError("Malformed Git blob declaration")


def decode_blob(payload: dict[str, Any], row: dict[str, Any]) -> bytes:
    if payload.get("sha") != row["sha"] or payload.get("encoding") != "base64":
        raise ValueError("Blob payload identity/encoding differs from frozen tree")
    content = payload.get("content")
    if not isinstance(content, str):
        raise ValueError("Missing blob content")
    raw = base64.b64decode("".join(content.split()), validate=True)
    if len(raw) != row["size"] or payload.get("size") != len(raw) or blob_sha(raw) != row["sha"]:
        raise ValueError("Downloaded blob bytes differ from Git tree hash/size")
    return raw


def prepare(
    queue_path: Path, audit_path: Path, output: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    queue = json.loads(queue_path.read_bytes())
    audit = json.loads(audit_path.read_bytes())
    if queue["source_dataset_sha256"] != audit["dataset_ref"]["sha256"]:
        raise ValueError("Queue and source audit use different datasets")
    tasks = {
        row["task_id"]: {**row, "dataset_base": audit["dataset_base"]} for row in audit["tasks"]
    }
    selected = [
        row
        for row in queue["tasks"]
        if any(item["kind"] == "source_commit_closure" for item in row["items"])
    ]
    rows = []
    for queued in selected:
        old = tasks[queued["task_id"]]
        if any(queued[key] != old[key] for key in ("repo", "sha")):
            raise ValueError("Frozen source task identity changed")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", old["repo"]) or not re.fullmatch(
            r"[0-9a-f]{40}", old["sha"]
        ):
            raise ValueError("Malformed repository/revision")
        rows.append(
            {
                "task_id": old["task_id"],
                "repo": old["repo"],
                "sha": old["sha"],
                "is_canonical": old["is_canonical"],
                "before_source_completeness": old["source_completeness"],
                "before_size": old["measured_size"],
                "frozen_missing_java_paths": old.get("tree_comparison", {}).get(
                    "missing_java_paths", []
                ),
                "old_inventory": old["inventory"],
                "original_refs": old["original_refs"],
            }
        )
    if len({row["task_id"] for row in rows}) != len(rows):
        raise ValueError("Duplicate source task")
    plan = {
        "schema": "sag-source-recovery-plan-v1",
        "queue": {"path": str(queue_path.resolve()), "sha256": digest(queue_path)},
        "source_audit": {"path": str(audit_path.resolve()), "sha256": digest(audit_path)},
        "source_dataset_sha256": audit["dataset_ref"]["sha256"],
        "old_audit_base": str(audit_path.parent.resolve()),
        "selection": "Only source_commit_closure tasks; Java blob recovery limited to their already frozen missing paths",
        "tasks": rows,
        "request_bound": {
            "commit_objects": len({(r["repo"], r["sha"]) for r in rows}),
            "recursive_trees": len({(r["repo"], r["sha"]) for r in rows}),
            "missing_java_paths": sum(len(r["frozen_missing_java_paths"]) for r in rows),
        },
    }
    output.mkdir(parents=True, exist_ok=True)
    plan_path = output / "plan.json"
    if plan_path.exists():
        if json.loads(plan_path.read_bytes()) != plan:
            raise ValueError("Recovery plan changed; use a new output directory")
    else:
        write_json(plan_path, plan)
    return plan, tasks


def recover_task(
    task: dict[str, Any], old: dict[str, Any], plan: dict[str, Any], out: Path, fetch: bool
) -> dict[str, Any]:
    repo, sha = task["repo"], task["sha"]
    directory = out / "captures" / repo / sha
    record: dict[str, Any] = {
        **task,
        "after_source_completeness": "unavailable",
        "errors": [],
        "overlay": [],
        "collection_gaps": [],
        "campaign_ready": False,
    }
    try:
        original_base = Path(old["dataset_base"])
        original_archive = safe_ref(original_base, task["original_refs"]["source_archive"])
        observed_archive = file_ref(original_archive, original_base)
        record["original_archive_current_validation"] = observed_archive
        if observed_archive["sha256"] != task["original_refs"]["source_archive_sha256"]:
            raise ValueError("Original compressed archive changed since the sealed source audit")
        commit_ref, commit = capture(
            f"repos/{repo}/git/commits/{sha}", directory / "commit", out, fetch
        )
        record["commit_capture"] = commit_ref
        if commit.get("sha") != sha or not re.fullmatch(
            r"[0-9a-f]{40}", commit.get("tree", {}).get("sha", "")
        ):
            raise ValueError("Commit response has wrong immutable identity")
        tree_sha = commit["tree"]["sha"]
        tree_ref, tree = capture(
            f"repos/{repo}/git/trees/{tree_sha}?recursive=1", directory / "tree", out, fetch
        )
        record["tree_capture"] = tree_ref
        validate_tree(commit, tree, sha)
        record["tree_sha"] = tree_sha
        inventory_path = verify_ref(Path(plan["old_audit_base"]), task["old_inventory"])
        inventory = json.loads(inventory_path.read_bytes())["entries"]
        record["before_tree_comparison"] = compare_tree(inventory, tree)
        frozen = set(task["frozen_missing_java_paths"])
        actual_missing = set(record["before_tree_comparison"]["missing_java_paths"])
        if frozen - actual_missing:
            raise ValueError("Frozen missing Java scope does not match acquired exact tree")
        blobs = {row["path"]: row for row in tree["tree"] if row.get("type") == "blob"}
        for path in sorted(frozen):
            row = blobs[path]
            if row.get("mode") not in {"100644", "100755"} or not path.endswith(".java"):
                raise ValueError("Frozen overlay is not a regular Java blob")
            capture_ref, payload = capture(
                f"repos/{repo}/git/blobs/{row['sha']}", directory / "blobs" / row["sha"], out, fetch
            )
            raw = decode_blob(payload, row)
            dest = out / "overlay" / "objects" / (row["sha"] + ".java")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists() and dest.read_bytes() != raw:
                raise ValueError("Content-addressed overlay bytes already differ")
            if not dest.exists():
                dest.write_bytes(raw)
            item = {
                "source_path": path,
                "git_blob_sha1": row["sha"],
                "physical_lines": raw.count(b"\n") + int(bool(raw) and not raw.endswith(b"\n")),
                "source": file_ref(dest, out),
                "capture": capture_ref,
            }
            record["overlay"].append(item)
            inventory[path] = {
                "type": "file",
                "bytes": len(raw),
                "git_blob_sha1": row["sha"],
                "sha256": item["source"]["sha256"],
                "physical_lines": item["physical_lines"],
            }
        after = compare_tree(inventory, tree)
        record["after_tree_comparison"] = after
        if after["java_content_verified"]:
            record["after_source_completeness"] = "java_content_verified"
        else:
            record["after_source_completeness"] = "conflict"
        record["after_size"] = {
            "java_files": sum(
                p.endswith(".java") and x.get("type") == "file" for p, x in inventory.items()
            ),
            "java_physical_lines": sum(
                x.get("physical_lines", 0)
                for p, x in inventory.items()
                if p.endswith(".java") and x.get("type") == "file"
            ),
            "java_source_bytes": sum(
                x["bytes"]
                for p, x in inventory.items()
                if p.endswith(".java") and x.get("type") == "file"
            ),
        }
        record["evidence_layout"] = (
            "original_archive_plus_verified_java_overlay"
            if record["overlay"]
            else "original_archive"
        )
        record["original_archive_rewritten"] = False
        if after["missing_java_paths"]:
            record["collection_gaps"].append(
                "New exact tree reveals additional missing Java paths outside the frozen acquisition list; not downloaded"
            )
        if after["java_content_mismatches"]:
            record["collection_gaps"].append(
                "Java bytes differ from exact Git tree; no unapproved replacement was made"
            )
        if (
            after["missing_repository_blob_paths"]
            or after["repository_blob_content_mismatches"]
            or after["gitlinks"]
        ):
            record["collection_gaps"].append(
                "Non-Java paths/bytes or submodule closure still needs task-specific checkout review; Java proof alone does not certify a runnable checkout"
            )
        if record["overlay"]:
            record["collection_gaps"].append(
                "A runnable checkout must materialize the verified overlay at source_path and address the remaining checkout scope; the old tarball remains incomplete"
            )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        record["errors"].append(str(exc))
    dest = out / "tasks" / (task["task_id"] + ".json")
    write_json(dest, record)
    return record


def run(
    queue: Path, audit: Path, output: Path, fetch: bool = False, workers: int = 2
) -> dict[str, Any]:
    output = output.resolve()
    if output.is_relative_to(audit.parent.resolve()) or output.is_relative_to(
        queue.parent.resolve()
    ):
        raise ValueError("Recovery output must not be under the sealed rescreen directory")
    plan, old = prepare(queue, audit, output)
    result_rows = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {
            pool.submit(recover_task, row, old[row["task_id"]], plan, output, fetch): row
            for row in plan["tasks"]
        }
        for job in as_completed(jobs):
            record = job.result()
            result_rows.append(record)
            print(
                f"{record['repo']}: {record['before_source_completeness']} -> {record['after_source_completeness']}; overlay={len(record['overlay'])}; errors={record['errors']}",
                flush=True,
            )
    order = {row["task_id"]: i for i, row in enumerate(plan["tasks"])}
    result_rows.sort(key=lambda row: order[row["task_id"]])
    receipts = list(output.glob("captures/**/receipt.json"))
    successful = [
        json.loads(path.read_bytes())
        for path in receipts
        if json.loads(path.read_bytes()).get("http_status") == 200
    ]
    summary = {
        "tasks": len(result_rows),
        "unique_revisions": len({(r["repo"], r["sha"]) for r in result_rows}),
        "java_content_verified_after": sum(
            r["after_source_completeness"] == "java_content_verified" for r in result_rows
        ),
        "overlay_java_paths": sum(len(r["overlay"]) for r in result_rows),
        "http_requests_recorded": len(receipts),
        "successful_http_requests": len(successful),
        "downloaded_body_bytes": sum(r["body"]["bytes"] for r in successful),
        "canonical_selection_changed": False,
        "old_archives_modified": False,
        "builds_or_model_calls": 0,
    }
    report = {
        "schema": "sag-source-recovery-result-v1",
        "plan": file_ref(output / "plan.json", output),
        "source_dataset_sha256": plan["source_dataset_sha256"],
        "created_at": now(),
        "summary": summary,
        "tasks": result_rows,
        "limits": [
            "Source verification only: no build/test replay or dependency judgment.",
            "Fory recovered files are a separate verified overlay, not a silently replaced original archive.",
            "Historical rescreen and canonical choices remain unchanged; an explicit later version may consume this evidence.",
        ],
    }
    write_json(output / "report.json", report)
    lines = [
        "# Frozen source evidence recovery",
        "",
        "Only the previously declared source gaps were queried from GitHub's official read-only Git object API. Old datasets, archives and canonical selections were not changed.",
        "",
        "```json",
        json.dumps(summary, indent=2),
        "```",
        "",
        "| Repository | Before | After | Java files before → after | Overlay paths | Remaining limitation |",
        "|---|---|---|---:|---:|---|",
    ]
    for row in result_rows:
        lines.append(
            f"| {row['repo']} | {row['before_source_completeness']} | {row['after_source_completeness']} | {row['before_size']['java_files']} → {row.get('after_size',{}).get('java_files','unknown')} | {len(row['overlay'])} | {'; '.join(row['errors']+row['collection_gaps']).replace('|','/')} |"
        )
    lines += [
        "",
        "Every response includes the exact URL, capture time, HTTP status, raw-response bytes and SHA256. Commit objects bind the recursive Git tree; Java files and downloaded overlays are checked using Git blob SHA1 plus byte size. Missing or truncated evidence never becomes verified.",
        "",
        "Re-run without --fetch to validate already captured evidence without network access. Native source content is never executed.",
    ]
    (output / "report.md").write_text("\n".join(lines) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--queue",
        type=Path,
        default=Path("output/java-benchmark-rescreen-20260922/COLLECTION_QUEUE.json"),
    )
    parser.add_argument(
        "--source-audit",
        type=Path,
        default=Path("output/java-benchmark-rescreen-20260922/source-audit/audit.json"),
    )
    parser.add_argument(
        "--output", type=Path, default=Path("output/java-benchmark-acquisition-20260922/source")
    )
    parser.add_argument(
        "--fetch", action="store_true", help="Allow fixed-list official read-only API acquisition"
    )
    args = parser.parse_args()
    print(
        json.dumps(run(args.queue, args.source_audit, args.output, args.fetch)["summary"], indent=2)
    )


if __name__ == "__main__":
    main()
