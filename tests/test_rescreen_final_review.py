"""Independent rescreen review: preserve grain, missingness and fixed selection."""

import hashlib
import json

import pytest

from scripts.assemble_benchmark_rescreen import merge
from scripts.rescreen_java_benchmark import audit, population_check


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def bound(path):
    raw = path.read_bytes()
    return {"path": path.name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def assembly_fixture(tmp_path):
    source, audits = tmp_path / "old", tmp_path / "audits"
    tasks = [
        {"task_id": "a-main", "repo": "org/a", "sha": "a" * 40},
        {"task_id": "a-alternative", "repo": "org/a", "sha": "b" * 40},
        {"task_id": "b-main", "repo": "org/b", "sha": "c" * 40},
    ]
    for row in tasks:
        row.update(official_ci_url="https://ci.example/" + row["task_id"], tool="maven")
    projects = []
    for i, row in enumerate((tasks[0], tasks[2]), 1):
        projects.append(
            {
                "repo": row["repo"],
                "repository_id": i,
                "organization": "org",
                "canonical_task_id": row["task_id"],
                "stars": 201,
                "development_exposure": False,
                "state": "static_qualified",
            }
        )
    save(source / "dataset.json", {"projects": projects, "reference_tasks": tasks})
    dataset_ref = bound(source / "dataset.json")
    candidates = [
        {
            "repo": f"org/{name}",
            "population": {"status": "eligible"},
            "reference_candidates": [r["task_id"] for r in tasks if r["repo"] == f"org/{name}"],
        }
        for name in "abcde"
    ]
    save(
        audits / "cohort-audit/audit.json",
        {
            "sources": [dataset_ref],
            "candidates": candidates,
            "summary": {"population_statuses": {"eligible": 5}},
            "observation": {"refreshed_live": False},
            "global_issues": ["capture_receipt_missing:one_page"],
        },
    )
    source_rows, req_rows, test_rows = [], [], []
    for row, ploc in zip(tasks, [10, 1000000, 30]):
        source_rows.append(
            {
                **row,
                "source_completeness": "java_content_verified",
                "measured_size": {"java_physical_lines": ploc, "java_files": 1},
                "collection_gaps": [],
                "issues": [],
            }
        )
        req_rows.append(
            {
                **row,
                "labels": [{"kind": "test", "status": "observed"}] * 3,
                "build_endpoint": "install",
                "requirements_readiness": "review_required",
                "unknowns": ["plan unresolved"],
            }
        )
        test_rows.append(
            {
                **row,
                "count_audit": {"status": "confirmed"},
                "identity_audit": {
                    "complete_case_records_available": True,
                    "unambiguous_observed_identity_supported": True,
                    "collision_key_count": 0,
                },
            }
        )
    for name, rows in [
        ("source-audit", source_rows),
        ("requirement-audit", req_rows),
        ("test-evidence-audit", test_rows),
    ]:
        save(audits / name / "audit.json", {"dataset_ref": dataset_ref, "tasks": rows})
    return source, audits


def test_canonical_grain_never_switches_to_more_informative_alternative(tmp_path):
    source, audits = assembly_fixture(tmp_path)
    result, queue = merge(source, audits)
    assert result["summary"]["candidates"] == 5
    assert len(result["projects"]) == 2 and len(result["tasks"]) == 3
    assert len(queue["other_candidates"]) == 3
    assert [p["canonical_task_id"] for p in result["projects"]] == ["a-main", "b-main"]
    assert (result["size_thresholds"]["q1"], result["size_thresholds"]["q3"]) == (15, 25)
    assert result["size_thresholds"]["verified_projects"] == 2
    assert result["size_thresholds"]["is_admission_threshold"] is False
    assert result["cohort_provenance_issues"] == ["capture_receipt_missing:one_page"]
    assert result["summary"]["ci_job_category_project_counts"] == {"test": 2}
    assert result["summary"]["requirements_readiness_all_tasks"] == {"review_required": 3}
    assert result["summary"]["campaign_ready_projects"] == 0
    assert all(p["campaign_ready"] is False for p in result["projects"])
    assert all(p["reference_replay_verified"] is False for p in result["projects"])


@pytest.mark.parametrize(
    "gap",
    [
        "Review non-Java byte differences against export/line-ending attributes",
        "Resolve omitted non-Java blobs against task scope",
        "Recover pinned submodule content before asserting a complete checkout",
    ],
)
def test_checkout_gaps_survive_without_changing_verified_java_size(tmp_path, gap):
    source, audits = assembly_fixture(tmp_path)
    before, _ = merge(source, audits)
    path = audits / "source-audit/audit.json"
    data = json.loads(path.read_bytes())
    data["tasks"][0]["collection_gaps"] = [gap]
    save(path, data)

    after, queue = merge(source, audits)
    project = next(p for p in after["projects"] if p["canonical_task_id"] == "a-main")
    assert project["source_completeness"] == "java_content_verified"
    assert project["source_size"] == before["projects"][0]["source_size"]
    assert project["size_stratum"] == before["projects"][0]["size_stratum"]
    assert after["size_thresholds"] == before["size_thresholds"]
    assert project["checkout_collection_gaps"] == [gap]
    assert project["campaign_ready"] is False
    queued = next(t for t in queue["tasks"] if t["task_id"] == "a-main")
    checks = [item for item in queued["items"] if item["kind"] == "checkout_scope_review"]
    assert len(checks) == 1 and checks[0]["basis"] == [gap]
    assert all(item["kind"] != "source_commit_closure" for item in queued["items"])


@pytest.mark.parametrize("mutation", ["dataset_hash", "task_sha", "task_repo", "duplicate_task"])
def test_cross_audit_bindings_reject_stale_or_colliding_sources(tmp_path, mutation):
    source, audits = assembly_fixture(tmp_path)
    path = audits / "source-audit/audit.json"
    data = json.loads(path.read_bytes())
    if mutation == "dataset_hash":
        data["dataset_ref"]["sha256"] = "0" * 64
    elif mutation == "task_sha":
        data["tasks"][0]["sha"] = "d" * 40
    elif mutation == "task_repo":
        data["tasks"][0]["repo"] = "org/other"
    else:
        data["tasks"].append(data["tasks"][0])
    save(path, data)
    with pytest.raises(ValueError):
        merge(source, audits)


def test_future_history_cannot_establish_archived_activity():
    repo = {
        "id": 1,
        "full_name": "org/repo",
        "default_branch": "main",
        "stargazers_count": 201,
        "language": "Java",
        "private": False,
        "archived": False,
        "fork": False,
    }
    screen = {
        "repo": "org/repo",
        "repository_id": 1,
        "default_branch": "main",
        "default_branch_sha": "a" * 40,
        "default_branch_commit_date": "2020-01-01T00:00:00Z",
    }
    head = {
        "sha": "a" * 40,
        "commit": {"committer": {"date": screen["default_branch_commit_date"]}},
    }
    future = {"commit": {"committer": {"date": "2030-01-01T00:00:00Z"}}}
    result = population_check(
        repo,
        screen,
        head=head,
        cutoff="2025-09-16",
        observed_at="2026-09-17T00:00:00Z",
        history=[future],
    )
    assert result["status"] == "unavailable"


def test_missing_transport_receipt_retains_frame_but_never_invents_capture(tmp_path):
    base = tmp_path / "old"
    repo = {
        "id": 1,
        "full_name": "org/repo",
        "default_branch": "main",
        "stargazers_count": 201,
        "language": "Java",
        "private": False,
        "archived": False,
        "fork": False,
    }
    head = {"sha": "a" * 40, "commit": {"committer": {"date": "2026-09-01T00:00:00Z"}}}
    screen = {
        "repo": "org/repo",
        "repository_id": 1,
        "default_branch": "main",
        "default_branch_sha": "a" * 40,
        "default_branch_commit_date": "2026-09-01T00:00:00Z",
    }
    save(base / "screen.json", screen)
    save(base / "raw/repos/org/repo/head.json", head)
    head_ref = bound(base / "raw/repos/org/repo/head.json")
    save(
        base / "raw/repos/org/repo/head.json.meta.json",
        {
            **head_ref,
            "returncode": 0,
            "url": "https://api.github.com/repos/org/repo/commits/main",
            "fetched_at": "2026-09-17T00:00:00Z",
        },
    )
    save(
        base / "raw/github/org-search-1.json",
        {"total_count": 1, "incomplete_results": False, "items": [repo]},
    )
    search_ref = bound(base / "raw/github/org-search-1.json")
    save(
        base / "discovery-integrity.json",
        {"raw_pages": [{**search_ref, "file": "raw/github/org-search-1.json"}]},
    )
    save(base / "dataset.json", {"projects": [], "reference_tasks": []})
    save(
        base / "candidate-ledger.json",
        {
            "rows": [
                {
                    "repo": "org/repo",
                    "repository_id": 1,
                    "organization": "org",
                    "state": "needs_review",
                    "discovery_screening": "screen.json",
                    "reviews": [],
                    "qualified_task_ids": [],
                    "development_exposure": False,
                }
            ]
        },
    )
    save(
        base / "discovery.json",
        {
            "repositories": [repo],
            "queries": [
                {"org": "org", "pages": 1, "query": "org:org language:Java", "total_count": 1}
            ],
            "cutoff": "2025-09-16",
            "collected_at": "2026-09-17T00:00:00+00:00",
        },
    )
    (base / "PROTOCOL.md").write_text("Existing collection protocol")
    result = audit(base)
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["previous_state"] == "needs_review"
    assert result["candidates"][0]["campaign_ready"] is False
    assert result["discovery_frame"][0]["status"] == "unavailable"
    assert any("capture_receipt_missing" in issue for issue in result["global_issues"])
    assert not (base / "raw/github/org-search-1.json.meta.json").exists()
