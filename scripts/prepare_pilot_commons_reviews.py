#!/usr/bin/env python3
"""Offline, explicit CSV/DBCP review for the frozen 2026-09-22 pilot.

This imports newly captured Java21/Java8 models. Historical Java17 reviews
provide disposition vocabulary only; every binding and artifact is rechecked.
No Maven, Docker, network or model invocation is performed.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shlex
import zipfile

from scripts.build_benchmark_requirements import (
    apply_reviewed_plan, archive_source, build_project, ci_binding_inventory,
    ci_native_text, pom_execution_bindings, reviewed_preparation, task_digest,
    text_at, xml_root,
)
from scripts.requirements_metadata_inventory import attach_preparation_inventory
from sag.benchmark.ci_count_semantics import unavailable
from sag.benchmark.requirements import (
    bound_file, canonical_digest, load_requirements, validate_ci_count_metadata,
)
from sag.metrics.target_record import TargetRecord

ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / "output/java-benchmark-pilot-20260922"
PRIOR = ROOT / "output/java-benchmark-acquisition-20260922/pilot-requirements"
CAPTURE = PILOT / "metadata-captured"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_refs(value, base):
    if isinstance(value, dict):
        if {"path", "sha256", "bytes"} <= value.keys():
            path = bound_file(base, value)
            assert path.stat().st_size == value["bytes"], path
        for child in value.values():
            check_refs(child, base)
    elif isinstance(value, list):
        for child in value:
            check_refs(child, base)


def prepare(project):
    prior, capture = PRIOR / project, CAPTURE / project
    output = PILOT / "prepared" / project
    inputs = PILOT / "preparation" / project / "import-inputs"
    output.mkdir(parents=True, exist_ok=True)
    inputs.mkdir(parents=True, exist_ok=True)
    task_bytes = (prior / "task.json").read_bytes()
    task = json.loads(task_bytes)
    step = task["steps"][0]
    assert step["java_major"] == {"commons-csv": 21, "commons-dbcp": 8}[project]
    (inputs / "task.json").write_bytes(task_bytes)
    state = json.loads((capture / "metadata-status.json").read_text())
    check_refs(state, CAPTURE)
    source_index = json.loads((capture / "source-poms/index.json").read_text())
    check_refs(source_index, CAPTURE)
    assert source_index["commit"] == task["sha"]
    assert len(source_index["files"]) == 1
    assert source_index["files"][0]["source_path"] == "pom.xml"
    assert source_index["files"][0]["pinned_bytes_equal"] is True
    for name in ("tracked-diff-before.txt", "untracked-before.txt", "tracked-diff.txt", "untracked.txt"):
        assert (capture / name).read_bytes() == b"", name
    source_pom = (capture / "source-poms/pom.xml").read_bytes()
    assert source_pom == (prior / "source-config/pom.xml").read_bytes()
    (inputs / "source-pom.xml").write_bytes(source_pom)
    log_bytes = (prior / "ci/selected-job-1.log").read_bytes()
    (inputs / "official-ci.log").write_bytes(log_bytes)
    selection = json.loads((prior / "reference-task.json").read_text())
    origin = json.loads((prior / "ci-job-inventory.json").read_text())[0]["raw_source"]
    raw_zip = Path(origin["path"])
    assert sha(raw_zip) == origin["sha256"] and raw_zip.stat().st_size == origin["bytes"]
    with zipfile.ZipFile(raw_zip) as archive:
        assert archive.read(origin["zip_member"]) == log_bytes
    selection_path = Path(origin["selection_ref"]["path"])
    assert sha(selection_path) == origin["selection_ref"]["sha256"]
    assert origin["ci_url"] == selection["official_ci_url"]
    official_refs = {
        "job_log": archive_source(inputs / "official-ci.log", output, basis="exact_selected_job_log", official_url=origin["ci_url"]),
        "run_log_zip": archive_source(raw_zip, output, basis="original_official_run_logs", official_url=origin["origin_url"]),
        "selected_cell_index": archive_source(selection_path, output, basis="frozen_official_job_selection"),
        "task_reference": archive_source(prior / "reference-task.json", output, basis="frozen_dataset_task"),
    }
    index = {"schema_version": 1, "repo": task["repo"], "sha": task["sha"],
             "ci_identity": selection["ci_identity"], "selected_url": selection["official_ci_url"],
             "zip_member": origin["zip_member"], "sources": official_refs,
             "scope": "Exact selected job from frozen run/attempt archive; not latest CI."}
    write(inputs / "ci" / project / "index.json", index)
    manifest_project = {
        "id": project, "repo": task["repo"], "commit": task["sha"],
        "task": {"path": "task.json", "sha256": sha(inputs / "task.json")},
        "steps": [dict(step, stages=["build", "test"])], "scope_note": selection["scope"],
        "ci": {"selected_url": selection["official_ci_url"],
               "selected_cell": selection["ci_identity"]["job_name"],
               "comparison_admitted": True, "modules": ["."],
               "evidence_status": "selected_official_scope_and_native_suite_summary",
               "evidence": [{"path": p, "sha256": sha(inputs / p)}
                            for p in ("source-pom.xml", "official-ci.log")]},
    }
    write(inputs / "manifest.json", {"projects": [manifest_project]})
    spec = build_project(manifest_project, inputs, output, ROOT)
    old = json.loads((ROOT / f"output/sag-benchmark20-requirements-20260922/review-{project}.json").read_text())
    model = xml_root((capture / "effective-pom.xml").read_bytes())
    assert model.tag == "project" and model.find("modules") is None
    module = text_at(model, "artifactId")
    assert module == old["module"]["id"]
    ci_text = ci_native_text(log_bytes.decode())
    top, nested = ci_binding_inventory(ci_text)
    assert {r["module"] for r in top} == {module}
    assert len(top) == 32 and len(nested) == (9 if project == "commons-csv" else 32)
    workspace = state["steps"][0]["runtime_probe"]["cwd"]
    sources = {name: archive_source(path, output, basis="selected_runtime_" + name)
               for name, path in {
                   "effective_pom": capture / "effective-pom.xml", "effective_settings": capture / "effective-settings.xml",
                   "pom_log": capture / "effective-pom.log", "settings_log": capture / "effective-settings.log",
                   "runtime": capture / "runtime.txt", "head": capture / "HEAD.txt",
                   "tracked_diff": capture / "tracked-diff.txt", "untracked": capture / "untracked.txt",
                   "preparation_request": CAPTURE / f"request-{project}.json",
                   "preparation_script": CAPTURE / state["preparation_script"]["path"],
                   "official_ci": inputs / "official-ci.log",
               }.items()}
    # Also enforce structured probe/argv/status identity for this single module.
    reviewed_preparation(state, sources, task, step, workspace)
    old_ci = {(r["goal"], r["execution"], r["occurrence"]): r for r in old["ci_bindings"]}
    bindings = []
    for row in top:
        key = row["goal"], row["execution"], row["occurrence"]
        if row["goal"] == "animal-sniffer:check":
            assert project == "commons-dbcp" and row["version"] == "1.27"
            disposition = {"disposition": "requirement", "requirement_id": "ci-step-1-java_api_signature",
                           "reason": "The Java8 animal-sniffer profile binds checkAPIcompatibility; CI explicitly checks org.codehaus.mojo.signature:java18:1.0."}
        else:
            assert key in old_ci, key
            disposition = {k: v for k, v in old_ci[key].items() if k not in row}
        if row["goal"] == "bundle:manifest":
            disposition["reason"] = "Prepares the OSGi manifest consumed by this task's mandatory main JAR. Complete command execution retains the support goal."
        bindings.append({**row, **disposition})
    declarations = pom_execution_bindings(model)
    observed = {(r["goal"], r["execution"], r["version"]) for r in top}
    for row in declarations:
        if (row["goal"], row["execution"], row["version"]) in observed:
            row.update(disposition="observed", reason="This exact plugin version, goal and execution occurs at top level in the selected-JDK official invocation.")
        else:
            assert row["phase"] in {"install", "deploy", "site", "site-deploy"}, row
            row.update(disposition="outside_task_lifecycle", reason="This install/deploy/site execution is outside clean verify and the explicit defaultGoal checks; no publication or install is added.")
    rows = deepcopy(old["requirements"])
    if project == "commons-dbcp":
        rows = [r for r in rows if r["subtype"] != "jpms_augmented_main_jar"]
        rows.append({"id": "ci-step-1-java_api_signature", "kind": "quality_check", "subtype": "java_api_signature",
                     "validation": {"rule": "native_goal"}, "expectations": {}})
        assert "moditect:add-module-info" not in {r["goal"] for r in top}
    positions = {r["requirement_id"]: r["position"] for r in bindings if r["disposition"] == "requirement"}
    rows.sort(key=lambda r: positions[r["id"]])
    for i, row in enumerate(rows):
        row.update(depends_on=[rows[i-1]["id"]] if i else [], dependencies_complete=True)
    # Artifact candidates from the old checklist must have fresh selected-job
    # native output-path witnesses, fresh coordinates and matching new model.
    artifacts = {a["path"]: a for r in rows for a in r.get("expectations", {}).get("artifacts", [])}
    assert len(artifacts) == 7
    artifact_witnesses = []
    for name, artifact in artifacts.items():
        matches = [{"line": i, "text": line} for i, line in enumerate(ci_text.splitlines(), 1)
                   if name in line and any(s in line for s in ("Building jar:", "Writing and validating BOM", "Creating SPDX File"))]
        assert matches, name
        artifact_witnesses.append({"artifact": artifact, "native_output_witnesses": matches})
    defaults = shlex.split(text_at(model, "build/defaultGoal"))
    assert defaults == shlex.split(text_at(xml_root(source_pom), "build/defaultGoal")) == old["selected_goals"]
    review = {k: deepcopy(old[k]) for k in ("schema_version", "review_protocol", "module")}
    review.update(project_id=project, commit=task["sha"], task_sha256=task_digest(task),
                  source_paths_relative_to="requirements_bundle_root", metadata_workspace=workspace,
                  reviewed_by="SAG pilot selected-runtime source review, 2026-09-22",
                  review_notes=[
                      "Fresh selected-JDK effective model, settings, active profiles and bound command receipts reviewed; no Java17 model reuse.",
                      "Metadata runtime is Ubuntu OpenJDK with the selected Java major and patch, whereas official CI reports Temurin. Only Java major and Maven exact version are task constraints; vendor equality is not claimed.",
                      "Original task.json bytes are preserved. CI/capture MAVEN_ARGS=-ntp is recorded separately; the frozen argv already contains --no-transfer-progress, so no lifecycle/property scope change is introduced.",
                      "DefaultGoal order and every effective-POM binding and top-level/nested selected CI occurrence are accounted for. Forks cannot substitute for independent top-level outcomes.",
                      "Seven distinct package files have native selected-job path witnesses; generated JPMS augmentation is required only for CSV Java21, and Java8 API-signature verification only for DBCP.",
                      "Test-source exclusions remain exactly as declared upstream. RAT is not exempted from agent-created files. Japicmp resolves the configured previous release through the public repository.",
                      "Metadata cache remains isolated from reference/agent runs. Annotation completeness means definition readiness, not execution success, identical OS/vendor, full testcase identities, or CI scoring.",
                  ], sources=sources, selected_goals=defaults, pom_bindings=declarations,
                  ci_bindings=bindings,
                  nested_ci_bindings=[{**r, "disposition": "fork_support", "reason": "Nested fork occurrence preserved with exact source line and version; the enclosing selected top-level goal remains mandatory."} for r in nested],
                  requirements=rows)
    write(output / "review.json", review)
    spec = apply_reviewed_plan(spec, task, output / "review.json", output)
    spec = attach_preparation_inventory(spec, task, CAPTURE, output)
    supplemental = {name: archive_source(path, output, basis=name) for name, path in {
        "active_profiles": capture / "active-profiles.log", "raw_java_probe": capture / "java-version.txt",
        "tracked_before": capture / "tracked-diff-before.txt", "untracked_before": capture / "untracked-before.txt",
        "source_config_index": prior / "source-config-index.json", "prior_java17_review_checklist": ROOT / f"output/sag-benchmark20-requirements-20260922/review-{project}.json",
    }.items()}
    spec["pilot_review"] = {"metadata_sources": supplemental, "official_sources": official_refs,
                            "artifact_path_review": artifact_witnesses,
                            "environment_note": review["review_notes"][2],
                            "runtime_vendor_equality_claimed": False}
    spec["ci_alignment"]["test_count_semantics"] = unavailable(
        "Selected GitHub Actions Maven summaries are archived, but the shared typed CI-count validator currently supports only Jenkins testReport; no legacy executed_count is used as a new assessed denominator.",
        sources=official_refs, selected_url=selection["official_ci_url"], selected_cell=selection["ci_identity"]["job_name"])
    total = re.findall(r"^\[(?:INFO|WARNING)\] Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)\s*$", ci_text, re.M)
    assert len(total) == 1
    reported, failed, errors, skipped = map(int, total[0])
    recorded = selection["reported_test_results_by_invocation"][0]["counts"]
    assert (reported, failed, errors, skipped) == tuple(recorded[k] for k in ("reported_total", "failures", "errors", "skipped"))
    target = TargetRecord.model_validate({
        "schema_version": 2, "repo": task["repo"], "sha": task["sha"],
        "harvested_at": datetime.now(timezone.utc).isoformat(), "matched_cell": selection["ci_identity"]["job_name"],
        "cells": [{"cell_id": selection["ci_identity"]["job_name"], "build": "ok", "executed_count": reported,
                   "red_count": failed + errors, "skipped": skipped, "modules": ["."], "modules_basis": "declared",
                   "command": shlex.join(step["argv"]), "grade": "B", "evidence_refs": [selection["official_ci_url"]]}],
        "notes": ["New exact-selected-cell target prepared offline from archived official bytes; harvested_at records this offline target construction, not a new CI download. Historical targets are unchanged.",
                  "Legacy executed_count is the native reported total including skipped records. Assessed is reported minus skipped; no identity equivalence is claimed.",
                  "Grade B retained because no JUnit XML or complete Jenkins API case pool is present. Source-bound native summary counts are disclosed without promoting them to grade A.",
                  "New common requirements evaluator CI comparison scores remain unavailable."]})
    write(output / "ci-target.json", target.model_dump(mode="json"))
    write(output / "requirements.json", spec)
    load_requirements(output / "requirements.json", task)
    validate_ci_count_metadata(spec, base=output, required=True)
    check_refs(spec, output)
    assert (output / "task.json").read_bytes() == task_bytes
    result = {"project_id": project, "status": "definition_ready_for_reference_replay", "task_sha256": task_digest(task),
              "requirements_sha256": canonical_digest(spec), "requirements_file_sha256": sha(output / "requirements.json"),
              "requirements": len(rows), "top_level_ci_bindings": len(top), "nested_ci_bindings": len(nested),
              "effective_pom_executions": len(declarations), "distinct_package_files": len(artifacts),
              "reported": reported, "assessed": reported-skipped, "passed": reported-skipped-failed-errors,
              "skipped": skipped, "failed": failed, "errors": errors,
              "ci_counts_status": "descriptive_native_summary_only_new_shared_score_unavailable",
              "reference_replay_performed": False, "agent_run_performed": False,
              "checks": {"all_requirement_source_refs": True, "original_task_bytes_unchanged": True,
                         "native_job_bytes_equal_original_zip_member": True, "structured_metadata_probes_checked": True}}
    write(output / "review-result.json", result)
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    for name in ("commons-csv", "commons-dbcp"):
        prepare(name)
