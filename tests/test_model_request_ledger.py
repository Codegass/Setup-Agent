"""Prospective request accounting uses fake provider calls, never a service."""

import json
from types import SimpleNamespace

import pytest

from sag.agent.advisor_compaction import AdvisorCompactor
from sag.agent.advisor_context import AdvisorSection
from sag.agent.model_request_ledger import ModelRequestLedger, read_model_request_ledger
from sag.agent.react_engine import ReActEngine
from sag.agent.token_tracker import TokenTracker
from sag.benchmark.campaign_telemetry import read_response_usage
from test_react_llm import make_client, make_config, make_response


def response(content="answer", usage=True):
    answer = make_response(content)
    answer.model = "returned-model"
    answer.choices[0].finish_reason = "stop"
    if usage:
        answer.usage = SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
        )
    return answer


def client_with_ledger(tmp_path):
    client = make_client(make_config(action_provider="openai", action_model="gpt-5.4-mini"))
    client.token_tracker = TokenTracker()
    client.token_tracker.set_iteration(3)
    client.request_ledger = ModelRequestLedger(tmp_path, "run-1")
    return client


def test_request_identity_binds_window_phase_and_response(tmp_path, monkeypatch):
    import hashlib
    client = client_with_ledger(tmp_path)
    client.trace_context = lambda: {"phase": "build"}
    returned = response()
    returned.id = "resp-window-1"
    monkeypatch.setattr("litellm.completion", lambda **params: returned)
    messages = [{"role": "user", "content": "current build window"}]
    client.get_native_turn(messages)
    end = json.loads(next(client.request_ledger.directory.glob("*.end.json")).read_text())
    expected = hashlib.sha256(json.dumps(messages, sort_keys=True, ensure_ascii=False,
                                        separators=(",", ":")).encode()).hexdigest()
    assert end["request_phase"] == "build"
    assert end["messages_sha256"] == expected == client.last_actor_receipt["messages_sha256"]
    assert end["request_id"] == client.last_actor_receipt["request_id"]
    assert end["response_id"] == client.last_actor_receipt["response_id"] == "resp-window-1"


def test_every_role_and_advisor_part_is_durable_before_dispatch_and_matches_csv(
    tmp_path, monkeypatch
):
    client = client_with_ledger(tmp_path)
    requests = []

    def complete(**params):
        starts = list(client.request_ledger.directory.glob("*.begin.json"))
        ends = list(client.request_ledger.directory.glob("*.end.json"))
        assert len(starts) == len(ends) + 1
        assert all(json.loads(path.read_bytes())["run_id"] == "run-1" for path in starts)
        requests.append(params)
        return response("private response never saved in ledger")

    monkeypatch.setattr("litellm.completion", complete)
    secret = "private prompt never saved in ledger"
    messages = [{"role": "user", "content": secret}]
    client.get_native_turn(messages)
    client.get_advisor_response(messages, model="advisor-model", max_tokens=100)
    client.get_advisor_response(messages, model="advisor-model", max_tokens=100)
    client.summarize_advisor_context(messages, max_tokens=100)
    client.request_ledger.close()
    ledger = read_model_request_ledger(tmp_path, "run-1")
    assert ledger["harness_requests_complete"] is True
    assert ledger["model_calls_complete"] is False
    assert ledger["request_counts"]["usage_known"] == 4
    assert sorted(call["role"] for call in ledger["model_calls"]) == [
        "actor",
        "advisor",
        "advisor",
        "summary",
    ]
    assert len({call["request_id"] for call in ledger["model_calls"]}) == 4
    assert requests[0]["messages"] == messages
    assert "num_retries" not in requests[0] and "num_retries" not in requests[1]
    assert requests[-1]["num_retries"] == 0  # Existing summary setting preserved.
    csv = tmp_path / "token_usage.csv"
    assert client.token_tracker.export_to_csv(str(csv))
    old = read_response_usage(csv.read_bytes())
    assert (
        sum(c["input_tokens"] + c["output_tokens"] for c in ledger["model_calls"])
        == sum(c["input_tokens"] + c["output_tokens"] for c in old["model_calls"])
        == 60
    )
    retained = "\n".join(p.read_text() for p in client.request_ledger.directory.glob("*.json"))
    assert secret not in retained and "private response" not in retained


def test_engine_actor_retry_records_failed_request_and_success_separately(tmp_path, monkeypatch):
    client = client_with_ledger(tmp_path)
    outcomes = [RuntimeError("credential-shaped-private-error"), response()]

    def complete(**params):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("litellm.completion", complete)
    engine = object.__new__(ReActEngine)
    engine.llm_client = client
    engine._is_transient_provider_error = lambda error: isinstance(error, RuntimeError)
    engine._native_turn_with_retry([], sleep=lambda _: None)
    client.request_ledger.close()
    ledger = read_model_request_ledger(tmp_path, "run-1")
    assert ledger["request_counts"] == {
        "started": 2,
        "returned": 1,
        "error": 1,
        "interrupted": 0,
        "in_flight": 0,
        "usage_known": 1,
        "usage_unknown": 1,
    }
    assert len(ledger["model_calls"]) == 1 and ledger["harness_requests_complete"] is True
    assert all(
        "credential-shaped" not in path.read_text()
        for path in client.request_ledger.directory.glob("*.json")
    )


def test_compression_rejected_response_retry_counts_both_requests(tmp_path, monkeypatch):
    client = client_with_ledger(tmp_path)
    outcomes = [
        response("not JSON"),
        response(json.dumps({"summary": "Failed", "quotes": ["Build failed."]})),
    ]
    monkeypatch.setattr("litellm.completion", lambda **params: outcomes.pop(0))
    compactor = AdvisorCompactor(
        model="openai/gpt-5.4-mini",
        complete=client.summarize_advisor_context,
        question="What failed?",
    )
    assert compactor(AdvisorSection("LOG", "Build failed."), 100)
    client.request_ledger.close()
    ledger = read_model_request_ledger(tmp_path, "run-1")
    assert [call["role"] for call in ledger["model_calls"]] == ["summary", "summary"]
    assert ledger["request_counts"]["usage_known"] == 2


@pytest.mark.parametrize("outcome", ["missing", "invalid", "interrupt"])
def test_unknown_usage_and_interruptions_are_not_zero_tokens(tmp_path, monkeypatch, outcome):
    client = client_with_ledger(tmp_path)
    answer = response(usage=False)
    if outcome == "invalid":
        answer.usage = SimpleNamespace(prompt_tokens=True, completion_tokens=5, total_tokens=6)

    def complete(**params):
        if outcome == "interrupt":
            raise KeyboardInterrupt()
        return answer

    monkeypatch.setattr("litellm.completion", complete)
    if outcome == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            client.get_advisor_response([], model="advisor-model", max_tokens=100)
    else:
        client.get_advisor_response([], model="advisor-model", max_tokens=100)
    client.request_ledger.close()
    ledger = read_model_request_ledger(tmp_path, "run-1")
    assert ledger["model_calls"] == []
    assert ledger["request_counts"]["usage_unknown"] == 1
    assert ledger["request_counts"]["interrupted"] == (1 if outcome == "interrupt" else 0)
    assert ledger["model_calls_complete"] is False


def test_process_disappearance_keeps_inflight_begin_without_fabricated_end(tmp_path):
    ledger = ModelRequestLedger(tmp_path, "run-1")
    ledger.begin(role="actor", model="test-model")
    out = read_model_request_ledger(tmp_path, "run-1")
    assert out["available"] is True and out["harness_requests_complete"] is False
    assert out["request_counts"]["in_flight"] == out["request_counts"]["usage_unknown"] == 1
    assert not list(ledger.directory.glob("*.end.json"))


def test_failed_begin_persistence_preserves_runtime_but_records_coverage_gap(tmp_path, monkeypatch):
    import sag.agent.model_request_ledger as module

    client = client_with_ledger(tmp_path)
    write = module._write_once

    def fail_begin(path, value):
        if path.name.endswith(".begin.json"):
            raise OSError("disk rejected write")
        return write(path, value)

    monkeypatch.setattr(module, "_write_once", fail_begin)
    monkeypatch.setattr("litellm.completion", lambda **params: response())
    assert client.get_native_turn([]).text == "answer"
    client.request_ledger.close()
    out = read_model_request_ledger(tmp_path, "run-1")
    assert out["harness_requests_complete"] is False and out["usage_errors"]
    assert out["model_calls"] == []
    assert out["attempted_requests"] == out["unobserved_requests"] == 1
    assert out["request_counts"]["usage_unknown"] == 1


def test_engine_uses_current_host_session_and_closes_ledger_even_without_csv_rows(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "sag.config.logger.get_session_logger", lambda: SimpleNamespace(session_log_dir=tmp_path)
    )
    engine = object.__new__(ReActEngine)
    engine.run_evidence_state = SimpleNamespace(run_id="run-1")
    engine.token_tracker = TokenTracker()
    engine.model_request_ledger = engine._create_model_request_ledger()
    engine._export_token_usage_csv()
    out = read_model_request_ledger(tmp_path, "run-1")
    assert out["available"] is True and out["harness_requests_complete"] is True
    assert out["request_counts"]["started"] == 0


def test_mismatched_end_or_reopening_cannot_claim_complete_accounting(tmp_path):
    ledger = ModelRequestLedger(tmp_path, "run-1")
    identity = ledger.begin(role="actor", model="test-model")
    ledger.finish(identity, response=response())
    ledger.close()
    path = ledger.directory / (identity + ".end.json")
    changed = json.loads(path.read_bytes())
    changed["run_id"] = "other"
    path.write_text(json.dumps(changed))
    out = read_model_request_ledger(tmp_path, "run-1")
    assert out["harness_requests_complete"] is False and not out["model_calls"]
    with pytest.raises(FileExistsError):
        ModelRequestLedger(tmp_path, "run-1")
    assert read_model_request_ledger(tmp_path, "run-1")["harness_requests_complete"] is False


def test_malformed_usage_and_torn_terminal_record_are_reported_not_silently_dropped(tmp_path):
    ledger = ModelRequestLedger(tmp_path, "run-1")
    first = ledger.begin(role="actor", model="test-model")
    second = ledger.begin(role="advisor", model="test-model")
    ledger.finish(first, response=response())
    ledger.finish(second, response=response())
    ledger.close()
    path = ledger.directory / (first + ".end.json")
    malformed = json.loads(path.read_bytes())
    malformed["usage"] = ["unexpected list"]
    path.write_text(json.dumps(malformed))
    (ledger.directory / (second + ".end.json")).write_text('{"schema_version":')
    out = read_model_request_ledger(tmp_path, "run-1")
    assert out["harness_requests_complete"] is False
    assert out["request_counts"]["usage_unknown"] == 2
    assert out["model_calls"] == [] and len(out["usage_errors"]) >= 2
