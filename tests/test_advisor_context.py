"""Model-budget boundaries and exact evidence delivery, without provider calls."""

import json
from types import SimpleNamespace

import pytest

from sag.agent.advisor_context import (
    AdvisorContextUnavailable,
    AdvisorSection,
    pack_advisor_context,
    pack_advisor_contexts,
)


@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch):
    monkeypatch.setattr(
        "sag.agent.advisor_context.litellm.model_cost",
        {
            "small": {"max_input_tokens": 4096, "max_output_tokens": 512},
            "large": {"max_input_tokens": 32000, "max_output_tokens": 4096},
            "output-only": {"max_tokens": 128000, "max_output_tokens": 128000},
        },
    )


def pack(model="large", **kwargs):
    return pack_advisor_context(
        model=model,
        system="Review the task. You cannot call tools.",
        required=kwargs.pop(
            "required",
            [AdvisorSection("TASK", "clean build; nativeCompile; execute native binary")],
        ),
        optional=kwargs.pop("optional", []),
        max_output_tokens=kwargs.pop("max_output_tokens", 512),
        **kwargs,
    )


def test_model_budget_preserves_task_and_whole_call_result_pair():
    exchange = AdvisorSection("exchange", "ASSISTANT: command\nTOOL: failure " * 100)
    small, sa = pack("small", optional=[exchange])
    large, la = pack("large", optional=[exchange])
    assert sa["input_token_budget"] < la["input_token_budget"]
    assert "nativeCompile; execute native binary" in small[1]["content"]
    assert "ASSISTANT: command" in small[1]["content"]
    assert "TOOL: failure" in small[1]["content"]
    assert exchange.text in large[1]["content"]
    assert sa["sections"][-1]["status"] == "summary"
    assert sa["estimated_input_tokens"] <= sa["input_token_budget"]


def test_configured_window_reserves_output_and_margin():
    _, audit = pack(context_window=6000, max_output_tokens=2000)
    assert audit["input_token_budget"] == 6000 - 2000 - audit["safety_margin_tokens"]
    assert audit["budget_source"] == "configured_context_window"
    assert audit["reserved_output_tokens"] == 2000


def test_unknown_model_is_not_assumed_to_have_executor_window():
    _, audit = pack("private-deployment")
    assert audit["context_window"] is None
    assert audit["budget_source"] == "unknown_window:32768_fallback_budget"
    assert audit["token_count_method"] == "utf8_bytes:conservative_estimate"


def test_catalog_output_limit_is_not_treated_as_input_window():
    _, audit = pack("output-only")
    assert audit["catalog_max_input_tokens"] is None
    assert audit["input_token_budget"] < 32768


@pytest.mark.parametrize(
    "text", ["x " * 5000, "必须执行全部测试🙂" * 1000], ids=["ascii", "unicode"]
)
def test_oversized_protected_context_is_reviewed_without_losing_any_source_characters(text):
    batches = pack_advisor_contexts(
        model="small",
        system="Review this partial task; no global verdict.",
        required=[AdvisorSection("TASK", text, policy="keep")],
        optional=[],
        max_output_tokens=512,
    )
    ranges = []
    for messages, audit in batches:
        part = audit["protected_slice"]
        ranges.append((part["start"], part["end"]))
        assert text[part["start"] : part["end"]] in messages[1]["content"]
        assert audit["estimated_input_tokens"] <= audit["input_token_budget"]
    assert ranges[0][0] == 0 and ranges[-1][1] == len(text)
    assert all(a[1] == b[0] for a, b in zip(ranges, ranges[1:]))


def test_long_output_preserves_ref_head_tail_and_discloses_partial_content():
    source = "COMMAND HEADER\n" + "middle\n" * 2000 + "NATIVE-IMAGE MISSING"
    messages, audit = pack(
        "small",
        optional=[
            AdvisorSection("LOG", source, ref="output_log", policy="excerpt"),
        ],
    )
    content = messages[1]["content"]
    assert "COMMAND HEADER" in content and "NATIVE-IMAGE MISSING" in content
    assert "source=output_log; partial context" in content
    assert audit["sections"][-1]["status"] == "excerpt"
    assert audit["sections"][-1]["source_chars"] == len(source)
    assert audit["estimated_input_tokens"] <= audit["input_token_budget"]


def test_large_section_does_not_displace_smaller_later_evidence():
    messages, _ = pack(
        "small",
        optional=[
            AdvisorSection("HUGE", "x" * 10000),
            AdvisorSection("CI", "GraalVM 21 is configured"),
        ],
    )
    assert "GraalVM 21 is configured" in messages[1]["content"]


def test_zero_remaining_input_and_invalid_output_are_explicit():
    with pytest.raises(AdvisorContextUnavailable, match="no_input_capacity"):
        pack(context_window=512)
    with pytest.raises(AdvisorContextUnavailable, match="output_budget_invalid"):
        pack("small", max_output_tokens=1024)


def test_engine_shares_task_schemas_and_cross_phase_full_output(tmp_path):
    from sag.agent.evidence_state import RunEvidenceState, StateScope
    from sag.tools.base import ToolResult
    from tests.test_advisor_tool import _advisor_engine

    engine = _advisor_engine()
    engine._executor_base_system_prompt = (
        "Pinned CI: clean build; Java 21 nativeCompile; execute binary."
    )
    schema = {"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}}
    engine.llm_client.build_tools_schema = lambda mode: [schema]
    engine.run_evidence_state = RunEvidenceState(run_id="context-test")
    result = ToolResult.completed_success(
        output="CI setup: " + "padding " * 900 + "GraalVM 21", output_ref="output_ci"
    )
    engine.run_evidence_state.ingest_tool_result(
        StateScope.PROJECT_ANALYSIS,
        "file_io",
        result,
        params={"action": "read", "path": "/workspace/project/.github/workflows/ci.yml"},
        source_phase="analyze",
        execution_id="exec-ci",
    )
    before = engine.run_evidence_state.model_dump_json()
    engine.control_event_sink = SimpleNamespace(path=tmp_path / "control_events.jsonl")
    response = engine.consult_advisor()
    assert response.metadata["advisor"] == "advice"
    sent = engine.llm_client.calls[0]["messages"]
    assert engine._executor_base_system_prompt in sent[1]["content"]
    assert "GraalVM 21" in sent[1]["content"]
    assert '"name":"search"' in sent[1]["content"]
    assert engine.run_evidence_state.model_dump_json() == before
    records = [
        json.loads(line)
        for line in (tmp_path / "contexts/full_outputs.jsonl").read_text().splitlines()
    ]
    request = json.loads(
        next(r["output"] for r in records if r["metadata"].get("kind") == "advisor_request")
    )
    assert request["messages"] == sent
    assert request["context"]["estimated_input_tokens"] <= request["context"]["input_token_budget"]


def test_insufficient_context_compresses_and_still_calls_provider():
    from tests.test_advisor_tool import _advisor_engine

    engine = _advisor_engine()
    engine.config.advisor_context_window = 4096
    engine._executor_base_system_prompt = "must retain task " * 200
    engine._advisor_redirect_armed = True
    result = engine.consult_advisor()
    assert len(engine.llm_client.calls) > 1
    assert result.succeeded and result.metadata["advisor"] == "advice"
    assert "Partial review 1/" in result.output
    assert engine._advisor_redirect_armed is False
    assert engine.advisor_telemetry["calls"][0]["context"]["status"] == "ready"


def test_rank_compresses_background_before_task_or_current_failure():
    task = "Keep all steps: clean build; nativeCompile; execute native binary with Java 21."
    failure = "nativeCompile failed, exit=1; Java 17 active; native binary NOT executed."
    messages, audit = pack(
        "small",
        required=[
            AdvisorSection("TASK", task, priority=0, policy="keep"),
            AdvisorSection("CURRENT FAILURE", failure, priority=1, policy="keep"),
            AdvisorSection("BACKGROUND", "old successful attempt\n" * 3000, priority=80),
        ],
    )
    content = messages[1]["content"]
    assert task in content and failure in content
    states = {s["name"]: s["status"] for s in audit["sections"]}
    assert states["TASK"] == states["CURRENT FAILURE"] == "full"
    assert states["BACKGROUND"] == "summary"


def test_priority_beats_input_order_and_verbose_successes():
    failure = "nativeCompile failed; native binary missing; exit=1; output_failure"
    messages, _ = pack(
        "small",
        optional=[
            AdvisorSection("NOISY SUCCESS", "success " * 2000, priority=90),
            AdvisorSection("FAILURE", failure, priority=10),
        ],
    )
    assert failure in messages[1]["content"]
    assert messages[1]["content"].index("FAILURE") < messages[1]["content"].index("NOISY SUCCESS")


def test_structured_summary_keeps_status_counts_and_refs_without_inventing_success():
    summary = (
        "build: failed; exit=1; tests: 1526 passed, 3 skipped; native: pending; output_failure"
    )
    messages, audit = pack(
        "small",
        optional=[
            AdvisorSection("ATTEMPTS", "verbose trace\n" * 4000, summary=summary),
        ],
    )
    assert summary in messages[1]["content"]
    assert "structured" in messages[1]["content"]
    assert audit["sections"][-1]["status"] == "summary"


def test_large_window_keeps_exact_text_and_does_not_summarize():
    task = "Keep literal -DskipTests=false and profile -Pnative."
    messages, audit = pack(required=[AdvisorSection("TASK", task, policy="keep")])
    assert task in messages[1]["content"]
    assert not audit["compression_applied"]


def test_many_required_sections_use_overview_instead_of_cancelling():
    messages, audit = pack(
        "small",
        required=[
            AdvisorSection(f"TASK {i}", f"task {i}: missing evidence " * 100) for i in range(40)
        ],
    )
    assert audit["status"] == "ready"
    assert audit["estimated_input_tokens"] <= audit["input_token_budget"]
    assert "SUMMARY" in messages[1]["content"]


def test_real_engine_compaction_preserves_typed_acceptance_and_persists_sources(tmp_path):
    from sag.agent.acceptance_task import AcceptanceTask
    from tests.test_advisor_tool import _advisor_engine

    engine = _advisor_engine()
    engine.config.advisor_context_window = 6000
    task = AcceptanceTask.model_validate(
        {
            "repo": "apache/freemarker",
            "sha": "a" * 40,
            "steps": [
                {
                    "id": "build",
                    "runner": "gradle",
                    "cwd": ".",
                    "argv": ["./gradlew", "clean", "build"],
                    "java_major": 17,
                },
                {
                    "id": "native",
                    "runner": "gradle",
                    "cwd": ".",
                    "argv": ["./gradlew", ":nativeCompile"],
                    "java_major": 21,
                },
                {"id": "run", "runner": "native", "cwd": ".", "argv": ["./build/native/app"]},
            ],
        }
    )
    engine.orchestrator = SimpleNamespace(
        acceptance_task=task, acceptance_task_root="/workspace/freemarker"
    )
    engine._executor_task_prompt = "Reproduce every official CI step.\n" + task.prompt(
        "/workspace/freemarker"
    )
    engine._executor_base_system_prompt = (
        "General executor instructions\n" * 2000 + "\n\n" + engine._executor_task_prompt
    )
    engine.control_event_sink = SimpleNamespace(path=tmp_path / "control_events.jsonl")
    result = engine.consult_advisor()
    assert result.metadata["advisor"] == "advice"
    content = engine.llm_client.calls[0]["messages"][1]["content"]
    assert task.prompt("/workspace/freemarker") in content
    assert content.count("./gradlew :nativeCompile") == 1
    records = [
        json.loads(line)
        for line in (tmp_path / "contexts/full_outputs.jsonl").read_text().splitlines()
    ]
    source = json.loads(
        next(r["output"] for r in records if r["metadata"].get("kind") == "advisor_context_source")
    )
    assert any("General executor instructions\n" * 2000 in s["text"] for s in source["sections"])
    assert engine.advisor_telemetry["calls"][0]["context"]["source_ref"]


def test_compressed_exchange_keeps_request_and_actual_failed_outcome():
    from sag.agent.react_types import ReActStep, StepType
    from sag.tools.base import ToolResult
    from tests.test_advisor_tool import _advisor_engine

    # Failed operation versus successful tool transport must remain distinct.
    result = ToolResult.completed_failure(
        output="verbose output\n" * 3000,
        error_code="NATIVE_IMAGE_MISSING",
    )
    engine = _advisor_engine(
        steps=[
            ReActStep(
                step_type=StepType.ACTION,
                timestamp="now",
                content="Run the native compile step.",
                tool_name="build",
                tool_call_id="build-1",
                tool_params={"command": "./gradlew nativeCompile -DskipTests=false"},
            ),
            ReActStep(
                step_type=StepType.OBSERVATION,
                timestamp="now",
                tool_call_id="build-1",
                tool_result=result,
                content=result.output,
            ),
        ]
    )
    source = engine._advisor_history_sections()[0]
    messages, audit = pack("small", optional=[source])
    content = messages[1]["content"]
    assert "nativeCompile -DskipTests=false" in content
    assert '"operation_outcome":"failed"' in content
    assert '"invocation_status":"completed"' in content
    assert result.output_ref in content and "NATIVE_IMAGE_MISSING" in content
    assert audit["sections"][-1]["status"] == "summary"


def test_context_window_environment_validation(monkeypatch, tmp_path):
    from pydantic import ValidationError
    from sag.config.settings import Config

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SAG_ADVISOR_CONTEXT_WINDOW", "8192")
    assert Config.from_env().advisor_context_window == 8192
    monkeypatch.setenv("SAG_ADVISOR_CONTEXT_WINDOW", "0")
    with pytest.raises(ValidationError):
        Config.from_env()
    monkeypatch.setenv("SAG_ADVISOR_CONTEXT_WINDOW", "")
    assert Config.from_env().advisor_context_window is None


def test_only_identical_handoff_is_deduplicated():
    from sag.agent.react_types import ReActStep, StepType
    from tests.test_advisor_tool import _advisor_engine

    marker = "=== CUMULATIVE PHASE HANDOFF ==="
    engine = _advisor_engine(
        steps=[
            ReActStep(
                step_type=StepType.SYSTEM_GUIDANCE,
                timestamp="now",
                content=f"=== PHASE: BUILD ===\n{marker}\nJava 17 active",
            )
        ]
    )
    engine._advisor_evidence_digest = lambda: f"{marker}\nJava 17 active"
    assert engine._advisor_messages()[1]["content"].count("Java 17 active") == 1
    engine._advisor_evidence_digest = lambda: f"{marker}\nJava 21 active"
    content = engine._advisor_messages()[1]["content"]
    assert "Java 17 active" in content and "Java 21 active" in content


def test_missing_executor_instructions_is_disclosed_without_calling_provider():
    from tests.test_advisor_tool import _advisor_engine

    engine = _advisor_engine()
    del engine._executor_base_system_prompt
    result = engine.consult_advisor()
    assert result.succeeded and not engine.llm_client.calls
    assert "executor_instructions_unavailable" in result.output
