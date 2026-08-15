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

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

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
    """Invoke the CLI with stdout and stderr kept apart, as a pipe would.

    The streams are separated deliberately: the contract is that STDOUT is
    JSON. Components the command reads through — the output store's manager,
    for one — log as they always do, and those lines belong on stderr.
    """
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


def test_the_command_writes_nothing_into_the_session_it_read(tmp_path):
    session_dir = _session_with_store(tmp_path)
    before = {p: p.stat().st_mtime_ns for p in sorted(session_dir.rglob("*"))}

    _run(str(session_dir), "--detail", "full")

    after = {p: p.stat().st_mtime_ns for p in sorted(session_dir.rglob("*"))}
    assert after == before
