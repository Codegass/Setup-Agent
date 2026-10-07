"""Recover fixed non-Java checkout gaps without extracting or executing projects."""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile


def load(path):
    return json.loads(Path(path).read_bytes())


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def bound(path, base):
    raw = path.read_bytes()
    return {"path": str(path.relative_to(base)), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def blob_sha(raw):
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def safe_path(value):
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts or "\\" in value:
        raise ValueError("Unsafe repository path")
    return path


def decode_blob(payload, expected_sha, expected_size):
    if payload.get("encoding") != "base64" or payload.get("sha") != expected_sha:
        raise ValueError("Blob identity/encoding mismatch")
    raw = base64.b64decode("".join(payload["content"].split()), validate=True)
    if len(raw) != expected_size or payload.get("size") != expected_size or blob_sha(raw) != expected_sha:
        raise ValueError("Blob bytes disagree with pinned tree")
    return raw


def compare_bytes(archived, recovered):
    if archived is None:
        return "missing_from_original_archive"
    if archived == recovered:
        return "identical"
    if archived.replace(b"\r\n", b"\n") == recovered.replace(b"\r\n", b"\n"):
        return "line_endings_only"
    return "other_byte_difference"


def parse_response(raw):
    match = re.match(rb"HTTP/\S+ (\d+)[^\r\n]*\r?\n", raw)
    if not match:
        raise ValueError("Missing HTTP status in GitHub response")
    separator = re.search(rb"\r?\n\r?\n", raw)
    if not separator:
        raise ValueError("Missing HTTP header terminator")
    return int(match.group(1)), raw[: separator.start()], raw[separator.end() :]


def fetch(endpoint, path, network):
    """Capture only official Git object API responses; cached bytes are hash checked."""
    if not re.fullmatch(r"repos/[\w.-]+/[\w.-]+/git/(?:commits|trees|blobs)/[0-9a-f]{40}(?:\?recursive=1)?", endpoint):
        raise ValueError("Not an allowed immutable Git object endpoint")
    url = "https://api.github.com/" + endpoint
    meta_path = path.with_suffix(path.suffix + ".meta.json")
    if path.exists() and meta_path.exists():
        meta = load(meta_path)
        raw = path.read_bytes()
        if meta.get("url") != url or meta.get("sha256") != hashlib.sha256(raw).hexdigest() or meta.get("bytes") != len(raw):
            raise ValueError("Cached response binding mismatch")
        headers = path.with_suffix(path.suffix + ".headers.txt").read_bytes()
        if hashlib.sha256(headers).hexdigest() != meta.get("response_headers_sha256"):
            raise ValueError("Cached response header hash mismatch")
        status = re.match(rb"HTTP/\S+ (\d+)", headers)
        if not status or int(status.group(1)) != meta.get("http_status"):
            raise ValueError("Cached HTTP status differs from receipt")
    else:
        if not network:
            raise ValueError("Network response not captured")
        path.parent.mkdir(parents=True, exist_ok=True)
        started = datetime.now(timezone.utc).isoformat()
        run = subprocess.run(["gh", "api", "--hostname", "github.com", "--include", endpoint], capture_output=True, timeout=120)
        try:
            status, headers, raw = parse_response(run.stdout)
        except ValueError:
            status, headers, raw = None, b"", run.stdout
        path.write_bytes(raw)
        header_path = path.with_suffix(path.suffix + ".headers.txt")
        header_path.write_bytes(headers)
        meta = {"url": url, "captured_at": started, "http_status": status, "returncode": run.returncode,
                "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                "response_headers_sha256": hashlib.sha256(headers).hexdigest(),
                "error": run.stderr.decode(errors="replace")[:1200] if run.returncode else None}
        save(meta_path, meta)
    if meta.get("http_status") != 200 or meta.get("returncode") != 0:
        raise ValueError("Official response unavailable: " + str(meta.get("http_status")))
    return json.loads(raw)


def archive_subset(archive, paths):
    result = {}
    with tarfile.open(archive, "r|gz") as stream:
        for member in stream:
            parts = safe_path(member.name).parts
            relative = "/".join(parts[1:])
            if not member.isfile() or not relative:
                continue
            if relative in paths or PurePosixPath(relative).name in {".gitattributes", ".gitmodules"}:
                handle = stream.extractfile(member)
                if handle is not None:
                    result[relative] = handle.read()
    return result


def audit(original, previous, output, network=False):
    source = load(previous / "source-audit/audit.json")
    queue = load(previous / "COLLECTION_QUEUE.json")
    expected_dataset = hashlib.sha256((original / "dataset.json").read_bytes()).hexdigest()
    if source["dataset_ref"]["sha256"] != expected_dataset or queue["source_dataset_sha256"] != expected_dataset:
        raise ValueError("Source audit and acquisition queue must bind the same original dataset")
    ids = {r["task_id"] for r in queue["tasks"] if any(i["kind"] == "checkout_scope_review" for i in r["items"])}
    tasks = [r for r in source["tasks"] if r["task_id"] in ids]
    grouped = {}
    for row in tasks:
        grouped.setdefault((row["repo"], row["sha"]), []).append(row)
    rows = []
    for (repo, sha), group in grouped.items():
        row = group[0]
        comparison = row["tree_comparison"]
        paths = sorted(set(comparison["missing_repository_blob_paths"] + comparison["repository_blob_content_mismatches"]))
        record = {"repo": repo, "sha": sha, "task_ids": [r["task_id"] for r in group],
                  "before": {"java_content": row["source_completeness"], "regular_blob_gaps": len(paths), "gitlinks": comparison["gitlinks"]},
                  "recovered_blobs": [], "issues": [], "campaign_ready": False,
                  "original_source_archive": row["original_refs"]["source_archive"],
                  "original_source_archive_sha256": row["original_refs"]["source_archive_sha256"]}
        directory = output / repo / sha
        try:
            commit = fetch(f"repos/{repo}/git/commits/{sha}", directory / "commit.json", network)
            if commit.get("sha") != sha:
                raise ValueError("Commit response mismatch")
            tree_sha = commit["tree"]["sha"]
            tree = fetch(f"repos/{repo}/git/trees/{tree_sha}?recursive=1", directory / "tree.json", network)
            if tree.get("sha") != tree_sha or tree.get("truncated") is not False:
                raise ValueError("Tree identity or completeness mismatch")
            entries = {r["path"]: r for r in tree["tree"]}
            if len(entries) != len(tree["tree"]):
                raise ValueError("Duplicate Git tree paths")
            archived_tree = load(original / row["tree_binding"]["path"])
            old_entries = {r["path"]: r for r in archived_tree["tree"]}
            if {(p, e["sha"], e["type"], e["mode"]) for p, e in entries.items()} != {(p, e["sha"], e["type"], e["mode"]) for p, e in old_entries.items()}:
                raise ValueError("Fresh pinned tree differs from archived tree entries")
            archive = original / row["original_refs"]["source_archive"]
            if hashlib.sha256(archive.read_bytes()).hexdigest() != row["original_refs"]["source_archive_sha256"]:
                raise ValueError("Original source archive changed")
            subset = archive_subset(archive, set(paths))
            context = []
            for name, raw in subset.items():
                if PurePosixPath(name).name in {".gitattributes", ".gitmodules"}:
                    target = directory / "archive-context" / safe_path(name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
                    context.append(bound(target, output))
            record["archive_context"] = context
            for name in paths:
                entry = entries[name]
                if entry["type"] != "blob" or entry["mode"] not in {"100644", "100755"}:
                    raise ValueError("Gap is not a regular file")
                blob_path = directory / "raw-blobs" / (entry["sha"] + ".json")
                blob = fetch(f"repos/{repo}/git/blobs/{entry['sha']}", blob_path, network)
                raw = decode_blob(blob, entry["sha"], entry["size"])
                target = directory / "overlay" / safe_path(name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
                record["recovered_blobs"].append({"repository_path": name, "git_blob_sha": entry["sha"], "mode": entry["mode"],
                    "file": bound(target, output), "raw_response": bound(blob_path, output),
                    "archive_difference": compare_bytes(subset.get(name), raw),
                    "original_archived_sha256": hashlib.sha256(subset[name]).hexdigest() if name in subset else None})
            record["git_object_evidence"] = [bound(directory / n, output) for n in ["commit.json", "tree.json"]]
            record["regular_blob_gaps_resolved"] = len(record["recovered_blobs"]) == len(paths)
            record["status"] = "regular_blob_gaps_recovered_submodules_pending" if comparison["gitlinks"] else "regular_blob_gaps_recovered"
            record["remaining_requirements"] = ["Review submodule scope and capture pinned submodule source"] if comparison["gitlinks"] else []
        except (ValueError, KeyError, OSError, subprocess.TimeoutExpired) as exc:
            record["issues"].append(str(exc))
            record["status"] = "unavailable_or_conflicting_source"
            record["regular_blob_gaps_resolved"] = False
        rows.append(record)
        print(repo, record["status"], len(record["recovered_blobs"]), flush=True)
    result = {"schema": "sag-checkout-recovery-v1", "captured_at": datetime.now(timezone.utc).isoformat(),
              "original_base": str(original), "evidence_base": str(output),
              "inputs": [bound(previous / "source-audit/audit.json", previous), bound(previous / "COLLECTION_QUEUE.json", previous)],
              "script": bound(Path(__file__).resolve(), Path(__file__).resolve().parents[1]),
              "summary": {"tasks": len(tasks), "unique_revisions": len(rows), "statuses": dict(Counter(r["status"] for r in rows)),
                          "recovered_regular_blobs": sum(len(r["recovered_blobs"]) for r in rows),
                          "byte_difference_types": dict(Counter(b["archive_difference"] for r in rows for b in r["recovered_blobs"]))},
              "limitations": ["Recovery targets only predeclared regular-file gaps; it does not prove the effective CI task plan.",
                              "Overlay blobs have pinned Git modes recorded; no runnable checkout has been materialized or tested.",
                              "Submodule presence does not alone prove a required extra language toolchain or project exclusion."],
              "revisions": rows}
    save(output / "audit.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", action="store_true", help="Permit only immutable public Git object reads")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    audit(root / "output/java-benchmark-20260916", root / "output/java-benchmark-rescreen-20260922",
          root / "output/java-benchmark-acquisition-20260922/checkout", args.network)


if __name__ == "__main__":
    main()
