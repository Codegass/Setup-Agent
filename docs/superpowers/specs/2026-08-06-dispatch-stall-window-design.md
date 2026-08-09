# Dispatch Stall Window — hold behind a controller barrier until terminal

**Date:** 2026-08-06
**Status:** revised 2026-08-08; controller-barrier policy approved
**Scope:** the dispatch-and-poll hold in `execute_command_with_soft_timeout`
(`src/sag/docker_orch/orch.py:1114`), its callers (maven/gradle build tools),
and the job barrier through obligation settlement. The 2026-08-08 revision
supersedes the old model-driven handoff and no-kill rules below where they
conflict.

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

**Stall clock:** any progress on either signal resets it. When it reaches
`dispatch_stall_seconds` (default **600**), the command enters controller-owned
diagnostic mode. The handle is preserved, the registered job barrier remains
active, and the model is not invoked.

The S2 probe must be one cheap container command merged into the existing
poll transport (the `STATE:`/`SIZE:`/`---TAIL---` probe grows a `PROGRESS:`
line) — one probe, one command (P3). The existing trusted-marker rule keeps:
only the head before `---TAIL---` carries markers, because build output in
the tail can contain anything.

**False-signal asymmetry, stated up front:** a missed progress signal causes an
early bounded diagnostic (not a model turn); a spurious progress signal causes
a longer hold, bounded by the wall-clock guard (§4.2). Diagnostics include CPU
and process state, so a quiet but CPU-active compile returns to normal waiting.
Cleanup is legal only after evidence seal under §4.

## 3. Two tiers: what the dispatch is FOR sets its ceiling

- **Prerequisite dispatches** — nothing downstream can proceed without them:
  effective action `deps`/`compile`, or `package`/`install`/`verify` with
  test execution skipped (`-DskipTests`, `-x test`). Hold while progress
  continues, **no total ceiling** except the wall-clock guard. A model turn
  during a prerequisite build has nothing useful to buy; the observed live
  behavior after a handoff is poll-burning.
- **Test-running dispatches** — the stall clock applies *inside* the existing
  total window. At stall (600s) or total
  (`dispatch_soft_timeout_seconds`, 900s, kept), the synchronous tool call may
  transition to a detached obligation, but controller mode remains a job
  barrier. The model cannot start a module run, claim partial, or perform
  unrelated work while that job is live or settlement is pending. Strategy
  changes become legal only after terminal evidence is settled or typed
  unpersisted.
- **Classification** is computed by the tool layer from the effective action
  and argv it already holds. Unclassifiable → test tier: refusing to guess
  must hold *less*, never more.

This tier boundary deliberately does not prejudge the open structural
decision for wall-clock-scale projects (bigger per-project caps vs.
per-module test strategy). Either resolution composes with this spec
unchanged.

## 4. Guards

1. **State ownership and cleanup.** A quiet-window crossing preserves the
   detached handle, writes the obligation, emits `job_stall_observed`, and
   may collect one bounded diagnostic bundle. “Quiet” is an observation, not a
   terminal verdict. A complete lightweight sample comes first: if progress or
   CPU activity is found, waiting resumes without a diagnostic or signal. Only
   repeated no-progress may seal evidence and clean the registered job PGID
   with `TERM`, a 120-second grace, then `KILL`. Keep the container and
   evidence. If cleanup itself fails, record `job_live_at_close`; never invent
   an exit.
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
   Reaching this wall guard is not evidence that a progressing job is stalled:
   the controller records the observed progress and closes honestly as
   `job_live_at_close` rather than diagnosing or killing it. The wall guard and
   quiet-window confirmation are distinct controller triggers.
3. **Held-to-completion equals within-window completion.** A dispatch that
   completes during a stall-window hold produces a result field-for-field
   identical to today's within-window completion. No consumer can tell how
   long the harness waited.

## 5. The controller event states observations, not conclusions

§3.9 spirit. `job_stall_observed` carries, per signal, when progress was last
observed ("stdout last grew 11m ago; no writes under target/ since 11m ago")
and references the bounded diagnostic bundle. It must not assert the process is
hung merely from elapsed time. The controller, not the model, owns poll,
diagnostic, settlement and cleanup until the barrier releases.

### 5.1 Detached-job identity and supervision

Every detached command runs under a fresh `setsid` session. The session leader
is both the recorded PID and PGID (`pid == pgid > 1`). A small supervisor stays
outside that group, waits for the session command, and atomically publishes its
exit marker. This split is intentional: `TERM -- -<pgid>` may stop the whole
job tree without killing the only process capable of recording how it ended.

The immutable dispatch handle and obligation carry `pid`, `pgid`, `pid_path`,
`pgid_path`, `identity_path`, and `process_identity_token`. The launcher token
is SHA-256 over the container boot id, PID, PGID, and launcher-time
`/proc/<pid>/stat` start ticks. A controller must refuse cleanup when any
identity field is absent or invalid, when PID and PGID do not name the same
registered session leader, or when a current kernel identity does not match
the launcher token. It reauthenticates immediately before both `TERM` and
`KILL`; a vanished leader never authorizes a later `KILL`, even if the numeric
PGID has reappeared. Process-name matching, project-name matching, `pkill`, and
`killall` are never admissible cleanup mechanisms.

### 5.2 One bounded bundle and typed observation

The first candidate quiet-window crossing whose initial lightweight sample is
complete and shows no progress writes exactly one durable bundle at
`.setup_agent/job_diagnostics/<job_id>/bundle.json`. Controller retries and
restarts reuse that bundle rather than resampling it. The bundle contains
bounded process/CPU/wait-channel, file-descriptor, socket, build-output,
report, JVM-thread, and log-tail observations plus a content hash for every
section. JVM collection makes at most one bounded `jcmd Thread.print` attempt,
with `jstack` only as its mutually exclusive fallback.

Every section has a hard byte cap and every potentially expensive subprocess
has a time budget. Socket state is sampled once, not once per PID. The later
progress comparison streams the complete recognized build/report tree into
its digest under a timeout; it does not take a traversal prefix. Each
sub-probe carries an explicit completeness result, and any timeout, truncation,
or malformed digest makes progress `unobservable`. An unobservable sample can
never contribute to a no-progress confirmation or authorize a signal.

The typed observation vocabulary is `cpu_active`, `io_wait`,
`thread_join_wait`, `deadlock_signature`, or `unknown`. These values are
derived only from the physical bundle. Project identity, repository name, and
historical anecdotes are not classifier inputs.

### 5.3 Confirmation, seal, and cleanup order

The controller takes one complete lightweight sample before the bundle and a
second sample after a short confirmation grace. Log growth, build/report
changes, child-process identity changes, or CPU tick growth reset the
no-progress condition and keep the barrier active. A terminal or
terminal-unpersisted job spends no further process wait. A separate
`wall_guard` trigger performs an honest terminal/progress observation but
cannot create a stall seal from elapsed time or one quiet sample.

Only repeated no-progress may create `seal.json`. Signal authority requires a
durable seal whose job id, PGID, diagnostic reference, and diagnostic
fingerprint match the registered handle. The exact autonomous sequence is:

1. publish and re-read the evidence seal and its referenced diagnostic bundle,
   including every raw-section hash;
2. reauthenticate the launcher token, then `TERM -- -<registered-pgid>`;
3. wait up to 120 seconds, polling only that group;
4. if that same group remains, reauthenticate the token again, then
   `KILL -- -<registered-pgid>`;
5. write `cleanup.json`, retain the container and every evidence file.

There is no mid-run human approval and no model/advisor turn inside this
sequence. A failed signal or a group still live after `KILL` remains the typed
fact `job_live_at_close`; it is never converted into an invented exit code.
Likewise, `TERM`/`KILL` does not itself make the obligation terminal. The
controller keeps a model-free reconciliation loop until the outside supervisor
publishes the terminal marker and WS2 settlement consumes it. A dead process
group whose marker never arrives is a typed marker-integrity/evidence failure,
not `job_live_at_close` and not an inferred exit code.

## 6. Config

| field | env | default | meaning |
|---|---|---|---|
| `dispatch_stall_seconds` | `SAG_DISPATCH_STALL_SECONDS` | `600` | stall window; `0` disables the stall clock (fixed-window behavior only — ablation/escape hatch) |
| `dispatch_soft_timeout_seconds` | (unchanged) | `900` | synchronous test-tool window before detached controller-barrier transition |
| `dispatch_poll_interval_seconds` | (unchanged) | `15` | poll cadence |

## 7. Non-goals

- Choosing the project's next repair after the current job becomes terminal;
  that is a later model-owned ActionIntent decision.
- Re-opening sealed runs or killing any process outside the registered job
  process group.
- Treating a quiet window by itself as a project failure or terminal state.

## 8. Acceptance — mutation discipline (M-fences)

Each fence must be shown red under exactly its mutation. Time is driven
through injected `now`/`sleep` (the M9 lesson: no real sleeps, no hangs when
a mutation removes an exit condition).

1. Quiet stdout + growing build tree → no stall diagnostic/barrier transition
   before the wall guard.
   *(mutation: drop S2 → red)*
2. Fully quiet → one diagnostic at `dispatch_stall_seconds` ± one poll
   interval, with the model still suspended.
   *(mutation: stall clock never fires or model regains control → red)*
3. Stall, then resume, then stall → the clock measures the second stall from
   the resume, not from dispatch. *(mutation: no reset → red)*
4. The hold deadline and `_await_open_obligations` call the SAME margin
   helper — a wiring pin on the shared object, not two lookalike sums.
   *(mutation: inline a second computation → red)*
5. No deadline provider installed → old fixed-window behavior, not an
   unbounded hold. *(mutation: default-unbounded → red)*
6. A quiet-window crossing first takes a complete lightweight sample; a
   CPU-active job returns to wait without a diagnostic. A complete quiet sample
   gets one bounded diagnostic and one confirmation sample.
   Confirmed no-progress cleanup targets only the registered negative PGID and
   follows identity reauthentication → TERM → 120s → reauthentication → KILL
   after evidence seal. Cleanup then waits for the supervisor marker rather
   than inventing process terminal evidence.
   *(mutation: immediate/broad kill or model handoff → red)*
7. A test-tier dispatch transitions to a detached barrier at min(stall,
   total); a prerequisite tier ignores the total. Neither transition gives the
   model an unrelated action turn. *(mutation: model turn → red)*
8. The stall event carries per-signal last-progress observations and never the
   bare claim "hung". *(mutation: conclusion wording → red)*
9. Held-to-completion result is field-for-field equal to a within-window
   completion. *(mutation: divergent shape → red)*

**Live anchor:** one rerun where a prerequisite build that previously handed
off at 900s is held to completion instead, and one induced stall (a synthetic
quiet command in a probe container) proving diagnostic → evidence seal →
job-PGID cleanup with zero intervening model turns.
