# Plan 7 Round Three — Acceptance and Corrections

**Date:** 2026-07-29
**Code:** main @ `f9a0294`. Full suite **3,574 passed / 1 skipped**. The four
locked verifier profiles unchanged (cli 9, bigtop 13, tvm 18 and 15).
**Supersedes:** the "still open" list in `2026-07-28-plan7-acceptance.md`.

> ## ⚠️ CORRECTION (2026-07-29, after walking the p7 and p7b transcripts)
>
> This report's headline section was wrong, and wrong in the direction that
> flattered the design. It said the model was given every affordance and
> declined twice. The control events say the opposite.
>
> **The model never declined anything.** In both p7 and p7b it read Gradle's
> sentence, diagnosed it correctly, and submitted that diagnosis through
> `phase(action='repair', target_phase='build', reason_code='java_version_mismatch')`
> with a hypothesis in its own words: *"provisioning JDK 21 should satisfy the
> build's runtime check and allow compilation to proceed."* Byte-for-byte the
> same call in both runs (`envelope-000028` in each).
>
> **The repair phase was never granted.** `_REPAIR_EDGES` is
> `{("test","build"), ("build","analyze")}`. `build→build` is `illegal_edge`,
> and so is `build→provision` — **no repair edge from build reaches the phase
> that installs a JDK**, so the call the harness itself proposed could not be
> routed by the channel the model used. The engine closes the attempt *before*
> it checks legality, so the refusal arrived with the build phase already
> terminal, test skipped, run over. All the model was ever told, in the report
> handoff, was `build-1: build->build rejected`.
>
> **In p7 the harness surfaced no proposal at all.** `_java_version_proposal`
> shipped in the p7b round. p7's model diagnosed it from Gradle's output alone.
>
> Three further faults found while walking it, all fixed
> (`166e9bd`, `054ad7f`, `364a797`):
>
> 1. `accepted_repair_for` compared the dispatch against the constant
>    `REPAIR_TOOL` (`"build"`) while the java proposal names `project`. Had the
>    model called the proposed call exactly, **it still would not have counted
>    as accepting it.**
> 2. `target_phase`'s schema carried the enum `["analyze", "build"]` — the
>    *union* of targets — described only as "direct dependency target". From
>    the build phase, `build` is an enum-valid value and not a move at all.
>    **The schema told the model the thing that cost it the run.**
> 3. The policy's typed verdict was computed, stored on the record, and dropped
>    by the handoff projection.
>
> The corrected reading is below. The p7c result itself stands: polaris builds,
> 3,188 classes, and the harness did it by reading Gradle's own wording.

## The headline: polaris builds

polaris across four graded runs:

| Run | Verdict | Where it stopped |
|---|---|---|
| campaign | FAILED, 0 tests | the compile **never ran** — every retry refused as `RETRY_WITHOUT_DELTA` |
| p7 | FAILED, 0 tests | compile ran under Java 17; the model diagnosed the mismatch unprompted and proposed the repair — refused as `illegal_edge`, phase already closed |
| p7b | FAILED, 0 tests | the harness surfaced the proposal too; the model agreed and proposed the same repair — refused identically |
| **p7c** | **PARTIAL, build SUCCESS** | **3,188 classes, 21/26 modules**; test phase blocked on coordinates |

The log line that made the difference: `build error requires Java 21,
re-provisioned, retry 1/1`. The harness read Gradle's own sentence, installed
the JDK it named, and re-ran — without asking.

What the three runs actually establish is narrower than this report first
claimed, and more useful. The tool layer succeeded because it is the only
place in the design where a JDK swap is reachable at all: the phase machine
has no edge that gets there, so no amount of model competence could have
routed one. **The model's diagnosis was correct in every run that got as far
as compiling.** What failed twice was the path from a correct diagnosis to the
action it implies.

That is a claim about plumbing, not about autonomy. Whether a weak model can
be trusted to act on a mechanically-mandatory step is a question these three
runs did not test, because the model never got to answer it. The affordances
are only now actually in place; the comparison has to be re-run to mean
anything.

## Why the model chose the channel it chose

Worth stating plainly, because "the model picked the wrong tool" would be the
easy reading and it is not supported.

The proposal said *"accept by calling it, or state why not"*. The model instead
called `phase(action='repair')` — the verb whose description is "proposes one
bounded direct-dependency repair with a failure signature, hypothesis, and
current-attempt evidence", which is precisely what it had. It filled
`target_phase` with a value the parameter's own enum listed as legal. Every
step of that is the documented surface behaving as documented.

The cost of the wrong guess was the entire remainder of the run, and it was
unrecoverable and unexplained. That asymmetry — a natural reading of the
affordance, priced at the whole run — is the defect. Not the guess.

## camel: the scoping fix visible in the wild

camel's run reports **0 tests in the main count and 11,596 in auxiliary**
(11,444 passed, 2 failed, 150 skipped), `receipt_scoped: true`.

That is the fix from this round working exactly as intended. The same shape
one round earlier — a Maven test job that never produced a terminal exit
receipt, so nothing could claim the reports it left on disk — put **17,798
tests straight into the main count** with no receipt behind any of them,
because an unresolvable primary coordinate silently restored the whole-tree
scan. Now the reports are visible, attributed to nobody, and excluded from
the headline. The number went down and the honesty went up.

The wrapper fix holds across all three dispatches (`/workspace/camel/mvnw`,
zero `NoSuchMethodError`). The build reports 4,451 compiled classes and a
reactor it could not fully verify.

## kafka: the campaign report's open question, answered

The report left "kafka's 546 against the old 2,937" as the one unexplained
number. It is not lost coverage — it is the opposite.

That run dispatched one `gradlew --build-cache test`. The receipt claimed 50
reports across 10 modules (546 tests). A further **4,686 passing tests** sat
in auxiliary because Gradle served most test tasks FROM-CACHE: their reports
were never rewritten, the content hashes did not move, and `report_delta`
could claim none of them. **The run observed 5,232 tests — 78% more than the
old benchmark's 2,937 — and reported 546.**

A cache hit is not weaker evidence than a write: Gradle states that the report
on disk *is* this build's result for that task, which is stronger than a file
merely existing. Reports under a directory a cached or up-to-date **test**
task vouched for now enter `report_delta.cached`, count toward the primary
rollup, and stay in their own bucket so what ran is always separable from what
was vouched for. A cached `compileJava` vouches for nothing, and an untouched
file nobody vouched for stays unclaimed — the Bigtop rule unchanged.

## Corrections to the campaign report

Two more attributions were wrong. Both were mine, from reading a phase
summary instead of the evidence.

**rocketmq-externals and ofbiz-plugins — not "analysis exhausted its budget".**
Both analyze phases *failed*, with the same reason: "Project survey facts
exist, but no static test-count fact was observed." Build and test were then
skipped as `analysis_not_ready`. The gate requires a static test count as a
precondition for readiness, and these two repositories cannot produce one —
rocketmq-externals is a bag of unrelated subprojects with no unified build,
ofbiz-plugins only builds inside the main OFBiz tree. For them, "this
repository states no unified test count" *is* the conclusion, not a missing
prerequisite. The harness treated an answer as an unmet requirement.

**ignite — two faults, neither of them budget.**

`ignite-checkstyle-${revision}.jar` was reported missing on every run. Maven's
CI-friendly versions leave that placeholder to resolve at build time, not in
the pom we read, so the path can never exist and the shortfall can never
close. Fixed: an expectation we cannot state is not an expectation.

`Built 100% of expected classes (< 100% threshold)` contradicts itself —
17,779 classes compiled, 20 short across four modules, rounded to 100%. Fixed:
the message states the counts it is deciding on, because a rounded percentage
is what produced a sentence nobody could act on.

## What this round changed

1. An unresolvable primary coordinate no longer restores unscoped counting.
2. The reactor summary and Gradle's task outcomes ride the receipt and set the
   coverage denominator; narrowing happens only when every module the build
   named maps to an expectation, and an incomplete mapping keeps the wide
   denominator and records `build_coverage_scope_unverified`.
3. Build-cache hits are claimable, in their own bucket.
4. A wrong parameter is refused and named instead of crashing the tool
   (`EnvTool.execute() got an unexpected keyword argument 'JAVA_HOME'`), and a
   refused executable path names the registered candidates the harness already
   holds.
5. The JDK recovery recognises Gradle's own wording, which is what made
   polaris build.
6. Unresolvable expectations and rounded coverage messages, above.

## Round four — the three faults the correction found

Same code, full suite **3,599 passed / 1 skipped**, four locked profiles
unchanged (cli 9, bigtop 13, tvm 18 and 15).

**`166e9bd` — a repair the model can perform is not a phase rollback.**
`accepted_repair_for` compared the dispatch against one constant while the java
proposal named another facade, so the exact call the harness asked for could
never be recognised as accepting the proposal that asked for it. Acceptance now
compares against the tool the stored proposal names, bounded by
`PROPOSABLE_TOOLS` so an ordinary call still costs no container read, and a test
pins the generators and the matcher to the same set. Separately,
`phase(action='repair')` now declines a typed code that already has a live
proposal and names the call instead — a proposal the model can simply perform
needs no rollback and no permission, which is what §C6 said all along.

**`054ad7f` — an edge the policy does not have is answered before anything
moves.** Whether an edge exists is a property of the request: no gate, no
validator, no physical evidence. `repair_targets_for(phase)` derives the legal
set from `_REPAIR_EDGES`, and the surface refuses an unlisted target with the
targets this phase does have — or states it has none. Budget exhaustion, the
recurrence guard and `repair_source_green` deliberately still close the phase:
the first two exist to end a run's rollbacks and the third routes a
validated-green attempt correctly.

**`364a797` — a refusal states why, and the parameter states which moves
exist.** `decision_reason` reaches the projection, and the line distinguishes
the model's question from the policy's answer:
`rejected (illegal_edge) asked=java_version_mismatch`. Empty by default, so
replayed transcripts render unchanged. `target_phase` derives both its enum and
its description from the policy table, and the description carries the
per-source rule the enum cannot express.

*Ordering note:* the live-proposal refusal outranks the edge refusal. polaris
tripped both, and "make this call" is the more useful answer — rolling back to
`analyze` would have re-surveyed the project and still not installed Java 21.

*Not moved:* `stale_repair_evidence`. It is malformed-request shaped like the
edge check, but an attempt's evidence refs reach `RunEvidenceState` on the gate
path, so a surface check could refuse a legitimate repair whose refs have not
landed yet. Needs a live run to confirm the ordering.

## Still open

0. **Re-run the polaris comparison.** The p7/p7b runs did not test what this
   report claimed they tested, and the affordances they were supposed to test
   are only now in place. Until that rerun exists, there is no evidence either
   way about whether the model can be left to act on a stated, installable
   version requirement.
1. **The analyze gate must be able to conclude.** "No static test count" is a
   valid finding for a repository with no unified build; it currently reads as
   permanent unreadiness and skips both remaining phases
   (rocketmq-externals, ofbiz-plugins).
2. **The test gate has the same shape.** polaris and camel both ended at "test
   coordinates remained unavailable (`unsafe_coordinates`)" with a healthy
   build behind them. Same family as (1): a precondition the project cannot
   satisfy in the form expected.
3. **Long-running test jobs.** camel's Maven test job never produced a
   terminal exit receipt within the budget, so 11,596 observed tests stayed
   unclaimable. Polling and budget need an exit that states what was seen.
4. Package four: verdict axes (correctness / positive evidence /
   applicability / breadth), the 80% pass-rate line that failed tapestry-5,
   and the advisor comparison runs.
