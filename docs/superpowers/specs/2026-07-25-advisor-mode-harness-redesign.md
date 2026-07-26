# SAG v2 — Advisor-Mode Harness Redesign

**Date:** 2026-07-25
**Status:** DRAFT — awaiting review (design not yet approved)
**Supersedes:** the protocol portions of
`docs/superpowers/plans/2026-07-24-weak-model-harness-reliability-fixes.md`;
absorbs its P0/P1 tool-layer items unchanged.
**Evidence base:**
`docs/superpowers/reports/2026-07-24-weak-model-cold-run-failure-analysis.md`
plus the 2026-07-25 architecture audit (summarized in Appendix A).

---

## 1. Why a structural redesign, not more point fixes

The 2026-07-24 cold runs (commons-cli success, bigtop partial, tvm failed)
were analyzed twice: once as individual failure chains (the existing report),
and once at the architecture level. The architecture audit found that the
failures are not independent defects but expressions of three structural
problems:

1. **Two coexisting design generations fight each other.** Gen 1 (the
   "babysitter" layer: surface-pattern evaluator, substring completion
   signals, tool-name repetition counting, automatic test-exclusion recovery,
   clone-time auto-install) contradicts Gen 2 (the "auditor" layer: evidence
   state, phase gates, LoopMemory, attempt policy, physical validator). The
   harness suggested and auto-executed `-Dtest=!` exclusions mid-run
   (`maven_tool.py:2005`, `tool_recovery.py:484`), then flagged the same
   exclusions as cheating in the final report (`physical_validator.py:4056`).
2. **The interaction protocol taxes exactly the model class it serves.** The
   THINK/ACTION split makes every effective action cost two LLM calls (the
   ACTION turn is a byte-exact echo of the resolved plan step — zero
   information, a real failure surface). Any failed observation invalidates
   the whole remaining plan, so fault-tolerant iteration ("try all four build
   islands regardless of individual failures") is inexpressible. Plan-time
   parameter schemas are not validated (bigtop lost cycles to `path` vs
   `working_directory`), while byte-exactness — which prevents no real
   errors — is. The custom text protocol (CURRENT_PLAN JSON, THOUGHT markers,
   single flat user message, no persistent system prompt) sits entirely
   outside the models' native tool-calling training distribution.
3. **The signal system is structurally biased toward premature closure.**
   Close-nudges fire on cheap substring evidence at the highest priority
   (COMPLETION SIGNALS never checks whether the matching output was a
   failure); repair signals require real state and get lower priority. Every
   gate rejection bundles a fully-spelled close call next to a three-word
   repair hint. In both failed runs the model had a wide-open root bash tool
   (empty blocklists) and never used it once — the one instruction that
   would have fixed both projects ("Use bash to install missing runtimes")
   lived only in the iteration-1 system prompt, which is overwritten from
   iteration 2 onward.

Model-quality counterfactual: a stronger model would likely have saved TVM
(one `apt install python3.12-venv` away) and partially saved bigtop — but it
could not repair the false `4/4` verdict (Groovy testcases filtered out of
the physical oracle) or the destructive auto-exclusions. Since SAG's product
is a **trustworthy verdict**, the harness is the primary owner. The redesign
therefore keeps the Gen 2 audit architecture, deletes the Gen 1 layer, and
replaces the interaction protocol.

## 2. The pattern adopted: advisor mode

Modeled on the Claude API advisor tool
(`https://platform.claude.com/docs/en/agents-and-tools/tool-use/advisor-tool`),
re-implemented inside the harness because SAG runs arbitrary models through
litellm (the Anthropic server-side tool is Claude-API-only):

- The **weak model (executor) owns the loop**: a native multi-turn
  function-calling conversation with full tool autonomy.
- Strategic intelligence is **a tool in its toolbox** (`advisor()`), not a
  supervisor above it. The executor decides *when* to consult; the harness
  decides *what the advisor sees* (always the full transcript — the executor
  cannot mis-cite evidence because it cites nothing).
- Advice is **advisory, not binding**: the executor is instructed to weigh
  it and adapt when it fails empirically.
- Call timing is steered by prompt guidance plus one narrow mechanical rule,
  calibrated per the official measured guidance (nudges help weak executors
  ~+7pp, hurt strong ones; mistimed nudges cost 3–4pp — which independently
  confirms what the COMPLETION SIGNALS misfires did to these runs).

This inverts the current architecture: today the planner is authoritative
and the executor is a serializer; in v2 the executor is authoritative and
the planner is consultable.

## 3. Design

### §3.1 Interaction protocol — single-executor native loop

**Delete** (protocol components, with their tests):
`reasoning_scheduler.py`, `current_plan.py`, the THOUGHT/CURRENT_PLAN
response parsing in `react_response_parser.py`, actor-mismatch validation,
`mode_prompts.thinking/action` in `react_engine.yaml`, plan placeholder
resolution (`{{step_N.*}}`), and the decorative plan fields
(`invalidate_on`, `expected_evidence`, `success_criteria` — the scheduler
never consumed them).

**Replace with:**

- One executor loop: each iteration is one LLM call carrying
  `messages=[system, ...conversation]` with native tool schemas
  (litellm; the existing `_tool_call_format_for_model` dispatch is reused).
- **Persistent system prompt on every request**: identity, tool guidance,
  current phase objective, communication rules, advisor timing block. This
  fixes the audit finding that the full system prompt rendered exactly once
  (iteration 1) and was overwritten thereafter.
- **Real conversation history**: assistant tool_calls and tool results
  accumulate verbatim. No per-iteration flat-message rebuild. Free-text
  assistant content is allowed and is simply the model talking (no THOUGHT
  protocol construct).
- Failure semantics: a failed tool call is one tool result; the model
  continues. Trying independent build islands sequentially despite failures
  is now the trivial default behavior.
- Iteration budget unchanged (one LLM call = one iteration; 150 cap).
- Tool-call validation: native function calling gives schema validation at
  dispatch. Wrong parameter names are rejected immediately with the correct
  name in the error (audited alias like `path` → `working_directory` may be
  auto-fixed with a recorded `ParameterFix`).

**Context structure:**

- **One conversation per phase.** On phase transition, a fresh conversation
  starts; the existing Gen 2 `HandoffProjection` is injected at the top of
  the new conversation. This preserves the current handoff machinery and
  keeps context small for weak models.
- Within a phase, when the conversation grows past a threshold, the oldest
  tool results are compacted deterministically (reusing `attempt_ledger`
  logic: one line per action with outcome and refs). Compaction must
  preserve **error tails** — the audit found the live view truncated
  failures head-only (the fatal stanza is at the tail) while only the
  ledger kept tails.

### §3.2 The advisor tool

A client-side tool named `advisor`, contract mirroring the official one:

- **No parameters.** On call, the harness assembles: the executor's full
  transcript (system prompt, conversation, tool results) **plus a
  deterministic evidence digest** — survey facts, what the current phase
  gate still lacks, untried independent islands, available repair ladders
  and their state, and (amended 2026-07-26) the latest test attempt's
  collection facts verbatim: command, scope, collected, selected,
  executed, collection errors. Without that last line a run whose every
  pytest attempt died in collection is indistinguishable, to the reviewer,
  from one that executed tests and failed them. The executor cannot choose
  or mis-cite what the advisor sees.
- **Default advisor: the same weak model, fresh context**, with an
  advisor-only system prompt ("you are a reviewer; strategic guidance only;
  you have no tools; keep it under ~80 words"). **Configurable** to a
  different (stronger) model per run config — enabling
  no-advisor / same-model / strong-model ablations without changing the
  harness. Advisor output hard-capped at `max_tokens=2048`.
- The advice returns as a tool result, framed as advisory ("weigh it
  seriously; adapt if it fails empirically; surface conflicts in a
  reconcile call").
- **Timing: how the executor knows when to consult.** The 2026-07-24 runs
  proved that prompt-only steering fails this model class (bash was fully
  open and instructed, and was never called once). Therefore the three
  critical consult moments are **mechanically guaranteed** and only
  optional mid-task consults rely on the model's judgment. Four layers,
  in increasing strength:
  1. *Tool description* carries the trigger conditions ("call before
     substantive work on a complex task, when stuck, before closing") —
     per official guidance, trigger conditions in the description itself
     measurably raise call rates. Present in every request.
  2. *Persistent system-prompt timing block* — survives every iteration
     under the new protocol (the old one rendered once and vanished).
  3. *Three mechanical guarantees* (evidence-triggered, not model-judged):
     - **Consult at phase entry** (amended 2026-07-26; supersedes
       *before acting*): on entering the build or test phase — including
       every re-entry after a repair loop — the HARNESS consults the
       advisor itself, before the executor's first turn of that phase. The
       consult is appended to the fresh phase window as a synthetic
       assistant tool_call (`advisor-entry-<n>`, native text
       `[harness] consulting the advisor at phase entry`) plus its tool
       result, following the forced-attempt precedent, so the pairing
       invariant and the evidence trail hold without a second code path.
       It counts against the per-phase cap; `advisor_mode="off"` and an
       exhausted cap skip it entirely, and any failure inside it degrades
       to no consult — the advisor never blocks a run.
       *Rationale (2026-07-26 post-acceptance audit):* the original
       mechanism refused the phase's first *state-changing* tool call and
       redirected it to `advisor()`. That cancelled correctly-planned
       work: in bigtop r1 it discarded two 4-island build batches (8
       wasted calls), and because phase re-entry resets the per-phase
       consult counter it re-armed the trap each time. A weak model cannot
       be required to re-remember a batch the harness threw away.
       Consulting at entry delivers the same advice, in the window before
       the model plans, at zero cancelled work.
     - **Before giving up:** a `phase(action='blocked')` or
       failure-outcome closure with no advisor consult since the most
       recent failure is rejected by the gate, redirecting to `advisor()`.
     - **When stuck:** on the second genuine recurrence detected by
       `LoopMemory` (same ActionKey + failure_signature, no state-vector
       progress), the harness replaces the repeat's tool result with an
       advisor redirect instead of executing the identical attempt again.
       LoopMemory is evidence-driven, so this trigger cannot misfire the
       way the deleted substring detectors did.
  4. *Over-calling protection:* the per-phase cap (below) bounds the
     opposite failure mode; the official turn-2 plain-text nudge is
     subsumed by guarantee (3a) and is not used. Additionally, a `phase(action='blocked')` or
    failure-outcome closure without an advisor consult since the last
    failure is rejected by the gate with the same redirect.
- **Cost guardrails:** per-phase advisor call cap (default 4). At the cap,
  further calls return a structured "cap reached, proceed with your best
  judgment" result. Advisor calls and timing are recorded in run telemetry.

### §3.3 Signal layer — Gen 1 deleted, all signals evidence-derived

**Delete** `agent_state_evaluator.py` entirely: completion-signal detector
(substring-based, never checks failure), task-completion variants,
tool-name repetition counter, idle-thinking detector (structurally
impossible in a native loop), ghost-state and context-switch reminders.

**Keep and promote to sole authority:**

- `LoopMemory` — recurrence judged by ActionKey + failure_signature +
  state-vector progress (the correct detector that the deleted evaluator
  never consulted). Its force-break remains.
- `TEST_ATTEMPT_REQUIRED` with harness-executed forced attempts (Gen 2,
  worked correctly in the cold runs).
- Phase gates and the physical validator (with §3.4 oracle fixes).

**Rejection-message standard** (applies to every gate/tool rejection):

- Must name a concrete, machine-derived repair action: missing OS package →
  the exact install invocation; venv failure → the next rung of the repair
  ladder; untried islands → the island list.
- Must never bundle a spelled-out closure call alongside a vague repair
  hint, and must never present `blocked`/`done partial` as the suggested
  next step while a mechanical repair remains untried.
- Must never assert evidence that does not exist (the TVM rejection called
  a clone-plus-failed-install a "real green build" because the workspace
  directory existed).

### §3.4 Tool layer — P0/P1 fixes folded in, hidden side effects removed

Carried over from the 2026-07-24 plan, unchanged in intent:

1. **Clone is side-effect free** — no dependency installation at clone
   time. Python provisioning runs after `analyze`, on the first
   deps/compile action, through the manifest-grounded preflight; if plain
   venv creation fails, the flow **must** enter the existing
   `ensure_venv_pip` ladder (today it returns before reaching it —
   `project_setup_tool.py:1172`). End-to-end Ubuntu 24.04 regression:
   clone → analyze → deps with missing `ensurepip`.
2. **Maven commands are argv** — per-token quoting at the final shell
   boundary; parse failure before the exit marker is a distinct launcher
   error, not a masqueraded inner failure. Regression tokens: `()`, `#`,
   commas, spaces, colons.
3. **No automatic failed-test suppression.** Build-phase promoted `install`
   may skip tests only explicitly and recorded; the test phase never
   converts failures into exclusions. The `-Dtest=!.../-DskipTests`
   suggestion strings are removed from tool output — tools must not coach
   evidence destruction.
4. **XML oracle counts every runtime testcase, including Groovy.**
   Bigtop-shaped regression: Groovy failures/errors must survive canonical
   aggregation; a narrowed later subset must not erase an earlier failing
   runner receipt without a versioned attempt identity.
5. **Gradle wrapper prerequisite preflight** — lazily verify/install
   `unzip` (SystemTool's own allowlist mapping only) when the wrapper needs
   to unpack a distribution; retry once.
6. **Primary test coordinate required** — one receipt at manifest
   `test_root`/`test_system` is mandatory; auxiliary islands add evidence
   but cannot substitute (fixes `attempt_policy.py` accepting any
   candidate).
7. **Build closure requires a current-phase build receipt** except for
   survey-proven no-target projects; missing venv modules and
   command-not-found are classified as local recoverable prerequisites,
   never external blockers.

Changed by the new protocol:

- The proposed mechanical "island attempt queue" (old P1-7) is dropped —
  the native loop makes per-island attempts natural. Its guarantee moves
  into the gate: closing the build phase while surveyed independent islands
  are untried is rejected, naming the islands (§3.3).
- Alias normalization (`path` → `working_directory`) happens at native
  dispatch with a recorded `ParameterFix`.

### §3.5 Cross-phase corrections (backward discoveries)

When a later phase discovers that an earlier phase's work is wrong — a
broken venv left by provision, wrong analyzer coordinates, a missing
toolchain — the run does **not** transition backward. Phases are focus
frames, not permission boundaries:

- **Repair in place.** The full tool surface is available in every phase:
  provision-scoped repairs (system package install, the python env repair
  ladder, re-provisioning a JDK) and `project(action='analyze')` re-runs
  are legal from any phase. Phase objectives direct attention; they do not
  gate tools. This codifies behavior that already exists informally (the
  bounded JDK-8 retry runs inside the build phase; attempt policy already
  forces re-analyze when survey candidates fail to resolve). Backward
  phase transitions are deliberately not supported: a weak model plus a
  reversible state machine invites phase ping-pong, and verdict evidence
  semantics would no longer be monotone.
- **Evidence supersession; claims stay append-only.** A correction writes
  a new verified fact superseding the stale one (facts already carry phase
  provenance and refs) plus a conflict entry naming what it invalidated.
  Earlier phase claims are never retroactively edited — the claim ledger
  is append-only for auditability. The sealed verdict is computed from
  final **physical** evidence, not from claims, so a stale "success"
  claim cannot survive into the verdict when the artifact it described is
  later found broken.
- **The advisor sees the contradiction.** The advisor's evidence digest
  includes handoff facts from earlier phases, so a consult at the stuck
  moment presents exactly the mismatch ("provision claimed venv ready;
  current failure shows pip missing"). Because "cannot proceed" triggers
  either the before-giving-up guarantee or the recurrence guarantee
  (§3.2), this scenario necessarily passes through an advisor consult —
  it does not depend on the model noticing on its own.
- **Oscillation guard.** In-place repairs are ordinary actions in
  LoopMemory; a repair that keeps failing hits the recurrence redirect,
  then force-break, then honest closure — bounded further by the
  iteration budget.
- **Out of scope:** changing the pinned repository ref or re-cloning at a
  different commit is a new run, not a correction.

### §3.6 Unchanged

Phase state machine (provision → analyze → build → test → report), the
`phase()` claim tool and gates, physical validator and verdict sealing,
evidence refs and output storage, detached job execution
(dispatch-and-poll), and the report layer — plus the report fixes from the
prior plan (a failed build cannot display "Blockers (0)"; recommendations
must use surveyed roots and bounded smoke coordinates). **The product
remains a trustworthy verdict; the audit architecture is preserved intact.**

### §3.7 Acceptance gate

Same pins as the prior plan (image `ubuntu:24.04@sha256:1f701c2d...`,
model `gpt-5.4-mini`, same repository refs), plus:

1. Targeted regressions for every §3.4 item; full local suite green.
2. `commons-cli`: retains canonical success, zero failed/error tests.
3. `bigtop`: primary `bigtop-data-generators` receipt; at least the
   historical 50-test anchor; no wrapper dependency failure; no
   Groovy-test disappearance from the sealed verdict.
4. `tvm`: venv repair ladder actually executes; at least one real
   build/deps attempt; native-unready testing stays bounded to the
   surveyed smoke coordinate.
5. Two repeats each for bigtop and tvm before declaring the stochastic
   weak-model path fixed.
6. New protocol invariants: zero occurrences of the deleted fault classes
   (SCHEDULER FAULT / MALFORMED_PLAN / ACTOR_MISMATCH) — they no longer
   exist; advisor call count/timing recorded in `run-pin.json` telemetry;
   ablation switch (no-advisor / same-model / strong-model) demonstrated
   functional.

## 4. Risks and mitigations

- **Weak-model tool-call formatting errors** (native function calling is
  more reliable than the custom protocol, not perfect): dispatch-layer
  schema errors return the corrected parameter name; audited alias
  normalization absorbs the common cases.
- **Advisor cost** (same-model advisor roughly doubles inference on
  consulted turns): per-phase cap, 2048-token output cap, and the official
  measured result that capped advice loses no quality.
- **Advisor under-calling by the weak executor**: the hard rule guarantees
  at least one consult before state-changing work; the timing block covers
  stuck/closure moments; both are calibrated from the official measured
  guidance rather than invented.
- **Regression surface of deleting the scheduler**: the phase gates,
  physical validator, and verdict sealing — the components that make the
  verdict trustworthy — are untouched; protocol deletion is covered by the
  acceptance gate's cold-run repeats.
- **Rewrite scope of `react_engine.py` (4,234 lines)**: implementation
  plan (next step) will stage it — native loop first behind the existing
  engine interface, Gen 1 deletion second, advisor third, tool-layer P0s
  in parallel — each stage with its own tests.

## Appendix A — architecture audit pointers (2026-07-25)

- Bash fully open to the model, zero uses in failed runs: `bash.py:26-27`
  (empty block/allow lists), `agent.py:417` (first registered tool);
  "Use bash to install missing runtimes" only in the once-rendered system
  prompt (`react_engine.yaml:54,89`; single call site
  `react_engine.py:2171`, overwritten at `:2369`).
- Echo-tax and plan-lock: `reasoning_scheduler.py:235-260`
  (byte-exact actor validation), `:69-84` (failure invalidates plan);
  `current_plan.py` (`invalidate_on` has zero consumers).
- Close-biased signals: `agent_state_evaluator.py:453-529` (completion
  detector, no failure check), `:387-451` (tool-name-only repetition);
  close/repair bundling in `phase_gates.py:459,557`.
- Failure head-only truncation: `tools/base.py:1291` (failures skip
  truncation) + `react_prompt_builder.py:237` (`content[:5000]` head).
- Cold-run evidence: sessions `20260724_020654_92495` (commons-cli),
  `20260724_021304_92677` (bigtop), `20260724_022039_92960` (tvm); live
  containers `sag-weakfix2-*` (bigtop lacks `unzip`; tvm venv is a
  pip-less husk at `/workspace/tvm/.venv`).
