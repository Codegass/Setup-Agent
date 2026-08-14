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


def _excluded_snapshot(*, auxiliary, stale, stale_reports):
    snapshot = _snapshot(receipt_scoped=True)
    snapshot["test_stats"] = {
        **snapshot["test_stats"],
        "auxiliary_test_stats": auxiliary,
        "stale_test_reports": stale_reports,
        "stale_test_stats": stale,
    }
    return snapshot


_UNPARSEABLE_ONLY = {
    "executed": 0,
    "passed": 0,
    "failed": 0,
    "errors": 0,
    "skipped": 0,
    "unparseable": 1,
}
_MEASURED = {"executed": 6, "passed": 6, "failed": 0, "errors": 0, "skipped": 0}


def test_a_volume_the_parser_could_not_read_is_unmeasured_not_a_measured_zero():
    """The excluded buckets stated ``executed: 0`` for reports the parser could
    not open at all — a measured outcome for bytes nobody measured. Zero is a
    count; an unreadable report has none."""
    metrics = _assemble(
        _excluded_snapshot(
            auxiliary=_UNPARSEABLE_ONLY,
            stale=_UNPARSEABLE_ONLY,
            stale_reports=["/workspace/p/target/surefire-reports/TEST-Broken.xml"],
        )
    )

    for bucket in ("quarantined_observations", "stale_observations"):
        observed = metrics["tests"][bucket]
        assert observed["availability"] == "unavailable", bucket
        assert observed["executed"] is None, bucket
        assert "could not be parsed" in observed["reason"], bucket
    # The files themselves stay counted: the run saw them, it just cannot say
    # what ran in them.
    assert metrics["tests"]["stale_observations"]["report_file_count"] == 1


def test_a_volume_the_parser_did_read_is_still_stated():
    """The other direction: a measured excluded volume keeps stating itself,
    unparseable neighbours or not."""
    metrics = _assemble(
        _excluded_snapshot(
            auxiliary={**_MEASURED, "unparseable": 1},
            stale=_MEASURED,
            stale_reports=["/workspace/p/target/surefire-reports/TEST-Stale.xml"],
        )
    )

    assert metrics["tests"]["quarantined_observations"]["executed"] == 6
    assert metrics["tests"]["stale_observations"]["executed"] == 6
    assert metrics["tests"]["stale_observations"]["reason_counts"] == {
        "receipt_claim_superseded": 6
    }


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
