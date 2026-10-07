#!/usr/bin/env python3
"""Audit v1 archives with v2 without inventing missing prospective evidence.

This is a compatibility replay, not a new run or a performance comparison.
Archived XML counts are independently reported as bytes observed in the archive;
they are never attached to a fabricated fresh acceptance invocation.
"""

import argparse
import json
from pathlib import Path
import tarfile
import xml.etree.ElementTree as ET

from sag.benchmark.evaluator import evaluate
from sag.benchmark.requirements import aggregate, file_digest, load_json, load_requirements


def archive_inventory(path):
    result = {
        "path": str(path),
        "sha256": file_digest(path),
        "reports": [],
        "counts": {
            "tests": 0,
            "failures": 0,
            "errors": 0,
            "skipped": 0,
            "case_elements": 0,
        },
        "scope_and_freshness": "not_certified_by_this_inventory",
    }
    # Never extract archive-supplied paths onto the filesystem.
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            if not member.isfile() or not member.name.endswith(".xml"):
                continue
            stream = archive.extractfile(member)
            raw = stream.read()
            try:
                root = ET.fromstring(raw)
            except ET.ParseError:
                continue
            suites = [
                s
                for s in root.iter()
                if s.tag.rsplit("}", 1)[-1] == "testsuite"
                and not any(c.tag.rsplit("}", 1)[-1] == "testsuite" for c in s)
            ]
            if not suites:
                continue
            import hashlib

            counts = {
                k: sum(int(s.get(k, "0")) for s in suites)
                for k in ("tests", "failures", "errors", "skipped")
            }
            counts["case_elements"] = sum(
                c.tag.rsplit("}", 1)[-1] == "testcase" for c in root.iter()
            )
            result["reports"].append(
                {"archive_member": member.name, "sha256": hashlib.sha256(raw).hexdigest(), **counts}
            )
            for k, v in counts.items():
                result["counts"][k] += v
    result["report_count"] = len(result["reports"])
    return result


def replay(repo, bundle, metadata, output):
    output.mkdir(parents=True, exist_ok=False)
    specs, scores, rows = {}, [], []
    for project in load_json(bundle / "manifest.json")["projects"]:
        project_id = project["id"]
        task = load_json(bundle / project["task"]["path"])
        spec = load_requirements(metadata / "requirements" / f"{project_id}.json", task)
        specs[project_id] = spec
        archive = (
            repo
            / "logs"
            / f"advisor-high20-r2-mini-high-{project_id}-20260914"
            / "runs"
            / project_id
        )
        collected_path = archive / "collected.json"
        collected = load_json(collected_path) if collected_path.is_file() else {}
        pin = collected.get("pin", {})
        # The old producer did not record a v2 requirements identity or v2 boundary
        # snapshots. Do not assert either on its behalf just to make it scoreable.
        run = {
            "run_id": collected.get("run_id"),
            "agent": "SAG archived v1",
            "repo": task["repo"],
            "commit": pin.get("target_repo_sha"),
            "evaluation_identity": pin.get("sanitized_config", {}).get("evaluation_protocol"),
            "invocations": [],
            "worktree": [],
        }
        score = evaluate(task, spec, run, archive)
        scores.append(score)
        (output / f"{project_id}.json").write_text(json.dumps(score, indent=2) + "\n")
        row = {
            "project_id": project_id,
            "archive_present": bool(collected),
            "archived_verdict_unchanged": collected.get("metrics", {}).get("verdict"),
            "archived_test_metrics_unchanged": {
                k: v for k, v in collected.get("metrics", {}).items() if k.startswith("unique_")
            },
            "v2_status": score["status"],
            "v2_human_interventions": score["human_interventions"],
            "interpretation": "missing_v2_prospective_evidence_is_not_a_new_build_failure",
        }
        if collected:
            row["archive_source"] = {
                "path": str(collected_path),
                "sha256": file_digest(collected_path),
            }
        reports = archive / "physical-test-reports.tar.gz"
        if reports.is_file():
            row["raw_report_inventory"] = archive_inventory(reports)
        rows.append(row)
    report = {
        "kind": "historical_protocol_compatibility_replay",
        "new_executions": 0,
        "aggregate": aggregate(specs, scores),
        "projects": rows,
        "limitation": "No v2 run identity, boundary evidence or intervention zeros are backfilled. "
        "Archive inventory is descriptive, not acceptance freshness or CI equivalence.",
    }
    path = output / "replay.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(replay(Path(__file__).resolve().parents[1], args.bundle, args.metadata, args.output))


if __name__ == "__main__":
    main()
