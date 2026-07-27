# SAG v2 Plan 6 — Build Contract Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `specs/2026-07-26-build-contract-loop-design.md` (second
design review passed 2026-07-26): bounded document map with typed claims,
pre-dispatch invocation contracts frozen from model intents, immutable
receipts with append-only assessments, causal claim graph, material-progress
retry law, and the contract-ridden native affordance.

**Architecture:** staged per spec §9 (Stage 0 → F); every stage lands via
reviewed worktree lanes with negative controls passing before the next stage
starts.

**Tech Stack:** Python 3.12, pytest, existing fake-orchestrator patterns.

## Global Constraints

- NEVER use `git stash` in lanes. Absent facts = absent keys. Recorded
  replay fixtures stay green WITHOUT editing them. No project-name policy.
- Commit messages carry no Co-Authored-By trailer.
- Spec §7 Category-3 boundary is binding on every rendering change.
- Second-review binding notes (a–e) folded into their stages below.

## Review binding notes

- **(a) Event groups:** claim transitions + assessment + invalidation commit
  as events sharing a `group_id` with a terminal `group_commit` record;
  replay treats an uncommitted group as absent.
- **(b) Per-testcase outcomes:** receipt v2 carries bounded per-node
  `{node_id, status, reason}` (cap 50 nodes, dominant-truncation recorded).
- **(c) Unverified-edge resolution:** consumer dispatch stays locked while
  unverified; a producer's own production receipt (physical artifact
  version) or bounded metadata probe resolves the edge; resolution to
  `version_incompatible` seals the consumer blocked.
- **(d) `mark_semantic_failure` migration:** replaced by append-only
  `ReceiptAssessment`; the gradle NO-SOURCE downgrade, phase-gate domain
  states and the verifier migrate in the same stage; Plan 5 anchors must
  stay green throughout.
- **(e) bash scope:** bash remains a model-visible probe tool; its output
  can never mint a `ClaimRecord`, satisfy a contract precondition, or count
  as a capability observation.

---

## Stage 0 — Evidence prerequisite (receipt v2 + append-only assessments)

Spec §9.1. Two lanes.

**Task 0.1 (lane z1): receipt schema v2 + assessment records.**
Files: `src/sag/agent/invocation_receipts.py`, NEW
`src/sag/agent/evidence_assessments.py`, call sites
`maven_tool.py`/`gradle_tool.py`/`python_tool.py`, NEW tests.
- Receipt v2 (schema_version 2): adds `target_sha`,
  `survey_fingerprint`/`config_fingerprint` (from the existing survey/run
  pin sources), `domain_id` (nearest surveyed domain root, absent when
  none), `actual_cwd`, `compliance: "exact"` (constant until Stage B),
  `toolchain_fingerprint` (resolved runner executable path + version
  line), `output_content_hash`, and bounded per-testcase outcomes per
  note (b) parsed from the invocation's own report delta. v1 keys keep
  their exact names/shapes (existing consumers unchanged).
- `evidence_assessments.py`: `ReceiptAssessment` +
  `ControlAssessment` dataclasses and atomic append-only writers
  (`/workspace/.setup_agent/evidence_assessments/<assessment_id>.json`),
  idempotent by `(receipt_or_event_id, typed_code, fingerprints)` — a
  rewrite attempt of an existing id with different content is an error.
- DELETE `mark_semantic_failure`; the gradle NO-SOURCE path writes a
  `ReceiptAssessment(typed_code="compile_no_source_mismatch")` instead.
  Receipts are finalized once; a finalized receipt file is never rewritten.
- Pre-dispatch facade rejections that today return only a ToolResult
  (pytest args rejected, native smoke unavailable) additionally write a
  `ControlAssessment` (stage="precondition", typed code = error_code).

**Task 0.2 (lane z2): consumers + replay/idempotence.**
Files: `src/sag/agent/phase_gates.py`, `src/sag/agent/physical_validator.py`
(receipt readers), `scripts/verify_native_test_policy.py`, NEW tests.
- Domain-state derivation: a receipt is semantically failed when EITHER its
  own `outcome=="failed"` OR a `ReceiptAssessment` with a failure-class
  typed code exists for it (assessments win over raw exit).
- Receipt readers accept v1 and v2 (version-gated, no silent coercion).
- Persistence failure of a receipt OR assessment blocks evidence closure
  (extend the existing fail-closed path).
- Replay tests: same receipts + assessments ⇒ byte-identical derived state;
  partially-written assessment (temp file present, final absent) ⇒ treated
  as absent; double-ingestion ⇒ single state transition.
- Verifier: receipt-v2 aware; new assertion `receipts.immutable` (no
  receipt file whose content hash changed across the session's own
  events); Plan 5 profiles must keep passing on the recorded Plan 5
  sessions (regression check inside the lane).

Stage exit: full suite + verifier on all four recorded Plan 5 battery
sessions unchanged (cli 5/5, bigtop 9/9, tvm 9/9 ×2).

---

## Stage A — Bounded survey (DocumentMap + PolicyClaims + DomainFacts)

Spec §C1/C2. Bound on `9d3636c`. Three lanes; cross-lane contracts EXACT.

**Shared contracts.**
- `DocumentMapEntry` (spec §C1 fields verbatim; `entry_id =
  "doc-" + sha256(path)[:12]`; `section_index` = list of
  `{section_id, kind, title_or_key, start_line, end_line}`).
- Budgets (constants, copied into the run pin): `MAX_FILES=400`,
  `MAX_TOTAL_BYTES=8_000_000`, `MAX_FILE_BYTES=512_000`, `MAX_DEPTH=6`.
- Persistence: `/workspace/.setup_agent/document_map.json` (map +
  `document_map_fingerprint` = sha256 of sorted `entry_id:source_hash`
  pairs + `partial_map` conflict list); claims at
  `/workspace/.setup_agent/claims/<claim_id>.json`
  (`claim_id = "<kind>-" + sha256(canonical source_ref)[:12]`).
- Claim union per spec §C1: `source_class` discriminates; wrong-variant
  `source_ref` is schema-invalid (validation error, never coerced).

**Task A1 (lane a1): document map.** NEW `src/sag/agent/document_map.py`
(+ tests). Bounded enumeration under the verified checkout only (realpath
containment; symlink escapes/binaries/generated/vendored trees/over-budget
→ excluded + `partial_map` entries). Kind detection by extension+content;
section indexing: markdown headings + fenced command blocks; YAML
top-level keys and job/step paths; XML tag paths (depth ≤4); TOML tables;
CMake `set()/option()` lines; shell variable assignments and command
lines. One in-container `find` for enumeration + bounded `cat` per
indexed file. Deterministic output ordering.

**Task A2 (lane a2): claim records + extractors.** NEW
`src/sag/agent/claim_records.py` (+ tests). The union types with
per-variant validation; deterministic extractors (input: DocumentMapEntry
+ file text) producing `PolicyClaim`s for: tool version constraints
(maven enforcer / README prose patterns with explicit version literals),
lifecycle commands (fenced blocks and CI `run:` steps that invoke
mvn/gradle/pip/pytest/cmake — argv + cwd preserved verbatim),
dependency pins (`pip install` / requirements lines / Docker RUN),
env/CMake definitions (`CMAKE_ARGS`, `set(USE_X ...)`, `option()`).
Applicability record `{domain?, os?, arch?, workflow_job?, goal?}` from
the entry's context; equal-applicability duplicates with differing
typed_value → both recorded + one conflict record. Headings alone extract
nothing. `UntrustedDocInterpretation` type exists but has no path into
claims (enforced by construction + test).

**Task A3 (lane a3): DomainFacts projection + Category-3 boundary.**
Files: `src/sag/agent/physical_survey.py`,
`src/sag/tools/internal/project_analyzer.py` (+ tests). Emit `DomainFacts`
(spec §C2 shape) alongside the existing keys: `role`/`environment`
default `"unknown"` (never guessed), `capability_state` from existing
native probes where present, `documented_actions` = claim IDs matched to
the domain by applicability (consume lane a2's claim files by documented
schema; hand-written fixtures in tests), `fact_epoch` = monotonic int
starting 1, `open_conflicts` from existing conflict sources +
`partial_map`. Existing `build_domains`/`domain_edges` keys unchanged;
edges gain `edge_id = "edge-" + sha256(consumer+producer+coordinate)[:12]`
and `support_claim_ids` (absent when none). Rendering constraint tests:
analyze output / phase intro / handoff contain coordinates, constraints
and open conflicts only — no goal, no recommended call, no probe sequence
(Category-3 boundary assertions, spec §6 row 1).

Stage exit: negative controls (symlink escape, oversized/binary/vendored,
headings-only markdown, malicious text → no executable claim), full suite,
Plan 5 profiles unchanged on recorded sessions.

## Stage B — Execution binding (ActionIntent → InvocationContract)

Spec §C3 + edge execution law. Bound on `9966311`. Two lanes.

**Binding decision (envelope ordering).** The engine emits the
action_envelope BEFORE tool execution; materialized argv exists only inside
the build facade. Therefore: the contract is frozen inside `build_tool`
AFTER envelope emission but strictly BEFORE physical dispatch, records the
`envelope_id`, and the ToolResult metadata + invocation receipt carry
`contract_id`/`contract_hash`. The verifier walks
envelope → contract (by envelope_id) → receipt (by contract_id); the
envelope hash formula is unchanged (byte-compat).

**Contract v1 fields (subset of spec §C3, absent-when-unknown):**
`schema_version=1, contract_id ("ic-"+sha256(envelope_id+argv)[:12]),
contract_hash (sha256 of the canonical payload sans hash), envelope_id,
target_sha, config_fingerprint, document_map_fingerprint, fact_epoch,
domain_id, intent_source ("model"|"controller"), requested_call
{tool, params}, effective_action, expected_cwd, expected_argv,
blocking_conflict_ids, predecessor_contract_id?` — persisted atomically at
`/workspace/.setup_agent/invocation_contracts/<contract_id>.json`.
`expected_observations`/`direct_falsifiers`/`supersedes` arrive with
Stage C.

**Task B1 (lane b1): contract module + facade freeze.**
NEW `src/sag/agent/invocation_contracts.py` (+ tests); integrate in
`src/sag/tools/build/build_tool.py`: after the backend materializes the
effective action/argv (dry — the backends already compute both before
running), freeze + persist the contract, then dispatch; persistence
failure ⇒ refuse dispatch (fail closed, named error). The maven/gradle/
python receipt writers receive `contract_id`/`contract_hash` and compute
`compliance`: "exact" (actual argv == expected), "equivalent" (recorded
normalization only), "deviated" otherwise.

**Task B2 (lane b2): edge execution law + chain verification.**
Files: `src/sag/tools/build/build_tool.py` refusal path (coordinate with
b1 via disjoint hunks: b2 owns the PRE-materialization edge check),
`scripts/verify_native_test_policy.py`, tests.
- Before materialization: read `domain_edges`; a build/test dispatch whose
  working_directory falls under a consumer root of a
  `version_incompatible` edge is REFUSED (completed_failure,
  error_code=DOMAIN_EDGE_BLOCKED, the edge detail verbatim, no runner
  invocation, ControlAssessment written); an `unverified` edge consumer is
  refused with error_code=DOMAIN_EDGE_UNVERIFIED naming the producer that
  must build first (resolution machinery lands in Stage C).
- Verifier: new assertion family `contracts.chain` — every runner receipt
  with a `contract_id` has a persisted contract whose `envelope_id` exists
  in the events and whose `contract_hash` recomputes; receipts without
  contracts allowed only for pre-Stage-B sessions (version-gated).
Stage exit: full suite; Plan 5 profiles unchanged; negative controls
(blocked consumer never dispatches; contract persistence failure blocks
dispatch; hash mismatch detected by verifier).

## Stage C — Causal loop (ClaimGraph + assessments + retrieval + repair)

Spec §C5/C6. Claim transitions with AND/OR support sets, cycle rejection,
event groups per note (a); mismatch taxonomy (unknown/blocked/stale vs
contradicted via direct falsifiers only); typed targeted retrieval over the
Stage A map; `RepairContract` → accepted/modified `ActionIntent` →
Stage B freeze path.

## Stage D — Retry authority

Spec §C7. Retry identity vector, material-delta validation, transient/flaky
budget, poll/resume exemption; controller-signed token validated by the
facade against authoritative LoopMemory.

## Stage E — Native affordance

Spec §C8. `build(action="native", features, definitions)` behind contract
freeze only; allowlists; platform resolver; capability probes close the
receipt; TVM S0→S3 battery per spec §5.

## Stage F — Proof

Spec §6 in full: architecture/causal-safety assertions, untrusted-input
negative controls, three anchors, metamorphic renamed fixtures,
held-out regressions (pyyaml, httpcomponents-client, all Plan 5 anchors).
