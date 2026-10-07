"""Join offline rescreen audits without promoting candidates to executable tasks."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import statistics


def load(path):
    return json.loads(Path(path).read_text())


def file_ref(path, base):
    path = Path(path).resolve(); raw = path.read_bytes()
    return {"path": str(path.relative_to(Path(base).resolve())), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def task_index(rows, expected):
    ids = [r["task_id"] for r in rows]
    if len(ids) != len(set(ids)) or set(ids) != set(expected):
        raise ValueError("Audit must cover the same task IDs exactly once")
    result = {r["task_id"]: r for r in rows}
    for tid, task in expected.items():
        if any(result[tid].get(k) != task.get(k) for k in ("repo", "sha")):
            raise ValueError("Audit task revision/repository mismatch: " + tid)
    return result


def size_strata(canonical):
    values = [r["measured_size"]["java_physical_lines"] for r in canonical
              if r["source_completeness"] == "java_content_verified"]
    # Two is the minimum number required to define empirical interpolation here;
    # it is not a project inclusion quota or engineering size threshold.
    if len(values) < 2:
        return {"status": "unavailable", "verified_projects": len(values), "q1": None, "q3": None}
    q1, _, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return {"status": "descriptive_archived_pool", "verified_projects": len(values), "q1": q1, "q3": q3,
            "measurement": "whole archived repository Java physical lines including tests/examples/fixtures",
            "method": "statistics.quantiles(n=4, method='inclusive'); ties stay together",
            "rules": {"small": "PLOC <= Q1", "medium": "Q1 < PLOC <= Q3", "large": "PLOC > Q3",
                      "size_unknown": "Java content not verified against exact commit"},
            "is_admission_threshold": False, "is_runtime_budget": False}


def stratum(source, thresholds):
    if source["source_completeness"] != "java_content_verified" or thresholds["q1"] is None:
        return "size_unknown"
    value = source["measured_size"]["java_physical_lines"]
    return "small" if value <= thresholds["q1"] else "medium" if value <= thresholds["q3"] else "large"


def observed_kinds(task):
    return sorted({r["kind"] for r in task["labels"] if r["status"] == "observed"})


def merge(source, audits):
    old = load(source / "dataset.json")
    expected = {r["task_id"]: r for r in old["reference_tasks"]}
    if len(expected) != len(old["reference_tasks"]): raise ValueError("Duplicate input task")
    cohort = load(audits / "cohort-audit/audit.json")
    loaded = {name: load(audits / name / "audit.json") for name in
              ("source-audit", "requirement-audit", "test-evidence-audit")}
    wanted = hashlib.sha256((source / "dataset.json").read_bytes()).hexdigest()
    for name, data in loaded.items():
        binding = data.get("dataset_ref", data.get("source_dataset", {}))
        if binding.get("sha256") != wanted:
            raise ValueError("Audit used another dataset: " + name)
    if next(r for r in cohort["sources"] if r["path"] == "dataset.json")["sha256"] != wanted:
        raise ValueError("Cohort audit used another dataset")
    indices = {name: task_index(data["tasks"], expected) for name, data in loaded.items()}
    source_rows = indices["source-audit"]
    requirements = indices["requirement-audit"]
    tests = indices["test-evidence-audit"]
    canonical_ids = {p["canonical_task_id"] for p in old["projects"]}
    thresholds = size_strata([source_rows[tid] for tid in canonical_ids])
    population = {r["repo"]: r for r in cohort["candidates"]}
    projects = []
    for previous in old["projects"]:
        tid = previous["canonical_task_id"]
        src, req, test = source_rows[tid], requirements[tid], tests[tid]
        ia = test.get("identity_audit", {})
        projects.append({"repo": previous["repo"], "repository_id": previous["repository_id"],
                         "organization": previous["organization"], "canonical_task_id": tid,
                         "sha": expected[tid]["sha"], "official_ci_url": expected[tid]["official_ci_url"],
                         "tool": expected[tid]["tool"], "archived_stars": previous["stars"],
                         "population_status": population[previous["repo"]]["population"]["status"],
                         "development_exposure": previous["development_exposure"],
                         "previous_state": previous["state"], "state": "reference_candidate_under_v2_review",
                         "source_completeness": src["source_completeness"], "source_size": src.get("measured_size"),
                         "checkout_collection_gaps": src.get("collection_gaps", []) + src.get("issues", []),
                         "size_stratum": stratum(src, thresholds), "ci_job_observed_categories": observed_kinds(req),
                         "requirement_labels": req["labels"], "build_endpoint": req["build_endpoint"],
                         "requirements_readiness": req["requirements_readiness"],
                         "ci_count_audit": test.get("count_audit", {}).get("status", "unavailable"),
                         "complete_case_records": ia.get("complete_case_records_available", False),
                         "within_run_labels_unambiguous": ia.get("unambiguous_observed_identity_supported", False),
                         "identity_collision_keys": ia.get("collision_key_count"),
                         "cross_agent_identity_equivalence": "not_evaluated",
                         "reference_replay_verified": False, "campaign_ready": False})
    all_tasks = [{"task_id": tid, "repo": task["repo"], "sha": task["sha"], "canonical": tid in canonical_ids,
                  "source_audit": source_rows[tid], "requirement_audit": requirements[tid],
                  "test_evidence_audit": tests[tid]} for tid, task in expected.items()]
    queue = []
    for row in all_tasks:
        reasons = []
        src, req, test = row["source_audit"], row["requirement_audit"], row["test_evidence_audit"]
        if src["source_completeness"] != "java_content_verified":
            reasons.append({"kind": "source_commit_closure", "basis": src.get("collection_gaps", []) + src.get("issues", []),
                            "next_action": "Acquire/reconcile exact-commit Git tree and missing source bytes; do not replace CI revision."})
        elif src.get("collection_gaps") or src.get("issues"):
            reasons.append({"kind": "checkout_scope_review", "basis": src.get("collection_gaps", []) + src.get("issues", []),
                            "next_action": "Resolve non-Java blob differences, omitted paths or pinned submodule content against task scope; Java verification alone is not complete checkout verification."})
        if req["requirements_readiness"] != "complete":
            reasons.append({"kind": "requirements_freeze", "basis": req.get("unknowns", []),
                            "next_action": "Resolve effective command/plan, artifact roles and test producers, then import a reviewed requirements definition."})
        if test.get("count_audit", {}).get("status") != "confirmed":
            reasons.append({"kind": "native_count_revalidation", "basis": test.get("count_audit", {}).get("gaps", []),
                            "next_action": "Reparse complete selected native invocation/report pools; inventory arithmetic alone is insufficient."})
        ia = test.get("identity_audit", {})
        if not ia.get("complete_case_records_available"):
            reasons.append({"kind": "official_case_record_recovery", "basis": ia.get("gaps", []),
                            "next_action": "Seek official complete report/XML/scan for the frozen task; preserve count-only status until source and scope reconcile."})
        elif not ia.get("unambiguous_observed_identity_supported"):
            reasons.append({"kind": "case_namespace_disambiguation", "basis": {"collision_keys": ia.get("collision_key_count"), "duplicate_excess": ia.get("duplicate_excess")},
                            "next_action": "Recover stable producer/module/invocation context; never use an archive ordinal or silently remove duplicates."})
        if ia.get("complete_case_records_available"):
            reasons.append({"kind": "cross_run_identity_mapping", "basis": "Not evaluated; within-run uniqueness is insufficient",
                            "next_action": "Freeze producer/module/step namespace mapping and validate against reference replay before case-level agent comparison."})
        queue.append({"task_id": row["task_id"], "repo": row["repo"], "sha": row["sha"],
                      "canonical": row["canonical"], "items": reasons,
                      "subsequent_gate": "reference replay and complete campaign records; no model launch from this queue"})
    def project_capability(field):
        return len({r["repo"] for r in tests.values() if r.get("identity_audit", {}).get(field)})
    summary = {"candidates": len(cohort["candidates"]), "reference_projects": len(projects), "reference_tasks": len(all_tasks),
               "population_statuses": cohort["summary"]["population_statuses"],
               "source_statuses_canonical": dict(Counter(p["source_completeness"] for p in projects)),
               "size_strata": dict(Counter(p["size_stratum"] for p in projects)),
               "ci_job_category_project_counts": dict(Counter(k for p in projects for k in p["ci_job_observed_categories"])),
               "requirements_readiness_canonical": dict(Counter(p["requirements_readiness"] for p in projects)),
               "requirements_readiness_all_tasks": dict(Counter(r["requirements_readiness"] for r in requirements.values())),
               "canonical_case_records_complete": sum(p["complete_case_records"] for p in projects),
               "canonical_labels_unambiguous": sum(p["within_run_labels_unambiguous"] for p in projects),
               "projects_with_any_complete_case_reference": project_capability("complete_case_records_available"),
               "projects_with_any_unambiguous_case_reference": project_capability("unambiguous_observed_identity_supported"),
               "canonical_count_audit": dict(Counter(p["ci_count_audit"] for p in projects)),
               "task_collection_gaps": dict(Counter(i["kind"] for r in queue for i in r["items"])),
               "campaign_ready_projects": 0, "new_model_calls": 0, "new_builds": 0, "new_network_collection": False}
    result = {"schema": "sag-benchmark-rescreen-v1", "status": "offline_review_completed_with_explicit_gaps_not_final_dataset_admission",
              "assembly_script": file_ref(Path(__file__).resolve(), Path(__file__).resolve().parents[1]),
              "observation": cohort["observation"], "source_root": str(source),
              "input_audits": [file_ref(audits / n / "audit.json", audits) for n in ["cohort-audit", *loaded]],
              "summary": summary, "size_thresholds": thresholds, "cohort_provenance_issues": cohort["global_issues"],
              "canonical_selection": "Unchanged from archived dataset; no selection by SAG outcome or testcase availability",
              "projects": projects, "tasks": all_tasks}
    return result, {"schema": "sag-evidence-acquisition-queue-v1", "source_dataset_sha256": wanted,
                    "ordering": "All existing references inventoried; dependency categories, not outcome-based priority scores",
                    "tasks": queue, "other_candidates": [r for r in cohort["candidates"] if not r["reference_candidates"]]}


def write_outputs(result, queue, output):
    output.mkdir(parents=True, exist_ok=True)
    for name, data in [("rescreen.json", result), ("COLLECTION_QUEUE.json", queue)]:
        (output / name).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    fields = ["repo", "organization", "canonical_task_id", "sha", "official_ci_url", "tool", "archived_stars",
              "source_completeness", "java_files", "java_physical_lines", "size_stratum", "ci_job_observed_categories",
              "checkout_collection_gaps",
              "requirements_readiness", "ci_count_audit", "complete_case_records", "within_run_labels_unambiguous",
              "development_exposure", "campaign_ready"]
    with (output / "projects.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader()
        for p in result["projects"]:
            row = {k: p.get(k) for k in fields}
            row.update({k: (p.get("source_size") or {}).get(k) for k in ("java_files", "java_physical_lines")})
            row["ci_job_observed_categories"] = ";".join(p["ci_job_observed_categories"])
            row["checkout_collection_gaps"] = ";".join(p["checkout_collection_gaps"])
            writer.writerow(row)
    lines = ["# Canonical reference candidate inventory", "",
             "This preserves the archived canonical selections. Labels describe native invocations in the selected CI job. Membership in the frozen command scope remains to be reviewed; they are not mandatory task groups or successful SAG execution.", "",
             "| Repository | Size stratum | Java PLOC in archive | CI-job observed categories | Full requirements | Case rows complete | Labels collision-free within CI run |",
             "|---|---|---:|---|---|---|---|"]
    for p in result["projects"]:
        lines.append(f"| {p['repo']} | {p['size_stratum']} | {(p.get('source_size') or {}).get('java_physical_lines','unknown')} | {', '.join(p['ci_job_observed_categories'])} | {p['requirements_readiness']} | {p['complete_case_records']} | {p['within_run_labels_unambiguous']} |")
    (output / "PROJECTS.md").write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--audits", type=Path, required=True)
    args = parser.parse_args()
    if args.audits.resolve().is_relative_to(args.source.resolve()):
        raise ValueError("Do not overwrite the archived source dataset")
    result, queue = merge(args.source.resolve(), args.audits.resolve())
    write_outputs(result, queue, args.audits)
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
