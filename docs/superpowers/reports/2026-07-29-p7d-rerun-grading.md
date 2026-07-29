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
| runner | `mvnw`, zero `NoSuchMethodError` | ⚠️ **corrected 2026-07-29:** the one terminal receipt shows `/usr/local/bin/mvn` → distro **Maven 3.8.7**, not the wrapper. Zero `NoSuchMethodError` and 18,592 classes are facts, but "wrapper held" was wrong; why 3.8.7 passes where the campaign's 3.9.15 failed, and why the wrapper preference did not engage, is unexplained and open |
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

One hundred percent and one-of-twenty-six in the same sentence.

⚠️ **The mechanism below replaces this report's first explanation, which was
wrong (corrected 2026-07-29 after walking the receipt).** The first version
attributed the 100% to the `d5dc330` scope-narrowing. The narrowing never
ran: its input is a receipt's `module_outcomes`, and the run's only receipt —
the failed Java-17 compile — has no such field. The actual chain is three
older defaults stacking:

1. the survey cannot parse polaris's Kotlin settings
   (`root_shape: single_module`, `build_islands: []`), so **no per-module
   class expectation could be derived**;
2. with no derivable expectations, `class_coverage` **defaults to 1.0** —
   "nothing to check" read as "everything passed". The hard JVM gate only
   catches the zero-classes case, and the in-flight job had already compiled
   `build-logic` (the build scripts' own helper project), so classes > 0;
3. the module scan that knew the truth — `1/26 built · no output yet: [+25]
   · tests ran in 0/8 test-bearing modules` — is **commentary appended to
   the reason string**, not an input to the verdict.

The P0-F truth table exists to stop exactly this ("the gate may confirm or
downgrade, never upgrade") and did not fire, because its cap is keyed on
unclosed survey domains and polaris has `build_islands: []` — no domains, no
cap. camel escaped by luck of shape: its survey parsed the reactor, the
incomplete mapping kept the wide denominator (`maven_reactor_unverified`),
and the gate said partial.

The faults, restated after the correction:

1. A detached job writes no receipt, ever — both runners drop the evidence at
   an explicit early return (`gradle_tool.py:682`, `maven_tool.py:1061`) —
   so everything downstream graded either a snapshot or an orphan.
2. Coverage with no derivable expectations must say "no basis", not 100%.
3. The computation that knew the truth decorated the sentence instead of
   deciding it.
4. The no-upgrade cap must also key on unfinished work, not only on unclosed
   domains. An empty domain graph is not evidence that nothing is unfinished.

All four are absorbed into the evidence-lifecycle design
(`specs/2026-07-29-evidence-lifecycle-design.md`), which these two runs
motivated.

## Still open, re-ranked

1. **Terminal semantics for detached jobs.** Now three symptoms: unclaimable
   test counts (polaris 321, camel 11,492), and a build gate that can grade —
   and upgrade on — a moving target.
2. **The analyze gate must be able to conclude** (rocketmq-externals,
   ofbiz-plugins; unchanged).
3. Package four: verdict axes, the 80% line, advisor comparison runs.
