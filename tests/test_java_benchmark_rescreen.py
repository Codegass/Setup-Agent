"""Population rules must not turn missing evidence or target failure into exclusion."""
from copy import deepcopy
import hashlib
import json

import pytest

from scripts.rescreen_java_benchmark import population_check, response, unique_index


def fixture():
    repo = {"id": 1, "full_name": "org/repo", "default_branch": "main", "stargazers_count": 201,
            "language": "Java", "private": False, "archived": False, "fork": False}
    screen = {"repo": "org/repo", "repository_id": 1, "default_branch": "main",
              "default_branch_sha": "a" * 40, "default_branch_commit_date": "2026-09-01T00:00:00Z"}
    head = {"sha": "a" * 40, "commit": {"committer": {"date": screen["default_branch_commit_date"]}}}
    return repo, screen, head


def check(repo, screen, head, **kwargs):
    return population_check(repo, screen, head=head, cutoff="2025-09-16", observed_at="2026-09-17T00:00:00Z", **kwargs)


@pytest.mark.parametrize("stars,status", [(200, "ineligible"), (201, "eligible"), (None, "unavailable"), (True, "unavailable")])
def test_strict_user_star_threshold_and_missingness(stars, status):
    repo, screen, head = fixture(); repo["stargazers_count"] = stars
    assert check(repo, screen, head)["status"] == status


def test_old_head_requires_history_and_empty_verified_history_can_exclude():
    repo, screen, head = fixture()
    head["commit"]["committer"]["date"] = screen["default_branch_commit_date"] = "2020-01-01T00:00:00Z"
    assert check(repo, screen, head)["status"] == "unavailable"
    assert check(repo, screen, head, history=[])["status"] == "ineligible"
    recent = {"commit": {"committer": {"date": "2026-01-01T00:00:00Z"}}}
    assert check(repo, screen, head, history=[recent])["status"] == "eligible"


def test_future_timestamp_or_wrong_repository_cannot_establish_activity():
    repo, screen, head = fixture()
    changed = deepcopy(head); changed["commit"]["committer"]["date"] = "2030-01-01T00:00:00Z"
    screen["default_branch_commit_date"] = "2030-01-01T00:00:00Z"
    assert check(repo, screen, changed)["status"] == "unavailable"
    repo["id"] = 2
    assert check(repo, screen, head)["status"] == "unavailable"


def test_docker_filenames_or_historical_target_failure_are_not_population_exclusions():
    repo, screen, head = fixture()
    screen.update(android_named_paths=["optional/AndroidManifest.xml"], state="target_inapplicable", reasons=["DOCKER_REQUIRED"])
    assert check(repo, screen, head)["status"] == "eligible"


def test_duplicate_ids_are_reported_instead_of_silently_last_wins():
    index, errors = unique_index([{"id": "x"}, {"id": "x"}, {"id": "y"}], "id")
    assert set(index) == {"y"} and errors == ["duplicate_or_missing_id:x"]


def test_raw_capture_is_byte_and_scope_bound(tmp_path):
    raw = b'{"sha":"abc"}'
    path = tmp_path / "head.json"; path.write_bytes(raw)
    meta = {"returncode": 0, "url": "https://api.github.com/repos/org/repo/commits/main",
            "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    path.with_suffix(".json.meta.json").write_text(json.dumps(meta))
    evidence = []
    assert response(tmp_path, "head.json", evidence, expected_path="/repos/org/repo/commits/main")[0]["sha"] == "abc"
    assert len(evidence) == 2
    with pytest.raises(ValueError): response(tmp_path, "head.json", [], expected_path="/repos/other/repo/commits/main")
    path.write_bytes(b'{}')
    with pytest.raises(ValueError, match="bytes"): response(tmp_path, "head.json", [])
