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

Spec §C5/C6 + binding note (a). Bound on `8eaa414`. Three lanes.

**Shared contracts (EXACT).**
- Claim transitions are control events, kind `claim_transition`, grouped:
  every event in a group carries `group_id`
  (`"grp-"+sha256(trigger id)[:12]`); the group ends with kind
  `claim_transition` payload `{group_id, terminal: true}` (the
  group-commit record). Replay treats an uncommitted group as absent.
- Current graph state materializes at
  `/workspace/.setup_agent/claim_graph.json` (atomic rewrite, rebuildable
  from events; the events are the truth).
- `ReceiptAssessment.typed_code` vocabulary for the assessor (extensible
  set, these are the Stage C base): `expectation_met`,
  `no_dispatch`, `transient_network`, `timeout`, `permission_denied`,
  `precondition_unmet`, `stale_fingerprint`, `deviated_receipt`,
  `falsifier_<predicate_id>` (contradiction), `capability_absent_<name>`.
- `RepairContract` persisted at
  `/workspace/.setup_agent/repair_contracts/<repair_id>.json`
  (`repair_id = "rep-"+sha256(trigger_assessment_id)[:12]`), fields per
  spec §C6 verbatim; `proposed_public_call = {tool, params}` only.

**Task C1 (lane c1): ClaimGraph.** NEW `src/sag/agent/claim_graph.py`
(+ tests); register `claim_transition` in `control_events.py` (kinds +
payload model `{claim_id, from_status, to_status, cause_assessment_id?,
group_id, terminal?}`; hash-stable absent-key pattern as before).
Graph API: `load(events, claim_files)`, `transition(claim_id, to,
cause)`, support edges with `{"all_of": [...]}` / `{"any_of": [...]}`,
cycle rejection at edge insert, `invalidate_dependents(claim_id)` —
downstream flips to `unknown` ONLY when its complete support is lost
(alternate any_of path keeps it alive), epoch checks (a transition citing
a stale fact_epoch is refused). Deterministic replay test: same events ⇒
same graph; uncommitted group ⇒ ignored.

**Task C2 (lane c2): assessor.** Files:
`src/sag/agent/evidence_assessments.py` (assessor function),
`src/sag/agent/invocation_contracts.py` + `src/sag/tools/build/build_tool.py`
(freeze gains `expected_observations` + `direct_falsifiers`), tests.
- Freeze additions (minimal typed set): for build/compile/package/install
  contracts `expected_observations=["artifact_or_report_delta"]`; for
  test contracts `expected_observations=["report_delta"]`;
  `direct_falsifiers=[{"predicate_id": "empty_delta_despite_success",
  "kind": "delta_empty_on_exit0"}]`.
- `assess_receipt(contract, receipt, *, current_fingerprints) ->
  ReceiptAssessment`: taxonomy per spec §C5 — no dispatch/timeout/
  permission ⇒ blocked-class codes; fingerprint mismatch ⇒
  `stale_fingerprint`; `compliance=="deviated"` ⇒ `deviated_receipt`
  (never a contradiction); exact/equivalent + fresh + falsifier predicate
  true ⇒ `falsifier_<id>`; success path ⇒ `expectation_met`. Capability:
  a per-testcase skip whose reason matches a named capability pattern
  already carried by the smoke claim ⇒ `capability_absent_<name>`
  (llvm/cuda from the existing skip-reason facts — pattern data, not
  project names).
- Wire: after each receipt lands in build_tool's dispatch path, run the
  assessor and persist the assessment (idempotent).

**Task C3 (lane c3): retrieval + RepairContract.** NEW
`src/sag/agent/repair_contracts.py`, `src/sag/agent/react_engine.py`
(surfacing + acceptance detection), tests.
- Retriever: `retrieve_for(typed_code, *, document_map, domain_id,
  applicability) -> list[entry sections]` — bounded (≤5 entries), selects
  by kind/ecosystem tags (e.g. `capability_absent_*` ⇒ CI workflows +
  CMake + install docs; dependency codes ⇒ metadata/Docker), runs the
  Stage A extractors on the indexed sections only, records new claims +
  conflicts, returns `unknown` (empty) when nothing applicable.
- RepairContract builder: from a failure/capability assessment + newly
  retrieved claims, propose ONE public call (e.g. the documented
  lifecycle argv for the domain, or `build(action='deps'/'test')
  variants); `supporting_claim_ids` mandatory — no claims, no proposal
  (emit `unknown` instead, spec hard rule).
- Engine surfacing: when a repair proposal exists for the latest
  assessment, append a bounded evidence-triggered block to the next
  observation: `[repair] <typed code>: proposed <tool>(<params>) —
  provenance <claim ids>; accept by calling it, or state why not.` (This
  is reactive, Category-3 compatible.)
- Acceptance detection: next model call with tool+params equal to a live
  proposal ⇒ action context `intent_source="accepted_repair"` +
  `repair_id` recorded on the contract (extend the b1 thread-through).
Stage exit: full suite; Plan 5 profiles unchanged; negative controls
(no claims ⇒ no proposal; deviated receipt cannot contradict; uncommitted
group invisible; cycle rejected).

## Stage D — Retry authority

Spec §C7. Bound on `4c8d65a`. Single lane.

**Retry identity (EXACT):** `retry_key = sha256(canonical({target_sha,
domain_id?, normalized_action: {tool, verb, argv_tokens_sorted:false —
tokens in order}, typed_code, environment_fingerprint?}))[:16]`, computed
from the FAILED dispatch's contract + its ReceiptAssessment (or
ControlAssessment for receiptless refusals).

**Task D1 (lane d1):** NEW `src/sag/agent/retry_authority.py` (+ tests);
integrate: `src/sag/agent/react_engine.py` (controller signs), 
`src/sag/tools/build/build_tool.py` (facade validates).
1. After each failure-class assessment the controller records the
   `retry_key` in the authoritative LoopMemory-adjacent store
   (`/workspace/.setup_agent/retry_ledger.json`, atomic rewrite:
   `{retry_key: {count, last_contract_id, typed_code}}`).
2. Before freezing a contract for a build/test dispatch, the facade
   computes the candidate's retry_key-equivalent (same normalization
   over the ABOUT-TO-RUN action). If the key exists in the ledger, the
   dispatch needs a material delta: different argv tokens, different
   environment fingerprint, a contract with `intent_source=
   accepted_repair`, or a changed fact_epoch. Prose/expectation changes
   are NOT deltas. No delta ⇒ REFUSE (completed_failure,
   error_code=RETRY_WITHOUT_DELTA, ControlAssessment
   typed_code="retry_without_delta", the prior typed_code + count named
   in the output).
3. Transient budget: typed codes `transient_network`/`timeout` allow up
   to 2 identical retries (count tracked per key; the 3rd refuses).
4. Poll/resume + detached completions never touch the ledger (they are
   lifecycle, not retries — assert via the existing detached statuses).
Stage exit: full suite; Plan 5 profiles unchanged; negative controls
(identical deterministic retry refused; repair-stamped retry allowed;
transient allows 2; detached poll exempt).

## Stage E — Native affordance

Spec §C8. Bound on `524baa0`. Single lane; the LIVE TVM S0→S3 battery
runs with Stage F's acceptance runs — E ships the machinery + negative
controls.

**Allowlists (EXACT, module data):** definitions keys must match
`^(USE_[A-Z0-9_]+|BUILD_TESTING)$` with values `ON|OFF` only; features
resolve through `NATIVE_FEATURE_RESOLVER = {"llvm": {"debian_packages":
["llvm-dev", "libxml2-dev"], "probe": "llvm-config --version"}}`
(extensible data, no project names). Anything else — compiler launchers,
toolchain files, absolute/escaped paths, arbitrary -D keys — is rejected
with a ControlAssessment.

**Task E1 (lane e1):** files: `src/sag/tools/build/build_tool.py`
(native verb), `src/sag/tools/build/backends.py` (PythonBackend native
materialization), `src/sag/tools/internal/python_tool.py` (native rebuild
execution path), `src/sag/agent/repair_contracts.py` (capability +
dependency-pin proposals), NEW `tests/test_native_affordance.py`.
1. `build(action='native', features=[...], definitions={...},
   working_directory=...)`: validated against the allowlists; dispatch
   REQUIRES (a) a `capability_absent_<feature>` assessment on record for
   the domain AND (b) supporting policy claims for the definitions
   (CI/docs `CMAKE_ARGS`/`set(USE_X ...)` claims) — the freeze records
   those claim IDs; model params carry no provenance. Missing either ⇒
   refusal error_code=NATIVE_WITHOUT_PROVENANCE, typed_code
   `native_without_provenance` ("no project-owned repair policy — the
   state is unknown, not repairable").
2. Execution (python system): resolver installs the feature's
   debian_packages (apt, allowlisted list only), runs the feature probe,
   then re-runs the project's own editable install with
   `CMAKE_ARGS="<definitions>"` through the EXISTING python deps
   machinery (env overlay, --no-deps rung preserved); receipt
   `capability_observations` records the probe output line.
3. `repair_contracts.build_repair` extensions: `capability_absent_<n>` +
   a matching `USE_<N>`-definition claim ⇒ propose
   `build(action='native', features=[n], definitions={...from claims})`;
   a dependency-class failure + an exact-pin dependency claim
   (`pkg==literal`) ⇒ propose `build(action='deps', args="<pin>")` —
   and the deps path accepts an allowlisted exact-pin args install
   (literal pins from claims only; wire minimally in python_tool).
4. Negative controls: bad definition key/value rejected; unknown feature
   rejected; no capability assessment ⇒ refused; no supporting claims ⇒
   refused; model-supplied provenance ignored; a positive smoke after
   native never unlocks unbounded collect (existing receipt semantics —
   assert unchanged).
Stage exit: full suite; Plan 5 profiles unchanged.

## Stage F — Proof

Spec §6. Bound on the Stage E merge. Two lanes + the live battery (run by
the reviewer). Verified gap driving f1: `discover_document_map`,
claim extraction, `claim_graph`, and live repair CREATION have zero
production callers — the loop exists but is not wired into a run.

**Task F1 (lane f1): live wiring.** Files:
`src/sag/tools/internal/project_analyzer.py`, `src/sag/agent/react_engine.py`,
`src/sag/tools/build/build_tool.py` (one bounded hunk), NEW
`tests/test_live_loop_wiring.py`.
1. Analyze path: after the survey facts land, run
   `discover_document_map` + `write_document_map`, then the claim
   extractors over indexed entries (bounded text fetch per entry budget)
   + `write_claims`; DomainFacts projection then sees real claims. Failure
   of either is a recorded conflict, never an analyze failure.
2. Repair creation: after a failure-class ReceiptAssessment is persisted
   (the c3 surfacing seam), when no repair exists for it yet, call
   `retrieve_for` (text fetch via orchestrator over the persisted map) +
   `build_repair` + `write_repair`; surfacing then finds it. Bounded: one
   creation attempt per assessment.
3. Claim transitions: after `expectation_met`, group-transition the
   contract's `supporting_claim_ids` from untested → confirmed; after a
   `falsifier_*` assessment on an exact/equivalent fresh receipt,
   group-transition those claims → contradicted + `invalidate_dependents`
   (one event group per binding note (a), emitted via the engine's
   control sink; claim_graph.materialize refreshes the snapshot).
4. Live-wiring tests with fake orchestrator: analyze produces map+claims
   files; a failure assessment yields a persisted repair + surfaced
   block; expectation_met confirms supporting claims; falsifier
   contradicts + invalidates; all replay fixtures untouched.

**Task F2 (lane f2): proof suites + verifier.** Files:
`scripts/verify_native_test_policy.py`, NEW
`tests/test_metamorphic_fixtures.py`, NEW
`tests/test_no_project_name_policy.py`.
1. Metamorphic: the bigtop-shaped domain/edge fixtures re-run with
   renamed roots/groups/artifacts/versions must produce structurally
   identical facts/edges/contract shapes (ids differ, structure equal).
2. No-project-name policy: a test walks `src/sag/**/*.py` and asserts the
   names commons-cli, bigtop, bigpetstore, tvm appear ONLY in comments/
   docstrings, never in executable code (ast-based: scan Name/Str
   comparisons and literals inside conditionals).
3. Verifier Plan 6 upgrades (auto-armed only when the session carries
   contracts): every runner receipt has contract_id + the chain holds
   (upgrade contracts.chain from silent to REQUIRED when
   invocation_contracts/ is non-empty); an assessments dir exists when
   receipts exist; analyze sessions carry document_map.json.
Stage exit: full suite; Plan 5 recorded profiles STILL 6/10/10/10
(pre-Plan-6 sessions have no contracts — assertions stay silent there).

**Live battery (reviewer-run, after F merges):** cli + bigtop + tvm
(serial), --record, machine-verified; TVM additionally graded on the
S0→S3 expectations (capability assessment present, repair persisted with
CI-claim provenance, accepted_repair contract if the model takes it,
honest outcome either way); Plan 6 final acceptance report.
