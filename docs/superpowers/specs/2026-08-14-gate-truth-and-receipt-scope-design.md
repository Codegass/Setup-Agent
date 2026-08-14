# Gate truth and receipt scope — the scorekeeper stops lying by omission

**Date:** 2026-08-14
**Status:** design decision, ready to implement (tasks #48/#49/#50)
**Evidence:** D2-r2 slice corpus (commit 2e9ebea), byte-verified. Every claim
below carries its slice locator.

The campaign's three highest-impact harness defects share one disease: the
layer between physical evidence and the sealed verdict speaks with more than
one voice. This spec sets three contracts that reduce it to one.

## 1. Fix A — receipt-scoped counting must not zero real executions (#48, merges #39)

### Convicting evidence

- geode: two `./gradlew test` runs ended BUILD SUCCESSFUL;
  `auxiliary_test_stats` recorded executed 10,448 / passed 10,422; the sealed
  `test_stats.raw/unique` recorded **0**, `rates.test.cases` = 0/9754 band
  `none` (geode.md §0, §2.101).
- freemarker / httpcomponents-client: the decisive test dispatch ran in the
  BUILD phase; its receipt exists and the counts matched baseline, yet the
  test phase closed `test_candidate_resolution_unavailable`
  (freemarker.md / httpcomponents-client.md, final gates).
- polaris: `discovered: 1347` while `test_catalog_summary.by_module` totals
  593 (polaris.md Slice 1) — the denominator side of the same question,
  tracked by #39's denominator rules and out of scope here beyond what §1.3
  states.

### What stays (deliberately)

Receipt scoping exists to stop fabricated or stale reports from counting:
a report XML only enters the headline count when its content hash is claimed
by a terminal invocation receipt. That anti-fabrication property is correct
and stays. P4 still holds: nothing here removes evidence.

### Contract

1. **Run-wide receipts.** The verified-claims input to the report scan
   (`physical_validator._verified_report_claims(records, primary_root)`)
   admits terminal invocation receipts from ANY phase of the current run —
   a `build(action=test)` receipt harvested during the build phase binds its
   reports exactly as a test-phase receipt does. Receipts remain run-scoped
   (never from a previous run; the hash claim already enforces content
   identity).
2. **Gate close consults run-wide receipts.** The engine's close path may
   only emit `test_candidate_resolution_unavailable` after the run-wide
   receipt set is empty of test-bearing receipts. freemarker's shape — counts
   sealed from a build-phase receipt, close code says "unavailable" — becomes
   unconstructible: with such a receipt present the close grades execution
   (`test_execution_observed`).
3. **Unattributed executions become a named conflict, not a silent zero.**
   When `auxiliary_test_stats.executed > 0` and the headline executed count
   is 0, the verdict MUST carry conflict
   `test_executions_unattributed_to_receipts` and the rates block MUST state
   the excluded volume in the cases grain's reason (e.g. `0/9754 — 10,448
   executions visible on disk but bound to no receipt`). Auxiliary counts
   still do not enter the headline number: visibility without authority.
4. **Forensic gate before implementation.** The implementing change must
   first reproduce, from the archived geode session host artifacts
   (`logs/session_20260813_193300_102527_767e4d983969_99841/.setup_agent/`),
   the exact drop point: did the two gradlew receipts exist with report
   claims, and which paths did they claim vs. which report dirs the scan
   found? If the dropper is a claims-production bug (e.g. claims limited to
   the primary coordinate's directory while reports landed across module
   dirs), fix the claims producer as part of this task; if the dropper is
   phase-scoping of `records`, item 1 already covers it. The finding is
   recorded in the implementation commit message.

### Acceptance

- A fixture with a build-phase test receipt and its reports: headline count
  admits them; test close grades `test_execution_observed`.
- A fixture with on-disk reports bound to no receipt: headline 0, conflict
  `test_executions_unattributed_to_receipts` present, reason names the
  volume.
- Existing anti-fabrication fences stay green (stale/claimed-changed reports
  still excluded).

## 2. Fix B — the 80% thresholds leave the gate/claim chain (#49)

### Convicting evidence

- kafka: model claimed `partial`; gate upgraded to `success`, reason
  *"Tests passed above the 80% threshold: 3568/3571 (99.9%)"* (kafka.md S8).
- geode: *"Tests below the 80% pass threshold: 0/0 (0.0%)"* (geode.md §2).
- ignite: reason *"Tests below the 80% pass threshold: 29/37 (78.4%)"*
  sealed alongside `validator_state: "green"`, claim upgraded to `success`
  (ignite.md S12) — the reason text and the outcome point in opposite
  directions.

v4 (rate-banded verdict, approved 2026-08-10) retired the invented 80% from
the verdict chain. These are the surviving sites upstream of it.

### Contract

1. **No pass-rate adjudication.** Remove `DEFAULT_TEST_PASS_THRESHOLD` and
   `DEFAULT_TEST_EXECUTION_THRESHOLD` from every claim-validation and
   outcome-derivation path:
   - `physical_validator.py:5381/5387` reason strings and the branch that
     selects them; the `test_pass_threshold`/`test_execution_threshold`
     parameters at 1024/1100 and their pass-throughs
     (`verdict_finalizer.py:1336-1363`, `agent.py:1574/1695`,
     `report_tool.py:2045/2125`, `settings.py` fields and env vars).
   - `build_tool.py:1733`: the analysis judgment word derives from execution
     (did the invocation run its selected tests to terminal state), never
     from pass rate. Red counts remain exact sealed facts.
2. **Replacement language is execution-based.** Gate reasons state what was
   executed against what was discovered and the red count as fact:
   `executed 3,571 of 20,497 discovered · 3,568 passed, 2 failed, 1 skipped`.
   The only remaining red-test influence is v4's weak signal (demote
   fully→most in the rates block) — which lives in `verdict_rates`, not in
   gates.
3. **Direction fence.** A gate result whose reason text asserts a deficiency
   class (below/insufficient/missing) cannot carry an upgrading
   `expected_outcome`, and vice versa. Implementation: the reason string is
   RENDERED FROM the decision branch (one function produces both the code,
   the outcome and the reason), never assembled independently — plus a fence
   test that reconstructs the ignite shape and asserts it cannot be produced.
4. **Config compatibility.** The `SAG_TEST_PASS_THRESHOLD` /
   `SAG_TEST_EXECUTION_THRESHOLD` env vars and settings fields are removed;
   nothing outside the retired sites reads them (verify by grep, delete
   dead plumbing including `verdict_finalizer.py:662-666`'s deleted param).

### Acceptance

- Grep for `TEST_PASS_THRESHOLD|TEST_EXECUTION_THRESHOLD|pass threshold`
  in `src/sag` returns only historical comments (or nothing).
- kafka shape: a `partial` claim over a 99.9% pass receipt is adjudicated on
  execution evidence, with no percentage in the reason.
- ignite shape: unconstructible (fence test).

## 3. Fix C — one grader, one word (#50)

### Convicting evidence

- camel §0.7: the observation delivered to the model read
  `"validated outcome 'failed'"`; the sealed `gate_decision` and phase record
  read `unknown`/`blocked` — three statements, two words.
- polaris S10: sequence 167's embedded `gate_result` says
  `test_execution_observed` / "All 12 tests passed" / `success`/`green`;
  sequence 169's `gate_decision` says
  `test_candidate_resolution_unavailable` / `unknown`/`unavailable`.

Mechanism (located): `phase_tool.py:472-476` renders the outcome word from
the gate object it computed; the engine's close path
(`react_engine.py:2204/3099`) re-grades and seals its own decision. Two
graders; the model acts on the first word, history records the second.

### Contract

1. **The accepted path seals what it delivered.** When a claim is accepted
   and the observation text carries an outcome word, the engine routes and
   seals THAT gate result object. Any post-acceptance re-derivation that
   would change the word is forbidden on this path — if new evidence must
   change the outcome after delivery, the change MUST produce a fresh
   observation to the model naming both words and the reason
   (`gate_outcome_revised`), and the sealed record carries the revision
   chain. No silent divergence.
2. **Engine-generated decisions render their own observation.** Where the
   engine (not phase_tool) produces the closing decision, the observation
   text delivered to the model is generated from the same event object that
   is sealed — one serialization, two sinks.
3. **Embedded copies are the same object.** `metadata.gate_result` embedded
   in a tool result and the `gate_decision` control event for the same
   grading MUST be serializations of the same object (polaris's
   adjacent-line contradiction becomes unconstructible). Where two gradings
   legitimately exist in sequence (a claim gate followed by a close gate),
   each carries a distinct `decision_id` and the later one names the earlier
   (`supersedes`), so no reader can mistake them for the same statement.
4. **Consistency fence.** A fence test walks a synthetic run and asserts:
   for every delivered observation carrying an outcome word, the sealed
   `gate_decision` for that `decision_id` carries the same word; camel's
   shape reconstructed must fail closed (engine refuses to seal a diverging
   word without the revision observation).

### Acceptance

- camel shape: unconstructible without a `gate_outcome_revised` observation.
- polaris shape: embedded and event serializations byte-equal for the same
  `decision_id`; sequential gradings carry `supersedes` linkage.
- Existing claim-matrix and transition-policy fences stay green.

## 4. Shared constraints

- Integrity-failure families (`gate_decision_persist_failed`,
  `repair_context_projection_invalid`, barrier/dispatch) keep abort
  semantics — untouched, as in #45.
- RunEvidenceState stays engine-written; no tool writes it directly.
- TDD per house style: fence test first, red, minimal change, green, full
  suite green before each commit. No `git stash`, no co-author trailers.
- Implementation order: A → B → C (A changes counting facts B's reasons
  render; C fences the words both produce).
