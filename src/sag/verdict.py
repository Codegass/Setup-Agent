"""The run-verdict kernel (spec §6): one ordering, one combiner.

Everything that announces an outcome — CLI banner, exit code, report Result
header, webui state — must derive it from here. The combined verdict is the
MINIMUM across independent judges (phase machine, physical validation), with
evidence conflicts capping at partial: honest uncertainty can never be
announced as a clean success."""

from typing import Iterable, Optional

VERDICT_ORDER = ["failed", "partial", "success"]
_RANK = {v: i for i, v in enumerate(VERDICT_ORDER)}

# Conflicts that merely RESTATE counted test failures. The pass-rate threshold
# policy (evaluate_run_verdict) has already adjudicated those counts into the
# physical verdict, so feeding them back into the conflict cap would demote
# every threshold-pass run with any failing test to partial (round-6 review:
# build green + 206/214 = 96.3% >= threshold announced 'partial' where
# pre-stage-3 said SUCCESS). The cap stays reserved for genuine uncertainty
# about the evidence itself (e.g. test_report_parse_error).
ADJUDICATED_CONFLICTS = frozenset({"test_failures_detected", "test_errors_detected"})


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
