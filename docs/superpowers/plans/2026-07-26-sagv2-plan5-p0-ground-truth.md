# SAG v2 Plan 5 — P0 Ground-Truth Repairs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the P0 set from the ground-truth review
(`docs/superpowers/reports/2026-07-26-three-project-harness-ground-truth-review.md`):
capability receipts require positive evidence, invocation-scoped evidence,
typed build domains, semantic action conservation, and native capability
state — ordered E → A → B(+F) → C → D per the approved sequencing.

**Architecture:** Each stage lands as TDD'd changes on `main` via reviewed
worktree lanes. Every stage's acceptance comes from the review's falsifiable
matrix (§"Falsifiable acceptance matrix"), machine-asserted where possible.

**Tech Stack:** Python 3.12, pytest, existing SAG fake-orchestrator test
patterns.

## Global Constraints

- Base commit for Stage A lanes: `3ae1206` (verify HEAD, `git reset --hard 3ae1206` if the worktree seeded stale).
- NEVER use `git stash` in lanes (refs/stash is repo-shared).
- Absent facts serialize as absent keys (byte-compat pattern from Plans 2–4).
- No prompt-only fixes; prompt text changes only where a projected fact changes.
- No project-name special cases (no `if "tvm"`-style branches).
- Commit messages must NOT carry a Co-Authored-By trailer.

---

## Stage A — P0-E: receipts require positive evidence (+ verifier negative assertions)

Matrix rows covered: "Native smoke is all skipped → no receipt is written; the
second bare call is still bounded", "One native smoke passes and others skip →
receipt is written with positive evidence", "Source changes → old capability
receipt is rejected" (target-SHA binding, minimal form).

### Task A1: positive-evidence receipt semantics

**Files:**
- Modify: `src/sag/tools/internal/python_tool.py` (write condition ~1377–1402; read validation `_native_smoke_receipt` ~1729–1748; writer `_write_native_smoke_receipt` ~1750–1770)
- Test: `tests/test_native_smoke_receipt_positive_evidence.py` (new; follow the fake-orchestrator style of the existing `-k "smoke_receipt or native_smoke"` tests)

**Interfaces:**
- Produces: receipt JSON gains `"target_sha": str` and `"stats"["passed"]: int`; ToolResult metadata gains `"smoke_capability_unproven": true` (only on the clean-but-all-skipped case).
- Consumes: `junit_counts` (`tests`, `failed_tests`, `error_tests`, `skipped_tests`) already in scope at the write site.

**Behavior (exact):**
1. Write condition additionally requires
   `passed = executed - failed - errors - skipped >= 1`.
2. Clean all-skipped smoke (`executed >= 1`, zero failed/error, `passed == 0`):
   no receipt; `metadata["smoke_capability_unproven"] = True`; model-visible
   line: `[test] bounded smoke: all {executed} selected tests were skipped —
   capability NOT proven; no receipt written; the next bare test call remains
   bounded`.
3. Receipt payload adds `target_sha` (via orchestrator
   `git -C <project_root> rev-parse HEAD`; omit the key when the command
   fails) and `stats.passed`.
4. `_native_smoke_receipt` additionally rejects (returns None for) receipts
   whose `stats.passed` is missing or `< 1`, and receipts carrying a
   `target_sha` that mismatches the current `rev-parse HEAD` of the project
   root. Legacy vacuous receipts (like the live TVM one) are thereby inert.

**Tests (write first, one behavior each):** all-skipped mints nothing and sets
`smoke_capability_unproven`; 1-passed-2-skipped mints a receipt with
`passed == 1` and `target_sha`; a `passed: 0` receipt on disk does NOT unlock
(`native_bounded` stays true); a `target_sha`-mismatched receipt does not
unlock; a valid receipt still unlocks; byte-compat — no new metadata keys on
untouched paths.

Commit: `fix!: native smoke receipt requires positive evidence (P0-E)`

### Task A2: verifier negative assertions

**Files:**
- Modify: `scripts/verify_native_test_policy.py` (tvm profile only)

**Behavior (exact):**
1. Per pytest attempt whose metadata shows `tests >= 1`,
   `skipped_tests == tests`, zero failed/error: assert
   `smoke_receipt_written` is absent/false
   (`tvm.attempt{N}.no_receipt_on_all_skipped`).
2. Session-level: when `<session>/.setup_agent/native_smoke_receipt.json`
   exists, assert `stats.passed >= 1` (`tvm.receipt.positive_evidence`).
3. `receipt_minted` tracking trusts the flag only when that attempt's junit
   counts show `passed >= 1`; later attempts without a valid receipt must
   remain `scope == "filtered"`.

**Negative control (required, documented in the lane report):** running the
updated verifier against `logs/session_20260726_153134_67903` MUST now FAIL
on the vacuous receipt; the prior 7 assertions still pass.

Commit: `feat: verifier rejects vacuous capability receipts (P0-E negative assertions)`

---

## Stage B — P0-A: minimal invocation receipts, scoped evidence

Runner calls (maven/gradle/python test paths) persist a minimal
`InvocationReceipt`; the validator's primary test rollup consumes receipts
instead of an unscoped `rglob("*.xml")`. Matrix rows: primary/auxiliary
coexistence (Bigtop stays exactly 50/50), retry-overwrite content hashes,
JDK retry no-double-count, receipt-persistence failure blocks closure.

### Binding notes (Stage B, bound on `85fa4c7`)

**Receipt schema v1 (the cross-lane contract — EXACT):** one JSON file per
runner invocation at
`/workspace/.setup_agent/invocation_receipts/<receipt_id>.json`, written
atomically (temp file + `mv`):

```json
{
  "schema_version": 1,
  "receipt_id": "inv-<phase>-<attempt>-<seq>",
  "tool": "maven" | "gradle" | "python",
  "requested_action": "<the model's verb>",
  "effective_action": "<the verb actually executed>",
  "argv": "<full command line>",
  "working_directory": "/workspace/<...>",
  "exit_code": 0,
  "outcome": "completed" | "failed",
  "report_delta": {
    "new": [{"path": "...", "sha256": "..."}],
    "changed": [{"path": "...", "sha256": "..."}]
  }
}
```

`report_delta` is computed from before/after snapshots of report-XML content
hashes over the same scan roots the validator uses (project root +
pytest-reports dir). Unchanged files never appear. Absent facts = absent keys.

**Task B1 (lane b1): receipt module + runner integration.**
Files: NEW `src/sag/agent/invocation_receipts.py` (+ NEW test file);
integrate in `src/sag/tools/internal/maven_tool.py`,
`src/sag/tools/internal/gradle_tool.py`,
`src/sag/tools/internal/python_tool.py` (test path). Module interface:
`snapshot_reports(execute, scan_roots) -> dict[path, sha256]`,
`report_delta(before, after) -> dict`,
`write_receipt(execute, receipt: dict) -> bool` (False on persistence
failure; callers surface `receipt_persisted: false` in ToolResult metadata —
never raise). Each build/test invocation records requested vs effective
action (the facade's verb mapping is already computed at the call sites) and
the exact argv. Snapshot cost is one `find`+`sha256sum` shell round-trip per
side of the invocation.

**Task B2 (lane b2): validator + gate consumption.**
Files: `src/sag/agent/physical_validator.py`, `src/sag/agent/phase_gates.py`
(+ NEW test file with hand-written schema-v1 receipt fixtures).
`parse_test_reports` partitions the scanned XMLs: **primary** = files claimed
by `report_delta` of receipts whose `working_directory` is at/under the
primary test coordinate root and whose current content hash still matches the
receipt (hash mismatch ⇒ stale, excluded and flagged); **auxiliary** =
everything else, carried as a separate visible block
(`auxiliary_test_stats`), never entering the primary numerator/denominator.
No receipts present ⇒ legacy fallback to the current global scan with
`receipt_scoped: false`. Corrupt/unreadable receipts dir when receipts are
expected ⇒ validation error; the phase gate refuses closure (matrix row
"receipt persistence fails"). Sealed snapshot carries the new keys only when
present (byte-compat with replay fixtures, same pattern as Plan 4).

## Stage C — P0-B(+F): typed build domains and sealed domain outcomes

Survey emits neutral build domains (`produces`/`requires` coordinates,
role, environment, documented lifecycle with provenance). Independence is
derived from the coordinate graph — never a directory heuristic; incompatible
edges (Bigtop producer 3.7 vs consumers 3.5/3.6) are sealed before any
attempt. Gate/finalizer/report preserve per-domain states via the review's
truth table: required+blocked forbids global success; gates can no longer
upgrade a truthful partial; auxiliary failure is never silently erased.
Matrix rows: "not called independent", "gate cannot refine partial to
success", "classified blocker is not a green waiver".

## Stage D — P0-C: semantic action conservation

Action contracts across the build facade: compile compiles only; NO-SOURCE
cannot close a source-bearing domain; language-aware Gradle task selection
(Scala/Kotlin/Groovy); package/install skips unit+integration tests by
default using documented lifecycle args retained with provenance;
requested action, effective action, actual argv, and semantic delta are
model-visible with the first result. Matrix rows: Scala NO-SOURCE, packaging
skips environment tests, README args survive survey→execution.

## Stage E — P0-D: native capability state

Replace the binary "native ready/not built" with
`native_artifacts_present` + named capabilities (llvm/cuda:
present/absent/unknown) + `package_integrity`. Delete
`NATIVE_NOT_BUILT_TEST_GUIDANCE`'s phase-outcome trigger; native-state text is
projected from an artifact probe, never from build-phase outcome. Add a typed
native build affordance (`build(action='native', features=[...],
definitions={...}, provenance=[...])`) usable only with surveyed project-owned
provenance (CI/docs pins). Advisor digest carries the capability facts.
Matrix rows: ".so exists while integrity partial → no layer may say 'not
built'", "no project-owned policy → report unknown, don't invent flags".

---

## Acceptance

After Stage E: generative/state-transition suite green, then same-pin
three-project reruns asserted against the review's "Same-pin end-to-end
anchors" (§ anchors: commons 921/0/0 + four JARs + zero model XML parsing;
Bigtop truthful partial, primary exactly 50/50, mismatches sealed; TVM honest
path — five artifacts present, LLVM absent as capability fact, all-skipped
mints nothing and stays bounded).
