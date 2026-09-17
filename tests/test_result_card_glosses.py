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


#: Every conflict id the archive actually contains, with how many distinct runs
#: recorded it, measured over the 681 `verdict.json` under `logs/` on
#: 2026-09-17. The fence above enumerates the codes three modules export as
#: named constants; these are the ones that reach a reader in practice, and 43
#: of the 53 distinct ids — 303 of 779 occurrences — printed as bare machine
#: words on the terminal, in the report and in the browser.
#:
#: An id carrying a handle after a colon is listed by its family: the handle
#: identifies one job or one attempt and is for a bug report, not for a reader.
OBSERVED_CONFLICTS: dict[str, int] = {
    "maven_reactor_unverified": 73,
    "metrics_conflict": 41,
    "test_primary_coordinate_unresolved": 41,
    "build_validation_failed": 36,
    "acceptance_task_incomplete": 30,
    "build_requirements_unavailable": 18,
    "jdk_mismatch": 11,
    "build_receipt_module_scope_unavailable": 10,
    "maven_success_vs_test_failures": 5,
    "acceptance_task_unavailable": 4,
    "job_barrier_integrity_failure": 2,
    "build_receipt_scope_unavailable": 1,
    "job_terminal_unpersisted": 20,
    "job_live_at_close": 7,
    "forced_test_attempt_nonreceipt": 4,
}


def test_every_conflict_the_archive_records_reads_as_a_sentence():
    """Measured, not enumerated from constants — that is what let these through.

    `test_inventory_covers_every_producing_module` above covers the three
    modules that export their codes as named constants, and passes. These ids
    are raised inline, so nothing enumerated them and nobody counted how often
    they occur. They are 39% of every conflict occurrence in the archive.
    """

    unglossed = {code for code in OBSERVED_CONFLICTS if gloss(code) == code}
    assert not unglossed, (
        "conflict ids a reader meets as bare machine words: "
        + ", ".join(f"{code} ({OBSERVED_CONFLICTS[code]} runs)" for code in sorted(unglossed))
    )


def test_a_conflict_that_carries_a_handle_is_read_by_its_family():
    """`job_live_at_close:58db946542a8` is one fact plus one handle.

    The handle names which job, which is what a bug report quotes and what no
    reader of the Notes list can use. Without this, 31 of the archive's
    occurrences printed a hex id on a line of English sentences.
    """

    assert gloss("job_live_at_close:58db946542a8") == gloss("job_live_at_close")
    assert gloss("job_terminal_unpersisted:083f87b9600d") == gloss("job_terminal_unpersisted")
    assert gloss(
        "forced_test_attempt_nonreceipt:test-1:/workspace/ignite:maven:"
        "candidate_mismatch:FORCED_TEST_CANDIDATE_MISMATCH"
    ) == gloss("forced_test_attempt_nonreceipt")
    # A family nobody glossed still shows the whole id, handle and all: there
    # is nothing else to show, and half an unknown code is worse than all of it.
    assert gloss("never_seen_family:abc123") == "never_seen_family:abc123"


def test_the_handle_families_are_the_ones_the_archive_carries():
    """A guard on the guard: the three families above are really suffixed.

    If one of them stopped carrying a handle this test fails rather than the
    family test passing for the wrong reason.
    """

    for family in ("job_live_at_close", "job_terminal_unpersisted", "forced_test_attempt_nonreceipt"):
        assert gloss(family) != family, f"{family} itself must be glossed"
        assert gloss(f"{family}:handle") == gloss(family)
