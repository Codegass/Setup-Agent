"""`sag list` names what each run produced, instead of a free-text comment.

Every column comes from the same read model the Workbench dashboard renders,
so the terminal and the web page cannot disagree about a workspace.
"""

from click.testing import CliRunner

from sag.main import cli
from sag.web.models import DashboardResponse, DockerSummary, WorkspaceSummary

CONNECTED = DashboardResponse(docker=DockerSummary(status="connected"), workspaces=[])

ONE_WORKSPACE = DashboardResponse(
    docker=DockerSummary(status="connected"),
    workspaces=[
        WorkspaceSummary(
            id="sag-kafka",
            project="kafka",
            container="sag-kafka",
            docker=DockerSummary(status="running"),
            updated="2026-09-16 10:00",
        )
    ],
)

UNREADABLE = DashboardResponse(
    docker=DockerSummary(status="unavailable"),
    workspaces=[],
    read_status="unavailable",
    read_error="Workspace data could not be read. Retry when Docker is available.",
)


def _invoke(monkeypatch, dashboard):
    # A fixed width keeps the table's headers on one line, so an assertion
    # about a header is about the header and not about the terminal.
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setattr(
        "sag.web.read_model.ReadModelBuilder.dashboard",
        lambda self: dashboard,
    )
    return CliRunner().invoke(cli, ["list"])


def test_columns_name_the_result_not_a_free_text_comment(monkeypatch):
    result = _invoke(monkeypatch, ONE_WORKSPACE)

    assert result.exit_code == 0
    assert "Last Comment" not in result.output
    headers = ("Project", "Container", "State", "Setup", "Required task", "Tests", "Updated")
    assert len(headers) == 7
    missing = [header for header in headers if header not in result.output]
    assert missing == []
    assert "kafka" in result.output
    assert "sag-kafka" in result.output
    assert "running" in result.output


def test_a_workspace_with_no_recorded_result_says_nothing_is_known(monkeypatch):
    result = _invoke(monkeypatch, ONE_WORKSPACE)

    # Setup, Required task and Tests have no source yet; they say so rather
    # than borrowing a number from somewhere else.
    assert result.output.count("—") >= 3


def test_an_empty_dashboard_teaches_the_first_command(monkeypatch):
    result = _invoke(monkeypatch, CONNECTED)

    assert result.exit_code == 0
    assert "No SAG workspaces found." in result.output
    assert "sag project" in result.output


def test_a_failed_read_is_not_reported_as_an_empty_dashboard(monkeypatch):
    result = _invoke(monkeypatch, UNREADABLE)

    assert result.exit_code == 0
    assert "Workspace data could not be read" in result.output
    assert "No SAG workspaces found." not in result.output
