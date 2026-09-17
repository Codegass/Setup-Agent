"""`sag result` reads a finished run back, from a container or a directory.

The command is a reader. It re-judges nothing, writes nothing, and exits 0
whenever a result could be read at all — a partial run is a result, and
reading one is not running one. Three things are fenced here because each has
a way of going wrong that no amount of reading the code shows:

- **`--json` is a pipe's contract.** The welcome panel goes to stdout for most
  commands, and one panel above the document makes `sag result X --json | jq`
  useless.
- **A container name is a container name.** The orchestrator takes its image
  first and its project second, so a name handed over positionally opens a
  container nobody asked for.
- **Counts come from the run's own ledger, wherever the run kept it** — beside
  the session or under the container's `.setup_agent/` — and a ledger with no
  turns states no turns rather than zero of them.
"""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import sag.config as config_module
import sag.config.logger as logger_module
from sag.agent.verdict_finalizer import RunVerdictSnapshot
from sag.main import cli

from result_card_fakes import snapshot_dict

FIXTURES = Path(__file__).parent / "fixtures" / "trajectory"
KAFKA = FIXTURES / "kafka-d2r3"


@pytest.fixture(autouse=True)
def _clean_cli_state(monkeypatch, tmp_path):
    """The house pattern: no leftover config, no `logs/` written by a reader."""
    monkeypatch.setattr(config_module, "_config", None)
    monkeypatch.setattr(logger_module, "_session_logger", None)
    monkeypatch.chdir(tmp_path)


def _recorded(tmp_path, **overrides) -> Path:
    """A session directory shaped the way a recorded run leaves one."""
    directory = tmp_path / "session_1"
    (directory / ".setup_agent").mkdir(parents=True)
    (directory / ".setup_agent" / "verdict.json").write_text(
        json.dumps(snapshot_dict(**overrides)), encoding="utf-8"
    )
    return directory


def _with_ledger(directory: Path) -> Path:
    """Add the kafka run's own ledger, token ledger and run pin to a session."""
    for name in ("control_events.jsonl", "token_usage.csv", "run-pin.json"):
        (directory / name).write_bytes((KAFKA / name).read_bytes())
    return directory


def _archive(tmp_path) -> Path:
    """A campaign archive: the container's copy, kept beside the run."""
    directory = tmp_path / "archived"
    evidence = directory / "container-evidence" / ".setup_agent"
    evidence.mkdir(parents=True)
    (evidence / "verdict.json").write_text(json.dumps(snapshot_dict()), encoding="utf-8")
    for name in ("control_events.jsonl", "token_usage.csv", "run-pin.json"):
        (evidence / name).write_bytes((KAFKA / name).read_bytes())
    return directory


class _FakeOrchestrator:
    """The real constructor's shape: the image first, the project second.

    A double with one parameter cannot see the defect this stands against —
    a positionally passed container name would bind to `project_name` there
    and look right while the real class binds it to `base_image`.
    """

    def __init__(self, base_image=None, project_name=None):
        self.base_image = base_image
        self.project_name = project_name
        self.container_name = f"sag-{project_name}" if project_name else "sag-default"


def test_prints_the_block_for_a_recorded_session(tmp_path):
    result = CliRunner().invoke(cli, ["result", str(_recorded(tmp_path))])

    assert result.exit_code == 0, result.output
    assert "Required task" in result.output
    assert "Official CI" in result.output


def test_json_prints_the_card_and_nothing_a_parser_chokes_on(tmp_path):
    """Stdout is the document, whole. No banner above it, or the pipe is dead."""
    result = CliRunner().invoke(cli, ["result", str(_recorded(tmp_path)), "--json"])

    assert result.exit_code == 0, result.output
    card = json.loads(result.output)
    assert card["schema_version"] == 1
    assert [row["key"] for row in card["rows"]][0] == "setup"


def test_finds_a_verdict_when_pointed_at_the_setup_agent_directory_itself(tmp_path):
    """A run writes its verdict under `.setup_agent/`; naming that works too."""
    directory = tmp_path / "flat"
    directory.mkdir()
    (directory / "verdict.json").write_text(json.dumps(snapshot_dict()), encoding="utf-8")

    assert CliRunner().invoke(cli, ["result", str(directory)]).exit_code == 0


def test_a_campaign_archive_states_the_turns_its_ledger_recorded(tmp_path):
    """The counts are read from where the verdict was found, not from one path.

    An archived run keeps everything under `container-evidence/.setup_agent/`.
    Looking for the ledger at the directory the user named leaves every count
    absent for every archive on disk, while the card still prints a result.
    """
    result = CliRunner().invoke(cli, ["result", str(_archive(tmp_path)), "--json"])

    assert result.exit_code == 0, result.output
    stats = json.loads(result.output)["stats"]
    assert stats["turns"] == 24
    assert stats["tool_calls"] == 24
    assert stats["model"]  # the run pin beside the verdict names the models


def test_a_partial_run_states_its_verdict_without_claiming_an_exit_code(tmp_path):
    """Reading a result is not running one, so no exit code is announced.

    The end-of-run block ends `Setup verdict: partial · exit 1`, and there it
    is true because the same call produces the block and the code. Here the
    command deliberately exits 0, so that line would state a code the command
    did not return.
    """
    result = CliRunner().invoke(cli, ["result", str(_recorded(tmp_path, verdict="partial"))])

    assert result.exit_code == 0, result.output
    assert "partial" in result.output
    assert "exit 1" not in result.output


def test_nothing_readable_exits_one(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()

    result = CliRunner().invoke(cli, ["result", str(empty)])

    assert result.exit_code == 1
    assert "no result" in result.output.lower()


def test_the_container_branch_opens_the_container_the_user_named(monkeypatch):
    """`sag result sag-commons-cli` reads sag-commons-cli, not sag-default."""
    opened = {}

    def _snapshot(orchestrator, **kwargs):
        opened["container"] = orchestrator.container_name
        return RunVerdictSnapshot.model_validate(snapshot_dict())

    monkeypatch.setattr("sag.main.DockerOrchestrator", _FakeOrchestrator)
    monkeypatch.setattr("sag.main.read_live_verdict_snapshot", _snapshot)
    monkeypatch.setattr("sag.main._read_module_metrics_for_cli", lambda orchestrator: None)
    monkeypatch.setattr("sag.main._read_metrics_v2_for_cli", lambda orchestrator: None)

    result = CliRunner().invoke(cli, ["result", "sag-commons-cli", "--json"])

    assert result.exit_code == 0, result.output
    assert opened["container"] == "sag-commons-cli"
    assert json.loads(result.output)["container"] == "sag-commons-cli"


def test_a_ledger_with_no_turns_states_no_counts_rather_than_zero(tmp_path):
    """A run whose turns were never recorded counted none, not zero of them."""
    directory = _recorded(tmp_path)
    (directory / "control_events.jsonl").write_text("", encoding="utf-8")

    result = CliRunner().invoke(cli, ["result", str(directory), "--json"])

    assert result.exit_code == 0, result.output
    stats = json.loads(result.output)["stats"]
    assert stats["turns"] is None
    assert stats["tool_calls"] is None
    assert stats["tool_failures"] is None


def test_the_counts_are_the_ones_the_end_of_run_block_states(tmp_path):
    """One definition of a tool failure, shared with `sag run`'s own block."""
    from sag.main import _read_run_counts_for_cli

    directory = _with_ledger(_recorded(tmp_path))
    counts = _read_run_counts_for_cli(str(directory))

    result = CliRunner().invoke(cli, ["result", str(directory), "--json"])

    assert result.exit_code == 0, result.output
    stats = json.loads(result.output)["stats"]
    assert stats["tool_failures"] == counts["tool_failures"] == 4
    assert stats["turns"] == counts["turn_count"] == 24


def test_the_card_bills_the_tokens_the_session_recorded(tmp_path):
    """The token ledger beside the run is read, on the trajectory's terms."""
    directory = _with_ledger(_recorded(tmp_path))

    result = CliRunner().invoke(cli, ["result", str(directory), "--json"])

    assert result.exit_code == 0, result.output
    stats = json.loads(result.output)["stats"]
    assert stats["tokens_in"] and stats["tokens_in"] > 4134
    assert stats["tokens_out"] and stats["tokens_out"] > 0


def test_the_command_writes_nothing_into_the_session_it_read(tmp_path):
    directory = _with_ledger(_recorded(tmp_path))
    before = {p: p.stat().st_mtime_ns for p in sorted(directory.rglob("*"))}

    result = CliRunner().invoke(cli, ["result", str(directory)])

    assert result.exit_code == 0, result.output
    assert {p: p.stat().st_mtime_ns for p in sorted(directory.rglob("*"))} == before
