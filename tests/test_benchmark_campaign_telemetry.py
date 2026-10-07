"""Runner cost linkage must not turn response-only records into total costs."""
import csv
import io

import pytest
from test_benchmark_sag_adapter import prepare

from sag.benchmark.campaign_telemetry import finalize_campaign_analysis, read_response_usage
from sag.benchmark.recorder import write_json
from sag.benchmark.requirements import bound_file, load_json


def csv_bytes(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=["iteration", "timestamp", "type", "tool_name", "model", "total_tokens", "prompt_tokens", "completion_tokens", "reasoning_tokens", "actual_output_tokens"])
    writer.writeheader()
    for i, values in enumerate(rows):
        row = dict(iteration=1, timestamp=f"2026-09-22T00:00:{i:02d}", type="executor",
                   tool_name="build", model="test-model", total_tokens=130,
                   prompt_tokens=100, completion_tokens=30, reasoning_tokens=20, actual_output_tokens=10)
        row.update(values)
        writer.writerow(row)
    return out.getvalue().encode()


def test_actor_advisor_summary_and_returned_retry_each_count_once():
    data = read_response_usage(csv_bytes([{}, {"type": "advisor"}, {"type": "advisor_compression"},
                                         {"type": "executor_retry"}]))
    assert [x["role"] for x in data["model_calls"]] == ["actor", "advisor", "summary", "retry"]
    assert sum(x["input_tokens"] + x["output_tokens"] for x in data["model_calls"]) == 520
    assert data["model_calls_complete"] is False
    assert data["usage_errors"] == []


def test_distinct_same_iteration_responses_are_not_dropped_as_duplicates():
    data = read_response_usage(csv_bytes([{}, {}]))
    assert len(data["model_calls"]) == 2
    data = read_response_usage(csv_bytes([{}, {"timestamp": "2026-09-22T00:00:00"}]))
    assert len(data["model_calls"]) == 1
    assert "duplicate" in data["usage_errors"][0]["reason"]


@pytest.mark.parametrize("raw", [None, b"", csv_bytes([]), csv_bytes([{"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "actual_output_tokens": 0}])])
def test_absent_or_default_zero_usage_never_becomes_an_observed_zero(raw):
    data = read_response_usage(raw)
    assert data["model_calls"] is None
    assert data["model_calls_complete"] is False


@pytest.mark.parametrize("change", [{"prompt_tokens": -1}, {"total_tokens": 140},
                                    {"reasoning_tokens": 31}, {"actual_output_tokens": 0},
                                    {"type": "unrecognized"}])
def test_invalid_rows_preserve_valid_known_subtotal(change):
    data = read_response_usage(csv_bytes([{}, change]))
    assert len(data["model_calls"]) == 1
    assert len(data["usage_errors"]) == 1


def test_postprocess_links_costs_without_rewriting_sealed_run_or_changing_success(tmp_path):
    run, _spec, protocol, _retained, _closed, base, export = prepare(tmp_path)
    paths, _record, prior = export()
    original = bound_file(base, paths["run"]).read_bytes()
    (base / "token_usage.csv").write_bytes(csv_bytes([{}, {"type": "advisor_compression"}, {"type": "advisor"}]))
    interventions = tmp_path / "interventions.json"
    write_json(interventions, {"run_id": run.state.run_id, "human_interventions": None,
                              "external_intervention_coverage": "unavailable"})
    result = {"authority_ok": True, "run_id": run.state.run_id, "evaluation_protocol": protocol,
              "process_started_at": "2026-09-22T00:00:00Z", "process_finished_at": "2026-09-22T00:03:00Z",
              "process_seconds": 180.25, "outer_timeout": True}
    linked = finalize_campaign_analysis(base, result, runner_file=__file__, intervention_file=interventions)
    assert linked["known_tokens"] == 390
    assert linked["total_tokens"] is None
    assert linked["unattended_seconds"] == 180.25
    assert linked["cost_coverage"] == "unavailable"
    score = load_json(bound_file(base, linked["result"]))
    assert score["status"] == prior["status"]
    assert score["human_interventions"] is None
    assert bound_file(base, paths["run"]).read_bytes() == original
    derived = load_json(bound_file(base, linked["run"]))
    assert derived["source_run"] == paths["run"]
    with pytest.raises(ValueError, match="already been closed"):
        finalize_campaign_analysis(base, result, runner_file=__file__, intervention_file=interventions)


def test_missing_token_file_keeps_known_and_total_unknown_but_retains_time(tmp_path):
    run, _spec, protocol, _retained, _closed, base, export = prepare(tmp_path)
    export()
    interventions = tmp_path / "interventions.json"
    write_json(interventions, {"run_id": run.state.run_id})
    result = {"authority_ok": True, "run_id": run.state.run_id, "evaluation_protocol": protocol,
              "process_started_at": "start", "process_finished_at": "finish", "process_seconds": 10}
    linked = finalize_campaign_analysis(base, result, runner_file=__file__, intervention_file=interventions)
    assert linked["known_tokens"] is linked["total_tokens"] is None
    assert linked["unattended_seconds"] == 10


def test_foreign_run_telemetry_is_rejected(tmp_path):
    run, _spec, protocol, _retained, _closed, base, export = prepare(tmp_path)
    export()
    interventions = tmp_path / "interventions.json"
    write_json(interventions, {"run_id": "other-run"})
    result = {"authority_ok": True, "run_id": run.state.run_id, "evaluation_protocol": protocol}
    with pytest.raises(ValueError, match="another run"):
        finalize_campaign_analysis(base, result, runner_file=__file__, intervention_file=interventions)


@pytest.mark.parametrize("mode,known,requests", [("responses", 39, 4), ("missing_usage", None, 1), ("no_requests", 0, 0)])
def test_durable_request_ledger_is_preferred_without_adding_the_csv_twice(tmp_path, mode, known, requests):
    from sag.agent.model_request_ledger import ModelRequestLedger

    run, _spec, protocol, _retained, _closed, base, export = prepare(tmp_path)
    export()
    ledger = ModelRequestLedger(base, run.state.run_id)
    if mode == "responses":
        failed = ledger.begin(role="actor", model="model", iteration=1)
        ledger.finish(failed, error=TimeoutError())
        for role in ("actor", "advisor", "summary"):
            request = ledger.begin(role=role, model="model", iteration=1)
            ledger.finish(request, response={"usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}})
    elif mode == "missing_usage":
        request = ledger.begin(role="actor", model="model", iteration=1)
        ledger.finish(request, response={})
    ledger.close()
    (base / "token_usage.csv").write_bytes(csv_bytes([{}, {}]))
    interventions = tmp_path / "interventions.json"
    write_json(interventions, {"run_id": run.state.run_id})
    result = {"authority_ok": True, "run_id": run.state.run_id, "evaluation_protocol": protocol,
              "process_started_at": "start", "process_finished_at": "finish", "process_seconds": 10}
    linked = finalize_campaign_analysis(base, result, runner_file=__file__, intervention_file=interventions)
    telemetry = load_json(bound_file(base, linked["telemetry"]))
    assert telemetry["model_calls_source_kind"] == "durable_harness_request_ledger"
    assert telemetry["request_counts"]["started"] == requests
    assert telemetry["harness_requests_complete"] is True
    assert telemetry["model_calls_complete"] is False
    assert linked["known_tokens"] == known
    assert linked["total_tokens"] is None
    assert telemetry["evidence_refs"]
    if mode == "responses":
        assert telemetry["request_counts"]["error"] == 1
        assert telemetry["request_counts"]["usage_unknown"] == 1
