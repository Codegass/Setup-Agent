#!/usr/bin/env python3
"""Exercise an exported package with Python -S, Git and a synthetic Maven fixture.

No Java, real project build, network or model request is performed.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def verify(bundle, output):
    bundle = bundle.resolve()
    hashes = json.loads((bundle / "checksums.json").read_text())
    planned = len(json.loads((bundle / "original-manifest-v1.json").read_text())["projects"])
    for relative, expected in hashes.items():
        if hashlib.sha256((bundle / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("Export checksum mismatch: " + relative)
    output.mkdir(parents=True, exist_ok=False)
    env = {
        **os.environ,
        "PYTHONPATH": str(bundle),
        "PYTHONNOUSERSITE": "1",
        "MAVEN_SKIP_RC": "true",
    }
    for name in (
        "MAVEN_ARGS",
        "MAVEN_OPTS",
        "MAVEN_CONFIG",
        "MAVEN_PROJECTBASEDIR",
        "JAVA_TOOL_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "_JAVA_OPTIONS",
    ):
        env.pop(name, None)

    def run(*args, cwd=bundle):
        result = subprocess.run(list(args), cwd=cwd, env=env, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        return result.stdout

    run(
        sys.executable,
        "-S",
        "-m",
        "sag.benchmark",
        "aggregate",
        "--requirements-dir",
        str(bundle / "requirements"),
        "--output",
        str(output / "empty-planned-cohort.json"),
    )
    empty = json.loads((output / "empty-planned-cohort.json").read_text())
    if empty["planned_projects"] != planned or empty["task_success"]["unavailable"] != planned:
        raise AssertionError("Missing attempts changed the frozen denominator")
    run(
        sys.executable,
        "-S",
        "-c",
        "from pathlib import Path; from sag.benchmark.requirements import load_json,load_requirements,validate_ci_count_metadata; specs=list(Path('requirements').glob('*.json')); [validate_ci_count_metadata(load_requirements(p,load_json(Path('tasks')/p.name)),base=Path('.'),required=True) for p in specs]; print(len(specs))",
    )

    with tempfile.TemporaryDirectory(
        prefix="sag-portable-fixture-", dir="/private/tmp"
    ) as temporary:
        base = Path(temporary)
        checkout, binaries = base / "checkout", base / "bin"
        checkout.mkdir()
        binaries.mkdir()
        (checkout / "pom.xml").write_text("<project/>\n")
        run("git", "init", "-q", str(checkout))
        run("git", "-C", str(checkout), "add", ".")
        run(
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.name=Protocol Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        )
        commit = run("git", "-C", str(checkout), "rev-parse", "HEAD").strip()
        launcher = binaries / "mvn"
        launcher.write_text("#!" + sys.executable + "\n" + """import sys
from pathlib import Path
if "--version" in sys.argv or "-version" in sys.argv:
    print("Apache Maven 3.9.16\\nJava version: 17.0.12")
else:
    report = Path("target/surefire-reports/TEST-demo.xml")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text('<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase classname="demo" name="works"/></testsuite>')
    print("[INFO] --- maven-compiler-plugin:1.0:compile (default-compile) @ demo ---")
    print("[INFO] Compiling 1 source file")
    print("[INFO] --- maven-surefire-plugin:1.0:test (default-test) @ demo ---")
    print("[INFO] Tests run: 1, Failures: 0, Errors: 0, Skipped: 0")
    print("[INFO] BUILD SUCCESS")
""")
        launcher.chmod(0o755)
        env["PATH"] = str(binaries) + os.pathsep + env["PATH"]
        task = {
            "schema_version": 1,
            "repo": "example/protocol-fixture",
            "sha": commit,
            "steps": [
                {
                    "id": "test",
                    "runner": "maven",
                    "cwd": ".",
                    "java_major": 17,
                    "maven_version": "3.9.16",
                    "argv": ["mvn", "-B", "-f", "pom.xml", "-V", "clean", "test", "--batch-mode"],
                }
            ],
        }
        rows = []
        for index, (kind, goal, execution) in enumerate(
            (
                ("compile", "compiler:compile", "default-compile"),
                ("test", "surefire:test", "default-test"),
            )
        ):
            rows.append(
                {
                    "id": kind,
                    "step_id": "test",
                    "kind": kind,
                    "subtype": "unit" if kind == "test" else "production",
                    "module": "demo",
                    "scope": {"status": "declared", "module_path": "."},
                    "depends_on": ["compile"] if kind == "test" else [],
                    "dependencies_complete": True,
                    "validation": {
                        "rule": "junit" if kind == "test" else "native_goal",
                        "goals": [goal],
                        "execution": execution,
                        "occurrence": 0,
                        "plan_resolved": True,
                        "position": index,
                    },
                }
            )
        task_hash = hashlib.sha256(
            json.dumps(task, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest()
        spec = {
            "schema_version": 2,
            "policy_version": "ci-requirements-v2",
            "project_id": "protocol-fixture",
            "task_sha256": task_hash,
            "annotation_completeness": {"status": "complete"},
            "preconditions": [{"id": "worktree_integrity"}, {"id": "runtime_conformance"}],
            "requirements": rows,
        }
        for name, data in (("task", task), ("requirements", spec)):
            (output / f"{name}.json").write_text(json.dumps(data, indent=2) + "\n")
        records = output / "records"
        common = (
            "--task",
            str(output / "task.json"),
            "--requirements",
            str(output / "requirements.json"),
            "--repo-root",
            str(checkout),
            "--records",
            str(records),
            "--run-id",
            "export-transport-fixture",
        )
        for command, options in (
            ("start", ()),
            ("step", ("--step-id", "test", "--timeout", "10")),
            ("close", ()),
        ):
            run(sys.executable, "-S", "-m", "sag.benchmark.recorder", command, *common, *options)
        run(
            sys.executable,
            "-S",
            "-m",
            "sag.benchmark",
            "score",
            "--task",
            str(output / "task.json"),
            "--requirements",
            str(output / "requirements.json"),
            "--run",
            str(records / "run.json"),
            "--output",
            str(output / "fixture-score.json"),
        )
        score = json.loads((output / "fixture-score.json").read_text())
        if score["status"] != "complete" or score["human_interventions"] is not None:
            raise AssertionError("Fixture score differs: " + json.dumps(score))
    report = {
        "kind": "synthetic_export_transport_validation",
        "python_site_packages": False,
        "java_builds": 0,
        "model_calls": 0,
        "network_requests": 0,
        "checksummed_files": len(hashes),
        "planned_missing_preserved": planned,
        "fixture_status": score["status"],
        "human_interventions_not_fabricated": score["human_interventions"] is None,
    }
    (output / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.bundle, args.output.resolve()), indent=2))
