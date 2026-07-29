# Evidence Lifecycle Completion — SAG v2 Plan 8 Design

**Date:** 2026-07-29
**Status:** approved direction (2026-07-29 review with Chenhao); design-only,
implementation not started
**Author:** worked out jointly against the p7d graded evidence
**Compatibility:** preserves the Category-3 facts-only boundary
(`2026-07-19-analyzer-diet.md`), the Bigtop attribution rule, the provenance
tiers, the P0-F truth table (its trigger is broadened, its direction is
unchanged), the pre-flight JDK recovery, and the material-progress retry law.
**Live evidence:** `session_20260729_111737_22356` (p7d polaris),
`session_20260729_111740_22389` (p7d camel), graded in
`reports/2026-07-29-p7d-rerun-grading.md` and corrected during the
2026-07-29 review.

## 1. Problem

The receipt system carries an unstated assumption: dispatch → terminal exit →
receipt → assessment → claim, all inside one tool call. A dispatch that
outlives the 900-second soft window breaks the assumption — the work
continues, the call returns — and the system has no representation of
"evidence still forming". The break is one explicit line, present in both
runners:

```python
# gradle_tool.py:682, maven_tool.py:1061
if result.get("dispatch_status") in DETACHED_HANDOFF_STATUSES:
    return          # <- the before-snapshot in hand is dropped; no receipt, ever
```

The exit code the job will eventually write (`/tmp/sag_jobs/<id>.log.exit`,
written atomically by the launcher), the complete log, and the before-snapshot
taken at dispatch all exist. Nothing returns to look.

What p7d showed this costs, per project:

**polaris.** One receipt in the whole run — the *failed* Java-17 compile
(exit 1). The successful Java-21 retry detached; no receipt. The test job
detached; no receipt. 321 tests ran and passed and sat unclaimable in
auxiliary. And the build gate graded a snapshot: the model honestly claimed
`partial` while its compile job was still running, and the gate **upgraded the
claim to success** on the sentence
`Built 100% of expected classes (>= 100% threshold) · Module coverage: 1/26
built [build-logic]`.

**camel.** One receipt (a `mvn verify` that exited 1). 11,492 tests observed,
zero claimable.

The polaris upgrade needed three older defaults to line up, and all three are
in scope because each is an instance of the same two principle gaps:

1. The survey cannot parse polaris's Kotlin settings, so
   `root_shape: single_module`, `build_islands: []` — **no per-module class
   expectations derivable**.
2. With no derivable expectations, `class_coverage` **defaults to 1.0** —
   "nothing to check" read as "everything passed". (The existing hard JVM
   gate catches only the zero-classes case; the in-flight job had already
   compiled `build-logic`, so classes > 0.)
3. The module scan that knew the truth (1/26 built, 25 with no output, tests
   in 0/8 test-bearing modules) is **commentary appended to the reason
   string**, not an input to the verdict. The P0-F no-upgrade cap is keyed
   only on unclosed survey domains, and the domain list was empty.

The same "absence collapsed to a verdict" shape, in the opposite direction:
the analyze gate demands a static test count as a readiness precondition, and
a repository that cannot produce one (rocketmq-externals, ofbiz-plugins) reads
as permanently unready and skips build and test — absence collapsed to
*failure* where coverage collapsed it to *success*.

## 2. Principles

> **P1 — the run's clock is not the evidence's clock.** Work that outlives
> the call still finishes, and its evidence completes when *it* completes.
> Accounting follows the evidence. Until the books are settled, nothing is
> allowed to conclude upward.
>
> **P2 — no basis is its own answer.** A check that cannot derive its
> expectations returns "no basis" — never "met" (coverage's 1.0 default) and
> never "unmet" (the analyze gate's permanent unreadiness). The caller
> decides what no-basis means for the phase; the check never collapses it.
>
> **P3 — one question, one computation.** When two computations answer the
> same question, the wrong one eventually decides while the right one
> decorates. The decider and the display consume the same object. And between
> tiers: a receipt-proven statement of structure outranks a survey guess the
> moment it exists — which is the provenance ladder this project already has,
> applied to structure.

## 3. Components

### 3.1 The job obligations ledger

One file per detached dispatch, written at the exact seam that today drops
the evidence (the `DETACHED_HANDOFF_STATUSES` early return in both runners):

```
/workspace/.setup_agent/job_obligations/<job_id>.json
```

```json
{
  "schema_version": 1,
  "job_id": "373f63e5a0a4",
  "tool": "gradle",
  "attempt": 1,
  "requested_action": "test",
  "effective_action": "test",
  "argv": "/workspace/polaris/gradlew --continue --build-cache test",
  "working_directory": "/workspace/polaris",
  "before": {"<report path>": "<sha256>", "...": "..."},
  "contract_id": "ic-...",
  "contract_hash": "...",
  "requirements_pins": {"...": "..."},
  "log_path": "/tmp/sag_jobs/373f63e5a0a4.log",
  "exit_code_path": "/tmp/sag_jobs/373f63e5a0a4.log.exit",
  "settled_receipt_id": null
}
```

Everything above is already in hand at the dispatch site — the `before`
snapshot is taken before `_run_build`, the contract is frozen by
`ensure_dispatch_contract`, the paths come from the detach handle. The ledger
is the same atomic-write, same-body-no-op, different-body-refused convention
as receipts and repairs. An obligation is **append-only**; settling it writes
`settled_receipt_id`, nothing else ever changes.

### 3.2 Settlement

Settling one obligation, when its exit file exists:

1. read the exit code from `exit_code_path` (atomic by construction);
2. take the `after` snapshot over the same roots the `before` used;
3. read the complete log from `log_path`; run the SAME parsers the
   synchronous path runs (`_gradle_module_outcomes` /
   `_reactor_module_outcomes`, cached-report roots);
4. write an ordinary receipt through `record_invocation` with the obligation's
   stored fields — same schema, same directory, same everything; the only
   thing that moved is *when*;
5. mark the obligation settled; emit one control event
   (`kind: "job_settled"`, payload: job_id, receipt_id, exit_code) so replay
   reproduces the state;
6. run the same post-receipt hooks the synchronous path runs
   (`ensure_receipt_assessed`, repair creation on typed failure) so a settled
   failure is assessed and proposable exactly like a synchronous one;
7. surface one bounded notice in the next observation:
   `[settled] job 373f63e5a0a4: exit 0 — receipt inv-gradle-1-0002, 8 report
   roots claimed`.

**Triggers, all idempotent** (an obligation with `settled_receipt_id` is
skipped; a missing exit file leaves it open):

- **after each executed action batch** — the engine sweeps the ledger (one
  `cat` of the obligations glob, one existence check per open obligation;
  obligations are rare, zero to two per run);
- **at phase-claim time** — `_inspect_phase` settles before it grades, so the
  gate only ever grades settled books;
- **before report generation** — the closing sweep.

**A job that never terminates** stays an open obligation. At evidence-close
it is recorded on the verdict as a conflict (`job_unsettled:<job_id>`) with
the obligation as provenance. Nothing is guessed from a partial log.

**Attribution stays the Bigtop rule.** The settling receipt's delta is its
own `before` (at dispatch) against `after` (at settlement) plus its own
cached roots — the job's own write window. One caveat is real: another
dispatch in the same roots *between* dispatch and settlement could write
reports inside that window. Receipts are ordered, so settlement excludes any
path already claimed by an intervening receipt — first claim wins, and the
exclusion is recorded on the settling receipt (`excluded_claimed_paths`
count). Unclaimed stays unclaimed.

### 3.3 Open obligations cap the gate

`validate_phase_claim` currently caps refinement above the claim while any
surveyed domain is unclosed (P0-F). The trigger broadens; the direction is
untouched:

> While any obligation in the ledger is unsettled **or** any surveyed domain
> is unclosed, the gate may CONFIRM or DOWNGRADE the claim, never refine it
> upward. Additionally, a GREEN validator state is capped to PARTIAL while an
> obligation is open: success requires settled books.

The reason names the evidence: `job 373f63e5a0a4 has no terminal receipt —
the claim is confirmable at most`. An honest `partial`/`unknown` claim passes
exactly as today; honesty is never punished. polaris p7d under this rule:
claimed partial → validated partial, and the books settle in the test phase
where the classes and reports actually land.

### 3.4 Coverage carries its basis

`coverage_info` gains `basis: "derived" | "none"`. When no per-module class
expectation could be derived, `class_coverage` is **absent**, not 1.0:

- basis `none`, classes > 0 → **PARTIAL**:
  `compiled 1,706 classes; no per-module expectation could be derived —
  coverage has no basis`;
- basis `none`, classes = 0 → BLOCKED (the existing hard JVM gate, now a
  special case of the general rule);
- basis `derived` → exactly today's thresholds and messages.

### 3.5 The decider and the display are one computation

Today two scans answer "which modules built": the survey-derived expectation
walk (decides) and `module_coverage`'s subproject scan (decorates). They
disagreed in one sentence on polaris. One module-scan result object becomes
the input to **both** the validated outcome and the checklist line, with a
test in the `repair_moves()` style pinning them together: a reason whose
decision half says 100% while its commentary half says 1/26 must be
unconstructible.

Denominator authority, in order, all named in the message:

1. a **terminal receipt's** `module_outcomes` (the #17 narrowing rules,
   unchanged — but only ever from a receipt, which after §3.2 means only
   from completed work);
2. the module scan on disk;
3. the survey's expectations.

### 3.6 Receipt-proven structure outranks survey structure

When the first terminal build receipt carries non-empty `module_outcomes`,
the harness persists a structure fact at receipt provenance (the module list,
keyed as `_module_key` does today, `provenance: <receipt_id>`) into the build
requirements. From then on the coverage denominator, the test-bearing module
list, and the domain graph read the receipt-proven structure; the survey's
`single_module` / empty-islands guess remains the proposer only until real
work has stated otherwise. A newer terminal receipt may update the structure;
a survey re-run may never demote it. This closes the polaris chain — "survey
blind → empty domains → every guard keyed on domains disarmed" — because
guards key on obligations (§3.3) and structure arrives from receipts (§3.5).

It deliberately does NOT attempt to parse Kotlin settings or imperative
version checks statically. Pre-flight owns stated-requirement recovery and
owns it well.

### 3.7 The analyze gate can conclude

P2 applied to the other collapse. "No static test count derivable" becomes a
recorded fact (`analysis.test_count_basis = "none"`, provenance the survey),
and analysis readiness stops requiring a static count. Build and test
proceed; the final count is whatever receipts prove, which is the only count
the verdict trusts anyway. A repository that genuinely has no unified build
(rocketmq-externals as a bag of subprojects, ofbiz-plugins outside its parent
tree) produces a run that *attempts* its parts and states what it found,
instead of `analysis_not_ready` skipping both phases. The stated finding —
"this repository declares no unified build" — is admissible evidence for an
honest `blocked`/`partial` close, not a missing prerequisite.

## 4. What does not change

- **The Bigtop rule.** An untouched file nobody vouched for stays unclaimed.
  Settlement only lets completed work claim its own write window.
- **The provenance tiers** — strengthened: structure itself now rides them.
- **Pre-flight JDK recovery** — untouched; it is the working answer for
  stated version requirements.
- **The retry law** — untouched and better fed: settled receipts move retry
  keys with real terminal outcomes.
- **The truth table's direction** — the gate still never upgrades; only the
  trigger set for the cap grows.
- **Category 3** — settlement writes receipts and facts, never prescriptions.
- **Replay compatibility** — the `job_settled` control event and ledger files
  are additive; transcripts recorded before this design replay exactly as
  they do today.

## 5. Stages

Each stage lands with its tests first, the full suite green, and the four
locked verifier profiles unchanged (cli 9, bigtop 13, tvm 18 and 15) before
the next begins.

| Stage | Contents | Retires |
|---|---|---|
| 1 | obligations ledger at both runners' detach seams; settlement (sweep, phase-claim, pre-report); `job_settled` control event; post-receipt hooks on settled receipts | the orphaned main counts (polaris 321, camel 11,492) |
| 2 | gate cap on unsettled obligations (`validate_phase_claim` trigger broadened; green capped to partial) | the partial→success upgrade |
| 3 | coverage `basis` tri-state | "100%" from nothing to check |
| 4 | unified module-scan object (decider = display, pinned by test); receipt-proven structure promotion | the sentence that contradicted itself; the disarmed-guard chain |
| 5 | analyze-gate conclusion (`test_count_basis`, readiness change) | rocketmq-externals / ofbiz-plugins `analysis_not_ready` |

Live anchors after stages 1–2: rerun polaris and camel, grade from control
events. After stage 5: rerun rocketmq-externals and ofbiz-plugins.

## 6. Acceptance

1. **polaris rerun:** the test job's tests appear in the **main count** once
   its receipt settles (or, if the run ends first, the verdict carries
   `job_unsettled` and auxiliary stays honest); the build gate validates at
   most the claim while the compile job is open; no reason sentence can pair
   a passing coverage verdict with a minority module scan.
2. **camel rerun:** same, at 11k scale; wrapper/runner selection stated
   truthfully in the graded report.
3. **rocketmq-externals, ofbiz-plugins:** analyze concludes with the stated
   finding; build is attempted; whatever happens next is evidence, not a
   skip.
4. **No sealed-run regression:** all four locked profiles byte-identical;
   replay of every existing recorded session unchanged.
5. **The settled path is the synchronous path:** one test dispatches, forces
   a detach, settles, and asserts the receipt is field-for-field what the
   synchronous path would have written (timing aside) — one schema, one
   writer, no second bookkeeping system.

## 7. Risks and their answers

- **Late `after` snapshots misattribute intervening writes** → first-claim
  exclusion against ordered receipts (§3.2), recorded, tested.
- **Settlement surprises the model** ("where did this receipt come from?") →
  the one bounded `[settled]` notice line; no synthetic tool results.
- **A poll-heavy model starves settlement** → the engine sweep runs after
  every action batch regardless of which tools the batch used.
- **Two writers to one receipt id** → settlement allocates ids through the
  same `record_invocation` counter as the synchronous path; the obligation
  stores no id until settled.
- **Ledger corruption** → same read discipline as every evidence directory:
  a line that does not parse is skipped, never fatal.
