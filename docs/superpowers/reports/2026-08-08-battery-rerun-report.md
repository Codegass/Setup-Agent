# 23-Project Battery Rerun — fixed harness vs the 7/27 campaign

**Date:** 2026-08-08
**Code:** main @ `eeb4ee0` (Plans 6–8 + p9 stall window + #30, all merged).
**Setup:** the campaign's own 23 Java projects at the campaign-pinned refs,
3 lanes, big-first, `--record` per run. Total wall time 2h58m
(00:57–03:55). All sessions archived under repo `logs/`
(`battery-20260808/` holds console logs + progress; per-run session dirs as
usual). Containers `sag-bt-*` left up for hand-verification.

## Per-project comparison

| project | campaign 7/27 (main count) | battery 8/8 (main count) | bucket |
|---|---|---|---|
| tomcat-jakartaee-migration | 成功 52/52 | **SUCCESS 52/52** | held |
| commons-dbcp | 成功 1,596/1,605 | **SUCCESS 1,596/1,605** | held |
| kafka | 部分 546 | **PARTIAL 2,516/2,516** | **↑ flip** — the snapshot clamp is dead; 4.6× the campaign count |
| jackrabbit | 部分 main 0 (6,667 observed) | **PARTIAL 4,722/4,722** (raw 6,667) | **↑ flip** — the exact stranded executions now claimed |
| tapestry-5 | 失败 253/329 | **PARTIAL 2,417** (2,037✓/14✗/366 skip) | **↑ flip** |
| polaris | 失败 0 | **PARTIAL 2,571** (2,513✓/47✗) | **↑ flip** (18,913 solo on p9a — 3-lane load shrank the model's reach, not the claiming) |
| seatunnel-web | 失败 0 | **PARTIAL 23** (19✓) | ↑ small flip |
| gora | 部分 0 | **PARTIAL 10/10** | ↑ small flip |
| freemarker | 部分 870/870 | PARTIAL 870/870 | held; gate honestly caps green tests over a partial build |
| spark-kubernetes-operator | 部分 175 | PARTIAL 175/175 | held |
| kogito-examples | 部分 2 | PARTIAL 1 | held (examples repo) |
| ignite | 部分 0 | PARTIAL 2 | held (hard) |
| camel-quarkus | 部分 0 | PARTIAL 0 | held (hard; model gave up in 5 min, 8 failed actions) |
| lucene | 部分 17,328 | PARTIAL 6 | ↓ model course: ran a 6-test smoke and closed; 0 unsettled, 0 handoffs — honest account of a small run |
| geode | 部分 10,390 | PARTIAL 0 | ↓ model course: build partial, test phase never drove a suite |
| cayenne | 部分 4,562 | PARTIAL 0 | ↓ model course: same shape as geode |
| cassandra-java-driver | 部分 4,434/4,590 | FAILED 352 (252✓/85 err) | ↓ smaller scope; the 85 errors are counted, not painted over |
| httpcomponents-client | 部分 663/663 | FAILED 0 | ↓ build failed this run (campaign built clean — run variance, worth one retry) |
| samza | 部分 2,351 | PARTIAL 0 | ↓ wall-clock + real hangs: build SUCCESS, then test dispatches hung on absent brokers — **the stall window's first natural firing** ("handed off after 863s: no observable progress for 600s"), jobs unsettled at close |
| camel | 失败 0 | PARTIAL 0 | structural: wall-clock-scale reactor, honest zero (`job_unsettled`, named) |
| samza-hello-samza | 失败 0 | FAILED 0 | env-dependent example (needs YARN) |
| rocketmq-externals | 失败 0 | FAILED 0 | held (custom mdh branch) |
| ofbiz-plugins | 失败 0 | FAILED 0 | structural (plugins repo without the ofbiz framework) |

**Verdicts:** campaign 2 成功 / 14 部分 / 7 失败 → battery 2 SUCCESS / 15
PARTIAL / 6 FAILED. **Totals:** campaign ≈43,200 claimed vs battery
≈15,300 — see the honest read below.

## The honest read

1. **The claiming chain is fixed and proves it on the projects that were
   its victims.** Every project the campaign report flagged as
   "observed-but-not-counted" flipped: kafka 546→2,516 (the exactly-50-files
   clamp retro-explained and dead), jackrabbit 0→4,722 (raw 6,667 = the
   campaign's stranded number, now claimed), tapestry-5 253→2,417, polaris
   0→2,571. Six flips up, none of them by generosity — failed and skipped
   tests are itemized.
2. **The lower grand total is model course variance, not harness loss.**
   The five big regressions (lucene 17k→6, geode 10k→0, cayenne 4.5k→0,
   cassandra 4.4k→352, samza 2.3k→0) all autopsy to the model taking a
   different course this run: 0 unsettled jobs and 0 handoffs on
   lucene/geode/cayenne — the suites were simply never driven. gpt-5.4-mini's
   run-to-run reach is the dominant noise source, exactly as flagged in the
   p8 grading. The harness's job was to make the numbers truthful either
   way; it did.
3. **The stall window fired naturally** — samza's hung gradle tests
   (waiting on brokers that don't exist in the container) were handed back
   at 863s with "no observable progress for 600s" and per-signal
   observations, instead of silently absorbing the full 900s each with a
   message implying progress. Nothing was killed; the close attributed the
   unsettled jobs by name.
4. **Wall-clock scale is now visibly THE binding constraint** for
   camel/samza-class projects — builds succeed, test jobs outlive every
   window, accounting is honest, and the number stays small. The (a)
   bigger-caps vs (b) per-module-tests decision is where the next real
   points are.

## Follow-ups this battery argues for

- Retry httpcomponents-client once (campaign built it clean; one flaky
  build shouldn't stand as its record).
- The model-course variance now dwarfs harness noise: consider pinning a
  minimal per-project test-drive expectation (the §3.6 consumers / Stage 5
  work) so a 6-test lucene run cannot read as a completed test phase.
- #32 (five-analyzer misclassification) stays open; it misdirected the
  model in this battery too (polaris/gradle "Error"-in-task-name).
