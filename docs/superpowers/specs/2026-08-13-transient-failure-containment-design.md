# Transient-failure containment — a blip is not a verdict

**Date:** 2026-08-13
**Status:** design decision, ready to implement
**Scope:** task #45. The two abort paths D2's sealed phase records actually
convict: one LLM 5xx killing tapestry-5 mid-recovery, and one
`repair_assessment_persist_failed` killing rocketmq while the model's blocked
claim was factually correct.

## 1. What stays fatal, deliberately

`_fatal_harness_control_failure` today carries three families. Two are
INTEGRITY failures — `job_barrier_integrity_failure` (a malformed detached
result left no ledger to sweep) and `repair_dispatch_integrity_failure` (a
repair dispatch whose evidence cannot be trusted). Further model turns after
either could act on, or manufacture, corrupted evidence. **They keep exactly
today's abort semantics.** This spec touches nothing about them.

## 2. LLM provider errors: retry the transient, abort the deterministic

`react_engine.py:3326-3335` aborts the run on the FIRST exception from
`get_native_turn`. D2 tapestry-5: one `litellm.InternalServerError`, no retry,
run dead in the analyze phase two turns after the model had self-corrected.

**Contract:**

- Transient classes retry with backoff; deterministic classes abort at once.
  - Transient: `InternalServerError`, `ServiceUnavailableError`,
    `RateLimitError`, `APIConnectionError`, `Timeout` (matched via litellm's
    exception types, defensively — an unknown exception class is treated as
    deterministic, fail-closed).
  - Deterministic: `BadRequestError` (the wire-schema incident proved these
    reproduce byte-for-byte), `AuthenticationError`, context-window errors,
    and anything unrecognized.
- Bounds: 3 retries, backoff 5s / 15s / 45s, ~65s worst case against a 7,200s
  wall clock. The wall-clock guard still runs first on every iteration, so
  retrying can never extend a run past its cap.
- Each retry is logged with the attempt number; exhaustion aborts with the
  same reason text as today plus the attempt count — the abort stays honest,
  it just stops being trigger-happy.
- The sleep is injected (house Fuse pattern) so tests never wait.

## 3. Persist exhaustion: an honest phase close, not a run abort

`_record_rejected_completion_repair` already retries `write_assessment` twice;
on exhaustion it currently sets `_fatal_harness_control_failure =
"repair_assessment_persist_failed"` and the loop aborts the RUN.

The fail-closed instinct is right — the judge's ceiling constraint could not
be made durable, so the phase must not continue — but aborting the run
destroys report delivery and brands the model's (often correct) final state
"aborted". The containment that preserves both:

- On persist exhaustion, the engine CLOSES THE CURRENT PHASE immediately:
  a harness-owned blocker (`repair_assessment_persist_failed`, category
  `harness_control`) is recorded, the phase closes `blocked` through the
  normal gate path, and transition policy routes it (dependents skip exactly
  as they do for any blocked build).
- The risk window is zero by construction: the un-persisted ceiling could
  only be exceeded by further model turns IN that phase, and the phase ends
  in the same breath.
- The run then proceeds to evidence close and report delivery as any
  blocked-phase run does: `dependents_skipped`, never `aborted`. The verdict
  seals from whatever evidence exists.
- Two persist attempts stay two: the failure mode on rocketmq was not
  slow-storage flakiness (that investigation stays open under this task),
  and a phase close is the correct response whether the third attempt would
  have succeeded or not.

## 4. Acceptance

- A `get_native_turn` that raises `InternalServerError` twice then succeeds:
  the run continues, no abort, two backoff sleeps recorded.
- A persistent `InternalServerError`: abort after 3 retries, reason names the
  count.
- A `BadRequestError`: immediate abort, zero retries.
- Persist exhaustion: the phase closes `blocked` with the harness-owned
  blocker, the run terminates `completed` with `dependents_skipped`, the
  report delivers, and no `aborted` appears anywhere in the sealed records.
- Integrity failures: unchanged fences stay green untouched.
