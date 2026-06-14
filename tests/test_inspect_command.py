"""sag inspect: render journals + phase history for debugging what the model
saw (spec §7). Helpers are pure; sources (container/session-dir) are injected."""

import json

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
