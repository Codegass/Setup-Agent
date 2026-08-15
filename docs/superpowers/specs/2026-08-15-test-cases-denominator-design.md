# The test-cases denominator — what can legitimately bound executed tests (#39)

**Date:** 2026-08-15
**Status:** design decision, ready to implement
**Owner definition (fixed):** test rate = executed / discovered. Red tests are
project facts, never SAG's repair duty. This spec does not change the metric;
it defines how its denominator is PRODUCED and what it may claim.

## 0. Evidence

- polaris d2r3: `discovered: 1347` while `test_catalog_summary.by_module`
  totals 593 across 8 modules — two producers, one field, no provenance
  (polaris.md Slice 1).
- tapestry-5 / dbcp / httpcomponents / jackrabbit / samza (d2r2): numerator
  exceeds the static denominator — parameterized suites and factories expand
  declarations into more executions than were declared. The unbounded band
  states it honestly today.
- camel (d2r2/d2r3): "static discovery found no count" — some repositories
  have no unified census at all (the Plan 8 lane-c lesson: no basis is its
  own answer, task #26).
- geode/kafka (with gate-truth landed): the attributed executed count is
  receipt-backed and trustworthy; the denominator is the weak side.
- bash-run tests produce no receipts by design (geode d2r2); with #54 fixed,
  every build-tool dispatch persists its receipt, so unattributed volume now
  measures exactly the out-of-harness executions.

## 1. The core distinction: a census is a floor, not a ceiling

A static census counts test DECLARATIONS. Executions expand it
(parameterization, factories, repeated runs) and shrink it (exclusions,
filters, skipped modules). Therefore:

1. **The static census is never treated as a ceiling.** The unbounded band
   (`rate_denominator_not_a_bound`, INFO) is the correct and permanent
   treatment when receipt-backed executions exceed it — it is not a defect
   to fix but a fact to state. No future change may cap or scale the
   numerator to the census.
2. **The only legitimate ceiling is execution-derived:** the terminal
   receipt-backed run's own totals (executed + skipped + filtered as the
   runner reports them) bound what that run could count. This bound applies
   to per-run claims (the gate's "did this dispatch run its selection"),
   never to the session-level rate, whose denominator stays the census by
   owner definition.

## 2. Denominator production rules

1. **One producer.** `test_stats.discovered` comes from exactly one census
   producer per run. The auditable producer is the per-module sum with named
   modules (`test_catalog_summary.by_module`); a total that no module list
   explains (polaris's 1,347) is banned — if the trunk carries a bare total
   and a module breakdown that disagree, the module sum WINS and the
   disagreement seals as conflict `test_census_sources_disagree` with both
   numbers.
2. **Named basis.** The sealed denominator carries
   `denominator_basis: complete | partial | none`:
   - `complete` — every surveyed test-bearing module was measured;
   - `partial` — some modules unmeasured (the denominator is a floor; the
     cases-grain reason names how many modules are missing from it);
   - `none` — no census exists; the cases grain is `unavailable` exactly as
     today, and no invented number appears.
3. **No cross-language mixing** (already fenced: java static counts never
   feed python denominators — 992229d stays).
4. **Expansion is stated, not normalized.** When receipt-backed executions
   exceed a `complete` census, the reason names the expansion
   (`… parameterized expansion over N declared`), keeping the unbounded band.

## 3. What stays out of scope

- **No bash receipt path.** Tests run through `bash` remain unattributed and
  disclosed (gate-truth contract). Legitimizing a receipt-free evidence
  producer would reopen the fabrication door the claim partition closed. The
  model-facing objectives already steer dispatches through
  `build(action=test)`; that is the whole remedy.
- **The rate definition.** executed/discovered stays; bands stay; the weak
  signal stays.

## 4. Acceptance

- polaris shape: one denominator (593, by_module), conflict
  `test_census_sources_disagree` naming 1,347, basis `partial` or `complete`
  as measured.
- camel shape: basis `none`, cases grain `unavailable`, no invented number.
- tapestry shape: unchanged unbounded band, reason names expansion.
- A partial census names the unmeasured modules in the grain reason.
