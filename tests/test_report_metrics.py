from sag.tools.report_metrics import assemble_report_metrics


def _snapshot(*, receipt_scoped=False):
    return {
        "verdict": "partial",
        "build_evidence": {
            "observed": True,
            "judgment": "success",
            "domain_states": {"root": {"state": "success"}},
        },
        "test_stats": {
            "unique": {
                "executed": 9497,
                "passed": 9480,
                "failed": 5,
                "errors": 0,
                "skipped": 12,
            },
            "raw": {
                "executed": 18839,
                "passed": 18805,
                "failed": 5,
                "errors": 0,
                "skipped": 29,
            },
            "receipt_scoped": receipt_scoped,
            "judgment": "failed",
        },
    }


def _assemble(snapshot, **kwargs):
    return assemble_report_metrics(
        snapshot=snapshot,
        build_evidence={},
        test_analysis={},
        conflicts=[],
        evidence_refs=[],
        generated_at="2026-08-08T12:00:00Z",
        **kwargs,
    )


def test_unscoped_old_aggregate_is_observation_only():
    metrics = _assemble(_snapshot())

    assert metrics["schema_version"] == 2
    assert metrics["tests"]["claimed"]["latest_subjects"]["executed"] is None
    assert metrics["tests"]["claimed"]["latest_cases"]["executed"] is None
    assert metrics["tests"]["claimed"]["receipt_executions"]["executed"] is None
    assert metrics["tests"]["unattributed_observations"]["executed"] == 18839
    assert metrics["tests"]["unattributed_observations"]["failed"] == 5


def test_receipt_scoped_rows_enter_only_receipt_execution_grain():
    metrics = _assemble(_snapshot(receipt_scoped=True))

    receipt = metrics["tests"]["claimed"]["receipt_executions"]
    assert receipt["executed"] == 18839
    assert receipt["passed"] == 18805
    assert metrics["tests"]["unattributed_observations"]["executed"] == 0


def test_absent_measurements_are_null_not_zero():
    metrics = _assemble({})

    assert metrics["tests"]["claimed"]["latest_subjects"]["executed"] is None
    assert metrics["tests"]["unattributed_observations"]["executed"] is None
    assert metrics["coverage"]["domains_discovered"] is None
    assert metrics["run"]["pin_status"] == "incomplete"
    assert metrics["evidence"] == {
        "integrity": "unavailable",
        "receipts_expected": None,
        "receipts_persisted": None,
        "terminal_receipts_unpersisted": None,
        "conflict_count": 0,
    }


def test_execution_metrics_never_reappear_as_unversioned_flat_aliases():
    metrics = _assemble(
        _snapshot(receipt_scoped=True),
        execution_metrics={
            "model": "claude-sonnet-4.5",
            "total_iterations": 6,
            "max_iterations": 40,
        },
    )

    assert "model" not in metrics
    assert "total_iterations" not in metrics
    assert "max_iterations" not in metrics
    assert "test" not in metrics
    assert "build" not in metrics


def test_run_pin_carries_the_reproducibility_surface():
    metrics = _assemble(
        _snapshot(receipt_scoped=True),
        run_pin={
            "run_id": "report-metrics-run",
            "target_repo_sha": "a" * 40,
            "sag_git_sha": "b" * 40,
            "prompt_bundle_sha256": "c" * 64,
            "container_image_digest": "sha256:" + "d" * 64,
            "thinking_model": "think",
            "action_model": "act",
            "sanitized_config": {"max_iterations": 40},
            "feature_flags": {"native_loop": True},
            "random_seed_or_null": None,
            "dependency_cache_state": "cold",
            "host_arch": "arm64",
            "run_order_index": 3,
        },
    )

    assert metrics["run"]["target_sha"] == "a" * 40
    assert metrics["run"]["model_pin"] == "thinking=think;action=act"
    assert metrics["run"]["run_order_index"] == 3
    assert metrics["run"]["pin_status"] == "complete"


def test_control_bundle_hash_covers_cache_seed_and_host_pins():
    base = {
        "run_id": "report-metrics-run",
        "target_repo_sha": "a" * 40,
        "sag_git_sha": "b" * 40,
        "prompt_bundle_sha256": "c" * 64,
        "container_image_digest": "sha256:" + "d" * 64,
        "thinking_model": "think",
        "action_model": "act",
        "sanitized_config": {"max_iterations": 40},
        "feature_flags": {"native_loop": True},
        "random_seed_or_null": 3,
        "dependency_cache_state": "cold",
        "host_arch": "arm64",
        "run_order_index": 3,
    }

    first = _assemble(_snapshot(), run_pin=base)["run"]["control_bundle_hash"]
    second = _assemble(
        _snapshot(),
        run_pin={**base, "dependency_cache_state": "warm"},
    )[
        "run"
    ]["control_bundle_hash"]

    assert first != second
