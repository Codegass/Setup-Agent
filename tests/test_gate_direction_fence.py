"""Fix B fences — the 80% thresholds leave the gate/claim chain (spec §2).

The disease these pin is one sentence pointing away from its own verdict:
ignite sealed *"Tests below the 80% pass threshold: 29/37 (78.4%)"* next to
`validator_state: "green"` and an upgraded `success` claim. The cure is not a
better sentence; it is that the sentence, the code and the outcome come out of
ONE decision, so the contradiction has nowhere to be assembled.
"""

import ast
import io
import re
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

# The retired policy, described as a CLASS rather than as the five tokens that
# happened to spell it. `pass threshold` was banned and "test pass rate >= 80%"
# walked past the ban; what both sentences do is state a pass-rate rule, so the
# rule is what is read for: the quantity by name, the word `threshold` standing
# next to test vocabulary, or a TYPED percentage doing the same. A rendered
# `{rate:.1f}%` is a measurement of what happened and stays allowed.
_PASS_RATE_NAMED = re.compile(r"pass[ _-]?rate", re.I)
_TYPED_PERCENTAGE = re.compile(r"\d+(?:\.\d+)?\s*%")
_GRADED_QUANTITY = re.compile(r"\btests?\b|\bpass(?:ed|es|ing)?\b|\brates?\b", re.I)


def states_a_pass_rate_policy(text: str) -> bool:
    """Does this sentence state a pass-rate rule to whoever reads it?"""
    if _PASS_RATE_NAMED.search(text):
        return True
    if "threshold" in text.lower() and _GRADED_QUANTITY.search(text):
        return True
    return bool(_TYPED_PERCENTAGE.search(text) and _GRADED_QUANTITY.search(text))


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
        "test_execution_state": "completed",  # Independent premise of this wording test.
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
        "test_execution_state": "completed",  # Synthetic wording control, not D3R1 replay.
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


def test_the_report_tool_refusals_teach_no_pass_rate_policy():
    """The last place the retired 80% was still stated to the model (§2.1/§2.2).

    Both refusal branches of `report` rendered *"'success': Build passed AND
    test pass rate >= 80%"* / *"'fail': ... pass rate < 80%"* as LIVE f-strings
    handed to `ToolResult.completed_failure(output=...)`, so the model read the
    retired policy as the definition of the status it was being asked for — and
    would answer `fail` at 78% for a rule nothing in the harness grades any
    more. The gate side lost pass-rate adjudication four commits ago; this was
    the CLAIM side of the same sentence.
    """
    from sag.tools.report_tool import ReportTool

    tool = ReportTool(workflow_mode="legacy")

    invalid_parameters = tool.execute(action="generate", status="success", unexpected_param=1)
    missing_status = tool.execute(action="generate", status="")

    for result in (invalid_parameters, missing_status):
        assert states_a_pass_rate_policy(result.output) is False, result.output
        assert "80%" not in result.output

    # What replaced it: what ran to a terminal result, and red as a project fact.
    for result in (invalid_parameters, missing_status):
        assert "terminal" in result.output
        assert "red tests" in result.output


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


_RATE_MARKER_LABELS = ("Execution Rate", "Pass Rate", "Unique Tests Executed")


def _marker_rows(pass_rate: float, exec_rate: float) -> list:
    """The five rate-keyed operator markers for one (pass, execution) pair."""
    from sag.tools.report_tool import ReportTool

    tool = ReportTool.__new__(ReportTool)
    snapshot = {
        "phases": {"clone": True, "build": True, "test": True},
        "physical_evidence": {"class_files": 10, "jar_files": 1},
        "status": {
            "static_test_count": 100,
            "tests_total": 100,
            "tests_unique": 100,
            "tests_passed": int(pass_rate),
            "tests_failed": 100 - int(pass_rate),
            "tests_errors": 0,
            "tests_skipped": 0,
            "pass_pct": pass_rate,
            "execution_rate": exec_rate,
        },
        "metrics_v2": {},
    }
    rendered = [
        *ReportTool._render_summary_dashboard(tool, snapshot),
        *ReportTool._render_detailed_test_analysis(tool, snapshot),
    ]
    return [line for line in rendered if any(label in line for label in _RATE_MARKER_LABELS)]


@pytest.mark.parametrize(
    ("rate", "marker", "forbidden"),
    (
        (100.0, "✅", ("⚠️", "❌")),
        (96.0, "⚠️", ("✅",)),
        (78.0, "⚠️", ("✅",)),
        (49.0, "❌", ("✅",)),
    ),
    ids=("complete", "above_the_old_95", "between_80_and_95", "heavy_red"),
)
def test_the_operator_markers_key_to_the_documented_heavy_red_rule(rate, marker, forbidden):
    """The five report markers still cut at the invented 80 (and an invented 95).

    §2 retired the 80% from the verdict chain, and the operator log's INFO row
    followed it into the shared band table — but the dashboard and the metrics
    tables kept grading `>= 95 / >= 80 / else` in five places. 96% and 78% got
    different icons for a distinction nothing in the harness makes.

    The one documented rule is the heavy-red signal (`verdict_rates.HALF_FLOOR`):
    below half the executed cases green is ❌, anything short of complete is ⚠️,
    and only complete is ✅. No invented cut-off survives.
    """
    rows = _marker_rows(rate, rate)

    assert len(rows) == 5, rows
    for row in rows:
        assert marker in row, row
        for icon in forbidden:
            assert icon not in row, row


def _observation_lines(status: dict) -> list:
    """The Key Observations block for one status projection."""
    from sag.tools.report_tool import ReportTool

    tool = ReportTool.__new__(ReportTool)
    tool.context_manager = None
    tool.physical_validator = None
    snapshot = {
        "status": status,
        "attention": {"raw": []},
        "project": {"type": "Java", "build_system": "maven"},
        "physical_evidence": {"build_system": "maven"},
    }
    rendered = ReportTool._render_issues_recommendations(tool, snapshot)
    return [line for line in rendered if line.startswith("- ") and "**" in line]


@pytest.mark.parametrize(
    ("rate", "marker", "forbidden"),
    (
        (100.0, "✅", ("⚠️", "❌")),
        (96.0, "⚠️", ("✅",)),
        (78.0, "⚠️", ("✅",)),
        (49.0, "❌", ("✅",)),
    ),
    ids=("complete", "above_the_old_95", "between_80_and_95", "heavy_red"),
)
def test_key_observations_grade_by_the_same_one_rule(rate, marker, forbidden):
    """The last three invented cut-offs (task #38 item 3).

    `_render_issues_recommendations` kept selecting operator PROSE at `>= 95`
    ("High Pass Rate"), `< 90` ("Low Execution Rate") and `< 80` ("Incomplete
    Coverage") after the five table markers were retired to `rate_marker` —
    three more boundaries the harness documents nowhere, deciding a sentence
    rather than grading an outcome. One rule now grades all of them, and the
    adjectives that were the boundaries in prose form go with them.
    """
    lines = _observation_lines(
        {
            "pass_pct": rate,
            "execution_rate": rate,
            "modules_expected": 100,
            "modules_seen": int(rate),
        }
    )

    assert len(lines) == 3, lines
    for line in lines:
        assert marker in line, line
        for icon in forbidden:
            assert icon not in line, line
    assert not [line for line in lines if "High " in line or "Low " in line]


def test_a_measured_zero_pass_rate_is_stated_not_dropped():
    """`if pass_rate and …` read a measured 0.0% as "no rate at all" and
    printed nothing, so the one run whose every test failed said least."""
    lines = _observation_lines({"pass_pct": 0.0, "execution_rate": 100.0})

    assert [line for line in lines if "Pass Rate" in line and "❌" in line], lines


def test_an_unmeasured_rate_stays_absent():
    """📊 belongs to a table that must show a row. An observation nobody made
    is not an observation: absence stays absence here."""
    assert _observation_lines({}) == []


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


# --------------------------------------------------------------------------- #
# §2.2 — the ban is a property of the SURFACE, not of five spellings
# --------------------------------------------------------------------------- #
def _literal_fragments(node: ast.AST) -> list:
    """Every TYPED string fragment of an expression, placeholders excluded.

    `f"{rate:.1f}% passed"` measures what happened and is not read; `"80%"` is
    a rule someone wrote down, and it is. Wrappers are followed — a sentence
    handed to the model through `_append_evidence_summary_to_output(...)` or a
    `join` is the same sentence — because the reader cannot see the wrapper.
    """
    if isinstance(node, ast.Constant):
        return [node.value] if isinstance(node.value, str) else []
    if isinstance(node, ast.JoinedStr):
        fragments = []
        for value in node.values:
            fragments.extend(_literal_fragments(value))
        return fragments
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return _literal_fragments(node.left) + _literal_fragments(node.right)
    if isinstance(node, ast.IfExp):
        return _literal_fragments(node.body) + _literal_fragments(node.orelse)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        fragments = []
        for element in node.elts:
            fragments.extend(_literal_fragments(element))
        return fragments
    if isinstance(node, ast.Call):
        fragments = _literal_fragments(node.func)
        for argument in node.args:
            fragments.extend(_literal_fragments(argument))
        for keyword in node.keywords:
            fragments.extend(_literal_fragments(keyword.value))
        return fragments
    return []


def _names_bound_to_text(scope: ast.AST) -> dict:
    """name -> every fragment assigned or appended to it in this scope.

    A tool that accumulates its output in a local and hands the local over says
    exactly what an inline string says; the model cannot tell them apart.
    """
    bound: dict = {}
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets, value = [node.target], node.value
        else:
            continue
        if value is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                bound.setdefault(target.id, []).extend(_literal_fragments(value))
    return bound


def _collect_model_facing(node: ast.AST, bound: dict, found: list) -> None:
    """Append (line, surface, fragment) for every literal handed to the model.

    Two surfaces reach it. A tool `description` — `BaseTool.get_schema` hands
    the tool description and its parameter descriptions over verbatim — and a
    ToolResult's `output`/`error`, both of which
    `tool_orchestration.format_tool_result` renders into the observation.
    `suggestions` is deliberately dropped there, so it is not model-facing and
    is not read here.
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        bound = {**bound, **_names_bound_to_text(node)}
    if isinstance(node, ast.Call):
        function = node.func
        is_result = (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id == "ToolResult"
        )
        for keyword in node.keywords:
            if keyword.arg == "description":
                surface = "description"
            elif keyword.arg in {"output", "error"} and is_result:
                surface = f"ToolResult.{keyword.arg}"
            else:
                continue
            fragments = _literal_fragments(keyword.value)
            if isinstance(keyword.value, ast.Name):
                fragments.extend(bound.get(keyword.value.id, []))
            for fragment in fragments:
                found.append((node.lineno, surface, fragment))
    elif isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "description":
                for fragment in _literal_fragments(value):
                    found.append(
                        (getattr(value, "lineno", node.lineno), "schema description", fragment)
                    )
    for child in ast.iter_child_nodes(node):
        _collect_model_facing(child, bound, found)


def _model_facing_offenders(source: str, label: str) -> list:
    found: list = []
    _collect_model_facing(ast.parse(source), {}, found)
    return [
        f"{label}:{line} [{surface}] {fragment.strip()[:80]!r}"
        for line, surface, fragment in found
        if states_a_pass_rate_policy(fragment)
    ]


def test_the_model_facing_fence_bites_on_every_surface_it_claims():
    """A fence nobody has seen fire is a fence-shaped comment.

    Each shape below is the retired policy standing on one of the surfaces the
    model reads — inline, accumulated in a local, passed through a wrapper, in
    a tool description, in a parameter schema — and each must be caught. The
    measurement that shares the vocabulary must not be: `{rate:.1f}%` states
    what happened rather than what the answer has to be.
    """
    caught = _model_facing_offenders(
        """
def refuse():
    return ToolResult.completed_failure(
        output="'success': Build passed AND test pass rate >= 80%",
        error="pass rate < 80%",
    )


def accumulate():
    output = "status meaning:\\n"
    output += "  'success' requires tests above the 80% threshold\\n"
    return ToolResult.completed_failure(output=output)


def wrap():
    return ToolResult.completed_failure(
        output=self._append_evidence_summary_to_output(
            "answer 'fail' when fewer than 80% of the tests passed"
        )
    )


def describe():
    BaseTool.__init__(self, name="report", description="Answer fail below 80% of tests.")


def schema():
    return {"status": {"description": "'success' when the pass rate clears the threshold"}}
""",
        "synthetic",
    )
    assert len(caught) == 6, caught

    measurements = _model_facing_offenders(
        """
def measure(rate, executed, discovered):
    return ToolResult.completed_success(
        output=f"executed {executed} of {discovered} discovered · {rate:.1f}% passed",
    )
""",
        "synthetic",
    )
    assert measurements == []


def test_no_model_facing_string_teaches_a_pass_rate_policy():
    """§2 acceptance, closed as a CLASS instead of as five spellings.

    The literal-token grep above banned `pass threshold` and walked straight
    past `report`'s live refusal text — *"'success': Build passed AND test pass
    rate >= 80%"* — for the same reason it walked past the phase objectives:
    the sentence spells the policy differently. What the model must never be
    handed is the RULE, on any surface it reads.
    """
    offenders = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        offenders.extend(
            _model_facing_offenders(
                path.read_text(encoding="utf-8"), str(path.relative_to(SRC_ROOT))
            )
        )
    assert offenders == []
