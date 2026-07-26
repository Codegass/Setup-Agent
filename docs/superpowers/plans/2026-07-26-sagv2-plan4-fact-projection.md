# SAG v2 — Plan 4: Fact Projection & Acceptance Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the six findings of the 2026-07-26 post-acceptance audit: the execution layer is reliable, but fact projection and the acceptance verifier are not. Make the harness's state semantics (native test scope, collection failures, island coverage) machine-honest and visible to the model, the advisor, the report, and the verdict.

**Architecture:** No protocol changes. Five surgical tasks on existing seams: the bounded-smoke policy becomes capability-gated instead of readiness-gated (python_tool); collection failures stop masquerading as executed tests (physical validator); collection facts flow into every consumer (tool output text, advisor digest, report root cause); the spec's untried-islands closure gate is finally implemented (attempt_policy/phase_tool); and the advisor's before-acting redirect is replaced by a mechanical consult-at-phase-entry so correctly-planned batches are never cancelled (engine + spec amendment).

**Audit:** the falsified-acceptance findings and six recommendations are recorded in the CORRECTION block of `docs/superpowers/reports/2026-07-26-sagv2-final-acceptance.md`.

## Ground truth from recon (verified at `460012a`)

- `python_tool._run_tests` (:1032–1300) already computes `collection_scope`
  (`filtered`/`full`), `selection_mode`, and stores them in result metadata;
  the guard comment at :1151 names exactly the failure it was meant to
  prevent. The defect: `native_unready = has_native_build and not
  _native_project_ready(...)` — an LLVM-less build passes the readiness
  probe (import + PEP 610 + any native lib), so the smoke guard disarms.
- Real collection-error XML shape (live TVM artifact
  `pytest-attempt-000001.xml`): `<testcase classname=""
  name="tests.nightly....test_x"><error message="collection failure">…
  RuntimeError: None of the following targets are supported by this build
  of TVM: ['llvm', …]`. Detection signature: `classname == ""` AND error
  message `collection failure`.
- Advisor digest seam: `react_engine._advisor_evidence_digest` (:3232).
- Untried islands text helper: `react_engine._untried_island_targets`
  (:2880). Island coordinates come from the `build_requirements.json`
  manifest (`build_islands`), same source `attempt_policy` reads.

## Global Constraints

- Never add `Co-Authored-By` trailers. NEVER use `git stash` (one
  pre-existing unrelated stash@{0} — do not touch).
- Full suite green after every task (baseline at plan time: 2,539 passed /
  1 skipped, env ±1 skip — measure your own).
- §3.3 message standard for every new rejection/refusal text.
- Pairing invariant unchanged; any new harness-authored advisor consult is
  a synthetic assistant tool_call + tool result pair (forced-attempt
  precedent, `forced-<n>` ids).
- Baselines for regression thinking: bigtop must keep 54/54 at the primary
  coordinate; commons-cli must keep 982/921/0.

## Task DAG

Stage A (parallel, disjoint): T1 (python_tool smoke-first) ∥ T2 (validator collection semantics) ∥ T3 (report root cause) ∥ T4 (untried-islands gate).
Stage B (one lane): T5 (consult-at-entry + digest projection + spec amendment).
Stage C: T6 — orchestrator-run machine-asserted verification battery (NOT a lane).

---

### Task 1: Bounded smoke is capability-gated, not readiness-gated

**Files:**
- Modify: `src/sag/tools/internal/python_tool.py` (`_run_tests` :1032–1300 and the native gating around `_native_project_ready`)
- Test: `tests/test_native_smoke_capability_gate.py` (new)

**Policy (implement exactly; audit recommendation 2):**
1. For a project with `has_native_build` AND a verified smoke candidate
   (`_verified_native_smoke_candidate` non-None), a bare
   `build(action='test')` ALWAYS selects the surveyed smoke
   (`collection_scope=filtered`, `selection_mode="survey_candidate"`) —
   `_native_project_ready` no longer disables this. Readiness becomes an
   informational metadata field (`native_ready_probe`), not a gate.
2. **Capability receipt:** when a filtered smoke attempt finishes with
   executed ≥ 1 and zero collection errors and zero test errors/failures,
   write `/workspace/.setup_agent/native_smoke_receipt.json`
   (`{"project_root", "candidate", "stats", "attempt"}`, via the
   orchestrator). A bare test runs the FULL suite only when this receipt
   exists for the same project_root. No receipt → smoke again.
3. Explicit full-suite args without a receipt → refuse with a §3.3 message
   naming the exact smoke path to run first (extend the existing arg
   sanitation branch at ~:1106–1131; keep its current allowlist behavior
   for non-native projects).
4. The result OUTPUT TEXT (model-visible) gains one line, always, for
   every pytest attempt:
   `Collection: {scope} — {collected} collected, {selected} selected, {executed} executed, {collection_errors} collection errors`
   (executed/collection_errors from Task 2's metadata once merged; until
   then compute executed = collected_after_deselection when the run
   started, 0 on collection failure — read the real result-building code
   and keep it honest).
5. Metadata keeps every existing key; adds `native_ready_probe: bool`,
   `smoke_receipt_present: bool`.

- [ ] **Step 1: red tests** — scripted-orchestrator style: (a) native
project + verified smoke + READY probe → bare test still runs filtered
smoke (today: full — this is the falsifying test for the audit's finding);
(b) smoke success writes the receipt file (orchestrator records the write);
(c) bare test WITH receipt present → full scope allowed; (d) explicit
full-suite args without receipt → refusal naming the smoke path; (e) output
text contains the `Collection:` line with the correct scope.
- [ ] **Step 2–4: implement, green** — plus `tests/test_python_tool.py tests/test_native_build_guidance.py tests/test_provider_no_deps_rung.py` and full suite.
- [ ] **Step 5: Commit** — `git commit -m "feat: native bounded smoke is capability-gated — receipt before full suite"`

---

### Task 2: Collection failures are not executed tests

**Files:**
- Modify: `src/sag/agent/physical_validator.py` (the pytest/JUnit XML aggregation — the same `_parse_single_test_xml`/`_collect_testcases_from_suite` region Plan 1 Task 5 touched)
- Test: `tests/test_collection_error_semantics.py` (new)

**Semantics (audit recommendation 3):**
- A testcase entry with `classname == ""` and an `<error message="collection failure">`
  child is a **collection error node**: counted in a new `collection_errors`
  stat, EXCLUDED from `total/passed/failed/errors/skipped` and from unique
  test identities.
- Skipped nodes emitted alongside a collection failure for the same file
  (pytest emits paired skip nodes) remain counted as skipped ONLY when they
  carry a non-empty classname; empty-classname skip nodes accompanying
  collection errors are collection artifacts and join `collection_errors_skipped`
  (kept separately, not in `skipped`).
- The per-file stats dict and the sealed verdict `test_stats` both gain
  `collection_errors` (and raw/unique views stay consistent). A run whose
  pytest attempts show ONLY collection nodes has `executed = 0`.
- Preserve the dominant structured error text: collect the first line of
  the most frequent collection-error message into the parse result as
  `collection_error_summary` (e.g. `RuntimeError: None of the following
  targets are supported by this build of TVM: ['llvm', …]`) for Task 3's
  report to quote.

- [ ] **Step 1: red test** — fixture XML copied from the real TVM artifact
shape (testsuite `errors="28" skipped="28" tests="56"`, empty-classname
collection-failure testcases + empty-classname skips): assert
`total == 0`, `collection_errors == 28`, `collection_errors_skipped == 28`,
`executed`-style counts zero, `collection_error_summary` starts with
`RuntimeError: None of the following targets`. Second fixture: a normal
suite with real classnames is counted exactly as today (regression lock,
reuse the Plan-1 Groovy fixture shape).
- [ ] **Step 2–4: implement, green** — plus `tests/test_groovy_tests_survive_oracle.py tests/test_physical_validator.py tests/test_snapshot_surface_agreement.py` and full suite.
- [ ] **Step 5: Commit** — `git commit -m "fix: pytest collection failures count as collection_errors, never as executed tests"`

---

### Task 3: Report root cause comes from structured errors

**Files:**
- Modify: `src/sag/tools/report_tool.py` (failure narrative + blockers section from Plan 3 Task 3)
- Test: extend `tests/test_report_honesty.py`

**Behavior (audit recommendations 3/4):** when sealed evidence carries
`collection_errors > 0`, the report's test section states
`Test collection failed for N files — 0 tests executed` and quotes
`collection_error_summary` verbatim as the root cause; the blockers section
derives a blocker from it. The report never presents collection numbers as
executed tests. When `collection_scope`/command facts are present on the
latest test attempt, the report names the scope and command.

- [ ] Steps: red tests (TVM-shaped inputs → assert the three renderings;
existing tests untouched) → implement → report suites + full suite →
commit `git commit -m "fix: report quotes structured collection errors as the test root cause"`

---

### Task 4: Untried-islands closure gate (spec §3.3, finally implemented)

**Files:**
- Modify: `src/sag/agent/attempt_policy.py` (extend the Task-8/Plan-1 build-closure region), `src/sag/tools/phase_tool.py` (the existing `build_attempt_requirement` hook)
- Test: `tests/test_untried_islands_gate.py` (new)

**Behavior:** `build_attempt_requirement` (or a sibling
`untried_islands_requirement` called from the same phase_tool hook) also
rejects build-phase `done`/`blocked` closure when the manifest's
`build_islands` contains islands with NO build attempt receipt bound to
that island root (receipt matching: any build-family observation whose
resolved `working_directory` normalizes into the island root). The
rejection names the untried island roots (§3.3). Exemptions: islands list
empty/absent; every island has a receipt (success or failure both count —
attempted is the bar); or the claim is `done` with outcome `success`
(the gate targets closure-by-giving-up; a success claim is already
gate-checked physically). Fail-closed on unreadable manifest is NOT
required here (Plan 1's attempt gate already handles no-attempt cases);
unreadable manifest → no island requirement.

- [ ] Steps: red tests (bigtop-shaped manifest, 4 islands: closure with 1
attempted → rejected naming the other 3; closure with all 4 attempted
(mixed outcomes) → passes to the gate; success-outcome claim → exempt;
no islands → exempt) → implement → `tests/test_build_closure_policy.py
tests/test_phase_tool.py tests/test_test_attempt_policy.py` + full suite →
commit `git commit -m "feat: build closure names untried surveyed islands before it may close"`

---

### Task 5: Consult-at-entry replaces the before-acting redirect

**Files:**
- Modify: `src/sag/agent/react_engine.py` (guarantee 1 mechanism; `_advisor_evidence_digest` :3232), `docs/superpowers/specs/2026-07-25-advisor-mode-harness-redesign.md` (§3.2 guarantee 3a amendment)
- Test: `tests/test_consult_at_entry.py` (new); update `tests/test_advisor_guarantees.py` and `tests/test_advisor_engine_flow.py` expectations

**Why (audit finding):** before-acting cancelled two correctly-planned
4-island batches in bigtop r1 (8 wasted calls), and phase re-entry resets
`_advisor_calls_in_phase`, re-arming the trap. A weak model cannot be
required to re-remember a cancelled batch.

**Mechanism:**
1. DELETE the `before-acting` redirect rule from `_advisor_redirect_for_call`.
2. On entering the build or test phase (the same seam that emits the phase
   intro — native loop phase init and `_apply_phase_decision`), when
   advisor is enabled and the phase-entry consult has not already happened
   for this entry: the HARNESS performs the consult mechanically — a
   synthetic assistant tool_call (`advisor-entry-<n>` id, native_text
   `"[harness] consulting the advisor at phase entry"`) whose result is
   `consult_advisor()`'s output, appended via the forced-attempt precedent
   so pairing and evidence recording hold. It counts toward the phase cap
   as today.
3. Re-entry after a repair loop performs a fresh entry consult (bounded by
   the cap); `before-giving-up` and `when-stuck` rules are unchanged.
4. `_advisor_evidence_digest` gains a final section when the evidence
   state holds any test-attempt observation with collection metadata:
   `Last test attempt: {command} — scope={collection_scope}, collected={collected}, selected={selected}, executed={executed}, collection_errors={n}`.
5. Spec amendment (same commit): §3.2 mechanical guarantee list — replace
   the before-acting bullet with consult-at-entry, noting the audit
   rationale and the date.

- [ ] Steps: red tests — (a) entering build phase produces exactly one
harness-authored advisor pair before the model's first turn; (b) a 4-call
build batch immediately after entry executes with ZERO redirects (the
bigtop regression shape); (c) re-entry consults again, capped; (d)
`advisor_mode=off` → no entry consult, run unaffected; (e) digest contains
the `Last test attempt:` line when a pytest observation with
collection metadata exists. Update the two existing advisor test files'
before-acting expectations (delete/replace with entry-consult
expectations, commented with the audit reference). → implement → advisor
suites + `tests/test_native_loop_engine.py` + full suite → commit
`git commit -m "feat!: advisor consult-at-entry replaces the before-acting redirect"`

---

### Task 6: Machine-asserted re-verification (orchestrator-run, NOT a lane)

After Stage A+B merge, the orchestrator writes
`scripts/verify_native_test_policy.py` (session-dir walker) and runs:
commons-cli ×1 (canary), bigtop ×1 (island gate + primary coordinate
regression), tvm ×2 (same pins). Machine assertions per audit
recommendation 6, applied to each tvm session:

1. every pytest attempt has `collection_scope == "filtered"` until a smoke
   receipt exists; zero unfiltered collects without a receipt;
2. the filtered command contains the exact surveyed smoke path
   (`tests/python/all-platform-minimal-test`);
3. `selected` ∈ 1..50 on filtered attempts; `collection_errors` recorded
   separately with `executed` consistent;
4. the report's root-cause line matches the structured
   `collection_error_summary`;
5. envelope/pairing/hash walk green; bigtop keeps ≥50 primary-coordinate
   tests; cli keeps 982/921/0.

Results go into a verification report; acceptance state upgrades from
"TVM PENDING" only if all assertions pass on both tvm runs.
