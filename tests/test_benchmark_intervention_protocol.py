"""Zero is a prospective protocol observation, not a historical default."""
from pathlib import Path

import pytest

from sag.benchmark.intervention_protocol import (
    CHANNELS, close, copy_evidence, initialize, now, record_event, summary, validate_protocol,
)
from sag.benchmark.requirements import canonical_digest, load_json


def policy():
    return {"version": "sag-unattended-v1", "scope": "task_affecting_human_actions",
            "channels": dict(CHANNELS), "window": "process_start_to_termination",
            "initial_task_excluded": True, "manual_cancellation": "counted",
            "operator_commitment": "report_any_task_affecting_deviation"}


def begin(tmp_path):
    directory = tmp_path / "ledger"
    initialize(directory, policy=policy(), run_key="p", runner_file=Path(__file__))
    result = {"run_key": "p", "run_id": "run-1", "process_id": 42,
              "process_started_at": now(), "process_finished_at": now(),
              "intervention_protocol_sha256": canonical_digest(policy())}
    return directory, result


def test_closed_prospective_protocol_can_report_sourced_zero(tmp_path):
    directory, result = begin(tmp_path)
    close(directory, result, process_started=True, process_finished=True)
    measured = summary(directory, result)
    assert measured["source"] == "trusted_noninteractive_runner"
    assert measured["coverage"] == "complete"
    assert measured["human_interventions"] == 0
    assert measured["input_policy"]["external_channels"] == "prohibited_or_logged"
    assert measured["external_host_monitoring"] == "not_technically_monitored"
    copied = copy_evidence(directory, tmp_path / "export", base=tmp_path, result=result)
    assert copied["protocol_records"]["start"]["path"] == "export/start.json"
    assert (tmp_path / copied["protocol_records"]["close"]["path"]).is_file()


@pytest.mark.parametrize("kind", ["guidance", "manual_edit", "environment_change", "manual_restore"])
def test_reported_external_deviation_is_positive_not_a_protocol_zero(tmp_path, kind):
    directory, result = begin(tmp_path)
    record_event(directory, kind=kind, detail="Operator repaired the task environment")
    close(directory, result, process_started=True, process_finished=True)
    assert summary(directory, result)["human_interventions"] == 1


@pytest.mark.parametrize("change,expected", [({"outer_timeout": True}, 0), ({"cancelled": True}, 1)])
def test_manual_stop_counts_but_predeclared_automatic_timeout_does_not(tmp_path, change, expected):
    directory, result = begin(tmp_path)
    result.update(change)
    close(directory, result, process_started=True, process_finished=True)
    close(directory, result, process_started=True, process_finished=True)
    assert summary(directory, result)["human_interventions"] == expected


def test_unknown_deviation_or_incomplete_interval_stays_unknown(tmp_path):
    directory, result = begin(tmp_path)
    record_event(directory, kind="unknown", detail="Cannot determine whether operator supplied help")
    close(directory, result, process_started=True, process_finished=True)
    assert summary(directory, result)["human_interventions"] is None
    other = tmp_path / "other"
    initialize(other, policy=policy(), run_key="p", runner_file=Path(__file__))
    close(other, result, process_started=True, process_finished=False)
    assert summary(other, result)["human_interventions"] is None


def test_reported_cancellation_and_its_stop_signal_are_not_counted_twice(tmp_path):
    directory, result = begin(tmp_path)
    record_event(directory, kind="manual_cancel", detail="Operator requested cancellation")
    result["cancelled"] = True
    close(directory, result, process_started=True, process_finished=True)
    assert summary(directory, result)["human_interventions"] == 1


def test_historical_interval_cannot_be_retroactively_registered(tmp_path):
    directory, result = begin(tmp_path)
    result.update(process_started_at="2026-01-01T00:00:00Z", process_finished_at="2026-01-01T01:00:00Z")
    close(directory, result, process_started=True, process_finished=True)
    assert summary(directory, result)["coverage"] == "unavailable"
    assert summary(directory, result)["human_interventions"] is None


def test_late_mutation_and_foreign_policy_cannot_reuse_closed_zero(tmp_path):
    directory, result = begin(tmp_path)
    close(directory, result, process_started=True, process_finished=True)
    with pytest.raises(ValueError, match="closed"):
        record_event(directory, kind="manual_edit", detail="Late report")
    with pytest.raises(ValueError, match="identity"):
        summary(directory, result | {"intervention_protocol_sha256": "0" * 64})
    (directory / "event-external.json").write_text("{}")
    with pytest.raises(ValueError, match="event set changed"):
        summary(directory, result)


@pytest.mark.parametrize("change", [{"version": "legacy"}, {"channels": {"stdin": "disabled"}},
                                    {"operator_commitment": ""}, {"initial_task_excluded": False},
                                    {"manual_cancellation": "ignored"}])
def test_incomplete_protocol_cannot_be_opted_in(change):
    with pytest.raises(ValueError):
        validate_protocol(policy() | change)


def prospective_analysis(tmp_path, kind=None):
    from test_benchmark_sag_adapter import prepare
    from sag.benchmark.campaign_telemetry import finalize_campaign_analysis
    from sag.benchmark.recorder import write_json
    from sag.benchmark.requirements import bound_file, file_digest

    run, _spec, protocol, _retained, _closed, base, export = prepare(tmp_path)
    export()
    attempt = tmp_path / "attempt"
    directory = attempt / "intervention-ledger"
    manifest = {"intervention_protocol": policy(), "runner_sha256": file_digest(__file__),
                "projects": [{"run_key": "demo", "repo": run.task.repo, "sha": run.task.sha,
                              "evaluation_protocol": protocol}]}
    initialize(directory, policy=policy(), run_key="demo", runner_file=Path(__file__), manifest=manifest)
    result = {"authority_ok": True, "run_key": "demo", "run_id": run.state.run_id,
              "process_id": 123, "evaluation_protocol": protocol,
              "process_started_at": now(), "process_seconds": 0.1,
              "intervention_protocol_sha256": canonical_digest(policy())}
    if kind:
        record_event(directory, kind=kind, detail="Operator adjusted the running task")
    result["process_finished_at"] = now()
    close(directory, result, process_started=True, process_finished=True)
    interventions = attempt / "intervention-telemetry.json"
    write_json(interventions, summary(directory, result))
    linked = finalize_campaign_analysis(base, result, runner_file=__file__, intervention_file=interventions)
    telemetry = load_json(bound_file(base, linked["telemetry"]))
    exported_run = load_json(bound_file(base, linked["run"]))
    return base, linked, telemetry, exported_run


@pytest.mark.parametrize("kind,count", [(None, 0), ("manual_edit", 1), ("unknown", None)])
def test_common_evaluator_consumes_bound_prospective_interventions(tmp_path, kind, count):
    from sag.benchmark.intervention_protocol import verified_intervention_count
    from sag.benchmark.requirements import bound_file

    base, linked, telemetry, run = prospective_analysis(tmp_path, kind)
    assert verified_intervention_count(base, telemetry, run) == count
    score = load_json(bound_file(base, linked["result"]))
    assert score["human_interventions"] == count
    assert score["unattended_seconds"] == 0.1


@pytest.mark.parametrize("problem", ["manifest", "run_id", "run_pin", "policy", "start", "closed", "event", "claimed_zero"])
def test_self_asserted_or_mismatched_zero_is_never_accepted(tmp_path, problem):
    from sag.benchmark.intervention_protocol import verified_intervention_count
    from sag.benchmark.requirements import bound_file

    base, _linked, telemetry, run = prospective_analysis(tmp_path, "manual_edit")
    if problem == "manifest":
        bound_file(base, telemetry["protocol_records"]["manifest"]).write_text("{}")
    elif problem == "run_id":
        run["run_id"] = "other"
    elif problem == "run_pin":
        run["run_pin"]["sha256"] = "f" * 64
    elif problem == "policy":
        telemetry["intervention_protocol"]["operator_commitment"] = "none"
    elif problem == "start":
        telemetry["protocol_records"]["start"]["sha256"] = "f" * 64
    elif problem == "closed":
        telemetry["coverage"] = "unavailable"
    elif problem == "event":
        telemetry["protocol_records"]["events"] = []
    else:
        telemetry["human_interventions"] = 0
    assert verified_intervention_count(base, telemetry, run) is None
