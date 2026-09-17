"""`sag trajectory` — the derivation, one session at a time, on stdout.

The command is a reader: it opens an archived or running session directory,
folds it through the same reducer everything else uses, and prints JSON. Two
properties are load-bearing and both are fenced here:

- **stdout is machine-readable, always.** No banner, no rich panel, no log
  line — a snapshot must parse as a `Trajectory` and each `--follow` line as a
  `TrajectoryDelta`, or the command is useless to the thing consuming it.
- **the full tier resolves bytes through the existing store reader.** Refs are
  looked up in `contexts/full_outputs.jsonl` via `OutputStorageManager`, which
  is the only component allowed to know that file's layout. A store that is
  not there, or a ref it does not carry, is a warning — never a crash.

The session bytes below are the archived kafka d2r3 fixture, and the single
full-output record is that same run's real one for `output_6163859b019d`.
"""

import io
import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from rich.console import Console as RichConsole

import sag.config as config_module
import sag.config.logger as logger_module
from sag.main import cli
from sag.trajectory.schema import Trajectory, TrajectoryDelta

FIXTURES = Path(__file__).parent / "fixtures" / "trajectory"
KAFKA = FIXTURES / "kafka-d2r3"

# The first record of the archived kafka run's `contexts/full_outputs.jsonl`,
# verbatim — the clone output that its first turn's observation refs.
REAL_FULL_OUTPUT_RECORD = (
    '{"ref_id": "output_6163859b019d", "task_id": "phase_provision", "tool_name": "project", '
    '"timestamp": "2026-08-14T07:28:48.954378", "output_length": 383, '
    '"output": "\\u2705 Repository cloned successfully!\\n\\n\\ud83d\\udcc2 Repository: '
    "https://github.com/apache/kafka.git\\n\\ud83d\\udcc1 Directory: /workspace/kafka\\n"
    "\\ud83d\\udd16 Ref: 4.3.1\\n\\ud83e\\uddfe Commit: "
    "26b251a451ce941d3d7a55e6487bcb7f16b5ad48\\n\\ud83d\\udd0d Project Type: gradle\\n"
    "\\ud83d\\udccb Build Files: /workspace/kafka/committer-tools/requirements.txt, "
    "/workspace/kafka/docker/requirements.txt, /workspace/kafka/release/requirements.txt, "
    '/workspace/kafka/build.gradle\\n", '
    '"metadata": {"invocation_status": "completed", "operation_outcome": "success", '
    '"evidence_status": "verified", "error_code": null, "failure_signature": null, '
    '"action": "clone"}}'
)


@pytest.fixture(autouse=True)
def _clean_cli_state(monkeypatch, tmp_path):
    """The house pattern: no leftover config, no `logs/` written by a reader."""
    monkeypatch.setattr(config_module, "_config", None)
    monkeypatch.setattr(logger_module, "_session_logger", None)
    monkeypatch.chdir(tmp_path)


def _run(*args: str):
    """Invoke the JSON format with stdout and stderr kept apart, as a pipe would.

    The streams are separated deliberately: the contract of `--format json` is
    that STDOUT is JSON. Components the command reads through — the output
    store's manager, for one — log as they always do, and those lines belong
    on stderr. The format is asked for explicitly because the command's own
    default is the table a person reads; every contract below is about the
    document a program reads.
    """
    return CliRunner(mix_stderr=False).invoke(cli, ["trajectory", "--format", "json", *args])


def _table(*args: str):
    """Invoke the command as a person does, with no format asked for."""
    return CliRunner(mix_stderr=False).invoke(cli, ["trajectory", *args])


def _session_with_store(tmp_path: Path) -> Path:
    """A session directory carrying the kafka ledger and one stored output."""
    session_dir = tmp_path / "session"
    contexts = session_dir / ".setup_agent" / "contexts"
    contexts.mkdir(parents=True)
    (session_dir / "control_events.jsonl").write_bytes(
        (KAFKA / "control_events.jsonl").read_bytes()
    )
    (contexts / "full_outputs.jsonl").write_text(REAL_FULL_OUTPUT_RECORD + "\n", encoding="utf-8")
    return session_dir


def test_a_snapshot_is_a_trajectory_on_stdout_and_nothing_else(tmp_path):
    result = _run(str(KAFKA))

    assert result.exit_code == 0, result.output
    snapshot = Trajectory.model_validate_json(result.output)
    assert sum(1 for t in snapshot.turns if t.call is not None) == 24
    assert snapshot.schema_version == 1
    assert not (tmp_path / "logs").exists()  # a reader opens no session log


def test_the_snapshot_carries_the_joins_the_builder_makes():
    result = _run(str(KAFKA))
    snapshot = Trajectory.model_validate_json(result.output)

    assert snapshot.session.run_id.endswith("b9b1b06dfff3")
    assert [t for t in snapshot.turns if t.tokens is not None][0].tokens.input == 4134
    assert any(w.code == "missing_loop_decision" for w in snapshot.warnings)


def test_the_summary_tier_names_refs_and_carries_no_bytes(tmp_path):
    result = _run(str(_session_with_store(tmp_path)))

    snapshot = Trajectory.model_validate_json(result.output)
    assert snapshot.outputs is None
    assert snapshot.turns[0].observation.ref == "output_6163859b019d"


def test_the_full_tier_resolves_the_bytes_a_ref_names(tmp_path):
    result = _run(str(_session_with_store(tmp_path)), "--detail", "full")

    assert result.exit_code == 0, result.output
    snapshot = Trajectory.model_validate_json(result.output)
    assert snapshot.outputs["output_6163859b019d"].startswith("✅ Repository cloned successfully!")
    unresolved = [w for w in snapshot.warnings if w.code == "unresolved_output_ref"]
    assert {w.detail for w in unresolved} and "output_6163859b019d" not in {
        w.detail for w in unresolved
    }


def test_a_full_tier_run_with_no_store_says_so_instead_of_crashing():
    result = _run(str(KAFKA), "--detail", "full")

    assert result.exit_code == 0, result.output
    snapshot = Trajectory.model_validate_json(result.output)
    assert snapshot.outputs == {}
    assert [w.code for w in snapshot.warnings if w.code == "missing_output_store"] == [
        "missing_output_store"
    ]


def test_follow_prints_one_json_delta_per_line_until_interrupted(monkeypatch):
    """`--follow` is a stream of deltas; an interrupt ends it, quietly."""
    deltas = [
        TrajectoryDelta(session_patch={"run_id": "r-1"}),
        TrajectoryDelta(warnings=[{"code": "missing_tool_result", "detail": "seq 124"}]),
    ]
    seen = {}

    def fake_follow(session_dir, **kwargs):
        seen["session_dir"] = session_dir
        seen.update(kwargs)
        yield from deltas
        raise KeyboardInterrupt

    monkeypatch.setattr("sag.main.follow_trajectory", fake_follow)
    result = _run(str(KAFKA), "--follow")

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert [TrajectoryDelta.model_validate_json(line) for line in lines] == deltas
    assert Path(seen["session_dir"]) == KAFKA and seen["detail"] == "summary"


def test_follow_prints_what_the_end_of_the_follow_had_left_to_say(tmp_path, monkeypatch):
    """A statement made by stopping still belongs on stdout, in the same shape.

    The torn tail of a ledger is only knowable once the follow ends, so it never
    rides an event's delta. A consumer parsing one delta per line must get it on
    the same terms as the rest, or the one statement the live mode can only make
    at the end is the one statement a pipe never sees.
    """
    torn = TrajectoryDelta(warnings=[{"code": "ledger_tail_torn", "detail": "offset 42"}])

    class _Stream:
        def __iter__(self):
            return iter([TrajectoryDelta(session_patch={"run_id": "r-1"})])

        def close(self):
            return torn

    monkeypatch.setattr("sag.main.follow_trajectory", lambda session_dir, **kwargs: _Stream())
    result = _run(str(KAFKA), "--follow")

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert TrajectoryDelta.model_validate_json(lines[-1]) == torn
    assert len(lines) == 2


def test_follow_passes_the_detail_tier_through(monkeypatch):
    seen = {}

    def fake_follow(session_dir, **kwargs):
        seen.update(kwargs)
        raise KeyboardInterrupt
        yield  # pragma: no cover - generator marker

    monkeypatch.setattr("sag.main.follow_trajectory", fake_follow)
    result = _run(str(KAFKA), "--follow", "--detail", "full")

    assert result.exit_code == 0
    assert seen["detail"] == "full"


def test_a_detail_tier_that_does_not_exist_is_rejected_by_the_parser():
    """Only the two tiers of spec §3 exist, and stdout stays empty when asked
    for a third."""
    result = _run(str(KAFKA), "--detail", "verbose")

    assert result.exit_code != 0
    assert result.output == ""
    assert "verbose" in result.stderr


def test_a_session_directory_that_is_not_there_fails_without_a_traceback(tmp_path):
    result = _run(str(tmp_path / "never-existed"))

    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.output)


def test_an_error_is_written_to_stderr_because_stdout_is_the_contract(tmp_path):
    """A failure must not put a single byte where the JSON goes.

    `sag trajectory | jq` is the point of the command. A reader that pipes
    stdout gets nothing on failure and the reason on its terminal — the same
    bargain every other filter makes. Printing the error onto stdout instead
    hands the parser a red panel and calls it a trajectory.
    """
    result = _run(str(tmp_path / "never-existed"))

    assert result.exit_code != 0
    assert result.stdout == ""
    assert "never-existed" in result.stderr


def test_the_parsers_own_refusals_land_on_stderr_too(tmp_path):
    """Consistency: click's rejections and the command's own use one channel."""
    (tmp_path / "a-file").write_text("not a directory", encoding="utf-8")
    result = _run(str(tmp_path / "a-file"))

    assert result.exit_code != 0
    assert result.stdout == ""
    assert "a-file" in result.stderr


def test_an_oserror_that_is_not_a_missing_file_still_lands_on_stderr(monkeypatch):
    """Two error families were named; the I/O this command does has many more.

    `--follow` is built to run for the length of a run, so the terminal or the
    mount underneath it can disappear mid-stream: a pty that has gone away
    answers a write with `OSError(EIO)`, and a stale mount answers a read the
    same way. Neither is a `FileNotFoundError` and neither is a `ValueError`,
    so both escaped as tracebacks — the same broken bargain as printing an
    error onto stdout, since a caller parsing this command's output gets
    Python's diagnostics where a trajectory was promised. (Click handles the
    one OSError it knows, `EPIPE`, on its own; every other one is ours.)
    """
    import errno

    import click

    real_echo = click.echo

    def dead_terminal(message=None, *args, err=False, **kwargs):
        if not err:  # the stdout end is gone; stderr is still someone's console
            raise OSError(errno.EIO, "Input/output error")
        return real_echo(message, *args, err=err, **kwargs)

    monkeypatch.setattr("sag.main.click.echo", dead_terminal)
    result = _run(str(KAFKA))

    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert result.stdout == ""
    assert "Input/output error" in result.stderr


def test_the_command_writes_nothing_into_the_session_it_read(tmp_path):
    session_dir = _session_with_store(tmp_path)
    before = {p: p.stat().st_mtime_ns for p in sorted(session_dir.rglob("*"))}

    _run(str(session_dir), "--detail", "full")

    after = {p: p.stat().st_mtime_ns for p in sorted(session_dir.rglob("*"))}
    assert after == before


# -- the table: the same turns, read by a person rather than a program ------


def test_table_is_the_default_format():
    """Asked for nothing, the command answers the way a person reads it."""
    result = _table(str(KAFKA))

    assert result.exit_code == 0, result.stderr
    assert not result.stdout.lstrip().startswith("{")
    assert "▸ provision" in result.stdout
    first = result.stdout.splitlines()[0]
    assert "20260814_072758" in first and "24 turns" in first


def test_json_format_still_prints_one_document():
    result = _run(str(KAFKA))

    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout.strip())
    assert document["schema_version"] == 1


def test_detail_with_table_is_a_usage_error():
    """One table, one detail tier: asking for a second one is a mistake."""
    result = _table(str(KAFKA), "--detail", "full")

    assert result.exit_code == 2
    assert "--detail" in result.stderr


def test_a_mirrored_session_renders_the_turns_its_ledger_holds(tmp_path):
    """The ledger is found where the builder finds it, not at one fixed path.

    A live run mirrors its control events into the container's
    `.setup_agent/`, so a mirrored session keeps its ledger one directory down
    — and that is the second input this command's own help offers. A table
    that opened `<session>/control_events.jsonl` and nothing else printed a
    header claiming 24 turns and then showed none of them.
    """
    session_dir = tmp_path / "mirrored"
    (session_dir / ".setup_agent").mkdir(parents=True)
    (session_dir / ".setup_agent" / "control_events.jsonl").write_bytes(
        (KAFKA / "control_events.jsonl").read_bytes()
    )

    result = _table(str(session_dir))

    assert result.exit_code == 0, result.stderr
    assert "24 turns" in result.stdout.splitlines()[0]
    assert "▸ provision" in result.stdout
    assert result.stdout.count("↳") + result.stdout.count("·") > 0


def test_the_table_paints_a_terminal_instead_of_printing_its_markup(monkeypatch):
    """Styled text reaches a terminal as colour, never as the tags for it.

    The renderer writes Rich markup. A sink that puts those bytes straight on
    the stream interprets none of it, so a real terminal shows the reader
    `[bold]▸ provision[/bold]`. Only a console that is actually painting can
    tell the two sinks apart, which is why this test drives one.
    """
    painted = io.StringIO()
    monkeypatch.setattr(
        "sag.main.console", RichConsole(file=painted, force_terminal=True, width=100)
    )

    result = _table(str(KAFKA))

    assert result.exit_code == 0, result.stderr
    text = painted.getvalue()
    assert "▸ provision" in text
    assert "\x1b[" in text
    for tag in ("[bold]", "[/bold]", "[green]", "[/green]", "[red]", "[yellow]", "[dim]"):
        assert tag not in text


def test_following_the_table_ends_the_stream_once_and_states_its_tail(monkeypatch):
    """A follow ends by closing both ends, in order, exactly once.

    `follow_trajectory` returns a stream whose `close()` yields the one
    statement only the end of a follow can make. Dropping it loses the torn
    tail; closing the renderer before following makes the first live turn
    raise. Both ends close here, and the tail is rendered before they do.
    """
    delta = TrajectoryDelta(
        warnings=[{"code": "ledger_tail_torn", "detail": "the last line is half written"}]
    )
    closed = []

    class _Stream:
        def __iter__(self):
            return iter([])

        def close(self):
            closed.append("stream")
            return delta

    monkeypatch.setattr("sag.main.follow_trajectory", lambda session_dir, **kwargs: _Stream())

    result = _table(str(KAFKA), "--follow")

    assert result.exit_code == 0, result.stderr
    assert closed == ["stream"]
    # Stated as the stream states a hole, not dumped as the document does it.
    assert "! ledger_tail_torn: the last line is half written" in result.stdout
    assert not result.stdout.lstrip().startswith("{")


def test_a_directory_with_no_ledger_says_so_instead_of_nothing(tmp_path):
    """Silence is the one answer this surface may not give.

    `--format json` states `missing_control_events` for a directory holding no
    ledger. The table printed a header and zero bytes at exit 0 for the same
    input — implying the absence the document states.
    """
    empty = tmp_path / "no-ledger"
    empty.mkdir()

    result = _table(str(empty))

    assert result.exit_code == 0, result.stderr
    assert "missing_control_events" in result.stdout
    assert "control_events.jsonl" in result.stdout


def test_a_campaign_archive_reads_the_run_inside_it(tmp_path):
    """`sag result` finds a run one level down, and this command must agree.

    The block `sag result` prints ends with a `Next` line naming this command
    and the directory it was given. When only one of the two searched the
    archive, that line named a command that printed nothing.
    """
    archive = tmp_path / "runs" / "commons-cli"
    evidence = archive / "container-evidence" / ".setup_agent"
    evidence.mkdir(parents=True)
    (evidence / "control_events.jsonl").write_bytes((KAFKA / "control_events.jsonl").read_bytes())

    result = _table(str(archive))

    assert result.exit_code == 0, result.stderr
    assert "24 turns" in result.stdout.splitlines()[0]
    assert "▸ provision" in result.stdout


def test_following_states_everything_a_replay_of_the_same_ledger_states(monkeypatch):
    """One ledger, one answer, whichever way it is read.

    A phase closing lives in the raw event and no delta carries it, so a follow
    driven by deltas alone dropped every `✓ <phase>` line a replay prints — the
    same command giving two different answers for one input.
    """
    import sag.main as main_module

    replay = _table(str(KAFKA))
    assert replay.exit_code == 0, replay.stderr

    real_follow = main_module.follow_trajectory

    def _stop(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(
        main_module,
        "follow_trajectory",
        lambda session_dir, **kwargs: real_follow(session_dir, sleep=_stop, **kwargs),
    )
    followed = _table(str(KAFKA), "--follow")

    assert followed.exit_code == 0, followed.stderr
    closes = [line for line in replay.stdout.splitlines() if line.startswith("✓ ")]
    assert len(closes) == 5
    missing = [line for line in closes if line not in followed.stdout]
    assert missing == []


def test_both_formats_answer_for_the_same_run(tmp_path):
    """One directory, one run, whichever format is asked for.

    The table learned to look inside a campaign archive first. A document that
    kept looking only at the directory named answered for no run at all — an
    empty run_id and no turns — for the input its own table read whole.
    """
    archive = tmp_path / "runs" / "commons-cli"
    evidence = archive / "container-evidence" / ".setup_agent"
    evidence.mkdir(parents=True)
    (evidence / "control_events.jsonl").write_bytes((KAFKA / "control_events.jsonl").read_bytes())

    table = _table(str(archive))
    document = json.loads(_run(str(archive)).stdout)

    assert table.exit_code == 0, table.stderr
    assert document["session"]["run_id"]
    assert document["session"]["run_id"] in table.stdout.splitlines()[0]
    assert len(document["turns"]) == 24
