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

Spec §C1/C2. Bind concretely at launch on Stage 0's merged HEAD. Scope:
deterministic discovery with pinned budgets (files/bytes/depth in the run
pin), section-level indexes, typed claim union with per-variant source_ref
validation, applicability matching, equal-applicability conflicts,
`partial_map` conflict on any exclusion, DomainFacts projection (facts
only — documented_actions are claim IDs), edge law data (`edge_id`,
support claims, revision invalidation). Negative controls: symlink escape,
oversized/binary/vendored input, headings-only markdown stays unextracted,
malicious text cannot become executable policy.

## Stage B — Execution binding (ActionIntent → InvocationContract)

Spec §C3 + edge execution law. The native tool-call from the model IS the
ActionIntent (no protocol change); the facade freezes fingerprints,
validates against edges/gates/containment, materializes effective action +
argv WITHOUT dispatch, persists + hashes the contract, binds envelope, then
dispatches. Documented-command normalization keeps original + normalized +
equivalence proof. Consumer dispatch locked on unverified edges per note
(c). Receipt v2 gains real `contract_id`/`contract_hash`/`compliance`.

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
