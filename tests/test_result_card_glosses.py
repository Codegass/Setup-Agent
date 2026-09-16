"""Every reason code a result row can cite has one plain-English sentence."""

from sag.agent.acceptance_task import TASK_REASON_CODES
from sag.agent.ci_comparison import CI_REASON_CODES
from sag.agent.java_success_certificates import (
    BLOCKED_AXIS_REASON_CODES,
    TYPED_DEFEATER_CODES,
)
from sag.metrics import attainment
from sag.result_card.glosses import KNOWN_REASON_CODES, REASON_GLOSS, gloss
from sag.verdict import ADJUDICATED_CONFLICTS, BUILD_SCOPE_CONFLICTS
from sag.verdict_rates import (
    HEAVY_RED_CONFLICT,
    UNBOUNDED_CONFLICT,
    UNCOUNTED_REPORT_CONFLICTS,
)

FORBIDDEN = (
    "sealed",
    "canonical",
    "claimed",
    "quarantined",
    "subject",
    "snapshot",
    "metrics-v2",
    "verdict-bearing",
    "promoting",
)


def _attainment_codes() -> set[str]:
    return {
        attainment.CERTIFICATE_AUTHORITY_UNAVAILABLE,
        attainment.COUNTS_NOT_RECEIPT_BOUND,
        attainment.TARGET_WITHOUT_COUNTS,
        attainment.TARGET_TEST_UNIVERSE_EMPTY,
        attainment.TARGET_BUILD_UNIVERSE_UNAVAILABLE,
        attainment.COMPARISON_SUBJECT_UNAVAILABLE,
        attainment.COMPARISON_SUBJECT_MISMATCH,
        attainment.NEW_RED_BEYOND_TARGET,
        attainment.CLEAN_BY_COUNTS_ONLY,
        attainment.MODULES_BELOW_TARGET,
        attainment.BUILD_AXIS_NOT_SUCCESSFUL,
        attainment.EXECUTION_BELOW_TARGET,
    }


def _certificate_codes() -> set[str]:
    return set(TYPED_DEFEATER_CODES) | set(BLOCKED_AXIS_REASON_CODES.values())


def _conflict_codes() -> set[str]:
    return {
        *ADJUDICATED_CONFLICTS,
        *BUILD_SCOPE_CONFLICTS,
        *UNCOUNTED_REPORT_CONFLICTS,
        HEAVY_RED_CONFLICT,
        UNBOUNDED_CONFLICT,
        # Maintained by hand. These four are raised inline by the verdict
        # kernel -- src/sag/agent/verdict_finalizer.py and
        # src/sag/agent/phase_gates.py -- and are not exported as named
        # constants, so this test cannot enumerate them. A conflict id added
        # beside them there would ship unglossed and render as a bare code.
        "validated_test_stats_invalid",
        "test_execution_interrupted",
        "test_stats_basis_incomparable",
        "build_oracle_divergence",
    }


def test_inventory_covers_every_producing_module():
    expected = (
        _attainment_codes()
        | _certificate_codes()
        | _conflict_codes()
        | set(CI_REASON_CODES)
        | set(TASK_REASON_CODES)
    )
    missing = expected - KNOWN_REASON_CODES
    assert not missing, f"codes produced but not inventoried: {sorted(missing)}"


def test_every_inventoried_code_has_a_gloss():
    missing = {code for code in KNOWN_REASON_CODES if not REASON_GLOSS.get(code, "").strip()}
    assert not missing, f"inventoried codes without a gloss: {sorted(missing)}"


def test_glosses_are_plain_sentences():
    assert len(REASON_GLOSS) > 50
    for code, text in REASON_GLOSS.items():
        assert text == text.strip(), code
        assert not text.endswith("."), f"{code}: glosses are clause-shaped, no trailing period"
        assert len(text) <= 120, f"{code}: {len(text)} chars"
        lowered = text.lower()
        for word in FORBIDDEN:
            assert word not in lowered, f"{code} uses the forbidden word {word!r}"


def test_unknown_code_returns_itself():
    assert gloss("SOME_CODE_WE_HAVE_NEVER_SEEN") == "SOME_CODE_WE_HAVE_NEVER_SEEN"


def test_known_code_returns_its_sentence():
    assert gloss("official_ci_cell_not_matched") == (
        "no CI job on this commit matches the run's JDK and OS"
    )
