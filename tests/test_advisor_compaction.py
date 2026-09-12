import json

import pytest

from sag.agent.advisor_compaction import AdvisorCompactor
from sag.agent.advisor_context import AdvisorSection, pack_advisor_context, pack_advisor_contexts


def test_repeated_log_lines_compact_before_paying_for_semantic_summary():
    calls = []
    source = "same download line\n" * 3000 + "Native compile failed; binary NOT run."
    compactor = AdvisorCompactor(
        model="gpt-5.4-mini", complete=lambda *a, **k: calls.append(True), question="native"
    )
    messages, _ = pack_advisor_context(
        model="openai/gpt-5.6-terra",
        system="Review",
        required=[],
        optional=[AdvisorSection("LOG", source)],
        max_output_tokens=2048,
        context_window=4096,
        summarizer=compactor,
    )
    assert not calls
    assert "repeated 3000 times" in messages[1]["content"]
    assert "binary NOT run" in messages[1]["content"]


def test_overflow_history_is_compacted_together_without_summarizing_a_short_fragment():
    calls = []

    def complete(messages, **kwargs):
        source = json.loads(messages[1]["content"])["source"]
        calls.append(source)
        return response("Earlier failure remains unresolved.", ["OLD FAILURE remains unresolved."])

    compactor = AdvisorCompactor(
        model="gpt-5.4-mini", complete=complete, question="Fix current failure"
    )
    history = [
        AdvisorSection(
            f"attempt {i}",
            "\n".join(f"attempt {i} download record {j}" for j in range(100)),
            priority=30,
        )
        for i in range(12)
    ]
    history[-1] = AdvisorSection("earlier failure", "OLD FAILURE remains unresolved.", priority=30)
    batches = pack_advisor_contexts(
        model="openai/gpt-5.6-terra",
        system="Review",
        required=[],
        optional=history,
        max_output_tokens=2048,
        context_window=4096,
        summarizer=compactor,
    )
    assert len(calls) == 1 and "attempt 0" in calls[0] and "OLD FAILURE" in calls[0]
    assert "OLD FAILURE remains unresolved." in batches[0][0][1]["content"]


def response(summary, quotes, **extra):
    return {
        "finish_reason": "stop",
        "content": json.dumps({"summary": summary, "quotes": quotes}),
        **extra,
    }


def test_semantic_compression_is_lazy_and_preserves_protected_task(monkeypatch):
    calls = []
    source = (
        "\n".join(f"Successful download {i}." for i in range(2000))
        + "\nNative compile failed: no native-image."
    )

    def complete(messages, **kwargs):
        calls.append(messages)
        return response(
            "The native compiler is missing.", ["Native compile failed: no native-image."]
        )

    compactor = AdvisorCompactor(
        model="openai/gpt-5.4-mini", complete=complete, question="Finish native tests"
    )
    params = dict(
        model="openai/gpt-5.6-terra",
        system="Review",
        max_output_tokens=2048,
        required=[
            AdvisorSection(
                "TASK", "Java 21; nativeCompile; run binary; no skipTests", policy="keep"
            )
        ],
        optional=[AdvisorSection("LOG", source, priority=20)],
        summarizer=compactor,
    )
    _, full = pack_advisor_context(**params, context_window=65536)
    assert not calls and not full["compression_applied"]
    messages, small = pack_advisor_context(**params, context_window=4096)
    assert calls and "Native compile failed: no native-image." in messages[1]["content"]
    assert "Java 21; nativeCompile; run binary; no skipTests" in messages[1]["content"]
    assert small["estimated_input_tokens"] <= small["input_token_budget"]
    assert compactor.calls[0]["status"] == "validated_quotes"


@pytest.mark.parametrize(
    "bad",
    [
        response("Everything passed.", ["invented quote"]),
        {"finish_reason": "length", "content": "{}"},
        {"finish_reason": "stop", "content": "not json"},
    ],
)
def test_invalid_summary_retries_once_then_falls_back_and_advisor_still_receives_failure(bad):
    compactor = AdvisorCompactor(
        model="openai/gpt-5.4-mini", complete=lambda *a, **k: bad, question="Complete all tests"
    )
    source = (
        "\n".join(f"download noise {i}" for i in range(5000))
        + "\nexit=1; Native compilation failed; binary NOT run."
    )
    messages, _ = pack_advisor_context(
        model="openai/gpt-5.6-terra",
        system="Review",
        max_output_tokens=2048,
        context_window=4096,
        required=[AdvisorSection("TASK", "Native tests")],
        optional=[AdvisorSection("LOG", source)],
        summarizer=compactor,
    )
    assert len(compactor.calls) == 2
    assert all(c["status"] == "fallback" for c in compactor.calls)
    assert "binary NOT run" in messages[1]["content"]
    assert "Everything passed" not in messages[1]["content"]


def test_summarizer_uses_its_own_window_and_discloses_unvisited_remainder():
    requests = []

    def complete(messages, **kwargs):
        requests.append(messages)
        source = json.loads(messages[1]["content"])["source"]
        return response("Partial source reviewed.", [source[:50]])

    compactor = AdvisorCompactor(
        model="private-small-model",
        context_window=8000,
        complete=complete,
        question="task " * 10000,
    )
    text = "Observed output with a long unrelated history.\n" * 4000 + "NEW FAILURE"
    note = compactor(AdvisorSection("history", text), 1000)
    assert len(requests) == 4
    assert all(c["estimated_input_tokens"] <= c["input_budget"] for c in compactor.calls)
    assert compactor.calls[0]["question_is_partial"]
    assert "UNSUMMARIZED remainder" in note and "NEW FAILURE" in note


def test_quote_provenance_is_not_claimed_as_semantic_entailment():
    note = AdvisorCompactor.validate(
        json.dumps({"summary": "Possible interpretation", "quotes": ["raw fact"]}), "raw fact"
    )
    assert "Interpretation (unverified)" in note
    with pytest.raises(ValueError, match="unsupported_source_quote"):
        AdvisorCompactor.validate(json.dumps({"summary": "x", "quotes": ["absent"]}), "raw fact")


def test_summary_client_has_no_tools_and_separate_usage_and_receipt(monkeypatch):
    from tests.test_react_llm import make_client, make_config, make_response

    client = make_client(make_config(action_model="gpt-5.4-mini", action_provider="openai"))
    client.last_advisor_receipt = {"content": "prior advice"}
    calls = []
    answer = make_response('{"summary":"pending","quotes":["pending"]}')
    answer.choices[0].finish_reason = "stop"
    monkeypatch.setattr("litellm.completion", lambda **kw: calls.append(kw) or answer)
    client.summarize_advisor_context([{"role": "user", "content": "pending"}], max_tokens=4096)
    assert calls[0]["model"] == "gpt-5.4-mini" and "tools" not in calls[0]
    assert calls[0]["max_tokens"] == 4096 and calls[0]["drop_params"] is False
    assert client.token_tracker.calls[0][2] == "advisor_compression"
    assert client.last_advisor_receipt == {"content": "prior advice"}


def test_live_engine_semantic_wiring_preserves_evidence_and_persists_summary_requests(tmp_path):
    from types import SimpleNamespace
    from sag.agent.evidence_state import RunEvidenceState, StateScope
    from sag.tools.base import ToolResult
    from tests.test_advisor_tool import _advisor_engine

    engine = _advisor_engine()
    engine.config.advisor_context_compression = "semantic"
    engine.config.advisor_context_window = 8192
    engine.config.get_litellm_model_name = lambda _: "gpt-5.4-mini"
    calls = []

    def complete(messages, **kwargs):
        calls.append(messages)
        return response("Native compiler is absent.", ["native-image NOT installed."])

    engine.llm_client.summarize_advisor_context = complete
    engine.control_event_sink = SimpleNamespace(path=tmp_path / "control_events.jsonl")
    engine.run_evidence_state = RunEvidenceState(run_id="semantic-live-wiring")
    result = ToolResult.completed_failure(
        output="\n".join(f"download noise {i}" for i in range(3000))
        + "\nnative-image NOT installed.",
        error_code="NATIVE_IMAGE_MISSING",
    )
    engine.run_evidence_state.ingest_tool_result(
        StateScope.PROJECT_ANALYSIS,
        "build",
        result,
        params={"command": "nativeCompile"},
        source_phase="build",
    )
    before = engine.run_evidence_state.model_dump_json()
    reply = engine.consult_advisor()
    assert reply.metadata["advisor"] == "advice" and calls
    assert engine.run_evidence_state.model_dump_json() == before
    record = engine.advisor_telemetry["calls"][0]["context"]
    assert record["summary_calls"][0]["request_ref"] and record["summary_calls"][0]["receipt_ref"]
    assert "native-image NOT installed." in engine.llm_client.calls[0]["messages"][1]["content"]
