"""The d3 anchor selector: which commit's CI record is worth harvesting.

Every response here is a literal modeled on the real shapes in the 2026-08-27
probe archive (`d3_anchor_candidates.json`): commons-dbcp's `build (17, false)`
matrix with its one live red cell, geode's CodeQL-only pseudo-anchor, and
cassandra-java-driver's lone Jenkins commit status.  The network is never
touched -- the module's single `fetch` function is replaced wholesale.
"""

from __future__ import annotations

import json

import pytest

from scripts.d3_select_anchor import (
    SelectorError,
    is_noise,
    main,
    select_anchor,
)

REPO = "apache/demo"
HEAD_SHA = "aaaaaaa1111111111111111111111111111111a1"
MIDDLE_SHA = "bbbbbbb2222222222222222222222222222222b2"
OLDEST_SHA = "ccccccc3333333333333333333333333333333c3"


class FakeApi:
    """One canned GitHub API, standing in for the module's ``fetch``."""

    def __init__(self, bodies: dict[str, object]) -> None:
        self.bodies = dict(bodies)
        self.calls: list[str] = []

    def __call__(self, path: str) -> object:
        self.calls.append(path)
        if path not in self.bodies:
            raise AssertionError(f"the selector asked for an unexpected path: {path}")
        return self.bodies[path]


def _commit(sha: str, date: str) -> dict[str, object]:
    return {"sha": sha, "commit": {"committer": {"date": date}, "author": {"date": date}}}


def _check_runs(*runs: tuple[str, str]) -> dict[str, object]:
    return {
        "total_count": len(runs),
        "check_runs": [
            {"name": name, "status": "completed", "conclusion": conclusion}
            for name, conclusion in runs
        ],
    }


def _statuses(*statuses: tuple[str, str]) -> dict[str, object]:
    return {
        "state": "success",
        "statuses": [{"context": context, "state": state} for context, state in statuses],
    }


def _bodies(
    *,
    commits: list[dict[str, object]],
    signals: dict[str, tuple[dict[str, object], dict[str, object]]],
    max_commits: int = 15,
    branch: str = "main",
    with_default_branch: bool = True,
) -> dict[str, object]:
    bodies: dict[str, object] = {
        f"repos/{REPO}/commits?sha={branch}&per_page={max_commits}": commits,
    }
    if with_default_branch:
        bodies[f"repos/{REPO}"] = {"default_branch": branch}
    for sha, (checks, statuses) in signals.items():
        bodies[_checks_path(sha)] = checks
        bodies[_status_path(sha)] = statuses
    return bodies


def _checks_path(sha: str, page: int = 1) -> str:
    return f"repos/{REPO}/commits/{sha}/check-runs?per_page=100&page={page}"


def _status_path(sha: str, page: int = 1) -> str:
    return f"repos/{REPO}/commits/{sha}/status?per_page=100&page={page}"


@pytest.fixture
def api(monkeypatch):
    def install(bodies: dict[str, object]) -> FakeApi:
        fake = FakeApi(bodies)
        monkeypatch.setattr("scripts.d3_select_anchor.fetch", fake)
        return fake

    return install


class TestNoiseFilter:
    @pytest.mark.parametrize(
        "name",
        [
            "CodeQL",
            "Analyze (java)",
            "Dependabot updates",
            "Copilot code review",
            "Dependency Review",
            "add-labels",
            "Close stale PRs",
            "Deploy docs",
            "website build",
            "SonarCloud",
            "triage",
            "lint-pr-title",
            "semantic-pull-request",
            "DCO",
            "license/cla",
            "notify-mailing-list",
        ],
    )
    def test_the_flows_that_run_on_every_commit_are_noise(self, name):
        assert is_noise(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "build (17, false)",
            "JDK 25, DB postgres",
            "acceptanceTest (ubuntu-latest, liberica, 17)",
            "build / JUnit tests Java 17",
            "Scorecards analysis",
        ],
    )
    def test_a_build_or_test_cell_is_not_noise(self, name):
        assert is_noise(name) is False


class TestSelectAnchor:
    def test_the_newest_commit_with_real_checks_wins(self, api):
        fake = api(
            _bodies(
                commits=[
                    _commit(HEAD_SHA, "2026-08-27T17:41:11Z"),
                    _commit(MIDDLE_SHA, "2026-08-26T09:02:00Z"),
                ],
                signals={
                    HEAD_SHA: (
                        _check_runs(
                            ("build (17, false)", "success"),
                            ("build (28-ea, true)", "failure"),
                        ),
                        _statuses(),
                    ),
                },
            )
        )
        anchor = select_anchor(REPO)

        assert anchor["repo"] == REPO
        assert anchor["sha"] == HEAD_SHA
        assert anchor["date"] == "2026-08-27T17:41:11Z"
        assert anchor["checks"] == [
            {"name": "build (17, false)", "conclusion": "success"},
            {"name": "build (28-ea, true)", "conclusion": "failure"},
        ]
        assert anchor["statuses"] == []
        assert anchor["noise_filtered"] is True
        # The walk stopped at HEAD: no older commit was ever asked about.
        assert not any(MIDDLE_SHA in call for call in fake.calls)

    def test_a_red_check_is_evidence_and_anchors_the_commit(self, api):
        api(
            _bodies(
                commits=[_commit(HEAD_SHA, "2026-08-27T17:41:11Z")],
                signals={HEAD_SHA: (_check_runs(("build (8, false)", "failure")), _statuses())},
            )
        )
        anchor = select_anchor(REPO)
        assert anchor["checks"] == [{"name": "build (8, false)", "conclusion": "failure"}]

    def test_noise_only_commits_are_walked_past(self, api):
        api(
            _bodies(
                commits=[
                    _commit(HEAD_SHA, "2026-08-27T17:41:11Z"),
                    _commit(MIDDLE_SHA, "2026-08-26T09:02:00Z"),
                    _commit(OLDEST_SHA, "2026-08-25T08:00:00Z"),
                ],
                signals={
                    # geode's pseudo-anchor: four green checks, all CodeQL.
                    HEAD_SHA: (
                        _check_runs(("Analyze (java)", "success"), ("CodeQL", "success")),
                        _statuses(),
                    ),
                    MIDDLE_SHA: (
                        _check_runs(("Dependabot updates", "success")),
                        _statuses(("license/cla", "success")),
                    ),
                    OLDEST_SHA: (
                        _check_runs(("build / JUnit tests Java 17", "success")),
                        _statuses(),
                    ),
                },
            )
        )
        anchor = select_anchor(REPO)

        assert anchor["sha"] == OLDEST_SHA
        assert anchor["checks"] == [
            {"name": "build / JUnit tests Java 17", "conclusion": "success"}
        ]

    def test_a_commit_status_alone_anchors_when_no_check_runs_exist(self, api):
        api(
            _bodies(
                commits=[_commit(HEAD_SHA, "2026-07-08T20:12:05Z")],
                signals={
                    HEAD_SHA: (
                        _check_runs(),
                        _statuses(("continuous-integration/jenkins/branch", "failure")),
                    )
                },
            )
        )
        anchor = select_anchor(REPO)

        assert anchor["sha"] == HEAD_SHA
        assert anchor["statuses"] == [
            {"context": "continuous-integration/jenkins/branch", "state": "failure"}
        ]

    def test_a_check_that_decided_nothing_is_not_evidence(self, api):
        api(
            _bodies(
                commits=[
                    _commit(HEAD_SHA, "2026-08-27T17:41:11Z"),
                    _commit(MIDDLE_SHA, "2026-08-26T09:02:00Z"),
                ],
                signals={
                    HEAD_SHA: (
                        {
                            "check_runs": [
                                {"name": "build (17, false)", "status": "in_progress"},
                                {
                                    "name": "build (21, false)",
                                    "status": "completed",
                                    "conclusion": "skipped",
                                },
                            ]
                        },
                        _statuses(("continuous-integration/jenkins/branch", "pending")),
                    ),
                    MIDDLE_SHA: (
                        _check_runs(("build (17, false)", "success")),
                        _statuses(),
                    ),
                },
            )
        )
        assert select_anchor(REPO)["sha"] == MIDDLE_SHA

    def test_no_anchor_in_the_window_is_an_error_that_names_the_window(self, api):
        api(
            _bodies(
                commits=[_commit(HEAD_SHA, "2026-08-27T17:41:11Z")],
                signals={HEAD_SHA: (_check_runs(("CodeQL", "success")), _statuses())},
            )
        )
        with pytest.raises(SelectorError) as excinfo:
            select_anchor(REPO)
        assert "newest 1" in str(excinfo.value)
        assert REPO in str(excinfo.value)

    def test_an_explicit_branch_skips_the_default_branch_lookup(self, api):
        fake = api(
            _bodies(
                commits=[_commit(HEAD_SHA, "2026-08-27T17:41:11Z")],
                signals={HEAD_SHA: (_check_runs(("build (17, false)", "success")), _statuses())},
                branch="4.3",
                with_default_branch=False,
            )
        )
        assert select_anchor(REPO, branch="4.3")["sha"] == HEAD_SHA
        assert f"repos/{REPO}" not in fake.calls

    def test_the_window_is_the_max_commits_the_caller_asked_for(self, api):
        fake = api(
            _bodies(
                commits=[_commit(HEAD_SHA, "2026-08-27T17:41:11Z")],
                signals={HEAD_SHA: (_check_runs(("build (17, false)", "success")), _statuses())},
                max_commits=3,
            )
        )
        select_anchor(REPO, max_commits=3)
        assert f"repos/{REPO}/commits?sha=main&per_page=3" in fake.calls

    def test_a_malformed_repo_is_refused_before_any_call(self, api):
        fake = api({})
        with pytest.raises(SelectorError):
            select_anchor("kafka")
        assert fake.calls == []


class TestSignalPagination:
    """A commit's CI record is read whole, not one API page of it.

    GitHub hands back 30 check runs per page by default and 100 at most, and
    busy repositories put more than that on a single commit.  A build cell that
    lands behind a page of bot noise still has to count as evidence, and the
    anchor it produces still has to carry every cell.
    """

    @staticmethod
    def _noise_page(count: int) -> dict[str, object]:
        return {
            "check_runs": [
                {"name": f"CodeQL scan {index}", "status": "completed", "conclusion": "success"}
                for index in range(count)
            ]
        }

    def test_a_build_cell_on_the_second_page_of_checks_is_still_evidence(self, api):
        bodies = _bodies(
            commits=[_commit(HEAD_SHA, "2026-08-27T17:41:11Z")],
            signals={HEAD_SHA: (_check_runs(), _statuses())},
        )
        bodies[_checks_path(HEAD_SHA)] = self._noise_page(100)
        bodies[_checks_path(HEAD_SHA, page=2)] = _check_runs(
            ("build (17, false)", "success"),
            ("build (28-ea, true)", "failure"),
        )
        api(bodies)

        anchor = select_anchor(REPO)

        assert anchor["sha"] == HEAD_SHA
        assert anchor["checks"] == [
            {"name": "build (17, false)", "conclusion": "success"},
            {"name": "build (28-ea, true)", "conclusion": "failure"},
        ]

    def test_a_jenkins_status_on_the_second_page_is_still_evidence(self, api):
        bodies = _bodies(
            commits=[_commit(HEAD_SHA, "2026-07-08T20:12:05Z")],
            signals={HEAD_SHA: (_check_runs(), _statuses())},
        )
        bodies[_status_path(HEAD_SHA)] = _statuses(
            *((f"license/cla-{index}", "success") for index in range(100))
        )
        bodies[_status_path(HEAD_SHA, page=2)] = _statuses(
            ("continuous-integration/jenkins/branch", "failure")
        )
        api(bodies)

        anchor = select_anchor(REPO)

        assert anchor["statuses"] == [
            {"context": "continuous-integration/jenkins/branch", "state": "failure"}
        ]

    def test_a_short_first_page_ends_the_walk(self, api):
        fake = api(
            _bodies(
                commits=[_commit(HEAD_SHA, "2026-08-27T17:41:11Z")],
                signals={HEAD_SHA: (_check_runs(("build (17, false)", "success")), _statuses())},
            )
        )
        select_anchor(REPO)

        assert _checks_path(HEAD_SHA) in fake.calls
        assert _checks_path(HEAD_SHA, page=2) not in fake.calls
        assert _status_path(HEAD_SHA, page=2) not in fake.calls

    def test_an_unbounded_run_of_full_pages_stops_at_the_page_cap(self, api, monkeypatch):
        monkeypatch.setattr("scripts.d3_select_anchor.API_PAGE_SIZE", 2)
        monkeypatch.setattr("scripts.d3_select_anchor.MAX_SIGNAL_PAGES", 2)
        noise = self._noise_page(2)
        bodies: dict[str, object] = {
            f"repos/{REPO}": {"default_branch": "main"},
            f"repos/{REPO}/commits?sha=main&per_page=15": [
                _commit(HEAD_SHA, "2026-08-27T17:41:11Z")
            ],
        }
        for page in (1, 2):
            bodies[f"repos/{REPO}/commits/{HEAD_SHA}/check-runs?per_page=2&page={page}"] = noise
            bodies[f"repos/{REPO}/commits/{HEAD_SHA}/status?per_page=2&page={page}"] = _statuses()
        fake = api(bodies)

        # A third page would be an unexpected path and the fake would say so.
        with pytest.raises(SelectorError):
            select_anchor(REPO)
        assert sum(1 for call in fake.calls if "check-runs" in call) == 2


class TestCli:
    def test_stdout_carries_exactly_one_json_object(self, api, capsys):
        api(
            _bodies(
                commits=[_commit(HEAD_SHA, "2026-08-27T17:41:11Z")],
                signals={HEAD_SHA: (_check_runs(("build (17, false)", "success")), _statuses())},
            )
        )
        assert main(["--repo", REPO]) == 0

        captured = capsys.readouterr()
        assert captured.err == ""
        anchor = json.loads(captured.out)
        assert anchor["sha"] == HEAD_SHA
        assert anchor["noise_filtered"] is True

    def test_no_anchor_exits_one_with_an_empty_stdout(self, api, capsys):
        api(
            _bodies(
                commits=[_commit(HEAD_SHA, "2026-08-27T17:41:11Z")],
                signals={HEAD_SHA: (_check_runs(("Dependabot updates", "success")), _statuses())},
            )
        )
        assert main(["--repo", REPO]) == 1

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "D3 ANCHOR" in captured.err

    def test_a_failing_api_call_exits_one_rather_than_raising(self, monkeypatch, capsys):
        def explode(path: str) -> object:
            raise SelectorError("gh api repos/apache/demo failed: HTTP 404")

        monkeypatch.setattr("scripts.d3_select_anchor.fetch", explode)
        assert main(["--repo", REPO]) == 1
        assert "HTTP 404" in capsys.readouterr().err
