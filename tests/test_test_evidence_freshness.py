"""A validation call owns its test-evidence read; no prior call owns its facts."""

from types import SimpleNamespace

import pytest

from sag.agent.physical_validator import PhysicalValidator


@pytest.fixture
def live_reads(monkeypatch):
    validator = PhysicalValidator(docker_orchestrator=object(), receipt_run_id="run-one")
    state = {"records": [], "executed": 4, "root": "/workspace/demo/one", "reads": 0}

    def read():
        state["reads"] += 1
        return state["records"]

    def parse(_project, *, primary_root, receipt_records):
        return {
            "valid": True,
            "total_tests": state["executed"],
            "passed_tests": state["executed"],
            "test_success": True,
            "report_files": [f"{primary_root or '/workspace/demo'}/TEST-test.xml"],
        }

    monkeypatch.setattr(validator, "_read_live_invocation_receipts", read)
    monkeypatch.setattr(validator, "_parse_test_reports_compact_in_container", parse)
    monkeypatch.setattr(validator, "_check_modules_without_tests", lambda *_: [])
    monkeypatch.setattr(
        "sag.agent.attempt_policy.resolve_survey_test_candidates",
        lambda _: SimpleNamespace(
            primary=SimpleNamespace(root=state["root"]),
            candidates=(SimpleNamespace(root=state["root"]),),
        ),
    )
    return validator, state


def test_unreadable_then_recovered_ledger_does_not_reuse_failure(live_reads):
    validator, state = live_reads
    state["records"] = None
    assert not validator.parse_test_reports("/workspace/demo")["valid"]
    state["records"] = []
    fresh = validator.parse_test_reports("/workspace/demo")
    assert fresh["valid"]
    assert fresh["total_tests"] == 4
    assert not fresh.get("receipt_error")


@pytest.mark.parametrize("change", ["new_receipt", "new_attempt", "new_run", "new_scope"])
def test_changed_evidence_is_read_again_for_the_same_project(live_reads, change):
    validator, state = live_reads
    state["records"] = [{"receipt_id": "inv-one"}]
    first = validator.parse_test_reports("/workspace/demo")
    if change == "new_receipt":
        state["records"].append({"receipt_id": "inv-two"})
    elif change == "new_attempt":
        state["records"] = [{"receipt_id": "inv-next-attempt"}]
    elif change == "new_run":
        validator.receipt_run_id = "run-two"
        state["records"] = [{"receipt_id": "inv-next-run"}]
    else:
        state["root"] = "/workspace/demo/two"
    state["executed"] = 2
    second = validator.parse_test_reports("/workspace/demo")
    assert first["total_tests"] == 4
    assert second["total_tests"] == 2
    assert state["reads"] == 2
    assert second["report_files"] == [f'{state["root"]}/TEST-test.xml']
    assert second["test_modules"] == [state["root"]]


def test_empty_receipt_read_cannot_reuse_prior_coordinate_resolution(live_reads):
    validator, state = live_reads
    state["records"] = [{"receipt_id": "inv-one"}]
    assert validator.parse_test_reports("/workspace/demo")["test_modules"]
    state["records"] = []
    second = validator.parse_test_reports("/workspace/demo")
    assert "test_modules" not in second


def test_consumer_changes_and_metric_enrichment_do_not_leak_to_later_reads(live_reads, monkeypatch):
    validator, _ = live_reads
    monkeypatch.setattr(validator, "_detect_test_exclusions", lambda _: ["old-exclusion"])
    first = validator.parse_test_reports_with_metrics("/workspace/demo")
    first["total_tests"] = 999
    first["report_files"].append("foreign.xml")
    second = validator.parse_test_reports("/workspace/demo")
    assert second["total_tests"] == 4
    assert "test_exclusions" not in second
    assert "foreign.xml" not in second["report_files"]


def test_catalog_enrichment_is_owned_by_one_call(live_reads):
    validator, _ = live_reads
    catalog = SimpleNamespace(count=lambda: 17, to_dict=lambda: {"by_module": {"core": 17}})
    enriched = validator.parse_test_reports_with_catalog("/tmp/demo", catalog)
    assert enriched["catalog_test_count"] == 17
    plain = validator.parse_test_reports("/tmp/demo")
    assert "catalog_test_count" not in plain
    assert "catalog_by_module" not in plain


def test_test_status_rechecks_integrity_before_reusing_executed_counts(live_reads, monkeypatch):
    validator, state = live_reads
    monkeypatch.setattr(validator, "parse_test_reports_with_catalog", validator.parse_test_reports)
    monkeypatch.setattr(validator, "_python_collected_count", lambda _: None)
    assert validator.validate_test_status("demo")["status"] == "SUCCESS"
    state["records"] = None
    result = validator.validate_test_status("demo")
    assert result["status"] == "FAILED"
    assert result["evidence_status"] == "conflict"
    assert result["total_tests"] == 0
    assert "test_receipt_unreadable" in result["conflicts"]
