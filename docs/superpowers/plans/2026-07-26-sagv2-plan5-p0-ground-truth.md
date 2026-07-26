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

Survey emits neutral build domains; independence is derived from the
coordinate graph — never a directory heuristic; incompatible edges (Bigtop
producer 3.7 vs consumers 3.5/3.6) are sealed before any attempt; gates can
no longer upgrade a truthful partial. Matrix rows: "not called independent",
"gate cannot refine partial to success", "classified blocker is not a green
waiver".

### Binding notes (Stage C, bound on `ac64511`)

**Domain schema v1 (cross-lane contract — EXACT).** The analyzer
recommendation gains two keys next to the existing `build_islands` (which
stays untouched for byte-compat):

```json
"build_domains": [
  {
    "root": "/workspace/bigtop/bigtop-data-generators",
    "system": "gradle",
    "languages": ["java", "groovy"],
    "produces": [{"group": "org.apache.bigtop", "name": "bigpetstore-data-generator", "version": "3.7.0-SNAPSHOT"}],
    "requires": [{"group": "...", "name": "...", "version": "3.5.0-SNAPSHOT"}]
  }
],
"domain_edges": [
  {"consumer": "<root>", "producer": "<root>",
   "status": "compatible" | "version_incompatible",
   "detail": "requires org.apache.bigtop:bigpetstore-data-generator 3.5.0-SNAPSHOT; producer builds 3.7.0-SNAPSHOT"}
]
```

Unparseable coordinates are simply absent (absent fact = absent key/list);
an edge exists only when one domain's `requires` names another domain's
`produces` (group+name match); `version_incompatible` iff the version
strings differ. **Rollup contract:** the gate seals
`domain_states: {"<root>": {"state": "success"|"failed"|"blocked"|"untried", "blocker": "<detail>"?}}`
(absent entirely when no domains were surveyed).

**Task C1 (lane c1): survey coordinates + analyzer graph.**
Files: `src/sag/agent/physical_survey.py`,
`src/sag/tools/internal/project_analyzer.py` (+ NEW test file).
Maven produces/requires from pom.xml (groupId/artifactId/version with
parent-fallback for group/version; dependencies GAV when literal). Gradle
produces from `group`/`version` in build.gradle or gradle.properties plus
subproject names; requires from literal `"g:n:v"` dependency strings
(recursive over the multi-project's subproject build files). Regex-level
extraction; anything non-literal is omitted, never guessed. The analyzer
derives `domain_edges` and stores both keys in the recommendation; the
model-visible island guidance names incompatible edges BEFORE any attempt
("producer builds 3.7.0-SNAPSHOT; <root> requires 3.5.0-SNAPSHOT — record
the mismatch, do not silently alias"). Acceptance: a Bigtop-shaped fixture
(4 domains, producer 3.7 vs consumers 3.5/3.6) yields exactly 2
`version_incompatible` edges with those details.

**Task C2 (lane c2): domain truth table at the gate + sealing.**
Files: `src/sag/agent/attempt_policy.py`, `src/sag/agent/phase_gates.py`,
`src/sag/agent/verdict_finalizer.py` (+ NEW test file).
1. `UntriedIslandsRequirement.message` drops the falsified sentence "Each
   island builds independently, so one island's failure says nothing about
   the others" — replaced by graph-aware text (independent only when no
   edges; incompatible edges are named blockers).
2. `phase_gates` computes `domain_states` (Stage B invocation receipts give
   per-root attempt outcomes: a receipt whose `working_directory` is
   at/under a domain root with outcome completed/failed ⇒
   success/failed; a `version_incompatible` edge ⇒ blocked with the edge
   detail; no receipt and no blocker ⇒ untried) and seals it into the
   build/test rollups (absent when no domains).
3. Truth table (P0-F): the gate may CONFIRM or DOWNGRADE a terminal claim,
   never upgrade — when the model claims partial/failed and any surveyed
   domain is failed/blocked/untried, the validated outcome stays at the
   claim (matrix row: global artifact presence cannot refine a truthful
   2/4 partial into success). Single-domain projects (cli, tvm) keep
   today's behavior byte-identically (no `domain_states` key when the
   survey found no multi-domain decomposition).
4. `SnapshotTestStats`-style sealing: `domain_states` reaches the sealed
   verdict via the existing absent-when-None serializer pattern (extend
   the snapshot model where the build evidence seals — mirror the Stage B
   follow-up commit `ac64511`).

## Stage D — P0-C: semantic action conservation

Action contracts across the build facade. Matrix rows: Scala NO-SOURCE,
packaging skips environment tests, requested/effective visible.

### Binding notes (Stage D, bound on `1de43e2`, single lane)

Files: `src/sag/tools/build/backends.py`, `src/sag/tools/build/build_tool.py`,
`src/sag/tools/internal/gradle_tool.py`, `src/sag/tools/internal/maven_tool.py`
(+ NEW test file). Four contracts:

1. **Language-aware Gradle compile.** `GradleBackend.VERBS["compile"]` stops
   being a hardcoded `compileJava`: probe `src/main/scala|kotlin|groovy`
   under the working directory (plus subproject dirs when a settings file
   exists) via the orchestrator; run the union of the needed `compileX`
   tasks (`compileScala` when Scala sources exist, etc., `compileJava`
   always included). Facts only — no guessing beyond directory existence.
2. **NO-SOURCE cannot close a source-bearing compile.** When the executed
   Gradle compile tasks ALL report `NO-SOURCE` while the probe found
   sources in any compile language, the analysis marks the build NOT
   successful with an explicit error naming the mismatch ("scala sources
   present; executed tasks reported NO-SOURCE — the compile did not cover
   the sources").
3. **install/package never run tests.** Maven `install`/`package` argv gains
   `-DskipTests` (Bigtop ground truth: naked `mvn install` ran
   environment-dependent tests during the build phase and manufactured a
   failure); Gradle install/publish paths gain `-x test`. The `test` verb
   is the only test owner. No phase special-casing — the verb itself
   carries the contract.
4. **Visible semantic delta.** Whenever the effective action differs
   semantically from the requested verb (compile→install promotion, verb
   substitutions, added skip flags), the ToolResult output begins with
   `[build] requested '<verb>' → executing '<argv-fragment>' (<reason>)`.
   Same-name task translation (compile→compileJava alone) needs no line.
   Stage B receipts already record requested/effective/argv — this makes
   the delta model-visible BEFORE the model reasons about the result.

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
