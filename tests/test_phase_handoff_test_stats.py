"""Regression for the actual RocketMQ Report window that lost 3220/3201."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sag.agent.evidence_state import RunEvidenceState
from sag.agent.phase_handoff import PhaseHandoff
from sag.agent.phase_machine import PhaseMachine
from sag.agent.react_engine import ReActEngine
from sag.agent.react_prompt_builder import ReActPromptBuilder


@pytest.fixture
def stats():
    fixture = Path(__file__).parent / "fixtures/phase_handoff/rocketmq-stats.json"
    return json.loads(fixture.read_text())["test_stats"]


def _handoff(tmp_path, stats):
    state = RunEvidenceState(run_id="rocketmq-handoff-regression")
    handoff = PhaseHandoff(state, storage_path=tmp_path / "phase-handoff.json")
    state.register_fact(
        scope="test_runtime",
        key="test.stats",
        value=stats,
        source_ref="output_gate_test",
        source_phase="test",
    )
    return state, handoff


def _visible_stats(prompt):
    line = next(line for line in prompt.splitlines() if line.startswith("- test.stats "))
    return json.JSONDecoder().raw_decode(line.split("]=", 1)[1])[0]


@pytest.mark.parametrize("with_prompt_builder", [False, True])
def test_report_intro_keeps_actual_outcomes_separate_from_discovery(
    tmp_path, stats, with_prompt_builder
):
    _, handoff = _handoff(tmp_path, stats)
    engine = ReActEngine.__new__(ReActEngine)
    engine.phase_machine = PhaseMachine(start_phase="report")
    engine.phase_handoff = handoff
    engine.config = SimpleNamespace(phase_handoff_char_budget=6000)
    engine._phase_budget_numbers = lambda phase: (100, 0, 20)
    engine._detected_build_system = lambda: "maven"
    if with_prompt_builder:
        engine.prompt_builder = ReActPromptBuilder.__new__(ReActPromptBuilder)

    prompt = engine._phase_intro_step().content

    visible = _visible_stats(prompt)
    assert visible["discovered"] == 3487
    assert (
        visible["raw"]
        == visible["unique"]
        == {
            "executed": 3220,
            "passed": 3201,
            "failed": 0,
            "errors": 0,
            "skipped": 19,
        }
    )
    assert visible["execution_state"] == "completed"
    assert visible["receipt_scoped"] is True
    assert visible["execution_receipt_ids"] == stats["execution_receipt_ids"]
    assert "discovered is inventory, not executed or passed" in prompt
    assert "phase=test ref=output_gate_test" in prompt


@pytest.mark.parametrize("execution_state", ["partial", "failed", "unavailable", None])
def test_unavailable_or_incomplete_counts_are_not_reinterpreted(tmp_path, stats, execution_state):
    stats.update(execution_state=execution_state, receipt_scoped=None, denominator_basis="partial")
    stats["conflicts"] = ["test_report_parse_error", "test_execution_incomplete"]
    stats["raw"]["passed"] = None
    stats["unique"]["executed"] = 3000
    _, handoff = _handoff(tmp_path, stats)

    visible = _visible_stats(handoff.project_for("report", char_budget=6000).to_prompt_text())

    assert visible == stats
    assert visible["raw"]["passed"] is None
    assert visible["raw"]["executed"] != visible["unique"]["executed"]


def test_large_detail_arrays_remain_accessible_without_displacing_counts(tmp_path, stats):
    stats["test_modules"] = [f"/workspace/module-{i}" for i in range(1000)]
    stats["conflicts"] = [f"unavailable-report-{i}" for i in range(100)]
    _, handoff = _handoff(tmp_path, stats)

    projection = handoff.project_for("report", char_budget=2000)
    prompt = projection.to_prompt_text()
    visible = _visible_stats(prompt)

    assert len(prompt) <= 2000
    assert visible["raw"] == stats["raw"]
    assert visible["conflicts"]["detail_count"] == 100
    assert visible["test_modules"]["detail_count"] == 1000
    assert str(handoff.storage_path) in prompt
    assert not projection.has_omissions  # Detail truncation alone must still expose the file.
    persisted = json.loads(handoff.storage_path.read_text())
    assert persisted["facts"][0]["value"] == stats


def test_report_reserves_latest_count_fact_before_historical_failures(tmp_path, stats):
    state, handoff = _handoff(tmp_path, stats)
    for i in range(30):
        state.register_fact(
            scope="artifacts",
            key=f"build.detail_{i}",
            value="artifact " * 80,
            source_ref=f"output_build_{i}",
            source_phase="build",
        )
        state.record_blocker(f"historical-blocker-{i}", evidence_ref=f"output_failed_{i}")
    latest = {**stats, "discovered": None}
    state.register_fact(
        scope="test_runtime",
        key="test.stats",
        value=latest,
        source_ref="output_latest_test",
        source_phase="test",
    )

    projection = handoff.project_for("report", char_budget=1800)
    prompt = projection.to_prompt_text()

    assert len(prompt) <= 1800
    assert _visible_stats(prompt) == latest
    assert projection.has_omissions
    assert str(handoff.storage_path) in prompt


def test_tiny_budget_omits_whole_fact_with_retrievable_reference(tmp_path, stats):
    _, handoff = _handoff(tmp_path, stats)
    projection = handoff.project_for("report", char_budget=500)
    prompt = projection.to_prompt_text()

    assert len(prompt) <= 500
    assert "- test.stats" not in prompt
    assert projection.omitted_fact_count == 1
    assert str(handoff.storage_path) in prompt


def test_other_long_facts_have_valid_preview_and_full_file(tmp_path):
    state, handoff = _handoff(tmp_path, {})
    value = {"detail": "很长的事实" * 200}
    state.register_fact(
        scope="artifacts",
        key="build.details",
        value=value,
        source_ref="output_build_detail",
        source_phase="build",
    )
    prompt = handoff.project_for("test", char_budget=4000).to_prompt_text()
    line = next(line for line in prompt.splitlines() if line.startswith("- build.details "))
    preview = json.JSONDecoder().raw_decode(line.split("]=", 1)[1])[0]

    assert preview["truncated"] is True
    assert str(handoff.storage_path) in prompt
    assert json.loads(handoff.storage_path.read_text())["facts"][0]["value"] == value
