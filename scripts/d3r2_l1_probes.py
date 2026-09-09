#!/usr/bin/env python3
"""Two fixed, no-model Docker fixtures for the R11 L1 production tool boundary.

Prepare records the exact source, fixture files and command order without
starting Docker. Run creates one fresh container per fixture and retains every
attempt, including the deliberately red Python environment control.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import shlex
import shutil
import subprocess
import tarfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAMES = ("maven-parent-shade", "make-pytest-repair")


def fixtures() -> dict[str, dict[str, str]]:
    parent = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion><groupId>org.sag.fixture</groupId>
  <artifactId>l1-parent</artifactId><version>1.0</version><packaging>pom</packaging>
  <modules><module>producer</module><module>consumer</module></modules>
  <properties><maven.compiler.release>17</maven.compiler.release>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
  <build><plugins>
    <plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-compiler-plugin</artifactId><version>3.13.0</version></plugin>
    <plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-surefire-plugin</artifactId><version>3.5.2</version></plugin>
    <plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-enforcer-plugin</artifactId><version>3.5.0</version>
      <executions><execution><goals><goal>enforce</goal></goals><configuration><rules>
        <requireJavaVersion><version>11</version></requireJavaVersion>
      </rules></configuration></execution></executions></plugin>
  </plugins></build>
</project>
"""
    child = """<parent><groupId>org.sag.fixture</groupId><artifactId>l1-parent</artifactId>
      <version>1.0</version><relativePath>../pom.xml</relativePath></parent>"""
    return {
        "maven-parent-shade": {
            ".gitignore": "**/target/\n**/dependency-reduced-pom.xml\n",
            "pom.xml": parent,
            "producer/pom.xml": f"""<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>
  {child}<artifactId>l1-producer</artifactId>
  <dependencies><dependency><groupId>org.apache.commons</groupId><artifactId>commons-lang3</artifactId><version>3.17.0</version></dependency></dependencies>
  <build><plugins><plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-shade-plugin</artifactId><version>3.6.0</version>
    <executions><execution><phase>package</phase><goals><goal>shade</goal></goals><configuration>
      <relocations><relocation><pattern>org.apache.commons.lang3</pattern><shadedPattern>org.sag.fixture.vendor</shadedPattern></relocation></relocations>
    </configuration></execution></executions>
  </plugin></plugins></build></project>
""",
            "producer/src/main/java/org/sag/fixture/Greeting.java": """package org.sag.fixture;
public final class Greeting {
  public static String value() { return org.apache.commons.lang3.StringUtils.upperCase("ready"); }
}
""",
            "consumer/pom.xml": f"""<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>
  {child}<artifactId>l1-consumer</artifactId><dependencies>
    <dependency><groupId>org.sag.fixture</groupId><artifactId>l1-producer</artifactId><version>1.0</version></dependency>
    <dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency>
  </dependencies></project>
""",
            "consumer/src/test/java/org/sag/fixture/GreetingTest.java": """package org.sag.fixture;
public class GreetingTest {
  @org.junit.Test public void shadedDependencyWorks() {
    org.junit.Assert.assertEquals("READY", Client.value());
  }
}
""",
            "consumer/src/main/java/org/sag/fixture/Client.java": """package org.sag.fixture;
public final class Client {
  public static String value() { return Greeting.value(); }
}
""",
            "README.md": "Build producer first: mvn clean install -pl producer -am -DskipTests\nThen verify: mvn verify\nThe parent Enforcer floor is Java 11; compiler release is 17.\n",
        },
        "make-pytest-repair": {
            ".gitignore": ".venv/\n.ready\n__pycache__/\n.pytest_cache/\n*.egg-info/\nbuild/\ndist/\n",
            "pyproject.toml": '[build-system]\nrequires = ["setuptools>=68"]\nbuild-backend = "setuptools.build_meta"\n[project]\nname = "sag-l1-make-fixture"\nversion = "1.0"\nrequires-python = ">=3.10"\n',
            "fixture_module.py": 'VALUE = "ready"\n',
            "Makefile": ".PHONY: unit prepare\nunit: prepare\n\tpython -m pytest tests/\nprepare:\n\tprintf ready > .ready\n",
            "tests/test_ready.py": 'from pathlib import Path\ndef test_ready():\n    assert Path(".ready").is_file(), "fixture environment not prepared"\n',
            "README.md": "make unit prepares .ready then runs python -m pytest tests/.\nThe negative control omits make prepare. The repair changes only .ready.\n",
        },
    }


def steps(name: str) -> list[dict]:
    cwd = f"/workspace/{name}"
    if name == "maven-parent-shade":
        return [
            {
                "tool": "build",
                "params": {
                    "action": "install",
                    "system": "maven",
                    "source_command": "mvn clean install -pl producer -am -DskipTests",
                    "args": "clean -pl producer -am -DskipTests",
                    "working_directory": cwd,
                    "timeout": 900,
                },
            },
            {
                "tool": "build",
                "params": {
                    "action": "verify",
                    "system": "maven",
                    "source_command": "mvn verify",
                    "working_directory": cwd,
                    "timeout": 900,
                },
            },
        ]
    test = {
        "action": "test",
        "system": "python",
        "source_command": "python -m pytest tests/",
        "args": "tests/",
        "working_directory": cwd,
        "timeout": 180,
    }
    return [
        {"tool": "build", "params": dict(test), "expect": "one red; environment marker absent"},
        {
            "tool": "bash",
            "params": {"command": "make prepare", "working_directory": cwd, "timeout": 60},
            "expect": "environment preparation only",
        },
        {
            "tool": "build",
            "params": dict(test),
            "expect": "one green; retry references first test contract",
        },
    ]


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def source_hashes() -> dict[str, str]:
    paths = sorted((ROOT / "src/sag").rglob("*.py")) + [
        Path(__file__),
        ROOT / "scripts/run_d0_docker_probes.py",
    ]
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str) + "\n")


def require_source(plan: dict) -> None:
    import sag

    if not Path(sag.__file__).resolve().is_relative_to(ROOT):
        raise RuntimeError("L1 imported SAG from another source checkout")
    if git("rev-parse", "HEAD") != plan["candidate_sha"] or (
        not plan.get("allow_dirty_source") and git("status", "--porcelain")
    ):
        raise RuntimeError("L1 requires the clean frozen candidate source")
    if source_hashes() != plan["source_sha256"]:
        raise RuntimeError("L1 candidate source changed after prepare")


def prepare(
    out: Path, *, expected_sha: str, image_id: str, allow_dirty_source: bool = False
) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", image_id
    ):
        raise ValueError("full candidate commit and image SHA256 are required")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,60}", out.name):
        raise ValueError("campaign directory name must be a short lowercase identifier")
    plan = {
        "schema_version": 1,
        "candidate_sha": expected_sha,
        "image": {"id": image_id},
        "source_sha256": source_hashes(),
        "mode": "fixed controller actions through production tools; no model phase lifecycle or CI attainment claim",
        "cache": "new container per fixture; image contents only; no host cache mount",
        "fixture_files": fixtures(),
        "steps": {name: steps(name) for name in NAMES},
        "model_pin": "none:no-model",
        "host_arch": platform.machine(),
        "prepared_at": datetime.now(timezone.utc).isoformat(),
    }
    plan.update(
        allow_dirty_source=allow_dirty_source, initial_git_status=git("status", "--porcelain")
    )
    require_source(plan)
    out.mkdir(parents=True, exist_ok=False)
    write(out / "protocol.json", plan)
    with tarfile.open(out / "source.tar.gz", "w:gz") as archive:
        for relative in plan["source_sha256"]:
            archive.add(ROOT / relative, arcname=relative)
    require_source(plan)


def report_facts(audit, receipts: list[dict]) -> list[dict]:
    """Read only receipt-claimed XML; retain every attempt's separate report."""
    from sag.runtime.container_io import read_container_text

    facts = []
    for receipt in receipts:
        reports = []
        for bucket in ("new", "changed"):
            for row in receipt["report_delta"][bucket]:
                body = read_container_text(audit, row["path"], exact_bytes=True)
                if body is None or hashlib.sha256(body.encode()).hexdigest() != row["sha256"]:
                    raise RuntimeError(f"receipt-claimed XML hash is unavailable: {row['path']}")
                cases = list(ET.fromstring(body).iter("testcase"))
                reports.append(
                    {
                        **row,
                        "executed": len(cases),
                        "red": sum(
                            any(child.tag in {"failure", "error"} for child in case)
                            for case in cases
                        ),
                    }
                )
        if reports:
            facts.append(
                {
                    "receipt_id": receipt["receipt_id"],
                    "contract_id": receipt["contract_id"],
                    "exit_code": receipt.get("exit_code"),
                    "cwd": receipt["actual_cwd"],
                    "argv": receipt["argv"],
                    "reports": reports,
                    "executed": sum(row["executed"] for row in reports),
                    "red": sum(row["red"] for row in reports),
                }
            )
    return facts


def archive_fixture(runtime, audit, observed: dict) -> None:
    """Retain D0's byte/publication checks without its fixed probe-name registry."""
    from scripts.run_d0_docker_probes import _verify_archived_evidence_epochs

    destination = runtime.artifact_dir
    destination.mkdir(parents=True, exist_ok=False)
    epoch = runtime.epoch_for(audit)
    container = destination / "container-1"
    container.mkdir()
    shutil.copy2(epoch.control_event_path, container / "control-events.jsonl")
    write(
        container / "evidence-epoch.json",
        {
            "schema_version": 1,
            "run_id": epoch.run_id,
            "container_name": audit.container_name,
            "host_control_event_path": str(epoch.control_event_path),
            "control_event_path": "container-1/control-events.jsonl",
        },
    )
    write(destination / "result.json", observed)
    write(destination / "run-pin.json", runtime.run_pin)
    for row in sorted(audit.records, key=lambda row: row["command_index"]):
        with (destination / "container-1-commands.jsonl").open("a") as handle:
            handle.write(json.dumps(row) + "\n")
    blobs = container / "command-blobs"
    blobs.mkdir()
    for digest, body in audit.blobs.items():
        (blobs / digest).write_bytes(body)
    for source, leaf in (
        ("/workspace/.setup_agent", ".setup_agent"),
        ("/tmp/sag_jobs", "sag_jobs"),
        (f"/workspace/{runtime.spec.name}", "fixture"),
    ):
        subprocess.run(
            ["docker", "cp", f"{audit.container_name}:{source}", str(container / leaf)],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    inspect = subprocess.check_output(["docker", "inspect", audit.container_name], text=True)
    (destination / "container-1-inspect.json").write_text(inspect)
    events = _verify_archived_evidence_epochs(destination)
    write(destination / "verified-control-events.json", events)
    checksums = {
        str(path.relative_to(destination)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(destination.rglob("*"))
        if path.is_file()
    }
    write(destination / "checksums.json", checksums)
    write(
        destination / "seal.json",
        {
            "status": "sealed",
            "schema_version": 1,
            "checksums_sha256": hashlib.sha256(
                (destination / "checksums.json").read_bytes()
            ).hexdigest(),
        },
    )


def publish_pin(audit, epoch, plan, fixture_sha: str, name: str) -> None:
    from sag.agent.control_events import RunPin, canonical_json
    from sag.agent.evidence_publications import (
        RUN_PIN_LOGICAL_ARTIFACT_ID,
        latest_publication_raw_sha256,
        publish_evidence_revision,
    )
    from sag.utils.container_io import write_container_text_atomic

    pin = RunPin(
        run_id=epoch.run_id,
        target_repo_sha=fixture_sha,
        container_image_digest=plan["image"]["id"],
        sag_git_sha=plan["candidate_sha"],
        thinking_model="none:no-model",
        action_model="none:no-model",
        sanitized_config={
            "campaign_kind": "d3r2-l1",
            "target_repository": f"fixture/{name}",
            "steps": plan["steps"][name],
        },
        prompt_bundle_sha256=hashlib.sha256(b"not-applicable:no-model").hexdigest(),
        feature_flags={},
        run_order_index=NAMES.index(name),
        random_seed_or_null=None,
        dependency_cache_state=plan["cache"],
        host_arch=platform.machine(),
        advisor={"mode": "off", "calls": []},
    )
    body = canonical_json(pin)
    if not write_container_text_atomic(
        audit, "/workspace/.setup_agent/run-pin.json", body, validate_json=True
    ).persisted:
        raise RuntimeError("L1 run pin did not persist")
    if not publish_evidence_revision(
        audit,
        record_kind="run_pin",
        record_id=RUN_PIN_LOGICAL_ARTIFACT_ID,
        logical_artifact_id=RUN_PIN_LOGICAL_ARTIFACT_ID,
        raw=body.encode(),
        expected_previous_raw_sha256=latest_publication_raw_sha256(
            audit, RUN_PIN_LOGICAL_ARTIFACT_ID
        ),
    ).published:
        raise RuntimeError("L1 run pin was not host published")


def run_fixture(out: Path, plan: dict, name: str) -> dict:
    from sag.agent.physical_validator import PhysicalValidator
    from sag.tools.base import bind_tool_result_output_storage
    from sag.tools.bash import BashTool
    from sag.tools.build.build_tool import BuildTool
    from sag.tools.internal.maven_tool import MavenTool
    from sag.tools.internal.project_analyzer import ProjectAnalyzerTool
    from sag.tools.internal.python_tool import PythonTool
    from sag.utils.container_io import write_container_text_atomic
    from scripts.run_d0_docker_probes import (
        NOT_APPLICABLE_SHA256,
        DockerProbeRuntime,
        ProbeSpec,
        _controller_action_scope,
        _strict_container_receipts,
    )

    require_source(plan)
    result_path = out / f"{name}-result.json"
    if result_path.exists() or (out / name).exists() or (out / ".scratch" / name).exists():
        raise RuntimeError(f"L1 fixture already attempted; preserve it without rerunning: {name}")
    pin = {**plan, "prompt_bundle_sha256": NOT_APPLICABLE_SHA256}
    runtime = DockerProbeRuntime(
        ROOT,
        out,
        out.name,
        plan["image"]["id"],
        ProbeSpec(name, NAMES.index(name), "fixed real L1 fixture"),
        pin,
        True,
    )
    observed = {"name": name, "passed": False, "steps": [], "failures": []}
    audit = None
    container_id = None
    try:
        audit = runtime.new_container()
        container_id = subprocess.check_output(
            ["docker", "inspect", "--format", "{{.Id}}", audit.container_name], text=True
        ).strip()
        epoch = runtime.epoch_for(audit)
        root = f"/workspace/{name}"
        runtime.register_container_evidence(audit, root, archive_name="fixture")
        packages = "python3 python3-venv git make"
        if name == "maven-parent-shade":
            packages += " maven openjdk-17-jdk-headless"
        bootstrap_started = time.monotonic()
        bootstrap = audit.execute_control_command(
            f"mkdir -p {shlex.quote(root)} && apt-get update && apt-get install -y --no-install-recommends {packages}",
            timeout=900,
        )
        observed["bootstrap"] = {
            "packages": packages,
            "exit_code": bootstrap.get("exit_code"),
            "duration_seconds": time.monotonic() - bootstrap_started,
            "cache_change": "fresh container installs fixed bootstrap package names from current apt repository; versions preserved in command archive",
        }
        if bootstrap.get("exit_code") != 0:
            raise RuntimeError("L1 bootstrap dependencies could not be installed")
        observed["bootstrap"]["versions"] = audit.execute_control_command(
            f"dpkg-query -W {packages}"
        )
        for relative, body in plan["fixture_files"][name].items():
            if not write_container_text_atomic(audit, f"{root}/{relative}", body).persisted:
                raise RuntimeError(f"fixture file did not persist: {relative}")
        command = f"cd {shlex.quote(root)} && git init -q && git add . && GIT_AUTHOR_DATE=2026-09-09T00:00:00Z GIT_COMMITTER_DATE=2026-09-09T00:00:00Z git -c user.name=SAG-L1 -c user.email=l1@fixture.invalid commit -qm 'Fixed L1 fixture' && git rev-parse HEAD"
        initialized = audit.execute_control_command(command)
        fixture_sha = str(initialized.get("output") or "").strip().splitlines()
        fixture_sha = fixture_sha[-1] if fixture_sha else ""
        if initialized.get("exit_code") != 0 or not re.fullmatch(r"[0-9a-f]{40}", fixture_sha):
            raise RuntimeError("fixture revision initialization failed")
        observed.update(fixture_sha=fixture_sha, run_id=epoch.run_id, container_id=container_id)
        publish_pin(audit, epoch, plan, fixture_sha, name)
        if name == "make-pytest-repair":
            missing_command = "/workspace/definitely-missing-python -m pytest"
            missing = audit.execute_command_with_monitoring(
                missing_command,
                workdir=root,
                optimize_for_maven=False,
                enable_cpu_monitoring=False,
                absolute_timeout=30,
                silent_timeout=30,
            )
            audit._record_result(
                index=audit._allocate_index(),
                kind="monitoring-negative-control",
                command=missing_command,
                result=missing,
            )
            observed["missing_executable_control"] = {
                "command": missing_command,
                "result": missing,
                "scope": "process exit observation only; no test execution or receipt claim",
            }
            if missing.get("exit_code") != 127 or missing.get("success") is not False:
                raise RuntimeError("missing executable monitoring did not preserve exit 127")
            setup_params = {
                "command": "python3 -m venv .venv && .venv/bin/python -m pip install pytest",
                "working_directory": root,
                "timeout": 180,
            }
            with _controller_action_scope(
                runtime,
                audit=audit,
                label=f"{name}-venv",
                domain_id=name,
                tool="bash",
                params=setup_params,
                next_action_kind="bash",
            ):
                setup_result = BashTool(audit).execute(**setup_params)
            observed["environment_bootstrap"] = {
                "params": setup_params,
                "result": setup_result.model_dump(mode="json"),
            }
            if not setup_result.succeeded:
                raise RuntimeError("Python venv prerequisite could not be prepared")
        with (
            runtime.evidence_epoch(audit),
            bind_tool_result_output_storage(
                runtime.output_storage_for(audit),
                task_id=f"survey-{name}",
                tool_name="project_analyzer",
            ),
        ):
            survey = ProjectAnalyzerTool(audit).execute(project_path=root, update_context=False)
        observed["survey"] = survey.model_dump(mode="json")
        if not survey.succeeded:
            raise RuntimeError("fixture project survey failed")
        build = BuildTool(audit, maven_tool=MavenTool(audit), python_tool=PythonTool(audit))
        predecessor = None
        for index, step in enumerate(plan["steps"][name]):
            require_source(plan)
            params = step["params"]
            tool = step["tool"]
            with _controller_action_scope(
                runtime,
                audit=audit,
                label=f"{name}-{index}",
                domain_id=name,
                tool=tool,
                params=params,
                next_action_kind=params.get("action", "bash"),
                predecessor_contract_id=(
                    predecessor if name == "make-pytest-repair" and index == 2 else None
                ),
            ):
                result = (build if tool == "build" else BashTool(audit)).execute(**params)
            receipts = _strict_container_receipts(audit)
            observed["steps"].append(
                {
                    "index": index,
                    "planned": step,
                    "result": result.model_dump(mode="json"),
                    "receipts": receipts,
                }
            )
            write(out / f"{name}-progress.json", observed)
            if name == "make-pytest-repair" and index == 0:
                test_receipts = [
                    r for r in receipts if r.get("tool") == "python" and r.get("exit_code") == 1
                ]
                if len(test_receipts) != 1:
                    raise RuntimeError(
                        "negative control did not produce exactly one red Python receipt"
                    )
                predecessor = test_receipts[0]["contract_id"]
            elif not result.succeeded:
                raise RuntimeError(f"planned step {index} failed: {result.error_code}")
        validator = PhysicalValidator(audit, project_path="/workspace", receipt_run_id=epoch.run_id)
        observed["physical"] = {
            "build": validator.validate_build_status(name),
            "test": validator.validate_test_status(name),
        }
        observed["receipts"] = _strict_container_receipts(audit)
        reports = report_facts(audit, observed["receipts"])
        observed["report_facts"] = reports
        expected = [(1, 0)] if name == "maven-parent-shade" else [(1, 1), (1, 0)]
        if [(row["executed"], row["red"]) for row in reports] != expected:
            raise RuntimeError(
                "receipt-bound XML did not match the fixed per-attempt case outcomes"
            )
        physical_test = observed["physical"]["test"]
        if (
            not physical_test.get("has_test_reports")
            or physical_test.get("test_execution_state") != "completed"
        ):
            raise RuntimeError(
                "production physical validator did not verify completed test execution"
            )
        if name == "make-pytest-repair":
            from sag.runtime.container_io import read_container_text

            first, second = reports
            contract_text = read_container_text(
                audit,
                f"/workspace/.setup_agent/invocation_contracts/{second['contract_id']}.json",
                exact_bytes=True,
            )
            contract = json.loads(contract_text or "null")
            if not contract or contract.get("predecessor_contract_id") != first["contract_id"]:
                raise RuntimeError("Python repair contract is not linked to the failed attempt")

            def test_argv(row):
                argv = row["argv"]
                tokens = shlex.split(argv) if isinstance(argv, str) else argv
                return [token for token in tokens if not token.startswith("--junitxml=")]

            if first["cwd"] != second["cwd"] or test_argv(first) != test_argv(second):
                raise RuntimeError("Python retry changed its command, cwd or selected test scope")
            if first["reports"][0]["sha256"] == second["reports"][0]["sha256"]:
                raise RuntimeError("Python failed/repaired XML unexpectedly shares one hash")
            observed["repair"] = {
                "predecessor_contract_id": first["contract_id"],
                "same_command_cwd_test_scope": True,
                "environment_change": "make prepare created .ready; fixture source bytes unchanged",
            }
        if name == "maven-parent-shade":
            inspect = "import json,zipfile; p='producer/target/l1-producer-1.0.jar'; z=zipfile.ZipFile(p); print(json.dumps({'jar':p,'relocated_dependency': 'org/sag/fixture/vendor/StringUtils.class' in z.namelist(),'own_class':'org/sag/fixture/Greeting.class' in z.namelist()}))"
            artifact = audit.execute_control_command(
                f"cd {shlex.quote(root)} && python3 -c {shlex.quote(inspect)}"
            )
            observed["shade_artifact"] = artifact
            data = json.loads(str(artifact.get("output") or ""))
            if (
                artifact.get("exit_code") != 0
                or not data["relocated_dependency"]
                or not data["own_class"]
            ):
                raise RuntimeError(
                    "shade artifact is missing its own or relocated dependency class"
                )
        require_source(plan)
        observed["passed"] = True
    except BaseException as exc:
        observed["failures"].append(f"{type(exc).__name__}: {exc}")
    finally:
        try:
            if audit is not None:
                archive_fixture(runtime, audit, observed)
                observed["archive_integrity"] = "sealed"
        except BaseException as exc:
            observed["passed"] = False
            observed["archive_integrity"] = "failed"
            observed["failures"].append(f"archive {type(exc).__name__}: {exc}")
        finally:
            try:
                if container_id:
                    stopped = subprocess.run(
                        ["docker", "stop", "--time", "5", container_id],
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                    )
                    observed["stop_exit_code"] = stopped.returncode
            except BaseException as exc:
                observed["passed"] = False
                observed["failures"].append(f"cleanup {type(exc).__name__}: {exc}")
            finally:
                write(result_path, observed)
    return observed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--run", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--expected-sha")
    parser.add_argument("--image-id")
    parser.add_argument(
        "--allow-dirty-source",
        action="store_true",
        help="L1 local boundary check only; freeze all source hashes and disclose dirty worktree",
    )
    parser.add_argument("--only", choices=NAMES, action="append")
    args = parser.parse_args()
    out = args.out.resolve()
    if out.is_relative_to((ROOT / "logs/d3r1-20260907").resolve()):
        raise ValueError("sealed D3R1 evidence is read-only")
    if args.prepare:
        prepare(
            out,
            expected_sha=args.expected_sha or "",
            image_id=args.image_id or "",
            allow_dirty_source=args.allow_dirty_source,
        )
        return 0
    plan = json.loads((out / "protocol.json").read_text())
    if plan["fixture_files"] != fixtures() or plan["steps"] != {
        name: steps(name) for name in NAMES
    }:
        raise RuntimeError("L1 fixture protocol changed after prepare")
    results = [run_fixture(out, plan, name) for name in NAMES if not args.only or name in args.only]
    print(
        json.dumps(
            [
                {key: r.get(key) for key in ("name", "passed", "failures", "archive_integrity")}
                for r in results
            ]
        ),
        flush=True,
    )
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
