"""The run-verdict kernel (spec §6): one ordering, one combiner.

Everything that announces an outcome — CLI banner, exit code, report Result
header, webui state — must derive it from here. The combined verdict is the
MINIMUM across independent judges (phase machine, physical validation), with
evidence conflicts capping at partial: honest uncertainty can never be
announced as a clean success."""

from typing import Iterable, Optional

VERDICT_ORDER = ["failed", "partial", "success"]
_RANK = {v: i for i, v in enumerate(VERDICT_ORDER)}


def combine_verdicts(*verdicts: Optional[str]) -> str:
    """Minimum of the known verdicts; non-verdicts (None/unknown) abstain."""
    known = [v for v in verdicts if v in _RANK]
    if not known:
        return "success"
    return min(known, key=_RANK.__getitem__)


def run_verdict(machine_outcome: Optional[str], physical_verdict: Optional[str],
                conflicts: Iterable[str]) -> str:
    base = combine_verdicts(machine_outcome, physical_verdict)
    if list(conflicts):
        return combine_verdicts(base, "partial")
    return base
