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
    GateResult,
    ValidatorState,
    _GradedDecision,
    check_phase_claim,
    reason_asserts_deficiency,
    validate_phase_claim,
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
    """Fed ignite's sealed pairing where nothing later re-renders it, no path
    reaches a green close.

    The pairing checked is the one the gate would SEAL: this status executed
    nothing, so no counts branch replaces the validator's sentence and the
    contradiction is the decision. The gate degrades to UNAVAILABLE — a
    harness-visible refusal to grade — and never launders it into `success`.
    Fail closed, not fail quiet.
    """
    status = _ignite_status()
    status.update(
        {
            "reason": IGNITE_SEALED_REASON,
            "total_tests": 0,
            "passed_tests": 0,
            "failed_tests": 0,
            "unique_tests": 0,
            "unique_passed_tests": 0,
            "unique_failed_tests": 0,
            "static_test_count": 0,
            "test_stats": {
                "discovered": 0,
                "executed": 0,
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
            },
        }
    )

    gate = _gate(status, PhaseOutcome.PARTIAL)

    assert gate.validator_state is ValidatorState.UNAVAILABLE
    assert gate.validated_outcome is PhaseOutcome.UNKNOWN
    # The sentence survives as the DIAGNOSIS of the refusal, never as a grade.
    assert "deficiency" in gate.reason


def test_a_draft_reason_the_next_branch_discards_never_degrades_the_seal():
    """The refusal grades the decision that is SEALED, not an intermediate one.

    The `else` arm drafts a decision from the validator's raw sentence, and the
    receipt-bound execution branch immediately replaces it with its own render.
    Raising on the draft degraded the whole gate to `validator_unavailable` /
    HARNESS_RECOVERY_REQUIRED over a sentence no reader would ever have seen —
    a fail-closed that punished the run for prose the harness itself discarded.
    """
    status = _ignite_status()
    status["reason"] = IGNITE_SEALED_REASON

    gate = _gate(status, PhaseOutcome.PARTIAL)

    assert gate.validator_state is ValidatorState.GREEN
    assert gate.code == "test_execution_observed"
    assert gate.reason == "executed 37 of 37 discovered · 29 passed, 8 failed, 0 skipped"
    assert IGNITE_SEALED_REASON not in gate.reason


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
    assert reason_asserts_deficiency("no test reports found — the runner receipt is missing")
    assert not reason_asserts_deficiency("executed 37 of 37 discovered · 29 passed, 8 failed")


def test_the_lexicon_reads_a_predicate_and_not_a_noun():
    """`missing` names a thing as often as it grades one.

    bigtop's green build sealed *"The repair produced the missing local
    artifact"* — a repaired absence, stated by the branch that fixed it. A bare
    substring refused that sentence, which would have failed a run closed on
    honest evidence. The class is the ASSERTION that evidence is short, so the
    markers are anchored to the predicate form it takes.
    """
    assert not reason_asserts_deficiency("The repair produced the missing local artifact")
    assert reason_asserts_deficiency("the terminal runner receipt is missing")
    assert reason_asserts_deficiency("two expected artifacts are still missing")


# --------------------------------------------------------------------------- #
# §2.3 — the direction fence is a property of the RESULT, not of one producer
# --------------------------------------------------------------------------- #
def _green_gate(reason: str, *, code: str = "build_green") -> GateResult:
    return GateResult(
        accepted=True,
        validated_outcome=PhaseOutcome.SUCCESS,
        claim_disposition=ClaimDisposition.CONFIRMED,
        validator_state=ValidatorState.GREEN,
        reason=reason,
        code=code,
    )


def test_no_producer_can_pair_a_green_word_with_a_deficiency_sentence():
    """`_GradedDecision` is local to `_inspect_test`; the ignite pairing has to
    be unconstructible for the four engine closes and every other producer too."""
    with pytest.raises(ValueError, match="deficiency"):
        _green_gate(IGNITE_SEALED_REASON, code="test_execution_observed")


def test_the_hand_written_close_funnel_refuses_the_ignite_pairing():
    """The engine closes reach `validate_phase_claim` with hand-written prose."""
    with pytest.raises(ValueError, match="deficiency"):
        validate_phase_claim(
            PhaseClaim(phase="test", claimed_outcome=PhaseOutcome.PARTIAL),
            ValidatorState.GREEN,
            reason=IGNITE_SEALED_REASON,
            code="test_execution_observed",
        )


def test_a_capped_gate_may_still_state_the_deficiency_it_capped_on():
    """The refusal is about DIRECTION, not about vocabulary: a non-green word
    stating a shortfall is exactly what an honest gate does."""
    gate = validate_phase_claim(
        PhaseClaim(phase="test", claimed_outcome=PhaseOutcome.PARTIAL),
        ValidatorState.PARTIAL,
        reason="two expected artifacts are still missing",
        code="build_partial",
    )

    assert gate.validated_outcome is PhaseOutcome.PARTIAL
    assert gate.reason == "two expected artifacts are still missing"


def test_a_green_build_reason_keeps_its_inventory_clause():
    """`_inspect_build` appends a coverage checklist to a GREEN reason. Naming
    what has no output yet is an inventory of work, not a grade of the evidence
    — the line exists precisely so an accepted phase still says what remains."""
    checklist = _green_gate(
        "artifacts present under the project root · Module coverage: 3/5 built "
        "[core, api, tools] · no output yet: [examples, docs]"
    )
    islands = _green_gate(
        "artifacts present under the project root · Recommended islands: 1/2 built "
        "· remaining: gradle 'build' in /workspace/demo/native"
    )

    assert "no output yet" in checklist.reason
    assert "remaining:" in islands.reason


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


def test_the_phase_objectives_teach_no_threshold_the_harness_does_not_have():
    """The most model-visible surviving pass-rate site (§2 acceptance).

    Both TEST objectives read *"Partial pass above threshold is a valid outcome
    when reported honestly"* for four commits after the threshold left the
    chain: the model planned and claimed against a policy nothing in the
    harness enforces. The literal-token grep below walked past it because
    "pass threshold" never appears — so the model-facing surfaces ban the bare
    word.
    """
    from sag.agent.react_engine import (
        KICKOFF_PHASE_OBJECTIVES,
        PHASE_OBJECTIVES,
        PYTHON_PHASE_OBJECTIVES,
    )

    for name, objectives in (
        ("PHASE_OBJECTIVES", PHASE_OBJECTIVES),
        ("PYTHON_PHASE_OBJECTIVES", PYTHON_PHASE_OBJECTIVES),
        ("KICKOFF_PHASE_OBJECTIVES", KICKOFF_PHASE_OBJECTIVES),
    ):
        for phase, text in objectives.items():
            assert "threshold" not in text.lower(), f"{name}[{phase!r}]"
            assert "%" not in text, f"{name}[{phase!r}]"

    for objectives in (PHASE_OBJECTIVES, PYTHON_PHASE_OBJECTIVES):
        objective = objectives["test"]
        # What replaced it: execution honesty, and red as a fact to report.
        assert "discovered" in objective
        assert "repair" in objective


def test_the_operator_log_states_a_band_and_not_an_invented_cut_off():
    """§2's vocabulary retirement reaches the attention lines too: the INFO row
    still cut modules at an invented 80%, the one number v4 replaced with the
    shared band table."""
    from sag.tools.report_tool import ReportTool

    snapshot = {
        "phases": {"build": True, "test": True},
        "status": {"tests_total": 120, "pass_pct": 95.0},
        "test_history": {"ignored_lines": 0},
        "flags": {},
        "per_module": {
            "core": {"total": 100, "passed": 99, "pass_pct": 99.0},
            "api": {"total": 20, "passed": 15, "pass_pct": 75.0},
            "tools": {"total": 20, "passed": 4, "pass_pct": 20.0},
            "docs": {"total": 0, "passed": 0, "pass_pct": None},
        },
    }

    items = ReportTool._evaluate_attention_flags(ReportTool.__new__(ReportTool), snapshot)
    messages = [item["message"] for item in items]

    assert not [message for message in messages if "80%" in message]
    banded = [message for message in messages if "tools" in message]
    assert banded, messages
    assert "few" in banded[0]
    # A module that ran nothing has no fraction to band; it is not a low score.
    assert "docs" not in banded[0]
    assert "core" not in banded[0]
    # The boundary is the band table's, not an invented one: 15/20 is `most`.
    assert "api" not in banded[0]


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
