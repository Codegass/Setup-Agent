# TVM Smoke Cold Run — Plan 1 Verification

**Date:** 2026-07-26
**Session:** `logs/session_20260726_013814_75859` (`--record`; container `sag-plan1smoke-tvm-828d-r1`, kept)
**Code under test:** `a0d51e6` (Plan 1 merged)
**Pins vs 2026-07-24 baseline:** same image digest, same model (`gpt-5.4-mini`),
same prompt bundle (`6cf51855…`), same ref (`828d117e`). Only variable: Plan 1.

## Verdict on the smoke goals

| Check | Result |
|---|---|
| venv repair ladder executes live | ✅ main.log:857-922 — plain venv fails (ensurepip) → ensurepip rung fails → recreate rung fails → **apt rung installs `python3-venv python3-pip python3.12-venv` → venv recreated with pip** |
| ≥1 real build attempt in build phase | ✅ `build(action='deps')` at 01:43:02 (`output_bdb594494cfb`), 38s, terminal receipt |
| no ensurepip-lie-down path | ✅ provisioning completed; the old blocker class never surfaced to the model |
| verdict progression | ⚠️ still `failed`, 0 tests — but the death moved one layer deeper (below) |

Also exercised for the first time in a live run: **local-provider recovery**.
The deps ladder detected the unsatisfiable index requirement, installed the
surveyed provider (`pip install -e 3rdparty/tvm-ffi` → built
`apache-tvm-ffi-0.1.13.dev47+g21e30c3b1` successfully), and retried the root
install.

## New frontier (Plan 3 backlog item)

The retry of `pip install -e .` fails identically:
`Could not find a version that satisfies apache-tvm-ffi>=0.1.13` (index max:
`0.1.13rc1`). Root cause is PEP 440 ordering: the locally installed
`0.1.13.dev47` is a *pre-release below* `0.1.13`, so pip's resolver rejects it
against the declared floor and re-queries the index regardless.

**Missing rung:** after a successful local-provider install whose version does
not satisfy the declared floor, the root install must fall back to
`pip install -e . --no-deps` plus explicit installation of the remaining
declared dependencies (`ml_dtypes numpy typing_extensions`), recorded as a
narrated ladder step. → Plan 3, python_tool deps ladder.

## Run-shape observations (Plan 2 targets, unchanged by Plan 1)

- Two provision parameter errors (`project_path` rejected; missing Java
  version) cost cycles — native function-calling schema validation (Plan 2)
  addresses this class.
- Gen 1 `COMPLETION SIGNALS DETECTED` fired 5× including immediately after
  the embedded clone-time pip failure (clone auto-install still present;
  removal deferred to Plan 2 per Plan 1's deviations note).
- The model still never used bash; zero improvised repair.
- Build closure gates behaved correctly in the negative direction: with a
  real deps receipt present, `BUILD_ATTEMPT_REQUIRED` did not fire, and the
  `apache-tvm-ffi` reason (a genuine dependency-resolution failure, not a
  local prerequisite signature) was rightly allowed through to the gate.

## Baseline comparison

| | 2026-07-24 baseline | 2026-07-26 smoke |
|---|---|---|
| provision | died: venv unreachable ladder | ✅ repaired mechanically (+ JDK8/Maven for jvm binding) |
| build calls | 0 | 1 real `deps` attempt with provider recovery |
| provider recovery | never exercised | ✅ exercised, provider installed |
| death point | ensurepip treated as external blocker | PEP 440 version-floor vs local dev provider |
| duration / iterations | 3 min / 18 | 6 min / ~30 |
