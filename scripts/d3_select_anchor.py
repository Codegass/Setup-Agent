#!/usr/bin/env python3
"""Pick a project's anchor commit: the newest one its own CI actually judged.

A d3 pin is only worth harvesting when the project's CI said something about
the exact revision SAG will be handed.  This selector walks the default branch
from HEAD backwards and stops at the first commit whose check runs or commit
statuses carry a verdict that is about the code.

Two filters decide "about the code":

* **Noise.** CodeQL, Dependabot, Copilot, label/stale bots, docs and website
  builds, Sonar, triage and notify flows are removed by name.  geode's HEAD in
  the 2026-08-27 probe carried four green checks and all four were CodeQL: a
  commit like that is a pseudo-anchor, and treating it as evidence would pin a
  target that proves nothing about the build.
* **Verdict.** A check still running, skipped, cancelled or neutral judged
  nothing.  A red check is kept -- ``failure`` is evidence, and a live red cell
  is a legitimate target (commons-dbcp's anchor carried one).

Network access is one function, :func:`fetch`, so a test replaces the whole
GitHub API by monkeypatching it.  stdout carries exactly one JSON object -- the
anchor -- and every diagnostic goes to stderr; a run with no anchor exits 1.

Examples::

    python scripts/d3_select_anchor.py --repo apache/kafka
    python scripts/d3_select_anchor.py --repo apache/lucene --branch main
    python scripts/d3_select_anchor.py --repo apache/geode --max-commits 30
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any

# Substrings, matched case-insensitively against a check-run name or a status
# context.  These are the flows that run on every commit regardless of what the
# commit does; none of them is a statement about whether the project builds.
NOISE_NAME_SUBSTRINGS: tuple[str, ...] = (
    "codeql",
    "dependabot",
    "copilot",
    "label",
    "stale",
    "docs",
    "website",
    "site",
    "sonar",
    "triage",
    "comment",
    "notify",
    "lint-pr",
    "semantic",
    "dco",
    "analyze",
    "dependency review",
    "license",
)

# Conclusions that decided nothing: the check exists but judged no code.
UNDECIDED_CONCLUSIONS: frozenset[str] = frozenset(
    {"", "skipped", "cancelled", "neutral", "stale", "action_required"}
)
# Commit-status states that decided nothing.  "error" is kept: it is a verdict.
UNDECIDED_STATES: frozenset[str] = frozenset({"", "pending"})

DEFAULT_MAX_COMMITS = 15
MAX_COMMITS_BOUND = 100

_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


class SelectorError(Exception):
    """A selector run that cannot name an anchor."""


def fetch(path: str) -> Any:
    """Return the parsed JSON body of one ``gh api`` GET.

    The only door to the network in this module: every caller goes through it
    by name, so a test that replaces this attribute replaces the whole GitHub
    API.  GitHub's list endpoints answer with an array rather than an object,
    so the body is whatever JSON came back.
    """

    completed = subprocess.run(
        ["gh", "api", "-H", "Accept: application/vnd.github+json", path],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        raise SelectorError(f"gh api {path} failed: {detail[-1] if detail else 'no detail'}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SelectorError(f"gh api {path} did not answer JSON: {exc}") from exc


def is_noise(name: str) -> bool:
    """Whether a check name or status context is a non-build flow."""

    lowered = (name or "").lower()
    return any(token in lowered for token in NOISE_NAME_SUBSTRINGS)


def default_branch(repo: str) -> str:
    """Return the repository's default branch."""

    body = fetch(f"repos/{repo}")
    branch = str((body or {}).get("default_branch") or "").strip() if isinstance(body, dict) else ""
    if not branch:
        raise SelectorError(f"{repo} does not name a default branch")
    return branch


def recent_commits(repo: str, branch: str, max_commits: int) -> tuple[dict[str, Any], ...]:
    """Return the newest commits on ``branch``, newest first."""

    body = fetch(f"repos/{repo}/commits?sha={branch}&per_page={max_commits}")
    if not isinstance(body, list):
        raise SelectorError(f"{repo}@{branch} did not answer a commit list")
    return tuple(item for item in body if isinstance(item, dict))


def commit_date(commit: dict[str, Any]) -> str:
    """Return the commit's committer date, falling back to its author date."""

    detail = commit.get("commit")
    if not isinstance(detail, dict):
        return ""
    for role in ("committer", "author"):
        actor = detail.get(role)
        if isinstance(actor, dict):
            date = str(actor.get("date") or "").strip()
            if date:
                return date
    return ""


def commit_signals(repo: str, sha: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Return one commit's noise-filtered, verdict-carrying checks and statuses."""

    checks_body = fetch(f"repos/{repo}/commits/{sha}/check-runs")
    raw_checks = checks_body.get("check_runs") if isinstance(checks_body, dict) else None
    checks: list[dict[str, str]] = []
    for run in raw_checks or ():
        if not isinstance(run, dict):
            continue
        name = str(run.get("name") or "").strip()
        conclusion = str(run.get("conclusion") or "").strip().lower()
        if not name or is_noise(name) or conclusion in UNDECIDED_CONCLUSIONS:
            continue
        checks.append({"name": name, "conclusion": conclusion})

    status_body = fetch(f"repos/{repo}/commits/{sha}/status")
    raw_statuses = status_body.get("statuses") if isinstance(status_body, dict) else None
    statuses: list[dict[str, str]] = []
    for status in raw_statuses or ():
        if not isinstance(status, dict):
            continue
        context = str(status.get("context") or "").strip()
        state = str(status.get("state") or "").strip().lower()
        if not context or is_noise(context) or state in UNDECIDED_STATES:
            continue
        statuses.append({"context": context, "state": state})
    return checks, statuses


def select_anchor(
    repo: str,
    *,
    branch: str | None = None,
    max_commits: int = DEFAULT_MAX_COMMITS,
) -> dict[str, Any]:
    """Return the newest commit on ``branch`` that carries build or test evidence."""

    if not _REPO_RE.match((repo or "").strip()):
        raise SelectorError("repo must be owner/name")
    repo = repo.strip()
    if not 1 <= max_commits <= MAX_COMMITS_BOUND:
        raise SelectorError(f"max commits must be between 1 and {MAX_COMMITS_BOUND}")

    target_branch = (branch or "").strip() or default_branch(repo)
    commits = recent_commits(repo, target_branch, max_commits)
    if not commits:
        raise SelectorError(f"{repo}@{target_branch} has no commits")

    for commit in commits:
        sha = str(commit.get("sha") or "").strip()
        if not sha:
            continue
        checks, statuses = commit_signals(repo, sha)
        if not checks and not statuses:
            continue
        return {
            "repo": repo,
            "sha": sha,
            "date": commit_date(commit),
            "checks": checks,
            "statuses": statuses,
            "noise_filtered": True,
        }

    raise SelectorError(
        f"no commit among the newest {len(commits)} on {repo}@{target_branch} "
        "carries noise-filtered build or test evidence"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="d3_select_anchor.py",
        description="Select a project's d3 anchor commit from its own CI record.",
    )
    parser.add_argument("--repo", required=True, help="owner/name, e.g. apache/kafka")
    parser.add_argument("--branch", default=None, help="branch to walk (default: the repo's own)")
    parser.add_argument(
        "--max-commits",
        type=int,
        default=DEFAULT_MAX_COMMITS,
        help=f"how far back to walk (default: {DEFAULT_MAX_COMMITS})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        anchor = select_anchor(args.repo, branch=args.branch, max_commits=args.max_commits)
    except SelectorError as exc:
        print(f"D3 ANCHOR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(anchor))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
