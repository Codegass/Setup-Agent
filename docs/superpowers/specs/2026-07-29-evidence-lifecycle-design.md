# Evidence Lifecycle Completion — SAG v2 Plan 8 Design

**Date:** 2026-07-29
**Status:** revised 2026-07-29 after round one of implementation. Stages 1–4
exist on lane branches under review; §3.7 was found wrong at its root and is
rewritten below, so Stage 5 has not been implemented against the corrected
text. §3.4, §3.6 and §7 carry constraints the round-one code violated.
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
>
> **P4 — removing evidence must never improve a verdict.** Discarding a
> receipt, failing to read one, or swallowing an exception into `None` may make
> a verdict less certain. It may never make it better.

P4 was added after three implementation rounds each shipped an upward
refinement in the same file, and it is the diagnosis of why. The #17 narrowing
has an inverted incentive built into it: the coverage denominator shrinks with
the attempted-module set, so **anything that removes a module from that set
makes the build look more complete**. Three changes, each defensible on its own
terms, each tripped it:

| round | the change | the effect |
|---|---|---|
| 1 | fall back to the persisted structure when the receipt probe returns nothing | a stale single-module structure narrowed a 26-module denominator to 1 |
| 2 | key the authority on "a receipt stated modules" | the minority-scan cap was disarmed by a receipt's mere existence |
| 3 | filter non-terminal receipts out of `_attempted_modules` | dropping an OOM-killed reactor's 26 modules let a scoped `-pl m0` retry narrow to 1 and grade GREEN — a regression from both main and round two |

All three are the same bug. So the rule is structural, not a patch: a receipt
the harness will not trust as a *prover* must still be counted as a *claimant*.
An unreadable, non-terminal, or crashed dispatch **caps** the verdict — exactly
as §3.3 already does for an unsettled obligation — and never shrinks the
denominator. Narrowing is licensed only by evidence the harness is willing to
stand behind in both directions.

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

`coverage_info` gains `basis: "derived" | "none"`. Basis is `none` only when
**no expectation of any kind** could be derived. A jar-only expectation that was
derived and found IS a basis — `classes_expected == 0` means "no class-based
expectation", which is a different statement and must not be read as this one
(round-one implementation error, caught by both reviewers: a Kotlin/Scala module
whose only expectation was its JAR, all_present, was downgraded to PARTIAL and
told falsely that nothing could be derived). When basis is `none`,
`class_coverage` is **absent**, not 1.0:

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

Three constraints the round-one implementation violated, now explicit:

**Terminal means the dispatch ended, not that the exit code is an integer.**
An OOM-killed detached build gets a synthesized exit code (`orch.py:1237-1240`
returns 1 for a vanished poll) and a truncated log; Gradle prints task lines
incrementally, so a build killed at module 40 of 300 names exactly 40 and would
enshrine that guess at the top provenance tier. §3.2's "nothing is guessed from
a partial log" governs here too, so the receipt must carry enough for a reader
to tell how the dispatch ended.

**A persisted structure is not this pass's attempted-module list.** Narrowing
the coverage denominator is licensed only by what THIS run's receipts say they
attempted (§3.5 rung (a)). Substituting the stored structure when the receipt
probe returns nothing shrinks the denominator on evidence that never said
anything about this dispatch, which refines a partial build upward — the
violation this whole plan exists to prevent.

**Update is not unconditional replacement.** A scoped dispatch (`mvn -pl m0`)
states one module; that must not demote a wide reactor's statement of
twenty-six. Only a terminal receipt whose statement is at least as wide may
restate the structure.

### 3.7 A refusal that carries what it saw

> **This section replaces an earlier version that was wrong at its root, and
> the correction is worth recording because the wrong version survived a spec
> review.** The first version said analysis readiness required a static test
> count and should stop requiring one. Readiness never required a static count:
> `_inspect_analyze` (`phase_gates.py:939-947`) returns PARTIAL with
> `build_entry_ready: True` whenever `analyzed` is truthy, and `analyzed` is set
> by ANY survey marker — `project_type`, `build_system`,
> `build_recommendation`, `survey`. Implementing the wrong version relaxed the
> only remaining precondition that analysis produced anything, granting build
> entry to runs where nothing was surveyed and sealing a verified fact and a
> verdict sentence asserting a survey finding no survey ever produced. Both
> round-one reviewers found it independently, each with a measured five-input
> enumeration across the two commits.

The live shape, read from the sealed evidence of
`session_20260727_082937_1609` (rocketmq-externals) and
`session_20260727_082940_1649` (ofbiz-plugins):

```
validator_state: red   build_entry_ready: false
code: analysis_static_count_missing
facts: {trunk_context_found: true, static_test_count_present: false}
```

and, in the control events, **five** `project(action='analyze')` calls all
returning `PROJECT_NOT_FOUND` — on `/workspace/rocketmq-externals`, on the same
path with a trailing slash, via `.git/..`, and on two guessed nestings. The
model then gave up on the survey and closed the phase.

**The refusal is correct.** `/workspace/rocketmq-externals` contains
`README.md`, `dev`, `docs`, and twenty-six sibling subproject directories. It
holds none of the twelve project indicators `is_valid_project_directory`
(`physical_survey.py:2342-2397`) looks for, and no `src`/`lib`/`app`/`source`.
The root genuinely is not a project. The campaign report's characterization —
"a bag of unrelated subprojects with no unified build" — was right; its
*mechanism* ("the gate requires a static test count as a precondition") was
wrong, and this is the third correction to that one entry.

So the defect is not readiness, and not the analyzer's judgement. It is that a
correct refusal is a **dead end** and then gets **mislabelled**:

**(a) The refusal states only what it was asked, never what it saw.**
`PROJECT_NOT_FOUND` carries `requested_path` and nothing else. The same walk
that rejected the root passed twenty-six directories each holding a `pom.xml`.
That is a fact, and stating it costs one more observation:

> `PROJECT_NOT_FOUND: /workspace/rocketmq-externals holds no project indicator
> and no source directory. 26 subdirectories each contain a build file:
> rocketmq-connect, rocketmq-console, rocketmq-flink, … (+23).`

This is Category-3 clean — an observation, not a recommendation, and no plan.
It replaces five path guesses with one statement the model can act on or close
on. Bound the enumeration (a name list plus a count, like every other bounded
projection in the loop).

**(b) The status code contradicts the state.** `analysis_static_count_missing`
projects "Project survey facts exist, but no static test-count fact was
observed" — and on this path no survey facts exist at all. The truthful code
`analysis_facts_missing` ("No persisted project survey facts were observed") is
**unreachable**: `physical_validator.py:4462` claims the code slot when
`static_test_count` is absent, and the fallback at `:4488` only fires
`if not analyzed and not analysis_status_code`. So the sentence that misled the
campaign report is emitted by construction whenever a trunk exists and the
survey found nothing. Assign the code from what was actually observed: no
survey markers → `analysis_facts_missing`; markers but no count →
`analysis_static_count_missing`. This is §2 P3 again — the sentence and the
state must come from one determination.

**(c) Readiness is unchanged.** No survey → RED → `analysis_not_ready` is the
honest outcome, and this section must not touch it. What changes is that the
run now carries a stated finding about the repository instead of a false
sentence about a missing count, and the model reaches that finding in one
refusal instead of five.

Whether a run should go on to attempt the twenty-six subprojects is a real
question and explicitly **out of scope**: it is a multi-project-repository
design question, not an evidence-lifecycle one. Stating the finding is what
makes it answerable later.

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
| 5 | the refusal states what it saw; the status code matches the state (`analysis_facts_missing` made reachable). **Readiness unchanged.** | the five blind path guesses, and the sentence that misattributed the cause three times |

Live anchors after stages 1–2: rerun polaris and camel, grade from control
events. After stage 5: rerun rocketmq-externals and ofbiz-plugins.

**Stage 5 is now the smallest of the five, not the largest.** The first version
of §3.7 made it a readiness change; the corrected version is an observation and
a code assignment. If a future reader finds it suspiciously cheap, that is the
point — the expensive version was the wrong one.

## 6. Acceptance

1. **polaris rerun:** the test job's tests appear in the **main count** once
   its receipt settles (or, if the run ends first, the verdict carries
   `job_unsettled` and auxiliary stays honest); the build gate validates at
   most the claim while the compile job is open; no reason sentence can pair
   a passing coverage verdict with a minority module scan.
2. **camel rerun:** same, at 11k scale; wrapper/runner selection stated
   truthfully in the graded report.
3. **rocketmq-externals, ofbiz-plugins:** one `project(action='analyze')` call,
   not five; the refusal names the twenty-six subprojects it saw; the sealed
   code is `analysis_facts_missing` and the sentence it projects is true of the
   run. Readiness stays RED and the run closes honestly on that finding.
4. **No sealed-run regression:** all four locked profiles byte-identical;
   replay of every existing recorded session unchanged.
5. **The settled path is the synchronous path:** one test dispatches, forces
   a detach, settles, and asserts the receipt is field-for-field what the
   synchronous path would have written (timing aside) — one schema, one
   writer, no second bookkeeping system.
6. **Every new test fails under the mutation it exists to catch.** Round one
   shipped a test for P3 that asserted against a fake re-implementing the
   production method, so deleting the production caching left all 3,611 tests
   green. Each stage's key test must be shown to fail when the behaviour it
   pins is removed, and the demonstration recorded in the acceptance report.

## 7. Risks and their answers

- **Late `after` snapshots misattribute intervening writes** → first-claim
  exclusion scoped to receipts written AFTER the dispatch, not to every receipt
  on disk (§3.2). Round one applied it to all of them, which cost a detached
  retry its own rewrite — the very case Stage 1 exists to retire. The ledger
  must record enough at dispatch time to order receipts against it.
- **Settlement surprises the model** ("where did this receipt come from?") →
  the one bounded `[settled]` notice line; no synthetic tool results.
- **A poll-heavy model starves settlement** → the engine sweep runs after
  every action batch regardless of which tools the batch used.
- **Two writers to one receipt id** → settlement allocates ids through the
  same `record_invocation` counter as the synchronous path; the obligation
  stores no id until settled.
- **Ledger corruption** → same read discipline as every evidence directory:
  a line that does not parse is skipped, never fatal.
