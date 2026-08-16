# Live proof — the repaired harness completes build AND test

**Date:** 2026-08-09
**Branch:** `AA/sag-evidence-control-repair` @ `fa8a908`
**Goal (owner's words):** 跑测试保证 SAG 能把 build 和 test 都搞定 — that is the
metric everything below serves.

## Results

| Project | Battery 8/8 (before) | Live proof 8/9 (after) | Evidence |
|---|---|---|---|
| commons-dbcp @ rel/2.14.0 | SUCCESS 1,596/1,605 | **SUCCESS — 1,605 executed / 1,596 passed / 9 skipped, 0 conflicts** | campaign-parity regression held; `sag-lp-commons-dbcp`, `logs/session_20260809_2*_80866` |
| cayenne @ 4.2.3 | PARTIAL, **0 canonical** (4,586 physically ran; receipt died on ARG_MAX) | **PARTIAL — 4,550 executed / 4,526 passed / 24 skipped / 0 failed, 8,655 compiled classes** | the multi-thousand-row receipt persisted through the chunked transport; honest conflicts (`build_modules_incomplete`, `reactor_scope_narrowed`); `sag-lp-cayenne` |

The cayenne row is the decisive one: the exact project class the battery
audit blamed on evidence-transport loss now carries its full test corpus
into the canonical verdict.

## The path there (one day)

1. **Checkpointed the loop's 13h of uncommitted work** (`09407f4`, +72.6k).
2. **Fixture migration 682 → 0** across ~46 files by 9 subagents; suite
   4,784 green (`fe817f7`).
3. **survey_fingerprint v2 pin completed** (plan Task 1.5, pre-D0 blocker):
   producer stamps, schema verifies by recomputation, binding pins went
   pairwise so domain-less projects reach real verdicts (`fe817f7`).
4. **D0 sealed 7/7** (`logs/d0-20260809-r13/`) after 15 runner-drift
   repairs — production refused every untrue certification along the way
   (`f049270`).
5. **Seven live defects** only real model runs could expose, each fixed
   with a fence the same hour:
   - bootstrap raced the authority install (`b7c4cfd`);
   - top-level `allOf` in the `project` wire schema — OpenAI refuses it
     (`9b74d27`);
   - the run-pin mirror was ONE BYTE off (heredoc trailing newline vs the
     published hash) and blinded the whole evidence ledger (`5edc01d`);
   - `test -d -- /path` — POSIX `test` has no `--`, so every bash call
     with a cwd was refused (`5edc01d`);
   - `ENV_EXECUTABLE_NOT_FOUND` now routes to
     `project(action='provision', packages=[...])` (`5edc01d`);
   - no-op convergence closed the test phase while the gate's promised
     phase-floor never ran — the floor now executes once before any
     convergence close, mutation-verified (`fa8a908`).
6. Suite at close: **4,796 passed / 1 skipped**, web 190/190.

## What this does and does not prove

- **Proved:** provision → analyze → build → test → honest verdict, end to
  end, on a fast green project (campaign parity) and on a large reactor
  whose receipts previously exceeded ARG_MAX. The claiming chain, chunked
  transport, obligation v3, controller barrier, and phase-floor all fired
  live.
- **Not yet proved:** the D1 ladder (18 targeted weak-model runs across
  the audit's eight failure classes — wrapper/unzip, dynamic JDK, samza
  stall diagnostics, camel settlement scale) and D2 (23×1 golden battery)
  remain per the plan. The two proofs here are necessary, not sufficient.

## Open follow-ups

- **D1 → D2** per `docs/superpowers/plans/2026-08-08-sag-evidence-control-repair-plan.md`.
- Task #38 cleanups: dead fail-closed fallbacks (build_tool, attempt_policy,
  physical_validator), the unformatted `toolchain_manager:resolve:194` log
  line, and the never-reproduced evidence_publications UnboundLocalError
  watch item.
  - **2026-08-15 — the UnboundLocalError watch item is CLOSED, refuted.** The
    sighting was against B1's in-flight copy, not the merged file. Evidence on
    the merged file: `mypy --enable-error-code possibly-undefined` reports
    nothing in `evidence_publications.py` (the whole-`src/sag` run's seven
    findings are all elsewhere and all correlated-control-flow false
    positives), and
    `test_the_concurrent_publication_path_binds_every_name_it_reads` drives the
    named path — eight threads publishing, revising, verifying and
    snapshotting one authority — tolerating only the typed CAS conflict and
    failing on any accident. Mutation-verified: an unbound name planted in
    `publish_revision` fails it.
  - **2026-08-15 — dead fail-closed fallbacks: two found and deleted**, both
    the same `effective_action or requested_action` read (`attempt_policy`,
    `physical_validator`); proof in the commit for task #38. `build_tool`'s
    uncovered fail-closed branches were examined and KEPT: they guard an
    `EnvOverlayStore` that really can raise, which no contract prevents.
- Maven version-requirement UX: the model's self-authored `[3.9,)` was
  refused against apt's 3.8.7; honest, but the refusal could name the
  registered version and the requirement's source.
- Evidence on disk awaiting hand-verification: `logs/d0-20260809-r*/`
  (~1.8 GB, gitignored), `logs/liveproof-20260809/`, containers
  `sag-lp-*`, plus the 25 retained battery/anchor containers.
