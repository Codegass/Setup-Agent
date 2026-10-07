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
REQUIREMENTS_PROTOCOL = "requirements-v2"
ENV_ALIASES = {
    "thinking_max_tokens": "SAG_MAX_THINKING_TOKENS",
    "action_max_tokens": "SAG_MAX_ACTION_TOKENS",
    "azure_api_version": "AZURE_API_VERSION",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def requirements_protocol(manifest: dict) -> bool:
    """Old manifests retain their original, explicitly narrower protocol."""
    protocol = manifest.get("protocol")
    if protocol not in (None, "legacy", REQUIREMENTS_PROTOCOL):
        raise ValueError(f"Unsupported campaign protocol: {protocol}")
    return protocol == REQUIREMENTS_PROTOCOL


def requirements_preflight(manifest: dict, project: dict) -> dict | None:
    """Freeze the task, CI target and sidecar before inspecting any container."""
    if not requirements_protocol(manifest):
        return None
    from sag.agent.acceptance_task import load_acceptance_task
    from sag.agent.ci_comparison import load_ci_target
    from sag.benchmark.requirements import (
        POLICY_VERSION, canonical_digest, load_requirements,
        requirements_evidence_root, validate_ci_count_metadata,
    )

    if not re.fullmatch(r"[0-9a-f]{40}", str(project.get("sha", ""))):
        raise ValueError("Formal campaign requires a full frozen project ref")
    if project.get("acceptance_command"):
        raise ValueError("Formal campaign uses the ordered task, not acceptance_command")
    for file_key, hash_key in (
        ("acceptance_task_file", "acceptance_task_sha256"),
        ("target_file", "target_sha256"),
        ("requirements_file", "requirements_file_sha256"),
    ):
        if not project.get(file_key) or not re.fullmatch(
            r"[0-9a-f]{64}", str(project.get(hash_key, ""))
        ):
            raise ValueError(f"Formal campaign requires {file_key} and {hash_key}")
        if digest(Path(project[file_key])) != project[hash_key]:
            raise ValueError(f"Prepared {file_key} bytes changed")
    task = load_acceptance_task(project["acceptance_task_file"])
    target = load_ci_target(project["target_file"])
    if (task.repo, task.sha) != (project.get("repo"), project["sha"]):
        raise ValueError("Formal acceptance task subject differs")
    if (target.record.repo, target.record.sha) != (task.repo, task.sha):
        raise ValueError("Formal CI target subject differs")
    if "matched_cell" in project and target.record.matched_cell != project["matched_cell"]:
        raise ValueError("Frozen target cell changed")
    spec = load_requirements(project["requirements_file"], task=task.model_dump(mode="json"))
    if spec.get("annotation_completeness", {}).get("status") != "complete":
        raise ValueError("Formal requirements annotation is not complete; metadata review required")
    count_semantics = validate_ci_count_metadata(
        spec, base=requirements_evidence_root(project["requirements_file"]), required=True)
    if manifest.get("require_ci_count_comparability") is True and count_semantics["status"] != "available":
        raise ValueError("Campaign requires source-verified CI count comparability")
    protocol = {
        "protocol": REQUIREMENTS_PROTOCOL,
        "task_sha256": task.sha256,
        "requirements_sha256": canonical_digest(spec),
        "requirements_file_sha256": project["requirements_file_sha256"],
        "policy_version": POLICY_VERSION,
    }
    prepared_identity = project.get("evaluation_protocol")
    if prepared_identity is not None and prepared_identity != protocol:
        raise ValueError("Prepared evaluation protocol changed")
    return protocol


def collected_protocol_mismatches(pin: Any, project: dict, protocol: dict | None) -> list[str]:
    if protocol is None:
        return []
    config = pin.get("sanitized_config") if isinstance(pin, dict) else None
    if not isinstance(config, dict):
        return ["evaluation_protocol", "ci_target"]
    mismatches = []
    if config.get("evaluation_protocol") != protocol:
        mismatches.append("evaluation_protocol")
    target = config.get("ci_target")
    expected = {
        "record_sha256": project["target_sha256"],
        "repo": project["repo"],
        "sha": project["sha"],
        "matched_cell": project.get("matched_cell"),
    }
    if not isinstance(target, dict) or any(target.get(k) != v for k, v in expected.items()):
        mismatches.append("ci_target")
    return mismatches


def intervention_telemetry(result: dict, *, process_started: bool, process_finished: bool) -> dict:
    """Report only channels the runner observes; never turn silence into total zero."""
    events = []
    if result.get("cancelled"):
        events.append({"kind": "manual_cancel", "source": "campaign_signal_handler"})
    observed = process_started and process_finished
    return {
        "schema_version": 2,
        "run_id": result.get("run_id"),
        "run_key": result["run_key"],
        "source": "campaign_noninteractive_process_record",
        "runner_sha256": digest(Path(__file__)),
        "entrypoint": "sag project",
        "observation_start": result.get("process_started_at"),
        "observation_end": result.get("process_finished_at"),
        "input_policy": {"stdin": "DEVNULL", "runner_control_input": "disabled"},
        "controlled_channel_coverage": "complete" if observed else "unavailable",
        "controlled_channel_interventions": len(events) if observed else None,
        "external_intervention_coverage": "unavailable",
        "external_intervention_limit": "Host or Docker edits outside this runner are not monitored",
        "human_interventions": None,
        "events": events,
        "automatic_timeout": result.get("outer_timeout", False),
        "autonomous_success_eligibility": "unavailable",
    }


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
    protocol = getattr(args, "protocol", "legacy")
    if protocol == REQUIREMENTS_PROTOCOL:
        if args.phase == "paired":
            raise ValueError("The frozen historical paired baseline does not implement requirements-v2")
        tasks_dir = getattr(args, "acceptance_tasks_dir", None)
        requirements_dir = getattr(args, "requirements_dir", None)
        if tasks_dir is None or requirements_dir is None:
            raise ValueError("Formal preparation requires task and requirements directories")
        for project in projects:
            task_file = tasks_dir.resolve() / f"{project['seat']}.json"
            requirements_file = requirements_dir.resolve() / f"{project['seat']}.json"
            project.update(
                target_file=project["target_source"],
                acceptance_task_file=str(task_file),
                acceptance_task_sha256=digest(task_file),
                requirements_file=str(requirements_file),
                requirements_file_sha256=digest(requirements_file),
            )
            project["evaluation_protocol"] = requirements_preflight(
                {"protocol": protocol}, project
            )
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
        "protocol": protocol,
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
            "baseline": (
                "Every formal arm receives the same frozen task, CI target and requirements"
                if protocol == REQUIREMENTS_PROTOCOL
                else "no --ci-target-file; no production CI comparison result"
            ),
            "runs_per_subject_variant": 1,
            "efficiency_claim": "exploratory pair only, not a stable general speedup estimate",
        },
        "runner_path": str(Path(__file__).resolve()),
        "runner_sha256": digest(Path(__file__)),
        "python_environment": str(args.python_environment.resolve()),
        "projects": projects,
    }
    intervention_file = getattr(args, "intervention_protocol_file", None)
    if intervention_file is not None:
        from sag.benchmark.intervention_protocol import validate_protocol

        manifest["intervention_protocol"] = validate_protocol(json.loads(intervention_file.read_bytes()))
    out.mkdir(parents=True)
    for project in projects:
        dest = out / "targets" / f"{project['run_key']}.json"
        dest.parent.mkdir(exist_ok=True)
        shutil.copyfile(project["target_source"], dest)
        project["target_file"] = str(dest)
        if protocol == REQUIREMENTS_PROTOCOL:
            from sag.benchmark.requirements import copy_ci_count_sources
            copy_ci_count_sources(project["requirements_file"], out)
            for key, child in (("acceptance_task_file", "tasks"), ("requirements_file", "requirements")):
                dest = out / child / f"{project['run_key']}.json"
                dest.parent.mkdir(exist_ok=True)
                shutil.copyfile(project[key], dest)
                project[key] = str(dest)
            requirements_preflight(manifest, project)
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
        + (
            "The requirements-v2 protocol passes and verifies all three frozen inputs for every arm. Requirements with pending metadata review cannot start a formal attempt. Controlled process input is disabled; external host and Docker intervention coverage remains unavailable, so the runner does not claim an all-channel human-intervention zero.\n"
            if protocol == REQUIREMENTS_PROTOCOL
            else "The baseline cannot publish the new production CI comparison; this is recorded explicitly. Separate corrected historical labels from newly completed tasks. The independent Jenkins control is a changed task and is excluded from original 23-project progress. No efficiency claim is justified by one pair.\n"
        )
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
    formal = requirements_protocol(manifest)
    if formal:
        required = ("acceptance_task_file", "target_file", "requirements_file")
        if any(not project.get(key) for key in required):
            raise ValueError("Formal command requires acceptance task, CI target and requirements")
        if not re.fullmatch(r"[0-9a-f]{40}", str(project.get("sha", ""))):
            raise ValueError("Formal command requires a full frozen project ref")
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
    if formal or project["variant"] == "candidate":
        argv.extend(["--ci-target-file", project["target_file"]])
        if project.get("acceptance_command"):
            argv.extend(["--acceptance-command", project["acceptance_command"]])
        if project.get("acceptance_task_file"):
            argv.extend(["--acceptance-task-file", project["acceptance_task_file"]])
        if formal:
            argv.extend(["--requirements-file", project["requirements_file"]])
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


def _raw_log_hash(path: Path, length: int | None = None) -> str:
    """Hash physical bytes without retaining a large runner log in memory."""
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while length is None or length > 0:
            chunk = stream.read(1024 * 1024 if length is None else min(length, 1024 * 1024))
            if not chunk:
                break
            result.update(chunk)
            if length is not None:
                length -= len(chunk)
    return result.hexdigest()


def archive_raw_job_logs(directory: Path, evidence: Path, identity: str) -> dict:
    """Collect referenced job files; this does not grant receipt or run authority."""
    result: dict[str, Any] = {
        "scope": "OutputStorage full_log_path references; physical bytes kept separately from summaries",
        "receipt_authority": "not_evaluated_by_archiver",
        "files": [],
        "errors": [],
    }
    roots = [evidence / ".setup_agent"]
    for session in sorted((directory / "logs").glob("session_*")):
        roots.extend([session, session / ".setup_agent"])
    references: dict[str, list[dict]] = {}

    def record(metadata: Any, ref: Any, source: str) -> None:
        if not isinstance(metadata, dict):
            raise ValueError("OutputStorage metadata is not an object")
        path = metadata.get("full_log_path")
        if path is None:
            if metadata.get("output_storage_truncated") is True:
                result["errors"].append(f"{source}:{ref}: truncated output has no full_log_path")
            return
        if (
            not isinstance(path, str)
            or re.fullmatch(r"/tmp/sag_jobs/[0-9a-f]{12,64}\.log", path) is None
        ):
            result["errors"].append(
                f"{source}:{ref}: full_log_path is outside the job-log boundary"
            )
            return
        # Keep every declaration, including disagreements between index/journal
        # and host/container mirrors. No last-write-wins evidence repair.
        references.setdefault(path, []).append(
            {
                "metadata_source": source,
                "output_ref": ref,
                "declared_bytes": metadata.get("full_log_bytes"),
                "declared_sha256": metadata.get("full_log_sha256"),
            }
        )

    for root in roots:
        for name in ("output_index.json", "output_index.jsonl", "full_outputs.jsonl"):
            path = root / "contexts" / name
            source = str(path.relative_to(directory))
            if path.is_symlink():
                result["errors"].append(f"{source}: metadata is a symlink")
                continue
            if not path.exists():
                continue
            if not path.is_file() or not path.resolve().is_relative_to(directory.resolve()):
                result["errors"].append(f"{source}: metadata is not a contained regular file")
                continue
            try:
                if path.suffix == ".json":
                    payload = json.loads(path.read_text())
                    if not isinstance(payload, dict):
                        raise ValueError("OutputStorage index is not an object")
                    for ref, entry in payload.items():
                        record(entry.get("metadata", {}), ref, source)
                else:
                    with path.open() as stream:
                        for number, line in enumerate(stream, 1):
                            payload = json.loads(line)
                            record(
                                payload.get("metadata", {}),
                                payload.get("ref_id"),
                                f"{source}:{number}",
                            )
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                result["errors"].append(f"{source}: {type(exc).__name__}: {exc}")

    for source, claims in sorted(references.items()):
        path = directory / "raw-job-logs" / posixpath.basename(source)
        path.parent.mkdir(parents=True, exist_ok=True)
        item = {
            "source_path": source,
            "path": str(path.relative_to(directory)),
            "metadata_claims": claims,
            "receipt_authority": "not_evaluated_by_archiver",
            "exit_code": None,
            "bytes": None,
            "sha256": None,
            "physical_copy_complete": False,
            "declared_match": "unavailable",
            "complete": False,
        }
        try:
            copied = subprocess.run(
                ["docker", "cp", f"{identity}:{source}", str(path)],
                capture_output=True,
                text=True,
                timeout=300,
            )
            item.update(exit_code=copied.returncode, error=copied.stderr)
            if path.is_symlink():
                item["error"] = "raw job log copy is a symlink, not physical file bytes"
            elif path.is_file():
                item.update(bytes=path.stat().st_size, sha256=_raw_log_hash(path))
                item["physical_copy_complete"] = copied.returncode == 0
                valid = all(
                    type(claim["declared_bytes"]) is int
                    and claim["declared_bytes"] >= 0
                    and isinstance(claim["declared_sha256"], str)
                    and re.fullmatch(r"[0-9a-f]{64}", claim["declared_sha256"]) is not None
                    for claim in claims
                )
                if not valid:
                    item["declared_match"] = "invalid_metadata"
                else:
                    expected = {
                        (claim["declared_bytes"], claim["declared_sha256"]) for claim in claims
                    }
                    if len(expected) != 1:
                        item["declared_match"] = "conflicting_metadata"
                    else:
                        size, sha = next(iter(expected))
                        item["declared_match"] = "mismatch"
                        if (item["bytes"], item["sha256"]) == (size, sha):
                            item["declared_match"] = "exact"
                        elif item["bytes"] == size + 1:
                            with path.open("rb") as stream:
                                stream.seek(-1, 2)
                                terminal_lf = stream.read(1) == b"\n"
                            normalized_sha = _raw_log_hash(path, size) if terminal_lf else None
                            if normalized_sha == sha:
                                # Existing execute_control_command text transport
                                # strips output. Recognize only this measured one-LF
                                # case, never rewrite the physical archive file.
                                item.update(
                                    declared_match="normalized_output_match",
                                    transform="remove_one_terminal_lf",
                                    normalized_bytes=size,
                                    normalized_sha256=normalized_sha,
                                )
                item["complete"] = item["physical_copy_complete"] and item["declared_match"] in {
                    "exact",
                    "normalized_output_match",
                }
        except (OSError, subprocess.TimeoutExpired) as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            # A failed/timeout copy may still leave useful partial physical
            # bytes. Retain and fingerprint them without claiming completeness.
            if item["bytes"] is None and not path.is_symlink() and path.is_file():
                try:
                    item.update(bytes=path.stat().st_size, sha256=_raw_log_hash(path))
                except OSError as exc:
                    item["retained_file_error"] = f"{type(exc).__name__}: {exc}"
        result["files"].append(item)
    result["complete"] = not result["errors"] and all(item["complete"] for item in result["files"])
    result["status"] = (
        ("complete" if references else "not_required") if result["complete"] else "incomplete"
    )
    return result


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
        diagnostics["raw_job_logs"] = archive_raw_job_logs(directory, evidence, identity)
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


def configuration_failure(out: Path, manifest: dict, project: dict, exc: Exception) -> dict:
    """A frozen slot stays in the denominator even when it cannot be dispatched."""
    result = {
        "run_key": project["run_key"],
        "repo": project.get("repo"),
        "target_sha": project.get("sha"),
        "variant": project.get("variant"),
        "protocol": manifest.get("protocol"),
        "status": "unavailable",
        "authority_ok": False,
        "dispatched": False,
        "configuration_error": f"{type(exc).__name__}: {exc}",
        "planned_slot_retained": True,
        "finished_at": now(),
    }
    directory = out / "runs" / project["run_key"]
    if directory.exists():
        raise RuntimeError("Protocol preflight failed for an existing attempt; review before reuse") from exc
    save(directory / "result.json", result)
    event(out, "configuration_unavailable", run_key=project["run_key"], error=result["configuration_error"])
    return result


def run_one(out: Path, manifest: dict, project: dict) -> dict:
    try:
        protocol = requirements_preflight(manifest, project)
        intervention_policy = manifest.get("intervention_protocol")
        if intervention_policy is not None:
            from sag.benchmark.intervention_protocol import validate_protocol

            validate_protocol(intervention_policy)
    except (OSError, ValueError, RuntimeError) as exc:
        return configuration_failure(out, manifest, project, exc)
    task_hash = None
    task_pin = None
    if project.get("acceptance_task_file"):
        from sag.agent.acceptance_task import load_acceptance_task

        if project["variant"] != "candidate" and protocol is None:
            raise ValueError("The baseline does not support a required task file")
        task_path = Path(project["acceptance_task_file"])
        task_hash = digest(task_path)
        if task_hash != project.get("acceptance_task_sha256"):
            raise RuntimeError("Prepared acceptance task bytes changed")
        task = load_acceptance_task(task_path)
        if (task.repo, task.sha) != (project["repo"], project["sha"]):
            raise RuntimeError("Prepared acceptance task subject differs")
        task_pin = {"sha256": task.sha256, "definition": task.model_dump(mode="json")}
    elif project.get("acceptance_task_sha256"):
        raise RuntimeError("Prepared acceptance task file is missing")
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
            "acceptance_task_file_sha256": task_hash,
        }
        if protocol is not None:
            expected["evaluation_protocol"] = protocol
        if intervention_policy is not None:
            from sag.benchmark.requirements import canonical_digest

            expected["intervention_protocol_sha256"] = canonical_digest(intervention_policy)
        elif previous.get("intervention_protocol_sha256") is not None:
            raise RuntimeError("Existing result used a different intervention protocol")
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
    if protocol is not None:
        from sag.benchmark.requirements import copy_ci_count_sources
        copy_ci_count_sources(project["requirements_file"], directory)
        result.update(
            protocol=REQUIREMENTS_PROTOCOL,
            evaluation_protocol=protocol,
            production_ci_result_supported=True,
        )
        for file_key, expected_digest, filename, flag in (
            ("target_file", project["target_sha256"], "ci-target.json", "--ci-target-file"),
            ("requirements_file", project["requirements_file_sha256"], "requirements.json", "--requirements-file"),
        ):
            archived = directory / filename
            shutil.copyfile(project[file_key], archived)
            if digest(archived) != expected_digest:
                raise RuntimeError(f"{file_key} changed during archival")
            flag_index = result["command"].index(flag)
            result["command"][flag_index + 1] = str(archived)
    if intervention_policy is not None:
        from sag.benchmark.requirements import canonical_digest

        result["intervention_protocol_sha256"] = canonical_digest(intervention_policy)
    if task_hash is not None:
        result["acceptance_task_file_sha256"] = task_hash
        shutil.copyfile(project["acceptance_task_file"], directory / "acceptance-task.json")
        # The child reads the archived bytes checked above, not a mutable
        # external path. The run pin records the parsed definition as well.
        if digest(directory / "acceptance-task.json") != task_hash:
            raise RuntimeError("Acceptance task changed during archival")
        flag = result["command"].index("--acceptance-task-file")
        result["command"][flag + 1] = str(directory / "acceptance-task.json")
    save(directory / "started.json", result)
    event(out, "started", run_key=project["run_key"], container=project["container"])
    container_id = None
    process = None
    process_tick = None
    intervention_ledger = None

    def close_intervention_ledger():
        if intervention_ledger is not None:
            from sag.benchmark.intervention_protocol import close

            try:
                close(intervention_ledger, result, process_started=process is not None,
                      process_finished=process is not None and process.poll() is not None)
            except Exception as exc:
                result["intervention_protocol_error"] = f"{type(exc).__name__}: {exc}"

    try:
        if intervention_policy is not None:
            from sag.benchmark.intervention_protocol import initialize

            intervention_ledger = directory / "intervention-ledger"
            initialize(intervention_ledger, policy=intervention_policy,
                       run_key=result["run_key"], runner_file=Path(__file__), manifest=manifest)
        save(
            directory / "effective-config.json",
            effective_config(manifest, project, environment, directory),
        )
        with (directory / "console.log").open("wb") as log:
            result["process_started_at"] = now()
            process_tick = time.monotonic()
            process = subprocess.Popen(
                result["command"],
                cwd=directory,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            result["process_id"] = process.pid
            save(
                directory / "process.json", {"pid": process.pid, "started_at": result["started_at"]}
            )
            save(
                directory / "intervention-telemetry.json",
                intervention_telemetry(result, process_started=True, process_finished=False),
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
            result["process_finished_at"] = now()
            result["process_seconds"] = round(time.monotonic() - process_tick, 3)
            if STOP.is_set():
                result["cancelled"] = True
            close_intervention_ledger()
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
                if task_pin is not None and (
                    not isinstance(pin, dict)
                    or (pin.get("sanitized_config") or {}).get("acceptance_task") != task_pin
                ):
                    mismatches.append("acceptance_task")
                mismatches.extend(collected_protocol_mismatches(pin, project, protocol))
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
            # Collection can fail after the process and its intervention ledger
            # are already closed. Preserve that observed execution window;
            # collector/archive time is not agent execution time.
            if "process_finished_at" not in result:
                result["process_finished_at"] = now()
            if "process_seconds" not in result:
                result["process_seconds"] = round(time.monotonic() - process_tick, 3)
            if STOP.is_set():
                result["cancelled"] = True
    finally:
        close_intervention_ledger()
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
                    and diagnostics.get("raw_job_logs", {}).get("complete") is True
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
        telemetry = intervention_telemetry(
            result, process_started=process is not None,
            process_finished=process is not None and process.poll() is not None,
        )
        if intervention_ledger is not None:
            from sag.benchmark.intervention_protocol import summary

            try:
                telemetry.update(summary(intervention_ledger, result))
                telemetry["autonomous_success_eligibility"] = "protocol_observed" if telemetry["coverage"] == "complete" else "unavailable"
            except Exception as exc:
                telemetry["protocol_error"] = f"{type(exc).__name__}: {exc}"
        save(directory / "intervention-telemetry.json", telemetry)
        result["intervention_telemetry_sha256"] = digest(directory / "intervention-telemetry.json")
        if protocol is not None:
            try:
                from sag.benchmark.campaign_telemetry import finalize_campaign_analysis

                sessions = sorted((directory / "logs").glob("session_*"))
                if len(sessions) != 1:
                    raise ValueError("Expected one host session for requirements analysis")
                result["requirements_analysis"] = finalize_campaign_analysis(
                    sessions[0], result, runner_file=Path(__file__),
                    intervention_file=directory / "intervention-telemetry.json",
                )
            except Exception as exc:
                result["requirements_analysis"] = {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
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
    action.add_argument("--record-intervention", metavar="RUN_KEY")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--expected-sha")
    parser.add_argument("--baseline-source", type=Path)
    parser.add_argument("--baseline-sha", default=BASELINE_SHA)
    parser.add_argument("--targets-dir", type=Path)
    parser.add_argument("--protocol", choices=("legacy", REQUIREMENTS_PROTOCOL), default="legacy")
    parser.add_argument("--acceptance-tasks-dir", type=Path)
    parser.add_argument("--requirements-dir", type=Path)
    parser.add_argument("--intervention-protocol-file", type=Path)
    parser.add_argument("--intervention-kind", choices=("guidance", "manual_edit", "environment_change", "manual_restore", "manual_cancel", "unknown"))
    parser.add_argument("--intervention-detail")
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
    if args.record_intervention:
        from sag.benchmark.intervention_protocol import record_event

        if args.record_intervention not in {p["run_key"] for p in manifest["projects"]}:
            parser.error("--record-intervention must name a prepared run key")
        if not args.intervention_kind or not args.intervention_detail:
            parser.error("Recording an intervention requires its kind and description")
        recorded = record_event(out / "runs" / args.record_intervention / "intervention-ledger",
                                kind=args.intervention_kind, detail=args.intervention_detail)
        print(json.dumps(recorded))
        return
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
