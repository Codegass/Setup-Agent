#!/usr/bin/env python3
"""Re-audit existing benchmark archives, offline and without extracting a checkout.

Only existing dataset evidence is read. No project source is executed. Java size
means regular .java files across the archive, including tests/examples/fixtures.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import statistics
import tarfile
import tempfile
from typing import Any
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

SCHEMA = "sag-source-archive-audit-v1"
CHUNK = 1024 * 1024
MEASUREMENT = (
    "All regular, case-sensitive *.java files in the source archive, including "
    "tests, examples and generated fixtures. PLOC counts LF-delimited physical "
    "lines, including blank lines/comments and a final unterminated line. This "
    "is not semantic SLOC, target-only size, compiled-file coverage or run cost."
)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def ref(path: Path, base: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(base).as_posix(),
        "sha256": digest(path),
        "bytes": path.stat().st_size,
    }


def safe_ref(base: Path, value: str) -> Path:
    parts = PurePosixPath(value).parts
    if not value or "\\" in value or value.startswith("/") or any(p in (".", "..") for p in parts):
        raise ValueError(f"Unsafe evidence path: {value!r}")
    path = base / value
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError(f"Evidence path escapes dataset: {value!r}")
    current = base
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Symlink evidence path: {value!r}")
    return path


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_content(path: Path, data: bytes) -> None:
    """Atomic content-addressed write when identical POMs occur in parallel archives."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def member_path(name: str) -> tuple[str, str]:
    """Validate before removing the codeload archive's single wrapper directory."""
    name = name.rstrip("/")
    if not name or name.startswith("/") or "\\" in name or "\0" in name:
        raise ValueError("absolute, empty, backslash or NUL archive path")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts) or re.match(r"^[A-Za-z]:", parts[0]):
        raise ValueError("non-canonical or traversing archive path")
    return parts[0], "/".join(parts[1:])


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def descriptor(data: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {"default_goal": None, "packaging": None, "default_goal_line": None}
    # No DTD or custom entity expansion. Literal XML facts only; never inheritance.
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        result["parse_error"] = "DTD/entity declaration is not interpreted"
        return result
    try:
        root = ET.fromstring(data)
        if root.tag.rsplit("}", 1)[-1] != "project":
            result["parse_error"] = "Root element is not Maven project"
            return result
        child = lambda element, name: next(
            (x for x in element if x.tag.rsplit("}", 1)[-1] == name), None
        )
        packaging = child(root, "packaging")
        if packaging is not None:
            result["packaging"] = (packaging.text or "").strip()
        build = child(root, "build")
        goal = child(build, "defaultGoal") if build is not None else None
        if goal is not None:
            result["default_goal"] = "".join(goal.itertext()).strip()
            text = data.decode("utf-8", errors="replace")
            hits = list(re.finditer(r"<(?:[\w.-]+:)?defaultGoal(?:\s[^>]*)?>", text))
            # More than one goal (profiles) has no safe single line assignment.
            if len(hits) == 1:
                result["default_goal_line"] = text[: hits[0].start()].count("\n") + 1
        result["interpretation"] = (
            "Literal direct project/build/defaultGoal and project/packaging; no effective POM inference"
        )
    except ET.ParseError as exc:
        result["parse_error"] = str(exc)
    return result


def scan_archive(path: Path, requested_descriptors: set[str], out: Path) -> dict[str, Any]:
    sha = digest(path)
    entries: dict[str, dict[str, Any]] = {}
    descriptors: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    roots: set[str] = set()
    size = Counter(java_files=0, java_physical_lines=0, java_source_bytes=0)
    category_files: Counter[str] = Counter()
    category_lines: Counter[str] = Counter()
    completed = False
    seen: set[str] = set()
    try:
        with tarfile.open(path, "r|gz") as archive:
            for member in archive:
                try:
                    top, name = member_path(member.name)
                except ValueError as exc:
                    issues.append(
                        {"code": "unsafe_member_path", "member": member.name, "detail": str(exc)}
                    )
                    continue
                roots.add(top)
                if member.name.rstrip("/") in seen:
                    issues.append({"code": "duplicate_archive_member", "member": member.name})
                    continue
                seen.add(member.name.rstrip("/"))
                if not name:
                    if not member.isdir():
                        issues.append({"code": "non_directory_wrapper", "member": member.name})
                    continue
                if member.isdir():
                    continue
                item: dict[str, Any] = {"archive_member": member.name, "bytes": member.size}
                if member.issym():
                    item.update(
                        type="symlink",
                        target=member.linkname,
                        git_blob_sha1=blob_sha(member.linkname.encode("utf-8", "surrogateescape")),
                    )
                    target = posixpath.normpath(
                        posixpath.join(posixpath.dirname(name), member.linkname)
                    )
                    if (
                        member.linkname.startswith("/")
                        or "\\" in member.linkname
                        or target == ".."
                        or target.startswith("../")
                    ):
                        issues.append(
                            {
                                "code": "unsafe_symlink_target",
                                "member": member.name,
                                "target": member.linkname,
                            }
                        )
                elif member.isfile():
                    source = archive.extractfile(member)
                    if source is None:
                        raise ValueError(f"Missing member stream: {member.name}")
                    h = hashlib.sha1(b"blob " + str(member.size).encode() + b"\0")
                    h256 = hashlib.sha256()
                    actual, lines, tail = 0, 0, b""
                    chunks: list[bytes] | None = [] if name in requested_descriptors else None
                    for block in iter(lambda: source.read(CHUNK), b""):
                        actual += len(block)
                        h.update(block)
                        h256.update(block)
                        if name.endswith(".java"):
                            lines += block.count(b"\n")
                            tail = block[-1:]
                        if chunks is not None:
                            chunks.append(block)
                    item.update(
                        type="file",
                        bytes=actual,
                        git_blob_sha1=h.hexdigest(),
                        sha256=h256.hexdigest(),
                        executable=bool(member.mode & 0o111),
                    )
                    if actual != member.size:
                        issues.append({"code": "member_size_mismatch", "member": member.name})
                    if name.endswith(".java"):
                        lines += int(bool(actual) and tail != b"\n")
                        item["physical_lines"] = lines
                        size.update(
                            java_files=1, java_physical_lines=lines, java_source_bytes=actual
                        )
                        category = (
                            "test"
                            if set(name.lower().split("/")) & {"test", "tests", "testsrc"}
                            else "other"
                        )
                        category_files[category] += 1
                        category_lines[category] += lines
                    if chunks is not None:
                        data = b"".join(chunks)
                        dest = out / "descriptors" / (h256.hexdigest() + ".xml")
                        write_content(dest, data)
                        descriptors.append(
                            {
                                "source_path": name,
                                "archive_member": member.name,
                                **ref(dest, out),
                                "git_blob_sha1": h.hexdigest(),
                                **descriptor(data),
                            }
                        )
                else:
                    item.update(
                        type="hardlink" if member.islnk() else "unsupported", target=member.linkname
                    )
                    issues.append(
                        {
                            "code": "unsupported_member_type",
                            "member": member.name,
                            "type": item["type"],
                        }
                    )
                if name in entries:
                    issues.append({"code": "duplicate_repository_path", "path": name})
                entries[name] = item
        completed = True
    except (OSError, EOFError, tarfile.TarError, ValueError) as exc:
        issues.append({"code": "archive_read_error", "detail": str(exc)})
    if len(roots) != 1:
        issues.append({"code": "archive_wrapper_count", "roots": sorted(roots)})
    for name in entries:
        for parent in PurePosixPath(name).parents:
            if str(parent) in entries:
                issues.append(
                    {"code": "member_below_non_directory", "path": name, "ancestor": str(parent)}
                )
    inventory = out / "inventories" / (sha + ".json")
    write_json(inventory, {"archive_sha256": sha, "entries": entries})
    return {
        "sha256": sha,
        "bytes": path.stat().st_size,
        "read_complete": completed,
        "path_safe": not issues,
        "issues": issues,
        "inventory": ref(inventory, out),
        "measured_size": dict(size),
        "descriptors": descriptors,
        "size_by_path_convention": {
            "java_files_by_path": dict(category_files),
            "java_lines_by_path": dict(category_lines),
        },
    }


def source_url_matches(url: str, repo: str, sha: str, tree: bool = False) -> bool:
    parsed = urlparse(url)
    path = unquote(parsed.path).rstrip("/")
    if tree:
        return (
            parsed.scheme == "https"
            and parsed.hostname == "api.github.com"
            and path == f"/repos/{repo}/git/trees/{sha}"
        )
    return parsed.scheme == "https" and (
        (parsed.hostname == "codeload.github.com" and path == f"/{repo}/tar.gz/{sha}")
        or (parsed.hostname == "api.github.com" and path == f"/repos/{repo}/tarball/{sha}")
    )


def index_trees(base: Path) -> dict[tuple[str, str], list[Path]]:
    result: dict[tuple[str, str], list[Path]] = defaultdict(list)
    for meta_path in sorted(base.rglob("*tree*.json.meta.json")):
        try:
            meta = json.loads(meta_path.read_bytes())
            parsed = urlparse(meta.get("url", ""))
            match = re.fullmatch(r"/repos/([^/]+/[^/]+)/git/trees/([0-9a-f]{40})/?", parsed.path)
            if match and parsed.scheme == "https" and parsed.hostname == "api.github.com":
                path = Path(str(meta_path)[: -len(".meta.json")])
                result[(match[1], match[2])].append(path)
        except (ValueError, OSError):
            continue
    return result


def verified_tree(
    path: Path, base: Path, repo: str, sha: str
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    info: dict[str, Any] = {"path": path.relative_to(base).as_posix(), "valid": False, "issues": []}
    try:
        path = safe_ref(base, info["path"])
        meta_path = safe_ref(base, info["path"] + ".meta.json")
        data = path.read_bytes()
        meta = json.loads(meta_path.read_bytes())
        tree = json.loads(data)
        info.update(ref(path, base))
        info["metadata"] = ref(meta_path, base)
        info["url"] = meta.get("url")
        info["response_sha"] = tree.get("sha")
        info["truncated"] = tree.get("truncated")
        if not source_url_matches(meta.get("url", ""), repo, sha, tree=True):
            info["issues"].append("Tree request URL is not exact repository/commit")
        if meta.get("sha256") != hashlib.sha256(data).hexdigest() or meta.get("bytes") != len(data):
            info["issues"].append("Tree metadata hash/size does not match raw response")
        if (
            meta.get("returncode", 0) != 0
            or tree.get("truncated") is not False
            or not isinstance(tree.get("tree"), list)
        ):
            info["issues"].append("Tree response was failed, truncated or malformed")
        if tree.get("sha") != sha or not source_url_matches(
            tree.get("url", ""), repo, sha, tree=True
        ):
            info["issues"].append("Tree response is not bound to the pinned SHA")
        paths = [row.get("path") for row in tree.get("tree", [])]
        if len(paths) != len(set(paths)) or any(
            not isinstance(p, str) or p.startswith("/") or ".." in p.split("/") for p in paths
        ):
            info["issues"].append("Tree has duplicate or unsafe paths")
        info["valid"] = not info["issues"]
        return info, tree if info["valid"] else None
    except (ValueError, OSError, TypeError) as exc:
        info["issues"].append(str(exc))
        return info, None


def compare_tree(entries: dict[str, Any], tree: dict[str, Any]) -> dict[str, Any]:
    tracked = {row["path"]: row for row in tree["tree"] if row.get("type") == "blob"}
    regular = {p: row for p, row in tracked.items() if row.get("mode") in {"100644", "100755"}}
    tj = {p: row for p, row in regular.items() if p.endswith(".java")}
    aj = {p: row for p, row in entries.items() if p.endswith(".java") and row.get("type") == "file"}
    missing = sorted(set(tj) - set(aj))
    extra = sorted(set(aj) - set(tj))
    mismatched = sorted(
        p
        for p in set(tj) & set(aj)
        if tj[p].get("sha") != aj[p].get("git_blob_sha1") or tj[p].get("size") != aj[p].get("bytes")
    )
    all_mismatch = sorted(
        p
        for p in set(tracked) & set(entries)
        if tracked[p].get("sha") != entries[p].get("git_blob_sha1")
    )
    return {
        "tracked_regular_java_files": len(tj),
        "archived_regular_java_files": len(aj),
        "missing_java_paths": missing,
        "extra_java_paths": extra,
        "java_content_mismatches": mismatched,
        "java_content_verified": not (missing or extra or mismatched),
        "missing_repository_blob_paths": sorted(set(tracked) - set(entries)),
        "extra_repository_paths": sorted(set(entries) - set(tracked)),
        "repository_blob_content_mismatches": all_mismatch,
        "java_symlink_paths": sorted(
            p for p, row in tracked.items() if p.endswith(".java") and row.get("mode") == "120000"
        ),
        "gitlinks": [
            {"path": row["path"], "sha": row.get("sha")}
            for row in tree["tree"]
            if row.get("type") == "commit"
        ],
        "repository_scope_note": "Gitlinks require separate pinned submodule source; Java regular-file verification does not imply a complete buildable checkout.",
    }


def audit_task(
    task: dict[str, Any],
    canonical: bool,
    scan: dict[str, Any] | None,
    base: Path,
    out: Path,
    trees: dict[tuple[str, str], list[Path]],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "task_id": task["task_id"],
        "repo": task["repo"],
        "sha": task["sha"],
        "is_canonical": canonical,
        "source_completeness": "unavailable",
        "issues": [],
        "collection_gaps": [],
        "descriptors": [],
    }
    result["original_refs"] = {
        key: task.get(key)
        for key in (
            "source_archive",
            "source_archive_sha256",
            "source_review_file",
            "source_archive_coverage",
            "review_source",
            "official_ci_url",
        )
    }
    if scan is None:
        result["issues"].append("Archive unavailable or unsafe evidence reference")
        return result
    result.update(
        measured_size=scan["measured_size"],
        size_by_path_convention=scan["size_by_path_convention"],
        inventory=scan["inventory"],
        descriptors=[dict(item) for item in scan["descriptors"]],
    )
    validation = {
        key: scan[key] for key in ("sha256", "bytes", "read_complete", "path_safe", "issues")
    }
    validation["declared_hash_matches"] = task.get("source_archive_sha256") == scan["sha256"]
    review: dict[str, Any] = {}
    try:
        archive_path = safe_ref(base, task["source_archive"])
        meta_path = safe_ref(base, task["source_archive"] + ".meta.json")
        if not meta_path.exists():
            # Both existing collectors preserve the same URL/hash/size evidence,
            # but research captures used a directory-level download receipt.
            meta_path = safe_ref(
                base, (archive_path.parent / "archive-download.json").relative_to(base).as_posix()
            )
            alternate = json.loads(meta_path.read_bytes())
            if (
                alternate.get("archive") != task["source_archive"]
                or alternate.get("sha") != task["sha"]
            ):
                raise ValueError("Directory download receipt is not bound to this archive/SHA")
        meta = json.loads(meta_path.read_bytes())
        validation["metadata"] = ref(meta_path, base)
        validation["origin_url"] = meta.get("url")
        validation["metadata_hash_matches"] = (
            meta.get("sha256") == scan["sha256"] and meta.get("bytes") == scan["bytes"]
        )
        validation["origin_exact_sha"] = source_url_matches(
            meta.get("url", ""), task["repo"], task["sha"]
        )
        review_path = safe_ref(base, task["source_review_file"])
        review = json.loads(review_path.read_bytes())
        validation["source_review"] = ref(review_path, base)
        validation["review_identity_matches"] = (
            review.get("repo") == task["repo"]
            and review.get("sha") == task["sha"]
            and review.get("archive_sha256") == scan["sha256"]
        )
        result["archive_validation"] = validation
    except (OSError, ValueError, KeyError) as exc:
        validation["evidence_error"] = str(exc)
        result["archive_validation"] = validation
    result["old_size_comparison"] = {
        key: {
            "recorded": task.get("source_size", {}).get(key),
            "measured": value,
            "equal": task.get("source_size", {}).get(key) == value,
        }
        for key, value in scan["measured_size"].items()
    }
    old_coverage = task.get("source_archive_coverage", {}).get("evidence_file") or review.get(
        "coverage_audit"
    )
    if old_coverage:
        try:
            result["old_coverage_ref"] = ref(safe_ref(base, old_coverage), base)
        except (OSError, ValueError) as exc:
            result["issues"].append(f"Old coverage audit not readable: {exc}")
    paths = trees.get((task["repo"], task["sha"]), [])
    # Also show the historical default-tree mismatch, without promoting it.
    default = base / "raw" / "repos" / task["repo"] / "tree.json"
    candidates = list(paths) + ([default] if default.exists() and default not in paths else [])
    result["tree_candidates"] = []
    valid_trees = []
    for path in candidates:
        info, tree = verified_tree(path, base, task["repo"], task["sha"])
        result["tree_candidates"].append(info)
        if tree is not None:
            valid_trees.append((info, tree))
    archive_ok = all(
        validation.get(key) is True
        for key in (
            "declared_hash_matches",
            "metadata_hash_matches",
            "origin_exact_sha",
            "review_identity_matches",
            "read_complete",
            "path_safe",
        )
    )
    if not archive_ok:
        result["issues"].append("Archive integrity/provenance/safety is not fully verified")
    if not all(x["equal"] for x in result["old_size_comparison"].values()):
        result["issues"].append("Recomputed Java size differs from recorded dataset size")
    if valid_trees:
        info, tree = valid_trees[0]
        inventory = json.loads((out / scan["inventory"]["path"]).read_bytes())["entries"]
        comparison = compare_tree(inventory, tree)
        result["tree_binding"] = info
        result["tree_comparison"] = comparison
        conflicting_trees = any(other["tree"] != tree["tree"] for _, other in valid_trees[1:])
        if conflicting_trees:
            result["issues"].append("Multiple exact-SHA tree responses conflict")
        if comparison["java_content_verified"] and archive_ok and not conflicting_trees:
            result["source_completeness"] = "java_content_verified"
        elif (
            comparison["java_content_verified"]
            and "evidence_error" in validation
            and not conflicting_trees
        ):
            result["source_completeness"] = "unavailable"
        else:
            result["source_completeness"] = "conflict"
        if comparison["missing_java_paths"]:
            result["issues"].append(
                "Archived Java scope omits tracked Java files; measured size is a lower bound"
            )
            result["collection_gaps"].append(
                "Recover omitted pinned Java blobs or a non-export-filtered source checkout; do not overwrite the old archive"
            )
        if comparison["java_content_mismatches"]:
            result["collection_gaps"].append("Recover exact pinned bytes for mismatched Java blobs")
        if comparison["missing_repository_blob_paths"] or comparison["gitlinks"]:
            result["collection_gaps"].append(
                "Before runnable checkout claims, resolve omitted non-Java blobs and pinned submodule content against task scope"
            )
        if comparison["repository_blob_content_mismatches"]:
            result["collection_gaps"].append(
                "Review non-Java byte differences against export/line-ending attributes before asserting a byte-identical checkout"
            )
        for item in result["descriptors"]:
            tracked = next(
                (row for row in tree["tree"] if row.get("path") == item["source_path"]), None
            )
            item["pinned_blob_verified"] = bool(
                tracked
                and tracked.get("sha") == item["git_blob_sha1"]
                and tracked.get("type") == "blob"
            )
    else:
        result["source_completeness"] = "internal_consistency_only" if archive_ok else "unavailable"
        result["collection_gaps"].append(
            "Collect full Git tree at this exact CI SHA, retain raw response and request/hash metadata, then compare every Java blob"
        )
        for item in result["descriptors"]:
            item["pinned_blob_verified"] = False
    return result


def run(dataset: Path, output: Path, workers: int = 2) -> dict[str, Any]:
    base = dataset.parent.resolve()
    output = output.resolve()
    if output.is_relative_to(base):
        raise ValueError("New audit output must be outside the original dataset directory")
    output.mkdir(parents=True, exist_ok=True)
    data = json.loads(dataset.read_bytes())
    tasks = data["reference_tasks"]
    canonical_ids = {
        p.get("canonical_task_id") for p in data["projects"] if p.get("state") == "static_qualified"
    }
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for task in tasks:
        groups[task["source_archive"]].append(task)
    trees = index_trees(base)
    scans: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for name, group in groups.items():
            try:
                path = safe_ref(base, name)
                if not path.is_file():
                    raise ValueError("archive missing")
                requested = {"pom.xml"}
                requested.update(
                    x["path"]
                    for task in group
                    for x in (task.get("environment_and_commands", {}).get("default_goals") or [])
                    if isinstance(x, dict) and isinstance(x.get("path"), str)
                )
                futures[pool.submit(scan_archive, path, requested, output)] = name
            except (ValueError, OSError):
                scans[name] = None
        for future in as_completed(futures):
            name = futures[future]
            scans[name] = future.result()
            print(f"Audited {len(scans)}/{len(groups)} archives: {name}", flush=True)
    results = [
        audit_task(
            task,
            task["task_id"] in canonical_ids,
            scans.get(task["source_archive"]),
            base,
            output,
            trees,
        )
        for task in tasks
    ]
    canonical = [row for row in results if row["is_canonical"]]
    verified = [row for row in canonical if row["source_completeness"] == "java_content_verified"]
    summary = {
        "task_count": len(results),
        "canonical_project_count": len(canonical),
        "unique_archives": len(groups),
        "task_source_completeness": dict(Counter(x["source_completeness"] for x in results)),
        "canonical_source_completeness": dict(Counter(x["source_completeness"] for x in canonical)),
        "task_size_mismatch_count": sum(
            any(not m["equal"] for m in x.get("old_size_comparison", {}).values()) for x in results
        ),
        "canonical_repository_gaps": {
            key: sum(bool(row.get("tree_comparison", {}).get(key)) for row in canonical)
            for key in (
                "missing_repository_blob_paths",
                "repository_blob_content_mismatches",
                "gitlinks",
            )
        },
        "verified_canonical_size": {
            key: {
                "n": len(verified),
                "min": min((x["measured_size"][key] for x in verified), default=None),
                "median": (
                    statistics.median(x["measured_size"][key] for x in verified)
                    if verified
                    else None
                ),
                "max": max((x["measured_size"][key] for x in verified), default=None),
            }
            for key in ("java_files", "java_physical_lines", "java_source_bytes")
        },
    }
    audit = {
        "schema": SCHEMA,
        "dataset_base": str(base),
        "audit_base": str(output),
        "dataset_ref": ref(dataset.resolve(), base),
        "audit_script": {
            "path": str(Path(__file__).resolve()),
            "sha256": digest(Path(__file__).resolve()),
        },
        "method": {
            "offline": True,
            "executes_project_code": False,
            "extracts_checkout": False,
            "measurement_scope": MEASUREMENT,
            "size_thresholds": "None. Continuous measures retained; cohort owner freezes any sampling strata separately.",
            "binding": "Archive/source-review/request metadata hashes plus untruncated exact-repository/SHA Git tree; per-Java-file Git SHA1 blob and size comparison.",
            "status_definition": {
                "java_content_verified": "All tracked regular Java paths and content match an exact-SHA tree; does not assert full buildable checkout",
                "internal_consistency_only": "Archive integrity and source provenance agree, but no usable exact-SHA tree exists",
                "conflict": "Available exact tree conflicts with archive Java scope/content, or archive proof is invalid",
                "unavailable": "Archive or provenance cannot be verified",
            },
        },
        "summary": summary,
        "tasks": results,
        "canonical_projects": [
            {
                key: row.get(key)
                for key in (
                    "task_id",
                    "repo",
                    "sha",
                    "source_completeness",
                    "measured_size",
                    "issues",
                    "collection_gaps",
                )
            }
            for row in canonical
        ],
    }
    write_json(output / "audit.json", audit)
    lines = [
        "# Existing Java source archive audit",
        "",
        "This is a read-only re-audit of existing evidence. No new CI data, build or model run was performed.",
        "",
        MEASUREMENT,
        "",
        f"- Tasks: {len(results)}; canonical projects: {len(canonical)}; distinct archives: {len(groups)}.",
        f"- Task status: `{json.dumps(summary['task_source_completeness'], sort_keys=True)}`.",
        f"- Canonical status: `{json.dumps(summary['canonical_source_completeness'], sort_keys=True)}`.",
        f"- Tasks with recomputed size differing from old counts: {summary['task_size_mismatch_count']}.",
        "",
        "Java content verification does not prove a runnable checkout: export filtering, non-Java files, symlinks and submodules remain separately disclosed. Missing trees are not promoted by matching old totals. No project is excluded by a new size threshold.",
        "",
        f"Canonical repository gaps: `{json.dumps(summary['canonical_repository_gaps'], sort_keys=True)}`. Non-Java byte differences are retained as differences; this audit does not assume they are corruption or normalize them away.",
        "",
        "## Tasks needing source follow-up",
        "",
        "| Repository | Task | Status | Reason |",
        "|---|---|---|---|",
    ]
    for row in results:
        if row["source_completeness"] != "java_content_verified":
            lines.append(
                f"| {row['repo']} | {row['task_id']} | {row['source_completeness']} | {'; '.join(row['issues'] + row['collection_gaps']).replace('|', '/')} |"
            )
    lines += [
        "",
        "## Reproduce",
        "",
        f"`uv run --offline --no-sync python scripts/benchmark_source_audit.py --dataset {dataset} --output {output.relative_to(Path.cwd()) if output.is_relative_to(Path.cwd()) else output}`",
        "",
        "The machine-readable audit contains original references, raw hash bindings, tree comparisons, per-task continuous size, inventory references and copied POM descriptors. Descriptor paths and inventory paths are relative to audit_base; original provenance references are relative to dataset_base. Default goals are literal root-POM facts, not inferred effective lifecycle requirements.",
    ]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", type=Path, default=Path("output/java-benchmark-20260916/dataset.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("output/java-benchmark-rescreen-20260922/source-audit")
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        help="Archive scan concurrency only; not a dataset selection parameter",
    )
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    print(json.dumps(run(args.dataset, args.output, args.workers)["summary"], indent=2))


if __name__ == "__main__":
    main()
