# p9 Stall-Window Anchor Grading — Task 8, Live

**Date:** 2026-08-07
**Code:** `p9/stall-window` @ `c6e5615` (8 stall-window commits + the #30
search-surface fix). Full suite **3,867 passed / 1 skipped**; mutation pass
M1–M10 each red under its own fence and green after revert.
**Runs:** `session_20260807_190333_64639` (p9a polaris, 18.5 min),
`session_20260807_192604_65621` (p9a camel, 109 min), and the induced-stall
probe (`logs/p9-stall-probe-20260807/`). All archived into repo `logs/`;
containers sag-p9a-polaris and sag-p9a-camel left up for hand-verification.

## Headline

| | p8a (same projects, same model) | p9a |
|---|---|---|
| polaris main count | 119 | **18,828 / 18,913 passed** (29 real failures, PARTIAL, honest) |
| polaris duration | ~85 min with handoffs | **18.5 min, zero handoffs, zero open obligations** |
| camel | 170 claimed + 11,411 attributed to unsettled job | verify job unsettled again (wall-clock-scale, unchanged by design); close waited **~83 min** for it |

The polaris delta is NOT all stall-window: p8a ran three anchors
concurrently and p9a ran solo. But the mechanism's contribution is visible:
both dispatches completed **in-band** inside their windows, so every test
was claimed by an ordinary receipt with no ledger round-trip at all.

## Mechanism scoreboard — what fired live

1. **The new probe transport** — polaris and camel poll commands carry
   `NOW:$(date +%s)` (container clock) and the `find -newermt @<ts-1>`
   build-tree scan, S1-only on the first cycle, exactly per spec §2. 158
   probe cycles with tree scan in camel's first log segment alone.
2. **Test-tier min(stall, total), the RIGHT half winning** — camel's
   `mvnw --fail-at-end -Dmaven.test.failure.ignore=true verify` (test-
   bearing → windowed; note `-Dmaven.test.failure.ignore` is correctly NOT
   read as a skip flag by the tokenizing parser) produced stdout
   continuously, so the 600s stall clock never fired and the handoff came
   at the **900s total window** (19:33:03 dispatch → 19:48:03 handoff).
   A progressing test job held its full window; a stalled one would have
   left at 600 (the induced probe proves that branch).
3. **The stall handoff, §5 contract** — induced probe, ALL PASS, twice
   (the second run was accidental and free): handoff at 60s ± poll,
   `handoff_reason: stalled`, per-signal observations ("stdout last grew
   60s ago (log size 0 bytes); no build-tree writes observed since
   dispatch"), "NOT killed" stated, "hung" never said, process verified
   alive after, no exit file.
4. **Obligation → 83-minute close wait → honest unsettled close** — camel:
   the verify handoff wrote its obligation (dispatch_sequence 4); at
   evidence close 19:52:07 the wait engaged with "**5071s of wall clock
   remain before the report reserve**" and held until 21:14:53 before
   closing `job_unsettled` with the job named on the verdict. p8a camel
   waited 3,094s; p9a waited ~4,966s — the whole leftover budget, spent on
   the one thing that needed time. This also live-confirms the SHARED
   deadline helper (`_hold_deadline`) computing real margins in the real
   engine — the same computation the dispatch hold consumes through the
   installed provider.
5. **In-band completion unchanged** (guard §4.3) — polaris compile
   (5m19s) and test (~10 min) returned field-normal in-band results; the
   verdict path never saw a held-vs-quick difference.

## What did NOT fire live, stated plainly

- **The unbounded progress-tier hold past 900s** — the plan's target
  scenario — was not observed, for opposite reasons on the two projects:
  polaris's compile finished in 5m19s (p8a's 900s handoff was three-way
  concurrent load, not build size), and camel's three compile attempts
  each failed fast (`PluginDescriptorParsingException`, ~1–2.5 min each)
  before any long hold could begin. The past-900s hold rests on fences
  1/5 and mutations M1/M7, plus the live-confirmed deadline helper — not
  yet on a live build. The next naturally slow prerequisite build will
  settle it; inducing one artificially (load-rigging the machine) would
  not prove anything the fences don't.
- **The orchestrator consulting the installed provider live** — no live
  dispatch needed the wall guard. Pinned by the X2 fence (which drives the
  real run-loop seam) and indirectly by the wait's live margin computation.

## Discoveries out of the anchors

- **#32 (filed):** `classify_detached_completion` sweeps five hypothetical
  analyzers over the same tail because it never receives the real command;
  the make analyzer's bare `"Error"` substring matched the Kotlin task NAME
  `checkKotlinGradlePluginConfigurationErrors`, so polaris's successful
  compile (exit 0, "BUILD SUCCESSFUL in 5m 19s") was reported to the model
  as `DETACHED_OPERATION_FAILED`. Only 2 "Error" hits in the whole 26KB
  stored output — both in that task name. Pre-existing (not touched by
  p9); the physical validator contained the damage (verdict numbers clean),
  but the model burned ~4 actions on a failure that never happened.
- **camel's compile-vs-verify split** — compile failed 3× on
  `PluginDescriptorParsingException` yet the verify job compiled 51,590
  classes; the maven wrapper/version mystery documented since p7d remains
  open and is unchanged by this branch.
- **Log rotation nearly caused a false grading** — main.log rotates at
  ~3MB; the camel session's live story sat in three .gz segments and the
  visible main.log covered only the final minute. First reads said "no
  dispatches, no wait" — both wrong. Graders must always sweep
  `main.*.log.gz`.

## Verdict against the spec's acceptance (§8)

Fences 1–9: **met** (M1–M10 all red-then-green, including the review
round's ten added fences). Live anchor half 1 (prerequisite held past 900s
to completion): **not yet observed** — carried as the one open live item.
Live anchor half 2 (induced stall with §5 text): **met, twice**.

The branch is ready to merge on the fence evidence; the open live item is
an observation opportunity, not a code risk — every path it would exercise
is mutation-pinned, and its failure mode (early handoff) is the pre-p9
status quo.
