from copy import deepcopy
from pathlib import Path

import pytest

from scripts import benchmark_eclipse_discovery as d


def repository(**changes):
    return {
        "id": 1,
        "full_name": "org/repo",
        "private": False,
        "fork": False,
        "archived": False,
        "language": "Java",
        "stargazers_count": 201,
        "default_branch": "main",
        "owner": {"id": 7},
        **changes,
    }


def record(repo=None, project="p"):
    return {
        "repository": repo or repository(),
        "registry_sources": [{"project_id": project}],
        "metadata_source": {"observed_at": "2026-09-22T00:00:00+00:00"},
    }


@pytest.mark.parametrize(
    "stars,status",
    [(200, "excluded"), (201, "needs_activity"), (None, "unknown"), (True, "unknown")],
)
def test_stars_are_strict_nonboolean(stars, status):
    assert d.initial_population(repository(stargazers_count=stars))["status"] == status


def test_unknown_and_known_exclusion_are_both_retained():
    result = d.initial_population(repository(language=None, archived=True))
    assert result["status"] == "unknown"
    assert result["issues"] == ["primary_language_unknown"]
    assert result["exclusion_reasons"] == ["archived"]


def test_same_repository_id_is_not_a_second_project_and_conflicts_not_cherry_picked():
    a = record()
    b = record(repository(full_name="renamed/repo"), "second")
    rows = d.repository_rows([a, b], {1})
    assert len(rows) == 1 and rows[0]["overlap_old315"]
    assert rows[0]["project_ids"] == ["p", "second"]
    b["repository"]["stargazers_count"] = 200
    row = d.repository_rows([a, b], set())[0]
    assert row["population_status"] == "unknown"
    assert "metadata_observations_disagree" in row["issues"]


def test_ignore_is_scoped_to_its_project_not_explicit_other_membership():
    frame = {
        "organizations": {"org": [{"project_id": "first", "ignored_repos": ["repo"]}]},
        "explicit_repositories": {"org/repo": [{"project_id": "second"}]},
    }
    keep, ignore = d.registry_memberships(frame, "ORG", "org/repo")
    assert keep == [{"project_id": "second"}]
    assert ignore[0]["project_id"] == "first"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com.evil/org/repo",
        "https://github.com/org /repo",
        "https://github.com/org",
        "https://other.org/org/repo",
    ],
)
def test_registry_membership_does_not_guess_invalid_urls(url):
    with pytest.raises(ValueError):
        d.repository_name(url)


def fake_capture(tmp_path, responses):
    def capture(endpoint, directory, root, network):
        payload, headers = responses.pop(0)
        raw = tmp_path / f"response-{len(responses)}.http"
        raw.write_bytes(b"HTTP/2.0 200 OK\r\n" + headers + b"\r\n\r\n[]")
        return {
            "body": {"path": "body.json"},
            "receipt": {"path": "receipt.json"},
            "raw_response": {"path": raw.name},
            "completed_at": "2026-09-22T12:00:00+00:00",
        }, payload

    return capture


@pytest.mark.parametrize("second", [[repository()], []])
def test_duplicate_or_broken_pagination_not_complete(tmp_path, monkeypatch, second):
    link = b'Link: <https://api.github.com/orgs/org/repos?per_page=100&page=2>; rel="next"'
    # Duplicate ID on page 2 or empty page that incorrectly promises page 3.
    third = b'Link: <https://api.github.com/orgs/org/repos?per_page=100&page=3>; rel="next"'
    responses = [([repository()], link), (second, third)]
    monkeypatch.setattr(d, "capture", fake_capture(tmp_path, responses))
    frame = {
        "organizations": {"org": [{"project_id": "p", "ignored_repos": []}]},
        "explicit_repositories": {},
    }
    result = d.organization_repositories("org", frame, tmp_path, False)
    assert result["status"] == "unknown_or_partial" and result["errors"]


def test_page_jump_is_rejected(tmp_path, monkeypatch):
    link = b'Link: <https://api.github.com/orgs/org/repos?per_page=100&page=3>; rel="next"'
    monkeypatch.setattr(d, "capture", fake_capture(tmp_path, [([repository()], link)]))
    frame = {
        "organizations": {"org": [{"project_id": "p", "ignored_repos": []}]},
        "explicit_repositories": {},
    }
    result = d.organization_repositories("org", frame, tmp_path, False)
    assert result["status"] == "unknown_or_partial"
    assert "page scope" in result["errors"][0]


@pytest.mark.parametrize(
    "date,history,status",
    [
        ("2025-09-22T00:00:00Z", None, "eligible"),
        ("2025-09-21T23:59:59Z", [], "excluded"),
        ("2027-01-01T00:00:00Z", None, "unknown"),
        (
            "2025-01-01T00:00:00Z",
            [{"commit": {"committer": {"date": "2027-01-01T00:00:00Z"}}}],
            "unknown",
        ),
    ],
)
def test_activity_boundaries(tmp_path, monkeypatch, date, history, status):
    row = d.repository_rows([record()], set())[0]
    head = {"sha": "a" * 40, "commit": {"committer": {"date": date}}}
    responses = [(head, b"")] + ([(history, b"")] if history is not None else [])
    monkeypatch.setattr(d, "capture", fake_capture(tmp_path, responses))
    result = d.activity_record(row, tmp_path, False)
    assert result["population_status"] == status
    assert result["default_branch_sha"] == "a" * 40


def test_missing_metadata_is_visible(tmp_path, monkeypatch):
    def failed(*args):
        raise ValueError("HTTP 404")

    monkeypatch.setattr(d, "capture", failed)
    result = d.explicit_repository("org/repo", [{"project_id": "p"}], tmp_path, False)
    assert result["record"] is None and result["errors"] == ["HTTP 404"]
    assert result["registry_sources"] == [{"project_id": "p"}]


@pytest.mark.parametrize(
    "url", ["https://github.com/../repo", "https://github.com/org/..", "https://github.com/./repo"]
)
def test_registry_path_cannot_escape_output(url):
    with pytest.raises(ValueError):
        d.repository_name(url)


def test_same_project_explicit_list_does_not_undo_its_ignore():
    first = {"project_id": "first", "ignored_repos": ["org/repo"]}
    frame = {
        "organizations": {"org": [first]},
        "explicit_repositories": {"org/repo": [{"project_id": "first"}]},
    }
    keep, ignored = d.registry_memberships(frame, "org", "org/repo")
    assert keep == [] and ignored == [first]
    assert d.explicit_memberships(frame, "org/repo") == []
    frame["explicit_repositories"]["org/repo"].append({"project_id": "second"})
    assert d.explicit_memberships(frame, "org/repo") == [{"project_id": "second"}]
