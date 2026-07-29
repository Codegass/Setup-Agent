# p7d Rerun Grading — polaris and camel on the Repair-Channel Fixes

**Date:** 2026-07-29
**Code:** main @ `f37f40d` (fixes `166e9bd`, `054ad7f`, `364a797`).
**Runs:** `session_20260729_111737_22356` (polaris, `apache-polaris-1.5.0`),
`session_20260729_111740_22389` (camel, `camel-4.20.0`), both `--record`,
same pins as p7c, launched concurrently.
**Graded from control events and receipts, not phase summaries.**

## Result in one line

Both PARTIAL, both a layer deeper than p7c; the repair channel the fixes
repaired was never exercised because the tool layer preempts it — which is
the designed order — and the graded evidence surfaced one new gate fault,
worse than first read.

## polaris

| | p7c | p7d |
|---|---|---|
| build | SUCCESS, 3,188 classes | SUCCESS at gate; 1,706 classes at verdict, jars present, job still finishing |
| test | `unsafe_coordinates`, nothing dispatched | **dispatched and ran: 321 tests, 321 passed** — auxiliary, `receipt_scoped: true` |
| verdict | PARTIAL | PARTIAL |

Java 21 arrived the same way as p7c: the model provisioned 17 (the survey
still cannot read the imperative check in `settings.gradle.kts`), the compile
dispatch hit the sentence, and the pre-flight recovery acted on it inside the
same dispatch — `[pre-flight] build error requires Java 21, re-provisioned,
retry 1/1`.

The test phase moved past the p7c wall. Where p7c ended at "test coordinates
remained unavailable", p7d dispatched the Gradle test job, polled it patiently
(the model's polling discipline was correct throughout: escalating sleeps, two
honest `blocked(outcome='unknown')` claims, notes that state facts), and 321
tests ran and passed. They sit in auxiliary because the job never produced a
terminal exit receipt within budget — the same shape that holds camel's main
count at zero, now confirmed on a second project.

## camel

| | p7c | p7d |
|---|---|---|
| build | 4,451 classes, reactor unverified | **18,592 classes**, claimed unknown → validated PARTIAL |
| test | 11,596 auxiliary (11,444 passed) | 11,492 auxiliary (11,347 passed, 1 failed, 144 skipped), `receipt_scoped: true` |
| wrapper | `mvnw`, zero `NoSuchMethodError` | held — zero `NoSuchMethodError` |
| verdict | PARTIAL | PARTIAL |

The reactor got roughly four times further than p7c before the budget ended.
The terminal shape is unchanged and is the same fault as polaris's test
phase: a non-terminal job leaves everything it produced unclaimable.

## The repair channel was not exercised, and that is the finding

Neither run created a repair contract, and neither model called
`phase(action='repair')`. For the java mismatch this is by design: the
pre-flight recovery (`8f496f8`) sits in front of the proposal machinery and
resolves the failure inside the dispatch, so the question the p7/p7b runs
were supposed to answer — will the model accept a surfaced repair proposal —
**cannot arise for this failure class anymore**. The proposal channel is now
a fallback for failures the tool layer cannot read. Open item 0 from the
round-three report is answered by construction, not by observation: the
comparison is moot where pre-flight covers, and still untested where it does
not.

## New fault: the gate upgraded an honest claim on an in-flight snapshot

The worst single fact in these runs, stated precisely:

```
polaris build: claimed = partial   →  validated = SUCCESS (green)
camel   build: claimed = unknown   →  validated = partial
```

The polaris **model was honest** — it claimed `partial` while its compile job
was still running. The **gate upgraded the claim to success**, on this
evidence: `Built 100% of expected classes (>= 100% threshold) · Module
coverage: 1/26 built [build-logic]`.

One hundred percent and one-of-twenty-six in the same sentence. The
scope-narrowing from `d5dc330` behaved as specified — every module the build
output had named so far mapped to an expectation — but the build output was a
snapshot of a job in progress, and the only module it had named was the only
module it had reached. The wide denominator at that moment was about 5%.

The P0-F truth table exists to stop exactly this ("the gate may confirm or
downgrade, never upgrade") and did not fire, because its cap is keyed on
unclosed survey domains and polaris has `build_islands: []` — no domains, no
cap. camel escaped by luck of shape: its incomplete mapping kept the wide
denominator (`maven_reactor_unverified`), so the gate said partial.

Two faults, recorded as one open item:

1. Narrowing must not treat an in-flight task list as the reactor's statement
   of scope. A task list is the reactor's statement only when the job has a
   terminal receipt.
2. The no-upgrade cap must also key on a non-terminal receipt, not only on
   unclosed domains. An empty domain graph is not evidence that nothing is
   unfinished.

Both roll up into the long-running-jobs item, which these two runs have
promoted from "camel's problem" to the single fault holding back both
projects' main counts and now the build gate's integrity.

## Still open, re-ranked

1. **Terminal semantics for detached jobs.** Now three symptoms: unclaimable
   test counts (polaris 321, camel 11,492), and a build gate that can grade —
   and upgrade on — a moving target.
2. **The analyze gate must be able to conclude** (rocketmq-externals,
   ofbiz-plugins; unchanged).
3. Package four: verdict axes, the 80% line, advisor comparison runs.
