"""Join historical references and extension candidates without rewriting evidence.

The registry is an index, not a legacy dataset.json or an executable campaign.
References retain their source snapshot root and hash-bound JSON locators.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output/java-benchmark-integrated-20260922"
SOURCES = {
    "original": ROOT / "output/java-benchmark-20260916",
    "rescreen": ROOT / "output/java-benchmark-rescreen-20260922",
    "acquisition": ROOT / "output/java-benchmark-acquisition-20260922",
    "expansion": ROOT / "output/java-benchmark-org-expansion-20260922",
}
DOCUMENTS = {
    "original_pins": ("expansion", "BASELINE.json"),
    "old_dataset": ("original", "dataset.json"),
    "old_ledger": ("original", "candidate-ledger.json"),
    "cohort": ("rescreen", "cohort-audit/audit.json"),
    "rescreen": ("rescreen", "rescreen.json"),
    "acquisition": ("acquisition", "acquisition-summary.json"),
    "eclipse": ("expansion", "eclipse/candidates.json"),
    "fasterxml": ("expansion", "fasterxml/discovery.json"),
    "faster_tasks": ("expansion", "fasterxml/selected-ci.json"),
    "faster_evidence": ("expansion", "ci/fasterxml-evidence-audit.json"),
    "eclipse_evidence": ("expansion", "ci/eclipse-prefix-evidence.json"),
    "applicability": ("expansion", "eclipse/source-applicability/review.json"),
    "new_source": ("expansion", "source/fasterxml/report.json"),
    "entrypoints": ("expansion", "ci/eclipse-entrypoints.json"),
}


def file_ref(path):
    raw = path.read_bytes()
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def locate(document, pointer):
    return {"document": document, "json_pointer": pointer}


def resolve_locator(registry, registry_path, locator, *, cache=None):
    """Resolve a nested legacy row using its own evidence root, never the new root."""
    document = registry["documents"][locator["document"]]
    relative = PurePosixPath(document["path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Document path escapes its declared source snapshot")
    root = (Path(registry_path).parent / registry["sources"][document["source"]]["root"]).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Document symlink escapes its declared source snapshot")
    cache_key = (str(path), document["sha256"], document["bytes"])
    if cache is None or cache_key not in cache:
        if file_ref(path) != {key: document[key] for key in ("sha256", "bytes")}:
            raise ValueError("Source document hash/length changed")
        value = json.loads(path.read_bytes())
        if cache is not None:
            cache[cache_key] = value
    else:
        value = cache[cache_key]
    pointer = locator["json_pointer"]
    if pointer and not pointer.startswith("/"):
        raise ValueError("Malformed JSON pointer")
    for key in pointer.split("/")[1:]:
        key = key.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", key):
                raise ValueError("Invalid JSON array index")
            value = value[int(key)]
        else:
            value = value[key]
    return value, root


def index(rows, key):
    result = {}
    for position, row in enumerate(rows):
        identity = row[key]
        if identity in result:
            raise ValueError("Duplicate identity: " + str(identity))
        result[identity] = (position, row)
    return result


def band(size, policy):
    if size.get("status") != "java_content_verified":
        return "size_unknown"
    value = size.get("java_physical_lines")
    if type(value) is not int or value < 0:
        raise ValueError("Verified source has no valid physical line count")
    return "small" if value <= policy["q1"] else "medium" if value <= policy["q3"] else "large"


def admission_valid(decision, candidate):
    """Original reference admission; neither XML nor v2/replay is required here."""
    if decision.get("decision") != "static_qualified":
        return False
    required = ("population", "task_applicability", "official_ci_binding", "successful_required_scope",
                "complete_build_test_counts", "runtime_disclosed", "source_and_license", "selection_disclosed")
    if any(decision.get("checks", {}).get(key) != "passed" for key in required):
        raise ValueError("Static admission has missing/failed original-policy checks")
    if candidate["population_status"] != "eligible" or decision["repository_id"] != candidate["repository_id"]:
        raise ValueError("Admission repository/population mismatch")
    task = decision["reference_task"]
    if task.get("repository_id") != candidate["repository_id"] or decision.get("repo") != candidate["repo"]:
        raise ValueError("Admission nested repository identity mismatch")
    if task["repo"] != candidate["repo"] or not re.fullmatch(r"[0-9a-f]{40}", task["sha"]):
        raise ValueError("Admission source identity mismatch")
    if task["sha"] != candidate["ci_candidate"].get("sha"):
        raise ValueError("Admission differs from the reviewed candidate revision")
    if task["evidence_level"] not in {"complete_suite_counts", "complete_case_results"}:
        raise ValueError("Unsupported evidence capability")
    if any(type(task["ci"].get(key)) is not int or task["ci"][key] <= 0
           for key in ("run_id", "run_attempt", "job_id")):
        raise ValueError("CI run/attempt/job must be explicit positive identifiers")
    if any(task["ci"].get(key) != candidate["ci_candidate"].get(key) for key in ("run_id", "run_attempt", "job_id")):
        raise ValueError("Admission differs from the reviewed CI run/attempt/job")
    counts = task["tests"]
    keys = ("reported_count", "passed_count", "failed_count", "error_count", "skipped_count", "assessed_count")
    if any(type(counts.get(key)) is not int or counts[key] < 0 for key in keys):
        raise ValueError("Native outcome counts must be explicit nonnegative integers")
    if (counts["reported_count"] != sum(counts[k] for k in keys[1:5])
            or counts["assessed_count"] != counts["reported_count"] - counts["skipped_count"]):
        raise ValueError("Native outcome counts do not reconcile")
    if task["build"]["status"] != "success" or counts["failed_count"] or counts["error_count"]:
        raise ValueError("Required reference build/test outcomes were not successful")
    if not counts["assessed_count"]:
        raise ValueError("A build/test reference requires observed executed tests")
    if (decision.get("complete_case_scope_review") == "passed") != (task["evidence_level"] == "complete_case_results"):
        raise ValueError("Complete testcase capability and reviewed scope must agree")
    if not decision.get("evidence_locators"):
        raise ValueError("Admission requires inspectable evidence references")
    return True


def validate_locators(registry, registry_path, admissions):
    """Check every registry/decision locator before publishing the derived index."""
    cache, checked, raw_files = {}, 0, {}

    def visit(value):
        nonlocal checked
        if isinstance(value, dict):
            if "document" in value and "json_pointer" in value:
                resolve_locator(registry, registry_path, value, cache=cache)
                checked += 1
            else:
                for child in value.values():
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(registry)
    visit(admissions)

    def verify_files(value):
        if isinstance(value, dict):
            if all(key in value for key in ("path", "sha256", "bytes")):
                path = Path(value["path"])
                if not path.is_absolute():
                    path = Path(registry_path).parent / registry["sources"]["expansion"]["root"] / path
                path = path.resolve()
                allowed = [(Path(registry_path).parent / source["root"]).resolve()
                           for source in registry["sources"].values() if source["read_only"]]
                if not any(path.is_relative_to(root) for root in allowed):
                    raise ValueError("Raw evidence path is outside the sealed sources")
                if str(path) not in raw_files:
                    raw_files[str(path)] = file_ref(path)
                measured = raw_files[str(path)]
                if measured != {key: value[key] for key in ("sha256", "bytes")}:
                    raise ValueError("Raw evidence hash/length changed: " + str(path))
            for child in value.values():
                verify_files(child)
        elif isinstance(value, list):
            for child in value:
                verify_files(child)

    for decision in admissions["decisions"]:
        if decision.get("decision") != "static_qualified":
            continue
        reviewed = [resolve_locator(registry, registry_path, loc, cache=cache)[0]
                    for loc in decision["evidence_locators"]]
        if decision["reference_task"] not in reviewed:
            raise ValueError("Admission task differs from the archived reviewed task")
        task = decision["reference_task"]
        if not any(isinstance(row, dict) and row.get("recommendation") == "static_qualified"
                   and all(row.get(key) == task[key] for key in ("repo", "repository_id", "sha"))
                   and all(row.get("ci", {}).get(key) == task["ci"][key] for key in ("run_id", "run_attempt", "job_id"))
                   for row in reviewed):
            raise ValueError("Admission lacks a matching qualified review")
        verify_files(task)
    return {"resolved_locators": checked, "verified_documents": len(cache), "verified_admission_raw_files": len(raw_files)}


def assemble(data, admissions=None):
    admissions = admissions or {"decisions": []}
    old_rows = data["old_ledger"]["rows"]
    old_projects = index(data["old_dataset"]["projects"], "repository_id")
    cohort = index(data["cohort"]["candidates"], "repository_id")
    audit = index(data["acquisition"]["projects"], "repo")
    rescreen = index(data["rescreen"]["projects"], "repo")
    policy = data["acquisition"]["size_policy"]
    projects, tasks = [], []
    for n, row in enumerate(old_rows):
        rid, repo = row["repository_id"], row["repo"]
        cn, co = cohort[rid]
        if co["repo"] != repo:
            raise ValueError("Historical repository join mismatch")
        _, updated = audit.get(repo, (None, {}))
        _, prior = rescreen.get(repo, (None, {}))
        legacy = old_projects.get(rid, (None, {}))[1]
        size = {"status": updated.get("source_after", "not_measured"),
                "sha": updated.get("sha"), "java_files": updated.get("java_files"),
                "java_physical_lines": updated.get("java_physical_lines")}
        refs = [locate("old_ledger", f"/rows/{n}"), locate("cohort", f"/candidates/{cn}")]
        if updated:
            refs += [locate("acquisition", f"/projects/{audit[repo][0]}"), locate("rescreen", f"/projects/{rescreen[repo][0]}")]
            if legacy["canonical_sha"] != updated["sha"] or legacy["canonical_task_id"] != updated["canonical_task_id"]:
                raise ValueError("Historical canonical identity changed")
        target = "historically_reviewed_applicable" if legacy else "reviewed_target_inapplicable" if row["state"] == "target_inapplicable" else "review_pending"
        projects.append({"repo": repo, "repository_id": rid, "maintainer": row["organization"],
            "family": row["organization"], "wave": "original_2026-09-16_17", "stars_at_observation": row["stars"],
            "population_status": co["population"]["status"], "activity_cutoff": co["population"]["cutoff"],
            "historical_disposition": row["state"], "historical_reference_included": bool(legacy),
            "reference_status": "historical_static_reference_with_current_audit" if legacy else "not_admitted",
            "target_applicability": target, "alternatives_exhausted": False,
            "canonical_task_id": legacy.get("canonical_task_id"),
            "ci_candidate": {"sha": legacy.get("canonical_sha"), "url": legacy.get("canonical_ci_url")},
            "declared_reference_capability": legacy.get("canonical_evidence_level"),
            "current_count_scope_audit": prior.get("ci_count_audit", "not_reviewed"),
            "complete_case_records": updated.get("case_records_after", False),
            "within_run_labels_unambiguous": updated.get("within_run_labels_unambiguous", False),
            "cross_run_identity": "not_evaluated", "partial_case_records": None,
            "source": size, "descriptive_size_band": band(size, policy),
            "build_tool": legacy.get("canonical_tool", "not_reviewed"),
            "ci_job_observed_categories": updated.get("ci_job_observed_categories", []),
            "category_scope": "CI job observations; mandatory task requirements need separate review",
            "requirements_readiness": updated.get("requirements_readiness", "not_reviewed"),
            "reference_replay": "not_verified", "campaign_ready": False,
            "development_exposure": row.get("development_exposure", "not_assessed"),
            "reasons": row.get("reasons", []), "evidence": refs})
    canonical = {p["canonical_task_id"] for p in projects if p["canonical_task_id"]}
    for n, task in enumerate(data["old_dataset"]["reference_tasks"]):
        tasks.append({"task_id": task["task_id"], "repo": task["repo"], "sha": task["sha"],
                      "canonical": task["task_id"] in canonical, "admission_basis": "historical_original_protocol",
                      "evidence_root_source": "original", "task": locate("old_dataset", f"/reference_tasks/{n}"),
                      "campaign_ready": False})
    new_evidence = {r["repo"]: r for r in data["faster_evidence"]["tasks"] + data["eclipse_evidence"]["rows"]}
    new_sources = index(data["new_source"]["rows"], "repo")
    static = index(data["applicability"]["repositories"], "repo")
    faster_ci = index(data["faster_tasks"]["tasks"], "repo")
    new_reviews = index(data.get("reference_review", {"rows": []})["rows"], "repo")
    new_rows = [("eclipse", "/repositories", n, row, "Eclipse Foundation") for n, row in enumerate(data["eclipse"]["repositories"]) if row["new_candidate"]]
    new_rows += [("fasterxml", "/rows", n, row, "FasterXML") for n, row in enumerate(data["fasterxml"]["rows"]) if row["status"] == "metadata_candidate"]
    for doc, pointer, n, row, maintainer in new_rows:
        repo = row["repo"]
        evidence = new_evidence.get(repo, {})
        _, source = new_sources.get(repo, (None, {}))
        _, applicability = static.get(repo, (None, {}))
        _, ci = faster_ci.get(repo, (None, evidence))
        review_position, review = new_reviews.get(repo, (None, {}))
        ci_sha = ci.get("sha")
        if source and source["sha"] != ci_sha:
            raise ValueError("Source size and candidate CI SHA disagree")
        scope = applicability.get("source_applicability", "review_pending")
        size = {"status": source.get("status", "not_measured"), "sha": source.get("sha"),
                **{key: source.get("scan", {}).get("measured_size", {}).get(key) for key in ("java_files", "java_physical_lines")}}
        refs = [locate(doc, f"{pointer}/{n}")]
        if source:
            refs.append(locate("new_source", f"/rows/{new_sources[repo][0]}"))
        if applicability:
            refs.append(locate("applicability", f"/repositories/{static[repo][0]}"))
        if review:
            refs.append(locate("reference_review", f"/rows/{review_position}"))
        parsed = [a["parsed_report"] for a in evidence.get("artifacts", []) if a.get("parsed_report")]
        partial = {"reported": sum(p["observed_counts"]["reported_count"] for p in parsed),
                   "scope": "observed report pool; not a whole-task denominator"} if parsed else None
        projects.append({"repo": repo, "repository_id": row["repository_id"], "maintainer": maintainer,
            "family": row.get("family", ";".join(row.get("project_ids", []))), "wave": "extension_2026-09-22",
            "stars_at_observation": row["stars"], "population_status": "eligible", "activity_cutoff": "2025-09-22",
            "historical_disposition": None, "historical_reference_included": False,
            "reference_status": "evidence_unavailable" if review.get("recommendation") == "evidence_unavailable" else "needs_review",
            "target_applicability": "reviewed_target_inapplicable" if scope == "outside_maven_gradle_product_build_scope" else "review_pending",
            "target_source_review": scope, "alternatives_exhausted": False,
            "canonical_task_id": None, "ci_candidate": {"sha": ci_sha, "url": ci.get("official_ci_url"),
                **{key: ci.get(key, ci.get("ci_identity", {}).get(key)) for key in ("run_id", "run_attempt", "job_id")},
                "selection_status": ci.get("selection_status", "not_selected"), "log_binding": evidence.get("selected_log_binding", "not_reviewed")},
            "declared_reference_capability": None, "current_count_scope_audit": "review_pending",
            "case_evidence_status": evidence.get("case_evidence_status", "not_reviewed"),
            "complete_case_records": False, "within_run_labels_unambiguous": False,
            "cross_run_identity": "not_evaluated", "partial_case_records": partial,
            "source": size, "descriptive_size_band": band(size, policy),
            "build_tool": "maven" if maintainer == "FasterXML" and ci_sha else "not_reviewed",
            "ci_job_observed_categories": [], "category_scope": "Not yet joined; no mandatory group inferred",
            "requirements_readiness": "review_required", "reference_replay": "not_verified", "campaign_ready": False,
            "development_exposure": "not_assessed_for_expansion",
            "reasons": ([review["reason"]] if review else []) + applicability.get("remaining_gaps", []), "evidence": refs})
    by_id = index(projects, "repository_id")
    index(projects, "repo")
    for n, decision in enumerate(admissions["decisions"]):
        _, candidate = by_id[decision["repository_id"]]
        if not admission_valid(decision, candidate):
            continue
        if candidate["historical_reference_included"] or candidate["canonical_task_id"]:
            raise ValueError("Admission must not silently replace an existing reference")
        task = decision["reference_task"]
        candidate.update(reference_status="static_qualified", canonical_task_id=task["task_id"],
                         declared_reference_capability=task["evidence_level"], current_count_scope_audit="confirmed",
                         target_applicability="reviewed_applicable")
        candidate["ci_job_observed_categories"] = task.get("ci_job_observed_categories", [])
        candidate["category_scope"] = "CI job observations; mandatory task requirements need separate review"
        # Complete case evidence requires its own explicit scope check; counts alone never upgrade it.
        candidate["complete_case_records"] = decision.get("complete_case_scope_review") == "passed"
        candidate["evidence"].append(locate("admissions", f"/decisions/{n}"))
        candidate["reasons"] = [decision["reason"]]
        tasks.append({"task_id": task["task_id"], "repo": task["repo"], "sha": task["sha"], "canonical": True,
                      "admission_basis": "original_protocol_applied_to_extension", "evidence_root_source": "expansion",
                      "task": locate("admissions", f"/decisions/{n}/reference_task"), "campaign_ready": False})
    index(tasks, "task_id")
    for project in projects:
        if project["canonical_task_id"]:
            task = next((t for t in tasks if t["task_id"] == project["canonical_task_id"]), None)
            if not task or task["repo"] != project["repo"] or task["sha"] != project["ci_candidate"]["sha"]:
                raise ValueError("Canonical project/task join mismatch")
    projects.sort(key=lambda r: r["repo"].lower())
    return {"schema": "sag-java-benchmark-registry-v2", "status": "versioned_registry_not_a_campaign_manifest",
        "policy": {"static_reference": "Original 2026-09-16 protocol, uniformly applied to all frames",
                   "count_only_allowed": True, "case_identity_required_for_static_reference": False,
                   "requirements_v2_and_reference_replay_are_campaign_gates": True,
                   "missing_data_is_not_zero": True, "size": {**policy, "scope": "Same frozen descriptive thresholds for all measured versions; not admission or a final-cohort quantile estimate"}},
        "summary": {"registered_candidate_records": len(projects), "original_candidate_records": len(old_rows),
            "extension_population_candidates": len(new_rows), "historical_reference_projects": len(old_projects),
            "new_static_reference_projects": sum(p["reference_status"] == "static_qualified" for p in projects),
            "reference_projects": sum(p["canonical_task_id"] is not None for p in projects), "reference_tasks": len(tasks),
            "canonical_complete_case_record_projects": sum(p["complete_case_records"] and bool(p["canonical_task_id"]) for p in projects),
            "canonical_within_run_unambiguous_projects": sum(p["within_run_labels_unambiguous"] and bool(p["canonical_task_id"]) for p in projects),
            "cross_run_identity": "not_evaluated", "campaign_ready_projects": 0,
            "wave_counts": dict(Counter(p["wave"] for p in projects)),
            "denominator_warning": "Merged registration counts are not a single-wave screening or experimental denominator"},
        "candidates": projects, "reference_tasks": tasks,
        "full_expansion_discovery": {"eclipse": locate("eclipse", ""), "fasterxml": locate("fasterxml", ""),
                                     "scope": "All observed exclusions/unknowns remain in their full source frames; the 73 are downstream population candidates"}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    output = args.output.resolve()
    if any(output == root or root in output.parents for root in SOURCES.values()):
        raise ValueError("Cannot write inside a sealed input snapshot")
    output.mkdir(parents=True, exist_ok=True)
    roots = dict(SOURCES, integration=output)
    specs = dict(DOCUMENTS)
    if (output / "review/fasterxml-reference-review.json").exists():
        specs["reference_review"] = ("integration", "review/fasterxml-reference-review.json")
        specs["reviewed_reference_tasks"] = ("integration", "review/reference_tasks.json")
    admissions_path = output / "admission-decisions.json"
    if not admissions_path.exists():
        write(admissions_path, {"schema": "registry-static-admission-decisions-v1", "decisions": []})
    specs["admissions"] = ("integration", "admission-decisions.json")
    data, documents = {}, {}
    # Verify every consumed sealed document before deriving a new registration.
    manifests = {key: {r["path"]: r for r in json.loads((root / "checksums.json").read_bytes())["files"]}
                 for key, root in SOURCES.items() if (root / "checksums.json").exists()}
    baseline_path = SOURCES["expansion"] / "BASELINE.json"
    expected_baseline = manifests["expansion"]["BASELINE.json"]
    if file_ref(baseline_path) != {key: expected_baseline[key] for key in ("sha256", "bytes")}:
        raise ValueError("Original input pins differ from the sealed expansion baseline")
    baseline = json.loads(baseline_path.read_bytes())
    original_pins = {Path(r["path"]).name: r for r in baseline["sources"] if Path(r["path"]).parent == SOURCES["original"]}
    for key, (source, name) in specs.items():
        path = roots[source] / name
        measured = file_ref(path)
        expected = original_pins.get(name) if source == "original" else manifests.get(source, {}).get(name)
        if source != "integration" and (not expected or measured != {k: expected[k] for k in ("sha256", "bytes")}):
            raise ValueError("Input is not bound to its sealed manifest: " + str(path))
        data[key] = json.loads(path.read_bytes())
        documents[key] = {"source": source, "path": name, **measured}
    registry = assemble(data, data["admissions"])
    registry["sources"] = {key: {"root": os.path.relpath(root, output), "read_only": key != "integration"} for key, root in roots.items()}
    registry["documents"] = documents
    registry["assembly_script"] = {"path": os.path.relpath(Path(__file__).resolve(), output), **file_ref(Path(__file__))}
    registry["locator_validation"] = validate_locators(registry, output / "registry.json", data["admissions"])
    write(output / "registry.json", registry)
    with (output / "PROJECTS.csv").open("w", newline="") as stream:
        fields = ["repo", "repository_id", "maintainer", "family", "wave", "stars_at_observation", "population_status",
                  "reference_status", "historical_disposition", "target_applicability", "canonical_task_id", "build_tool",
                  "complete_case_records", "within_run_labels_unambiguous", "requirements_readiness", "reference_replay",
                  "campaign_ready", "descriptive_size_band", "development_exposure"]
        writer = csv.DictWriter(stream, fieldnames=fields + ["source_sha", "source_status", "java_files", "java_physical_lines"])
        writer.writeheader()
        for p in registry["candidates"]:
            writer.writerow({**{key: p.get(key) for key in fields}, "source_sha": p["source"].get("sha"),
                "source_status": p["source"]["status"], **{key: p["source"].get(key) for key in ("java_files", "java_physical_lines")}})
    s = registry["summary"]
    policy = registry["policy"]["size"]
    new_references = []
    for decision in data["admissions"]["decisions"]:
        if decision.get("decision") != "static_qualified":
            continue
        task = decision["reference_task"]
        new_references.append(
            f"**New reference: {task['repo']}.** Revision `{task['sha']}`; official job `{task['ci']['job_id']}` "
            f"(run `{task['ci']['run_id']}`, attempt {task['ci']['run_attempt']}). Native CI outcomes: "
            f"{task['tests']['assessed_count']} executed, {task['tests']['passed_count']} passed, "
            f"{task['tests']['failed_count']} failed, {task['tests']['error_count']} errors and {task['tests']['skipped_count']} skipped. "
            f"Evidence capability: `{task['evidence_level']}`. {decision['reason']} "
            "See the admission decision for exact commands, observed versus configured runtime, artifact evidence, "
            "source archive and limitations. This is an official-CI reference, not a SAG replay result.")
    new_reference_text = "\n\n".join(new_references) or "No new static reference has been admitted."
    reference_sizes = Counter(p["descriptive_size_band"] for p in registry["candidates"] if p["canonical_task_id"])
    text = f"""# SAG Java dataset: integrated registry, 2026-09-22

The extension is part of the dataset registry under the original admission policy. It is not an automatic addition to the runnable benchmark.

| Quantity | Count |
|---|---:|
| Registered candidate records | {s['registered_candidate_records']} |
| Original candidate records | {s['original_candidate_records']} |
| New population candidates | {s['extension_population_candidates']} |
| Historical reference projects | {s['historical_reference_projects']} |
| Newly reviewed static references | {s['new_static_reference_projects']} |
| Reference projects / nested tasks | {s['reference_projects']} / {s['reference_tasks']} |
| Canonical references with complete case records | {s['canonical_complete_case_record_projects']} |
| Canonical references with unambiguous labels within the archived run | {s['canonical_within_run_unambiguous_projects']} |
| Campaign-ready projects | {s['campaign_ready_projects']} |

The merged record count is not a same-stage, same-date screening denominator: the original 315 include historical exclusions; the new 73 passed the expansion population screen. Full expansion-frame exclusions and unknowns remain linked, not discarded. Metadata observations retain their original September 16–17 or September 22 window.

**One policy for old and new projects.** Complete attributable native build/test counts can support static reference admission without testcase identities. Requirements-v2, a runnable pinned checkout, reference replay and the runner protocol are separate campaign gates for every project. Historical references retain their old decisions alongside current source/count/requirement gaps; they are not silently recertified. Size uses the same frozen descriptive Q1/Q3 ({policy['q1']}/{policy['q3']}) for all verified source versions, not a newly estimated cohort or an admission limit.

Reference-project size labels: {reference_sizes['small']} small, {reference_sizes['medium']} medium, {reference_sizes['large']} large, {reference_sizes['size_unknown']} unknown. Raw Java physical-line counts and pinned revisions are retained in the project table. These labels describe source size, not runtime cost or an OOM prediction. Observed build categories remain separate from reviewed mandatory requirements; an empty category list with a pending review does not mean that no requirements apply.

CDT's 4,719 newly archived records remain an observed report pool while full task and environment applicability are reviewed. Adoptium JDK's reviewed default product build is outside the Maven/Gradle target scope; it is not population-excluded and alternatives are not claimed exhausted. Cross-run testcase identity remains unverified.

{new_reference_text}

[Registry](registry.json) · [Candidate table](PROJECTS.csv) · [New admission decisions](admission-decisions.json) · [Validation](validation.json) · [Usage and consistent rules](../../docs/benchmark-dataset.md)

This file and registry are rebuilt offline by `scripts/assemble_benchmark_registry.py`. A locator resolves through its named source snapshot and verifies its document hash/length; legacy evidence paths remain relative to the original root. Do not pass registry.json to legacy dataset.json consumers or campaign runners. No source build, workflow, model call, or legacy snapshot mutation occurs during integration.
"""
    (output / "DATASET_CARD.md").write_text(text)
    print(json.dumps(s, ensure_ascii=False))


if __name__ == "__main__":
    main()
