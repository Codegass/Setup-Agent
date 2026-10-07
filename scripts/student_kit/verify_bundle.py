#!/usr/bin/env python3
"""Verify portable bytes and replay five archived fixtures without Java/network."""

import hashlib
import json
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(BASE))


def main():
    hashes = json.loads((BASE / "checksums.json").read_text())
    for relative, expected in hashes.items():
        path = BASE / relative
        if path.is_symlink() or not path.resolve().is_relative_to(BASE):
            raise ValueError("Invalid bundle path: " + relative)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Checksum mismatch: " + relative)
    from sag.benchmark.requirements import load_json, load_requirements, aggregate
    from sag.benchmark.evaluator import evaluate
    from sag.benchmark.ci_verification import verify_reference

    manifest = load_json(BASE / "manifest.json")
    rows, specs = [], {}
    for project in manifest["projects"]:
        name = project["project"]
        folder, fixture = BASE / "projects" / name, BASE / "fixtures" / name
        task = load_json(folder / "task.json")
        spec = load_requirements(folder / "requirements.json", task)
        reference = verify_reference(task, spec, folder)
        score = evaluate(
            task, spec, load_json(fixture / "run.json"), fixture / "evidence", ci_source_base=folder
        )
        if score != load_json(fixture / "expected.json"):
            raise ValueError("Archived replay changed: " + name)
        # Admission is frozen independently of the ability to parse CI counts.
        if not project["comparison_admitted"] and score["ci_scope_attainment"] is not None:
            raise ValueError("Unadmitted task acquired an official score")
        specs[name] = spec
        rows.append(
            {
                "project": name,
                "local": score["status"],
                "ci_scope": score["ci_verification"]["scope"]["status"],
                "ci_assessed": reference["counts"]["assessed"],
            }
        )
    empty = aggregate(specs, [])
    if empty["planned_projects"] != len(rows) or empty["task_success"]["unavailable"] != len(rows):
        raise ValueError("Missing attempts removed from the planned denominator")
    print(
        json.dumps(
            {
                "verified_files": len(hashes),
                "archived_fixtures": rows,
                "missing_attempt_denominator": len(rows),
                "new_agent_runs": 0,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
