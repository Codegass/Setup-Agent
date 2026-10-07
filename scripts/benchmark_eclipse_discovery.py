"""Discover the registry-declared Eclipse GitHub frame, without project execution."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import parse_qs, quote, urlparse

from scripts.benchmark_source_recovery import capture, file_ref, split_http, verify_ref
from scripts.benchmark_source_audit import write_json
from scripts.rescreen_java_benchmark import population_check

REGISTRY_SHA = "446f4e9555a4910a8b36c4519bdd2719bd923fda"
REGISTRY_REPO = "eclipse/eclipse-projects"
OBSERVATION_DATE = "2026-09-22"
ACTIVITY_CUTOFF = "2025-09-22T00:00:00Z"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def links(headers):
    result = {}
    for line in headers.decode("utf-8", errors="replace").splitlines():
        if line.lower().startswith("link:"):
            for url, rel in re.findall(r'<([^>]+)>;\s*rel="([^\"]+)"', line):
                result[rel] = url
    return result


def pmi_page(page, output, network):
    url = f"https://projects.eclipse.org/api/projects?github_only=1&pagesize=100&page={page}"
    folder = output / "raw/pmi"
    folder.mkdir(parents=True, exist_ok=True)
    body_path, header_path = folder / f"page-{page}.json", folder / f"page-{page}.headers.txt"
    meta_path = folder / f"page-{page}.json.meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_bytes())
        raw, headers = body_path.read_bytes(), header_path.read_bytes()
        if (
            meta["url"] != url
            or hashlib.sha256(raw).hexdigest() != meta["sha256"]
            or len(raw) != meta["bytes"]
            or hashlib.sha256(headers).hexdigest() != meta["headers_sha256"]
        ):
            raise ValueError("PMI cached response binding changed")
    else:
        if not network:
            raise ValueError("PMI page not captured")
        started = utc_now()
        run = subprocess.run(
            [
                "curl",
                "--silent",
                "--show-error",
                "--location",
                "--max-time",
                "45",
                "--dump-header",
                str(header_path),
                "--output",
                str(body_path),
                "--write-out",
                "%{http_code}\n%{url_effective}",
                url,
            ],
            capture_output=True,
            timeout=50,
        )
        raw = body_path.read_bytes() if body_path.exists() else b""
        headers = header_path.read_bytes() if header_path.exists() else b""
        status = run.stdout.decode().splitlines()
        meta = {
            "url": url,
            "effective_url": status[1] if len(status) > 1 else None,
            "captured_at": started,
            "completed_at": utc_now(),
            "http_status": int(status[0]) if status and status[0].isdigit() else None,
            "returncode": run.returncode,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "headers_sha256": hashlib.sha256(headers).hexdigest(),
            "stderr": run.stderr.decode(errors="replace"),
        }
        write_json(meta_path, meta)
    if (
        meta["http_status"] != 200
        or meta["returncode"] != 0
        or urlparse(meta["effective_url"]).hostname != "projects.eclipse.org"
    ):
        raise ValueError("Official PMI response unavailable")
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("PMI page is not a project list")
    return data, {
        "body": file_ref(body_path, output),
        "headers": file_ref(header_path, output),
        "receipt": file_ref(meta_path, output),
        "links": links(headers),
        "url": url,
    }


def repository_name(value):
    if not isinstance(value, str):
        raise ValueError("Repository URL is not a string")
    parsed = urlparse(value)
    if parsed.scheme not in {"https", "http"} or parsed.hostname not in {
        "github.com",
        "www.github.com",
    }:
        raise ValueError("Repository is not an explicit GitHub URL")
    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path) or any(
        part in {".", ".."} for part in path.split("/")
    ):
        raise ValueError("Invalid explicit GitHub repository path")
    return path


def registry_frame(output, network=False):
    first, first_ref = pmi_page(1, output, network)
    last_url = first_ref["links"].get("last")
    if not last_url:
        raise ValueError("PMI pagination does not declare a final page")
    last = urlparse(last_url)
    params = parse_qs(last.query)
    if (
        last.hostname != "projects.eclipse.org"
        or last.path != "/api/projects"
        or params.get("github_only") != ["1"]
        or params.get("pagesize") != ["100"]
    ):
        raise ValueError("PMI last-page scope changed")
    final_page = int(params["page"][0])
    pages, projects = [first_ref], list(first)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = {
            pool.submit(pmi_page, page, output, network): page for page in range(2, final_page + 1)
        }
        results = {1: (first, first_ref)}
        for future in as_completed(pending):
            results[pending[future]] = future.result()
    pages, projects = [], []
    for page in range(1, final_page + 1):
        rows, page_ref = results[page]
        if page != final_page:
            next_url = page_ref["links"].get("next", "")
            if parse_qs(urlparse(next_url).query).get("page") != [str(page + 1)]:
                raise ValueError("PMI pagination chain is incomplete")
        elif page_ref["links"].get("next"):
            raise ValueError("PMI frame changed while pages were collected")
        pages.append(page_ref)
        projects.extend((row, page, offset) for offset, row in enumerate(rows))
    seen = set()
    orgs, repos = defaultdict(list), defaultdict(list)
    rows, issues = [], []
    for project, page, offset in projects:
        pid = project.get("project_id")
        if not pid or pid in seen:
            raise ValueError("Missing/duplicate project in PMI pages")
        seen.add(pid)
        origin = {
            "project_id": pid,
            "project_name": project.get("name"),
            "project_url": project.get("url"),
            "page": page,
            "row_index": offset,
            "registry_body": pages[page - 1]["body"],
        }
        github = project.get("github") or {}
        org = github.get("org")
        ignored = github.get("ignored_repos") or []
        if org:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", org) or org in {".", ".."}:
                issues.append({"project_id": pid, "reason": "invalid_declared_org", "value": org})
            else:
                orgs[org.lower()].append(
                    {
                        **origin,
                        "declared_org": org,
                        "ignored_repos": ignored,
                        "membership_basis": "PMI dedicated GitHub organization, minus its explicit ignored repositories",
                    }
                )
        explicit = []
        for entry in project.get("github_repos", []):
            try:
                name = repository_name(entry.get("url"))
                repos[name.lower()].append(
                    {
                        **origin,
                        "declared_repo": name,
                        "original_url": entry["url"],
                        "membership_basis": "PMI explicitly listed repository",
                    }
                )
                explicit.append(name)
            except ValueError as exc:
                issues.append({"project_id": pid, "reason": str(exc), "value": entry})
        rows.append(
            {
                **origin,
                "state": project.get("state"),
                "declared_org": org,
                "ignored_repos": ignored,
                "explicit_repositories": explicit,
            }
        )
    frame = {
        "schema": "eclipse-registry-frame-v1",
        "observation_date": OBSERVATION_DATE,
        "activity_cutoff": ACTIVITY_CUTOFF,
        "registry_source": {
            "repository": REGISTRY_REPO,
            "sha": REGISTRY_SHA,
            "generator": ".github/workflows/generate.py",
            "url": f"https://github.com/{REGISTRY_REPO}/blob/{REGISTRY_SHA}/.github/workflows/generate.py",
        },
        "scope": "All projects returned by the official PMI github_only=1 pages; dedicated org public repositories minus ignored_repos, plus explicitly listed repositories. No name-prefix or outcome selection.",
        "limitations": [
            "Eclipse's GitHub overview explicitly says the list is incomplete; completeness applies to this captured registry frame, not all conceivable Eclipse resources.",
            "PMI pages are individually timestamped, not an atomic foundation-wide snapshot; duplicate IDs and broken pagination are rejected.",
            "Metadata admission is separate from Maven/Gradle, Android, essential Docker and CI evidence review.",
        ],
        "pages": pages,
        "projects": rows,
        "organizations": dict(sorted(orgs.items())),
        "explicit_repositories": dict(sorted(repos.items())),
        "issues": issues,
        "summary": {
            "project_records": len(rows),
            "pages": len(pages),
            "declared_organizations": len(orgs),
            "explicit_repositories": len(repos),
            "registry_issues": len(issues),
        },
    }
    path = output / "registry-frame.json"
    if path.exists() and json.loads(path.read_bytes()) != frame:
        raise ValueError("Existing registry frame differs; use a new snapshot directory")
    write_json(path, frame)
    return frame


def registry_memberships(frame, org, repo):
    """An ignore applies to its declaring project, never unrelated registrations."""
    memberships, ignored = [], []
    for origin in frame["organizations"].get(org.lower(), []):
        exclusions = {str(value).lower().removesuffix(".git") for value in origin["ignored_repos"]}
        if repo.lower() in exclusions or repo.rsplit("/", 1)[-1].lower() in exclusions:
            ignored.append(origin)
        else:
            memberships.append(origin)
    ignored_projects = {origin["project_id"] for origin in ignored}
    memberships.extend(
        origin
        for origin in frame["explicit_repositories"].get(repo.lower(), [])
        if origin["project_id"] not in ignored_projects
    )
    return memberships, ignored


def explicit_memberships(frame, repo):
    memberships, _ = registry_memberships(frame, repo.split("/", 1)[0], repo)
    return [origin for origin in memberships if "ignored_repos" not in origin]


def initial_population(repository):
    excluded, unknown = [], []
    for field in ("private", "fork", "archived"):
        if type(repository.get(field)) is not bool:
            unknown.append(field + "_unknown")
        elif repository[field]:
            excluded.append(field)
    stars = repository.get("stargazers_count")
    if type(stars) is not int:
        unknown.append("stars_unknown")
    elif stars <= 200:
        excluded.append("stars_not_greater_than_200")
    if repository.get("language") is None:
        unknown.append("primary_language_unknown")
    elif repository["language"] != "Java":
        excluded.append("primary_language_not_java")
    return {
        "status": "unknown" if unknown else "excluded" if excluded else "needs_activity",
        "exclusion_reasons": excluded,
        "issues": unknown,
    }


def organization_repositories(org, frame, output, network):
    records, pages, errors, excluded = [], [], [], []
    page = 1
    seen = set()
    try:
        while True:
            endpoint = f"orgs/{org}/repos?type=public&per_page=100&sort=full_name&direction=asc&page={page}"
            receipt, payload = capture(
                endpoint,
                output / "raw/github/organizations" / org / f"page-{page:03d}",
                output,
                network,
            )
            if not isinstance(payload, list):
                raise ValueError("Organization response is not a repository list")
            pages.append(receipt)
            for offset, repository in enumerate(payload):
                rid, name = repository.get("id"), repository.get("full_name")
                if type(rid) is not int or not isinstance(name, str) or rid in seen:
                    raise ValueError("Organization pagination has duplicate/malformed repository")
                if repository_name("https://github.com/" + name) != name:
                    raise ValueError("Organization repository identity is malformed")
                seen.add(rid)
                memberships, ignored = registry_memberships(frame, org, name)
                entry = {
                    "repository": repository,
                    "registry_sources": memberships,
                    "ignored_memberships": ignored,
                    "metadata_source": {
                        "body": receipt["body"],
                        "receipt": receipt["receipt"],
                        "row_index": offset,
                        "observed_at": receipt["completed_at"],
                    },
                }
                if memberships:
                    records.append(entry)
                else:
                    excluded.append(entry)
            response = (output / receipt["raw_response"]["path"]).read_bytes()
            next_url = links(response.split(b"\r\n\r\n", 1)[0]).get("next")
            if not next_url:
                break
            parsed, params = urlparse(next_url), parse_qs(urlparse(next_url).query)
            owner_ids = {item.get("owner", {}).get("id") for item in payload}
            allowed_paths = {f"/orgs/{org}/repos"}
            if len(owner_ids) == 1 and type(next(iter(owner_ids))) is int:
                allowed_paths.add(f"/organizations/{next(iter(owner_ids))}/repos")
            if (
                not payload
                or parsed.hostname != "api.github.com"
                or parsed.path not in allowed_paths
            ):
                raise ValueError("Organization pagination changed resource")
            if params.get("page") != [str(page + 1)] or params.get("per_page") != ["100"]:
                raise ValueError("Organization pagination changed page scope")
            page += 1
    except (ValueError, OSError, KeyError, TypeError) as exc:
        errors.append(str(exc))
    return {
        "org": org,
        "status": "captured_complete_pagination" if not errors else "unknown_or_partial",
        "pages": pages,
        "errors": errors,
        "records": records,
        "registry_ignored_records": excluded,
    }


def explicit_repository(name, sources, output, network):
    try:
        receipt, payload = capture(
            f"repos/{name}", output / "raw/github/explicit-repositories" / name, output, network
        )
        if (
            not isinstance(payload, dict)
            or type(payload.get("id")) is not int
            or not isinstance(payload.get("full_name"), str)
        ):
            raise ValueError("Explicit repository response is malformed")
        if repository_name("https://github.com/" + payload["full_name"]) != payload["full_name"]:
            raise ValueError("Explicit repository identity is malformed")
        return {
            "requested_repo": name,
            "record": {
                "repository": payload,
                "registry_sources": sources,
                "ignored_memberships": [],
                "metadata_source": {
                    "body": receipt["body"],
                    "receipt": receipt["receipt"],
                    "row_index": None,
                    "observed_at": receipt["completed_at"],
                },
            },
            "errors": [],
        }
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return {
            "requested_repo": name,
            "record": None,
            "errors": [str(exc)],
            "registry_sources": sources,
            "failed_capture_receipts": [
                file_ref(p, output)
                for p in sorted(
                    (output / "raw/github/explicit-repositories" / name).glob(
                        "attempt-*/receipt.json"
                    )
                )
            ],
        }


def activity_record(row, output, network):
    name, branch = row["repo"], row["default_branch"]
    try:
        if not branch:
            raise ValueError("Default branch is absent")
        receipt, head = capture(
            f"repos/{name}/commits/{quote(branch, safe='')}",
            output / "raw/github/activity" / name / "head",
            output,
            network,
        )
        if not isinstance(head, dict):
            raise ValueError("Default branch response is not a commit object")
        sha = head.get("sha")
        if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
            raise ValueError("Head does not identify an immutable commit")
        date = head["commit"]["committer"]["date"]
        moment = datetime.fromisoformat(date.replace("Z", "+00:00"))
        upper = datetime.fromisoformat(receipt["completed_at"])
        cutoff = datetime.fromisoformat(ACTIVITY_CUTOFF.replace("Z", "+00:00"))
        row.update(
            default_branch_sha=sha,
            default_branch_commit_date=date,
            head_source={"body": receipt["body"], "receipt": receipt["receipt"]},
        )
        if moment > upper:
            raise ValueError("Default branch timestamp is later than capture")
        history = None
        if moment < cutoff:
            endpoint = f"repos/{name}/commits?sha={sha}&since={quote(ACTIVITY_CUTOFF,safe='')}&until={quote(receipt['completed_at'],safe='')}&per_page=1"
            history_ref, history = capture(
                endpoint, output / "raw/github/activity" / name / "recent-history", output, network
            )
            row["history_source"] = {"body": history_ref["body"], "receipt": history_ref["receipt"]}
            if not isinstance(history, list):
                raise ValueError("Recent history is not a list")
        repository = {
            "id": row["repository_id"],
            "full_name": name,
            "stargazers_count": row["stars"],
            "language": row["primary_language"],
            "default_branch": branch,
            **{key: row[key] for key in ("private", "fork", "archived")},
        }
        checked = population_check(
            repository,
            row,
            cutoff=ACTIVITY_CUTOFF[:10],
            head=head,
            history=history,
            observed_at=receipt["completed_at"],
        )
        row["population_status"] = {
            "eligible": "eligible",
            "ineligible": "excluded",
            "unavailable": "unknown",
        }[checked["status"]]
        row["exclusion_reasons"].extend(checked["exclusion_reasons"])
        row["issues"].extend(checked["issues"])
        row["activity_basis"] = "pinned_default_branch_commit_history"
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as exc:
        row["population_status"] = "unknown"
        row["issues"].append(str(exc))
    return row


def repository_rows(records, old_ids):
    by_id = defaultdict(list)
    for record in records:
        by_id[record["repository"]["id"]].append(record)
    rows = []
    for rid, observations in sorted(by_id.items()):
        observations.sort(
            key=lambda value: (
                value["metadata_source"]["observed_at"],
                value["repository"]["full_name"],
            )
        )
        repository = observations[0]["repository"]
        population = initial_population(repository)
        important = (
            "language",
            "private",
            "fork",
            "archived",
            "default_branch",
            "stargazers_count",
        )
        if any(
            any(item["repository"].get(k) != repository.get(k) for k in important)
            for item in observations[1:]
        ):
            population["status"] = "unknown"
            population["issues"].append("metadata_observations_disagree")
        sources = [origin for item in observations for origin in item["registry_sources"]]
        rows.append(
            {
                "repo": repository["full_name"],
                "repository_id": rid,
                "default_branch": repository.get("default_branch"),
                "default_branch_sha": None,
                "stars": repository.get("stargazers_count"),
                "primary_language": repository.get("language"),
                "private": repository.get("private"),
                "fork": repository.get("fork"),
                "archived": repository.get("archived"),
                "population_status": population["status"],
                "exclusion_reasons": population["exclusion_reasons"],
                "issues": population["issues"],
                "registry_sources": sources,
                "project_ids": sorted({s["project_id"] for s in sources}),
                "metadata_observations": [item["metadata_source"] for item in observations],
                "repository_aliases_observed": sorted(
                    {item["repository"]["full_name"] for item in observations}
                ),
                "overlap_old315": rid in old_ids,
                "ci_selection_status": "not_selected",
                "task_applicability": "not_reviewed",
                "campaign_ready": False,
            }
        )
    return rows


def checkpoint(organizations, frame, output, old_ids, network):
    records = [record for group in organizations for record in group["records"]]
    rows = repository_rows(records, old_ids)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda row: activity_record(row, output, network),
                [row for row in rows if row["population_status"] == "needs_activity"],
            )
        )
    for row in rows:
        row["new_candidate"] = row["population_status"] == "eligible" and not row["overlap_old315"]
    report = {
        "schema": "eclipse-incremental-metadata-v1",
        "complete": False,
        "organizations_observed": len(organizations),
        "organizations_declared": len(frame["organizations"]),
        "observation_date": OBSERVATION_DATE,
        "frame": file_ref(output / "registry-frame.json", output),
        "repositories": rows,
        "limitations": [
            "Incremental registry enumeration; explicit repository pass and full identity reconciliation still pending."
        ],
    }
    temporary = output / "metadata-candidates.partial.tmp.json"
    write_json(temporary, report)
    temporary.replace(output / "metadata-candidates.partial.json")
    print(f"Incremental new candidates: {sum(row['new_candidate'] for row in rows)}", flush=True)


def discover_metadata(frame, output, network=False):
    old_path = Path("output/java-benchmark-20260916/candidate-ledger.json").resolve()
    old = json.loads(old_path.read_bytes())
    old_ids = {item["repository_id"] for item in old["rows"]}
    organizations = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = {
            pool.submit(organization_repositories, org, frame, output, network): org
            for org in frame["organizations"]
        }
        for future in as_completed(pending):
            organizations.append(future.result())
            if len(organizations) % 25 == 0:
                print(
                    f"Captured organization metadata {len(organizations)}/{len(pending)}",
                    flush=True,
                )
                checkpoint(organizations, frame, output, old_ids, network)
    organizations.sort(key=lambda value: value["org"])
    records = [record for group in organizations for record in group["records"]]
    known_names = {record["repository"]["full_name"].lower() for record in records}
    explicit = []
    # Query every explicit repository not already present, including aliases and
    # absent repositories in a dedicated org. No membership is silently lost.
    remaining = {
        name: explicit_memberships(frame, name)
        for name in frame["explicit_repositories"]
        if name not in known_names and explicit_memberships(frame, name)
    }
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = {
            pool.submit(explicit_repository, name, origins, output, network): name
            for name, origins in remaining.items()
        }
        for future in as_completed(pending):
            explicit.append(future.result())
            if len(explicit) % 50 == 0:
                print(
                    f"Captured explicit repository metadata {len(explicit)}/{len(pending)}",
                    flush=True,
                )
    explicit.sort(key=lambda item: item["requested_repo"])
    records.extend(item["record"] for item in explicit if item["record"] is not None)
    write_json(
        output / "metadata-inventory.json", {"organizations": organizations, "explicit": explicit}
    )
    old_path = Path("output/java-benchmark-20260916/candidate-ledger.json").resolve()
    old = json.loads(old_path.read_bytes())
    old_ids = {item["repository_id"] for item in old["rows"]}
    rows = repository_rows(records, old_ids)
    eligible_to_check = [row for row in rows if row["population_status"] == "needs_activity"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = {
            pool.submit(activity_record, row, output, network): row["repo"]
            for row in eligible_to_check
        }
        for future in as_completed(pending):
            future.result()
    rows.sort(key=lambda value: value["repo"].lower())
    for row in rows:
        row["new_candidate"] = row["population_status"] == "eligible" and not row["overlap_old315"]
    report = {
        "schema": "eclipse-benchmark-metadata-v1",
        "complete": True,
        "completion_scope": "This captured registry frame enumeration; unknown resources remain explicit and foundation-wide completeness is not claimed.",
        "observation_date": OBSERVATION_DATE,
        "activity_cutoff": ACTIVITY_CUTOFF,
        "frame": file_ref(output / "registry-frame.json", output),
        "metadata_inventory": file_ref(output / "metadata-inventory.json", output),
        "previous_frame": {
            "path": str(old_path),
            "sha256": hashlib.sha256(old_path.read_bytes()).hexdigest(),
            "bytes": old_path.stat().st_size,
        },
        "selection": "Official registry membership, public nonfork nonarchived primary Java, stars strictly greater than 200, default-branch activity since the common cutoff. No CI outcome/size/test-record criterion is used here.",
        "summary": {
            "registry_projects": len(frame["projects"]),
            "declared_organizations": len(organizations),
            "organization_coverage": dict(Counter(item["status"] for item in organizations)),
            "repository_ids_observed": len(rows),
            "population_statuses": dict(Counter(row["population_status"] for row in rows)),
            "exclusion_reason_counts_nonexclusive": dict(
                Counter(reason for row in rows for reason in row["exclusion_reasons"])
            ),
            "unknown_reason_counts_nonexclusive": dict(
                Counter(reason for row in rows for reason in row["issues"])
            ),
            "unknown_without_known_exclusion": sum(
                row["population_status"] == "unknown" and not row["exclusion_reasons"]
                for row in rows
            ),
            "unknown_with_known_exclusion": sum(
                row["population_status"] == "unknown" and bool(row["exclusion_reasons"])
                for row in rows
            ),
            "new_candidates": sum(row["new_candidate"] for row in rows),
            "overlap_old315": sum(row["overlap_old315"] for row in rows),
            "explicit_repository_unknowns": sum(item["record"] is None for item in explicit),
            "registry_field_issues": len(frame["issues"]),
            "registry_ignored_memberships": sum(
                len(record["ignored_memberships"])
                for group in organizations
                for record in group["records"] + group["registry_ignored_records"]
            ),
            "registry_fully_excluded_repositories": sum(
                len(group["registry_ignored_records"]) for group in organizations
            ),
        },
        "unknown_frame_sources": [
            {key: group[key] for key in ("org", "status", "errors")}
            for group in organizations
            if group["errors"]
        ]
        + [item for item in explicit if item["errors"]]
        + frame["issues"],
        "repositories": rows,
        "limitations": frame["limitations"]
        + [
            "All eligible rows still require build-tool, Android/mixed-language/essential-Docker and official CI task applicability review.",
            "Unknown takes precedence when required metadata is absent, following the existing population checker; independently known exclusion reasons are also preserved and counted. These unknowns are not automatically plausible Java candidates.",
            "The foundation frame is not claimed globally exhaustive. Missing organizations/repositories remain explicit unknowns, not zero repositories.",
        ],
    }
    write_json(output / "candidates.json", report)
    lines = [
        "# Eclipse registry metadata discovery",
        "",
        report["selection"],
        "",
        "```json",
        json.dumps(report["summary"], indent=2),
        "```",
        "",
        "The organization list comes from a pinned Eclipse-maintained registry generator and five timestamped official PMI pages. An organization prefix was never used as evidence of membership. Shared organizations were restricted to their explicit registered repositories. Registered ignored repositories remain in the inventory with their exclusion source.",
        "",
        "This is metadata screening only. No project has yet been admitted as an executable benchmark task.",
        "",
        f"Among metadata-unknown repositories, {report['summary']['unknown_with_known_exclusion']} also have independently known disqualifying facts (for example stars <= 200), while {report['summary']['unknown_without_known_exclusion']} have no known exclusion yet. The former are not a queue of potentially eligible projects or pending CI collection.",
        "",
        "Ignored memberships apply per PMI project. The four Leda exclusions are explicitly registered under the distinct Leda Incubator project, so their membership source is the Incubator, not the excluding parent project.",
        "",
        "| New candidate | Stars | Default-branch SHA | Eclipse project IDs |",
        "|---|---:|---|---|",
    ]
    for row in rows:
        if row["new_candidate"]:
            lines.append(
                f"| {row['repo']} | {row['stars']} | {row['default_branch_sha']} | {', '.join(row['project_ids'])} |"
            )
    lines += ["", "## Limits", ""] + ["- " + limitation for limitation in report["limitations"]]
    (output / "report.md").write_text("\n".join(lines) + "\n")
    return report


def audit_transport(output):
    """Recheck all captured GitHub attempts, including unavailable public resources."""
    rows = []
    for path in sorted((output / "raw/github").glob("**/attempt-*/receipt.json")):
        receipt = json.loads(path.read_bytes())
        body = verify_ref(output, receipt["body"]).read_bytes()
        raw = verify_ref(output, receipt["raw_response"]).read_bytes()
        verify_ref(output, receipt["stderr"])
        status, response_body = split_http(raw)
        if status != receipt["http_status"] or response_body != body:
            raise ValueError("Receipt contradicts raw HTTP bytes")
        if receipt["method"] != "GET" or not receipt["url"].startswith("https://api.github.com/"):
            raise ValueError("Unexpected transport method or host")
        rows.append(
            {
                "receipt": file_ref(path, output),
                "status": status,
                "body_bytes": len(body),
                "url": receipt["url"],
            }
        )
    result = {
        "schema": "eclipse-transport-replay-v1",
        "github_receipts": len(rows),
        "github_http_statuses": dict(Counter(row["status"] for row in rows)),
        "github_body_bytes": sum(row["body_bytes"] for row in rows),
        "raw_body_and_headers_verified": True,
        "rows": rows,
    }
    write_json(output / "transport-audit.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("output/java-benchmark-org-expansion-20260922/eclipse")
    )
    parser.add_argument("--network", action="store_true")
    parser.add_argument(
        "--metadata",
        action="store_true",
        help="Enumerate the entire registry-declared public metadata frame",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    frame = registry_frame(output, args.network)
    result = discover_metadata(frame, output, args.network) if args.metadata else frame
    if args.metadata:
        audit_transport(output)
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
