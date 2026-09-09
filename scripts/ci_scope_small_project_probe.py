#!/usr/bin/env python3
"""Manual real-container CI-scope probe; no Docker is run on import or by tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PROJECTS = {
    "commons-cli": ("apache/commons-cli", "e17111798da51037659b3594d9c0b3b525040081", "test"),
    "gson": ("google/gson", "b3f4ca20087f9066de4c340522ff84e0558e1ad1", "package"),
    "httpcomponents-client": (
        "apache/httpcomponents-client",
        "be07c77297576b08bc01bb93789eca6f5f9bf850",
        "verify",
    ),
}


def probe_steps(project: str) -> tuple[dict, dict]:
    """The predeclared deterministic protocol, independent of model choices."""
    root = f"/workspace/{project}"
    if project == "httpcomponents-client":
        common = "-B -f pom.xml -P-use-toolchains,nodoclint"
        calls = (
            ("compile", common, "mvn -B -f pom.xml compile -P-use-toolchains,nodoclint"),
            (
                "verify",
                "-B -f pom.xml clean install -P-use-toolchains,nodoclint",
                "mvn -B -f pom.xml clean verify install -P-use-toolchains,nodoclint",
            ),
        )
    else:
        action = "package" if project == "gson" else "compile"
        calls = ((action, "", f"mvn {action}"), ("test", "", "mvn test"))
    return tuple(
        {
            "action": action,
            "working_directory": root,
            "timeout": 900,
            "system": "maven",
            "source_command": source,
            **({"args": args} if args else {}),
        }
        for action, args, source in calls
    )


def seal_probe_plan(runtime, audit, epoch, project: str, steps: tuple[dict, dict]):
    """Bind a controller-authored task to real source reads before dispatch."""
    from sag.agent.evidence_state import RunEvidenceState, StateScope
    from sag.agent.output_storage import attach_durable_output_ref
    from sag.agent.phase_gates import claim_identity
    from sag.agent.phase_machine import PhaseAttemptRecord, PhaseClaim
    from sag.agent.project_execution_plan import (
        canonical_authored_plan_sha256,
        seal_and_write_project_execution_plan,
        validate_authored_plan,
        validate_reviewed_document_evidence,
    )
    from sag.runtime.container_io import read_container_text
    from sag.tools.base import ToolResult, bind_tool_result_output_storage

    attempt = "deterministic-analyze"
    state = RunEvidenceState(run_id=epoch.run_id)
    params = {"action": "read", "path": f"/workspace/{project}/pom.xml"}
    storage = runtime.output_storage_for(audit)
    with (
        runtime.evidence_epoch(audit),
        bind_tool_result_output_storage(
            storage, task_id=attempt, tool_name="controller_source_read"
        ),
    ):
        body = read_container_text(audit, params["path"], exact_bytes=True)
        if body is None:
            raise RuntimeError("probe plan source is unavailable")
        result = attach_durable_output_ref(
            ToolResult.completed_success(
                output=body,
                metadata={
                    "path": params["path"],
                    "source_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                },
            ),
            storage,
            task_id=attempt,
            tool_name="controller_source_read",
            action="read",
        )
    if not result.succeeded or not result.output_ref:
        raise RuntimeError("probe plan source read was not durably recorded")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "controller_source_read",
        result,
        params=params,
        source_phase="analyze",
        source_attempt_id=attempt,
    )
    refs = [result.output_ref]
    step = lambda call: {
        "tool": "build",
        "params": call,
        "purpose": "Execute the frozen deterministic probe task.",
        "evidence_refs": refs,
    }
    plan = validate_authored_plan(
        {
            "summary": "Controller-authored deterministic probe protocol; no model strategy claim.",
            "documents_reviewed": [
                {
                    "path": params["path"],
                    "reason": "Read the pinned Maven project definition.",
                    "evidence_refs": refs,
                }
            ],
            "build_steps": [step(steps[0])],
            "build_success_criteria": ["The declared build call finishes."],
            "test_steps": [step(steps[1])],
            "test_success_criteria": [
                "The declared test call completes with nonempty verified reports."
            ],
            "test_disposition": {
                "status": "planned",
                "reason": "Frozen experiment protocol requires the recorded test call.",
                "execution_mechanism": "Maven Surefire/Failsafe through BuildTool and verified XML.",
                "verdict_scope": "product_test_cases",
                "readiness": "ready",
                "evidence_refs": refs,
                "definition_evidence_refs": refs,
            },
            "environment_constraints": [],
            "risks": [],
            "unresolved_questions": [],
        },
        require_test_disposition=True,
    )
    validate_reviewed_document_evidence(
        plan,
        observations=state.tool_observations,
        output_reader=storage.retrieve_output,
        source_attempt_id=attempt,
    )
    claim = PhaseClaim(
        phase="analyze",
        signal="done",
        claimed_outcome="success",
        execution_plan_sha256=canonical_authored_plan_sha256(plan),
        evidence_refs=tuple(refs),
    )
    artifact = seal_and_write_project_execution_plan(
        plan,
        audit,
        source_attempt_id=attempt,
        claim_sha256=claim_identity(claim),
    )
    record = PhaseAttemptRecord(
        phase="analyze",
        attempt_id=attempt,
        termination="completed",
        outcome="success",
        transition="advance",
        claim=claim,
    )
    return artifact, record, result, params


def dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str) + "\n")


def engine_hashes() -> dict[str, str]:
    paths = sorted((ROOT / "src/sag").rglob("*.py")) + [
        Path(__file__),
        ROOT / "scripts/run_d0_docker_probes.py",
    ]
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def target_ablations(view, target) -> dict:
    """Change only the external target; subject mismatches remain invalid."""
    from sag.metrics.attainment import evaluate_attainment
    from sag.metrics.target_record import TargetRecord

    result = {"full_target": evaluate_attainment(view, target).model_dump(mode="json")}
    no_scope = target.model_dump(mode="json")
    for cell in no_scope["cells"]:
        cell.update(modules=[], modules_basis=None)
    result["without_target_scope"] = evaluate_attainment(view, TargetRecord(**no_scope)).model_dump(
        mode="json"
    )
    result["without_target"] = {
        "attainment": None,
        "reason": "no CI target, execution evidence only",
    }
    for key, value in (("repo", "unrelated/repository"), ("sha", "0" * 40)):
        payload = target.model_dump(mode="json")
        payload[key] = value
        result["mismatched_" + key] = evaluate_attainment(view, TargetRecord(**payload)).model_dump(
            mode="json"
        )
    return result


def checked_control(audit, command: str):
    result = audit.execute_control_command(command)
    if result.get("exit_code") != 0:
        raise RuntimeError(f"control mutation failed ({result.get('exit_code')}): {command}")
    return result


def run_project(
    project: str,
    out: Path,
    *,
    container_name: str,
    experiment_id: str,
    expected_source_sha: str,
    ci_target_file: Path | None = None,
) -> dict:
    from loguru import logger

    from sag.agent.attempt_policy import resolve_current_build_receipt_scope
    from sag.agent.ci_comparison import load_ci_target
    from sag.agent.control_events import RunPin, canonical_json
    from sag.agent.evidence_publications import (
        RUN_PIN_LOGICAL_ARTIFACT_ID,
        latest_publication_raw_sha256,
        publish_evidence_revision,
    )
    from sag.agent.evidence_state import EvidenceRole, RunEvidenceState, StateScope
    from sag.agent.invocation_receipts import set_active_receipt_run_id
    from sag.agent.phase_gates import _inspect_test
    from sag.agent.physical_validator import PhysicalValidator
    from sag.agent.verdict_finalizer import EvidenceCloseReason, VerdictFinalizer
    from sag.docker_orch.orch import DockerOrchestrator
    from sag.tools.base import bind_tool_result_output_storage
    from sag.tools.build.build_tool import BuildTool
    from sag.tools.internal.maven_tool import MavenTool
    from sag.tools.internal.project_analyzer import ProjectAnalyzerTool
    from sag.utils.container_io import write_container_text_atomic
    from scripts.run_d0_docker_probes import (
        CommandAudit,
        DockerProbeRuntime,
        ProbeSpec,
        _controller_action_scope,
        _install_epoch_authority,
        _strict_container_receipts,
    )

    repo, sha, action = PROJECTS[project]
    import sag

    if not Path(sag.__file__).resolve().is_relative_to(ROOT.resolve()):
        raise RuntimeError("probe imported SAG from a different source checkout")
    actual_source = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    if (
        actual_source != expected_source_sha
        or subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()
    ):
        raise RuntimeError("probe requires the clean frozen source commit")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", experiment_id):
        raise ValueError("experiment id must be a bounded path-safe identifier")
    if experiment_id not in container_name:
        raise ValueError("container name must include this experiment id")
    if out.resolve().is_relative_to((ROOT / "logs/d3r1-20260907").resolve()):
        raise ValueError("sealed D3R1 evidence is read-only")
    name = container_name
    target = load_ci_target(ci_target_file) if ci_target_file else None
    if target is not None and (target.record.repo, target.record.sha) != (repo, sha):
        raise ValueError("probe target does not match its frozen project revision")
    steps = probe_steps(project)
    project_path = f"/workspace/{project}"
    destination = out / project
    destination.mkdir(parents=True, exist_ok=False)
    logger.remove()
    logger.add(str(destination / "engine.log"), level="DEBUG")
    logger.add(sys.stderr, level="WARNING")
    initial_hashes = engine_hashes()

    def require_frozen_source() -> None:
        if (
            engine_hashes() != initial_hashes
            or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
            != expected_source_sha
            or subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=ROOT, text=True
            ).strip()
        ):
            raise RuntimeError("probe source changed after experiment freeze")

    dump(destination / "engine-source.sha256.json", initial_hashes)
    with tarfile.open(destination / "engine-source.tar.gz", "w:gz") as archive:
        for relative in initial_hashes:
            archive.add(ROOT / relative, arcname=relative)
    image_id = subprocess.check_output(
        ["docker", "inspect", "--format", "{{.Image}}", name], text=True
    ).strip()
    runtime = DockerProbeRuntime(
        ROOT,
        out / ".runtime",
        experiment_id,
        image_id,
        ProbeSpec(project, 0, "real source build scope ablation"),
        {"image": {"id": image_id}},
        True,
    )
    orch = DockerOrchestrator(base_image=image_id, project_name=name.removeprefix("sag-"))
    audit = CommandAudit(orch)
    runtime.audits.append(audit)
    runtime.containers.append(orch)
    epoch = runtime._register_epoch(audit, suffix="real")
    _install_epoch_authority(epoch.authority, audit)
    set_active_receipt_run_id(epoch.run_id)
    checked_control(audit, "mkdir -p /workspace/.setup_agent /tmp/sag_jobs")
    actual_sha = audit.execute_control_command(f"git -C {shlex.quote(project_path)} rev-parse HEAD")
    if str(actual_sha.get("output", "")).strip() != sha:
        raise RuntimeError("container source does not match fixed commit")
    dump(
        destination / "run-pin.json",
        {
            "repo": repo,
            "sha": sha,
            "run_id": epoch.run_id,
            "container": name,
            "image_id": image_id,
            "action": action,
            "steps": steps,
            "ci_target_sha256": target.raw_sha256 if target else None,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "mode": "deterministic production tool/receipt/physical/finalizer; no model phase lifecycle",
        },
    )
    production_pin = RunPin(
        run_id=epoch.run_id,
        target_repo_sha=sha,
        container_image_digest=image_id,
        sag_git_sha=subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        thinking_model="none:no-model",
        action_model="none:no-model",
        sanitized_config={
            "campaign_kind": "ci-scope-deterministic-real-project",
            "target_repository": repo,
            "ci_target_sha256": target.raw_sha256 if target else None,
            "experiment_id": experiment_id,
            "steps": steps,
        },
        prompt_bundle_sha256=hashlib.sha256(b"not-applicable:no-model").hexdigest(),
        feature_flags={},
        run_order_index=0,
        random_seed_or_null=None,
        dependency_cache_state="independent fresh container; fixed image cache only; no host cache mount",
        host_arch=platform.machine(),
        advisor={"mode": "off", "calls": []},
    )
    body = canonical_json(production_pin)
    previous = latest_publication_raw_sha256(audit, RUN_PIN_LOGICAL_ARTIFACT_ID)
    write = write_container_text_atomic(
        audit, "/workspace/.setup_agent/run-pin.json", body, validate_json=True
    )
    if not write.persisted:
        raise RuntimeError("run pin mirror did not persist")
    publication = publish_evidence_revision(
        audit,
        record_kind="run_pin",
        record_id=RUN_PIN_LOGICAL_ARTIFACT_ID,
        logical_artifact_id=RUN_PIN_LOGICAL_ARTIFACT_ID,
        raw=body.encode(),
        expected_previous_raw_sha256=previous,
    )
    if not publication.published:
        raise RuntimeError("run pin was not host published")
    dump(destination / "production-run-pin.json", production_pin.model_dump(mode="json"))
    with (
        runtime.evidence_epoch(audit),
        bind_tool_result_output_storage(
            runtime.output_storage_for(audit),
            task_id=f"survey-{project}",
            tool_name="project_analyzer",
        ),
    ):
        analysis = ProjectAnalyzerTool(audit).execute(
            project_path=project_path, update_context=False
        )
    dump(destination / "analysis.json", analysis.model_dump(mode="json"))
    if not analysis.succeeded:
        raise RuntimeError(f"production project survey failed: {analysis.error_code}")
    scope = resolve_current_build_receipt_scope(
        audit, run_id=epoch.run_id, workspace_root="/workspace", project_root=project_path
    )
    reader = PhysicalValidator(audit, project_path="/workspace", receipt_run_id=epoch.run_id)
    receipts_before = reader._read_live_invocation_receipts()
    bootstrap = {
        "receipt_scope": asdict(scope),
        "receipts_before_dispatch": receipts_before,
        "actual_sha": sha,
        "run_pin_published": publication.published,
        "survey_succeeded": analysis.succeeded,
        "workspace_root": "/workspace",
        "project_name": project,
    }
    dump(destination / "bootstrap-check.json", bootstrap)
    if not scope.available or receipts_before != []:
        raise RuntimeError(
            "pre-build bootstrap check did not establish an empty, bound receipt epoch"
        )
    print(
        f"{project}: bootstrap verified (published pin, bound empty receipt ledger, real manifest)",
        flush=True,
    )
    artifact, analyze_record, source_read, source_params = seal_probe_plan(
        runtime, audit, epoch, project, steps
    )
    dump(destination / "probe-plan.json", artifact.model_dump(mode="json"))
    dump(destination / "probe-analyze-record.json", asdict(analyze_record))
    dump(destination / "probe-source-read.json", source_read.model_dump(mode="json"))
    dump(destination / "probe-source-params.json", source_params)
    results = []
    for params in steps:
        require_frozen_source()
        verb = params["action"]
        print(f"{project}: dispatching production build {params}", flush=True)
        with _controller_action_scope(
            runtime,
            audit=audit,
            label=f"{project}-{verb}",
            domain_id=project,
            tool="build",
            params=params,
            next_action_kind=verb,
        ):
            result = BuildTool(audit, maven_tool=MavenTool(audit)).execute(**params)
        dump(destination / f"tool-result-{verb}.json", result.model_dump(mode="json"))
        results.append((params, result))
    dump(destination / "tool-result.json", result.model_dump(mode="json"))
    receipts = _strict_container_receipts(audit)
    dump(destination / "receipts.json", receipts)
    print(f"{project}: tool={result.operation_outcome.value}, receipts={len(receipts)}", flush=True)

    def snapshot(label: str, *, publish: bool = False):
        require_frozen_source()
        validator = PhysicalValidator(audit, project_path="/workspace", receipt_run_id=epoch.run_id)
        build = validator.validate_build_status(project)
        test = validator.validate_test_status(project)
        gate = _inspect_test(validator, project, audit)
        state = RunEvidenceState(run_id=epoch.run_id)
        state.record_phase_record(analyze_record)
        state.ingest_tool_result(
            StateScope.ARTIFACTS,
            "controller_source_read",
            source_read,
            params=source_params,
            source_phase="analyze",
            source_attempt_id=analyze_record.attempt_id,
        )
        for public_params, observed in results:
            state.ingest_tool_result(
                StateScope.ARTIFACTS,
                "build",
                observed,
                roles=(EvidenceRole.BUILD, EvidenceRole.TEST),
                params=public_params,
            )
        for key, value in gate.validated_facts.items():
            state.register_fact(StateScope.TEST_RUNTIME, key, value, "output_physical_test_scan")
        finalizer = VerdictFinalizer(
            audit, validator=validator, project_name=project, repository=repo, ci_target=target
        )
        if publish:
            verdict = finalizer.finalize(state, EvidenceCloseReason.TEST_TERMINATED)
        else:
            state.seal(
                finalized_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                close_reason="test_terminated",
            )
            verdict = finalizer._snapshot_for_state(state)
        data = {
            "build": build,
            "tests": test,
            "test_gate": asdict(gate),
            "snapshot": verdict.model_dump(mode="json"),
            "published": publish,
        }
        dump(destination / f"{label}.json", data)
        return data

    full = snapshot("full-evidence")
    # Archive source and evidence before physically removing anything in these new containers.
    subprocess.run(
        [
            "docker",
            "cp",
            f"{name}:/workspace/.setup_agent",
            str(destination / "setup-agent-evidence"),
        ],
        check=True,
    )
    subprocess.run(
        [
            "docker",
            "exec",
            name,
            "tar",
            "czf",
            "/tmp/project-evidence.tar.gz",
            "-C",
            "/workspace",
            project,
        ],
        check=True,
    )
    subprocess.run(
        [
            "docker",
            "cp",
            f"{name}:/tmp/project-evidence.tar.gz",
            str(destination / "project-evidence.tar.gz"),
        ],
        check=True,
    )
    receipt_dir = "/workspace/.setup_agent/invocation_receipts"
    checked_control(audit, f"mv {receipt_dir} /tmp/ci-scope-receipts-held")
    try:
        no_receipts = snapshot("without-receipts")
    finally:
        checked_control(audit, f"mv /tmp/ci-scope-receipts-held {receipt_dir}")
    # Hold every compiled/package output, including generated tests, without touching source.
    held_dir = "/tmp/ci-scope-targets-" + hashlib.sha256(epoch.run_id.encode()).hexdigest()[:12]
    script = """import pathlib, shutil, json
root=pathlib.Path(%r); held=pathlib.Path(%r); held.mkdir()
rows=[]
for index, path in enumerate(sorted(root.rglob('target'))):
 if not path.is_dir() or any(parent.name=='target' for parent in path.parents if parent!=root): continue
 destination=held/str(index); shutil.move(str(path),destination); rows.append([str(path),str(destination)])
(held/'paths.json').write_text(json.dumps(rows))
""" % (project_path, held_dir)
    checked_control(audit, "python3 -c " + shlex.quote(script))
    try:
        no_physical = snapshot("without-physical-outputs")
    finally:
        restore = f"import pathlib,json,shutil; p=pathlib.Path({held_dir!r}); [(shutil.move(b,a)) for a,b in json.loads((p/'paths.json').read_text())]"
        checked_control(audit, "python3 -c " + shlex.quote(restore))
    published = snapshot("published-finalizer", publish=True)
    dump(destination / "command-audit.json", audit.records)
    blobs = destination / "command-blobs"
    blobs.mkdir()
    for digest, blob_body in audit.blobs.items():
        (blobs / digest).write_bytes(blob_body)
    shutil.copy2(epoch.control_event_path, destination / "control-events.jsonl")
    final_hashes = engine_hashes()
    summary = {
        "project": project,
        "repo": repo,
        "sha": sha,
        "run_id": epoch.run_id,
        "tool_outcome": result.operation_outcome.value,
        "receipt_exit_codes": [r.get("exit_code") for r in receipts],
        "full": full["snapshot"]["verdict"],
        "without_receipts": no_receipts["snapshot"]["verdict"],
        "without_physical_outputs": no_physical["snapshot"]["verdict"],
        "published": published["snapshot"]["verdict"],
        "engine_source_unchanged": final_hashes == initial_hashes,
        "source_paths_changed_during_run": [
            p
            for p in sorted(set(initial_hashes) | set(final_hashes))
            if initial_hashes.get(p) != final_hashes.get(p)
        ],
    }
    dump(destination / "summary.json", summary)
    print(json.dumps(summary), flush=True)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", choices=PROJECTS, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--container", required=True, help="Existing isolated container for this experiment"
    )
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--expected-source-sha", required=True)
    parser.add_argument("--ci-target-file", type=Path)
    args = parser.parse_args()
    run_project(
        args.project,
        args.out,
        container_name=args.container,
        experiment_id=args.experiment_id,
        expected_source_sha=args.expected_source_sha,
        ci_target_file=args.ci_target_file,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
