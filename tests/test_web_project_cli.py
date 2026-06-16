"""Tests for the CLI-equivalent project command builder."""

import sys

from sag.web.project_cli import ProjectCliCommand

REPO = "https://github.com/apache/commons-cli.git"


def test_bare_repo_builds_minimal_project_command():
    command = ProjectCliCommand(repo_url=REPO)

    assert command.project_args() == ["project", REPO]


def test_name_option_is_included():
    command = ProjectCliCommand(repo_url=REPO, name="commons-cli-111")

    assert command.project_args() == ["project", REPO, "--name", "commons-cli-111"]


def test_ref_option_is_included():
    command = ProjectCliCommand(repo_url=REPO, ref="rel/commons-cli-1.11.0")

    assert command.project_args() == ["project", REPO, "--ref", "rel/commons-cli-1.11.0"]


def test_goal_option_is_included():
    command = ProjectCliCommand(repo_url=REPO, goal="Setup and verify Apache Commons CLI")

    assert command.project_args() == [
        "project",
        REPO,
        "--goal",
        "Setup and verify Apache Commons CLI",
    ]


def test_record_flag_is_included():
    command = ProjectCliCommand(repo_url=REPO, record=True)

    assert command.project_args() == ["project", REPO, "--record"]


def test_coverage_flag_appended_when_set():
    args = ProjectCliCommand(
        repo_url=REPO, record=True, coverage=True
    ).project_args()

    assert "--coverage" in args and "--record" in args


def test_coverage_flag_absent_when_unset():
    args = ProjectCliCommand(repo_url=REPO).project_args()

    assert "--coverage" not in args


def test_all_options_together_match_manual_sag_project_invocation():
    command = ProjectCliCommand(
        repo_url=REPO,
        name="commons-cli-111",
        ref="rel/commons-cli-1.11.0",
        goal="Setup and verify Apache Commons CLI",
        record=True,
    )

    # Equivalent to:
    # sag project <repo> --name commons-cli-111 --ref rel/commons-cli-1.11.0 \
    #   --goal "Setup and verify Apache Commons CLI" --record
    assert command.project_args() == [
        "project",
        REPO,
        "--name",
        "commons-cli-111",
        "--ref",
        "rel/commons-cli-1.11.0",
        "--goal",
        "Setup and verify Apache Commons CLI",
        "--record",
    ]


def test_argv_runs_the_cli_module_through_the_active_python():
    command = ProjectCliCommand(repo_url=REPO, ref="v1.0")

    assert command.argv() == [
        sys.executable,
        "-m",
        "sag.main",
        "project",
        REPO,
        "--ref",
        "v1.0",
    ]
