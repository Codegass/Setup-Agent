"""Fix B fences — the 80% thresholds leave the gate/claim chain (spec §2).

The disease these pin is one sentence pointing away from its own verdict:
ignite sealed *"Tests below the 80% pass threshold: 29/37 (78.4%)"* next to
`validator_state: "green"` and an upgraded `success` claim. The cure is not a
better sentence; it is that the sentence, the code and the outcome come out of
ONE decision, so the contradiction has nowhere to be assembled.
"""

import ast
import io
import tokenize
from pathlib import Path

import pytest

from sag.agent.phase_gates import (
    ClaimDisposition,
    ValidatorState,
    _GradedDecision,
    check_phase_claim,
    reason_asserts_deficiency,
)
from sag.agent.phase_machine import PhaseClaim, PhaseOutcome
from sag.config.settings import Config
from sag.verdict_rates import execution_sentence

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "sag"


class _TestEvidenceValidator:
    """A physical validator that returns exactly the status dict it was given."""

    def __init__(self, status):
        self._status = status

    def validate_build_status(self, project_name=None):
        return {
            "success": True,
            "build_complete": True,
            "evidence_status": "success",
            "evidence": {"build_system": "maven", "has_artifacts": True, "class_count": 12},
            "reason": "artifacts present under the project root",
        }

    def validate_test_status(self, project_name=None):
        return dict(self._status)


IGNITE_SEALED_REASON = "Tests below the 80% pass threshold: 29/37 (78.4%)"


def _ignite_status():
    """ignite S12's evidence (spec §2): 29/37 passing, receipt-bound.

    The reason is what the validator states TODAY — one decision covering the
    label, the evidence word and the sentence. What ignite actually sealed is
    `IGNITE_SEALED_REASON`, and the fence below proves that pairing has no path.
    """
    return {
        "has_test_reports": True,
        "evidence_status": "success",
        "reason": "executed 37 of 37 discovered · 29 passed, 8 failed, 0 skipped",
        "receipt_scoped": True,
        "total_tests": 37,
        "passed_tests": 29,
        "failed_tests": 8,
        "error_tests": 0,
        "skipped_tests": 0,
        "unique_tests": 37,
        "unique_passed_tests": 29,
        "unique_failed_tests": 8,
        "static_test_count": 37,
        "test_stats": {
            "discovered": 37,
            "executed": 37,
            "passed": 29,
            "failed": 8,
            "errors": 0,
            "skipped": 0,
        },
    }


def _kafka_status():
    """kafka S8 (spec §2): 3568/3571 over a 20,497-test discovery."""
    return {
        "has_test_reports": True,
        "evidence_status": "success",
        "reason": "Tests passed above the 80% threshold: 3568/3571 (99.9%)",
        "receipt_scoped": True,
        "total_tests": 3571,
        "passed_tests": 3568,
        "failed_tests": 2,
        "error_tests": 0,
        "skipped_tests": 1,
        "unique_tests": 3571,
        "unique_passed_tests": 3568,
        "unique_failed_tests": 2,
        "unique_skipped_tests": 1,
        "static_test_count": 20497,
        "test_stats": {
            "discovered": 20497,
            "executed": 3571,
            "passed": 3568,
            "failed": 2,
            "errors": 0,
            "skipped": 1,
        },
    }


def _gate(status, claimed):
    return check_phase_claim(
        "test",
        PhaseClaim(phase="test", claimed_outcome=claimed),
        _TestEvidenceValidator(status),
        None,
        "demo",
    )


# --------------------------------------------------------------------------- #
# §2.3 — the direction fence
# --------------------------------------------------------------------------- #
def test_ignite_shape_cannot_be_produced_by_the_test_gate():
    """The gate that upgrades to green renders the sentence that says why."""
    gate = _gate(_ignite_status(), PhaseOutcome.PARTIAL)

    assert gate.validator_state is ValidatorState.GREEN
    assert gate.validated_outcome is PhaseOutcome.SUCCESS
    assert gate.code == "test_execution_observed"
    assert not reason_asserts_deficiency(gate.reason)
    assert "80%" not in gate.reason
    assert "pass threshold" not in gate.reason
    assert gate.reason.startswith("executed 37 of 37 discovered")


def test_a_green_decision_refuses_a_deficiency_sentence():
    """The contradiction is unconstructible, not merely unrendered."""
    with pytest.raises(ValueError, match="deficiency"):
        _GradedDecision(
            state=ValidatorState.GREEN,
            code="test_execution_observed",
            reason="Tests below the 80% pass threshold: 29/37 (78.4%)",
        )


def test_a_non_green_decision_refuses_the_execution_sentence():
    """And vice versa: the affirming sentence belongs to the green branch."""
    with pytest.raises(ValueError, match="execution"):
        _GradedDecision(
            state=ValidatorState.RED,
            code="tests_not_executed",
            reason=execution_sentence(executed=37, discovered=37, passed=29, failed=8),
        )


def test_a_contradictory_validator_word_fails_closed_instead_of_going_green():
    """Even fed ignite's sealed pairing, no path reaches a green close.

    The gate degrades to UNAVAILABLE — a harness-visible refusal to grade — and
    never launders the contradiction into `success`. Fail closed, not fail quiet.
    """
    status = _ignite_status()
    status["reason"] = IGNITE_SEALED_REASON

    gate = _gate(status, PhaseOutcome.PARTIAL)

    assert gate.validator_state is ValidatorState.UNAVAILABLE
    assert gate.validated_outcome is PhaseOutcome.UNKNOWN
    # The sentence survives as the DIAGNOSIS of the refusal, never as a grade.
    assert "deficiency" in gate.reason


def test_a_zero_execution_gate_keeps_the_cause_the_validator_named():
    """Rendering one's own sentence must not generalize away a stated cause."""
    status = _ignite_status()
    status.update(
        {
            "reason": "Test collection failed for 28 files — 0 tests executed: ImportError",
            "total_tests": 0,
            "passed_tests": 0,
            "failed_tests": 0,
            "unique_tests": 0,
            "unique_passed_tests": 0,
            "unique_failed_tests": 0,
            "evidence_status": "blocked",
            "test_stats": {
                "discovered": 37,
                "executed": 0,
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
            },
        }
    )
    gate = _gate(status, PhaseOutcome.FAILED)

    assert gate.code == "tests_not_executed"
    assert gate.validator_state is ValidatorState.RED
    assert "28 files" in gate.reason
    assert "ImportError" in gate.reason


def test_the_deficiency_vocabulary_is_the_one_the_spec_names():
    assert reason_asserts_deficiency("Tests below the 80% pass threshold")
    assert reason_asserts_deficiency("insufficient test execution evidence")
    assert reason_asserts_deficiency("no test reports found — reports missing")
    assert not reason_asserts_deficiency("executed 37 of 37 discovered · 29 passed, 8 failed")


# --------------------------------------------------------------------------- #
# §2.2 — replacement language is execution-based
# --------------------------------------------------------------------------- #
def test_kafka_shape_is_adjudicated_on_execution_with_no_percentage():
    """A `partial` claim over a 99.9% receipt is graded on what ran, not on %."""
    gate = _gate(_kafka_status(), PhaseOutcome.PARTIAL)

    assert gate.validated_outcome is PhaseOutcome.SUCCESS
    assert gate.claim_disposition is ClaimDisposition.PESSIMISTIC
    assert "%" not in gate.reason
    assert "threshold" not in gate.reason
    assert gate.reason.startswith(
        "executed 3,571 of 20,497 discovered · 3,568 passed, 2 failed, 1 skipped"
    )


def test_execution_sentence_states_counts_and_never_a_rate():
    assert (
        execution_sentence(executed=3571, discovered=20497, passed=3568, failed=2, skipped=1)
        == "executed 3,571 of 20,497 discovered · 3,568 passed, 2 failed, 1 skipped"
    )
    # Errors are their own exact fact, never folded into "failed" silently.
    assert (
        execution_sentence(executed=100, discovered=120, passed=90, failed=5, errors=3, skipped=2)
        == "executed 100 of 120 discovered · 90 passed, 5 failed, 3 errored, 2 skipped"
    )
    # An unknown denominator is stated as unknown, not as zero.
    assert (
        execution_sentence(executed=214, discovered=None, passed=206, failed=3, skipped=5)
        == "executed 214 of an undetermined discovery · 206 passed, 3 failed, 5 skipped"
    )


def test_zero_execution_reads_as_zero_execution_not_as_a_failed_percentage():
    """geode §2: `Tests below the 80% pass threshold: 0/0 (0.0%)` is gone."""
    from sag.agent.physical_validator import PhysicalValidator

    validator = PhysicalValidator(project_path="/workspace")
    validator.parse_test_reports_with_catalog = lambda project_dir: {
        "valid": True,
        "total_tests": 0,
        "passed_tests": 0,
        "failed_tests": 0,
        "error_tests": 0,
        "skipped_tests": 0,
        "discovered": 9754,
        "report_files": [],
        "parsing_errors": [],
        "test_exclusions": [],
        "modules_without_tests": [],
    }
    validator._python_collected_count = lambda project_name: None

    status = validator.validate_test_status("geode")

    assert "pass threshold" not in status["reason"]
    assert "0.0%" not in status["reason"]
    assert status["reason"] == "no tests executed of 9,754 discovered"
    assert status["status"] == "FAILED"
    assert status["evidence_status"] == "blocked"


# --------------------------------------------------------------------------- #
# §2.4 — config compatibility: the plumbing is gone, not merely unread
# --------------------------------------------------------------------------- #
def test_the_threshold_settings_and_env_vars_are_gone(monkeypatch):
    monkeypatch.setenv("SAG_TEST_PASS_THRESHOLD", "0.95")
    monkeypatch.setenv("SAG_TEST_EXECUTION_THRESHOLD", "0.5")
    config = Config.from_env()

    assert not hasattr(config, "test_pass_threshold")
    assert not hasattr(config, "test_execution_threshold")
    assert "test_pass_threshold" not in Config.model_fields
    assert "test_execution_threshold" not in Config.model_fields


def _prose_lines(text: str) -> set:
    """Line numbers that are comment or docstring — where history may be quoted."""
    lines = set()
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT:
            lines.update(range(token.start[0], token.end[0] + 1))
    for node in ast.walk(ast.parse(text)):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return lines


def test_no_pass_rate_threshold_survives_anywhere_in_the_claim_chain():
    """spec §2 acceptance: the vocabulary survives only as quoted history.

    A comment naming what ignite sealed is evidence; an identifier or a live
    string is the policy still standing there.
    """
    banned = (
        "TEST_PASS_THRESHOLD",
        "TEST_EXECUTION_THRESHOLD",
        "test_pass_threshold",
        "test_execution_threshold",
        "pass threshold",
    )
    offenders = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        prose = _prose_lines(text)
        for number, line in enumerate(text.splitlines(), 1):
            if number in prose:
                continue
            for token in banned:
                if token in line:
                    offenders.append(f"{path.relative_to(SRC_ROOT)}:{number}:{token}")
    assert offenders == []
