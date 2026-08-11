# Filesystem-First JVM Build Validation Implementation Plan

> **For implementing agents:** Execute this plan task-by-task with strict TDD.
> The repository may already contain unrelated staged and unstaged work. Never
> use `git stash`, never stage by directory or with `git add -A`, and never add
> Co-Authorship metadata to a commit.

**Goal:** Replace JVM build completion based on Java-source/class-file
cardinality and arbitrary artifact existence with an independent, filesystem-
first reconciliation of required Java source obligations, whole generator/
compile/package units, final input and environment identity, and physically
verified output bytes.

**Architecture:** New pure model and reconciliation modules land first in
shadow mode without touching the current build gate. A host-owned Docker-
container evidence epoch then authorizes immutable observations across SAG run
partitions through a grow-only host index. Maven and Gradle adapters publish
typed generator, compile, and package observations plus output witnesses, while
`PhysicalValidator` independently re-derives expectations and final identity
from the filesystem. Publication follows the evidence dependency DAG; the
reconciliation is always last. Maven and Gradle cut over serially. Only after
both are authoritative does verdict v5 atomically replace the v4 live writer
and project one sealed `BuildReconciliationV1` to report, CLI, web, replay, and
evaluator surfaces.

**Tech stack:** Python >=3.10, pydantic v2 strict/frozen models, pytest, existing
container transport and host evidence-publication authority, Maven Compiler
Plugin status ledgers as bounded enrichment, and a controlled Gradle init script.

**Design authority:**
`docs/superpowers/specs/2026-08-11-filesystem-build-validation-design.md`,
frozen for this plan at SHA-256
`31f24538f4d6df8875dd8e514872ff8afaa85c3d542456a78463fbb592ba5d57`.

## Preconditions and repository boundary

The design specification is approved for implementation and records that Tasks
1–2 are complete and frozen while Task 3 has not started. The repository-
wide `docs/` rule still ignores both the approved specification and this plan,
and neither has been
explicitly staged or committed. Before Task 3 starts, or before the full
implementation workstream is committed or represented as integrated, the owner
must:

1. verify the design bytes still match the authority SHA-256 above;
2. explicitly include the design with `git add -f`;
3. explicitly include this plan with `git add -f`;
4. commit those documentation files as the design/plan baseline.

Those commands are recorded here for the future owner; **do not execute them as
part of writing this plan**:

```bash
git add -f docs/superpowers/specs/2026-08-11-filesystem-build-validation-design.md
git add -f docs/superpowers/plans/2026-08-11-filesystem-build-validation.md
git commit -m "docs: filesystem-first JVM build validation design and plan"
```

At plan-writing time, the rate-banded verdict implementation is a large,
uncommitted shared-worktree change. Tasks 1 and 2 below intentionally create
only new production and test files. Before Task 3 or any later task touches an
existing file, the rate-banded workstream must have a reviewed baseline commit,
and the implementer must re-read the current diff and ancestry rather than
assuming the plan-time worktree still exists.

Use the repository's `uv` workflow for Python commands:

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache uv run pytest <paths> -q -p no:cacheprovider
```

Do not run Docker in Tasks 1–5. Task 6 and later still use fake orchestrators
for unit/integration tests; real containers are reserved for the final live
acceptance task.

## Non-negotiable invariants

- The final filesystem is the judge. Runner exit codes and log text nominate
  candidate evidence but never replace post-hoc reads and hashes.
- One Docker container identity defines an evidence epoch. Compatible proofs
  from multiple SAG attempts and run IDs in that epoch may combine by whole
  generator/compile/package unit. The host epoch index is grow-only; deleting
  or tombstoning an admitted identity cannot shrink the live evidence set or
  improve truth.
- Expected sources and units are derived independently from the final census,
  frozen goal scope, and evaluated build model. Observations and outputs cannot
  define or shrink their own denominator.
- Every Java candidate is required, mechanically out of scope, delegated to a
  required child scope, unsupported-active, or unclassified. Unclassified is
  `UNVERIFIABLE`, never omission.
- Delegation conserves sources: every delegated parent candidate has exactly
  one path-and-hash-preserving edge into exactly one required child census.
  Child scopes form a strict ownership DAG with no cycles, multiple parents, or
  unexplained overlapping roots.
- A compile unit is current only through one whole-unit observation whose exact
  source-entry set and recomputed identity equal the final basis. Partial
  observations for one unit never Frankenstein-merge.
- Generator and package units obey the same whole-unit rule. Package identity
  closes over input compile identities and output digests, resources,
  packaging executable, plugins/tools, external dependencies, arguments,
  toolchain, packaging configuration, and requested artifacts.
- Source coverage and output integrity are independent axes. Class-file count is
  an absolute diagnostic only.
- `complete` requires exact closure. No percentage threshold turns `partial`
  into success.
- Known absence in a complete ledger is `MISSING_OR_INVALID`; unreadable or
  incomplete evidence is `UNVERIFIABLE`.
- The Physical Validator does not execute Maven, Gradle, javac, or project code.
- One reconciliation decides and displays. Consumers perform no local build
  aggregation or filesystem scans.
- Publication follows dependencies, not a global record-kind phase: independent
  scope/model/expectation and task-time environment specs predate dependent
  observations; each witness names an already-published observation; the
  close-time physical snapshot and final basis predate the reconciliation.
- `blocked` remains a controller/prerequisite state. Physical reconciliation
  uses `complete | partial | absent | unverifiable`.
- Python, native, Node, and other validators remain unchanged.

## Target file layout

New production modules:

- `src/sag/agent/jvm_build_models.py` — strict evidence schemas, canonical
  serialization, bounds, identities, and typed states.
- `src/sag/agent/jvm_build_reconciliation.py` — pure expectation derivation,
  whole-unit proof selection, output verification projection, and reconciliation.
- `src/sag/agent/evidence_epochs.py` — host-owned container-epoch index and
  complete cross-run publication snapshots.
- `src/sag/agent/jvm_build_census.py` — bounded JVM source candidate census and
  filesystem hashes.
- `src/sag/agent/jvm_build_model.py` — normalized Maven/Gradle build-model and
  final-basis construction from adapter facts.
- `src/sag/tools/internal/maven_build_evidence.py` — Maven expectation,
  observation, package, and witness adapter.
- `src/sag/tools/internal/gradle_build_evidence.py` — controlled Gradle init
  script generation and typed sidecar parsing.

New primary tests:

- `tests/test_jvm_build_models.py`
- `tests/test_jvm_build_reconciliation.py`
- `tests/test_build_evidence_epoch.py`
- `tests/test_jvm_build_census.py`
- `tests/test_jvm_build_model.py`
- `tests/test_maven_build_evidence.py`
- `tests/test_build_reconciliation_shadow.py`
- `tests/test_gradle_build_evidence.py`

Existing integration files are named under the tasks that are allowed to
modify them.

## Dependency and ownership map

```text
Task 1 strict models
  -> Task 2 pure reconciliation shadow
       -> Task 4 epoch publication
       -> Task 5 census/model/final basis

Rate-banded baseline commit
  -> Task 3 remove false v4 class/source claim

Tasks 2 + 4 + 5
  -> Task 6 Maven producer evidence
       -> Task 7 PhysicalValidator shadow integration
            -> Task 8 Maven authority cutover

Tasks 2 + 4 + 5 + 7
  -> Task 9 Gradle producer evidence
       -> Task 10 Gradle authority cutover

Tasks 8 + 10
  -> Task 11 verdict v5 and all projections
       -> Task 12 retire legacy JVM decisions and replay hardening
            -> Task 13 full and live acceptance
```

Tasks 1 and 2 are deliberately first and new-file-only. After that point,
cutover tasks are serial. Task 4 and the new-file portions of Task 5 may be
developed in parallel only after Task 1 freezes record-kind and identity names;
their integration commits remain ordered. No two workers may independently
implement synchronous and detached observation publication: both paths share
one receipt ID, schema, and writer.

---

### Task 1: Strict JVM build evidence models — new files only

**Depends on:** approved design document only.

**Files:**

- Create: `src/sag/agent/jvm_build_models.py`
- Create: `tests/test_jvm_build_models.py`

**Do not modify:** publication authority, `PhysicalValidator`, build tools,
verdict files, or any existing test.

**Required public surface:**

- Strict enums/literals for coverage, output, observation, reconciliation,
  evidence-record-set completeness, source-classification, role, language, and
  witness modes.
- Frozen, `extra="forbid"` models for every design §4 object:
  `SealedBuildEvidenceV1`, `ContainerEvidenceEpochV1`, `BuildGoalScopeV1`,
  `JvmSourceCensusV1`, `EvaluatedBuildModelV1`, `JvmBuildExpectationV1`,
  `CurrentPhysicalBasisSnapshotV1`, `FinalBuildBasisV1`,
  `GeneratorUnitObservationV1`, `CompileUnitObservationV1`,
  `PackageUnitObservationV1`, `PhysicalOutputWitnessV1`,
  `BuildEvidenceSetSnapshotV1`, `EvidenceRecordSetCompletenessV1`, and
  `BuildReconciliationV1`, including their nested records.
- Exact nested contracts for `JvmSourceCandidate`, `GeneratorUnitExpectation`,
  `CompileUnitExpectation`, `PackageUnitExpectation`, `GeneratedRootContract`,
  `ChildScopeExpectation`, `RequiredSourceEdge`, `SourceClassification`,
  `DelegationEdge`, all three final-unit basis records, root/entry witnesses,
  and selected proof references.
- `JvmBuildExpectationV1` independently carries
  `required_generator_unit_ids`, `required_compile_unit_ids`, and
  `required_package_unit_ids`; observations and artifacts cannot create or
  shrink any of those sets.
- `canonical_build_evidence_bytes(model) -> bytes`.
- `validate_build_evidence_json(model_type, raw) -> model` with duplicate-key
  rejection before JSON parsing.
- Content-addressed immutable IDs and stable logical IDs for mutable heads.
- Explicit entry-count, string/path-length, and canonical-byte bounds.
- Revalidated `model_copy(update=...)`; no unchecked pydantic copy boundary.

- [x] **Step 1: Write constructor and JSON-boundary red tests**

Pin all of the following in `tests/test_jvm_build_models.py`:

- valid minimal objects round-trip through canonical bytes;
- unknown fields fail;
- booleans fail strict integer fields;
- negative counts and non-SHA256 digests fail;
- duplicate JSON keys fail before pydantic can collapse them;
- a path that is absolute where relative is required, contains `..`, NUL, CR,
  or LF fails;
- source and output entries are unique by their typed identity;
- each delegated classification has exactly one structurally complete
  `DelegationEdge`, and non-delegated classifications cannot carry one;
- generator, compile, and package environment fields cannot be silently absent;
- package basis/observation fields bind input compile output digests, resources,
  `packaging_executable_path`/SHA256, `packaging_tool_entries`,
  `external_dependency_entries`, `packaging_args_fingerprint`,
  `packaging_toolchain_fingerprint`, `packaging_config_fingerprint`, and
  requested/observed artifacts;
- `CurrentPhysicalBasisSnapshotV1` partitions every requested member exactly
  once into current, missing, or unreadable, and rejects extra members or an
  incomplete output scan;
- `BuildEvidenceSetSnapshotV1` has exactly one
  `EvidenceRecordSetCompletenessV1` row for every completion-critical immutable
  kind, and each row's expected/observed/tombstoned IDs exactly match its typed
  object set;
- every completeness row's `observed_record_hashes` contains each observed
  record ID exactly once with its host-authorized raw SHA-256; missing, extra,
  duplicate, or mismatched ID/hash rows fail closed;
- it carries `child_censuses`, `child_expectations`, and
  `child_reconciliations` for every required child; their sealed IDs and
  reconciliation cross-links must agree before delegation can be checked;
- its record-set kinds are exactly `generator_observation`,
  `compile_observation`, `package_observation`, `output_witness`,
  `child_census`, `child_expectation`, and `child_reconciliation`; an unknown,
  duplicate, or missing kind fails closed;
- known-empty is represented only by `status=complete` with both ID sets empty;
  an absent completeness row or a bare empty object list is not known-empty;
- same immutable ID with changed semantic content cannot validate;
- `model_copy(update=...)` revalidates all bounds;
- canonical bytes are stable under input mapping order;
- future schema versions fail closed.

- [x] **Step 2: Run the red test**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache \
  uv run pytest tests/test_jvm_build_models.py -q -p no:cacheprovider
```

Expected: collection fails because `sag.agent.jvm_build_models` does not exist.

- [x] **Step 3: Implement the minimum strict models**

Keep this module pure: standard library plus pydantic only. It must not import
`PhysicalValidator`, an orchestrator, build tools, receipts, or publication
authority. Validation should normalize nothing silently; writers must supply
already-normalized values, and malformed values fail.

Use one strict base model so constructor, `model_validate`, JSON validation, and
`model_copy` enforce the same contract. Generate IDs from a documented identity
projection rather than from `created_at` or JSON insertion order.

- [x] **Step 4: Run focused and model-boundary tests**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache \
  uv run pytest tests/test_jvm_build_models.py \
  tests/test_receipt_v2_and_assessments.py -q -p no:cacheprovider
```

Expected: all pass; existing receipt models remain unchanged.

- [x] **Step 5: Review task scope before the future commit**

```bash
git diff --check -- src/sag/agent/jvm_build_models.py tests/test_jvm_build_models.py
git status --short -- src/sag/agent/jvm_build_models.py tests/test_jvm_build_models.py
```

Future commit scope must contain exactly these two files.

---

### Task 2: Pure expectation and reconciliation shadow kernel — complete/frozen

**Depends on:** Task 1.

**Files:**

- Create: `src/sag/agent/jvm_build_reconciliation.py`
- Create: `tests/test_jvm_build_reconciliation.py`

**Do not modify:** any existing production or test file.

**Required pure API:**

- `derive_expectation(census: JvmSourceCensusV1, goal_scope:
  BuildGoalScopeV1, build_model: EvaluatedBuildModelV1) ->
  JvmBuildExpectationV1` — receipts, observations, witnesses, task outcomes,
  class files, and artifact bytes are impossible inputs.
- `derive_final_basis(census: JvmSourceCensusV1, expectation:
  JvmBuildExpectationV1, build_model: EvaluatedBuildModelV1, physical:
  CurrentPhysicalBasisSnapshotV1, *, basis_revision: int) -> FinalBuildBasisV1`
  — consumes already-hashed physical facts for exactly the census/model-
  requested members; it cannot add, remove, or replace them. The host mutable-
  head writer supplies the positive revision; task observations cannot derive
  or alter it.
- Frozen pure `ReconciliationInputs` — one wrapper containing the exact epoch,
  goal scope, census, evaluated model, expectation, final basis, physical-basis
  snapshot, and typed evidence-set snapshot. It rejects mismatched epoch/ID
  cross-links at construction.
- Frozen pure `ReconciliationSelections` — the exact selected generator,
  compile, package observation/witness assignments, child census/expectation/
  reconciliation assignments, and selection conflicts used by the fold and
  digest.
- `reconcile_build(inputs: ReconciliationInputs) -> BuildReconciliationV1` — the
  typed `BuildEvidenceSetSnapshotV1` inside the wrapper is the only observation/
  witness/child input. It performs no reads, commands, publication, or
  rendering.
- `reconciliation_input_set_digest(inputs: ReconciliationInputs, selections:
  ReconciliationSelections) -> str` — covers every mutable head, complete
  immutable record-ID plus host-authorized raw-hash set, selected proof
  assignment, child reconciliation plus its authorized census/expectation IDs,
  and final physical source/config/environment/output hashes. It never derives
  record identity from reserialized model values.

- [x] **Step 1: Write the acceptance-kernel red tests**

Use small in-memory fixtures to pin:

1. `ReconciliationInputs` rejects epoch/ID cross-link drift and exposes no bare
   observation/witness/child-list arguments;
2. `ReconciliationSelections` is frozen, deterministic, and every selected ID
   enters `input_set_digest`;
3. the Task 1 typed boundary rejects an unknown, duplicate, or missing record-
   kind row; the reader's structurally valid seven-row `status=unavailable`
   projection reconciles as `unverifiable`;
4. the Task 1 typed boundary rejects a missing, duplicate, extra, or mismatched
   observed-ID/raw-hash row; changing a host raw hash in an otherwise valid
   complete row changes `input_set_digest` even when parsed model values match;
5. one Java source producing many class entries is `1/1`, not greater than 100%;
6. a legal source with no same-named class is current through its unit proof;
7. two distinct `Foo.java` paths never merge;
8. an observation omitting one required source is ineligible even when all of
   its outputs verify;
9. two half-unit observations never Frankenstein-merge;
10. compatible observations may combine across distinct units in one epoch;
11. a foreign-epoch observation is rejected at the typed snapshot/input
    boundary and therefore cannot become a selectable proof;
12. `NO-SOURCE`, skipped, and failed observations never become positive;
13. an exact `executed_success`, `current_noop`, `up_to_date`, or `from_cache`
   observation may be selected only when its recomputed identity matches;
14. a deleted or hash-mismatched output leaves source coverage current but makes
    the build incomplete;
15. a generator is current only with exact inputs/environment and a verified
    witness; a `later_compile_input` consumer also matches generated bytes;
16. a package proof becomes stale when any input compile output digest,
    resource, executable, tool/plugin, external dependency, argument,
    toolchain, packaging config, or requested artifact changes;
17. every delegated source is conserved by one path/hash edge into one child;
    a missing/duplicate handoff, cycle, multiple parent, or unexplained root
    overlap is `unverifiable`;
18. every selected child reconciliation cross-links to the independently
    authorized child census and expectation, and each parent delegation edge
    matches the child census's canonical source path and hash; a child status or
    count projection alone is insufficient;
19. unclassified source, unsupported-active language, unreadable child, or
    incomplete evidence set produces `unverifiable`;
20. no required completion obligations and no blocker folds to `complete`, with
    explicit zero counts and the Java grain later projected as not applicable;
21. a bare empty list or absent completeness row is rejected at the typed
    boundary; a structurally valid unavailable, set-mismatch, tombstoned, or
    conflict row produces `unverifiable`, never known absence;
22. required obligations plus typed `status=complete` empty/negative evidence
    produces `absent`;
23. current plus known missing produces `partial`;
24. exact generator/compile/package/child/output closure produces `complete`;
25. class count changes alone never alter source coverage or public state;
26. `complete`, `partial`, `absent`, and `unverifiable` all emit the full bounded
    count/conflict projection; no early return truncates reconciliation output.

Add direct metamorphic tests for every invariant in design §12.1. Do not use
randomized tests as the only fence; every invariant needs one named example.

- [x] **Step 2: Run the red test**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache \
  uv run pytest tests/test_jvm_build_reconciliation.py -q -p no:cacheprovider
```

Expected: collection fails because the reconciliation module does not exist.

- [x] **Step 3: Implement the pure kernel**

Select `K` by whole compile unit first, then derive current edges and sources.
Never award edge-level credit before a unit is eligible. Select at most one
positive proof per required generator, compile, or package unit. A failed
compile observation against the same final basis is always a blocking conflict;
no producer field can bypass it.

Validate delegation before recursive aggregation: require a source-preserving
bijection for every parent handoff against the sealed child census, verify the
child expectation/reconciliation cross-links, then topologically traverse the
strict child scope DAG. Never accept a child status/count projection by itself.
Count project Java sources by namespaced `(scope_id, source_id)` and never
double-count a delegated parent entry.

Require an exact observed-record-ID to host-raw-SHA bijection before selection.
Construct one `ReconciliationSelections`, compute its input-set digest from
those host raw hashes and the frozen `ReconciliationInputs`, and perform one
final status fold after all axes have emitted counts and conflicts. Do not
early-return on empty obligations, empty ledgers, or the first conflict: no
obligations/no blockers is `complete`; required obligations plus complete
known-empty evidence is `absent`; the same empty lists behind incomplete/
unavailable set authority are `unverifiable`.

Keep physical output projection independent from coverage. An empty witness is
valid only for a unit whose independently derived requirement is
`empty_permitted` or `none`.

- [x] **Step 4: Run the pure shadow suite**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache \
  uv run pytest tests/test_jvm_build_models.py \
  tests/test_jvm_build_reconciliation.py -q -p no:cacheprovider
```

Expected: all pass without Docker, project commands, or imports from the live
validator.

- [x] **Step 5: Inspect isolation**

```bash
git diff --check -- src/sag/agent/jvm_build_reconciliation.py \
  tests/test_jvm_build_reconciliation.py
git status --short
```

Future commit scope for Task 2 is exactly the two new files. If any existing
file changed, stop and separate it before proceeding.

Freeze result (2026-08-11):

- `src/sag/agent/jvm_build_models.py` SHA-256
  `d7b08e0b03208ff0c43a1cfd8b6ee78d407c3553a94377ee93f0fe03097e8b9b`;
- `src/sag/agent/jvm_build_reconciliation.py` SHA-256
  `bb8594100cb48663100cf6296532d4a6379fead870cfc6e05d2abd7e45dacfcf`;
- `tests/test_jvm_build_models.py` SHA-256
  `388e05b691a2b00f893ee59db0c4eb939529f9dea349133ed62d0f0b056626ce`;
- `tests/test_jvm_build_reconciliation.py` SHA-256
  `28b98a74139fffa918449be9498de8b384c6a598a3a2c3e5ed179ab4d72389ed`;
- Task 1 + Task 2: `302 passed`; exact Python 3.10 mypy: zero
  issues; Black, isort, py_compile, Python 3.10 grammar, forbidden-I/O purity,
  digest invariants and independent adversarial review: pass;
- no Docker, stage, commit, push, or live-gate cutover occurred. Task 3 remains
  blocked on a reviewed, committed rate-banded v4 baseline and a fresh overlap
  audit of the shared dirty worktree.

---

### Task 3: Stop the false v4 class/source claim and cardinality decision

**Depends on:** Tasks 1–2 and a reviewed, committed rate-banded v4 baseline.

**Files:**

- Modify: `src/sag/agent/module_coverage.py`
- Modify: `src/sag/verdict_rates.py`
- Modify: `src/sag/agent/verdict_finalizer.py`
- Modify: `src/sag/agent/physical_validator.py`
- Modify tests:
  - `tests/test_module_coverage_shared.py`
  - `tests/test_verdict_rates.py`
  - `tests/test_verdict_finalizer.py`
  - `tests/test_physical_validator.py`
  - `tests/test_coverage_basis.py`
  - `tests/test_build_coverage_scope.py`
  - `tests/test_build_test_verdict.py`
  - `tests/test_snapshot_surface_agreement.py`
  - `tests/test_report_honesty.py`
  - `tests/test_evaluate_golden_battery.py`
  - `tests/verdict_rate_fakes.py`

**Compatibility boundary:** v4 remains the current writer in this task. Its
`rates.build.classes` key remains structurally present but is always:

```json
{"band": "unavailable", "reason": "class count is diagnostic only"}
```

`build_evidence.compiled_classes` remains the absolute diagnostic. Do not write
`source_files` merely to manufacture a denominator, and do not bump to v5 yet.

- [ ] **Step 1: Re-read the post-rate baseline and write red tests**

First inspect rather than assuming plan-time line numbers:

```bash
git status --short
git log -3 --oneline --decorate
git diff -- src/sag/agent/module_coverage.py src/sag/verdict_rates.py \
  src/sag/agent/verdict_finalizer.py src/sag/agent/physical_validator.py
```

Pin these behaviors:

- 8,655 class artifacts and 2,489 Java files never serialize `8655/2489`;
- v4 round-trip retains `compiled_classes == 8655` while the classes grain is
  unavailable;
- the human line labels the class number as an observed diagnostic, never a
  fraction or coverage rate;
- adding arbitrary `.class` files cannot improve the legacy JVM decision;
- `_parse_single_maven_expected_artifacts` and Gradle expectation parsing do not
  assign `min_count = java_count`;
- `_verify_expected_artifacts` performs only typed path/presence checks during
  the migration and emits no `class_coverage`;
- `build_coverage_threshold` no longer participates in JVM decisions. Keep the
  configuration field readable for compatibility until legacy removal.

- [ ] **Step 2: Run the focused tests and confirm the intended failures**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache \
  uv run pytest tests/test_verdict_rates.py \
  tests/test_module_coverage_shared.py \
  tests/test_verdict_finalizer.py \
  tests/test_physical_validator.py \
  tests/test_coverage_basis.py \
  tests/test_build_coverage_scope.py \
  tests/test_build_test_verdict.py -q -p no:cacheprovider
```

Expected: failures name the old class/source fraction or threshold behavior;
unrelated rate/test semantics stay green.

- [ ] **Step 3: Implement the compatibility correction**

Remove the source-file denominator threading introduced solely for v4 class
rates. Replace the class grain with the typed unavailable payload. Retain the
absolute `compiled_classes` evidence and samples.

In legacy JVM validation, replace source-count minimum checks with bounded,
typed artifact-path presence only. This is a temporary migration fallback, not
new authority; it remains capped by the existing module scan and is deleted in
Task 12. Do not reinterpret the number of expected output directories as
expected classes.

- [ ] **Step 4: Run the focused and surface tests**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache \
  uv run pytest tests/test_verdict_rates.py \
  tests/test_module_coverage_shared.py \
  tests/test_verdict_finalizer.py \
  tests/test_physical_validator.py \
  tests/test_coverage_basis.py \
  tests/test_build_coverage_scope.py \
  tests/test_build_test_verdict.py \
  tests/test_snapshot_surface_agreement.py \
  tests/test_report_honesty.py \
  tests/test_evaluate_golden_battery.py -q -p no:cacheprovider
```

- [ ] **Step 5: Verify that only the intended baseline changed**

```bash
git diff --check
git diff --name-only
```

Stage only the explicit Task 3 paths in a future commit. Do not absorb any
remaining rate-banded or user changes accidentally.

---

### Task 4: Container evidence epoch and cross-run publication union

**Depends on:** Task 1 record-kind/identity names. May be developed alongside
the new-file portion of Task 5, but must merge first.

**Files:**

- Create: `src/sag/agent/evidence_epochs.py`
- Create: `tests/test_build_evidence_epoch.py`
- Modify: `src/sag/agent/control_events.py`
- Modify: `src/sag/agent/evidence_publications.py`
- Modify: `src/sag/agent/evidence_records.py`
- Modify: `src/sag/config/logger.py`
- Modify: `src/sag/agent/agent.py`
- Modify: `src/sag/agent/replay.py`
- Modify tests:
  - `tests/test_evidence_publications.py`
  - `tests/test_control_layer_replay.py`
  - `tests/test_evidence_replay_idempotence.py`

**Record vocabulary:**

Immutable kinds:

- `container_evidence_epoch`
- `jvm_build_expectation`
- `current_physical_basis_snapshot`
- `generator_unit_observation`
- `compile_unit_observation`
- `package_unit_observation`
- `physical_output_witness`
- `build_reconciliation`

Mutable latest-head kinds:

- `build_goal_scope`
- `jvm_source_census`
- `evaluated_build_model`
- `final_build_basis`

The reconciliation reader's completeness rows are a closed seven-kind
vocabulary: `generator_observation`, `compile_observation`,
`package_observation`, `output_witness`, `child_census`, `child_expectation`, and
`child_reconciliation`. Each maps to its exact typed object list; unknown,
duplicate, or missing rows fail closed.

The host epoch index is host authority, not a container-discovered record. It
maps one immutable Docker store identity to the complete canonical set of
admitted run partitions and their host control-stream identities. The index is
itself bounded, revisioned, sealed, and grow-only.

**Publication dependency DAG:** Do not implement one global “all bases, then all
observations, then all witnesses” phase. Enforce these edges:

```text
container epoch + authorized goal scope + mechanical build config
  -> evaluated model
container epoch + bounded final filesystem + model generated-root contracts
  -> JVM source census
goal scope + evaluated model + JVM source census
  -> expectation revision
scope/model/expectation revision + task-time environment specification
  -> dependent generator/compile/package observation
  -> witness naming that already-published observation

upstream observation + witness
  -> any dependent census/expectation revision
  -> downstream observation + witness

current model/expectation heads + current physical bytes
  -> CurrentPhysicalBasisSnapshotV1
  -> mutable FinalBuildBasisV1 head
grow-only epoch union + typed record-set completeness
  + already-published required child censuses/expectations/reconciliations
  -> BuildEvidenceSetSnapshotV1
final basis + physical snapshot + evidence-set snapshot
  -> child-before-parent BuildReconciliationV1
  -> verdict/projections
```

An observation never names a future witness. Generator/compile witnesses may
therefore predate dependent compile/package observations without creating an
identity cycle. The reconciliation is the final build-evidence publication for
its exact close-time input set.

- [ ] **Step 1: Write epoch red tests**

Pin:

- a newly observed immutable container ID mints one epoch;
- a later run attaching to the same container recovers the same epoch and sees
  the complete authorized union;
- a different container ID cannot reuse the epoch even with identical bytes and
  run IDs;
- every successor epoch-index revision has an admitted-run set identical to or
  a strict superset of its predecessor; an identical admitted set may carry
  only monotonic integrity/tombstone state and may never clear it;
- a reordered/non-canonical list, removed run, cleared tombstone, or same
  revision with changed bytes fails closed rather than creating a valid head;
- the current run remains the only writer of its own partition;
- a missing, truncated, duplicate, reordered, or unknown partition entry makes
  the union unavailable;
- deleting a previously authorized record causes an exact-set mismatch and can
  never improve truth;
- container mirror files not named by the host union are inert;
- a tombstoned run partition or record identity remains permanently expected,
  preserves its failure/conflict history, and caps reconciliation to
  `unverifiable`;
- mutation/tombstone chains remain per logical artifact, epoch-consistent, and
  cannot shrink any complete immutable candidate set;
- recovery is deterministic across process restarts;
- old per-run streams remain readable for historical replay but cannot claim an
  epoch-wide positive result.

- [ ] **Step 2: Run the red tests**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache \
  uv run pytest tests/test_build_evidence_epoch.py \
  tests/test_evidence_publications.py \
  tests/test_control_layer_replay.py -q -p no:cacheprovider
```

- [ ] **Step 3: Implement host-owned epoch indexing**

Do not scan `logs/session_*` or `/workspace/.setup_agent` to discover prior
runs. Persist one bounded, sealed host index when `EvidencePublicationAuthority`
binds to `orchestrator.evidence_store_identity()`. Register the current session
partition exactly once. A successor index may only preserve or add admitted
runs. Reads freeze one latest index head and every admitted partition snapshot
before validating bytes; a tombstone remains part of the exact expected set.

Keep write authority run-local. Add an epoch-union read handle rather than
loosening `publication.run_id == current run_id` checks globally. Existing
receipt and verdict readers that are intentionally current-run-only must retain
their behavior until explicitly migrated.

- [ ] **Step 4: Run publication, recovery, and replay tests**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache \
  uv run pytest tests/test_build_evidence_epoch.py \
  tests/test_evidence_publications.py \
  tests/test_evidence_replay_idempotence.py \
  tests/test_control_layer_replay.py -q -p no:cacheprovider
```

- [ ] **Step 5: Audit duplicated record-kind vocabularies**

Add a test asserting the publication and control-event record-kind sets agree.
Do not rely on manually synchronized literals without a fence. Add dependency-
DAG tests that reject witness-before-observation, observation-before-its
scope/model/expectation/environment spec, parent-before-child reconciliation,
and reconciliation-before-final-basis publication. The reader must also reject
a parent snapshot that has a child reconciliation but lacks the corresponding
authorized child census or expectation.

---

### Task 5: Filesystem census, evaluated build model, goal scope, and final basis

**Depends on:** Tasks 1–2. Publication integration depends on Task 4.

**Files:**

- Create: `src/sag/agent/jvm_build_census.py`
- Create: `src/sag/agent/jvm_build_model.py`
- Create: `tests/test_jvm_build_census.py`
- Create: `tests/test_jvm_build_model.py`
- Modify after the rate baseline:
  - `src/sag/agent/physical_survey.py`
  - `src/sag/tools/internal/build_preflight.py`
  - `src/sag/agent/invocation_contracts.py`
  - `src/sag/agent/agent.py`
- Modify tests:
  - `tests/test_explicit_evidence_architecture.py`
  - `tests/test_build_tool.py`
  - relevant physical-survey/preflight tests discovered with `rg`

**Boundary:** Survey/config data may propose the model; observations, receipts,
outputs, class files, and task outcomes are forbidden inputs to
`derive_expectation`. The validation target comes from initial user/request
authority, not an attempted `-pl` or task selector. `FinalBuildBasisV1` may hash
only members enumerated by the independent model and expectation through
`CurrentPhysicalBasisSnapshotV1`; physical bytes cannot add or remove members.

- [ ] **Step 1: Write census red tests**

Use a scripted filesystem fake to pin:

- one bounded checkout-wide `JvmSourceCensusV1` pass inventories at least
  `.java`, `.kt`, `.scala`, `.groovy`, and `.aj`, with output/control/vendor
  pruning only when a typed rule supports it;
- content-addressed source identities include canonical relative path and SHA256;
- identical basenames in separate packages remain distinct;
- generated roots are scanned again at close;
- task observations cannot add source candidates, while active generated roots
  declared by `GeneratedRootContract` are independently scanned at close;
- unreadable roots and symlink escapes are conflicts, not empty lists;
- `.gitignore` and naming convention alone never exclude a source;
- all candidates receive a classification or appear in
  `unclassified_source_ids`;
- the filtered Java projection reuses this census and never performs a second
  filesystem scan;
- adding an uncovered active source can only preserve or lower reconciliation.

- [ ] **Step 2: Write model and basis red tests**

Pin conventional and custom Maven/Gradle shapes:

- active/inactive profiles and variants;
- main versus test lifecycle selection;
- custom source and output roots;
- `GeneratorUnitExpectation` inputs/environment/generated-root ownership and
  both `same_round_intermediate` and `later_compile_input` contracts;
- multiple compiler executions/source sets;
- required child build islands and exact `DelegationEdge` path/hash handoffs;
- delegation conservation, strict child-scope DAG topology, one-parent
  ownership, and mechanically disjoint overlap handling;
- Java plus unsupported Kotlin/Scala/Groovy;
- package inputs close over compile-unit identity hashes and current output
  digests, resources, packaging executable and SHA256, plugin/tool members,
  external dependencies, packaging arguments, toolchain, packaging config, and
  requested artifacts;
- compiler executable, args, toolchain, classpath, dependency-unit, and upstream
  output hashes in `compile_unit_identity_hash`;
- changing generator inputs/tools/dependencies, source, config, compiler,
  classpath, dependency, upstream output, package plugin/tool/external
  dependency, or requested artifact changes the corresponding final unit
  identity even when mtime is unchanged;
- observations and task outcomes cannot enter the model or basis constructors.

- [ ] **Step 3: Run the red tests**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache \
  uv run pytest tests/test_jvm_build_census.py \
  tests/test_jvm_build_model.py -q -p no:cacheprovider
```

- [ ] **Step 4: Implement the bounded readers and pure projections**

Reuse shared path containment and survey primitives where they already have
equivalent semantics; do not call agent-facing analysis or duplicate prose
projection. Produce one `JvmSourceCensusV1`, an independently evaluated model,
and a `CurrentPhysicalBasisSnapshotV1` restricted to exactly the model-requested
source/config/generator/compiler/toolchain/classpath/resource/output/packaging
members. Hash files and ordered tree entries with SHA256. Never substitute
mtime, git SHA alone, or a partial config or package-environment fingerprint.

`derive_expectation` must emit required generator/compile/package unit IDs,
required Java edges, complete classifications, delegation edges, child scope
IDs, and unsupported-active units. Validate delegation as a conserved bijection
over canonical path and content hash and reject a non-DAG child ownership graph.
`derive_final_basis` must produce generator, compile, and package unit bases; it
must not consult any task observation or outcome.

Freeze `BuildGoalScopeV1` before the first build dispatch. A full setup request
defaults to project/domain scope already authorized by the initial request. Only
a new user-authorized scope revision may narrow it. Invocation contracts copy
the scope ID/fingerprint but cannot mutate the scope.

- [ ] **Step 5: Publish shadow heads and run focused tests**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache \
  uv run pytest tests/test_jvm_build_models.py \
  tests/test_jvm_build_reconciliation.py \
  tests/test_jvm_build_census.py \
  tests/test_jvm_build_model.py \
  tests/test_explicit_evidence_architecture.py \
  tests/test_build_tool.py -q -p no:cacheprovider
```

Publish the shadow heads in the Task 4 dependency order. At this stage no gate
reads the shadow records.

---

### Task 6: Maven generator/compile/package observations and physical witnesses

**Depends on:** Tasks 1, 4, and 5.

**Files:**

- Create: `src/sag/tools/internal/maven_build_evidence.py`
- Create: `tests/test_maven_build_evidence.py`
- Modify: `src/sag/tools/internal/maven_tool.py`
- Modify: `src/sag/agent/invocation_receipts.py`
- Modify: `src/sag/agent/job_obligations.py`
- Modify: `src/sag/tools/build/backends.py`
- Modify tests:
  - `tests/test_maven_gradle_tool_contracts.py`
  - `tests/test_receipt_v2_and_assessments.py`
  - `tests/test_job_settlement.py`
  - `tests/test_settlement_triggers.py`
  - `tests/test_maven_command_quoting.py`

**Single-writer rule:** The synchronous completion path and detached settlement
must call the same observation/witness finalizer. Do not implement separate
schemas or evidence semantics in `maven_tool.py` and `job_obligations.py`.

- [ ] **Step 1: Write adapter red tests**

Pin:

- receipt ID is allocated before dispatch and survives synchronous/detached
  completion unchanged;
- all active reactor modules and all relevant compiler executions are recorded,
  not only `compile/default-compile`;
- actual lifecycle, profiles, `-pl`, `-am`, and resume selectors are observation
  scope only and never shrink the frozen goal;
- all matching `inputFiles.lst` and `createdFiles.lst` paths are contained,
  bounded, and execution-qualified;
- those lists are auxiliary unless the adapter proves their semantics;
- complete observation source entries are captured at task entry and are not
  copied from the independently derived expectation;
- generator units bind exact schema/IDL/template/config inputs, executable,
  tool/plugin and dependency members, arguments, toolchain, and declared
  generated roots;
- generated source roots and custom output directories are represented;
- a `later_compile_input` root is witnessed by its current generator before the
  dependent compile observation, while `same_round_intermediate` stays inside
  its independently declared compiler unit;
- successful, no-op, skipped, failed, and incomplete-ledger outcomes remain
  distinct;
- package observations name only artifacts required by the frozen lifecycle and
  bind exact input compile identities/output digests, resource bytes,
  executable, resolved plugin/tool members, external dependencies, arguments,
  toolchain, packaging config, and requested artifacts;
- changing any package-environment member makes an older observation stale even
  when the requested JAR/WAR path and mtime are unchanged;
- output entries carry path, byte count, SHA256, and exclusive/shared ownership;
- a write/publication failure affects evidence integrity without rewriting the
  runner's physical exit result;
- detached settlement emits byte-identical evidence to synchronous completion.

- [ ] **Step 2: Run the red tests**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache \
  uv run pytest tests/test_maven_build_evidence.py \
  tests/test_maven_gradle_tool_contracts.py \
  tests/test_receipt_v2_and_assessments.py \
  tests/test_job_settlement.py -q -p no:cacheprovider
```

- [ ] **Step 3: Implement one Maven evidence finalizer**

Preallocate `receipt_id` using the existing receipt identity mechanism before
launch. Pass it through detached obligations. The applicable scope/model/
expectation revision and task-time environment specification must already be
published before a dependent task observation. After terminal completion,
persist the ordinary receipt, then each immutable generator/compile/package
observation followed by the witness that names it. Publish an upstream witness
before any downstream observation whose identity consumes those bytes.
Publication must follow the Task 4 DAG and never create an observation that
points to a future witness.

The adapter may inspect compiler status ledgers and build outputs, but it does
not declare completion. It emits typed conflicts when it cannot establish a
complete unit identity or ownership set.

- [ ] **Step 4: Run Maven and settlement suites**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache \
  uv run pytest tests/test_maven_build_evidence.py \
  tests/test_maven_gradle_tool_contracts.py \
  tests/test_receipt_v2_and_assessments.py \
  tests/test_job_settlement.py \
  tests/test_settlement_triggers.py \
  tests/test_maven_command_quoting.py -q -p no:cacheprovider
```

- [ ] **Step 5: Inspect receipt and obligation overlap**

Confirm one preallocated ID and one evidence finalizer are used on retry,
version-recovery, synchronous, detached-finished, and detached-vanished paths.

---

### Task 7: PhysicalValidator shadow reconciliation

**Depends on:** Tasks 2, 4, 5, and 6.

**Files:**

- Create: `tests/test_build_reconciliation_shadow.py`
- Modify: `src/sag/agent/physical_validator.py`
- Modify: `src/sag/agent/phase_gates.py`
- Modify: `src/sag/agent/evidence_state.py` only if a bounded typed fact field is
  necessary; prefer an evidence reference over duplicating the full object.
- Modify tests:
  - `tests/test_physical_validator.py`
  - `tests/test_phase_gates.py`
  - `tests/test_terminal_claim_convergence.py`

**Shadow rule:** Current production build state and gate behavior remain
unchanged in this task. The validator computes and publishes
`BuildReconciliationV1`, then exposes only its ID, status, conflicts, and bounded
diagnostic comparison beside the legacy result.

- [ ] **Step 1: Write shadow red tests**

Pin:

- the validator reads the complete epoch-authorized sets and latest heads;
- it materializes one `EvidenceRecordSetCompletenessV1` row per required kind
  and passes one sealed `BuildEvidenceSetSnapshotV1`; it never substitutes a
  bare empty list for an unavailable or unreadable ledger;
- it includes the sealed census, expectation, and reconciliation triple for
  every required child and verifies their cross-links before using any child
  status/count projection;
- strict schema, seal, revision, cross-link, and exact-set checks occur before
  selection;
- it re-runs expectation derivation and requires byte equality;
- it builds `CurrentPhysicalBasisSnapshotV1` by clean-reading exactly the
  requested members and rehashes final source/config, generator/compiler/
  packaging executables and tools, toolchains, classpath/external dependencies,
  resources, upstream outputs, requested artifacts, and selected witness files;
- it verifies each delegation path/hash directly against the authorized child
  census and topologically validates the child-scope ownership DAG before
  recursive aggregation;
- it never invokes project executables;
- phase close and evidence close call the same reconciliation entry point;
- unchanged `input_set_digest` permits byte-identical reuse;
- any relevant mutation forces a new reconciliation;
- missing/corrupt/foreign/unpublished evidence becomes shadow
  `unverifiable`, never a legacy empty success;
- known-empty is accepted only from a complete typed record-set row; tombstones,
  set mismatch, an absent row, or incomplete/unavailable reads are
  `unverifiable`;
- the legacy decision is unchanged during shadow mode;
- shadow mismatch diagnostics are bounded and never prescriptive.

- [ ] **Step 2: Run the red tests**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache \
  uv run pytest tests/test_build_reconciliation_shadow.py \
  tests/test_physical_validator.py tests/test_phase_gates.py \
  -q -p no:cacheprovider
```

- [ ] **Step 3: Implement one read-only reconciliation entry point**

Add a narrow method such as
`PhysicalValidator.reconcile_jvm_build(project_name, close_kind)` and call it
from both close paths. Keep I/O and hash collection in the validator layer;
construct one frozen `ReconciliationInputs`, and delegate all set math to the
pure kernel. Publish the physical snapshot, then the latest final-basis head,
then child-before-parent reconciliation; the parent reconciliation is last for
its exact input set. Cache only by exact input-set digest, never TTL or mtime.

- [ ] **Step 4: Run shadow and closure tests**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache \
  uv run pytest tests/test_build_reconciliation_shadow.py \
  tests/test_physical_validator.py \
  tests/test_phase_gates.py \
  tests/test_terminal_claim_convergence.py \
  tests/test_settlement_triggers.py -q -p no:cacheprovider
```

- [ ] **Step 5: Confirm no authority cutover occurred**

Existing Maven/Gradle gate fixtures must retain their pre-Task-7 result. Only
new shadow facts and references may differ.

---

### Task 8: Maven authority and module projection cutover

**Depends on:** Task 7 and Maven live-shadow acceptance fixtures.

**Files:**

- Modify: `src/sag/agent/physical_validator.py`
- Modify: `src/sag/agent/module_coverage.py`
- Modify: `src/sag/tools/module_metrics.py`
- Modify: `src/sag/agent/phase_gates.py`
- Modify: `src/sag/agent/verdict_finalizer.py` only to ingest reconciliation
  status/conflicts; do not introduce v5 yet.
- Modify tests:
  - `tests/test_physical_validator.py`
  - `tests/test_physical_validator_modules.py`
  - `tests/test_module_coverage_shared.py`
  - `tests/test_phase_gates.py`
  - `tests/test_verdict_physical_oracle.py`
  - `tests/test_domain_truth_table.py`

**Cutover boundary:** Maven module status becomes a projection of required
generator/compile/package/language units in the reconciliation. A class or JAR
count can no longer mark a Maven module built. Gradle remains on its typed
legacy path until Task 10; mixed domains cannot inherit Maven proof across the
boundary.

- [ ] **Step 1: Write Maven cutover red tests**

Pin state mapping:

- `complete` -> physical green/build complete;
- `partial` -> known incomplete/physical partial;
- `absent` -> known no-current-build evidence/physical red;
- `unverifiable` -> validator unavailable plus integrity conflict;
- a module counts built only when every required generator, compile, package,
  active-language unit and child scope is current;
- Java source coverage may be full while a package witness is missing, yielding
  partial;
- Java source coverage may also be full while a generator witness, delegated
  child handoff, or package environment member is stale, yielding non-complete;
- all visible module and Java counts are copied from the same reconciliation;
- rate bands never override non-complete reconciliation;
- red tests do not change build-source completeness.

- [ ] **Step 2: Run the red tests**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache \
  uv run pytest tests/test_physical_validator.py \
  tests/test_physical_validator_modules.py \
  tests/test_module_coverage_shared.py \
  tests/test_phase_gates.py \
  tests/test_verdict_physical_oracle.py -q -p no:cacheprovider
```

- [ ] **Step 3: Cut over Maven only**

Add an explicit build-system dispatch. For Maven, `validate_build_status` must
use the current reconciliation as authority. `module_coverage` and
`assemble_module_metrics` become projections and do not scan a second time.
Preserve class/JAR counts and sample paths only as diagnostics.

- [ ] **Step 4: Run Maven authority and cross-domain tests**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache \
  uv run pytest tests/test_physical_validator.py \
  tests/test_physical_validator_modules.py \
  tests/test_module_coverage_shared.py \
  tests/test_phase_gates.py \
  tests/test_verdict_physical_oracle.py \
  tests/test_domain_truth_table.py -q -p no:cacheprovider
```

- [ ] **Step 5: Record shadow/cutover comparison evidence**

Retain machine-readable reconciliation fixtures, not prose-only summaries. Any
Maven project that moves upward relative to shadow is a blocker until explained
by an input-set change.

---

### Task 9: Controlled Gradle generator/compile/package observations

**Depends on:** Tasks 1, 4, 5, and 7. Starts only after Maven cutover is stable.

**Files:**

- Create: `src/sag/tools/internal/gradle_build_evidence.py`
- Create: `tests/test_gradle_build_evidence.py`
- Modify: `src/sag/tools/internal/gradle_tool.py`
- Modify: `src/sag/tools/build/backends.py`
- Modify: `src/sag/tools/build/build_tool.py`
- Modify: `src/sag/agent/invocation_receipts.py`
- Modify: `src/sag/agent/job_obligations.py`
- Modify tests:
  - `tests/test_build_tool.py`
  - `tests/test_maven_gradle_tool_contracts.py`
  - `tests/test_gradle_install_publish.py`
  - `tests/test_job_settlement.py`
  - `tests/test_settlement_triggers.py`

The controlled init script should be rendered from a bounded Python template
into the control-owned evidence directory. Avoid an unconfigured package-data
asset whose wheel inclusion is not guaranteed.

- [ ] **Step 1: Write Gradle adapter red tests**

Pin:

- the init script observes supported Java `SourceSet` and compile tasks without
  modifying project build files;
- expectation roots/patterns and task-entry source observations are emitted as
  separate records;
- supported generator tasks bind their independent model contract to exact
  task-time inputs, executable/tool/plugin/dependency environment, arguments,
  toolchain, generated roots, observation, and witness;
- task path, project, source set/variant, source entries, output roots, and
  supported currentness identity are exact;
- `EXECUTED`, `UP-TO-DATE`, and `FROM-CACHE` remain distinct positive candidates;
- source-bearing `NO-SOURCE`, skipped, and failed are non-positive;
- unknown/custom compiler tasks and active unsupported languages are explicit
  `UNVERIFIABLE` inputs;
- required package tasks bind compile input identities/output digests,
  resources, executable, plugin/tool members, external dependencies, arguments,
  toolchain, packaging config, and requested artifacts; unchanged artifact path
  or mtime cannot hide an environment change;
- no private `.gradle/executionHistory/*.bin` parsing occurs;
- synchronous and detached paths use one preallocated receipt ID and finalizer;
- every output witness names an already-published observation, and an upstream
  witness predates a dependent downstream observation;
- shell quoting cannot let project/task/path content alter the control script.

- [ ] **Step 2: Run the red tests**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache \
  uv run pytest tests/test_gradle_build_evidence.py \
  tests/test_build_tool.py \
  tests/test_maven_gradle_tool_contracts.py \
  tests/test_job_settlement.py -q -p no:cacheprovider
```

- [ ] **Step 3: Implement the init-script adapter**

Write the script to a control-owned, content-addressed path and add
`--init-script` mechanically to the frozen Gradle argv. The invocation contract
must bind the effective argv including that injected control argument. The
script writes bounded sidecars; the host runner validates and publishes them.
The script never writes a verdict. The host finalizer uses the same Task 4
publication DAG as Maven and never passes bare record lists to reconciliation.

- [ ] **Step 4: Run Gradle tool and settlement tests**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache \
  uv run pytest tests/test_gradle_build_evidence.py \
  tests/test_build_tool.py \
  tests/test_maven_gradle_tool_contracts.py \
  tests/test_gradle_install_publish.py \
  tests/test_job_settlement.py \
  tests/test_settlement_triggers.py -q -p no:cacheprovider
```

- [ ] **Step 5: Run command-boundary checks**

Verify facade, retry, recovery, detached handoff, and direct-internal call paths
all inject and bind the same script. An all-`NO-SOURCE` source-bearing build must
remain a typed failure on every path.

---

### Task 10: Gradle authority and mixed-language cutover

**Depends on:** Task 9 and Gradle live-shadow acceptance fixtures.

**Files:**

- Modify: `src/sag/agent/physical_validator.py`
- Modify: `src/sag/agent/module_coverage.py`
- Modify: `src/sag/tools/module_metrics.py`
- Modify: `src/sag/agent/phase_gates.py`
- Modify tests:
  - `tests/test_physical_validator.py`
  - `tests/test_physical_validator_modules.py`
  - `tests/test_module_coverage_shared.py`
  - `tests/test_phase_gates.py`
  - `tests/test_build_tool.py`
  - `tests/test_domain_truth_table.py`

- [ ] **Step 1: Write Gradle cutover red tests**

Cover executed, up-to-date, from-cache, no-source, custom compiler, shared output
root, generated-root producer/consumer, package environment mutation, mixed
Java/Kotlin, Kotlin-only, nested multi-project, and mixed Maven+Gradle domain
cases. Assert that Java coverage remains visible while an active unsupported
language caps the containing module to `unverifiable`.

- [ ] **Step 2: Run the red tests**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache \
  uv run pytest tests/test_gradle_build_evidence.py \
  tests/test_physical_validator.py \
  tests/test_module_coverage_shared.py \
  tests/test_phase_gates.py \
  tests/test_domain_truth_table.py -q -p no:cacheprovider
```

- [ ] **Step 3: Cut over Gradle**

Use the same public reconciliation mapping as Maven. Do not add a Gradle-only
verdict branch. Module and project aggregation consume typed units and child
scopes from `BuildReconciliationV1`.

- [ ] **Step 4: Run all JVM authority tests**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache \
  uv run pytest tests/test_maven_build_evidence.py \
  tests/test_gradle_build_evidence.py \
  tests/test_build_reconciliation_shadow.py \
  tests/test_physical_validator.py \
  tests/test_physical_validator_modules.py \
  tests/test_module_coverage_shared.py \
  tests/test_phase_gates.py \
  tests/test_domain_truth_table.py -q -p no:cacheprovider
```

---

### Task 11: Atomic verdict v5 and zero-math consumer projection

**Depends on:** Maven and Gradle authority cutovers, Tasks 8 and 10.

**Files:**

- Modify: `src/sag/agent/verdict_finalizer.py`
- Modify: `src/sag/verdict_rates.py`
- Modify: `src/sag/agent/module_coverage.py`
- Modify: `src/sag/tools/report_tool.py`
- Modify: `src/sag/main.py`
- Modify: `src/sag/web/models.py`
- Modify: `src/sag/web/session_registry.py`
- Modify: `scripts/evaluate_golden_battery.py`
- Modify: `src/sag/agent/replay.py`
- Modify tests:
  - `tests/test_verdict_finalizer.py`
  - `tests/test_verdict_rates.py`
  - `tests/test_verdict_live_authority.py`
  - `tests/test_snapshot_surface_agreement.py`
  - `tests/test_report_honesty.py`
  - `tests/test_cli_report_verdict_mirror.py`
  - `tests/test_cli_project_exit_codes.py`
  - `tests/test_evaluate_golden_battery.py`
  - `tests/test_control_layer_replay.py`
  - `webui/src/api/types.test.ts`
  - `webui/src/api/types.ts`

**Atomic schema rule:** v5 is the first live writer of
`rates.build.java_sources`. V4 becomes read-only historical at the same commit.
No intermediate writer may emit v5 without a sealed reconciliation or emit v4
with a Java-source claim.

- [ ] **Step 1: Write v5 red tests**

Pin:

- v5 build rates contain exactly `modules` and `java_sources`;
- `rates.build.classes` is forbidden in v5;
- no required Java inputs serialize an explicit
  `{"band":"not_applicable","reason":"no required Java inputs"}`;
- `compiled_class_artifacts` and physical output counts remain absolute detail;
- the legacy verdict word still derives only from build-modules and test-cases
  bands, then receives the reconciliation conflict/status cap;
- any reconciliation state other than `complete` cannot yield success;
- v3/v4 fixtures load read-only; a collected v4 class/source ratio is labelled
  `legacy class/source ratio` and never reinterpreted;
- the live reader rejects v3/v4 as current after v5 cutover;
- report, CLI, web, evaluator, and replay carry the exact serialized rates and
  reconciliation projection without recomputation;
- the rendered headline is:

```text
Build: modules 38/52 (half) · Java inputs 2589/2589 (fully)
```

The Java headline is the namespaced union of local and recursively required
child sources. A delegated parent entry is neither excluded nor double-counted;
details retain local/delegated/project counts from the reconciliation.

- [ ] **Step 2: Run the red Python and TypeScript tests**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache \
  uv run pytest tests/test_verdict_finalizer.py \
  tests/test_verdict_rates.py \
  tests/test_verdict_live_authority.py \
  tests/test_snapshot_surface_agreement.py \
  tests/test_report_honesty.py \
  tests/test_cli_report_verdict_mirror.py \
  tests/test_evaluate_golden_battery.py \
  tests/test_control_layer_replay.py -q -p no:cacheprovider

npm --prefix webui test -- src/api/types.test.ts
```

- [ ] **Step 3: Implement the atomic v5 writer/readers**

`VerdictFinalizer` reads one final `BuildReconciliationV1` and copies its module
and Java-source counts. `module_coverage` becomes a compatibility projection,
not a scanner. Every other consumer copies the v5 block or renders it.

Do not add a fallback that recomputes Java coverage from `compiled_classes`,
source census totals, module counts, or raw output roots.

- [ ] **Step 4: Run all projection agreement tests**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache \
  uv run pytest tests/test_verdict_finalizer.py \
  tests/test_verdict_rates.py \
  tests/test_verdict_live_authority.py \
  tests/test_snapshot_surface_agreement.py \
  tests/test_report_honesty.py \
  tests/test_cli_report_verdict_mirror.py \
  tests/test_cli_project_exit_codes.py \
  tests/test_evaluate_golden_battery.py \
  tests/test_control_layer_replay.py -q -p no:cacheprovider

npm --prefix webui test -- src/api/types.test.ts
```

- [ ] **Step 5: Verify schema history direction**

Confirm v3/v4 validation never publishes, upgrades, rewrites, or grants current
authority. V5 malformed/missing/unpublished snapshots fail closed.

---

### Task 12: Retire legacy JVM decisions and harden offline replay

**Depends on:** Task 11.

**Files:**

- Modify: `src/sag/agent/physical_validator.py`
- Modify: `src/sag/agent/module_coverage.py`
- Modify: `src/sag/tools/module_metrics.py`
- Modify: `src/sag/agent/react_engine.py` if it still treats artifact count as
  JVM physical progress/completion rather than a diagnostic.
- Modify: `src/sag/tools/internal/maven_tool.py` only to remove superseded
  completion fallbacks, not producer evidence.
- Modify: `src/sag/tools/internal/gradle_tool.py` on the same basis.
- Modify: `src/sag/agent/replay.py`
- Modify tests:
  - `tests/test_physical_validator.py`
  - `tests/test_coverage_basis.py`
  - `tests/test_build_coverage_scope.py`
  - `tests/test_build_test_verdict.py`
  - `tests/test_control_layer_replay.py`
  - `tests/test_replay_native_contract.py`
  - `tests/test_verdict_physical_oracle.py`

- [ ] **Step 1: Write deletion and negative replay tests**

Pin that none of these can produce positive JVM completion:

- arbitrary class/JAR existence;
- Maven status directory or fixed `default-compile/createdFiles.lst` existence;
- newest-class mtime;
- same-name Java/class guessing;
- `class_count > 0` module inference;
- checked-in/baseline artifacts;
- build-tool `SUCCESS` text without sealed proof;
- container-mirror-only or foreign-epoch evidence;
- missing or deleted failed observations, a shrunken epoch index, a tombstoned
  admitted identity, or an untyped bare-empty record list;
- historical v4 class/source metrics.

Also pin that class/JAR samples and absolute counts remain available for
forensics and progress diagnostics without entering truth.

- [ ] **Step 2: Run the red tests**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache \
  uv run pytest tests/test_physical_validator.py \
  tests/test_coverage_basis.py \
  tests/test_build_coverage_scope.py \
  tests/test_build_test_verdict.py \
  tests/test_control_layer_replay.py \
  tests/test_replay_native_contract.py \
  tests/test_verdict_physical_oracle.py -q -p no:cacheprovider
```

- [ ] **Step 3: Delete superseded decision branches**

Remove:

- source-count-derived class expectations and coverage;
- `build_coverage_threshold` reads from JVM validation;
- class/JAR/fingerprint success fallbacks;
- newest-class recency authority;
- `validate_missing_classes` as a build decision;
- module success inferred only from class count;
- any report/replay reconstruction from raw target/build directories.

Keep separately named diagnostic scan helpers only when a live consumer still
needs them. Update docstrings so no diagnostic claims validity.

- [ ] **Step 4: Run the complete non-live JVM and replay suite**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache \
  uv run pytest tests/test_jvm_build_models.py \
  tests/test_jvm_build_reconciliation.py \
  tests/test_build_evidence_epoch.py \
  tests/test_jvm_build_census.py \
  tests/test_jvm_build_model.py \
  tests/test_maven_build_evidence.py \
  tests/test_gradle_build_evidence.py \
  tests/test_build_reconciliation_shadow.py \
  tests/test_physical_validator.py \
  tests/test_physical_validator_modules.py \
  tests/test_module_coverage_shared.py \
  tests/test_phase_gates.py \
  tests/test_verdict_finalizer.py \
  tests/test_verdict_live_authority.py \
  tests/test_control_layer_replay.py \
  tests/test_replay_native_contract.py -q -p no:cacheprovider
```

---

### Task 13: Full verification, packaging, and live acceptance

**Depends on:** all prior tasks.

**Files:**

- Modify only if a test exposes a real defect in an earlier task.
- Create retained live evidence under an explicitly approved report/artifact
  path following repository convention.

- [ ] **Step 1: Run formatting/static checks on changed Python files**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache uv run ruff check \
  src/sag/agent/jvm_build_models.py \
  src/sag/agent/jvm_build_reconciliation.py \
  src/sag/agent/evidence_epochs.py \
  src/sag/agent/jvm_build_census.py \
  src/sag/agent/jvm_build_model.py \
  src/sag/tools/internal/maven_build_evidence.py \
  src/sag/tools/internal/gradle_build_evidence.py
```

- [ ] **Step 2: Run the complete Python suite**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache \
  uv run pytest -q -p no:cacheprovider
```

Expected: zero new failures. If dependency resolution for an isolated packaging
smoke cannot fetch `hatchling`, report it separately as environment/network
evidence; do not classify it as a code regression without reproducing outside
the isolated-build fetch.

- [ ] **Step 3: Run web tests and build**

```bash
npm --prefix webui test
npm --prefix webui run build
```

- [ ] **Step 4: Run packaging/import smoke**

```bash
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache uv build
env UV_CACHE_DIR=/tmp/setup-agent-fs-build-uv-cache \
  uv run python -c "from sag.agent.jvm_build_reconciliation import reconcile_build"
```

- [ ] **Step 5: Run the design §12.2 live acceptance matrix**

Use newly created containers and retain exact machine-readable evidence for at
least:

1. conventional multi-module Maven;
2. Maven generated sources or custom compiler execution, including a current
   generator witness before its later compile consumer;
3. multi-project Gradle with executed and up-to-date/from-cache tasks;
4. mixed-language Gradle proving unsupported-language caps;
5. two SAG attempts whose compatible whole units complete one container epoch;
6. a final Java-source mutation invalidating the earlier proof;
7. cross-process/cross-run reuse in the same container paired with a foreign-
   container negative control;
8. shared output-root units plus a requested package artifact whose complete
   packaging environment is captured;
9. parent/child build islands proving source-handoff conservation and one
   negative DAG/ownership control;
10. an epoch-index successor plus removal/tombstone negative controls proving
    grow-only membership.

For every run retain:

- container epoch and goal scope;
- source census, evaluated model, expectation, current physical-basis snapshot,
  and final basis;
- the typed evidence-set snapshot and every record-set completeness row;
- selected generator/compile/package observations and output witnesses;
- delegation edges plus every selected child's sealed census, expectation, and
  reconciliation;
- final reconciliation and input-set digest;
- verdict v5 rates;
- report/CLI/web/evaluator projections.

A prose-only final report is not acceptance evidence.

- [ ] **Step 6: Run explicit mutation and monotonicity proofs**

For retained fixtures or live containers, demonstrate:

- add one uncovered Java source;
- add arbitrary class files;
- delete/change a sealed output;
- preserve outputs with `UP-TO-DATE`/`FROM-CACHE`;
- split compatible whole units across attempts;
- attempt an illegal half-unit merge;
- reorder runs inside one epoch;
- copy bytes to a foreign epoch;
- activate a profile adding obligations;
- make a census/model/witness read unavailable;
- change an upstream dependency output;
- change a generator input/tool/dependency and verify its later consumer becomes
  stale;
- change a packaging plugin/tool, external dependency, resource, argument,
  toolchain, config, requested artifact, or compile-output digest without
  changing the artifact path;
- break one delegation path/hash handoff, introduce a child cycle/multiple
  parent/overlap, and prove no source disappears from the obligation graph;
- remove or cross-link-mismatch one child census/expectation while retaining a
  green child reconciliation projection, and prove the parent becomes
  `unverifiable`;
- replace a typed complete-empty evidence set with an absent row/bare empty
  list, and attempt to shrink or tombstone the epoch index;
- mutate after phase reconciliation and before evidence close.

Each change must preserve or lower truth exactly as design §12.1 specifies.

- [ ] **Step 7: Final scope and history audit**

```bash
git diff --check
git status --short
git log --oneline --decorate -12
```

Confirm:

- no unrelated user changes entered an implementation commit;
- no commit contains Co-Authorship metadata;
- the approved design and this plan are explicitly tracked despite `docs/`
  ignore rules;
- v4 is historical/read-only and v5 is the only live writer;
- every consumer reads the same sealed reconciliation projection;
- no class/source fraction or hidden JVM completion threshold remains.

## Serial cutover gate checklist

Do not advance to the next authority stage unless every item for the current
stage is true:

1. red tests failed for the intended old behavior before implementation;
2. focused tests pass after implementation;
3. schema/model boundaries are constructor-, JSON-, copy-, persistence-, and
   replay-tested;
4. missing/unreadable evidence lowers or preserves truth;
5. synchronous and detached paths publish the same evidence contract;
6. shadow reconciliation is retained and compared before a build-system cutover;
7. no downstream consumer recomputes coverage;
8. `git diff --name-only` matches the task's declared file ownership;
9. the worktree contains no staged unrelated changes;
10. epoch membership is grow-only and complete-set reads stay typed;
11. generator/package environments and delegation DAG invariants are closed;
12. publication follows the dependency DAG and reconciliation is last;
13. the task's future commit message contains no Co-Authorship trailer.
