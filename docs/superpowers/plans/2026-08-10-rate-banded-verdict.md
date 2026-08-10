# Rate-Banded Verdict Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single confusing success/partial/failed headline with three rate lines (build modules×classes, test cases×modules, coverage line %), each grain carrying its own band, and remove the invented 80% pass-rate from the verdict chain.

**Architecture:** One new pure module (`sag/verdict_rates.py`) owns the band table and the rate dataclasses; the verdict finalizer computes the rates block once (P3) from evidence it already holds; every surface (report, CLI, web, evaluator v2) renders that one block. The legacy `verdict` word survives as a mechanical derivation from bands for machine consumers.

**Tech Stack:** Python 3.12, pydantic v2 (frozen models, extra="forbid"), pytest.

## Global Constraints (from the spec, verbatim)

- Bands: `fully` == 100%; `most` 75–<100%; `half` 50–<75%; `few` >0–<50%; `none` == 0%; `unavailable` = denominator absent with a typed reason.
- Grains are listed separately, NEVER folded (no min, no weights).
- Test rate counts EXECUTION, not passing. Red tests: if `failed + errors > 50% of executed`, attach conflict `test_failures_heavy` and apply exactly one demotion `fully → most` on the test cases band only; bands at or below `most` are never demoted; red never rejects a phase close.
- Coverage headline: line % from the existing optional collection; when absent, a typed `unavailable — <reason>`, never a silent blank and never 0%.
- Legacy word derivation: build modules band `none` → `failed`; build modules `fully` AND test cases `fully` → `success`; everything else → `partial`. No rate threshold hides inside it.
- `evaluate_run_verdict`'s pass-rate policy leaves the verdict chain entirely; test-phase gate claimability becomes execution-based.
- verdict.json bumps `VERDICT_SCHEMA_VERSION` 3 → 4 with the `rates` block; metrics-v1 stays frozen; the v2 evaluator row gains the rates.
- Never weaken a test: re-express pinned expectations with a one-line premise comment. NEVER use `git stash`.

## File Structure

- Create `src/sag/verdict_rates.py` — pure: band table, `GrainRate`, `RateBand`, demotion, legacy-word derivation. No I/O, no imports from agent/.
- Create `tests/test_verdict_rates.py` — the pure module's complete fence.
- Modify `src/sag/agent/verdict_finalizer.py` — compute the rates block; schema v4; drop the 80% call.
- Modify `src/sag/agent/module_coverage.py` — expose the two build grains as one accessor (no second computation).
- Modify `src/sag/agent/phase_gates.py` — test gate claimability goes execution-based.
- Modify `src/sag/tools/report_tool.py`, `src/sag/main.py` — the three-line banner; word demoted to footnote.
- Modify `src/sag/web/…` projection + `scripts/evaluate_golden_battery.py` — surface the rates block.
- Tests: `tests/test_verdict_finalizer.py`, `tests/test_build_test_verdict.py`, `tests/test_phase_gates.py`, `tests/test_report_honesty.py`, evaluator tests — premise-updated, not weakened.

---

### Task 1: The pure rates module

**Files:**
- Create: `src/sag/verdict_rates.py`
- Test: `tests/test_verdict_rates.py`

**Interfaces:**
- Produces (later tasks rely on these exact names):
  - `band_for(numerator: int, denominator: int | None) -> str` — one of `"fully" | "most" | "half" | "few" | "none" | "unavailable"`.
  - `@dataclass(frozen=True) GrainRate: numerator: int; denominator: int | None; reason: str | None = None` with properties `rate: float | None` (percent, 1 decimal, None when unavailable) and `band: str`, and method `payload() -> dict` (serializes `{"rate", "band", "numerator", "denominator"}`, or `{"band": "unavailable", "reason": ...}`).
  - `demote_heavy_red(cases: GrainRate, failed: int, errors: int, executed: int) -> tuple[GrainRate, tuple[str, ...]]` — returns the (possibly demoted) grain and `("test_failures_heavy",)` when the signal fired.
  - `HEAVY_RED_DEMOTED_BAND = "most"` and `derived_verdict_word(build_modules: GrainRate, test_cases: GrainRate) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_verdict_rates.py
"""The band table and rate grains — the spec's §3 contract, edge-exact.

Bands: fully==100, most>=75, half>=50, few>0, none==0, unavailable when the
denominator is absent. Grains are never folded; red tests demote exactly
fully->most on the cases grain and nothing else (spec §4)."""

import pytest

from sag.verdict_rates import (
    GrainRate,
    band_for,
    demote_heavy_red,
    derived_verdict_word,
)


@pytest.mark.parametrize(
    "numerator, denominator, band",
    [
        (14, 14, "fully"),
        (99, 100, "most"),
        (75, 100, "most"),
        (74, 100, "half"),
        (50, 100, "half"),
        (49, 100, "few"),
        (1, 100, "few"),
        (0, 100, "none"),
        (0, 0, "unavailable"),
        (0, None, "unavailable"),
    ],
)
def test_band_edges(numerator, denominator, band):
    assert band_for(numerator, denominator) == band


def test_grain_rate_payload_and_percent():
    grain = GrainRate(numerator=6, denominator=18414)
    assert grain.band == "few"
    assert grain.rate == 0.0  # 6/18414 rounds to 0.0 but band stays few, not none
    assert grain.payload() == {
        "rate": 0.0,
        "band": "few",
        "numerator": 6,
        "denominator": 18414,
    }


def test_unavailable_grain_serializes_reason_never_zero():
    grain = GrainRate(numerator=0, denominator=None, reason="static discovery found no count")
    assert grain.band == "unavailable"
    assert grain.rate is None
    assert grain.payload() == {
        "band": "unavailable",
        "reason": "static discovery found no count",
    }


def test_heavy_red_demotes_exactly_fully_to_most():
    fully = GrainRate(numerator=100, denominator=100)
    demoted, conflicts = demote_heavy_red(fully, failed=60, errors=0, executed=100)
    assert demoted.band == "most" and conflicts == ("test_failures_heavy",)
    # at-or-below most: signal recorded, band untouched
    half = GrainRate(numerator=60, denominator=100)
    kept, conflicts = demote_heavy_red(half, failed=40, errors=21, executed=60)
    assert kept is half and conflicts == ("test_failures_heavy",)
    # no heavy red: nothing happens
    same, conflicts = demote_heavy_red(fully, failed=10, errors=0, executed=100)
    assert same is fully and conflicts == ()


def test_demoted_band_overrides_the_fraction():
    demoted, _ = demote_heavy_red(
        GrainRate(numerator=100, denominator=100), failed=80, errors=0, executed=100
    )
    assert demoted.payload()["band"] == "most"
    assert demoted.payload()["rate"] == 100.0  # the fraction stays honest


@pytest.mark.parametrize(
    "build_band_args, test_band_args, word",
    [
        ((0, 14), (100, 100), "failed"),      # none build -> failed
        ((14, 14), (100, 100), "success"),    # fully + fully -> success
        ((14, 14), (99, 100), "partial"),
        ((13, 14), (100, 100), "partial"),
        ((14, 14), (0, None), "partial"),     # unavailable is never success
    ],
)
def test_derived_verdict_word(build_band_args, test_band_args, word):
    build = GrainRate(numerator=build_band_args[0], denominator=build_band_args[1])
    test = GrainRate(numerator=test_band_args[0], denominator=test_band_args[1])
    assert derived_verdict_word(build, test) == word
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_verdict_rates.py -q -p no:cacheprovider`
Expected: collection ImportError (`No module named 'sag.verdict_rates'`).

- [ ] **Step 3: Implement**

```python
# src/sag/verdict_rates.py
"""Pure band table and rate grains for the rate-banded verdict (spec 2026-08-10).

One place decides what a percentage is CALLED. Grains are never folded — no
min(), no weights; the reader sees both grains and both bands. The only
mutation ever applied is the heavy-red weak signal: exactly fully -> most on
the test cases grain, because execution is what SAG grades and the project's
red is not SAG's repair duty."""

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
    """Spec §4: the weak signal. Fires when failed+errors > 50% of executed;
    the ONLY band change it may make is fully -> most on this grain."""
    if executed <= 0 or (failed + errors) * 2 <= executed:
        return cases, ()
    if cases.band == BAND_FULLY:
        return replace(cases, band_override=HEAVY_RED_DEMOTED_BAND), (HEAVY_RED_CONFLICT,)
    return cases, (HEAVY_RED_CONFLICT,)


def derived_verdict_word(build_modules: GrainRate, test_cases: GrainRate) -> str:
    """Spec §5: the legacy word, mechanically from bands. No rate threshold."""
    if build_modules.band == BAND_NONE:
        return "failed"
    if build_modules.band == BAND_FULLY and test_cases.band == BAND_FULLY:
        return "success"
    return "partial"
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_verdict_rates.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/sag/verdict_rates.py tests/test_verdict_rates.py
git commit -m "feat: the band table — one place decides what a percentage is called"
```

---

### Task 2: Build grains from the one module-coverage computation

**Files:**
- Modify: `src/sag/agent/module_coverage.py` (add ONE accessor at module end)
- Test: `tests/test_module_coverage_shared.py` (append)

**Interfaces:**
- Consumes: `GrainRate` from Task 1; `module_coverage(validator, project_name)`'s existing dict (its `summary` carries `modules_total` / `modules_built`; its per-module records carry the class-weighted expectation fields — read `module_coverage.py:202` before writing, and take numerator/denominator from the fields the summary ALREADY aggregates; do not re-scan).
- Produces: `build_grain_rates(coverage: dict | None, *, compiled_classes: int | None, source_files: int | None) -> dict[str, GrainRate]` returning `{"modules": GrainRate, "classes": GrainRate}`. Modules grain from `summary.modules_built / summary.modules_total`; when coverage is None or `modules_total == 0`, `GrainRate(0, None, reason="no module scan available")`. Classes grain from `compiled_classes / source_files` (the validator's existing physical counts, threaded by Task 4); absent either count → typed reason `"class census unavailable"`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_module_coverage_shared.py`)

```python
def test_build_grain_rates_read_the_summary_and_the_class_census():
    from sag.agent.module_coverage import build_grain_rates

    coverage = {"summary": {"modules_total": 14, "modules_built": 12}}
    grains = build_grain_rates(coverage, compiled_classes=3400, source_files=3412)

    assert grains["modules"].payload() == {
        "rate": 85.7, "band": "most", "numerator": 12, "denominator": 14,
    }
    assert grains["classes"].band == "most"
    assert grains["classes"].numerator == 3400


def test_build_grain_rates_type_their_absences():
    from sag.agent.module_coverage import build_grain_rates

    grains = build_grain_rates(None, compiled_classes=None, source_files=None)

    assert grains["modules"].payload() == {
        "band": "unavailable", "reason": "no module scan available",
    }
    assert grains["classes"].payload() == {
        "band": "unavailable", "reason": "class census unavailable",
    }
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/python -m pytest tests/test_module_coverage_shared.py -q -p no:cacheprovider` → ImportError on `build_grain_rates`.

- [ ] **Step 3: Implement** (append to `src/sag/agent/module_coverage.py`)

```python
def build_grain_rates(coverage, *, compiled_classes, source_files):
    """Spec §2.1: the two build grains, from numbers this module ALREADY
    aggregated (P3 — no second scan). Modules = scope, classes = substance."""
    from sag.verdict_rates import GrainRate

    summary = (coverage or {}).get("summary") or {}
    total = int(summary.get("modules_total") or 0)
    if total > 0:
        modules = GrainRate(numerator=int(summary.get("modules_built") or 0), denominator=total)
    else:
        modules = GrainRate(0, None, reason="no module scan available")
    if compiled_classes is not None and source_files:
        classes = GrainRate(numerator=int(compiled_classes), denominator=int(source_files))
    else:
        classes = GrainRate(0, None, reason="class census unavailable")
    return {"modules": modules, "classes": classes}
```

- [ ] **Step 4: Run to verify pass**, then the file's full suite.
- [ ] **Step 5: Commit** — `git commit -m "feat: build grains — scope and substance, from the one computation"`

---

### Task 3: Test grains — cases from the snapshot, modules from canonical rows × test islands

**Files:**
- Modify: `src/sag/agent/verdict_finalizer.py` (one helper above `_snapshot_verdict`)
- Test: `tests/test_verdict_finalizer.py` (append; read the file's fixture style first)

**Interfaces:**
- Consumes: `SnapshotTestStats` (has `unique.executed`, `discovered`, `unique.failed`, `unique.errors`); the finalizer's already-loaded canonical row set (each row carries its module coordinate — read `receipt_test_rows.py` for the exact field, it is the module root the envelope proved); the manifest's `test_islands` (roots with test sources).
- Produces: `test_grain_rates(stats: SnapshotTestStats, driven_modules: set[str], test_modules: set[str]) -> tuple[dict[str, GrainRate], tuple[str, ...]]` — `{"cases": ..., "modules": ...}` plus conflicts from `demote_heavy_red`. Cases: `unique.executed / discovered`; `discovered` None or 0 → reason `"static discovery found no count"`. Modules: `len(driven & test_modules) / len(test_modules)`; empty `test_modules` → reason `"no test modules surveyed"`. Modules without test sources never enter the denominator (spec §2.2).

- [ ] **Step 1: Failing test** (exact fixture imports copied from the top of `tests/test_verdict_finalizer.py`)

```python
def test_test_grain_rates_cases_and_modules_with_weak_signal():
    from sag.agent.verdict_finalizer import SnapshotTestCounts, SnapshotTestStats, test_grain_rates

    stats = SnapshotTestStats(
        discovered=100,
        unique=SnapshotTestCounts(executed=100, passed=20, failed=60, errors=20, skipped=0),
        raw=SnapshotTestCounts(executed=100, passed=20, failed=60, errors=20, skipped=0),
    )
    grains, conflicts = test_grain_rates(
        stats, driven_modules={"/w/p/core"}, test_modules={"/w/p/core", "/w/p/io"}
    )
    assert grains["cases"].band == "most"          # fully demoted by heavy red
    assert grains["cases"].rate == 100.0            # the fraction stays honest
    assert conflicts == ("test_failures_heavy",)
    assert grains["modules"].payload()["numerator"] == 1
    assert grains["modules"].band == "half"


def test_test_grain_rates_type_their_absences():
    from sag.agent.verdict_finalizer import SnapshotTestCounts, SnapshotTestStats, test_grain_rates

    stats = SnapshotTestStats(discovered=None, unique=SnapshotTestCounts(), raw=SnapshotTestCounts())
    grains, conflicts = test_grain_rates(stats, driven_modules=set(), test_modules=set())
    assert grains["cases"].payload()["reason"] == "static discovery found no count"
    assert grains["modules"].payload()["reason"] == "no test modules surveyed"
    assert conflicts == ()
```

- [ ] **Step 2: Verify failure.**
- [ ] **Step 3: Implement** in `verdict_finalizer.py`:

```python
def test_grain_rates(stats, driven_modules, test_modules):
    """Spec §2.2: execution-based test grains. Cases = substance, modules =
    scope (the lucene guard: 6/6 'discovered' cannot hide 1-of-40 modules)."""
    from sag.verdict_rates import GrainRate, demote_heavy_red

    if stats.discovered:
        cases = GrainRate(numerator=stats.unique.executed, denominator=stats.discovered)
    else:
        cases = GrainRate(0, None, reason="static discovery found no count")
    cases, conflicts = demote_heavy_red(
        cases,
        failed=stats.unique.failed,
        errors=stats.unique.errors,
        executed=stats.unique.executed,
    )
    if test_modules:
        modules = GrainRate(
            numerator=len(driven_modules & test_modules), denominator=len(test_modules)
        )
    else:
        modules = GrainRate(0, None, reason="no test modules surveyed")
    return {"cases": cases, "modules": modules}, conflicts
```

The caller (Task 4) supplies `driven_modules` from the canonical rows' proven module roots and `test_modules` from the live manifest's `test_islands` roots plus `test_root` when islands are empty — read both from evidence the finalizer already holds; add no new probes.

- [ ] **Step 4: Verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat: test grains — cases and modules, execution-based"`

---

### Task 4: The rates block in verdict.json — schema v4

**Files:**
- Modify: `src/sag/agent/verdict_finalizer.py` (`VERDICT_SCHEMA_VERSION = 4`; `RunVerdictSnapshot` gains `rates: dict`; the finalize path assembles it; `_snapshot_verdict`'s word becomes `derived_verdict_word` and `evaluate_run_verdict` leaves this path)
- Test: `tests/test_verdict_finalizer.py`, `tests/test_build_test_verdict.py` (premise-update pinned words: a red-test run that was `failed` under the 80% line is now graded by bands)

**Interfaces:**
- Consumes: Tasks 1–3 producers; `build_grain_rates` needs `compiled_classes` (already on `BuildEvidenceSnapshot.compiled_classes`) and `source_files` (the validator's existing source census — thread it into the finalize inputs next to compiled_classes; read where compiled_classes is populated and take the source count from the same physical status dict).
- Produces: `RunVerdictSnapshot.rates` serialized exactly as spec §5:
  `{"build": {"modules": .payload(), "classes": .payload()}, "test": {"cases": ..., "modules": ...}, "coverage": {...}}`. Coverage sub-block from `module_metrics.json`'s merged totals when present (`{"line_rate": <float>, "source": <coverage_source>, "status": "collected"}`), else `{"status": "unavailable", "reason": "coverage pass not run"}` (or `"no reports found"` when the pass ran empty — the runner records which). `test_failures_heavy` joins the snapshot's `conflicts`. The legacy `verdict` field = `derived_verdict_word(build_modules, test_cases)` EXCEPT: keep the existing conflict cap (`run_verdict(...)`) applied AFTER derivation so unadjudicated conflicts still cap at partial.
- Schema note: v3 fixtures must still LOAD (additive read: `rates` defaults to `{}` on old payloads via a `model_validator(mode="before")` that stamps `schema_version` forward only on WRITE); replay fixtures under `tests/fixtures/` are never rewritten.

- [ ] **Step 1: Failing tests** — three: (a) a finalize round-trip whose snapshot carries the full rates block with the exact payload shapes of Task 1; (b) a heavy-red finalize showing `test_failures_heavy` in `conflicts`, cases band `most`, and legacy word `partial` (NOT `failed` — the 80% line is gone; premise-comment the flipped pin); (c) a v3 fixture payload loads with `rates == {}`.
- [ ] **Step 2: Verify failures.**
- [ ] **Step 3: Implement** — assemble in the same function that builds `RunVerdictSnapshot` today; delete the `evaluate_run_verdict` import and its call sites in this file; keep `_snapshot_verdict`'s tri-state fold ONLY as input to gates that still read it, but the snapshot's `verdict` field now comes from `derived_verdict_word` + conflict cap.
- [ ] **Step 4: Run `tests/test_verdict_finalizer.py tests/test_build_test_verdict.py tests/test_evidence_replay_idempotence.py` to pass; premise-update pins that encoded the 80% behavior.**
- [ ] **Step 5: Commit** — `git commit -m "feat: verdict v4 — the rates block is the headline, the word is derived"`

---

### Task 5: Execution-based test gate

**Files:**
- Modify: `src/sag/agent/phase_gates.py` (the test-phase gate's claimability inputs)
- Test: `tests/test_phase_gates.py` (append; premise-update any pin that let pass-rate reject a close)

**Interfaces:**
- Consumes: the gate's existing validated facts (terminal receipt existence, execution counts). Nothing new from Tasks 1–4 — the gate does not read bands.
- Produces: a test-phase claim with a terminal runner receipt and executed > 0 is claimable regardless of failed/errors; the ONLY test-content rejections that remain are evidence-integrity ones (no receipt, unreadable ledger, unresolved coordinates). Where the gate previously consulted a pass-rate or `evaluate_run_verdict`, that consultation is deleted.

- [ ] **Step 1: Failing test**

```python
def test_red_tests_never_reject_a_test_phase_close():
    """Spec §4/§5: execution is what SAG grades. A driven surface with heavy
    failures closes claimably; red is the project's fact, not SAG's failure.
    Premise updated 2026-08-10: the invented 80% line left the chain."""
    # Build the gate exactly as the neighboring accepted-close tests in this
    # file do (copy the nearest green fixture), with unique executed=100,
    # failed=60, errors=20, one terminal runner receipt.
    # Assert: gate.accepted is True; "test_failures" not in gate.code.
```

Write it concretely by copying the nearest accepted-close fixture in the file — the fixture construction is file-specific; the assertion block above is the contract.

- [ ] **Step 2: Verify it fails** (today's gate rejects or caps on the red counts).
- [ ] **Step 3: Implement** — locate the test gate's use of pass-rate/`evaluate_run_verdict` in `phase_gates.py` (grep both), delete that input, and let the receipt+execution facts decide. Keep every integrity rejection untouched.
- [ ] **Step 4: Run `tests/test_phase_gates.py tests/test_test_attempt_policy.py tests/test_untried_islands_gate.py` — premise-update flipped pins.**
- [ ] **Step 5: Commit** — `git commit -m "feat: the test gate grades execution — red belongs to the project"`

---

### Task 6: The three-line banner — report, CLI

**Files:**
- Modify: `src/sag/tools/report_tool.py` (the summary renderer that today prints `Result:`/`Tests:` — line ~790), `src/sag/main.py` (the final `Project setup verdict:` print)
- Test: `tests/test_report_honesty.py` (append)

**Interfaces:**
- Consumes: the sealed snapshot's `rates` block (Task 4). Rendering helper `render_rate_lines(rates: dict) -> list[str]` lives in `src/sag/verdict_rates.py` (pure, takes the serialized block):

```python
def render_rate_lines(rates: dict) -> list[str]:
    """Three lines, grains side by side, band beside each fraction."""
    def grain(g):
        if g.get("band") == "unavailable":
            return f"unavailable — {g.get('reason')}"
        return f"{g['numerator']}/{g['denominator']} ({g['band']})"

    build = rates.get("build") or {}
    test = rates.get("test") or {}
    cov = rates.get("coverage") or {}
    if cov.get("status") == "collected":
        cov_line = f"{cov.get('line_rate')}% line ({cov.get('source')})"
    else:
        cov_line = f"unavailable — {cov.get('reason', 'not collected')}"
    return [
        f"Build:    modules {grain(build.get('modules', {}))} · classes {grain(build.get('classes', {}))}",
        f"Tests:    cases {grain(test.get('cases', {}))} · modules {grain(test.get('modules', {}))}",
        f"Coverage: {cov_line}",
    ]
```

- Produces: report body and CLI final output lead with these three lines; the legacy word prints once below them as `Verdict (derived): partial`. Failed/error counts stay visible on the Tests line's tail when nonzero: append `" — {failed} failed, {errors} errors (project-owned)"`.

- [ ] **Step 1: Failing test** — feed a snapshot dict with a full rates block through the report renderer; assert the three lines appear in order, the word appears only in the `Verdict (derived):` footnote, and a red-test snapshot shows the `project-owned` tail.
- [ ] **Step 2: Verify failure. Step 3: Implement (helper in verdict_rates.py + call sites). Step 4: Suite for `tests/test_report_honesty.py tests/test_report_tool_metrics_artifact.py`. Step 5: Commit** — `git commit -m "feat: the banner answers where and how much, not one word"`

---

### Task 7: Web projection + evaluator v2 row

**Files:**
- Modify: the web verdict projection (grep `evidenceLayers`/verdict in `src/sag/web/`), `scripts/evaluate_golden_battery.py`
- Test: existing web projection test file + `tests/test_evaluate_golden_battery.py` (append one rates-passthrough test each)

**Interfaces:**
- Consumes: the serialized `rates` block. Both surfaces pass it through UNTRANSFORMED (the band vocabulary is the API); the evaluator row gains `"rates": snapshot["rates"]` and its comparability check treats a v3 row (no rates) as incomparable-on-rates, never as zeros.
- Produces: web JSON carries `rates`; evaluator row carries `rates`; no aggregation math anywhere.

- [ ] Steps: failing passthrough tests → verify → implement → suite (`tests/test_evaluate_golden_battery.py`, web tests + `cd webui && npm test -- --run` if the UI reads the field) → commit `git commit -m "feat: rates ride the projections untransformed"`.

---

### Task 8: Full-suite convergence + live acceptance

**Files:**
- Test: whole tree.

- [ ] **Step 1:** `.venv/bin/python -m pytest tests/ -q -p no:cacheprovider` — every failure is either a premise-update (flipped 80% pins, banner pins) fixed with a one-line premise comment, or a real regression fixed in code. No assertion weakened.
- [ ] **Step 2:** Replay acceptance (spec §7): load the two live-proof verdicts (`docker exec sag-lp-commons-dbcp cat /workspace/.setup_agent/verdict.json`, same for `sag-lp-cayenne`) through the new finalizer path in a scratch script; confirm dbcp renders all-fully lines and cayenne renders its real fractions, both with the word in the footnote. Save both renders into `docs/superpowers/reports/` appendix material (do not commit yet — the report is the owner's gate).
- [ ] **Step 3:** One live rerun: `nohup .venv/bin/sag project https://github.com/apache/commons-dbcp.git --name lp-dbcp-rates --ref rel/commons-dbcp-2.14.0 --record --coverage > logs/liveproof-20260810/console-dbcp-rates.log 2>&1 &` — the banner must show three rate lines INCLUDING a collected coverage percentage (the `--coverage` flag exercises the JaCoCo pass end to end).
- [ ] **Step 4:** Commit any convergence fixes; final commit `git commit -m "feat: rate-banded verdict — acceptance green"`.

## Self-Review (done inline)

1. **Spec coverage:** §2.1→Task 2; §2.2→Task 3; §2.3+§5 schema→Task 4; §3 bands→Task 1; §4 weak signal→Tasks 1/3/5; §5 gates→Task 5, word derivation→Tasks 1/4, banner→Task 6; evaluator/web→Task 7; §7 acceptance→Task 8. No gaps.
2. **Placeholders:** Task 5 Step 1 directs copying the nearest green fixture with the contract stated — deliberate (fixture shapes are file-local); all other steps carry complete code.
3. **Type consistency:** `GrainRate(numerator=, denominator=, reason=, band_override=)`, `payload()`, `band_for`, `demote_heavy_red`, `derived_verdict_word`, `render_rate_lines`, `build_grain_rates`, `test_grain_rates` — names identical across Tasks 1–7.
