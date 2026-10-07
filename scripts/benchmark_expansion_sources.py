"""Freeze immutable candidate source versions; never run or extract project code.

Source completeness and size do not admit a candidate CI task to the benchmark.
The transfer bound is operational: exceeding it leaves evidence pending.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess

from scripts.benchmark_source_audit import (
    MEASUREMENT, compare_tree, digest, ref, scan_archive, write_json,
)
from scripts.benchmark_source_recovery import capture, validate_tree


def freeze_version(task, output, network=False):
    repo, sha = task["repo"], task["sha"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Unsafe or non-immutable source identity")
    directory = output / "versions" / repo / sha
    directory.mkdir(parents=True, exist_ok=True)
    row = {"repo": repo, "sha": sha, "status": "unavailable", "task_admission": "not_implied", "issues": []}
    archive, receipt_path = directory / "source.tar.gz", directory / "source.receipt.json"
    url = f"https://codeload.github.com/{repo}/tar.gz/{sha}"
    try:
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_bytes())
            if not receipt.get("body") or receipt["url"] != url or digest(archive) != receipt["body"]["sha256"] or archive.stat().st_size != receipt["body"]["bytes"]:
                raise ValueError("Cached source response binding changed")
        else:
            if not network:
                raise ValueError("Source archive not captured")
            headers = directory / "source.headers.txt"
            started = datetime.now(timezone.utc).isoformat()
            proc = subprocess.run([
                "curl", "--silent", "--show-error", "--proto", "=https", "--max-time", "180",
                "--max-filesize", "268435456", "--dump-header", str(headers), "--output", str(archive),
                "--write-out", "%{http_code}\n%{url_effective}", url,
            ], capture_output=True, timeout=190)
            status = proc.stdout.decode().splitlines()
            receipt = {
                "url": url, "method": "GET", "requested_at": started,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "http_status": int(status[0]) if status and status[0].isdigit() else None,
                "effective_url": status[1] if len(status) > 1 else None,
                "returncode": proc.returncode, "stderr": proc.stderr.decode(errors="replace"),
                "body": ref(archive, output) if archive.exists() else None,
                "headers": ref(headers, output) if headers.exists() else None,
                "operational_max_bytes": 268435456,
            }
            write_json(receipt_path, receipt)
        if receipt["returncode"] != 0 or receipt["http_status"] != 200 or receipt["effective_url"] != url:
            raise ValueError("Source transfer incomplete/unavailable; retain attempt, do not exclude repository")
        row["archive"] = ref(archive, output)
        row["receipt"] = ref(receipt_path, output)
        commit_ref, commit = capture(f"repos/{repo}/git/commits/{sha}", directory / "commit", output, network)
        tree_ref, tree = capture(f"repos/{repo}/git/trees/{commit['tree']['sha']}?recursive=1", directory / "tree", output, network)
        validate_tree(commit, tree, sha)
        row.update(commit=commit_ref, tree=tree_ref)
        descriptors = {r["path"] for r in tree["tree"] if r["path"] == "pom.xml" or r["path"].endswith("/pom.xml")}
        scan = scan_archive(archive, descriptors, output)
        entries = json.loads((output / scan["inventory"]["path"]).read_bytes())["entries"]
        comparison = compare_tree(entries, tree)
        row.update(scan=scan, tree_comparison=comparison)
        row["status"] = "java_content_verified" if scan["read_complete"] and scan["path_safe"] and comparison["java_content_verified"] else "conflict"
        row["full_repository_blob_match"] = row["status"] == "java_content_verified" and not any(
            comparison[key] for key in ["missing_repository_blob_paths", "extra_repository_paths", "repository_blob_content_mismatches", "gitlinks"]
        )
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        row["issues"].append(str(exc))
    write_json(directory / "audit.json", row)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--network", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    tasks_path = args.tasks.resolve()
    tasks = json.loads(tasks_path.read_bytes())["tasks"]
    seen, rows = set(), []
    for task in tasks:
        identity = (task["repo"], task["sha"])
        if identity in seen:
            continue
        seen.add(identity)
        row = freeze_version(task, output, args.network)
        rows.append(row)
        print(task["repo"], row["status"], flush=True)
    write_json(output / "report.json", {
        "schema": "expansion-source-archive-v1", "task_list": {"path": str(tasks_path), "sha256": digest(tasks_path), "bytes": tasks_path.stat().st_size},
        "measurement": MEASUREMENT, "scope": "Candidate source versions, not admitted benchmark references; no project execution.",
        "size_strata": "Not frozen until final cohort selection; raw continuous size only.",
        "summary": dict(Counter(row["status"] for row in rows)), "rows": rows,
    })


if __name__ == "__main__":
    main()
