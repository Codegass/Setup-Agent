"""Snapshot and sibling-artifact fixtures for result-card tests.

Built as plain dicts so a test can state exactly the fields it is about and
leave the rest at a neutral default. ``snapshot_dict`` returns the v5 payload
``RunVerdictSnapshot.model_validate`` accepts.
"""

from __future__ import annotations

from typing import Any

from verdict_rate_fakes import complete_verdict_rates

#: A snapshot cross-checks that its task completion and CI comparison belong to
#: the same run, so every run_id in a fixture must be this one.
RUN_ID = "20260914_210609_965730_e39856b237f2_9183-7-953da846d195"

CLEAN_TEST_COUNTS = {
    "executed": 994,
    "passed": 933,
    "failed": 0,
    "errors": 0,
    "skipped": 61,
}


def snapshot_dict(**overrides: Any) -> dict[str, Any]:
    """A complete v5 verdict payload; override any top-level key."""

    payload: dict[str, Any] = {
        "schema_version": 5,
        "run_id": RUN_ID,
        "finalized_at": "2026-09-14T21:14:51Z",
        "input_refs": [],
        "verdict": "success",
        "build_evidence": {
            "observed": True,
            "green": True,
            "judgment": "success",
            "source": "physical",
            "outcome": "success",
            "evidence_status": "verified",
            "refs": [],
            "compiled_classes": 119,
            "source_files": 36,
            "reactor_modules_succeeded": 1,
            "reactor_modules_total": 1,
        },
        "test_stats": {
            "discovered": 472,
            "denominator_basis": "complete",
            "unique": dict(CLEAN_TEST_COUNTS),
            "raw": dict(CLEAN_TEST_COUNTS),
            "flaky_count": 0,
            "judgment": "success",
            "collection_errors": 0,
            "receipt_scoped": True,
        },
        "rates": complete_verdict_rates("success"),
        "conflicts": [],
        "phase_records": [],
        "task_completion": {
            "run_id": RUN_ID,
            "task_sha256": "a" * 64,
            "status": "complete",
            "steps": [
                {
                    "id": "smoke-build-test",
                    "command": "mvn clean verify",
                    "status": "complete",
                    "receipt_id": "inv-maven-1-ee86ae186d94-0002",
                    "exit_code": 0,
                    "reason": None,
                }
            ],
            "reasons": [],
        },
        "ci_comparison": {
            "schema_version": 1,
            "status": "no_matched_cell",
            "run_id": RUN_ID,
            "repo": "apache/commons-cli",
            "target_sha": "e17111798da51037659b3594d9c0b3b525040081",
            "target_record_sha256": None,
            "certificate_input_sha256": None,
            "certificate": None,
            "attainment": None,
            "receipt_ids": [],
            "commands": [],
            "acceptance_command": None,
            "test_identity_basis": None,
            "reasons": ["official_ci_cell_not_matched"],
        },
    }
    payload.update(overrides)
    return payload


def phase_record(phase: str, *, outcome: str = "success", **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "phase": phase,
        "attempt_id": f"{phase}-1",
        "termination": "completed",
        "outcome": outcome,
        "transition": "advance",
        "key_results": "",
        "reason": "",
        "evidence": [],
        "evidence_refs": [],
        "claim": None,
        "validated_outcome": outcome,
        "claim_disposition": "confirmed",
        "legacy_claim": False,
        "prerequisite_ref": "",
    }
    record.update(overrides)
    return record


def attainment(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "verdict": "met",
        "cell_id": "Apache Jenkins commons-dbutils Linux JDK 17 #455",
        "cell_grade": "A",
        "valid": True,
        "target_usable": True,
        "clean": True,
        "clean_form": "ids",
        "built": True,
        "alpha": {"numerator": 523, "denominator": 523},
        "alpha_test": {"numerator": 523, "denominator": 523},
        "alpha_build": {"numerator": 1, "denominator": 1},
        "executed_observed": 523,
        "executed_target": 523,
        "red_observed": 0,
        "red_target": 0,
        "modules_matched": 1,
        "modules_target": 1,
        "missing_module_ids": [],
        "build_form": "modules",
        "modules_basis": "log",
        "unmatched_observed_module_ids": [],
        "lifecycle_parity": {
            "status": "equivalent",
            "form": "maven_phases",
            "ci_command": "mvn -B -f pom.xml -V clean test --batch-mode",
            "sag_commands": ["/opt/apache-maven-3.9.16/bin/mvn -B -f pom.xml -V clean test"],
            "ci_reach": "test",
            "sag_reach": "test",
            "missing": [],
            "extra": [],
        },
        "unexpected_red_ids": [],
        "reason_codes": [],
    }
    payload.update(overrides)
    return payload


def module_metrics(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": 1,
        "generated_at": "2026-09-14T21:14:44Z",
        "module_summary": {
            "modules_total": 1,
            "modules_built": 1,
            "modules_failed": 0,
            "modules_skipped": 0,
            "modules_tested": 1,
            "modules_not_tested": 0,
            "modules_test_bearing": 1,
            "modules_with_test_failures": 0,
            "build_systems": ["maven"],
            "single_module": True,
        },
        "modules": [
            {
                "name": "commons-cli",
                "path": ".",
                "build_status": "success",
                "build_source": "reactor",
                "class_count": 119,
                "jar_count": 4,
                "build_warnings": 0,
                "build_error_samples": [],
                "tests_total": 994,
                "tests_passed": 933,
                "tests_failed": 0,
                "tests_errors": 0,
                "tests_skipped": 61,
                "test_source": "runner_xml",
                "has_test_sources": True,
                "test_bearing_evidence": ["source_tree", "runner_xml"],
                "failing_names": [],
                "failing_count": 0,
                "evidence_refs": [],
            }
        ],
    }
    payload.update(overrides)
    return payload


__all__ = [
    "CLEAN_TEST_COUNTS",
    "RUN_ID",
    "attainment",
    "module_metrics",
    "phase_record",
    "snapshot_dict",
]
