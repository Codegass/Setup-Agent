# Plan 2 Verification — Native Executor Protocol, First Real-Model Contact

**Date:** 2026-07-26
**Code under test:** main @ Stage C merge (`sag_git_sha 6ca89e41…`), model `gpt-5.4-mini`, same image/prompt-model pins as all prior baselines.
**Sessions:** `logs/session_20260726_032045_95613` (commons-cli, `sag-plan2smoke-cli-afb0-r1`) · `logs/session_20260726_032047_95642` (tvm, `sag-plan2smoke-tvm-828d-r1`), both `--record`, containers kept.

## Verdict: Plan 2 acceptance PASSED

| Gate (Plan 2 Task 10) | Result |
|---|---|
| commons-cli retains canonical success | ✅ SUCCESS — 982 unique tests, 921 passed / 0 failed / 0 errors / 61 skipped — **digit-identical to the scheduler-protocol baseline** |
| TVM reaches ≥ Plan-1 smoke frontier | ✅ full deps ladder ran: root install → PyPI wall → local provider `3rdparty/tvm-ffi` built+installed → retry → same PEP 440 floor (`PYTHON_SETUP_FAILED`) |
| Zero provider 400s from message pairing | ✅ commons-cli 21 envelopes/21 results, **all 21 sha256 recompute byte-identically**, zero unanswered/orphan/double; TVM 80/80 paired |
| Zero deleted-fault-class strings in run logs | ✅ `SCHEDULER FAULT/CURRENT_PLAN/MALFORMED_PLAN` grep = 0 in both sessions |
| Event/telemetry contract | ✅ `feature_flags.native_loop: true`; no `scheduler_decision`/`planner_response` events; envelopes keyed by real provider `tool_call_id` (no `plan_index`); token CSV rows labelled `executor` |

## Wins

- **Echo tax gone, measurably.** commons-cli: ~2.5 min vs ~6 min under the
  dual-role protocol, same verdict, same test numbers. One LLM call per
  effective action.
- **Clone side-effect freedom works live**: TVM clone carried no embedded
  install failure; the venv ladder ran later behind the explicit
  provisioning path (apt `python3.12-venv` rung again).
- **The pairing invariant survived contact with a real weak model** across
  101 tool calls in two runs without a single repair or provider rejection.

## The new dominant failure shape (quantified input for Plan 3)

TVM: FAILED, 0 tests — same frontier as Plan 1, but the run *shape* changed.
Of 80 tool calls, **64 were phase/report ceremony churn**:
`phase:done` ×18, `phase:blocked` ×16, `phase:note` ×15, `report:generate`
×15 (~93 refusal/rejection strings in the log), against exactly one
`build(deps)`, one `build(compile)`, and **zero bash**. Freed from the
plan-lock, the weak model neither improvised repairs nor consulted anything —
it cycled claim variants against the gates until each phase closed.

This is precisely the behavior spec §3.2's mechanical guarantees exist to
break: **before-giving-up** (failure-outcome closure without a consult →
advisor redirect), **when-stuck** (LoopMemory second recurrence → advisor
redirect), and the per-phase advisor cap. The churn is not a Plan 2
regression — the old protocol produced the same FAILED verdict with less
noise because the scheduler suppressed retries — but it is the strongest
quantitative argument yet that Plan 3 is the load-bearing half of the
redesign.

## Notes

- `ControlReplayRunner` consumes recorded *fixtures* (ReplayHeader +
  events), not raw session logs; the equivalent live-data verification
  (dual-key hash walk + pairing) was performed directly and passed.
- commons-cli `build_evidence.compiled_classes` = 56 vs baseline 115:
  phase-timing difference (main classes at build-phase sealing vs
  install-time full compile). All 982 tests physically executed; not a
  defect.

## Plan 3 backlog (accumulated)

1. Advisor tool + four-layer timing guarantees (spec §3.2) — now with churn
   quantification as its baseline metric.
2. python deps ladder: post-provider `pip install -e . --no-deps` +
   explicit remaining deps when the local provider version sits below the
   declared floor (PEP 440 dev < release).
3. Physical-evidence enrichment trigger: replace the Gen 1 keyword substring
   trigger with an evidence trigger (tool name / EvidenceRole).
4. run-task mode lost the flat prompt's TASK PLAN block — decide replacement.
5. Report-layer fixes from the original plan (failed build cannot show
   "Blockers (0)"; recommendations from surveyed roots).
