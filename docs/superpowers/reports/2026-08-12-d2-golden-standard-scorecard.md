# D2 golden standard — 23 projects, one run each

**Date:** 2026-08-12
**Campaign:** `logs/d2-20260812/`, lock `e8d0470` (clean tree), prompt bundle
`cef63daf…`, 3 lanes, pre-registered big-first order, 11:13→14:56 (3h43m).
**Baseline:** the 2026-08-08 battery as corrected by
`2026-08-08-sag-performance-attribution-audit.md` (2 success / 16 partial /
5 failed, 15,322 canonical executions).

**Result: 0 success / 13 partial / 10 failed, 10,030 canonical executions.**

That headline is worse than the baseline, and the honest reading is that D2
did its job: it surfaced two defects no unit test and no D0 probe could
reach, and it confirmed one repair at scale.

## What this campaign proved

**The ARG_MAX evidence-transport failure is dead.** Zero occurrences of
`argument list too long` across 23 projects, including the reactors that died
of it in the baseline. This was the single mechanism the whole
2026-08-08 repair plan was built around.

**The wrapper/unzip repair (WS5) holds at scale.** HTTPComponents went from
FAILED with 0 executions in the baseline — the image lacked `unzip`, so the
Maven wrapper silently switched archive formats and validated a tarball
against a ZIP checksum — to **2,255 executions** today.

**Claiming is reproducible.** Jackrabbit sealed **4,722** executions, byte-identical
to the baseline's 4,722. Commons-dbcp sealed 1,605/1,596, matching both the
baseline and the 2026-08-10 live proof.

## The scorecard

Read from each run's sealed `verdict.json`, not from console text.

| project | verdict | physical build | classes | modules | executed/discovered |
|---|---|---|---|---:|---|
| jackrabbit | partial | partial | 4,230 | 19/26 (half) | **4,722**/2,534 |
| httpcomponents-client | partial | partial | 1,439 | 5/5 (fully) | **2,255**/1,856 |
| commons-dbcp | partial | success | 201 | 1/1 (fully) | **1,605**/1,163 |
| cayenne | partial | partial | 8,399 | 38/52 (half) | **1,373**/4,471 |
| tomcat-jakartaee-migration | partial | success | 21 | 1/1 (fully) | **52**/52 |
| seatunnel-web | partial | partial | 579 | 4/9 (few) | **23**/79 |
| kafka | failed | partial | 11,421 | 0/2 (none) | 0/20,497 |
| ignite | partial | partial | 9,339 | 25/37 (half) | 0/— |
| kogito-examples | partial | partial | 2,843 | 26/51 (half) | 0/— |
| camel-quarkus | partial | partial | 3,386 | 13/51 (few) | 0/— |
| freemarker | partial | partial | 2,092 | 2/2 (fully) | 0/976 |
| samza | failed | partial | 1,418 | 0/1 (none) | 0/— |
| cassandra-java-driver | partial | partial | 2 | 1/16 (few) | 0/— |
| camel | failed | failed | 0 | 0/51 (none) | 0/— |
| geode | failed | failed | 0 | 0/54 (none) | 0/— |
| tapestry-5 | failed | failed | 0 | 0/37 (none) | 0/— |
| polaris | failed | failed | 0 | 0/26 (none) | 0/— |
| lucene | failed | failed | 0 | 0/34 (none) | 0/— |
| gora | failed | failed | 0 | 0/23 (none) | 0/— |
| spark-kubernetes-operator | failed | failed | 0 | 0/5 (none) | 0/— |
| samza-hello-samza | failed | failed | 0 | 0/1 (none) | 0/— |
| ofbiz-plugins | partial | failed | 0 | unavailable | 0/— |
| rocketmq-externals | partial | failed | 0 | unavailable | 0/— |

**13 of 23 produced compiled classes. 6 of 23 drove tests.** The regression
against the baseline is concentrated on the BUILD side, not on claiming:
tapestry-5 (2,417 baseline executions), polaris (2,571), spark-kubernetes-operator
(175), gora (10) and lucene (6) all compiled nothing today, so no test could
follow.

## Why the builds failed

### 1. Toolchain acquisition loops — RETRACTED (2026-08-13)

> **This section's numbers were a measurement artifact, and its causal claim
> is withdrawn.** The counts (rocketmq ×453/×689, tapestry ×71, …) were
> `grep -c` over CONSOLE logs, which re-render branch-history JSON — old error
> lines included — on every context save. The authoritative
> `control_events.jsonl` shows the true counts: **1–5 ENV_EXECUTABLE_NOT_FOUND
> events per run**, runs of **17–34 model turns lasting 3–10 minutes**. Nothing
> looped until the run was spent; nothing was spent at all.
>
> The true causes of these six failures, from the sealed `phase_records`:
>
> - **tapestry-5**: analyze ABORTED on `LLM response unavailable:
>   litellm.InternalServerError` — one transient provider 5xx killed the run
>   with no retry (react_engine.py:3334), two turns AFTER the model had
>   recovered from the search lie by finding build.gradle itself with `find`.
> - **rocketmq-externals**: build ABORTED on `harness control recovery
>   exhausted: repair_assessment_persist_failed` — the controller failed to
>   persist its own repair context and killed the run; the model's `blocked`
>   claim ("no usable Maven toolchain, no wrapper") was factually correct.
> - **gora / geode / spark-kubernetes-operator / samza-hello-samza**: builds
>   BLOCKED honestly on real toolchain/runtime issues and the runs converged
>   quickly by design (`dependents_skipped`) — the fast-close machinery
>   working, not a loop. spark-kubernetes-operator's record even shows the
>   wrapper WAS found and used; the build failed on a runtime configuration
>   issue.
>
> The single-point abort family is task #45 and blocks the D2 re-run. The
> recurrence bound landed as `ab3f13e` remains as low-risk defense-in-depth,
> but the evidence that motivated it was inflated ~100× by this measurement
> error — recorded in its spec as well.

### 2. A failed search is reported as "no matches" (task #41)

`search_tool._grep_container` (src/sag/tools/search_tool.py:161-172) builds
`grep -n <pattern> <path> 2>/dev/null | head -N`, discards stderr, never
inspects the exit code, and always returns `completed_success`. grep's three
outcomes — match, no match, and **error** — collapse into one confident
"No matches for …". Plain grep is also BRE, so the build-file probe
`(^|/)build\.gradle$|(^|/)settings\.gradle$|(^|/)pom\.xml$|(^|/)gradlew$`
treats `|` literally and can never match.

> **Correction (2026-08-13).** That pattern is the model's own invention, not
> a harness probe — it appears nowhere in the repository. The distinction
> matters: `search(file:…)` greps file CONTENTS, so even with alternation and
> exit codes fixed, the call can never find a file *named* `gradlew`. The
> model reached for a name lookup the tool does not offer. Fixed defect
> tracked as #41 (landed, `cf7a7fb`); the missing affordance is #43.

Tapestry-5 has both `build.gradle` and `gradlew` on disk. It was told it had
no build files, tried `gradlew` 378 times and a nonexistent `/usr/bin/gradle`
59 times, and finished with zero compiled classes after driving 2,417 tests in
the baseline. Every one of the 23 projects hit this message (38–342 times each).

This is the same principle already fixed for container reads in `a41109d` —
*a read that did not succeed is not a read that found nothing* — missed on the
search surface. **It is old code (2026-06-10, last touched 2026-07-15), so it
was equally present in the baseline and does not explain the delta**; it is a
real defect that this campaign finally made visible.

### 3. Environment: a transient network outage

Three projects are contaminated and their rows are not evidence about the
harness: lucene (173 TLS handshake failures — the clone never completed),
samza (141), polaris (95 `JAVA_INSTALL_FAILED`, apt could not reach
ports.ubuntu.com). The window was roughly 12:57–13:07; host and container
reachability were verified healthy afterwards and the in-flight projects
recorded zero network errors.

### 4. My own verdict-derivation defect (task #40)

Kafka compiled 11,421 classes and the physical oracle judged the build
`partial`; the run sealed `failed`. The cause is the rule I wrote into the
2026-08-10 spec: *build modules band `none` → failed*. Kafka's module scan
reported 0 built of a denominator of 2 — for a Gradle project with dozens of
subprojects — and that degenerate count overrode the physical oracle. Samza is
the second instance (1,418 classes, sealed `failed`).

Two of the ten `failed` verdicts are therefore misgraded. The physical facts
underneath them are correct; only the word is wrong.

## A correction about my own measurement

My first scorecard reported test executions as "unavailable" for most
projects. That was wrong. I had extracted the numbers with a regex over
console text that matched `cases 1605/1163 (fully)` but not
`cases 1605/1163 (unbounded — …)`, so every project whose cases grain landed
in the new `unbounded` band read as missing data. Commons-dbcp, which I had
briefly written off as a regression, had in fact executed its full 1,605.

The instrument failed silently and I reported its silence as a finding — the
same defect class as #41, committed by me while investigating #41. The table
above is rebuilt from the sealed verdicts, which is what I should have read
first.

## What to do next

1. **Fix #42 (toolchain acquisition) and #41 (search).** These are the two
   defects standing between this harness and a representative build rate.
   Neither is large.
2. **Fix #40 (derived verdict).** The word must not let a degenerate module
   count override the physical oracle.
3. **Re-run D2 under a new lock.** Per the plan, no campaign is hot-patched;
   this one stands as the record that found the defects.
4. The `unbounded` band is doing exactly its job — it fired on kafka
   (11,421/1,319), dbcp (1,605/1,163), jackrabbit and httpcomponents, and each
   time it published both real counts and refused the impossible ratio instead
   of naming a flattering band. The underlying denominators still need the
   treatment task #39 specs.

**Evidence retained:** `logs/d2-20260812/` (lock, per-project console logs,
progress, results) and 23 `sag-d2-*` containers, plus each run's archived
session under `logs/session_20260812_*`.
