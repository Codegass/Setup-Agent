#!/usr/bin/env python3
"""Prepare, then run frozen D3R2 CLI comparisons in independent containers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import shutil
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASELINE_SHA = "79ae52aa2da7692eeaf63ab0b7b0a39c33ee20d4"
REFERENCE = ROOT / "logs/d3r1-20260907/manifest.json"
SMALL = (
    ("commons-cli", "apache/commons-cli", "e17111798da51037659b3594d9c0b3b525040081"),
    ("gson", "google/gson", "b3f4ca20087f9066de4c340522ff84e0558e1ad1"),
)
LOCK = threading.Lock()
STOP = threading.Event()
MAX_SOURCE_PATCH_BYTES = 16 * 1024 * 1024
ENV_ALIASES = {
    "thinking_max_tokens": "SAG_MAX_THINKING_TOKENS",
    "action_max_tokens": "SAG_MAX_ACTION_TOKENS",
    "azure_api_version": "AZURE_API_VERSION",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def command(argv: list[str], *, cwd: Path | None = None, timeout: int = 60) -> str:
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{argv[:3]} failed ({result.returncode}): {result.stderr[-800:]}")
    return result.stdout.strip()


def inspect_container(name: str) -> dict | None:
    result = subprocess.run(["docker", "inspect", name], capture_output=True, text=True, timeout=30)
    if result.returncode:
        # A daemon error is not evidence that the name is available.
        detail = result.stderr.casefold()
        if "no such object" in detail or "no such container" in detail:
            return None
        raise RuntimeError(f"Container inventory unavailable: {result.stderr[-400:]}")
    return json.loads(result.stdout)[0]


def safe_output(path: Path) -> Path:
    path = path.resolve()
    for protected in (ROOT / "logs/d3r1-20260907", ROOT / "logs/d3-freeze-20260830"):
        if path == protected or path.is_relative_to(protected):
            raise ValueError("Campaign output cannot be inside the sealed archive")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,60}", path.name):
        raise ValueError("Output directory name must be a short lowercase campaign id")
    return path


def verify_source(path: Path, sha: str, expected_files: dict | None = None) -> dict[str, str]:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("A full source commit SHA is required")
    if command(["git", "rev-parse", "HEAD"], cwd=path) != sha:
        raise RuntimeError(f"Source HEAD drift at {path}")
    if command(["git", "status", "--porcelain"], cwd=path):
        raise RuntimeError(f"Source checkout is dirty: {path}")
    tracked = command(["git", "ls-files", "-z"], cwd=path).split("\0")
    files = {name: digest(path / name) for name in tracked if name and (path / name).is_file()}
    if expected_files is not None and files != expected_files:
        raise RuntimeError(f"Source bytes drift at {path}")
    return files


def target_path(directory: Path, seat: str) -> Path:
    options = [directory / f"{seat}.json", directory / seat / "target_record.json"]
    found = [path.resolve() for path in options if path.is_file()]
    if len(found) != 1:
        raise ValueError(f"Expected one target file for {seat}, found {len(found)}")
    return found[0]


def selected_projects(phase: str, reference: dict, targets: Path) -> list[dict]:
    if phase == "23":
        projects = [
            dict(project, variant="candidate", run_key=project["seat"])
            for project in reference["projects"]
        ]
        if len(projects) != 23 or len({p["seat"] for p in projects}) != 23:
            raise ValueError("The reference must name exactly 23 unique projects")
    elif phase == "paired":
        projects = []
        for index, (seat, repo, sha) in enumerate(SMALL):
            # Alternate which version starts first across the two subjects.
            for variant in (("baseline", "candidate") if index == 0 else ("candidate", "baseline")):
                projects.append(
                    dict(
                        seat=seat,
                        repo=repo,
                        sha=sha,
                        variant=variant,
                        run_key=f"{seat}-{variant}",
                        heavy=False,
                    )
                )
    else:
        projects = [
            dict(
                seat="httpcomponents-client-jenkins17-225",
                repo="apache/httpcomponents-client",
                sha="be07c77297576b08bc01bb93789eca6f5f9bf850",
                variant="candidate",
                run_key="httpcomponents-client-jenkins17-225",
                heavy=False,
                goal="At the pinned revision, use JDK 17 from the repository root and complete mvn -B -f pom.xml clean verify install -P-use-toolchains,nodoclint. Preserve the full reactor and test scope. Record any unavailable prerequisite or failure; do not reduce the task scope.",
            )
        ]
    for project in projects:
        path = target_path(targets, project["seat"])
        target = json.loads(path.read_text())
        if (target["repo"], target["sha"]) != (project["repo"], project["sha"]):
            raise ValueError(f"Target subject mismatch for {project['seat']}")
        if phase == "23" and target["matched_cell"] != project["matched_cell"]:
            raise ValueError(f"Frozen target cell changed for {project['seat']}")
        project.update(
            target_source=str(path), target_sha256=digest(path), matched_cell=target["matched_cell"]
        )
    return projects


def config_environment(config: dict) -> dict[str, str]:
    # The reference is already sanitized. Do not persist credentials or endpoints.
    result = {}
    for key, value in config.items():
        if isinstance(value, (str, int, float, bool)):
            result[ENV_ALIASES.get(key, "SAG_" + key.upper())] = (
                str(value).lower() if isinstance(value, bool) else str(value)
            )
    return result


def prepare(args: argparse.Namespace) -> dict:
    out = safe_output(args.out)
    if out.exists():
        raise FileExistsError(f"Refusing existing output directory {out}")
    source = args.source.resolve()
    baseline = args.baseline_source.resolve() if args.baseline_source else None
    reference_path = args.reference_manifest.resolve()
    reference = json.loads(reference_path.read_text())
    if reference["sag_commit"] != BASELINE_SHA:
        raise ValueError("Reference manifest is not the frozen D3R1 baseline")
    sources = {
        "candidate": {
            "path": str(source),
            "sha": args.expected_sha,
            "files": verify_source(source, args.expected_sha),
        }
    }
    if args.phase == "paired":
        if baseline is None or args.baseline_sha != BASELINE_SHA:
            raise ValueError("Paired runs require the exact 79ae52a baseline checkout")
        sources["baseline"] = {
            "path": str(baseline),
            "sha": BASELINE_SHA,
            "files": verify_source(baseline, BASELINE_SHA),
        }
    projects = selected_projects(args.phase, reference, args.targets_dir.resolve())
    image = reference["docker_image_id"]
    image_info = json.loads(
        command(["docker", "image", "inspect", "--format", "{{json .}}", image])
    )
    if image_info["Id"] != image:
        raise RuntimeError("Docker image digest drift")
    resources = json.loads(command(["docker", "info", "--format", "{{json .}}"]))
    resources = {
        key: resources.get(key)
        for key in ("NCPU", "MemTotal", "Architecture", "OperatingSystem", "ServerVersion")
    }
    resource_changes = {
        key: {"before": reference["docker_resources"].get(key), "now": value}
        for key, value in resources.items()
        if reference["docker_resources"].get(key) != value
    }
    if any(key in resource_changes for key in ("NCPU", "MemTotal", "Architecture")):
        raise RuntimeError(f"Compute resources differ from frozen baseline: {resource_changes}")
    config = dict(reference["config"])
    config.update(
        docker_base_image=image,
        max_iterations=75 if args.phase == "paired" else 150,
        max_wall_clock_seconds=1200 if args.phase == "paired" else 7200,
    )
    for index, project in enumerate(projects):
        project.update(order=index, container=f"sag-{out.name}-{project['run_key']}")
        if inspect_container(project["container"]) is not None:
            raise RuntimeError(f"Refusing existing container {project['container']}")
    manifest = {
        "campaign": out.name,
        "created_at": now(),
        "phase": args.phase,
        "reference_manifest": str(reference_path),
        "reference_sha256": digest(reference_path),
        "sources": sources,
        "config": config,
        "environment": config_environment(config),
        "docker_image_id": image,
        "docker_resources": resources,
        "resource_changes": resource_changes,
        "image": {
            key: image_info.get(key)
            for key in ("Id", "Architecture", "Os", "Created", "RepoDigests")
        },
        "cache_policy": "Every attempt starts a new container from the same image; no host project/dependency cache mounts; baseline and candidate never share a container",
        "jdk_policy": "Use repository/CI constraints; actual selected JDK is measured by production receipts; independent Jenkins control requests JDK17",
        "schedule": "paired subjects sequential with alternating version order; full cohort light at most two, heavy exclusively serial",
        "evaluation": {
            "local_completion": "original task scope and execution completion",
            "ci": "missing target/scope/authority unscored; no post-run cell substitution",
            "baseline": "no --ci-target-file; no production CI comparison result",
            "runs_per_subject_variant": 1,
            "efficiency_claim": "exploratory pair only, not a stable general speedup estimate",
        },
        "runner_path": str(Path(__file__).resolve()),
        "runner_sha256": digest(Path(__file__)),
        "python_environment": str(args.python_environment.resolve()),
        "projects": projects,
    }
    out.mkdir(parents=True)
    for project in projects:
        dest = out / "targets" / f"{project['run_key']}.json"
        dest.parent.mkdir(exist_ok=True)
        shutil.copyfile(project["target_source"], dest)
        project["target_file"] = str(dest)
    for variant, identity in sources.items():
        command(
            [
                "git",
                "archive",
                "--format=tar.gz",
                f"--output={out / (variant + '-source.tar.gz')}",
                identity["sha"],
            ],
            cwd=Path(identity["path"]),
            timeout=120,
        )
    save(out / "manifest.json", manifest)
    (out / "manifest.sha256").write_text(digest(out / "manifest.json") + "\n")
    (out / "protocol.md").write_text(
        f"# {out.name}\n\nPrepared {manifest['created_at']}; phase {args.phase}.\n\n"
        "The manifest fixes both source revisions, each target byte digest and cell, base image, model parameters, budgets, resources and ordering before execution. All failed, partial, timeout and unavailable outcomes are retained. Existing result files are returned without rerunning. An incomplete attempt directory is a review boundary. Only newly observed containers whose project label, creation time and image match this attempt may be stopped.\n\n"
        f"Cache: {manifest['cache_policy']}.\n\nJDK: {manifest['jdk_policy']}.\n\n"
        "The baseline cannot publish the new production CI comparison; this is recorded explicitly. Separate corrected historical labels from newly completed tasks. The independent Jenkins control is a changed task and is excluded from original 23-project progress. No efficiency claim is justified by one pair.\n"
    )
    return manifest


def load_manifest(out: Path) -> dict:
    if digest(out / "manifest.json") != (out / "manifest.sha256").read_text().strip():
        raise RuntimeError("Prepared manifest bytes changed")
    manifest = json.loads((out / "manifest.json").read_text())
    if manifest["runner_sha256"] != digest(Path(__file__)):
        raise RuntimeError("Runner code changed after preparation")
    if digest(Path(manifest["reference_manifest"])) != manifest["reference_sha256"]:
        raise RuntimeError("Frozen reference manifest changed")
    return manifest


def event(out: Path, kind: str, **details: Any) -> None:
    row = {"timestamp": now(), "event": kind, **details}
    with LOCK:
        with (out / "progress.jsonl").open("a") as handle:
            handle.write(json.dumps(row) + "\n")
    print(json.dumps(row), flush=True)


def runtime_environment(manifest: dict, project: dict) -> dict[str, str]:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")  # Private provider credentials stay in process memory.
    identity = manifest["sources"][project["variant"]]
    source = Path(identity["path"])
    return (
        dict(os.environ)
        | manifest["environment"]
        | {
            "PYTHONPATH": os.pathsep.join([str(source / "src"), str(source)]),
            "UV_PROJECT_ENVIRONMENT": manifest["python_environment"],
            "UV_CACHE_DIR": "/private/tmp/sag-ci-scope-uv-cache",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "PYTHONUNBUFFERED": "1",
            "SAG_GIT_SHA": identity["sha"],
            "SAG_RUN_ORDER_INDEX": str(project["order"]),
            "SAG_DEPENDENCY_CACHE_STATE": manifest["cache_policy"],
        }
    )


def cli_command(manifest: dict, project: dict) -> list[str]:
    source = manifest["sources"][project["variant"]]["path"]
    argv = [
        shutil.which("uv") or "uv",
        "run",
        "--project",
        source,
        "--offline",
        "--no-sync",
        "python",
        "-m",
        "sag.main",
        "project",
        f"https://github.com/{project['repo']}.git",
        "--ref",
        project["sha"],
        "--name",
        project["container"].removeprefix("sag-"),
        "--record",
    ]
    if project.get("goal"):
        argv.extend(["--goal", project["goal"]])
    if project["variant"] == "candidate":
        argv.extend(["--ci-target-file", project["target_file"]])
    return argv


def effective_config(manifest: dict, project: dict, environment: dict, directory: Path) -> dict:
    source = manifest["sources"][project["variant"]]["path"]
    probe = subprocess.run(
        [
            shutil.which("uv") or "uv",
            "run",
            "--project",
            source,
            "--offline",
            "--no-sync",
            "python",
            "-c",
            "import json; from sag.config.settings import Config; from sag.agent.control_events import sanitize_config; print(json.dumps(sanitize_config(Config.from_env())))",
        ],
        cwd=directory,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if probe.returncode:
        raise RuntimeError(f"Could not verify effective configuration: {probe.stderr[-500:]}")
    config = json.loads(probe.stdout)
    changed = [key for key, value in manifest["config"].items() if config.get(key) != value]
    if changed:
        raise RuntimeError(f"Effective configuration differs from prepared values: {changed}")
    return config


def owned_container(data: dict, project: dict, started_at: str, image: str) -> bool:
    return (
        data.get("Image") == image
        and data.get("Config", {}).get("Labels", {}).get("setup-agent.project")
        == project["container"].removeprefix("sag-")
        and datetime.fromisoformat(data["Created"].replace("Z", "+00:00"))
        >= datetime.fromisoformat(started_at)
    )


def archive_container(project: dict, directory: Path, identity: str) -> dict:
    data = inspect_container(identity)
    if data is None or data.get("Id") != identity:
        raise RuntimeError("Owned container identity vanished")
    save(
        directory / "container.json",
        {
            key: data.get(key)
            for key in ("Id", "Image", "Created", "State", "Name", "Platform", "Mounts")
        },
    )
    diagnostics: dict[str, Any] = {"host_project_cache_mounted": bool(data.get("Mounts"))}
    evidence = directory / "container-evidence"
    evidence.mkdir(exist_ok=True)
    try:
        copied = subprocess.run(
            ["docker", "cp", f"{identity}:/workspace/.setup_agent", str(evidence)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        diagnostics["evidence_copy"] = {"exit_code": copied.returncode, "error": copied.stderr}
        if data["State"].get("Running"):
            project_path = "/workspace/" + project["repo"].split("/")[-1]
            for label, args in {
                "head": ["rev-parse", "HEAD"],
                "source_status": ["status", "--porcelain"],
                "source_diff": ["diff", "--stat"],
            }.items():
                probe = subprocess.run(
                    ["docker", "exec", identity, "git", "-C", project_path, *args],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                diagnostics[label] = {
                    "exit_code": probe.returncode,
                    "output": probe.stdout,
                    "error": probe.stderr,
                }
            patch_path = directory / "source.patch"
            patch = {
                "path": patch_path.name,
                "scope": "tracked changes against HEAD; untracked paths remain in source_status",
                "exit_code": None,
                "max_retained_bytes": MAX_SOURCE_PATCH_BYTES,
            }
            try:
                # Stream directly to a separate file, preserving binary patches
                # and legitimate empty output without a text/display projection.
                with patch_path.open("wb") as stream:
                    diff = subprocess.run(
                        [
                            "docker",
                            "exec",
                            identity,
                            "git",
                            "-C",
                            project_path,
                            "diff",
                            "HEAD",
                            "--binary",
                            "--no-ext-diff",
                            "--no-textconv",
                        ],
                        stdout=stream,
                        stderr=subprocess.PIPE,
                        timeout=60,
                    )
                patch.update(exit_code=diff.returncode, error=diff.stderr.decode(errors="replace"))
            except (OSError, subprocess.TimeoutExpired) as exc:
                patch["error"] = f"{type(exc).__name__}: {exc}"
                patch["timeout"] = isinstance(exc, subprocess.TimeoutExpired)
            finally:
                if patch_path.is_file():
                    observed_size = patch_path.stat().st_size
                    truncated = observed_size > MAX_SOURCE_PATCH_BYTES
                    if truncated:
                        with patch_path.open("r+b") as stream:
                            stream.truncate(MAX_SOURCE_PATCH_BYTES)
                    patch.update(
                        collected_bytes=observed_size,
                        retained_bytes=patch_path.stat().st_size,
                        sha256=digest(patch_path),
                        truncated=truncated,
                        complete=patch["exit_code"] == 0 and not truncated,
                    )
                else:
                    patch.update(retained_bytes=0, sha256=None, truncated=False, complete=False)
                diagnostics["source_patch"] = patch
            paths = set()
            # Keep diagnostic reports as well as claimed ones. Attribution needs
            # to distinguish a missing receipt from an actually absent report.
            discovery = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-w",
                    "/workspace",
                    identity,
                    "find",
                    ".",
                    "-type",
                    "f",
                    "(",
                    "-name",
                    "TEST-*.xml",
                    "-o",
                    "-name",
                    "testng-results.xml",
                    "-o",
                    "-name",
                    "failsafe-summary.xml",
                    "-o",
                    "-path",
                    "*/.setup_agent/pytest-reports/*.xml",
                    ")",
                    "-print0",
                ],
                capture_output=True,
                timeout=120,
            )
            diagnostics["report_discovery"] = {
                "exit_code": discovery.returncode,
                "error": discovery.stderr.decode(errors="replace"),
            }
            if discovery.returncode == 0:
                for raw_path in discovery.stdout.split(b"\0"):
                    if raw_path:
                        paths.add(raw_path.decode().removeprefix("./"))
            diagnostics["diagnostic_report_paths"] = len(paths)
            for receipt in evidence.glob("**/invocation_receipts/*.json"):
                try:
                    payload = json.loads(receipt.read_text())
                    for rows in payload.get("report_delta", {}).values():
                        for row in rows:
                            path = row.get("path", "")
                            if (
                                isinstance(path, str)
                                and "\0" not in path
                                and path.startswith("/workspace/")
                                and posixpath.normpath(path) == path
                            ):
                                paths.add(path.removeprefix("/workspace/"))
                except (ValueError, TypeError, AttributeError) as exc:
                    diagnostics.setdefault("receipt_read_errors", []).append(
                        f"{receipt.name}: {exc}"
                    )
            save(directory / "raw-report-paths.json", sorted(paths))
            with (directory / "physical-test-reports.tar.gz").open("wb") as stream:
                capture = subprocess.run(
                    [
                        "docker",
                        "exec",
                        "-i",
                        "-w",
                        "/workspace",
                        identity,
                        "tar",
                        "--null",
                        "--verbatim-files-from",
                        "-T",
                        "-",
                        "-czf",
                        "-",
                    ],
                    input=b"".join(path.encode() + b"\0" for path in sorted(paths)),
                    stdout=stream,
                    stderr=subprocess.PIPE,
                    timeout=300,
                )
            diagnostics["raw_reports"] = {
                "claimed_paths": len(paths),
                "exit_code": capture.returncode,
                "error": capture.stderr.decode(errors="replace"),
            }
    finally:
        if data["State"].get("Running"):
            stopped = subprocess.run(
                ["docker", "stop", "--time", "20", identity],
                capture_output=True,
                text=True,
                timeout=60,
            )
            diagnostics["stop"] = {"exit_code": stopped.returncode, "error": stopped.stderr}
        save(directory / "diagnostics.json", diagnostics)
    return diagnostics


def stop_process(process: subprocess.Popen) -> None:
    for sig, seconds in ((signal.SIGINT, 60), (signal.SIGTERM, 20), (signal.SIGKILL, 20)):
        if process.poll() is not None:
            break
        os.killpg(process.pid, sig)
        try:
            process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            continue


def run_one(out: Path, manifest: dict, project: dict) -> dict:
    directory = out / "runs" / project["run_key"]
    result_path = directory / "result.json"
    if result_path.exists():
        previous = json.loads(result_path.read_text())
        expected = {
            "run_key": project["run_key"],
            "repo": project["repo"],
            "target_sha": project["sha"],
            "variant": project["variant"],
            "sag_sha": manifest["sources"][project["variant"]]["sha"],
            "target_record_file_sha256": project["target_sha256"],
        }
        if any(previous.get(key) != value for key, value in expected.items()):
            raise RuntimeError("Existing result does not belong to this prepared attempt")
        return previous
    if directory.exists():
        raise RuntimeError(f"Incomplete prior attempt at {directory}; review before proceeding")
    if STOP.is_set():
        raise RuntimeError("Campaign interrupted before this attempt started")
    if inspect_container(project["container"]) is not None:
        raise RuntimeError(f"Refusing preexisting container {project['container']}")
    identity = manifest["sources"][project["variant"]]
    source = Path(identity["path"])
    verify_source(source, identity["sha"], identity["files"])
    if digest(Path(project["target_file"])) != project["target_sha256"]:
        raise RuntimeError("Prepared target bytes changed")
    environment = runtime_environment(manifest, project)
    directory.mkdir(parents=True)
    started = time.monotonic()
    result = {
        "run_key": project["run_key"],
        "seat": project["seat"],
        "variant": project["variant"],
        "repo": project["repo"],
        "target_sha": project["sha"],
        "sag_sha": identity["sha"],
        "started_at": now(),
        "command": cli_command(manifest, project),
        "target_record_file_sha256": project["target_sha256"],
        "production_ci_result_supported": project["variant"] == "candidate",
        "authority_ok": False,
    }
    save(directory / "started.json", result)
    event(out, "started", run_key=project["run_key"], container=project["container"])
    container_id = None
    process = None
    try:
        save(
            directory / "effective-config.json",
            effective_config(manifest, project, environment, directory),
        )
        with (directory / "console.log").open("wb") as log:
            process = subprocess.Popen(
                result["command"],
                cwd=directory,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            save(
                directory / "process.json", {"pid": process.pid, "started_at": result["started_at"]}
            )
            while process.poll() is None:
                if container_id is None:
                    data = inspect_container(project["container"])
                    if data is not None:
                        if not owned_container(
                            data, project, result["started_at"], manifest["docker_image_id"]
                        ):
                            raise RuntimeError("Container ownership could not be proven")
                        container_id = data["Id"]
                        save(
                            directory / "container-ownership.json",
                            {"Id": container_id, "Created": data["Created"], "observed_at": now()},
                        )
                if (
                    STOP.is_set()
                    or time.monotonic() - started
                    > manifest["config"]["max_wall_clock_seconds"] + 600
                ):
                    result["outer_timeout"] = not STOP.is_set()
                    result["cancelled"] = STOP.is_set()
                    stop_process(process)
                    break
                time.sleep(2)
            result["exit_code"] = process.returncode
        verify_source(source, identity["sha"], identity["files"])
        sessions = sorted((directory / "logs").glob("session_*"))
        result["sessions"] = [str(path.relative_to(out)) for path in sessions]
        if len(sessions) == 1:
            collect = subprocess.run(
                [
                    shutil.which("uv") or "uv",
                    "run",
                    "--project",
                    str(source),
                    "--offline",
                    "--no-sync",
                    "python",
                    "-c",
                    "import sys; from pathlib import Path; from scripts.collect_control_layer_ab import ABCollector; print(ABCollector().collect(Path(sys.argv[1])).model_dump_json())",
                    str(sessions[0]),
                ],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=120,
            )
            (directory / "collection.stderr.log").write_text(collect.stderr)
            if collect.returncode == 0:
                payload = json.loads(collect.stdout)
                save(directory / "collected.json", payload)
                # Host authorization proves this collected run, but not that it
                # belongs to the subject and implementation prepared here.
                pin = payload.get("pin")
                expected_pin = {
                    "target_repo_sha": project["sha"],
                    "sag_git_sha": identity["sha"],
                    "container_image_digest": manifest["docker_image_id"],
                }
                mismatches = [
                    key
                    for key, value in expected_pin.items()
                    if not isinstance(pin, dict) or pin.get(key) != value
                ]
                if mismatches:
                    raise RuntimeError(
                        "Collected run pin differs from prepared values: " + ", ".join(mismatches)
                    )
                result.update(
                    authority_ok=True,
                    verdict=payload["metrics"]["verdict"],
                    run_id=payload["run_id"],
                )
            else:
                result["collection_error"] = (
                    f"Collector exit {collect.returncode}; see collection.stderr.log"
                )
        else:
            result["collection_error"] = f"Expected one session, observed {len(sessions)}"
    except Exception as exc:
        result.update(runner_error=f"{type(exc).__name__}: {exc}", authority_ok=False)
        if process is not None:
            stop_process(process)
            result["exit_code"] = process.returncode
    finally:
        try:
            if container_id is None:
                data = inspect_container(project["container"])
                if data is not None and owned_container(
                    data, project, result["started_at"], manifest["docker_image_id"]
                ):
                    container_id = data["Id"]
            if container_id:
                diagnostics = archive_container(project, directory, container_id)
                result["container_id"] = container_id
                result["evidence_archive_complete"] = (
                    diagnostics.get("evidence_copy", {}).get("exit_code") == 0
                    and diagnostics.get("raw_reports", {}).get("exit_code") == 0
                    and diagnostics.get("report_discovery", {}).get("exit_code") == 0
                    and not diagnostics.get("receipt_read_errors")
                    and diagnostics.get("stop", {}).get("exit_code") == 0
                )
                if diagnostics.get("host_project_cache_mounted"):
                    result.update(authority_ok=False, cache_protocol_violation=True)
        except Exception as exc:
            result["archive_error"] = f"{type(exc).__name__}: {exc}"
            result["evidence_archive_complete"] = False
        result.update(finished_at=now(), seconds=round(time.monotonic() - started, 2))
        save(result_path, result)
        event(
            out,
            "finished",
            run_key=project["run_key"],
            verdict=result.get("verdict"),
            exit_code=result.get("exit_code"),
            authority_ok=result["authority_ok"],
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare", action="store_true")
    action.add_argument("--run", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--expected-sha")
    parser.add_argument("--baseline-source", type=Path)
    parser.add_argument("--baseline-sha", default=BASELINE_SHA)
    parser.add_argument("--targets-dir", type=Path)
    parser.add_argument("--phase", choices=("paired", "23", "official-control"), default="paired")
    parser.add_argument("--reference-manifest", type=Path, default=REFERENCE)
    parser.add_argument("--python-environment", type=Path, default=ROOT / ".venv")
    parser.add_argument("--only", nargs="+")
    args = parser.parse_args()
    if args.prepare:
        if not all((args.source, args.expected_sha, args.targets_dir)):
            parser.error("--prepare requires --source, --expected-sha and --targets-dir")
        manifest = prepare(args)
        print(
            json.dumps({"prepared": str(args.out.resolve()), "attempts": len(manifest["projects"])})
        )
        return
    out = safe_output(args.out)
    manifest = load_manifest(out)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: STOP.set())
    chosen = set(args.only or [project["run_key"] for project in manifest["projects"]])
    if not chosen <= {project["run_key"] for project in manifest["projects"]}:
        parser.error("--only must name prepared run keys")
    projects = [project for project in manifest["projects"] if project["run_key"] in chosen]
    if manifest["phase"] != "23":
        for project in projects:
            run_one(out, manifest, project)
    else:
        # The first seat is an instrumentation canary and remains in all results.
        canary = projects[0] if projects else None
        if canary:
            result = run_one(out, manifest, canary)
            if not (result["authority_ok"] and result.get("evidence_archive_complete")):
                raise RuntimeError(
                    "Canary lacks authorized close or complete archive; retain outcome and inspect before scheduling"
                )
        light = [project for project in projects if not project["heavy"] and project != canary]
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda project: run_one(out, manifest, project), light))
        for project in projects:
            if project["heavy"] and project != canary:
                run_one(out, manifest, project)
    event(out, "selection_complete", run_keys=[project["run_key"] for project in projects])


if __name__ == "__main__":
    main()
