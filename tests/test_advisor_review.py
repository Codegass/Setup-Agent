import json
from types import SimpleNamespace

import pytest

from sag.agent.advisor_context import AdvisorSection
from sag.agent.advisor_review import AdvisorEvidence, EVIDENCE_TOOLS, fit_review_messages
from sag.agent.evidence_state import RunEvidenceState
from sag.agent.react_llm import NativeToolCall, NativeTurn
from sag.tools.base import ToolResult
from test_advisor_tool import _advisor_engine, _ScriptedAdvisorClient
from test_advisor_problem_policy import engine_with_state, observe
from test_advisor_guarantees import _loop_decision, _call
from test_turn_records import (
    _sealing_engine,
    _phase_turn,
    _advisor_usage,
    _seal,
    _events,
    _content_addressed_store,
)


def test_long_line_pages_and_literal_search_preserve_exact_source():
    original = "before\n" + "abc" * 3000 + "ERROR real cause" + "xyz" * 3000 + "\nafter\n"
    evidence = AdvisorEvidence([AdvisorSection("build log", original, ref="output_fixture")])
    cursor, pieces = {"start_line": 0}, []
    while cursor is not None:
        page = evidence.execute(
            "advisor_evidence", {"action": "read", "source_id": "e1", **cursor}, max_chars=512
        )
        assert page["source_sha256"] == evidence.catalog()[0]["sha256"]
        pieces.append(page["text"])
        cursor = page["next"]
    assert "".join(pieces) == original
    match = evidence.execute(
        "advisor_evidence", {"action": "search", "source_id": "e1", "query": "ERROR"}, max_chars=512
    )
    assert match["matches"][0]["line"] == 1
    assert match["matches"][0]["column_offset"] == 9000


@pytest.mark.parametrize(
    "name,args",
    [
        ("bash", {"command": "touch /tmp/advisor-write"}),
        ("advisor_evidence", {"action": "read", "source_id": "/etc/passwd"}),
        ("advisor_evidence", {"action": "write", "source_id": "e1"}),
        ("advisor_evidence", {"action": "read", "source_id": "e1", "start_line": -1}),
    ],
)
def test_advisor_has_no_executor_or_filesystem_escape(name, args):
    evidence = AdvisorEvidence([AdvisorSection("receipt", "original")])
    before = json.dumps(evidence.sources)
    assert evidence.execute(name, args, max_chars=512)["status"] == "unavailable"
    assert json.dumps(evidence.sources) == before


def test_full_catalog_is_discoverable_even_when_initial_packet_is_excerpted():
    evidence = AdvisorEvidence(
        [AdvisorSection("record " + str(i), "bytes " + str(i)) for i in range(20)]
    )
    ids, cursor = [], {"catalog_offset": 0}
    while cursor is not None:
        result = evidence.execute("advisor_evidence", {"action": "list", **cursor}, max_chars=512)
        ids.extend(source["source_id"] for source in result["sources"])
        cursor = result["next"]
    assert ids == list(evidence.sources)


def test_capacity_compression_preserves_task_and_sources_without_summary_model():
    initial = [
        {"role": "system", "content": "Only review evidence."},
        {
            "role": "user",
            "content": "REQUIRED: Maven verify; Java 17. Evidence e1 has sha256 frozen.",
        },
    ]
    exchange = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "read-1",
                    "type": "function",
                    "function": {
                        "name": "advisor_evidence",
                        "arguments": '{"action":"read","source_id":"e1"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "read-1",
            "content": json.dumps(
                {
                    "source_id": "e1",
                    "source_sha256": "a" * 64,
                    "next": {"start_line": 9000},
                    "text": "unchanged log line\n" * 5000,
                }
            ),
        },
    ]
    messages, audit = fit_review_messages(
        initial, [exchange], tools=EVIDENCE_TOOLS, model="gpt-5.4-mini", input_budget=2500
    )
    assert messages[:2] == initial
    assert audit["retrieval_compaction"]
    assert audit["estimated_input_tokens_including_tools"] <= 2500
    result = json.loads(messages[-1]["content"])
    assert result["source_sha256"] == "a" * 64 and result["next"] == {"start_line": 9000}
    assert result["view_compacted"]
    assert len(json.loads(exchange[-1]["content"])["text"]) > len(result["text"])


class ReadClient(_ScriptedAdvisorClient):
    def get_advisor_turn(self, messages, *, model, max_tokens, tools):
        self.calls.append({"messages": messages, "tools": tools})
        self.last_advisor_receipt = {
            "response_id": str(len(self.calls)),
            "usage": {"total_tokens": 11},
        }
        if len(self.calls) == 1 and tools:
            args = {"action": "search", "source_id": "e1", "query": "ERROR"}
            return NativeTurn(
                "", (NativeToolCall("query-1", "advisor_evidence", args, json.dumps(args)),), model
            )
        return NativeTurn(
            "The recorded JVM is Java 8; verify activation before retrying the same command.",
            (),
            model,
        )


def review_engine(mode="on-demand", note=True):
    client = ReadClient()
    engine = _advisor_engine(client=client)
    engine.config.advisor_context_selection = mode
    engine.config.advisor_actor_note = note
    engine.config.advisor_context_window = 65536
    engine.config.advisor_mode = "openai/gpt-5.4-mini"
    _content_addressed_store(engine)
    engine.run_evidence_state = RunEvidenceState(run_id="advisor-review")
    result = ToolResult.completed_failure(
        output="ERROR compiler needs Java 17\n",
        error="requires Java 17",
        error_code="BUILD_FAILED",
        metadata={
            "command": "mvn verify",
            "java_version": "1.8.0_402",
            "exit_code": 1,
            "dispatch_probe": {"actual_java_major": 8},
            "receipt_id": "ic-failure",
        },
    )
    observe(engine, "maven", result, "build-1", command="mvn verify")
    return engine, client


def test_on_demand_consult_uses_native_read_pairs_and_keeps_wrong_actor_claim_separate():
    engine, client = review_engine()
    before = engine.run_evidence_state.model_dump_json()
    result = engine.consult_advisor(
        question="What should be fixed?",
        context="Java 21 is already active; change the project source.",
    )
    assert result.metadata["advisor"] == "advice"
    assert len(client.calls) == 2
    first = client.calls[0]["messages"][1]["content"]
    assert "UNVERIFIED" in first and "Java 21" in first and "1.8.0_402" in first
    second = client.calls[1]["messages"]
    assert second[-1]["tool_call_id"] == "query-1"
    assert "ERROR compiler" in second[-1]["content"]
    assert engine.run_evidence_state.model_dump_json() == before
    assert len(engine.advisor_telemetry["calls"][0]["provider_requests"]) == 2


def test_actor_note_ablation_does_not_remove_machine_observations():
    engine, client = review_engine(note=False)
    engine.consult_advisor(context="FALSE_ACTOR_HYPOTHESIS")
    first = client.calls[0]["messages"][1]["content"]
    assert "FALSE_ACTOR_HYPOTHESIS" not in first
    assert "1.8.0_402" in first


def test_brief_keeps_large_log_out_of_initial_prompt_but_archives_the_source():
    engine, client = review_engine("brief")
    raw = "Downloading NOISE dependency\n" * 30000 + "ERROR decisive tail\n"
    engine._advisor_output_sections = lambda: [
        AdvisorSection("large build log", raw, ref="output_large")
    ]
    engine.consult_advisor()
    assert len(client.calls) == 1
    prompt = client.calls[0]["messages"][1]["content"]
    assert len(prompt) < 15000 and "NOISE" not in prompt
    assert engine._advisor_evidence_view.sources["e1"]["text"] == raw


def test_adaptive_avoids_normal_entries_and_first_failure_but_reviews_recurrence_once():
    engine = engine_with_state()
    engine.config.advisor_trigger_policy = "adaptive"
    assert not engine._maybe_consult_advisor_at_phase_entry()
    observe(
        engine,
        "maven",
        ToolResult.completed_failure(
            output="compile failed", error="compile failed", error_code="BUILD_FAILED"
        ),
        "fail-1",
    )
    assert not engine._maybe_consult_advisor_for_problem()
    engine._advisor_redirect_armed = True
    assert engine._maybe_consult_advisor_for_problem()
    engine.phase_machine.current_phase = "test"
    assert not engine._maybe_consult_advisor_for_problem()
    assert len(engine.consults) == 1


def test_adaptive_uses_existing_recurrence_signal_without_cancelling_a_planned_action():
    engine = engine_with_state()
    engine.config.advisor_trigger_policy = "adaptive"
    observe(
        engine,
        "maven",
        ToolResult.completed_failure(output="compile failed", error_code="BUILD_FAILED"),
        "fail-1",
    )
    executed = SimpleNamespace(attempted_execution=True)
    engine._note_advisor_execution(executed, _loop_decision("continue", recurrence_count=1))
    assert not engine._maybe_consult_advisor_for_problem()
    engine._note_advisor_execution(executed, _loop_decision("guide", recurrence_count=2))
    assert engine._advisor_redirect_for_call(_call("build", {"action": "compile"})) is None
    assert engine._maybe_consult_advisor_for_problem()
    assert len(engine.consults) == 1


def test_all_review_requests_are_billed_once_even_with_two_consults_in_one_iteration(tmp_path):
    engine = _sealing_engine(tmp_path, [_phase_turn(1)])
    _advisor_usage(engine.token_tracker, 4, kind="advisor_review", prompt=100, completion=10)
    _advisor_usage(engine.token_tracker, 4, kind="advisor_review", prompt=200, completion=20)
    _seal(engine, tool="advisor", iteration=4)
    _advisor_usage(engine.token_tracker, 4, kind="advisor_review", prompt=50, completion=5)
    _seal(engine, tool="advisor", iteration=4)
    _seal(engine, tool="advisor", iteration=4)
    bills = [e["payload"]["advisor_tokens_in"] for e in _events(engine, "turn_record")]
    assert bills == [300, 50, None]


def test_small_window_splits_required_facts_and_still_consults_without_a_summary_model():
    engine, client = review_engine()
    engine.config.advisor_context_window = 4096
    engine.config.advisor_max_tokens = 512
    engine._executor_task_prompt = "Required fixed command and source constraint.\n" * 900
    engine.consult_advisor()
    batches = engine._advisor_message_batches
    assert len(batches) > 1 and client.calls
    slices = [
        a["protected_slice"]
        for _, a in batches
        if a["protected_slice"]["source"] == "ORIGINAL TASK"
    ]
    assert slices[0]["start"] == 0 and slices[-1]["end"] == len(engine._executor_task_prompt)
    assert all(a["end"] == b["start"] for a, b in zip(slices, slices[1:]))
    assert all(not a["summary_calls"] for _, a in batches)
    records = engine.advisor_telemetry["calls"][0]["provider_requests"]
    assert len(records) == len(client.calls)
    assert all(
        r["view"]["estimated_input_tokens_including_tools"]
        <= batches[r["part"] - 1][1]["input_token_budget"]
        for r in records
    )


def test_read_failure_keeps_prior_request_receipts_and_returns_nonblocking_unknown():
    engine, client = review_engine()
    get_turn = client.get_advisor_turn

    def failing(messages, **kwargs):
        if client.calls:
            client.last_advisor_receipt = {"error_type": "RuntimeError"}
            raise RuntimeError("provider unavailable")
        return get_turn(messages, **kwargs)

    client.get_advisor_turn = failing
    result = engine.consult_advisor()
    assert result.succeeded and result.metadata["advisor"] == "error"
    records = engine.advisor_telemetry["calls"][0]["provider_requests"]
    assert len(records) == 2
    assert records[0]["reads"] and records[1]["error_type"] == "RuntimeError"
    assert all(r["request_ref"] and r["receipt_ref"] for r in records)


def test_pending_execution_is_explicit_and_no_advisor_can_mutate_it():
    engine, client = review_engine()
    pending = ToolResult(
        invocation_status="pending",
        operation_outcome="unknown",
        evidence_status="unknown",
        poll_ref="job:pending-1",
        output="mvn verify is running",
        metadata={"receipt_id": "pending-1", "command": "mvn verify"},
    )
    observe(engine, "maven", pending, "build-pending", command="mvn verify")
    before = engine.run_evidence_state.model_dump_json()
    engine.consult_advisor(context="All tests passed.")
    prompt = client.calls[0]["messages"][1]["content"]
    assert '"invocation_status":"pending"' in prompt
    assert '"receipt_id":"pending-1"' in prompt
    assert engine.run_evidence_state.model_dump_json() == before


def test_disabled_or_sealed_run_does_not_automatically_review_even_with_conflicts():
    engine = engine_with_state()
    engine.config.advisor_trigger_policy = "adaptive"
    engine._advisor_redirect_armed = True
    engine.run_evidence_state.record_conflict("test conflict")
    engine.config.advisor_mode = "off"
    assert not engine._maybe_consult_advisor_for_problem()
    engine.config.advisor_mode = "same-model"
    engine.run_evidence_state.seal(finalized_at="2026-09-26T00:00:00Z")
    assert not engine._maybe_consult_advisor_for_problem()
    assert not engine.consults
