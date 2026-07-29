# Plan 7 Round Three — Acceptance and Corrections

**Date:** 2026-07-29
**Code:** main @ `f9a0294`. Full suite **3,574 passed / 1 skipped**. The four
locked verifier profiles unchanged (cli 9, bigtop 13, tvm 18 and 15).
**Supersedes:** the "still open" list in `2026-07-28-plan7-acceptance.md`.

## The headline: polaris builds

polaris across four graded runs:

| Run | Verdict | Where it stopped |
|---|---|---|
| campaign | FAILED, 0 tests | the compile **never ran** — every retry refused as `RETRY_WITHOUT_DELTA` |
| p7 | FAILED, 0 tests | compile ran; the model provisioned Java 17 and closed the phase |
| p7b | FAILED, 0 tests | the repair was proposed with provenance and the repair phase granted; the model declined both |
| **p7c** | **PARTIAL, build SUCCESS** | **3,188 classes, 21/26 modules**; test phase blocked on coordinates |

The log line that made the difference: `build error requires Java 21,
re-provisioned, retry 1/1`. The harness read Gradle's own sentence, installed
the JDK it named, and re-ran — without asking.

That is the boundary the design draws, tested three times. When a step is
mechanically mandatory — the build states the version it needs and that
version is installable — leaving the decision to the model failed twice with
every affordance in place: the fault was typed, the repair proposed with
provenance, and the repair phase granted on request. The harness doing it
itself succeeded on the first attempt.

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

## Still open

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
