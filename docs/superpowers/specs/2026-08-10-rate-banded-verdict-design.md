# Rate-banded verdict — three headline rates, two grains each, no invented threshold

**Date:** 2026-08-10
**Status:** draft for review
**Owner decision trail:** test rate counts EXECUTION not passing (project bugs
are not SAG's repair duty); heavy red tests are a weak signal only; coverage is
the existing optional JaCoCo/cov collection; grains are listed separately,
never folded with min(); bands are fully/most/half/few/none with 100/75/50/0
cutoffs; the 80% pass-rate line leaves the verdict chain.

## 1. Problem

The single `success|partial|failed` word answers none of the user's real
questions: did it build, how much of it, did the tests run, how many, where.
Live examples: a build-success run with zero tests and a 4,526-passed reactor
run both rendered "PARTIAL"; an 80% pass-rate line SAG invented turned red
projects into SAG failures. The report must lead with rates the user can act
on, and SAG's own grade must not absorb the project's bugs.

## 2. The three headline rates

All three are computed in ONE place (P3) and consumed by the report, the CLI
banner, the web projection, and the metrics-v2 evaluator row.

### 2.1 Build rate — two grains, listed separately

| grain | numerator / denominator | source (already computed) |
|---|---|---|
| modules | modules built OK / modules in scope | `module_coverage.py` single computation; reactor summary sets scope (#17) |
| classes | compiled class files / source files (class-weighted) | `module_coverage.py` substance check; catches hollow/cached shells (#18) |

### 2.2 Test rate — two grains, listed separately, EXECUTION-based

| grain | numerator / denominator | source |
|---|---|---|
| cases | canonical executed / statically discovered | `SnapshotTestCounts.execution_rate`; discovered from the static survey count |
| modules | modules with a driven test report / modules with test sources | derivable from module-qualified canonical rows (WS4) × surveyed test islands; modules without test sources owe nothing and leave the denominator |

Passing/failing counts remain VISIBLE (`— N failed, M errors`) but never
change SAG's grade, with one weak signal (§4).

### 2.3 Coverage rate — single grain headline

Line coverage percent from the existing optional collection
(`sag --coverage`: reuse project jacoco.xml, else non-invasive CLI/init-script
injection re-run; `sag/coverage/` parses into `module_metrics.json`).
Per-module detail stays in the report body. When not collected the headline
states a typed absence with its reason (`unavailable — coverage pass not
run` / `unavailable — no reports found`), never a silent blank and never 0%.

## 3. Bands

Applied per grain, always displayed WITH the raw fraction. Named constants,
no hidden judgment:

| band | rule |
|---|---|
| `fully` | rate == 100% |
| `most` | 75% <= rate < 100% |
| `half` | 50% <= rate < 75% |
| `few` | 0% < rate < 50% |
| `none` | rate == 0% |
| `unavailable` | denominator absent — typed reason attached |

Grains are NEVER folded (no min, no weights). Each grain self-describes; a
reader sees `cases 6/18,414 (few) · modules 1/40 (few)` and knows both what
ran and where. When one grain's denominator is unavailable, the other still
shows; the missing one says why (P4: absence is typed, never flattering).

## 4. Red tests: weak signal only

If `failed + errors > 50% of executed`, attach conflict
`test_failures_heavy` ("large-scale red often means the environment, not the
project") and apply exactly one demotion: a test `cases` band of `fully`
becomes `most`. Bands already at or below `most` are not demoted further —
execution is what SAG grades, and red never drops a driven surface any
lower. Red tests never reject a phase close.

## 5. The verdict word and the gates

- `evaluate_run_verdict`'s pass-rate policy (the invented 80%) leaves the
  verdict chain entirely. Test-phase gate claimability becomes
  execution-based: a terminal runner receipt exists and the test surface was
  driven; red tests do not reject the claim.
- `verdict.json` bumps to schema v4 with a `rates` block:

```json
"rates": {
  "build": {
    "modules": {"rate": 100.0, "band": "fully", "numerator": 14, "denominator": 14},
    "classes": {"rate": 100.0, "band": "fully", "numerator": 3412, "denominator": 3412}
  },
  "test": {
    "cases": {"rate": 0.03, "band": "few", "numerator": 6, "denominator": 18414},
    "modules": {"rate": 2.5, "band": "few", "numerator": 1, "denominator": 40}
  },
  "coverage": {"line_rate": 54.2, "source": "jacoco", "status": "collected"}
}
```

  An unavailable grain serializes `{"band": "unavailable", "reason": "<typed>"}`.
- The legacy `verdict` field REMAINS for machine consumers (gates routing,
  evaluator, campaign comparisons) but is now derived mechanically from
  bands: `none` build → `failed`; build modules `fully` AND test cases
  `fully` → `success`; everything else → `partial`. No rate threshold hides
  inside it.
- CLI banner and report lead with the three rate lines; the derived word
  drops to a footnote.

## 6. Out of scope

Receipt claiming, canonical counting, obligations, barrier, stall machinery:
untouched. metrics-v1 stays frozen; the v2 row gains the rates block.
`--coverage` remains optional.

## 7. Acceptance

- Unit: band table edges (100/75/50/0, unavailable), red-test demotion floor,
  derived-word mapping, v4 schema round-trip, absent-denominator typing.
- The two live-proof sessions replayed through the new finalizer render:
  dbcp → `modules fully · classes fully / cases fully` and cayenne →
  `modules <100% (band per real fraction) / cases most-or-better`, with the
  old confusing single word demoted to the footnote in both.
- One targeted live rerun (dbcp) showing the new banner.
