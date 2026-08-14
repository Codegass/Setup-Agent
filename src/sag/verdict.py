"""The run-verdict kernel (spec §6): one ordering, one combiner.

Everything that announces an outcome — CLI banner, exit code, report Result
header, webui state — must derive it from here. The combined verdict is the
MINIMUM across independent judges (phase machine, physical validation), with
evidence conflicts capping at partial: honest uncertainty can never be
announced as a clean success."""

from typing import Iterable, Optional

from sag.verdict_rates import UNATTRIBUTED_CONFLICT

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
#   * test_executions_unattributed_to_receipts names reports the receipts do not
#     claim. Capping on it inverted P4 from the REPORT side: bigtop's headline
#     50 + 4 unclaimed reports capped at partial, and DELETING those four files
#     lifted the cap — evidence presence worsened the verdict. The headline band
#     already derives from attributed counts alone, and stray unclaimed XML may
#     legitimately pre-date the checkout, so non-capping is the only treatment
#     monotone on both the receipt and the report axis. The conflict and its
#     disclosure sentence are unchanged: visibility without authority, now also
#     without a cap (spec 2026-08-14 amendment item 4).
#
# The cap stays reserved for genuine uncertainty about the evidence itself
# (e.g. test_report_parse_error).
ADJUDICATED_CONFLICTS = frozenset(
    {"test_failures_detected", "test_errors_detected", UNATTRIBUTED_CONFLICT}
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
