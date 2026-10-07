"""Re-audit an archived candidate frame without changing or fetching its data.

This checks source provenance and joins, not the correctness of every historical
task review. New requirement/source/test audits are joined separately by task ID.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


def load(path):
    return json.loads(Path(path).read_text())


def ref(path, root):
    path = Path(path).resolve()
    raw = path.read_bytes()
    return {"path": str(path.relative_to(Path(root).resolve())),
            "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def unique_index(rows, key):
    counts = Counter(row.get(key) for row in rows)
    problems = [f"duplicate_or_missing_{key}:{k}" for k, n in counts.items() if k is None or n != 1]
    return {r[key]: r for r in rows if counts[r.get(key)] == 1 and r.get(key) is not None}, problems


def response(base, relative, evidence, *, expected_path=None, expected_query=None):
    path = (base / relative).resolve()
    if not path.is_relative_to(base.resolve()):
        raise ValueError("Evidence path escapes the archive")
    meta_path = path.with_suffix(path.suffix + ".meta.json")
    raw = path.read_bytes()
    meta = load(meta_path)
    if (meta.get("returncode") != 0 or meta.get("sha256") != hashlib.sha256(raw).hexdigest()
            or meta.get("bytes") != len(raw)):
        raise ValueError("Raw response bytes differ from its capture receipt")
    url = urlparse(meta.get("url", ""))
    if url.scheme != "https" or url.netloc != "api.github.com":
        raise ValueError("Unexpected API provenance")
    if expected_path is not None and unquote(url.path) != expected_path:
        raise ValueError("Raw response belongs to a different repository or revision")
    if expected_query is not None:
        query = parse_qs(url.query)
        if any(query.get(k) != [v] for k, v in expected_query.items()):
            raise ValueError("Raw response query differs from the declared scope")
    evidence.extend([ref(path, base), ref(meta_path, base)])
    return json.loads(raw), meta


def population_check(repository, screen, *, cutoff, head, history=None, observed_at=None):
    """Only repository-wide facts can reject the population candidate."""
    issues, rejected = [], []
    if repository.get("id") != screen.get("repository_id"):
        issues.append("repository_identity_mismatch")
    if repository.get("full_name") != screen.get("repo"):
        issues.append("repository_name_mismatch")
    stars = repository.get("stargazers_count")
    if type(stars) is not int:
        issues.append("stars_unknown")
    elif stars <= 200:  # User-defined strict threshold, not a tuning parameter.
        rejected.append("stars_not_greater_than_200")
    for key in ("private", "fork", "archived"):
        if type(repository.get(key)) is not bool:
            issues.append(key + "_unknown")
        elif repository[key]:
            rejected.append(key)
    if repository.get("language") is None:
        issues.append("primary_language_unknown")
    elif repository.get("language") != "Java":
        rejected.append("primary_language_not_java")
    if (head.get("sha") != screen.get("default_branch_sha")
            or repository.get("default_branch") != screen.get("default_branch")):
        issues.append("default_branch_identity_mismatch")
    date = head.get("commit", {}).get("committer", {}).get("date")
    try:
        observed = datetime.fromisoformat(date.replace("Z", "+00:00"))
        threshold = datetime.fromisoformat(cutoff + "T00:00:00+00:00")
        upper = datetime.fromisoformat(observed_at.replace("Z", "+00:00")) if observed_at else None
        if upper and observed > upper:
            issues.append("activity_timestamp_after_capture")
        if date != screen.get("default_branch_commit_date"):
            issues.append("activity_summary_differs_from_raw_head")
        if observed < threshold:
            if history is None:
                issues.append("recent_default_branch_history_unavailable")
            elif not isinstance(history, list):
                issues.append("recent_history_response_not_a_list")
            elif not history:
                rejected.append("default_branch_inactive")
            elif not any(threshold <= timestamp and (upper is None or timestamp <= upper)
                         for timestamp in (datetime.fromisoformat(c["commit"]["committer"]["date"].replace("Z", "+00:00"))
                                           for c in history)):
                issues.append("recent_history_does_not_establish_activity")
    except (TypeError, ValueError, AttributeError, KeyError):
        issues.append("activity_timestamp_unavailable")
    return {"status": "unavailable" if issues else "ineligible" if rejected else "eligible",
            "exclusion_reasons": rejected, "issues": issues, "cutoff": cutoff,
            "activity_basis": "archived_default_branch_commit_history",
            "stars_observed": stars}


def audit(base):
    base = Path(base).resolve()
    dataset, discovery, ledger = (load(base / f) for f in
                                  ("dataset.json", "discovery.json", "candidate-ledger.json"))
    discovered, errors = unique_index(discovery["repositories"], "full_name")
    candidates, e = unique_index(ledger["rows"], "repo"); errors += e
    projects, e = unique_index(dataset["projects"], "repo"); errors += e
    tasks, e = unique_index(dataset["reference_tasks"], "task_id"); errors += e
    _, e = unique_index(ledger["rows"], "repository_id"); errors += e
    if set(discovered) != set(candidates):
        errors.append("candidate_discovery_membership_mismatch")
    if set(projects) != {r["repo"] for r in ledger["rows"] if r["state"] == "static_qualified"}:
        errors.append("static_qualified_project_membership_mismatch")
    frame = []
    page_repo_ids = []
    previous_inventory = load(base / "discovery-integrity.json")
    previous_pages = {r["file"]: r for r in previous_inventory["raw_pages"]}
    for query in discovery["queries"]:
        evidence, ids, problems = [], [], []
        for page in range(1, query["pages"] + 1):
            relative = f'raw/github/{query["org"]}-search-{page}.json'
            try:
                path = base / relative
                if not path.with_suffix(path.suffix + ".meta.json").exists():
                    # Preserve observed inventory even when transport provenance
                    # is missing. An earlier local hash is corroboration, not a
                    # replacement HTTP receipt or proof of the query URL.
                    actual = ref(path, base)
                    prior = previous_pages.get(relative, {})
                    if any(actual[k] != prior.get(k) for k in ("sha256", "bytes")):
                        raise ValueError("Unreceipted search page differs from earlier inventory")
                    evidence.extend([actual, ref(base / "discovery-integrity.json", base)])
                    payload = load(path)
                    problems.append(f"capture_receipt_missing:{relative}; bytes_match_earlier_inventory_only")
                else:
                    payload, _ = response(base, relative, evidence,
                                          expected_path="/search/repositories",
                                          expected_query={"q": query["query"], "page": str(page)})
                if payload.get("incomplete_results") is not False or payload.get("total_count") != query["total_count"]:
                    problems.append("search_incomplete_or_total_changed")
                ids.extend(r["id"] for r in payload["items"])
                for item in payload["items"]:
                    assembled = discovered.get(item.get("full_name"), {})
                    if any(assembled.get(k) != item.get(k) for k in
                           ("id", "full_name", "stargazers_count", "language", "private", "fork", "archived", "default_branch")):
                        problems.append("raw_repository_metadata_differs:" + str(item.get("full_name")))
            except (ValueError, KeyError, OSError) as exc:
                problems.append(str(exc))
        if len(ids) != len(set(ids)) or len(set(ids)) != query["total_count"]:
            problems.append("pagination_not_complete_unique")
        page_repo_ids.extend(ids)
        frame.append({**query, "status": "verified_archived_pages" if not problems else "unavailable",
                      "unique_repositories": len(set(ids)), "issues": problems, "sources": evidence})
        errors.extend(query["org"] + ":" + p for p in problems)
    if set(page_repo_ids) != {r["id"] for r in discovery["repositories"]}:
        errors.append("raw_search_discovery_ids_differ")
    rows = []
    for old in ledger["rows"]:
        name = old["repo"]
        evidence, problems, reviewed = [], [], []
        repository = discovered.get(name, {})
        population = {"status": "unavailable", "issues": ["source_not_checked"], "exclusion_reasons": []}
        try:
            screen_path = base / old["discovery_screening"]
            screen = load(screen_path); evidence.append(ref(screen_path, base))
            branch = repository["default_branch"]
            head, head_meta = response(base, f"raw/repos/{name}/head.json", evidence,
                               expected_path=f"/repos/{name}/commits/{branch}")
            history = None
            if head["commit"]["committer"]["date"][:10] < discovery["cutoff"]:
                history, _ = response(base, f"raw/repos/{name}/recent-default-branch-commits.json", evidence,
                                      expected_path=f"/repos/{name}/commits",
                                      expected_query={"sha": branch, "since": discovery["cutoff"] + "T00:00:00Z"})
            population = population_check(repository, screen, cutoff=discovery["cutoff"], head=head,
                                          history=history, observed_at=head_meta.get("fetched_at"))
        except (ValueError, KeyError, OSError) as exc:
            population["issues"] = [str(exc)]
        for review in old["reviews"]:
            try:
                source = load(base / review["file"])
                values = source if isinstance(source, list) else source.get("rows", source.get("decisions", source.get("repo_decisions")))
                record = values[review["row_index"]]
                if record["repo"] != name:
                    raise ValueError("Review row belongs to another repository")
                reviewed.append({"source": ref(base / review["file"], base), "row_index": review["row_index"],
                                 "historical_status": record.get("status", record.get("decision")),
                                 "reason": review["reason"], "scope": review["decision_scope"],
                                 "alternatives_exhausted": review["alternatives_exhausted"],
                                 "verification": "source_join_only_not_a_new_dependency_review"})
            except (TypeError, ValueError, IndexError, KeyError, OSError) as exc:
                problems.append("review_source:" + str(exc))
        qids = old["qualified_task_ids"]
        for tid in qids:
            if tid not in tasks or tasks[tid]["repo"] != name:
                problems.append("qualified_task_join_mismatch:" + tid)
        if old["state"] == "population_excluded" and population["status"] != "ineligible":
                problems.append("historical_population_exclusion_not_reconfirmed")
        if qids and old["state"] != "static_qualified":
            problems.append("qualified_reference_precedence_conflict")
        project = projects.get(name)
        if project:
            canonical = tasks.get(project.get("canonical_task_id"), {})
            if (canonical.get("repo") != name or canonical.get("sha") != project.get("canonical_sha")
                    or canonical.get("official_ci_url") != project.get("canonical_ci_url")
                    or canonical.get("task_id") not in qids):
                problems.append("canonical_identity_join_mismatch")
        rows.append({"repo": name, "repository_id": old["repository_id"], "organization": old["organization"],
                     "previous_state": old["state"], "population": population,
                     "reference_candidates": qids, "canonical_task_id": project.get("canonical_task_id") if project else None,
                     "development_exposure": old["development_exposure"],
                     "retained_as_reference_candidate": bool(qids) and population["status"] == "eligible" and not problems,
                     "full_requirements_reviewed": False, "campaign_ready": False,
                     "historical_target_reviews": reviewed, "source_refs": evidence, "issues": problems})
    task_checks = []
    for task in dataset["reference_tasks"]:
        problems = []
        if task["repo"] not in candidates or task["task_id"] not in candidates[task["repo"]]["qualified_task_ids"]:
            problems.append("task_not_declared_in_candidate")
        if len(task["sha"]) != 40 or any(c not in "0123456789abcdef" for c in task["sha"]):
            problems.append("revision_not_immutable_sha")
        if task["tool"] not in {"maven", "gradle"}:
            problems.append("unsupported_tool")
        try:
            completed = datetime.fromisoformat(task["completed_at"].replace("Z", "+00:00"))
            if completed > datetime.fromisoformat(discovery["collected_at"]):
                problems.append("ci_after_archived_observation")
        except (ValueError, KeyError):
            problems.append("ci_completion_timestamp_unknown")
        task_checks.append({"task_id": task["task_id"], "repo": task["repo"], "sha": task["sha"],
                            "status": "consistent" if not problems else "conflict", "issues": problems})
    summary = {"candidate_repositories": len(rows), "historical_reference_projects": len(projects),
               "historical_reference_tasks": len(tasks),
               "previous_states": dict(Counter(r["previous_state"] for r in rows)),
               "population_statuses": dict(Counter(r["population"]["status"] for r in rows)),
               "reference_candidates_retained": sum(r["retained_as_reference_candidate"] for r in rows),
               "candidate_join_issues": sum(bool(r["issues"]) for r in rows),
               "task_join_issues": sum(bool(r["issues"]) for r in task_checks)}
    return {"schema": "sag-archived-cohort-rescreen-v1", "audited_at": datetime.now(timezone.utc).isoformat(),
            "audit_script": ref(Path(__file__).resolve(), Path(__file__).resolve().parents[1]),
            "source_root": str(base), "observation": {"collected_at": discovery["collected_at"], "activity_cutoff": discovery["cutoff"],
             "refreshed_live": False, "scope": "Reassessment of the archived frame, not current GitHub metadata"},
            "sources": [ref(base / f, base) for f in ["dataset.json", "candidate-ledger.json", "discovery.json", "PROTOCOL.md"]],
            "summary": summary, "global_issues": errors, "discovery_frame": frame, "candidates": rows,
            "task_join_checks": task_checks,
            "limitations": ["Dependency, Android and mixed-language reviews here are source joins, not independently re-proven exclusions.",
                            "Source completeness, requirement annotation and test identity require the separate task-level audits.",
                            "No new network collection, builds, workflow runs or model calls."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(args.source.resolve()):
        raise ValueError("Do not rewrite the source snapshot")
    result = audit(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"summary": result["summary"], "global_issues": result["global_issues"]}, indent=2))


if __name__ == "__main__":
    main()
