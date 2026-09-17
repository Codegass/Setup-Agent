"""sag inspect: render journals + phase history for debugging what the model
saw (spec §7). Helpers are pure; sources (container/session-dir) are injected."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

import sag.config as config_module
import sag.config.logger as logger_module
from sag.main import _inspect_render_iteration, _inspect_render_timeline, cli

RECORDS = [
    {
        "iteration": 1,
        "phase": "build",
        "segments": {},
        "delta": {"added": 1, "compacted": 0},
        "total_chars": 4000,
        "intro_text": "=== PHASE: BUILD ===\nobjective",
        "step_span": 1,
    },
    {
        "iteration": 2,
        "phase": "build",
        "segments": {},
        "delta": {"added": 2, "compacted": 0},
        "total_chars": 5200,
        "step_span": 3,
    },
    {
        "iteration": 3,
        "phase": "build",
        "segments": {},
        "delta": {"added": 1, "compacted": 9},
        "total_chars": 4100,
        "ledger_text": "ATTEMPT LEDGER:\n✗ build: enforcer → output_a",
        "step_span": 2,
    },
]


def test_timeline_one_line_per_iteration_with_markers():
    out = _inspect_render_timeline(RECORDS)
    lines = [l for l in out.splitlines() if l.strip().startswith("iter")]
    assert len(lines) == 3
    assert "INTRO" in out and "LEDGER" in out
    assert "compacted=9" in out


def test_iteration_view_shows_window_composition():
    out = _inspect_render_iteration(
        RECORDS,
        3,
        history_entries=[
            {
                "type": "action",
                "tool_name": "build",
                "success": False,
                "output": "BUILD FAILED: enforcer",
            },
        ],
    )
    assert "=== PHASE: BUILD ===" in out, "nearest intro at-or-before the iteration"
    assert "ATTEMPT LEDGER" in out
    assert "BUILD FAILED" in out


def test_iteration_view_missing_iter_says_so():
    out = _inspect_render_iteration(RECORDS, 99, history_entries=[])
    assert "no journal record" in out.lower()


def test_iteration_view_does_not_add_extra_history_truncation():
    long_output = "BUILD OUTPUT " + ("0123456789 " * 40) + "output_full_abc123"
    out = _inspect_render_iteration(
        RECORDS,
        3,
        history_entries=[
            {
                "type": "action",
                "tool_name": "build",
                "success": False,
                "output": long_output,
            },
        ],
    )

    assert long_output in out


def test_iteration_view_expands_output_refs_from_content_entries():
    out = _inspect_render_iteration(
        RECORDS,
        3,
        history_entries=[
            {
                "type": "thought",
                "content": "Need to inspect output_full_abc123 before retrying.",
            },
        ],
        output_lookup=lambda ref: "FULL OUTPUT FROM CONTENT REF",
    )

    assert "FULL OUTPUT FROM CONTENT REF" in out


def test_inspect_iter_can_resolve_global_iteration_without_phase(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module, "_config", None)
    monkeypatch.setattr(logger_module, "_session_logger", None)
    monkeypatch.chdir(tmp_path)
    session_dir = _write_recorded_session(tmp_path)

    result = CliRunner().invoke(
        cli,
        [
            "inspect",
            "unused",
            "--session",
            str(session_dir),
            "--iter",
            "3",
        ],
    )

    assert result.exit_code == 0
    assert "phase build" in result.output
    assert "BUILD FAILED: enforcer" in result.output
    assert not (tmp_path / "logs").exists()


def test_python_module_inspect_binds_history_decoder_before_cli_execution(tmp_path):
    session_dir = _write_recorded_session(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "sag.main",
            "inspect",
            "unused",
            "--session",
            str(session_dir),
            "--iter",
            "3",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "BUILD FAILED: enforcer" in result.stdout
    assert "Inspect failed" not in result.stdout


def test_inspect_iter_expands_full_output_refs(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module, "_config", None)
    monkeypatch.setattr(logger_module, "_session_logger", None)
    monkeypatch.chdir(tmp_path)
    session_dir = _write_recorded_session(tmp_path)

    result = CliRunner().invoke(
        cli,
        [
            "inspect",
            "unused",
            "--session",
            str(session_dir),
            "--iter",
            "3",
        ],
    )

    assert result.exit_code == 0
    assert "FULL STORED BUILD LOG" in result.output
    assert "line 75" in result.output


def test_inspect_phase_list_does_not_create_session_logs(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module, "_config", None)
    monkeypatch.setattr(logger_module, "_session_logger", None)
    monkeypatch.chdir(tmp_path)
    session_dir = _write_recorded_session(tmp_path)

    result = CliRunner().invoke(
        cli,
        [
            "inspect",
            "unused",
            "--session",
            str(session_dir),
        ],
    )

    assert result.exit_code == 0
    assert "build" in result.output
    assert not (tmp_path / "logs").exists()


def test_inspect_rejects_unsafe_phase_names(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module, "_config", None)
    monkeypatch.setattr(logger_module, "_session_logger", None)
    monkeypatch.chdir(tmp_path)
    session_dir = _write_recorded_session(tmp_path)

    result = CliRunner().invoke(
        cli,
        [
            "inspect",
            "unused",
            "--session",
            str(session_dir),
            "--phase",
            "../build",
        ],
    )

    assert result.exit_code == 1
    assert "Invalid phase" in result.output
    assert not (tmp_path / "logs").exists()


def _write_recorded_session(tmp_path):
    session_dir = tmp_path / "recorded-session"
    contexts = session_dir / ".setup_agent" / "contexts"
    journal_dir = contexts / "journal"
    journal_dir.mkdir(parents=True)

    (contexts / "trunk_001.json").write_text(
        json.dumps(
            {
                "todo_list": [
                    {
                        "id": "phase_build",
                        "status": "completed",
                        "notes": "build completed",
                        "key_results": "tests discovered",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (journal_dir / "phase_build.journal.jsonl").write_text(
        "\n".join(json.dumps(record) for record in RECORDS),
        encoding="utf-8",
    )
    (contexts / "phase_build.json").write_text(
        json.dumps(
            {
                "history": [
                    {"type": "thought", "content": "I should build now."},
                    {
                        "type": "action",
                        "tool_name": "build",
                        "success": False,
                        "output": (
                            "BUILD FAILED: enforcer\n"
                            "... [Full output ref: output_full_abc123] ..."
                        ),
                    },
                    {"type": "thought", "content": "Retry with fixed Java."},
                ]
            }
        ),
        encoding="utf-8",
    )
    (contexts / "full_outputs.jsonl").write_text(
        json.dumps(
            {
                "ref_id": "output_full_abc123",
                "tool_name": "build",
                "task_id": "phase_build",
                "output": "FULL STORED BUILD LOG\nline 1\nline 75",
                "output_length": 32,
            }
        ),
        encoding="utf-8",
    )
    return session_dir


# --- sag inspect --turn: one turn end to end -------------------------------
#
# These read a real recorded control ledger. `--turn` derives from that ledger
# through `build_trajectory`, so the journal/contexts session
# `_write_recorded_session` writes above cannot serve them: it has no ledger.

LEDGER_FIXTURE = Path(__file__).parent / "fixtures" / "trajectory" / "sling-commons-osgi-v4"

WELCOME_LINE = "Automated project setup with AI"


@pytest.fixture
def ledger_session(tmp_path):
    """A copy of a recorded session that carries a control ledger."""

    session = tmp_path / "ledger-session"
    shutil.copytree(LEDGER_FIXTURE, session)
    return session


def _inspect(session, *args):
    return CliRunner().invoke(cli, ["inspect", "x", "--session", str(session), *args])


def test_inspect_does_not_print_the_welcome_panel(ledger_session):
    result = _inspect(ledger_session, "--turn", "1")

    assert result.exit_code == 0
    assert WELCOME_LINE not in result.output


def test_turn_shows_one_turns_call_and_result(ledger_session):
    result = _inspect(ledger_session, "--turn", "1")

    assert result.exit_code == 0
    assert "Turn 1" in result.output
    assert "tool: project" in result.output
    assert '"action": "clone"' in result.output
    assert "outcome: ok" in result.output
    # The headers alone prove nothing; the turn must have found a real call
    # and a real result.
    assert "this turn called no tool" not in result.output
    assert "no result is recorded for this turn" not in result.output


def test_a_turn_with_a_gate_names_the_word_it_delivered(ledger_session):
    result = _inspect(ledger_session, "--turn", "5")

    assert result.exit_code == 0
    assert "Gate: success" in result.output
    assert "decision gate-30aaf29fbbace326f509efb0aa6719f4" in result.output


def test_an_unknown_turn_names_the_range_that_exists(ledger_session):
    result = _inspect(ledger_session, "--turn", "9999")

    assert result.exit_code == 1
    assert "recorded turns" in result.output
    assert "1..21" in result.output


def test_a_turn_says_when_this_session_kept_no_bytes_for_a_ref(ledger_session):
    result = _inspect(ledger_session, "--turn", "1")

    assert result.exit_code == 0
    assert "model-visible ref output_16686c22765a" in result.output
    assert "evidence ref output_1b45ec5b0417" in result.output
    assert result.output.count("no bytes for this ref") == 2


def _synthetic_document(*, gate, observation, outputs):
    from sag.trajectory.schema import SessionInfo, Trajectory, Turn

    return Trajectory(
        session=SessionInfo(run_id="synthetic"),
        turns=[
            Turn(
                turn_id=1,
                phase="build",
                actor="model",
                observation=observation,
                gate=gate,
            )
        ],
        outputs=outputs,
    )


def test_a_gate_with_no_decision_id_does_not_print_none(monkeypatch, tmp_path):
    import sag.main as main_module
    from sag.trajectory.schema import GateInfo

    document = _synthetic_document(
        gate=GateInfo(word="success", decision_id=None),
        observation=None,
        outputs={},
    )
    monkeypatch.setattr(main_module, "build_trajectory", lambda *a, **k: document)

    result = _inspect(tmp_path, "--turn", "1")

    assert result.exit_code == 0
    assert "Gate: success" in result.output
    assert "None" not in result.output


def test_an_empty_result_is_not_reported_as_missing_from_the_store(monkeypatch, tmp_path):
    import sag.main as main_module
    from sag.trajectory.schema import ObservationInfo

    document = _synthetic_document(
        gate=None,
        observation=ObservationInfo(outcome="ok", ref="output_empty", evidence_ref="output_absent"),
        outputs={"output_empty": ""},
    )
    monkeypatch.setattr(main_module, "build_trajectory", lambda *a, **k: document)

    result = _inspect(tmp_path, "--turn", "1")

    assert result.exit_code == 0
    assert "the store holds this ref, and it is empty" in result.output
    assert "no bytes for this ref" in result.output
    assert result.output.count("no bytes for this ref") == 1
