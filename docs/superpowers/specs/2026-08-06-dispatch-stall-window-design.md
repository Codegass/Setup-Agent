# Dispatch Stall Window — hold while it progresses, hand off when it stalls

**Date:** 2026-08-06
**Status:** draft for review
**Scope:** the dispatch-and-poll hold in `execute_command_with_soft_timeout`
(`src/sag/docker_orch/orch.py:1114`) and its callers (maven/gradle build
tools). Everything downstream of a handoff — obligation ledger, settlement,
wait-at-close, the §3.3 cap — is out of scope and unchanged.

## 1. Problem

The soft window is a fixed 900-second countdown. The poll it runs every 15
seconds already answers three questions (exit file, `kill -0`, vanished) and
already measures `log_size` on every cycle (orch.py:1054) — but nothing
compares successive values. A deadlocked build and a healthy quiet one are
both `STATE:RUNNING`, so the window cannot tell them apart and treats both
the same way: hold 15 minutes, then hand off.

Both directions lose:

- A **healthy long build** is handed off at 15 minutes into a poll-burning
  loop the model must drive itself. Live: the camel p8a full-suite job and
  the kafka p8b big job were both handed off healthy; the model then spent
  its turns polling.
- A **truly hung build** cannot be detected before the same 15 minutes — and
  after handoff, never. The model polls a process that stopped progressing
  without ever being told that it stopped.

## 2. Design

Replace the question "how long has this command been running" with **"how
long since it last showed progress."** Two progress signals, checked on every
poll:

- **S1 — stdout growth.** `log_size` strictly greater than the maximum
  previously observed.
- **S2 — build-tree writes.** The newest mtime under the dispatch working
  directory's recognized build-output subtrees (`target/`, `build/`,
  `.setup_agent/pytest-reports/`) advanced past the last observation.
  Rationale: stdout can be legitimately silent while real work happens —
  surefire's `redirectTestOutputToFile` writes to report files, `gradle
  --quiet` suppresses the console, a single large C++ translation unit emits
  nothing until it finishes. S2 sees all of these.

**Stall clock:** any progress on either signal resets it; when it reaches
`dispatch_stall_seconds` (default **600**) the command is handed off — with
the same handle-preserving handoff as today, **never a kill**.

The S2 probe must be one cheap container command merged into the existing
poll transport (the `STATE:`/`SIZE:`/`---TAIL---` probe grows a `PROGRESS:`
line) — one probe, one command (P3). The existing trusted-marker rule keeps:
only the head before `---TAIL---` carries markers, because build output in
the tail can contain anything.

**False-signal asymmetry, stated up front:** a missed progress signal causes
an early handoff (mild — the model resumes polling exactly as it does
today); a spurious progress signal causes a longer hold, bounded by the
wall-clock guard (§4.2). Neither direction can kill a process or lose
evidence.

## 3. Two tiers: what the dispatch is FOR sets its ceiling

- **Prerequisite dispatches** — nothing downstream can proceed without them:
  effective action `deps`/`compile`, or `package`/`install`/`verify` with
  test execution skipped (`-DskipTests`, `-x test`). Hold while progress
  continues, **no total ceiling** except the wall-clock guard. A model turn
  during a prerequisite build has nothing useful to buy; the observed live
  behavior after a handoff is poll-burning.
- **Test-running dispatches** — the stall clock applies *inside* the
  existing total window: hand off at stall (600s) or total
  (`dispatch_soft_timeout_seconds`, 900s, kept), whichever comes first. Once
  a test dispatch outgrows its window the model has real alternatives
  (per-module invocations, partial claims) and the obligation/settlement
  path exists precisely to account for a handed-off test job. Holding for
  hours would spend the whole run inside one tool call and foreclose any
  strategy change.
- **Classification** is computed by the tool layer from the effective action
  and argv it already holds. Unclassifiable → test tier: refusing to guess
  must hold *less*, never more.

This tier boundary deliberately does not prejudge the open structural
decision for wall-clock-scale projects (bigger per-project caps vs.
per-module test strategy). Either resolution composes with this spec
unchanged.

## 4. Guards

1. **No kill semantics anywhere.** A stall handoff preserves the detached
   handle and writes the job obligation at the handoff seam exactly as
   today. "Stalled" is an observation about a window of time, not a verdict
   about the process.
2. **The wall-clock reserve bounds every hold.** No hold may extend past
   `run_started_at + wall_clock_cap − report_reserve`. This is the same
   question `_await_open_obligations` already answers
   (`react_engine.py:996`): extract ONE shared margin helper and make both
   call it (P3 — one question, one computation). The orchestrator never
   computes run budget itself; the engine installs a deadline provider at
   run start. **No provider installed** (unit doubles, run-task mode) →
   the old fixed-window behavior applies. A missing budget basis degrades
   to the bounded old behavior, never to an unbounded hold (P2: no basis is
   its own answer).
3. **Held-to-completion equals within-window completion.** A dispatch that
   completes during a stall-window hold produces a result field-for-field
   identical to today's within-window completion. No consumer can tell how
   long the harness waited.

## 5. The handoff message states observations, not conclusions

§3.9 spirit. A stall handoff must carry, per signal, when progress was last
observed ("stdout last grew 11m ago; no writes under target/ since 11m
ago"), the same poll instructions as today, and must NOT assert the process
is hung — the harness observed a quiet window, nothing more. The model
decides what to do with that observation (keep polling, investigate, or
abandon the attempt through its normal channels).

## 6. Config

| field | env | default | meaning |
|---|---|---|---|
| `dispatch_stall_seconds` | `SAG_DISPATCH_STALL_SECONDS` | `600` | stall window; `0` disables the stall clock (fixed-window behavior only — ablation/escape hatch) |
| `dispatch_soft_timeout_seconds` | (unchanged) | `900` | total window, now applying to the test tier only |
| `dispatch_poll_interval_seconds` | (unchanged) | `15` | poll cadence |

## 7. Non-goals

- The (a) bigger-caps vs (b) per-module-strategy decision for jobs larger
  than the wall clock.
- Any change to receipts, the obligation ledger, settlement, wait-at-close,
  or the §3.3 cap.
- Re-opening sealed runs, and any kill semantics.

## 8. Acceptance — mutation discipline (M-fences)

Each fence must be shown red under exactly its mutation. Time is driven
through injected `now`/`sleep` (the M9 lesson: no real sleeps, no hangs when
a mutation removes an exit condition).

1. Quiet stdout + growing build tree → no handoff before the wall guard.
   *(mutation: drop S2 → red)*
2. Fully stalled → handoff at `dispatch_stall_seconds` ± one poll interval.
   *(mutation: stall clock never fires → red; fixed-900 restored → red at 600)*
3. Stall, then resume, then stall → the clock measures the second stall from
   the resume, not from dispatch. *(mutation: no reset → red)*
4. The hold deadline and `_await_open_obligations` call the SAME margin
   helper — a wiring pin on the shared object, not two lookalike sums.
   *(mutation: inline a second computation → red)*
5. No deadline provider installed → old fixed-window behavior, not an
   unbounded hold. *(mutation: default-unbounded → red)*
6. A stall handoff does not kill: the handle is alive and pollable after.
   *(mutation: kill-on-handoff → red)*
7. A test-tier dispatch hands off at min(stall, total); a prerequisite-tier
   one ignores the total. *(mutation: tier collapse either way → red)*
8. The stall handoff text carries per-signal last-progress observations and
   never the bare claim "hung". *(mutation: conclusion wording → red)*
9. Held-to-completion result is field-for-field equal to a within-window
   completion. *(mutation: divergent shape → red)*

**Live anchor:** one rerun where a prerequisite build that previously handed
off at 900s is held to completion instead, and one induced stall (a
synthetic quiet command in a probe container) producing the §5 handoff text.
