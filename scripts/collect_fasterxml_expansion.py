#!/usr/bin/env python3
"""Read-only complete FasterXML frame and fixed-source CI suitability review.

Never runs repository code, builds, models, or workflows. All HTTP bodies and
headers are archived. Successful CI is not an admission rule.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlparse, parse_qs

import yaml

from scripts.rescreen_java_benchmark import population_check

ORG = "FasterXML"
OBSERVED_DATE = "2026-09-22"
CUTOFF = "2025-09-22T00:00:00Z"
BUILD = re.compile(r"(?:^|[\s;&|])(?:\./)?(?:mvnw?|gradlew?)(?:\s|$)")
TEST_GOAL = re.compile(r"(?:^|\s)(?:test|verify|install|package|build|check)(?:\s|$)")
CACHED_RUNS_ONLY = False


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def ref(path):
    raw = path.read_bytes()
    return {"path": str(path.resolve()), "sha256": digest(raw), "bytes": len(raw)}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def fetch(endpoint, path, *, binary=False):
    receipt = path.with_name(path.name + ".receipt.json")
    if receipt.exists() and path.exists():
        meta = json.loads(receipt.read_text())
        raw = path.read_bytes()
        if digest(raw) != meta["sha256"] or meta["url"] != "https://api.github.com/" + endpoint:
            raise ValueError("Archived HTTP response or endpoint changed")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        started = now()
        result = subprocess.run(["gh", "api", "--hostname", "github.com", "--include", "--method", "GET", endpoint], capture_output=True, timeout=90)
        raw_response = result.stdout
        if b"\r\n\r\n" in raw_response:
            headers, raw = raw_response.split(b"\r\n\r\n", 1)
        elif b"\n\n" in raw_response:
            headers, raw = raw_response.split(b"\n\n", 1)
        else:
            headers, raw = b"", raw_response
        header_text = headers.decode("utf-8", errors="replace")
        status = re.search(r"^HTTP/\S+\s+(\d+)", header_text)
        fields = {}
        for line in header_text.splitlines()[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                fields[k.lower()] = v.strip()
        path.write_bytes(raw)
        header_path = path.with_name(path.name + ".headers.txt")
        header_path.write_bytes(headers)
        meta = {"url": "https://api.github.com/" + endpoint, "retrieved_at": started, "finished_at": now(),
                "http_status": int(status[1]) if status else None, "exit_code": result.returncode,
                "sha256": digest(raw), "bytes": len(raw), "headers": ref(header_path),
                "link": fields.get("link"), "rate_remaining": fields.get("x-ratelimit-remaining"),
                "error": result.stderr.decode(errors="replace") if result.returncode else None}
        write(receipt, meta)
    if meta["exit_code"] or meta["http_status"] != 200:
        raise RuntimeError("HTTP unavailable " + endpoint + " status=" + str(meta["http_status"]))
    return (raw if binary else json.loads(raw)), {**ref(path), "receipt": ref(receipt), "url": meta["url"], "link": meta.get("link")}


def pages(endpoint, directory, key=None):
    rows, refs, page = [], [], 1
    while True:
        query = endpoint + ("&" if "?" in endpoint else "?") + "per_page=100&page=" + str(page)
        value, source = fetch(query, directory / ("page-" + str(page) + ".json"))
        items = value[key] if key else value
        if not isinstance(items, list):
            raise ValueError("Paged endpoint did not return a list")
        rows.extend(items)
        refs.append(source)
        if not source.get("link") or 'rel="next"' not in source["link"]:
            break
        page += 1
    return rows, refs


def metadata_eligibility(repo):
    reasons, issues = [], []
    for field in ("private", "fork", "archived"):
        if type(repo.get(field)) is not bool:
            issues.append(field + "_unknown")
        elif repo[field]:
            reasons.append(field)
    if type(repo.get("stargazers_count")) is not int:
        issues.append("stars_unknown")
    elif repo["stargazers_count"] <= 200:
        reasons.append("stars_not_greater_than_200")
    if repo.get("language") is None:
        issues.append("primary_language_unknown")
    elif repo["language"] != "Java":
        reasons.append("primary_language_not_Java")
    return reasons, issues


def discover(base, previous):
    organization, org_ref = fetch("orgs/" + ORG, base / "raw" / "organization.json")
    repos, page_refs = pages("orgs/" + ORG + "/repos?type=public&sort=full_name&direction=asc", base / "raw" / "organization-repositories")
    if len({r["id"] for r in repos}) != len(repos):
        raise ValueError("Repository ID repeated across complete organization pages")
    old = json.loads(previous.read_text())["rows"]
    old_ids = {r["repository_id"]: r["repo"] for r in old}
    rows = []
    for repo in repos:
        reasons, issues = metadata_eligibility(repo)
        row = {"repo": repo["full_name"], "repository_id": repo["id"], "stars": repo["stargazers_count"],
               "primary_language": repo["language"], "default_branch": repo["default_branch"],
               "fork": repo["fork"], "archived": repo["archived"], "public": not repo["private"], "repository_created_at": repo["created_at"],
               "original_frame_overlap": old_ids.get(repo["id"]),
               "family": "Jackson" if repo["name"].lower().startswith("jackson") else "FasterXML_non_Jackson",
               "family_independence_caveat": "Jackson repositories share maintainers/configuration and must not be treated as independent ecosystems.",
               "status": "metadata_excluded" if reasons else "metadata_unknown" if issues else "activity_review_pending", "reasons": reasons, "metadata_issues": issues}
        if not reasons and not issues:
            directory = base / "raw" / "repositories" / str(repo["id"])
            try:
                commit, source = fetch("repos/" + repo["full_name"] + "/commits/" + quote(repo["default_branch"], safe=""), directory / "default-branch-head.json")
                date = commit["commit"]["committer"]["date"]
                row.update(activity_head_sha=commit["sha"], default_branch_commit_date=date, activity_source=source)
                recent = None
                if date < CUTOFF:
                    recent, recent_ref = fetch("repos/" + repo["full_name"] + "/commits?sha=" + quote(repo["default_branch"], safe="") + "&since=" + CUTOFF + "&per_page=1", directory / "recent-default-history.json")
                    row["recent_history_source"] = recent_ref
                screen = {**row, "default_branch_sha": commit["sha"], "default_branch_commit_date": date}
                observed_at = json.loads(Path(source["receipt"]["path"]).read_text())["retrieved_at"]
                check = population_check(repo, screen, cutoff=CUTOFF[:10], head=commit, history=recent, observed_at=observed_at)
                row["population_check"] = check
                row["status"] = {"eligible": "metadata_candidate", "ineligible": "activity_excluded", "unavailable": "activity_unknown"}[check["status"]]
                row["reasons"].extend(check["exclusion_reasons"] + check["issues"])
            except Exception as exc:
                row["status"] = "activity_unknown"
                row["reasons"].append(str(exc))
        rows.append(row)
    result = {"schema": "org-expansion-discovery-v1", "organization": ORG, "organization_id": organization["id"],
              "observed_date": OBSERVED_DATE, "activity_cutoff": CUTOFF, "source": org_ref,
              "enumeration": {"complete": organization.get("public_repos") == len(repos), "repositories": len(repos),
                              "organization_public_repos": organization.get("public_repos"),
                              "public_count_matches_unique_ids": organization.get("public_repos") == len(repos),
                              "atomic_snapshot": False, "pages": page_refs,
                              "method": "public organization repository listing, complete Link pagination; no search rank truncation"},
              "original_315_ref": ref(previous), "rows": rows}
    write(base / "discovery.json", result)
    write(base / "candidates.json", {"observed_date": OBSERVED_DATE, "source": ref(base / "discovery.json"),
                                     "candidates": [r for r in rows if r["status"] == "metadata_candidate"]})
    return result


def content(repo, sha, path, directory):
    value, source = fetch("repos/" + repo + "/contents/" + quote(path, safe="/") + "?ref=" + sha,
                          directory / (path + ".api.json"))
    if value.get("encoding") != "base64" or value.get("type") != "file":
        raise ValueError("Not a complete regular source blob")
    raw = base64.b64decode(value["content"])
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    if blob != value["sha"]:
        raise ValueError("Source content Git blob mismatch")
    target = directory / "decoded" / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return raw.decode("utf-8", errors="replace"), {**ref(target), "repository_path": path, "git_blob_sha": blob,
                                                    "pinned_sha": sha, "response": source}


def workflow_commands(text):
    workflow = yaml.safe_load(text) or {}
    commands = []
    for key, job in (workflow.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        for index, step in enumerate(job.get("steps") or []):
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                commands.append({"job_key": key, "job_display_template": job.get("name", key),
                                 "runs_on": job.get("runs-on"), "job_if": job.get("if"), "continue_on_error": job.get("continue-on-error", False),
                                 "step_index": index, "step_name": step.get("name"), "step_if": step.get("if"),
                                 "command": step["run"], "environment": {"workflow": workflow.get("env"), "job": job.get("env"), "step": step.get("env")},
                                 "working_directory": step.get("working-directory"),
                                 "contains_build_tool": bool(BUILD.search(step["run"])),
                                 "contains_test_lifecycle_token": bool(TEST_GOAL.search(step["run"]))})
    return commands


def workflow_unknowns(text):
    """Do not interpret an unmapped action/defaultGoal as proof of no task."""
    workflow = yaml.safe_load(text) or {}
    unknowns = []
    for key, job in (workflow.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        if job.get("uses"):
            unknowns.append({"job_key": key, "reason": "reusable_workflow_scope_unreviewed", "uses": job["uses"]})
        for index, step in enumerate(job.get("steps") or []):
            if isinstance(step, dict) and step.get("uses"):
                unknowns.append({"job_key": key, "step_index": index, "reason": "action_scope_not_inferred_from_uses", "uses": step["uses"]})
    for command in workflow_commands(text):
        if command["contains_build_tool"] and not command["contains_test_lifecycle_token"]:
            unknowns.append({"job_key": command["job_key"], "step_index": command["step_index"],
                             "reason": "goal_or_defaultGoal_scope_requires_review", "command": command["command"]})
    return unknowns


def enumerate_runs(repo, branch, created_at, directory):
    """Partition the GitHub filtered-run 1,000-result API cap, not an admission cap."""
    begin = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    end = datetime.now(timezone.utc).replace(microsecond=0)
    refs, ranges, by_id, issues = [], [], {}, []
    # An interrupted/inconsistent prior attempt keeps its initial observation
    # boundary; do not silently advance time to chase a more convenient result.
    archived_windows = []
    for receipt in directory.glob("*/page-1.json.receipt.json"):
        value = json.loads(receipt.read_text())
        created = parse_qs(urlparse(value["url"]).query).get("created", [])
        if created and ".." in created[0]:
            lo, hi = [datetime.fromisoformat(x.replace("Z", "+00:00")) for x in created[0].split("..")]
            archived_windows.append((hi - lo, lo, hi))
    if archived_windows:
        _, begin, end = max(archived_windows)
    def collect(lo, hi):
        stamp = lambda value: value.strftime("%Y-%m-%dT%H:%M:%SZ")
        window = stamp(lo) + ".." + stamp(hi)
        endpoint = "repos/" + repo + "/actions/runs?branch=" + quote(branch, safe="") + "&status=completed&created=" + quote(window, safe="")
        folder = directory / (stamp(lo).replace(":", "-") + "_" + stamp(hi).replace(":", "-"))
        first, first_ref = fetch(endpoint + "&per_page=100&page=1", folder / "page-1.json")
        refs.append(first_ref)
        count = first.get("total_count")
        if type(count) is not int or count < 0:
            raise ValueError("Missing typed total_count for run enumeration")
        if count > 1000:
            if (hi - lo).total_seconds() < 1:
                raise ValueError("More than 1000 runs share one second: API range remains unresolved")
            mid = lo + timedelta(seconds=int((hi-lo).total_seconds()) // 2)
            ranges.append({"created_range": window, "api_total_count": count, "partitioned": True, "source": first_ref})
            collect(lo, mid)
            collect(mid + timedelta(seconds=1), hi)
            return
        rows = list(first["workflow_runs"])
        source, page, counts = first_ref, 1, [count]
        while source.get("link") and 'rel="next"' in source["link"]:
            page += 1
            value, source = fetch(endpoint + "&per_page=100&page=" + str(page), folder / ("page-" + str(page) + ".json"))
            refs.append(source)
            rows.extend(value["workflow_runs"])
            counts.append(value.get("total_count"))
        unique = {x["id"]: x for x in rows}
        if len(rows) != len(unique) or len(unique) != count or any(x != count for x in counts):
            issues.append({"reason": "run_pagination_count_or_id_drift", "created_range": window,
                           "page_total_counts": counts, "rows": len(rows), "unique_ids": len(unique)})
        for identity, run in unique.items():
            if identity in by_id:
                issues.append({"reason": "run_duplicate_across_created_ranges", "run_id": identity})
            by_id[identity] = run
        ranges.append({"created_range": window, "api_total_count": count, "captured_unique_runs": len(unique), "partitioned": False, "source": first_ref})
    # Preserve observation end on rerun, so cache replay does not silently change scope.
    manifest = directory / "enumeration.json"
    if manifest.exists():
        stored = json.loads(manifest.read_text())
        return reconcile_enumeration(stored["runs"], stored["extent"], branch, directory, manifest)
    if CACHED_RUNS_ONLY:
        for path in sorted(directory.glob("*/page-*.json")):
            if ".receipt." in path.name:
                continue
            receipt = json.loads(path.with_name(path.name + ".receipt.json").read_text())
            raw = path.read_bytes()
            if digest(raw) != receipt["sha256"]:
                raise ValueError("Cached run-page bytes changed")
            value = json.loads(raw)
            refs.append({**ref(path), "receipt": ref(path.with_name(path.name + ".receipt.json")), "url": receipt["url"]})
            ranges.append({"source": refs[-1], "api_total_count": value.get("total_count"), "captured_rows": len(value["workflow_runs"])})
            for run in value["workflow_runs"]:
                by_id[run["id"]] = run
        extent = {"complete_within_api_retention": False, "atomic_snapshot": False,
            "consistency_issues": [{"reason": "capture_stopped_with_inconsistent_or_partial_pagination_no_further_history_requests"}],
            "created_from": begin.isoformat(), "created_to": end.isoformat(), "unique_runs": len(by_id),
            "ranges": ranges, "pages": refs, "historical_deleted_runs_recoverable": False}
        cached_manifest = directory / "cached-range-review.json"
        write(cached_manifest, {"runs": list(by_id.values()), "extent": extent})
        return reconcile_enumeration(list(by_id.values()), extent, branch, directory, cached_manifest)
    collect(begin, end)
    extent = {"complete_within_api_retention": not issues, "atomic_snapshot": False, "consistency_issues": issues,
              "created_from": begin.isoformat(), "created_to": end.isoformat(),
              "unique_runs": len(by_id), "ranges": ranges, "pages": refs,
              "historical_deleted_runs_recoverable": False}
    write(manifest, {"runs": list(by_id.values()), "extent": extent})
    return reconcile_enumeration(list(by_id.values()), extent, branch, directory, manifest)


def reconcile_enumeration(runs, extent, branch, directory, manifest):
    """Never let an inconsistent alternate API query erase observed newer runs."""
    indexed = {run["id"]: run for run in runs}
    issues = list(extent.get("consistency_issues", []))
    wrong_branch = [run["id"] for run in runs if run.get("head_branch") != branch]
    if wrong_branch:
        issues.append({"reason": "filtered_endpoint_returned_other_branch", "run_ids": wrong_branch})
    extra, supplementary = [], []
    prior_pages = directory.parent.parent / "default-branch-runs"
    upper = datetime.fromisoformat(extent["created_to"])
    for path in sorted(prior_pages.glob("page-*.json")):
        if ".receipt." in path.name:
            continue
        receipt = json.loads(path.with_name(path.name + ".receipt.json").read_text())
        raw = path.read_bytes()
        if digest(raw) != receipt["sha256"]:
            raise ValueError("Prior run-page bytes changed")
        supplementary.append({**ref(path), "receipt": ref(path.with_name(path.name + ".receipt.json")), "url": receipt["url"]})
        for run in json.loads(raw)["workflow_runs"]:
            if run.get("head_branch") != branch or run.get("status") != "completed":
                continue
            if datetime.fromisoformat(run["created_at"].replace("Z", "+00:00")) > upper:
                continue
            if run["id"] not in indexed:
                extra.append(run["id"])
                indexed[run["id"]] = run
            elif run["updated_at"] > indexed[run["id"]]["updated_at"]:
                indexed[run["id"]] = run
    if extra:
        issues.append({"reason": "created_range_query_omits_previously_observed_in_range_runs", "run_ids": extra})
    # Reconciliation is deterministic and idempotent; preserve original range
    # totals rather than replacing them with the supplemented observed union.
    unique_issues = {json.dumps(x, sort_keys=True): x for x in issues}
    revised = {**extent, "consistency_issues": list(unique_issues.values()),
               "complete_within_api_retention": not unique_issues,
               "supplementary_unbounded_pages": supplementary, "observed_union_runs": len(indexed),
               "manifest": ref(manifest)}
    return list(indexed.values()), revised


def eligible_jobs(candidate, jobs):
    matching = []
    for job in jobs:
        labels = " ".join(job.get("labels", [])).lower()
        name = job["name"]
        names = {s["name"] for s in job.get("steps", [])}
        matches = []
        for command in candidate["ordinary_build_test_command_candidates"]:
            display = command["job_display_template"]
            literal_job_match = isinstance(display, str) and "${{" not in display and (name == display or name.startswith(display + " ("))
            step_match = command.get("step_name") in names
            if (step_match or literal_job_match) and command["continue_on_error"] is False:
                matches.append(command)
        job_keys = {x["job_key"] for x in matches}
        if (len(job_keys) == 1 and job.get("status") == "completed" and job.get("completed_at")
                and ("ubuntu" in labels or "linux" in labels or "ubuntu" in name.lower() or "linux" in name.lower())
                and not any(x in name.lower() for x in ["experimental", "early-access", "early access"])):
            matching.append((job, matches))
    matching.sort(key=lambda pair: pair[0]["html_url"])
    matching.sort(key=lambda pair: pair[0]["completed_at"], reverse=True)
    return matching


def inspect_candidate(base, row):
    output = base / "reviews" / (str(row["repository_id"]) + ".json")
    if output.exists():
        cached = json.loads(output.read_text())
        if cached.get("selection_algorithm") == "completion-time-v3" and cached.get("ci_discovery"):
            return {**cached, **{k: v for k, v in row.items() if k not in {"status", "reasons"}}}
        old = base / "reviews-provisional-v1" / output.name
        if not old.exists():
            write(old, cached)
    repo, head = row["repo"], row["activity_head_sha"]
    directory = base / "raw" / "repositories" / str(row["repository_id"])
    review = {**row, "status": "ci_task_review_required", "selection_algorithm": "completion-time-v3",
              "requirements_reviewed": False, "static_qualified": False, "case_records_admitted": False,
              "ci_candidates": [], "gaps": []}
    try:
        tree, tree_ref = fetch("repos/" + repo + "/git/trees/" + head + "?recursive=1", directory / "head-tree.json")
        if tree.get("truncated"):
            raise ValueError("HEAD tree truncated; cannot claim source path inventory complete")
        paths = [x["path"] for x in tree["tree"] if x["type"] == "blob"]
        review["head_source"] = {"sha": head, "tree": tree_ref,
            "maven_descriptors": [p for p in paths if p == "pom.xml" or p.endswith("/pom.xml")],
            "gradle_descriptors": [p for p in paths if p.rsplit("/", 1)[-1] in {"build.gradle", "build.gradle.kts"}],
            "workflow_paths": [p for p in paths if p.startswith(".github/workflows/") and p.endswith((".yml", ".yaml"))],
            "android_named_files_not_exclusion": [p for p in paths if p.rsplit("/", 1)[-1].lower() == "androidmanifest.xml"],
            "alternate_production_source_candidates_not_exclusion": [p for p in paths if "/src/main/" in "/" + p and p.endswith((".scala", ".kt", ".groovy", ".c", ".cc", ".cpp", ".rs"))]}
        runs, extent = enumerate_runs(repo, row["default_branch"], row["repository_created_at"], directory / "selection-v2" / "runs")
        # updated_at is used only as an upper bound; every fetched job verifies it.
        runs.sort(key=lambda value: (value.get("updated_at") or "", value["html_url"]), reverse=True)
        best = None
        unknown = []
        skipped_by_bound = 0
        for run in runs:
            if run["event"] not in {"push", "schedule", "workflow_dispatch", "workflow_call"} or run["head_branch"] != row["default_branch"]:
                continue
            if best and run["updated_at"] < best[1]["completed_at"]:
                skipped_by_bound += 1
                continue
            candidate = {k: run.get(k) for k in ["id", "name", "path", "workflow_id", "head_sha", "head_branch", "event", "conclusion", "status", "run_attempt", "html_url", "created_at", "updated_at"]}
            review["ci_candidates"].append(candidate)
            try:
                source_path = run["path"].split("@", 1)[0]
                text, source = content(repo, run["head_sha"], source_path, directory / "revisions" / run["head_sha"])
                commands = workflow_commands(text)
                ordinary = [c for c in commands if c["contains_build_tool"] and c["contains_test_lifecycle_token"]
                            and not re.search(r"(?:^|\s)(?:deploy|publish|release:perform)(?:\s|$)", c["command"])]
                candidate.update(workflow_source=source, source_commands=commands,
                                 source_scope_unknowns=workflow_unknowns(text), ordinary_build_test_command_candidates=ordinary,
                                 selection_status="candidate" if ordinary else "ordinary_task_not_resolved_from_direct_commands")
                if not ordinary:
                    # No direct command cannot exclude reusable/composite/defaultGoal tasks.
                    unknown.append({"run_id": run["id"], "updated_at": run["updated_at"], "reason": "workflow_task_scope_unresolved"})
                    continue
                jobs, job_refs = pages("repos/" + repo + "/actions/runs/" + str(run["id"]) + "/attempts/" + str(run["run_attempt"]) + "/jobs", directory / "runs" / str(run["id"]) / ("attempt-" + str(run["run_attempt"])) / "jobs", "jobs")
                candidate.update(jobs_source=job_refs, jobs=jobs)
                if any(job.get("completed_at") and job["completed_at"] > run["updated_at"] for job in jobs):
                    raise ValueError("Observed job completion exceeds run updated_at: selection bound invalid")
                matches = eligible_jobs(candidate, jobs)
                if not matches:
                    unknown.append({"run_id": run["id"], "updated_at": run["updated_at"], "reason": "ordinary_linux_cell_mapping_unresolved"})
                for job, mapped in matches:
                    if best is None or job["completed_at"] > best[1]["completed_at"] or (job["completed_at"] == best[1]["completed_at"] and job["html_url"] < best[1]["html_url"]):
                        best = (candidate, job, mapped)
            except Exception as exc:
                candidate.update(selection_status="workflow_or_job_evidence_unknown", error=str(exc))
                unknown.append({"run_id": run["id"], "updated_at": run["updated_at"], "reason": str(exc)})
        relevant_unknown = [x for x in unknown if best is None or x["updated_at"] >= best[1]["completed_at"]]
        review["ci_discovery"] = {"enumeration": extent, "runs_source_reviewed": len(review["ci_candidates"]),
            "runs_pruned_by_updated_at_upper_bound": skipped_by_bound,
            "selection": "eligible Linux job completed_at descending, URL ascending tie; all observable run metadata enumerated; run updated_at upper bound checked on fetched jobs; no conclusion filter",
            "upper_bound_assumption": "GitHub run updated_at is no earlier than any current-attempt job completed_at; all inspected jobs satisfy this, older uninspected jobs remain bounded by provider metadata.",
            "newer_or_equal_unresolved_runs": relevant_unknown,
            "selection_status": "provisional_scope_review" if relevant_unknown or not extent["complete_within_api_retention"] else "candidate_fixed_within_observable_api_history",
            "limitations": ["GitHub API pages are not an atomic snapshot; deleted historical runs are not observable.", "Selected source commands still require complete runtime/defaultGoal/action scope review."]}
        if best is None:
            review["status"] = "ci_reference_pending"
            review["gaps"].append("No source-confirmed ordinary Linux reference mapped; unresolved workflows/actions/defaultGoals are not evidence that no task exists.")
        else:
            selected, job, command_candidates = best
            sha = selected["head_sha"]
            task_tree, task_tree_ref = fetch("repos/" + repo + "/git/trees/" + sha + "?recursive=1", directory / "revisions" / sha / "tree.json")
            if task_tree.get("truncated"):
                raise ValueError("Selected CI task tree truncated")
            task_paths = [x["path"] for x in task_tree["tree"] if x["type"] == "blob"]
            refs = []
            for path in task_paths:
                if (path in {"pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "gradle.properties", "mvnw", "gradlew"}
                        or path.startswith(".mvn/") and not path.endswith(".jar") or path == "gradle/wrapper/gradle-wrapper.properties"):
                    _, source = content(repo, sha, path, directory / "revisions" / sha)
                    refs.append(source)
            log_dir = directory / "runs" / str(selected["id"]) / ("attempt-" + str(selected["run_attempt"]))
            try:
                _, log_ref = fetch("repos/" + repo + "/actions/jobs/" + str(job["id"]) + "/logs", log_dir / ("job-" + str(job["id"]) + ".log"), binary=True)
                log_error = None
            except Exception as exc:
                log_ref, log_error = None, str(exc)
            review["selected_ci"] = {"repo": repo, "repository_id": row["repository_id"], "sha": sha,
                "activity_head_sha": head, "default_branch": row["default_branch"], "run_id": selected["id"],
                "run_attempt": selected["run_attempt"], "run_url": selected["html_url"], "job_id": job["id"],
                "selected_job_ids": [job["id"]], "job_name": job["name"], "official_ci_url": job["html_url"],
                "job_completed_at": job["completed_at"], "conclusion": job["conclusion"], "run_conclusion": selected["conclusion"],
                "selection": review["ci_discovery"]["selection"], "selection_status": review["ci_discovery"]["selection_status"],
                "workflow_ref": selected["workflow_source"], "source_command_candidates": command_candidates,
                "jobs_refs": selected["jobs_source"], "source_tree_ref": task_tree_ref, "source_config_refs": refs,
                "job_log_ref": log_ref, "job_log_error": log_error,
                "command_scope_status": "source-to-job candidate mapping; exact actual argv/config/defaultGoal and complete task scope require log/source review",
                "admitted": False}
            review["gaps"].extend(["Review actual command/defaultGoal/profile/runtime against selected-job logs and effective module scope.",
                "Case report completeness/identity remains unreviewed; a green conclusion is not admission.",
                "Required Docker/Android/second production toolchain dependencies need task-bound review, not filename exclusion."])
            if relevant_unknown:
                review["gaps"].append("Newer/equal updated workflows have unresolved scope; selected direct-command candidate is provisional.")
            if not extent["complete_within_api_retention"]:
                review["gaps"].append("Run pagination count/ID drift under a fixed observation cutoff prevents a complete-history selection claim; all observed responses are retained.")
    except Exception as exc:
        review["status"] = "evidence_unknown"
        review["gaps"].append(str(exc))
    write(output, review)
    return review

def inspect(base):
    discovery = json.loads((base / "discovery.json").read_text())
    reviews = []
    for row in discovery["rows"]:
        if row["status"] != "metadata_candidate":
            continue
        review = inspect_candidate(base, row)
        reviews.append(review)
        write(base / "selected-ci.json", {"schema": "org-expansion-selected-ci-v1", "organization": ORG,
               "tasks": [x["selected_ci"] for x in reviews if x.get("selected_ci")], "complete": len(reviews) == len([r for r in discovery["rows"] if r["status"] == "metadata_candidate"]),
               "selection_never_filters_success": True, "all_task_scopes_pending_review": True})
        print(review["repo"], review["status"], review.get("selected_ci", {}).get("conclusion"), flush=True)
    write(base / "reviews.json", {"source": ref(base / "discovery.json"), "reviews": reviews})
    by_id = {r["repository_id"]: r for r in reviews}
    write(base / "ledger.json", {"organization": ORG, "observation_date": OBSERVED_DATE, "activity_cutoff": CUTOFF,
                                "enumeration_complete": discovery["enumeration"]["complete"], "rows": [by_id.get(r["repository_id"], r) for r in discovery["rows"]]})
    return reviews


def summarize(base):
    discovery = json.loads((base / "discovery.json").read_text())
    reviews = json.loads((base / "reviews.json").read_text())["reviews"]
    rows = []
    for review in reviews:
        task = review.get("selected_ci", {})
        selected = next((x for x in review["ci_candidates"] if x["id"] == task.get("run_id")), {})
        pom = next((x for x in task.get("source_config_refs", []) if x["repository_path"] == "pom.xml"), None)
        declarations = {"state": "unknown"}
        if pom:
            raw = Path(pom["path"]).read_bytes()
            if digest(raw) != pom["sha256"]:
                raise ValueError("Selected POM source bytes changed")
            root = ET.fromstring(raw)
            local = lambda value: value.rsplit("}", 1)[-1]
            for node in root.iter():
                node.tag = local(node.tag)
            value = lambda path: root.findtext(path)
            declarations = {"state": "literal_pinned_source_only", "source": pom,
                "packaging": value("packaging"), "artifact_id": value("artifactId"),
                "default_goal": value("build/defaultGoal"), "modules": [x.text for x in root.findall("modules/module")],
                "parent": {local(x.tag): x.text for x in root.findall("parent/*")},
                "effective_model_resolved": False}
        source_tree = task.get("source_tree_ref")
        paths = []
        if source_tree:
            raw = Path(source_tree["path"]).read_bytes()
            if digest(raw) != source_tree["sha256"]:
                raise ValueError("Selected tree source bytes changed")
            paths = [x["path"] for x in json.loads(raw)["tree"] if x["type"] == "blob"]
        workflow = yaml.safe_load(Path(task["workflow_ref"]["path"]).read_text()) if task.get("workflow_ref") else {}
        job_keys = {x["job_key"] for x in task.get("source_command_candidates", [])}
        job_inputs = [{"job_key": key, "runs_on": job.get("runs-on"), "matrix": job.get("strategy"),
                       "services": job.get("services"), "container": job.get("container"),
                       "source_steps": job.get("steps"), "environment": job.get("env")}
                      for key, job in (workflow.get("jobs") or {}).items() if key in job_keys]
        observed_job = next((x for x in selected.get("jobs", []) if x["id"] == task.get("job_id")), {})
        rows.append({"repo": review["repo"], "repository_id": review["repository_id"], "family": review["family"],
            "sha": task.get("sha"), "run_id": task.get("run_id"), "run_attempt": task.get("run_attempt"),
            "job_id": task.get("job_id"), "official_ci_url": task.get("official_ci_url"),
            "selection_status": task.get("selection_status", "pending"), "task_admitted": False,
            "source_build_tool": "maven" if any("mvn" in x["command"] for x in task.get("source_command_candidates", [])) else "unknown",
            "source_command_candidates": task.get("source_command_candidates", []),
            "step_execution_metadata": observed_job.get("steps", []), "job_configuration_source": job_inputs,
            "root_pom_declarations": declarations, "pinned_source_tree": source_tree,
            "maven_descriptor_paths": [x for x in paths if x == "pom.xml" or x.endswith("/pom.xml")],
            "alternate_production_source_candidates": [x for x in paths if "/src/main/" in "/" + x and x.endswith((".scala", ".kt", ".groovy", ".c", ".cc", ".cpp", ".rs"))],
            "android_manifest_paths": [x for x in paths if x.rsplit("/", 1)[-1].lower() == "androidmanifest.xml"],
            "exclusion_rule": "Path/name evidence alone does not establish required Docker, Android or second production-language tooling; task-bound dependency closure remains unreviewed.",
            "remaining_requirement_gaps": review["gaps"]})
    result = {"schema": "fasterxml-source-task-applicability-v1", "observed_date": OBSERVED_DATE,
              "discovery": ref(base / "discovery.json"), "selected_ci": ref(base / "selected-ci.json"),
              "script": ref(Path(__file__)), "tasks": rows, "formal_requirements_v2_complete": 0}
    write(base / "source-task-applicability.json", result)
    from collections import Counter
    states = Counter(row["status"] for row in discovery["rows"])
    chosen = [x["selected_ci"] for x in reviews if x.get("selected_ci")]
    fixed = sum(x.get("selection_status") == "candidate_fixed_within_observable_api_history" for x in chosen)
    lines = ["# FasterXML expansion: source frame and CI candidates", "",
        f"Observation date: {OBSERVED_DATE}; activity cutoff: {CUTOFF[:10]}. This is a read-only acquisition wave, not an agent experiment.", "",
        f"The official public repository pages contain {len(discovery['rows'])} unique repository IDs; the organization reports {discovery['enumeration']['organization_public_repos']} public repositories. These non-atomic observations agree. The metadata screen retains {states['metadata_candidate']} candidates, excludes {states['metadata_excluded']}, and leaves {states['metadata_unknown']} unknown. Strict stars >200, primary Java and default-branch activity apply. Unknown language is not silently reclassified as non-Java.", "",
        f"All {len(reviews)} threshold candidates were reviewed. {len(chosen)} exact source/run/attempt/job candidate tuples are available; {fixed} have no newer unresolved scope or pagination gap within observable API history. The other candidates remain provisional. **Zero are admitted reference tasks or complete requirements-v2 definitions.** An available/green job is not proof of full build/test evidence.", "",
        "The `selected-ci.json` field `complete` means that every metadata candidate has a recorded processing decision. It does not mean that task selection, requirements, report scope or admission is complete. Earlier source versions are not automatically relabeled as the final candidate revision when a corrected CI choice differs.", "",
        "CI selection enumerates available completed default-branch run metadata, partitions the GitHub 1,000 filtered-result API cap by creation time, and orders eligible ordinary Linux jobs by actual completion time (URL ascending on ties). It does not filter success. Run updated_at provides an explicit upper bound for deciding which older runs need source/job inspection; all fetched jobs are checked against it. API pages are non-atomic and deleted runs are unobservable. Pagination drift and newer unresolved reusable/action/defaultGoal scopes remain visible, so this is not an unconditional latest-task claim.", "",
        "| Repository | Family | Candidate job result | Selection | Selected SHA |", "|---|---|---|---|---|"]
    for review in reviews:
        task = review.get("selected_ci", {})
        lines.append(f"| {review['repo']} | {review['family']} | {task.get('conclusion', 'pending')} | {task.get('selection_status', 'pending')} | {task.get('sha', '')[:12]} |")
    lines.extend(["", "## What the source review establishes", "",
        "The candidate workflows use Maven wrappers and ordinary verify/test commands. Conditional coverage steps remain command candidates until matched to the selected job's executed/skipped steps; they are not silently added to a frozen task. Root POM packaging/defaultGoal/modules are literal pinned-source observations, not effective module denominators. Parent POMs, profiles, inherited quality gates, runtime versions and artifact expectations still need a full requirements review.", "",
        "Required Docker, Android and additional production toolchains are not inferred from filenames. The review records selected-job service/container configuration and alternate source paths, and keeps the dependency closure pending. No project was excluded by source size, test count, CI outcome, log accessibility or expected SAG performance.", "",
        "Jackson-family repositories share maintainers and build infrastructure. Treat repository observations as nested within that family, not independent ecosystem replications. The original 315-repository frame is deduplicated by repository ID; all overlap fields are retained.", "",
        "## Inspectable evidence and next action", "",
        "- `discovery.json` / `ledger.json`: all 68 repositories and screen dispositions; official page and activity provenance.",
        "- `selected-ci.json`: exact provisional/fixed candidate identities; `reviews/*.json` lists inspected runs, bounds, gaps and raw source/job references.",
        "- `source-task-applicability.json`: pinned POM declarations, command/step configuration and dependency-review gaps.",
        "- `raw/`: unchanged HTTP bodies, headers, URL/time/status/length/SHA-256 receipts, including errors. Earlier first-pass choices remain in `reviews-provisional-v1/`.",
        "- Next: independently close ordinary-task selection where pending, bind complete actual commands and runtime, acquire full build/test reports, and only then author requirements-v2 definitions. Do not switch CI cells for convenient XML or green results.", ""])
    (base / "REPORT.md").write_text("\n".join(lines))
    return result


def main():
    global CACHED_RUNS_ONLY
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["discover", "inspect", "summarize"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--previous-ledger", type=Path, default=Path("output/java-benchmark-20260916/candidate-ledger.json"))
    parser.add_argument("--cached-runs-only", action="store_true", help="Stop historical run acquisition; retain partial/inconsistent archived extents as pending")
    args = parser.parse_args()
    CACHED_RUNS_ONLY = args.cached_runs_only
    result = (discover(args.output.resolve(), args.previous_ledger.resolve()) if args.phase == "discover"
              else inspect(args.output.resolve()) if args.phase == "inspect" else summarize(args.output.resolve()))
    if args.phase == "discover":
        from collections import Counter
        print(json.dumps({"repos": len(result["rows"]), "states": dict(Counter(r["status"] for r in result["rows"])), "candidates": [r["repo"] for r in result["rows"] if r["status"] == "metadata_candidate"]}))


if __name__ == "__main__":
    main()
