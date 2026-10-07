"""The expansion sampler keeps selection uncertainty separate from admission."""
import json

import pytest

from scripts import collect_fasterxml_expansion as collector


def metadata(**changes):
    return {"private": False, "fork": False, "archived": False,
            "language": "Java", "stargazers_count": 201, **changes}


@pytest.mark.parametrize("stars,reasons,issues", [
    (200, ["stars_not_greater_than_200"], []),
    (201, [], []), (True, [], ["stars_unknown"]), (None, [], ["stars_unknown"]),
])
def test_strict_star_boundary_and_unknown_type(stars, reasons, issues):
    assert collector.metadata_eligibility(metadata(stargazers_count=stars)) == (reasons, issues)


def test_null_language_is_unknown_not_non_java():
    assert collector.metadata_eligibility(metadata(language=None)) == ([], ["primary_language_unknown"])


def test_default_goal_and_reusable_build_actions_are_not_task_absence():
    source = """jobs:
  ordinary:
    runs-on: ubuntu-latest
    steps:
      - run: mvn --batch-mode
      - uses: ./build-action
  delegated:
    uses: org/repo/.github/workflows/build.yml@main
"""
    rows = collector.workflow_unknowns(source)
    assert {r["reason"] for r in rows} == {
        "goal_or_defaultGoal_scope_requires_review", "action_scope_not_inferred_from_uses",
        "reusable_workflow_scope_unreviewed"}
    assert collector.workflow_commands(source)[0]["contains_build_tool"] is True


def test_linux_matrix_mapping_does_not_require_retained_steps_or_green():
    commands = collector.workflow_commands("""jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Build
        run: ./mvnw verify
""")
    candidate = {"ordinary_build_test_command_candidates": commands}
    jobs = [
        {"name": "build (8)", "id": 1, "html_url": "https://example/1", "labels": ["ubuntu-latest"],
         "steps": [], "status": "completed", "conclusion": "success", "completed_at": "2026-09-22T01:00:00Z"},
        {"name": "build (17)", "id": 2, "html_url": "https://example/2", "labels": ["ubuntu-latest"],
         "steps": [], "status": "completed", "conclusion": "failure", "completed_at": "2026-09-22T01:01:00Z"},
    ]
    matches = collector.eligible_jobs(candidate, jobs)
    assert [job["id"] for job, _ in matches] == [2, 1]


def test_ambiguous_workflow_job_names_are_not_resolved_by_step_name():
    source = """jobs:
  first:
    steps:
      - name: Build
        run: mvn verify
  second:
    steps:
      - name: Build
        run: mvn test
"""
    candidate = {"ordinary_build_test_command_candidates": collector.workflow_commands(source)}
    job = {"name": "unmapped", "labels": ["ubuntu-latest"], "steps": [{"name": "Build"}],
           "status": "completed", "completed_at": "2026-09-22T01:00:00Z", "html_url": "https://example/1"}
    assert collector.eligible_jobs(candidate, [job]) == []


def test_enumeration_count_drift_is_not_marked_complete(tmp_path, monkeypatch):
    def fetch(*args, **kwargs):
        return {"total_count": 2, "workflow_runs": [{"id": 1}]}, {"link": None}
    monkeypatch.setattr(collector, "fetch", fetch)
    runs, extent = collector.enumerate_runs("org/repo", "main", "2026-09-01T00:00:00Z", tmp_path)
    assert runs == [{"id": 1}]
    assert extent["complete_within_api_retention"] is False
    assert extent["consistency_issues"][0]["reason"] == "run_pagination_count_or_id_drift"


def test_api_host_cannot_drift_with_gh_host_environment(tmp_path, monkeypatch):
    seen = []
    class Result:
        stdout = b"HTTP/2.0 200 OK\r\n\r\n{}"
        stderr = b""
        returncode = 0
    def run(argv, **kwargs):
        seen.append(argv)
        return Result()
    monkeypatch.setattr(collector.subprocess, "run", run)
    collector.fetch("orgs/FasterXML", tmp_path / "org.json")
    assert seen[0][seen[0].index("--hostname") + 1] == "github.com"


def test_created_query_cannot_erase_newer_previously_observed_run(tmp_path):
    directory = tmp_path / "selection-v2" / "runs"
    directory.mkdir(parents=True)
    manifest = directory / "enumeration.json"
    manifest.write_text("{}")
    previous = tmp_path / "default-branch-runs"
    previous.mkdir()
    old = {"id": 1, "status": "completed", "head_branch": "main",
           "created_at": "2026-06-01T00:00:00Z", "updated_at": "2026-06-01T00:01:00Z"}
    newer = {**old, "id": 2, "created_at": "2026-09-22T00:00:00Z", "updated_at": "2026-09-22T00:01:00Z"}
    path = previous / "page-1.json"
    path.write_text(json.dumps({"workflow_runs": [newer]}))
    path.with_name(path.name + ".receipt.json").write_text(json.dumps({
        "sha256": collector.digest(path.read_bytes()), "url": "https://api.github.com/repos/org/repo/actions/runs?branch=main"}))
    runs, extent = collector.reconcile_enumeration([old], {
        "created_to": "2026-09-22T01:00:00+00:00", "complete_within_api_retention": True,
        "consistency_issues": []}, "main", directory, manifest)
    assert {x["id"] for x in runs} == {1, 2}
    assert extent["complete_within_api_retention"] is False
    assert extent["consistency_issues"][0]["reason"] == "created_range_query_omits_previously_observed_in_range_runs"


def test_cached_only_does_not_make_additional_history_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "CACHED_RUNS_ONLY", True)
    monkeypatch.setattr(collector, "fetch", lambda *a, **kw: pytest.fail("must not request history"))
    runs, extent = collector.enumerate_runs("org/repo", "main", "2026-09-01T00:00:00Z", tmp_path)
    assert runs == []
    assert extent["complete_within_api_retention"] is False
