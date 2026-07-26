# SAG v2 Final Acceptance — Plans 1–3 Complete (spec §3.7)

**Date:** 2026-07-26
**Code under test:** main @ `e349628` (Plan 1 + Plan 2 + Plan 3 all merged)
**Pins:** model `gpt-5.4-mini` (thinking=action=advisor), image
`ubuntu:24.04@sha256:1f701c2d…`, repo refs identical to every prior baseline.
**Battery:** six recorded cold runs (`--record`, containers kept):

| Run | Session | Verdict | Physical tests |
|---|---|---|---|
| commons-cli r1 | `session_20260726_132859_18062` | **SUCCESS** | 982 unique — 921 passed / 0 failed / 0 errors / 61 skipped |
| bigtop r1 | `session_20260726_132901_18089` | **PARTIAL** | 54 unique — 54 passed / 0 failed / 0 errors |
| bigtop r2 | `session_20260726_133302_18324` | **PARTIAL** | 54 unique — 54 passed / 0 failed / 0 errors |
| tvm r1 | `session_20260726_132903_18116` | **FAILED** (honest) | 56 unique — 0 passed / 28 errors / 28 skipped |
| tvm r2 | `session_20260726_133558_18455` | **FAILED** (honest) | 56 unique — 0 passed / 28 errors / 28 skipped |
| tvm ablation (`SAG_ADVISOR_MODE=off`) | `session_20260726_134028_18626` | **FAILED** (honest) | 56 unique — 0 passed / 28 errors / 28 skipped |

## §3.7 gate — every clause

1. **Targeted regressions + suite green** ✅ — 2,539 passed / 1 skipped / 0
   failed at `e349628`; +91 tests net across Plan 3.
2. **Same pins** ✅.
3. **commons-cli retains canonical success** ✅ — third digit-identical
   reproduction (982/921/0/61) across three protocol generations.
4. **bigtop** ✅ — primary `bigtop-data-generators` receipt present
   (`working_directory=/workspace/bigtop/bigtop-data-generators`); **54 ≥
   the 50-test historical anchor, all passing, twice**; the Gradle-wrapper
   `unzip` prerequisite auto-recovered in-run; the counted tests are the
   Groovy-sourced `org.apache.bigtop.datagenerators.*` suites — the classes
   the pre-Plan-1 oracle silently deleted. PARTIAL honestly reflects the
   remaining unfinished islands.
5. **tvm** ✅ — the venv repair ladder executed; provider install attempted
   and (new this round) the post-provider `--no-deps` rung got `apache-tvm`
   installed, so **TVM reached real test execution for the first time in
   any run**: the surveyed bounded smoke (56 testcases, never a full-suite
   sweep), erroring honestly on the missing native `libtvm_ffi.so`.
   Reproduced identically twice. FAILED is the correct verdict for a
   native-unready checkout; the remaining wall is the native CMake build,
   which is real work, not a harness defect.
6. **Two repeats for bigtop and tvm** ✅ — bigtop 54/54 twice; tvm
   56/0/28/28 three times (r1, r2, ablation). The stochastic weak-model
   path reproduces.
7. **Advisor telemetry + ablation switch** ✅ — run-pin `advisor` block
   present on every run (mode, calls, phases); the ablation run reports
   `mode=off, calls=0`, zero redirects, and the two `advisor()` calls the
   model still made were answered with the disabled message.

## Mechanical integrity — 6/6 runs

Every session: envelope↔tool_result pairing exact, all envelope sha256
recompute byte-identically, zero unanswered/orphan/double-answered, zero
scheduler-era events, zero deleted-fault-class strings, run-pin
`native_loop: true`.

## Advisor in the wild

| Run | Consults (pin) | Redirects fired | Ceremony share (advisor rows excluded) |
|---|---|---|---|
| cli r1 | 2 (build, test) | before-acting ×2 | 7/23 (30%) |
| bigtop r1 | 2 | before-acting ×8 | 6/23 (26%) |
| bigtop r2 | 2 | before-acting ×5 | 7/20 (35%) |
| tvm r1 | 6 | before-acting ×2, **before-giving-up ×2** | 17/31 (55%) |
| tvm r2 | 11 | before-acting ×4, **before-giving-up ×6** | 23/42 (55%) |
| tvm abl | 0 (off) | none | 9/22 (41%) |

Honest reading, against the Plan-2 churn baseline (64/80 = 80%):

- Ceremony churn dropped to 26–55% everywhere. The drop is attributable to
  the *combination* of Plan 3 changes (advisor redirects + report honesty +
  system-prompt guidance), not the advisor alone — the ablation run also
  sits at 41%.
- The advisor's distinct measurable effects: **before-giving-up fired in
  real runs** (8 total TVM redirects converted give-up attempts into
  consults), and advisor-on runs did materially more real build work (tvm
  r2: `build` ×10 vs ablation's ×3; bigtop r1: `build` ×13) — consults
  convert into retries with new angles. On TVM the extra attempts could not
  beat the native wall, so physical outcome is advisor-independent there;
  on bigtop the work-heavy profile coincides with the first true 54/54.
- The model also used `bash` in advisor-on runs (cli ×2, tvm-r2 ×1) — the
  first spontaneous bash usage in any recorded run of this model.
- Cap semantics observed: per phase *entry* (repair loops re-enter phases
  and reset the counter) — tvm r2 consulted 11× across 5 phase entries,
  never exceeding 4 in one entry.

## Program summary (Plans 1–3)

| Generation | TVM trajectory | Bigtop trajectory |
|---|---|---|
| Baseline (2026-07-24) | died at venv, 0 build calls, "ensurepip" claimed external | fake 4/4 (Groovy filtered), primary never ran |
| Plan 1 (tool layer) | venv auto-repaired, real deps attempt, died at PEP 440 floor | — |
| Plan 2 (native protocol) | same frontier, 80% ceremony churn exposed | — |
| Plan 3 (advisor + backlog) | **deps installed, 56 bounded smoke tests executed**, honest native-unready FAILED ×3 | **54/54 Groovy tests, primary coordinate, twice; honest PARTIAL** |

Remaining known frontier (out of §3.7 scope, candidate future work): TVM's
native CMake build; report-phase ceremony on failing runs is bounded but
still the largest waste block (~55%).
