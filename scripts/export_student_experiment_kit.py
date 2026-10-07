#!/usr/bin/env python3
"""Export a versioned five-project protocol kit from a verified archived replay.

Only byte-verified scorer inputs are exported as fixtures. These are historical
protocol checks, not a new success-rate experiment or an agent training set.
"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile

from scripts.export_requirements_evaluator import export_runtime
from sag.benchmark import requirements as definitions
from sag.benchmark.ci_sources import archive_sources
from sag.benchmark.evaluator import evaluate


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def export(repo, replay, destination, guide):
    report = definitions.load_json(replay / "results.json")
    if not report["original_inputs_unchanged"] or report["new_agent_or_model_calls"] != 0:
        raise ValueError("Expected a read-only archived-evidence replay")
    # Refuse to export a later implementation under an earlier replay's label.
    for relative, expected in report["source_hashes"].items():
        if digest(repo / relative) != expected:
            raise ValueError("Re-run the archived replay after changing " + relative)
    export_runtime(repo, destination)
    (destination / "requirements").mkdir()
    (destination / "fixtures").mkdir()
    projects = []
    for row in report["subjects"]:
        name = row["project"]
        original_run = repo / row["run"]
        session = original_run.parents[2]
        original_sources = repo / row["source_root"]
        project = destination / "projects" / name
        project.mkdir(parents=True)
        for filename in ("task.json", "requirements.json", "ci-target.json"):
            shutil.copyfile(original_sources / filename, project / filename)
        task = definitions.load_json(project / "task.json")
        spec = definitions.load_requirements(project / "requirements.json", task)
        archive_sources(spec, original_sources, project)
        shutil.copyfile(
            project / "requirements.json", destination / "requirements" / (name + ".json")
        )
        fixture = destination / "fixtures" / name
        fixture.mkdir()
        reads = set()
        original_digest = definitions.file_digest

        def track(path):
            path = Path(path).resolve()
            reads.add(path)
            return original_digest(path)

        try:
            definitions.file_digest = track
            run = definitions.load_json(original_run)
            expected = evaluate(task, spec, run, session, ci_source_base=project)
        finally:
            definitions.file_digest = original_digest
        prior = definitions.load_json(replay / (name + ".json"))
        if expected != prior:
            raise ValueError("Export differs from archived replay for " + name)
        # File access tracking uses the common byte-binding primitive. Never
        # copy the full session (which also contains prompts and other logs).
        for source in sorted(reads):
            if not source.is_relative_to(session.resolve()):
                continue  # Official CI/model closure was exported separately.
            target = fixture / "evidence" / source.relative_to(session.resolve())
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if digest(target) != original_digest(source):
                raise ValueError("Fixture source changed during export")
        shutil.copyfile(original_run, fixture / "run.json")
        write_json(fixture / "expected.json", expected)
        relocated = evaluate(task, spec, run, fixture / "evidence", ci_source_base=project)
        if relocated != expected:
            raise ValueError("Fixture cannot be replayed without original paths: " + name)
        projects.append(
            {
                "project": name,
                "repo": task["repo"],
                "commit": task["sha"],
                "evaluation_identity": definitions.evaluation_identity(spec),
                "comparison_admitted": spec["ci_alignment"]["comparison_admitted"],
                "selected_ci_url": spec["ci_alignment"]["selected_url"],
                "selected_ci_cell": spec["ci_alignment"]["selected_cell"],
                "expected_fixture_status": expected["status"],
                "expected_ci_scope": expected["ci_verification"]["scope"]["status"],
                "case_identity_comparison": "unavailable",
            }
        )
    shutil.copyfile(repo / "docs/student-experiment-guide-v2.md", destination / "README.md")
    shutil.copyfile(repo / "docs/benchmark-requirements-v2.md", destination / "PROTOCOL.md")
    shutil.copyfile(guide, destination / "Student_Experiment_Guide_v2.docx")
    for name in ("verify_bundle.py", "sag-mini.env.example"):
        shutil.copyfile(repo / "scripts/student_kit" / name, destination / name)
    historical = definitions.load_json(repo / "output/sag-benchmark20-20260917/manifest.json")
    write_json(
        destination / "historical-cohort-v1.json",
        {
            "usage": "Historical inventory only; not a ready v2 experiment manifest",
            "benchmark": historical["benchmark"],
            "projects": [
                {
                    "id": p["id"],
                    "repo": p["repo"],
                    "commit": p["commit"],
                    "selected_ci_url": p["ci"].get("selected_url"),
                    "status": "historical_v1_inventory_only",
                }
                for p in historical["projects"]
            ],
        },
    )
    # Include current harness bytes because a dirty checkout's HEAD alone is
    # insufficient to reproduce the implementation. This is source, not an image
    # or a claim that the student's launcher has passed live integration tests.
    source_files = [repo / name for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE")]
    for directory in ("src", "scripts"):
        source_files.extend(
            p
            for p in (repo / directory).rglob("*")
            if p.is_file()
            and "__pycache__" not in p.parts
            and not p.name.endswith((".pyc", ".pyo"))
        )
    if any(p.is_symlink() for p in source_files):
        raise ValueError("Source export refuses symlinks")
    source_hashes = {str(p.relative_to(repo)): digest(p) for p in sorted(source_files)}
    with tarfile.open(destination / "harness-source.tar.gz", "x:gz") as archive:
        for source in sorted(source_files):
            archive.add(source, arcname=str(source.relative_to(repo)), recursive=False)
    write_json(destination / "harness-source-checksums.json", source_hashes)
    base_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    write_json(
        destination / "manifest.json",
        {
            "schema_version": 1,
            "kit": "sag-student-pilot-v2-20260923",
            "purpose": "Five-subject recorder/scorer qualification, not a benchmark success-rate estimate",
            "evaluation_protocol": "requirements-v2",
            "ci_protocol": "selected-ci-verification-v2",
            "base_git_commit": base_commit,
            "implementation_identity": "harness-source-checksums.json",
            "base_commit_alone_is_not_source_identity": True,
            "prospective_live_student_runner_validated": False,
            "fixtures_are_archived_not_new_agent_runs": True,
            "projects": projects,
        },
    )
    hashes = {
        str(p.relative_to(destination)): digest(p)
        for p in sorted(destination.rglob("*"))
        if p.is_file()
    }
    write_json(destination / "checksums.json", hashes)
    result = subprocess.run(
        [sys.executable, "-I", str(destination / "verify_bundle.py")],
        cwd=destination.parent,
        capture_output=True,
        text=True,
        check=True,
    )
    archive_path = destination.with_suffix(".zip")
    with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in sorted([*hashes, "checksums.json"]):
            archive.write(destination / relative, relative)
    return {
        "archive": str(archive_path),
        "sha256": digest(archive_path),
        "bytes": archive_path.stat().st_size,
        "files": len(hashes),
        "self_test": json.loads(result.stdout),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--guide", required=True, type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            export(
                Path(__file__).resolve().parents[1],
                args.replay.resolve(),
                args.output.resolve(),
                args.guide.resolve(),
            ),
            indent=2,
        )
    )
