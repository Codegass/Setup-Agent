"""Capture a bounded, exact-SHA descriptor sample; never execute project files."""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from scripts.benchmark_source_audit import blob_sha, write_json
from scripts.benchmark_source_recovery import capture, file_ref

BASE = Path("output/java-benchmark-org-expansion-20260922/eclipse/source-applicability")
ROOT_FILES = {
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "gradle.properties",
    "Makefile",
    "CMakeLists.txt",
    "configure",
    "configure.ac",
    "build.xml",
    "README",
    "README.md",
    "README.adoc",
    "README.txt",
    "CONTRIBUTING.md",
    "gradlew",
    "mvnw",
}


def safe_source_path(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Invalid source path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(piece in {".", "..", ""} for piece in value.split("/")):
        raise ValueError("Source path escapes or aliases its scope")
    return path.parts


class Source:
    def __init__(self, base, repo, sha, network=False):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("Invalid repository identity")
        safe_source_path(repo)
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("Immutable commit required")
        self.base, self.repo, self.sha, self.network = Path(base), repo, sha, network
        self.folder = self.base / "captures" / repo / sha
        self.commit_ref, commit = capture(
            f"repos/{repo}/git/commits/{sha}", self.folder / "commit", self.base, network
        )
        if commit.get("sha") != sha or not re.fullmatch(
            r"[0-9a-f]{40}", commit.get("tree", {}).get("sha", "")
        ):
            raise ValueError("Commit identity or root tree binding mismatch")
        self.root_sha = commit["tree"]["sha"]
        self.trees = {}

    def tree(self, sha):
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("Invalid tree object identity")
        if sha not in self.trees:
            proof, data = capture(
                f"repos/{self.repo}/git/trees/{sha}",
                self.folder / "trees" / sha,
                self.base,
                self.network,
            )
            if (
                data.get("sha") != sha
                or data.get("truncated") is not False
                or not isinstance(data.get("tree"), list)
            ):
                raise ValueError("Tree is truncated or not bound to expected identity")
            paths = [row["path"] for row in data["tree"]]
            if len(paths) != len(set(paths)):
                raise ValueError("Duplicate tree entries")
            self.trees[sha] = proof, data["tree"]
        return self.trees[sha]

    def file(self, path):
        pieces = safe_source_path(path)
        sha, proofs = self.root_sha, []
        for offset, piece in enumerate(pieces):
            proof, entries = self.tree(sha)
            proofs.append(proof)
            item = next((entry for entry in entries if entry["path"] == piece), None)
            if item is None:
                return {"source_path": path, "status": "absent", "tree_evidence": proofs}
            if offset < len(pieces) - 1:
                if item["type"] != "tree" or item["mode"] != "040000":
                    return {
                        "source_path": path,
                        "status": "non_directory_ancestor",
                        "tree_evidence": proofs,
                    }
                sha = item["sha"]
        if item["type"] != "blob" or item["mode"] not in {"100644", "100755"}:
            return {
                "source_path": path,
                "status": "not_regular_file",
                "tree_entry": item,
                "tree_evidence": proofs,
            }
        if not re.fullmatch(r"[0-9a-f]{40}", item.get("sha", "")):
            raise ValueError("Invalid blob object identity")
        ref, value = capture(
            f"repos/{self.repo}/git/blobs/{item['sha']}",
            self.folder / "blobs" / item["sha"],
            self.base,
            self.network,
        )
        if value.get("sha") != item["sha"] or value.get("encoding") != "base64":
            raise ValueError("Blob identity or encoding mismatch")
        raw = base64.b64decode("".join(value["content"].split()), validate=True)
        if (
            len(raw) != item["size"]
            or len(raw) != value.get("size")
            or blob_sha(raw) != item["sha"]
        ):
            raise ValueError("Source bytes differ from pinned Git blob")
        dest = self.base / "objects" / hashlib.sha256(raw).hexdigest()
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.read_bytes() != raw:
            raise ValueError("Existing object bytes changed")
        dest.write_bytes(raw)
        return {
            "source_path": path,
            "status": "verified",
            "blob_sha": item["sha"],
            "file": file_ref(dest, self.base),
            "tree_evidence": proofs,
            "blob_evidence": ref,
        }


def capture_project(row, base, network):
    repo, sha = row["repo"], row["default_branch_sha"]
    result = {
        "repo": repo,
        "repository_id": row["repository_id"],
        "default_branch_sha": sha,
        "source_scope": "Candidate default branch snapshot; not automatically the selected CI revision",
        "files": [],
        "errors": [],
        "task_applicability": "not_reviewed",
    }
    try:
        source = Source(base, repo, sha, network)
        tree_proof, root = source.tree(source.root_sha)
        result.update(
            commit_evidence=source.commit_ref,
            root_tree_sha=source.root_sha,
            root_tree_evidence=tree_proof,
            root_entries=root,
        )
        chosen = sorted(item["path"] for item in root if item["path"] in ROOT_FILES)
        result["files"] = [source.file(path) for path in chosen]
    except (ValueError, KeyError, TypeError, OSError) as exc:
        result["errors"].append(str(exc))
    return result


def capture_supplemental(plan_path, base, network):
    plan = json.loads(plan_path.read_bytes())
    results = []
    for row in plan["repositories"]:
        source = Source(base, row["repo"], row["sha"], network)
        files = [source.file(path) for path in row["source_paths"]]
        results.append(
            {
                "repo": row["repo"],
                "sha": row["sha"],
                "reason": row["reason"],
                "commit_evidence": source.commit_ref,
                "files": files,
            }
        )
        print(row["repo"], len(files), "supplemental paths", flush=True)
    result = {"plan": file_ref(plan_path, base), "repositories": results}
    write_json(base / "supplemental-inventory.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", action="store_true")
    parser.add_argument("--output", type=Path, default=BASE)
    parser.add_argument("--supplemental-plan", type=Path)
    args = parser.parse_args()
    base = args.output.resolve()
    base.mkdir(parents=True, exist_ok=True)
    if args.supplemental_plan:
        capture_supplemental(args.supplemental_plan.resolve(), base, args.network)
        return
    candidate_path = base.parent / "candidates.json"
    candidates = json.loads(candidate_path.read_bytes())
    selected = sorted(
        (row for row in candidates["repositories"] if row["new_candidate"]),
        key=lambda row: row["repo"],
    )[:10]
    plan = {
        "schema": "eclipse-source-applicability-batch-v1",
        "candidate_source": file_ref(candidate_path, base.parent),
        "selection": "First ten by repository lexicographic order: a fixed operational review batch, not an eligibility threshold.",
        "root_file_names": sorted(ROOT_FILES),
        "repositories": [
            {key: row[key] for key in ("repo", "repository_id", "default_branch_sha")}
            for row in selected
        ],
    }
    plan_path = base / "plan.json"
    if plan_path.exists() and json.loads(plan_path.read_bytes()) != plan:
        raise ValueError("Review plan already frozen with different source or scope")
    write_json(plan_path, plan)
    results = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(capture_project, row, base, args.network) for row in selected]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(result["repo"], len(result["files"]), result["errors"], flush=True)
    results.sort(key=lambda row: row["repo"])
    write_json(
        base / "inventory.json",
        {
            "plan": file_ref(plan_path, base),
            "repositories": results,
            "source_execution": "none",
            "scope": "Static descriptors only; no full checkout/source-size completeness claim",
        },
    )


if __name__ == "__main__":
    main()
