# Plan 8 Anchor Grading — the Evidence Lifecycle, Live

**Date:** 2026-07-31
**Code:** `plan8/integration` @ `2a5bd14`, 28 commits over main. Full suite
**3,801 passed / 2 skipped** (the two are the pre-existing python3.12-on-PATH
and wheel-packaging skips). Four locked profiles 9 / 13 / 18 / 15, 0 failed,
re-verified after every commit. Fifteen mutations (M1–M15) each shown red
against exactly its fence.
**Runs:** `session_20260731_114114_29150` (p8a polaris),
`session_20260731_114116_29173` (p8a camel),
`session_20260731_114122_29197` (p8a kafka),
`session_20260731_134019_31659` (p8b kafka, after the snapshot fix). All
archived into repo `logs/`. Graded from control events and receipts.

## The headline numbers

| | before (p7d / campaign) | on this branch |
|---|---|---|
| polaris main count | **0** (321 stranded in auxiliary) | **119 passed, in the main count; auxiliary empty** |
| camel main count | **0** (11,492 stranded) | **170 claimed**; 11,411 attributed to the NAMED unsettled job |
| kafka | 546 main, 4,686 unclaimed, mechanism unknown | mechanism found and fixed: **the claim set was decided by an output clamp, not by the run** |

## Every Plan 8 mechanism fired live

1. **Ledger → settlement → receipt** — three times (kafka p8a, polaris p8a,
   camel p8a): a detached job's obligation was written at the runner's detach
   seam, the exit file appeared mid-run, the batch sweep settled it, and an
   ordinary receipt claimed the reports into the main count. Each settlement
   emitted its `job_settled` control event.
2. **A failed job's reports are still its reports** — all three settled jobs
   exited 1 (`--continue` / `--fail-at-end` runs with real test failures);
   their written reports were claimed regardless. That is the design.
3. **The wait policy** — camel p8a waited **3,094s** and kafka p8b **4,720s**
   of otherwise-evaporating wall clock for a job that never exited, then gave
   up at the report reserve and closed honestly. In p7d that same budget was
   simply lost. When the job exited in time (all p8a settlements), the wait
   cost zero.
4. **The honest unsettled close** — `job_unsettled:<id>` on the verdict, with
   its own replayable control event, and the auxiliary tests attributed to a
   named job instead of to nobody.
5. **The §3.3 cap, in words, on a live gate** — kafka p8b's test phase:
   `All 90 tests passed · job 92300a8f0c26 has no terminal receipt — the
   claim is confirmable at most`. Claimed partial, validated partial, no
   upgrade. The p7d polaris shape (partial upgraded to success on an
   in-flight snapshot) is dead in the wild, not only in tests.
6. **§3.4 basis wording live** — polaris build reason: `compiled 595 classes;
   1 expected artifact(s) missing (main JAR) and no class-based expectation
   could be derived`. No "100%" from nothing to check.
7. **§3.5 scan-owns-denominator live** — kafka build reason: `Not a complete
   build — the module scan owns the denominator and 0 of 2 modules produced
   output`.

## The discovery: the clamp was deciding the claim set

p8a kafka settled its test job and claimed **exactly 50** report files while
**260** sat on disk — all written before the job's exit file, before-snapshot
empty, no exclusions. The campaign's kafka receipt had claimed exactly 50 as
well. The same number across unrelated runs was the tell: `snapshot_reports`
read its sha256sum output through the presentation path, which truncates
beyond ~10,000 characters, and 260 hash lines is ~34KB. **The truncation, not
the run, decided what a receipt could claim.**

That one mechanism retro-explains the campaign's unexplained kafka split (546
main / 4,686 "unclaimed") and p8a's (465 / 3,196). It is also the last member
of the §3.9 family: the bracketing that receipt-scoping stands on was itself
lossy for any run large enough to matter. Fixed in `2a5bd14`
(`truncate_output=False` with the same small-double fallback `container_io`
uses); fenced by a 200-file snapshot through a clamping surface (M15 red).

**Confirmation status, honestly:** p8b was launched to confirm the fix at
scale, but the model took a different course (one small synchronous test
dispatch — 4 files, 90 tests, correctly claimed — plus one big detached job
that outlived even the 4,720s wait). So the at-scale confirmation of the
snapshot fix rests on the fence test and the p8a evidence chain, not yet on a
live 260-file settlement. The next run whose big job exits in time will
settle it.

## What the anchors did NOT fix, stated plainly

- **A job larger than the wall clock stays unclaimed.** camel's full-suite
  job (11k+ tests) and kafka p8b's (15k observed) need more than the 7,200s
  cap even with every leftover second spent waiting. The runs now say so
  honestly (`job_unsettled`, named, replayable) — but the number a reader
  sees is still small. The structural options — a larger cap for
  reactor-scale projects, or per-module test invocations — are a
  model-strategy/config question, out of Plan 8's scope, and now cleanly
  separable because the accounting no longer hides them.
- **Run-to-run variance is untouched.** polaris compiled 595 classes this
  run against p7c's 3,188; kafka's three runs took three different courses.
  The denominators are honest now; how far the model gets each run is its
  own question.
- **The test-coordinates gate** (`unsafe_coordinates`) still closes polaris
  and camel test phases as unknown — the known open item, untouched by
  design.
- **Stage 5 (§3.7)** is not on this branch (the corrected spec postdates the
  lanes; the wrong version was deliberately not landed), and **§3.6's three
  consumers** remain deferred with the persistence machinery in place.

## Process note

One evidence-preservation slip during the reruns: the p8a kafka container was
removed to reuse the name before its session had been archived into repo
`logs/`. The `--record` session dir was archived immediately after (all four
sessions now in repo `logs/`), and every probe result taken from the
container is recorded here, but the container workspace itself cannot be
re-probed. The polaris and camel containers were left untouched.

## Acceptance (spec §6), item by item

1. polaris: **met** — the settled job's tests are the main count; auxiliary
   empty.
2. camel: **met in the spec's own either/or** — the settled job claimed its
   170; the job that outlived the run carries `job_unsettled` and auxiliary
   stays honest at 11,411.
3. rocketmq/ofbiz: **not attempted** — Stage 5 not on this branch.
4. sealed-run regression: **met** — profiles byte-identical throughout;
   replay suite green with recorded sessions linked in.
5. settled path = synchronous path: **met by test** (field-for-field fence).
6. mutation discipline: **met** — M1–M15, each red against its fence.
7. green narrowed verdict still reports what is not built: **met by fence**;
   live kafka/polaris reasons carry the scan line.
8. a failed read caps and says it failed: **met by fences** for the manifest,
   the ledger, the receipts directory and the module scan; the ledger cap and
   the §3.3 cap each fired live.
