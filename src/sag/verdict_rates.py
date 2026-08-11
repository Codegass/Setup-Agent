"""Pure band table and rate grains for the rate-banded verdict.

One place decides what a percentage is called. Grains are never folded: no
minimum, weighting, or hidden aggregate combines them. The only band mutation
is the heavy-red weak signal, which can demote the test-cases grain from
``fully`` to ``most`` without changing its underlying fraction.
"""

from dataclasses import dataclass, replace

BAND_FULLY = "fully"
BAND_MOST = "most"
BAND_HALF = "half"
BAND_FEW = "few"
BAND_NONE = "none"
BAND_UNAVAILABLE = "unavailable"

MOST_FLOOR = 75.0
HALF_FLOOR = 50.0
HEAVY_RED_DEMOTED_BAND = BAND_MOST
HEAVY_RED_CONFLICT = "test_failures_heavy"


def band_for(numerator: int, denominator: int | None) -> str:
    """Name one exact fraction using the shared verdict band table."""

    if not denominator or denominator <= 0:
        return BAND_UNAVAILABLE
    if numerator <= 0:
        return BAND_NONE
    if numerator >= denominator:
        return BAND_FULLY
    percent = numerator / denominator * 100.0
    if percent >= MOST_FLOOR:
        return BAND_MOST
    if percent >= HALF_FLOOR:
        return BAND_HALF
    return BAND_FEW


@dataclass(frozen=True)
class GrainRate:
    """One independently reported numerator/denominator grain."""

    numerator: int
    denominator: int | None
    reason: str | None = None
    band_override: str | None = None

    @property
    def band(self) -> str:
        return self.band_override or band_for(self.numerator, self.denominator)

    @property
    def rate(self) -> float | None:
        if not self.denominator or self.denominator <= 0:
            return None
        return round(self.numerator / self.denominator * 100.0, 1)

    def payload(self) -> dict:
        """Serialize the grain without turning absence into a zero rate."""

        if self.band == BAND_UNAVAILABLE:
            return {
                "band": BAND_UNAVAILABLE,
                "reason": self.reason or "denominator unavailable",
            }
        return {
            "rate": self.rate,
            "band": self.band,
            "numerator": self.numerator,
            "denominator": self.denominator,
        }


def demote_heavy_red(
    cases: GrainRate, failed: int, errors: int, executed: int
) -> tuple[GrainRate, tuple[str, ...]]:
    """Record heavy project-owned red and demote only ``fully`` to ``most``."""

    if executed <= 0 or (failed + errors) * 2 <= executed:
        return cases, ()
    if cases.band == BAND_FULLY:
        return replace(cases, band_override=HEAVY_RED_DEMOTED_BAND), (HEAVY_RED_CONFLICT,)
    return cases, (HEAVY_RED_CONFLICT,)


def derived_verdict_word(build_modules: GrainRate, test_cases: GrainRate) -> str:
    """Derive the legacy word mechanically from two named bands."""

    if build_modules.band == BAND_NONE:
        return "failed"
    if build_modules.band == BAND_FULLY and test_cases.band == BAND_FULLY:
        return "success"
    return "partial"


def render_rate_lines(rates: dict) -> list[str]:
    """Render the one three-line rate headline from its serialized block."""

    def grain(value: object) -> str:
        item = value if isinstance(value, dict) else {}
        if item.get("band") == BAND_UNAVAILABLE:
            return f"unavailable — {item.get('reason') or 'rate unavailable'}"
        numerator = item.get("numerator")
        denominator = item.get("denominator")
        band = item.get("band")
        if numerator is None or denominator is None or not band:
            return "unavailable — rate unavailable"
        return f"{numerator}/{denominator} ({band})"

    build = rates.get("build") if isinstance(rates, dict) else {}
    tests = rates.get("test") if isinstance(rates, dict) else {}
    coverage = rates.get("coverage") if isinstance(rates, dict) else {}
    build = build if isinstance(build, dict) else {}
    tests = tests if isinstance(tests, dict) else {}
    coverage = coverage if isinstance(coverage, dict) else {}
    if coverage.get("status") == "collected":
        coverage_line = f"{coverage.get('line_rate')}% line ({coverage.get('source')})"
    else:
        coverage_line = f"unavailable — {coverage.get('reason') or 'not collected'}"
    return [
        "Build:    "
        f"modules {grain(build.get('modules'))} · classes {grain(build.get('classes'))}",
        "Tests:    " f"cases {grain(tests.get('cases'))} · modules {grain(tests.get('modules'))}",
        f"Coverage: {coverage_line}",
    ]
