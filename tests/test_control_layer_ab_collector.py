import json
from pathlib import Path

import pytest

from sag.agent.control_events import action_envelope_sha256
from scripts.collect_control_layer_ab import (
    CASSANDRA_CLASS_RANGE,
    CASSANDRA_RAW_EXECUTION_RANGE,
    CASSANDRA_UNIQUE_TEST_RANGE,
    ABCollector,
    CampaignStore,
    CollectionError,
    _validate_current_run_pin,
    build_legacy_run_pin,
    build_parser,
)

PIN = {
    "target_repo_sha": "a" * 40,
    "container_image_digest": "sha256:" + "b" * 64,
    "sag_git_sha": "c" * 40,
    "thinking_model": "thinking-model",
    "action_model": "action-model",
    "sanitized_config": {"max_iterations": 80},
    "prompt_bundle_sha256": "d" * 64,
    "feature_flags": {"control_scheduler": True},
    "random_seed_or_null": 17,
    # A REAL run-order index (the runner now assigns it per run, sequential
    # across the campaign plan) rather than the historical None placeholder.
    "run_order_index": 7,
    "dependency_cache_state": "warm",
    "host_arch": "arm64",
}


def test_panel_records_cassandra_canonical_and_raw_bases():
    panel_path = Path(__file__).parents[1] / "scripts" / "control_layer_panel.json"
    acceptance = json.loads(panel_path.read_text(encoding="utf-8"))["probes"][
        "cassandra-java-driver"
    ]["acceptance"]

    assert (
        acceptance["compiled_classes_min"],
        acceptance["compiled_classes_max"],
    ) == CASSANDRA_CLASS_RANGE
    assert (
        acceptance["unique_tests_min"],
        acceptance["unique_tests_max"],
    ) == CASSANDRA_UNIQUE_TEST_RANGE
    assert (
        acceptance["raw_executions_min"],
        acceptance["raw_executions_max"],
    ) == CASSANDRA_RAW_EXECUTION_RANGE


def _session(tmp_path: Path, *, excluding: str | None = None) -> Path:
    session = tmp_path / "session_one"
    setup = session / ".setup_agent"
    setup.mkdir(parents=True)
    pin = {key: value for key, value in PIN.items() if key != excluding}
    (setup / "run-pin.json").write_text(json.dumps(pin), encoding="utf-8")
    (setup / "verdict.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "run_id": "run-1",
                "finalized_at": "2026-07-17T12:00:00Z",
                "verdict": "success",
                "build_evidence": {
                    "observed": True,
                    "green": True,
                    "outcome": "success",
                    "evidence_status": "verified",
                    "refs": [],
                },
                "test_stats": {
                    "discovered": 541,
                    "unique": {
                        "executed": 541,
                        "passed": 541,
                        "failed": 0,
                        "errors": 0,
                        "skipped": 0,
                    },
                    "raw": {
                        "executed": 544,
                        "passed": 541,
                        "failed": 3,
                        "errors": 0,
                        "skipped": 0,
                    },
                    "flaky_count": 3,
                    "judgment": "success",
                },
                "conflicts": [],
                "phase_records": [],
                "input_refs": [],
            }
        ),
        encoding="utf-8",
    )
    (setup / "control_events.jsonl").write_text(
        '{"sequence":1,"kind":"evidence_close","payload":{"reason":"test_terminated"}}\n',
        encoding="utf-8",
    )
    (session / "token_usage.csv").write_text(
        "model_type,total_tokens\nthinking,100\naction,50\nthinking,80\n",
        encoding="utf-8",
    )
    return session


def test_collector_ignores_numbers_in_markdown(tmp_path):
    session = _session(tmp_path)
    (session / "setup-report.md").write_text("999999 tests passed", encoding="utf-8")

    record = ABCollector().collect(session)

    assert record.metrics.unique_passed == 541
    assert record.metrics.flaky_count == 3
    assert record.metrics.thought_calls == 2


def test_v3_collector_reads_compiled_classes_from_the_snapshot(tmp_path):
    session = _session(tmp_path)
    verdict_path = session / ".setup_agent" / "verdict.json"
    payload = json.loads(verdict_path.read_text(encoding="utf-8"))
    payload["build_evidence"]["compiled_classes"] = 8916
    verdict_path.write_text(json.dumps(payload), encoding="utf-8")

    record = ABCollector().collect(session)

    assert record.metrics.compiled_classes == 8916


def test_collector_rejects_incomplete_pin(tmp_path):
    session = _session(tmp_path, excluding="container_image_digest")

    with pytest.raises(CollectionError, match="container_image_digest"):
        ABCollector().collect(session)


def test_collector_uses_legacy_structured_adapter_without_markdown(tmp_path):
    session = tmp_path / "legacy"
    contexts = session / ".setup_agent" / "contexts"
    contexts.mkdir(parents=True)
    (session / ".setup_agent" / "run-pin.json").write_text(json.dumps(PIN), encoding="utf-8")
    (contexts / "phase_report.json").write_text(
        json.dumps(
            {
                "raw_data": {
                    "report_snapshot": {
                        "verdict": "partial",
                        "test_stats": {
                            "executed": 328,
                            "passed": 0,
                            "failed": 328,
                            "skipped": 0,
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (session / "setup-report.md").write_text("777777 passed", encoding="utf-8")

    record = ABCollector().collect(session)

    assert record.source_schema == "legacy_structured"
    assert record.metrics.unique_total == 328
    assert record.metrics.unique_passed == 0


def test_legacy_adapter_reads_the_recorded_report_action_when_snapshot_is_absent(tmp_path):
    session = tmp_path / "legacy-action"
    contexts = session / ".setup_agent" / "contexts"
    contexts.mkdir(parents=True)
    (session / ".setup_agent" / "run-pin.json").write_text(json.dumps(PIN), encoding="utf-8")
    (contexts / "phase_report.json").write_text(
        json.dumps(
            {
                "history": [
                    {
                        "type": "action",
                        "tool_name": "report",
                        "success": True,
                        "parameters": {
                            "status": "success",
                            "test_stats": {
                                "discovered": 559,
                                "executed": 559,
                                "passed": 541,
                                "failed": 0,
                                "skipped": 18,
                            },
                        },
                        "output": "Tests: 999999 / 999999 passed",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    record = ABCollector().collect(session)

    assert record.source_schema == "legacy_structured"
    assert record.metrics.verdict == "success"
    assert record.metrics.unique_total == 559
    assert record.metrics.unique_passed == 541
    assert record.metrics.unique_skipped == 18


def test_legacy_adapter_prefers_structured_physical_report_metrics(tmp_path):
    session = tmp_path / "legacy-physical"
    setup = session / ".setup_agent"
    contexts = setup / "contexts"
    contexts.mkdir(parents=True)
    (setup / "run-pin.json").write_text(json.dumps(PIN), encoding="utf-8")
    (contexts / "phase_report.json").write_text(
        json.dumps(
            {
                "history": [
                    {
                        "type": "action",
                        "tool_name": "report",
                        "success": True,
                        "parameters": {
                            "status": "success",
                            "test_stats": {
                                "executed": 0,
                                "passed": 0,
                                "failed": 0,
                                "skipped": 0,
                            },
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (setup / "report_metrics.json").write_text(
        json.dumps(
            {
                "build": {"class_count": 8916},
                "test": {
                    "total": 4928,
                    "passed": 4598,
                    "failed": 0,
                    "errors": 156,
                    "skipped": 174,
                    "unique_total": 2896,
                    "unique_passed": 2745,
                    "unique_failed": 0,
                    "unique_errors": 89,
                    "unique_skipped": 62,
                    "conflicts": ["test_errors_detected"],
                },
            }
        ),
        encoding="utf-8",
    )

    record = ABCollector().collect(session)

    assert record.metrics.compiled_classes == 8916
    assert record.metrics.unique_total == 2896
    assert record.metrics.unique_passed == 2745
    assert record.metrics.unique_errors == 89
    assert record.metrics.raw_executions == 4928
    assert record.metrics.conflicts == ("test_errors_detected",)
    # Path relativized at generation time (item 5): the record leaks no absolute
    # host worktree path, but still names the artifact by its tail.
    assert any(
        entry.endswith("report_metrics.json") for entry in record.structured_inputs
    )
    for entry in record.structured_inputs:
        assert not entry.startswith("/"), entry


def _valid_session_at(base: Path, session_name: str = "session_20260719_x") -> Path:
    """A complete session (valid pin + verdict + events + token csv) under base."""
    session = base / "logs" / session_name
    setup = session / ".setup_agent"
    setup.mkdir(parents=True)
    (setup / "run-pin.json").write_text(json.dumps(PIN), encoding="utf-8")
    (setup / "verdict.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "run_id": "run-1",
                "finalized_at": "2026-07-19T12:00:00Z",
                "verdict": "success",
                "build_evidence": {
                    "observed": True,
                    "green": True,
                    "outcome": "success",
                    "evidence_status": "verified",
                    "refs": [],
                    "compiled_classes": 10,
                },
                "test_stats": {
                    "discovered": 5,
                    "unique": {"executed": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                    "raw": {"executed": 5, "passed": 5, "failed": 0, "errors": 0, "skipped": 0},
                    "flaky_count": 0,
                    "judgment": "success",
                },
                "conflicts": [],
                "phase_records": [],
                "input_refs": [],
            }
        ),
        encoding="utf-8",
    )
    (setup / "control_events.jsonl").write_text(
        '{"sequence":1,"kind":"evidence_close","payload":{"reason":"test_terminated"}}\n',
        encoding="utf-8",
    )
    (session / "token_usage.csv").write_text(
        "model_type,total_tokens\nthinking,100\naction,50\n", encoding="utf-8"
    )
    return session


def test_collector_relativizes_all_paths_no_host_leak(tmp_path):
    # Item 5: a session materialized OUTSIDE the repo (a throwaway panel
    # worktree) must yield a record whose session_path and every
    # structured_inputs entry are relative — no absolute host path leaks into
    # the committed, hand-verified evidence. The <worktree> placeholder anchors
    # at the logs/ segment so the artifact is still identifiable.
    session = _valid_session_at(tmp_path / "wt-abc123")

    record = ABCollector().collect(session)

    host_prefix = str(tmp_path)
    assert not record.session_path.startswith("/")
    assert host_prefix not in record.session_path
    assert record.session_path.startswith("<worktree>/logs/session_")
    assert len(record.structured_inputs) >= 3  # pin + events + token + verdict
    for entry in record.structured_inputs:
        assert not entry.startswith("/"), entry
        assert host_prefix not in entry, entry
        assert entry.startswith("<worktree>/logs/session_"), entry


def test_collector_relativizes_in_repo_session_to_repo_relative(tmp_path, monkeypatch):
    # A run that lives UNDER this checkout relativizes to a repo-root-relative
    # path (no <worktree> placeholder, no leading slash).
    from scripts import collect_control_layer_ab as mod

    repo_root = tmp_path / "checkout"
    session = _valid_session_at(repo_root, session_name="session_in_repo")
    monkeypatch.setattr(mod, "_REPO_ROOT_FOR_RELATIVIZE", repo_root.resolve())

    record = ABCollector().collect(session)
    assert record.session_path == "logs/session_in_repo"
    assert not record.session_path.startswith("<worktree>")
    for entry in record.structured_inputs:
        assert entry.startswith("logs/session_in_repo"), entry


def test_collector_counts_the_live_token_csv_type_column(tmp_path):
    session = _session(tmp_path)
    (session / "token_usage.csv").write_text(
        "iteration,type,tool_name,model,total_tokens\n"
        "1,action,project,action-model,100\n"
        "2,thought,Think,thinking-model,200\n"
        "3,action,build,action-model,100\n",
        encoding="utf-8",
    )

    record = ABCollector().collect(session)

    assert record.metrics.thought_calls == 1
    assert record.metrics.action_calls == 2


def test_collector_flags_an_action_envelope_without_a_tool_result(tmp_path):
    session = _session(tmp_path)
    params = {"action": "compile"}
    envelope = {
        "sequence": 1,
        "kind": "action_envelope",
        "payload": {
            "envelope_id": "envelope-1",
            "plan_index": 0,
            "tool": "build",
            "exact_params": params,
            "envelope_sha256": action_envelope_sha256(
                plan_index=0,
                tool="build",
                exact_params=params,
            ),
        },
    }
    (session / ".setup_agent" / "control_events.jsonl").write_text(
        json.dumps(envelope) + "\n",
        encoding="utf-8",
    )

    record = ABCollector().collect(session)

    assert record.metrics.envelope_count == 1
    assert record.metrics.tool_result_count == 0
    assert record.metrics.unmatched_envelope_count == 1


def test_legacy_external_run_pin_is_complete_and_sanitized():
    pin = build_legacy_run_pin(
        target_repo_sha="a" * 40,
        container_image_digest="sha256:" + "b" * 64,
        sag_git_sha="c" * 40,
        runtime_config={
            "thinking_model": "thinking-model",
            "action_model": "action-model",
            "max_iterations": 50,
            "openai_api_key": "must-not-survive",
            "openai_base_url": "https://user:password@example.invalid/v1",
        },
        prompt_bundle_sha256="d" * 64,
        random_seed=17,
        dependency_cache_state="warm",
        host_arch="arm64",
    )

    assert pin.thinking_model == "thinking-model"
    assert pin.action_model == "action-model"
    assert pin.feature_flags == {
        "control_events": False,
        "reasoning_scheduler": False,
        "phase_machine": False,
    }
    assert pin.sanitized_config == {"max_iterations": 50}


def test_probe_runner_accepts_an_explicit_shared_env_file():
    args = build_parser().parse_args(
        [
            "run-probe",
            "--campaign",
            "campaign",
            "--probe",
            "paramiko",
            "--stage",
            "baseline",
            "--repeat",
            "1",
            "--sag-root",
            "baseline-worktree",
            "--dependency-cache",
            "warm",
            "--seed",
            "17",
            "--env-file",
            "/private/config/setup-agent.env",
        ]
    )

    assert args.env_file == "/private/config/setup-agent.env"


def test_summary_reports_median_and_range(tmp_path):
    campaign = CampaignStore(tmp_path / "campaign")
    campaign.add_metric_runs("paramiko", "ws4", "thought_calls", [8, 6, 7])

    metric = campaign.summarize().metric("paramiko", "thought_calls")

    assert metric.median == 7
    assert metric.minimum == 6
    assert metric.maximum == 8
    assert metric.render() == "7 [6-8]"


def test_campaign_rejects_duplicate_run_ids_and_pin_drift(tmp_path):
    session = _session(tmp_path)
    record = ABCollector().collect(session)
    campaign = CampaignStore(tmp_path / "campaign")
    campaign.append("paramiko", "ws7", record)

    with pytest.raises(CollectionError, match="duplicate run id"):
        campaign.append("paramiko", "ws7", record)

    changed = record.model_copy(
        update={"run_id": "run-2", "pin": record.pin.model_copy(update={"random_seed_or_null": 99})}
    )
    with pytest.raises(CollectionError, match="pin mismatch"):
        campaign.append("paramiko", "ws7", changed)


def test_campaign_append_allows_two_runs_differing_only_in_run_order_index(tmp_path):
    """Repeat runs under one probe/stage carry different run_order_index values
    (their position in the campaign's total order). That difference alone MUST
    NOT be rejected as pin drift — every other pin field still matches (P1)."""
    session = _session(tmp_path)
    record = ABCollector().collect(session)
    assert record.pin.run_order_index == PIN["run_order_index"]
    campaign = CampaignStore(tmp_path / "campaign")

    first = campaign.append("paramiko", "ws7", record)
    # Same arm, second run: identical pin EXCEPT a later run_order_index.
    second_record = record.model_copy(
        update={
            "run_id": "run-2",
            "pin": record.pin.model_copy(
                update={"run_order_index": record.pin.run_order_index + 3}
            ),
        }
    )
    campaign.append("paramiko", "ws7", second_record)

    payload = json.loads(Path(first).read_text(encoding="utf-8"))
    assert [r["run_id"] for r in payload["runs"]] == ["run-1", "run-2"]
    # The differing index stays recorded verbatim on each pin (not normalized).
    indices = [r["pin"]["run_order_index"] for r in payload["runs"]]
    assert indices == [PIN["run_order_index"], PIN["run_order_index"] + 3]


def test_campaign_append_rejects_non_index_drift_even_with_matching_index(tmp_path):
    """Excluding run_order_index from the pin comparison must not weaken any
    other check: a run whose index matches but whose sag_git_sha (or any other
    field) drifted is still rejected as a pin mismatch (P1)."""
    session = _session(tmp_path)
    record = ABCollector().collect(session)
    campaign = CampaignStore(tmp_path / "campaign")
    campaign.append("paramiko", "ws7", record)

    drifted = record.model_copy(
        update={
            "run_id": "run-2",
            # run_order_index intentionally UNCHANGED; a different pin field drifts.
            "pin": record.pin.model_copy(update={"sag_git_sha": "e" * 40}),
        }
    )
    with pytest.raises(CollectionError, match="pin mismatch"):
        campaign.append("paramiko", "ws7", drifted)


def _populate_valid_six_bar_campaign(tmp_path, *, current_stage="ws7"):
    record = ABCollector().collect(_session(tmp_path))
    campaign = CampaignStore(tmp_path / "campaign")
    for probe in ("tvm", "bigtop", "paramiko", "cassandra-java-driver"):
        for repeat in range(1, 4):
            baseline_metrics = record.metrics.model_copy(
                update={
                    "verdict": "success",
                    "thought_calls": 10 if probe == "paramiko" else 4,
                    "action_calls": 6,
                    "envelope_count": 6,
                    "compiled_classes": 8914 if probe == "cassandra-java-driver" else None,
                    "unique_total": 4590 if probe == "cassandra-java-driver" else 541,
                    "raw_executions": 4810 if probe == "cassandra-java-driver" else 544,
                }
            )
            campaign.append(
                probe,
                "baseline",
                record.model_copy(
                    update={
                        "run_id": f"{probe}-baseline-{repeat}",
                        "metrics": baseline_metrics,
                    }
                ),
            )
            current_metrics = baseline_metrics.model_copy(
                update={"thought_calls": 4 if probe == "paramiko" else 4}
            )
            campaign.append(
                probe,
                current_stage,
                record.model_copy(
                    update={
                        "run_id": f"{probe}-{current_stage}-{repeat}",
                        "metrics": current_metrics,
                        "surface_ok": True,
                    }
                ),
            )
    return campaign


def test_campaign_evaluates_all_six_bars(tmp_path):
    campaign = _populate_valid_six_bar_campaign(tmp_path)

    summary = campaign.evaluate("ws7", min_repeats=3)

    assert summary.failures == ()
    assert summary.metric("paramiko", "thought_calls", stage="ws7").render() == "4 [4-4]"


def test_campaign_enforces_numbered_bars_for_suffixed_stage_names(tmp_path):
    stage = "ws7-final3"
    campaign = _populate_valid_six_bar_campaign(tmp_path, current_stage=stage)
    path = campaign.path / f"tvm-{stage}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["runs"][0]["surface_ok"] = False
    payload["runs"][0]["metrics"]["evidence_contract_violations"] = 1
    path.write_text(json.dumps(payload), encoding="utf-8")

    summary = campaign.evaluate(stage, min_repeats=3)

    assert any("bar1 surface agreement failed" in failure for failure in summary.failures)
    assert any("bar4 missing post-step evidence" in failure for failure in summary.failures)


def test_campaign_gate_rejects_cassandra_metric_drift(tmp_path):
    campaign = _populate_valid_six_bar_campaign(tmp_path)
    path = campaign.path / "cassandra-java-driver-ws7.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["runs"][0]["metrics"]["compiled_classes"] = 8913
    path.write_text(json.dumps(payload), encoding="utf-8")

    summary = campaign.evaluate("ws7", min_repeats=3)

    assert any("8,914..8,916 compiled classes" in failure for failure in summary.failures)


def test_campaign_gate_rejects_cassandra_unique_or_raw_volume_drift(tmp_path):
    campaign = _populate_valid_six_bar_campaign(tmp_path)
    path = campaign.path / "cassandra-java-driver-ws7.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["runs"][0]["metrics"]["unique_total"] = 4900
    payload["runs"][1]["metrics"]["raw_executions"] = 4799
    path.write_text(json.dumps(payload), encoding="utf-8")

    summary = campaign.evaluate("ws7", min_repeats=3)

    assert any("unique tests are outside 4,500..4,700" in item for item in summary.failures)
    assert any("raw executions are outside 4,800..5,100" in item for item in summary.failures)


def test_campaign_does_not_equate_rejected_actor_calls_with_tool_envelopes(tmp_path):
    campaign = _populate_valid_six_bar_campaign(tmp_path)
    path = campaign.path / "paramiko-ws7.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["runs"][0]["metrics"]["action_calls"] = 99
    path.write_text(json.dumps(payload), encoding="utf-8")

    summary = campaign.evaluate("ws7", min_repeats=3)

    assert not any("action/envelope count drift" in failure for failure in summary.failures)


def test_failed_phase_gate_limit_is_not_the_whole_run_gate_total(tmp_path):
    campaign = _populate_valid_six_bar_campaign(tmp_path)
    path = campaign.path / "tvm-ws7.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["runs"][0]["metrics"].update(
        {
            "verdict": "failed",
            "gate_interactions": 5,
            "max_failure_gate_interactions": 2,
        }
    )
    path.write_text(json.dumps(payload), encoding="utf-8")

    summary = campaign.evaluate("ws7", min_repeats=3)

    assert not any("determined failure closed too late" in failure for failure in summary.failures)


def test_campaign_rejects_a_missed_second_failure_recurrence(tmp_path):
    campaign = _populate_valid_six_bar_campaign(tmp_path)
    path = campaign.path / "tvm-ws7.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["runs"][0]["metrics"]["second_occurrence_miss_count"] = 1
    path.write_text(json.dumps(payload), encoding="utf-8")

    summary = campaign.evaluate("ws7", min_repeats=3)

    assert any("second identical failure was not intercepted" in item for item in summary.failures)


def test_current_run_pin_requires_exact_host_container_mirrors(tmp_path):
    session = tmp_path / "session"
    setup = session / ".setup_agent"
    setup.mkdir(parents=True)
    canonical = json.dumps(PIN, sort_keys=True, separators=(",", ":"))
    (session / "run-pin.json").write_text(canonical, encoding="utf-8")
    (setup / "run-pin.json").write_text(canonical, encoding="utf-8")

    pin = _validate_current_run_pin(
        session,
        target_repo_sha=PIN["target_repo_sha"],
        sag_git_sha=PIN["sag_git_sha"],
        random_seed=PIN["random_seed_or_null"],
        dependency_cache_state=PIN["dependency_cache_state"],
        host_arch=PIN["host_arch"],
    )

    assert pin.model_dump(mode="json") == PIN
    assert pin.run_order_index == 7
    (setup / "run-pin.json").write_text(canonical + "\n", encoding="utf-8")
    assert (
        _validate_current_run_pin(
            session,
            target_repo_sha=PIN["target_repo_sha"],
            sag_git_sha=PIN["sag_git_sha"],
            random_seed=PIN["random_seed_or_null"],
            dependency_cache_state=PIN["dependency_cache_state"],
            host_arch=PIN["host_arch"],
        ).model_dump(mode="json")
        == PIN
    )
    drifted = {**PIN, "random_seed_or_null": 99}
    (setup / "run-pin.json").write_text(
        json.dumps(drifted, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    with pytest.raises(CollectionError, match="host/container run-pin mirrors differ"):
        _validate_current_run_pin(
            session,
            target_repo_sha=PIN["target_repo_sha"],
            sag_git_sha=PIN["sag_git_sha"],
            random_seed=PIN["random_seed_or_null"],
            dependency_cache_state=PIN["dependency_cache_state"],
            host_arch=PIN["host_arch"],
        )
