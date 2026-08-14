"""The run-verdict kernel (spec §6): one ordering, one combiner.

Everything that announces an outcome — CLI banner, exit code, report Result
header, webui state — must derive it from here. The combined verdict is the
MINIMUM across independent judges (phase machine, physical validation), with
evidence conflicts capping at partial: honest uncertainty can never be
announced as a clean success."""

from typing import Iterable, Optional

from sag.verdict_rates import EXCLUDED_VOLUME_CONFLICTS

VERDICT_ORDER = ["failed", "partial", "success"]
_RANK = {v: i for i, v in enumerate(VERDICT_ORDER)}

# Conflicts that are already ADJUDICATED elsewhere, so capping on them would
# either double-count a fact or move the verdict the wrong way.
#
#   * test_failures_detected / test_errors_detected merely RESTATE counted test
#     failures. The project's red is an exact sealed fact and the physical
#     verdict has already read it as execution (spec 2026-08-14 §2), so feeding
#     it back into the conflict cap would demote every fully-executed run with
#     any failing test to partial.
#   * EXCLUDED_VOLUME_CONFLICTS names the volume the headline left out, by both
#     doors: test_executions_unattributed_to_receipts (claimed by nobody) and
#     test_reports_stale (claimed, then rewritten). Capping on EITHER inverted
#     P4, from opposite sides. On the REPORT side: bigtop's headline 50 + 4
#     unclaimed reports capped at partial and DELETING those four files lifted
#     the cap. On the RECEIPT side: one corpus, one rewritten report, identical
#     headline 25 — with the receipt that revealed the rewrite the report was
#     STALE and capped, and DELETING that receipt made it auxiliary and sealed
#     success. Both are "removing evidence improved the verdict" (2026-07-29
#     evidence-lifecycle spec, P4).
#     The headline band derives from attributed counts alone, so excluded
#     volume has no second claim on the verdict to make; and the cap defended
#     nothing it was reached for — a run that wants the volume uncapped need
#     only dispatch through a non-receipting tool from the start, so the cap
#     taxed the run that used the receipting tool first. Anti-fabrication is
#     EXCLUSION, and exclusion is untouched: neither volume is ever counted.
#     Both stay named, pathed, counted and spoken in the cases grain
#     (spec 2026-08-14 amendment items 4 and 7).
#
# The cap stays reserved for genuine uncertainty about the evidence itself —
# bytes the harness could not READ (e.g. test_report_parse_error), never bytes
# it read, measured and deliberately left out of the numerator.
ADJUDICATED_CONFLICTS = frozenset(
    {"test_failures_detected", "test_errors_detected", *EXCLUDED_VOLUME_CONFLICTS}
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
