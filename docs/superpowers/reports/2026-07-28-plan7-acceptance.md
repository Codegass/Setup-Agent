# Plan 7 Acceptance — Campaign Fixes, Two Rounds

**Date:** 2026-07-28
**Code:** main @ `8f496f8`. Full suite **3,549 passed / 1 skipped**. The four
locked verifier profiles unchanged throughout (cli 9, bigtop 13, tvm 18 and
15).
**Plan:** `plans/2026-07-27-sagv2-plan7-campaign-fixes.md`, both approved
packages, plus a second round the reruns themselves made necessary.

## Result in one line

Four faults fixed and confirmed by rerun; three of the four projects moved
forward a layer; the canary held. Two of the attributions in the campaign
report were wrong and are corrected below — both were the model's guess, and
both real causes were the harness's own.

## What the reruns showed

| Project | Before Plan 7 | After | Confirmed by |
|---|---|---|---|
| commons-cli | SUCCESS, model hand-parsed XML in 7–8 actions with 3 shell crashes | SUCCESS 982/921/0/61 (8th identical), 3 actions, no crashes | verifier 9/9; `tests_run` and `test_stats` present in the result |
| camel | build died before compiling: `NoSuchMethodError` in the resolver | that error **gone**; the project's own `./mvnw` (pinning Maven 3.9.11) is used | `[toolchain] using the project's own ./mvnw (pins Maven 3.9.11)` in the log; zero `NoSuchMethodError` |
| polaris | compile **never ran** — every retry refused as `RETRY_WITHOUT_DELTA` | compile ran; the java mismatch is typed and a repair is proposed | zero `RETRY_WITHOUT_DELTA`; `java_version_mismatch` assessment + repair contract on disk |
| camel-quarkus | 2 of 2 Mavens blocked, `active: None`, 64 refusals | overlay healthy: `active: /opt/maven/bin/mvn`, nothing blocked, **0 refusals** | live overlay dump |

## The four faults

**1. A registered toolchain did not change the retry identity.** This was my
own doing: the fingerprint added during the Plan 6 battery hashed
`/workspace/.setup_agent/toolchains.json`, and no container in the campaign
has that file — `EnvTool` register/activate writes only the env overlay and
never calls the registry writer. Two stores, no connection, so the fingerprint
was permanently absent and the retry key could never move. polaris installed
and activated Java 21 correctly at steps 28–33 and every subsequent compile
was refused with an identical key; the compile that "failed" had run before
the registration. camel-quarkus was the same fault with Maven. Fixed by
mirroring registration into the registry, with a repeat registration that
states identical facts writing nothing (so it cannot manufacture a false
delta).

A lesson worth keeping: a fingerprint over a store that is not always written
does not mean "no change detected" — it means "no change, ever". Fail-closed
in the wrong direction, and it closed on the recovery path.

**2. Maven ignored the project's own wrapper.** `GradleTool` defaulted to
`./gradlew`; `MavenTool` defaulted to the registered Maven. camel ships
`mvnw` pinning 3.9.11 and `.mvn/extensions.xml` with extensions built against
that resolver, so a different Maven fails before compiling. The same rule now
holds for both build systems, with a fallback to the registered Maven —
recorded, not silent — when the wrapper is missing, not executable, or fails
to start, because a wrapper that downloads its distribution needs a network.

**3. Test counts were computed and thrown away.** The Maven analyzer parsed
`result["output"]`, which the orchestrator had already clamped to 30 head plus
50 tail lines; `full_output` was assigned twelve lines later. In a 605 KB log
the aggregate `Tests run:` line sits in the omitted middle. The analysis now
reads the complete text — it is parsed by regex and never reaches the model's
context, so no truncation applies to it. Gradle had the same defect.

Separately, the truncation notice told the model to use `bash` with `grep`,
but the complete output is stored as a reference that grep cannot reach. It
now names the reference and the exact `output_search` call. commons-cli's
first action after the fix was that call.

**4. An Enforcer failure was attributed to Maven without checking either
whether the build said so or whether the version satisfied the range.** Live
camel-quarkus blocked Maven **3.9.15** for failing `[3.9.0,)` — a range it
plainly satisfies — because the only test was that *some* requirement value
existed, and that value had arrived as the caller's own tool parameter. The
real fault was Java 11 against a build needing 17+, so each retry installed a
newer Maven and condemned that one too until the overlay held no usable
candidate at all. Now only a Maven-version failure the build itself states may
block, and a version that satisfies the requirement never blocks — using the
toolchain manager's own comparator, since the code that decides whether a
runtime is usable must be the code that decides whether it may be condemned.

## What round two added, and what it did not reach

**A stated java mismatch is now a typed failure with a repair.** When a build
names both the java it needs and the java it got, that is receipt evidence —
the runner said it, in its own output — so `java_version_mismatch` carries the
assessment itself as its support and proposes provisioning the required
runtime. It is the only repair that needs no document claim, and the reason is
recorded in the code: the no-claims rule exists to stop the harness inventing
a remedy, and here the build stated it.

**The facade's own JDK recovery had no pattern for Gradle's wording.** The
bounded recovery has been there all along — classify the failure, re-provision
the version the error names, rerun the same argv once — and every precondition
held on polaris. What it lacked was the sentence: Gradle says "Dependency
requires at least JVM runtime version 21" and "Build requires Java 21", and
every pattern the classifier knew came from Maven Enforcer or javac. Verified:
the old classifier returns None on polaris's text, the new one returns 21. The
recovery stays in one layer — `gradle_tool` still does not retry on the facade
path, which a pre-existing test pins.

**polaris, honestly:** in the graded rerun the harness diagnosed the mismatch,
typed it, proposed provisioning 21 with provenance, and granted the repair
phase when the model asked for it. The model wrote a report and closed instead.
Every affordance was in place and the run still failed. With the classifier
fix the harness will now act on that sentence itself rather than asking — that
change is committed but has not yet been graded by a rerun.

> ⚠️ **The paragraph above is wrong and is corrected in
> `2026-07-29-plan7-round3-acceptance.md`.** The repair phase was **not**
> granted: the model asked for `build→build`, which is not an edge the policy
> has, and the engine closes the attempt before it checks legality — so the
> refusal arrived with the build phase already terminal and test skipped. The
> model did not choose to write a report; the engine routed it there. Nor was
> every affordance in place: `accepted_repair_for` could not have recognised
> the proposed call even if the model had made it, and `target_phase`'s enum
> listed `build` as a legal value. Fixed in `166e9bd`, `054ad7f`, `364a797`.

## Corrections to the 23-project report

Two attributions in `reports/2026-07-27-23-project-campaign-report.md` were
the model's own conclusion, written into its phase record and repeated by me.
Both are wrong:

- **polaris** — reported as "the build tool is not inheriting the registered
  Java 21 runtime". It was inheriting nothing because it never ran: the retry
  law refused every dispatch after the registration (fault 1). Attribution
  moves from "activation chain" to the harness's retry fingerprint.
- **camel-quarkus** — reported as "the environment overlay marks two Mavens
  usable, so the test phase cannot choose". The overlay ambiguity was real and
  is fixed, but the reason no Maven was usable is that both had been wrongly
  blocked (fault 4). Attribution moves to the Enforcer misattribution.

Neither correction changes the campaign's headline counts. Both move a project
from "framework fault, cause X" to "framework fault, cause Y", and in both
cases Y was introduced or missed by us rather than stated by the project.

## Still open, in order

1. `EnvTool` **crashes** on an unexpected keyword — live camel-quarkus:
   `Tool project crashed: EnvTool.execute() got an unexpected keyword argument
   'JAVA_HOME'`. A bad parameter must be refused, never crash.
2. A refused activation should name the registered candidates. The model asked
   for `/usr/lib/jvm/java-17-openjdk-amd64/bin/java` on an arm64 machine while
   the arm64 path was registered; the refusal said only that the path does not
   exist.
3. Regrade polaris and camel with the classifier fix in place.
4. camel-quarkus's remaining failure is a Maven `PluginIncompatibleException`,
   which names no version pair. Leaving it untyped is the honest choice — the
   harness should not guess a requirement the build never stated.
5. The kafka 546-against-2,937 split, then packages three and four from the
   campaign report.
