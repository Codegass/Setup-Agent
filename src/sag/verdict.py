"""The run-verdict kernel (spec §6): one ordering, one combiner.

Everything that announces an outcome — CLI banner, exit code, report Result
header, webui state — must derive it from here. The combined verdict is the
MINIMUM across independent judges (phase machine, physical validation), with
evidence conflicts capping at partial: honest uncertainty can never be
announced as a clean success."""

from typing import Iterable, Optional

from sag.case_census import CENSUS_CONFLICT
from sag.verdict_rates import UNCOUNTED_REPORT_CONFLICTS

VERDICT_ORDER = ["failed", "partial", "success"]
_RANK = {v: i for i, v in enumerate(VERDICT_ORDER)}

# The conflicts whose adjudication IS the headline counts: they say nothing the
# counts beside them do not already say. A consumer holding NO counts holds no
# adjudication either, and must state the uncertainty itself
# (report_tool._derive_evidence_status_from_test_stats; the same rule
# verdict_finalizer applies when counts leave a rollup).
COUNT_DERIVED_CONFLICTS = frozenset({"test_failures_detected", "test_errors_detected"})

# Conflicts that are already ADJUDICATED elsewhere, so capping on them would
# either double-count a fact or move the verdict the wrong way.
#
#   * COUNT_DERIVED_CONFLICTS merely RESTATE counted test failures. The
#     project's red is an exact sealed fact and the physical verdict has
#     already read it as execution (spec 2026-08-14 §2), so feeding it back
#     into the conflict cap would demote every fully-executed run with any
#     failing test to partial.
#   * UNCOUNTED_REPORT_CONFLICTS names every report fact the headline did not
#     count, by all three doors: test_executions_unattributed_to_receipts
#     (claimed by nobody), test_reports_stale (claimed, then rewritten) and
#     test_report_parse_error (could not be read at all). Capping on ANY of
#     them inverted P4, and each inversion was demonstrated on one corpus with
#     an identical headline:
#       - REPORT axis: bigtop's headline 50 + 4 unclaimed reports capped at
#         partial and DELETING those four files lifted the cap; likewise `rm`
#         on one corrupt report — claimed or unclaimed — lifted partial to
#         success while every counted execution stayed put.
#       - RECEIPT axis: headline 25, one rewritten report — with the receipt
#         that revealed the rewrite it was STALE and capped, without it the same
#         bytes were auxiliary and sealed success; the same for a corrupt
#         report, capping only while a receipt claimed it.
#     All of them are "removing evidence improved the verdict" (2026-07-29
#     evidence-lifecycle spec, P4).
#     The operative rule for all three is ATTRIBUTION, not readability: the
#     headline band derives from attributed counts alone, so a report the
#     headline never counted has no second claim on the verdict to make —
#     whether it was never claimed, claimed and rewritten, or claimed and
#     unopenable. The cap defended nothing it was reached for either: a run that
#     wants such a report uncapped need only dispatch through a non-receipting
#     tool from the start, so the cap taxed the run that used the receipting
#     tool first. Anti-fabrication is EXCLUSION, and exclusion is untouched:
#     none of these reports is ever counted. All three stay named, pathed,
#     counted where a count exists, and spoken in the cases grain (spec
#     2026-08-14 amendment items 4, 7 and 12).
#   * test_census_sources_disagree names a DISCOVERY-side bookkeeping split —
#     polaris's analyzer wrote 1,347 beside a module list explaining 593 — and
#     it is fully adjudicated where it is raised: the module sum wins, the
#     basis says what that number covers, and both numbers are sealed. It
#     states nothing about what the run EXECUTED, and no dispatch the run could
#     make would clear it, so capping on it would tax a static property of the
#     repository with no remedy available. The census is a floor; saying so is
#     observability, and observability never ends a run (#39 §2.1).
#
# The cap stays where deletion cannot buy it: an evidence-CLOSURE failure whose
# removal takes the headline's authority with it (test_receipt_unreadable —
# without the ledger nothing is attributed and the headline is 0), never a
# report file a run can delete without changing a single execution it ran.
ADJUDICATED_CONFLICTS = frozenset(
    {
        *COUNT_DERIVED_CONFLICTS,
        CENSUS_CONFLICT,
        *UNCOUNTED_REPORT_CONFLICTS,
    }
)


def combine_verdicts(*verdicts: Optional[str]) -> str:
    """Minimum of the known verdicts; non-verdicts (None/unknown) abstain."""
    known = [v for v in verdicts if v in _RANK]
    if not known:
        return "success"
    return min(known, key=_RANK.__getitem__)


def run_verdict(machine_outcome: Optional[str], physical_verdict: Optional[str],
                conflicts: Iterable[str]) -> str:
    base = combine_verdicts(machine_outcome, physical_verdict)
    if any(c not in ADJUDICATED_CONFLICTS for c in conflicts):
        return combine_verdicts(base, "partial")
    return base


def rescue_blocked_build(outcome: Optional[str], build_evidence_ok: bool) -> Optional[str]:
    """The blocked-build evidence-rescue (live 2026-06-24 pyyaml false-red;
    bug #11 2026-07-10 pyyaml-7 / libcloud-2 banner-vs-final split).

    ``outcome == "failed"`` here means the agent BLOCKED the critical build
    phase (or restated that belief through the report call's evidence status).
    When physical build evidence disagrees — validate_build_status found a
    real build (success=True: Java artifacts/fingerprints, or the Python
    ladder's success/partial-with-imports) — evidence outranks agent belief
    and the cap is PARTIAL, never FAILED and never promoted to success. With
    no physical build evidence the outcome passes through untouched, so an
    evidence-absent block stays FAILED on every surface.

    This is ONE function consumed by BOTH the agent finalization
    (SetupAgent._get_verified_final_status) and the report snapshot kernel
    (ReportTool._snapshot_kernel_verdict), so the report banner, the stored
    snapshot verdict, and the CLI final can never split on it.
    """
    if outcome == "failed" and build_evidence_ok:
        return "partial"
    return outcome
