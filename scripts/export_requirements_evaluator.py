#!/usr/bin/env python3
"""Export the stdlib evaluator and reviewed/draft metadata without changing v1.

Copies an explicit allowlist, never metadata preparation caches or credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import zipfile


def export_runtime(repo, destination):
    """Copy the explicit dependency closure for a standalone stdlib scorer."""
    destination.mkdir(parents=True, exist_ok=False)
    for module in (
        "__init__.py",
        "__main__.py",
        "requirements.py",
        "ci_count_semantics.py",
        "ci_sources.py",
        "ci_verification.py",
        "ci_native.py",
        "ci_jenkins.py",
        "empty_test_selection.py",
        "native_evidence.py",
        "native_hooks.py",
        "maven_trace.py",
        "maven_observation.py",
        "native_tests.py",
        "jvm_inputs.py",
        "compilation_evidence.py",
        "artifact_inventory.py",
        "installed_evidence.py",
        "wrapper_review.py",
        "evaluator.py",
        "recorder.py",
        "rat_attribution.py",
        "intervention_protocol.py",
    ):
        target = destination / "sag/benchmark" / module
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repo / "src/sag/benchmark" / module, target)
    (destination / "sag/__init__.py").write_text('"""Portable evaluator package."""\n')
    (destination / "sag/agent").mkdir()
    (destination / "sag/agent/__init__.py").write_text("")
    shutil.copyfile(
        repo / "src/sag/agent/worktree_evidence.py", destination / "sag/agent/worktree_evidence.py"
    )


def export(repo, metadata, original, destination):
    export_runtime(repo, destination)
    for directory in ("requirements", "sources"):
        if (metadata / directory).is_dir():
            shutil.copytree(metadata / directory, destination / directory)
    for directory in ("tasks", "historical-targets", "ci"):
        shutil.copytree(original / directory, destination / directory)
    shutil.copyfile(original / "manifest.json", destination / "original-manifest-v1.json")
    shutil.copyfile(metadata / "manifest.json", destination / "requirements-manifest-v2.json")
    shutil.copyfile(metadata / "README.md", destination / "metadata-review.md")
    shutil.copyfile(repo / "docs/benchmark-requirements-v2.md", destination / "README.md")
    hashes = {
        str(p.relative_to(destination)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(destination.rglob("*"))
        if p.is_file()
    }
    (destination / "checksums.json").write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n")
    archive = destination.with_suffix(".zip")
    if archive.exists():
        raise ValueError("Refusing to replace an existing export archive")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as out:
        for p in sorted(destination.rglob("*")):
            if p.is_file():
                out.write(p, p.relative_to(destination))
    return {
        "directory": str(destination),
        "archive": str(archive),
        "files": len(hashes),
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            export(Path(__file__).resolve().parents[1], args.metadata, args.original, args.output),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
