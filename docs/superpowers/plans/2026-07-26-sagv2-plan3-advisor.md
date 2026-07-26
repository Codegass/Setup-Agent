# SAG v2 — Plan 3: Advisor Tool + Backlog Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the advisor tool with its four-layer consult-timing architecture (spec §3.2), close the accumulated tool-layer backlog, codify cross-phase evidence supersession (§3.5), and clear the way for the full §3.7 acceptance.

**Architecture:** The advisor is a client-side no-parameter tool whose "server side" is the harness: on call, the engine assembles the full phase transcript plus a deterministic evidence digest and consults a fresh-context LLM (default: the same weak model; configurable) with a hard 2048-token output cap. Three mechanical guarantees (before-acting, before-giving-up, when-stuck) are engine-level pre-execution redirects that flow through the same evidence-recording path as refusals, so the pairing invariant and audit trail hold. All guarantees are disabled under `advisor_mode: "off"` (the ablation switch §3.7.6 requires).

**Tech Stack:** Python 3.11+, litellm, pytest.

**Spec:** `docs/superpowers/specs/2026-07-25-advisor-mode-harness-redesign.md` §3.2, §3.5, §3.3.
**Baseline metric:** Plan 2 verification (`docs/superpowers/reports/2026-07-26-plan2-native-protocol-verification.md`) — TVM ceremony churn 64/80 calls is the number the advisor guarantees exist to reduce.
**Companion:** `docs/superpowers/reports/2026-07-26-engine-anatomy-map.md` — post-Plan-2 line numbers have drifted; anchors here were re-verified at `31129d0` where given, otherwise grep first.

## Global Constraints

- Never add `Co-Authored-By` trailers to git commits. NEVER use `git stash` in worktrees (shared refs/stash).
- Full suite green after every task (post-Plan-2 baseline: 2,448 passed / 1 skipped, env ±1 skip — measure your own clean baseline).
- The pairing invariant is law: every redirect/refusal is a tool result that flows through the existing evidence-recording path (`_execute_action_step`'s refusal precedent from Plan 2 Stage B).
- §3.3 rejection standard for every new message: name the concrete next action; never bundle a spelled-out closure while a mechanical repair or a consult is untried.
- The advisor NEVER blocks a run: provider errors during a consult return a successful-shape tool result saying "advisor unavailable — proceed with your best judgment". A broken advisor must degrade to Plan-2 behavior, not abort.
- Deliberately deferred (not in this plan): the run-task-mode TASK PLAN block (legacy mode, outside the research thesis; recorded here so nobody hunts for it).

## Task DAG

Stage A (parallel, disjoint files): T1 (deps `--no-deps` rung) ∥ T2 (evidence-triggered enrichment) ∥ T3 (report-layer honesty).
Stage B (one sequential lane): T4 (advisor client+tool+config+telemetry) → T5 (mechanical guarantees + timing blocks) → T6 (engine-level advisor test + ablation).
Stage C: T7 (cross-phase supersession codification). T8 (§3.7 acceptance) is run by the orchestrator after merge, not by a lane.

---

### Task 1: Python deps ladder — post-provider `--no-deps` rung

Both smoke runs died identically: the local provider `apache-tvm-ffi` builds
and installs as `0.1.13.dev47`, but PEP 440 orders `0.1.13.dev47 < 0.1.13`,
so the retried root install re-fails the same version-floor resolution
(`logs/session_20260726_032047_95642` main.log:15892, output
`output_6ca8d2557d5e`).

**Files:**
- Modify: `src/sag/tools/internal/python_tool.py` — the provider-recovery block (`_recover_local_provider` result handling, ~:672–735; grep `provider_recovery_attempted`)
- Test: `tests/test_provider_no_deps_rung.py` (new)

**Interfaces:**
- Produces: after a successful provider install whose root-install retry
  fails AGAIN with `No matching distribution found` naming the provider's
  own distribution, the ladder runs one more narrated rung:
  1. `{venv}/bin/python -m pip install -e . --no-deps`
  2. `{venv}/bin/python -m pip install <remaining declared deps>` — the
     manifest's `python_declared_dependencies` minus any requirement whose
     name matches an installed local provider distribution.
  Both commands and their outcomes append to the tool output (`$ cmd` +
  tail style already used in the block). Success of both → the deps action
  reports success with metadata `{"provider_no_deps_rung": true}`. Failure
  of either → honest failure, rung narrated.

- [ ] **Step 1: Write the failing test** — scripted-orchestrator style
(house pattern: first-match rules, commands recorded). Model the TVM shape:
root install fails with `No matching distribution found for apache-tvm-ffi>=0.1.13`;
provider install at `3rdparty/tvm-ffi` succeeds; retried root install fails
identically; then assert (a) a `pip install -e . --no-deps` command ran and
succeeded, (b) a follow-up install ran containing `ml_dtypes numpy typing_extensions`
but NOT `apache-tvm-ffi`, (c) the result is success with
`metadata["provider_no_deps_rung"] is True`, (d) the output narrates both
rung commands. Second test: `--no-deps` itself fails → overall failure, no
false success. Read the real block first for the exact seams (manifest
access for declared deps, `_effective_install_failure`, `_tail`).

- [ ] **Step 2: red** — `python -m pytest tests/test_provider_no_deps_rung.py -v` → FAIL (rung absent).
- [ ] **Step 3: implement** in the provider-recovery result handling: only
when `provider_failed is False` and the retried root install failure output
matches `No matching distribution found for <provider-distribution-name>`
(use the provider metadata the recovery already carries). Guard one-shot.
- [ ] **Step 4: green** — new file + `tests/test_python_tool.py tests/test_venv_repair_ladder.py` → PASS; full suite.
- [ ] **Step 5: Commit** — `git commit -m "feat: post-provider --no-deps rung when the local provider sits below the declared floor"`

---

### Task 2: Physical-evidence enrichment — evidence trigger, not keywords

The enrichment payload is Gen 2; its trigger is the last Gen 1 survivor
(keyword substring scan of observation text — anatomy map §6, engine
`_get_physical_validation_state`; the Java-artifact probe misfires on
Python repos, audit finding).

**Files:**
- Modify: `src/sag/agent/react_engine.py` — the trigger inside the enrichment path (grep `_get_physical_validation_state` and the keyword tuple `"build", "compile", "test"`)
- Test: `tests/test_evidence_triggered_enrichment.py` (new)

**Interfaces:**
- Produces: enrichment fires iff the observation belongs to a tool
  execution whose tool is in `{"build", "maven", "gradle", "python"}` —
  passed explicitly (the native observation path knows its execution; thread
  the tool name into `_add_observation_step` as an optional kwarg
  `source_tool: str | None = None` set by `_execute_action_step`). Keyword
  scanning of content is deleted. Non-build tools (bash, search, phase,
  file_io, project, report, advisor) never trigger the probe; build-family
  tools always do, regardless of output wording. The probe itself
  (`_get_physical_validation_state`) is unchanged.

- [ ] **Step 1: red test** — fake engine (house pattern from
`tests/test_native_dispatch.py`'s fixture): (a) a bash observation whose
text contains "build success" does NOT invoke the probe (spy on
`_get_physical_validation_state`); (b) a `build` observation with neutral
text DOES. Today (a) fires and (b) may not — both assertions red.
- [ ] **Step 2–4: implement, green** — full suite plus
`tests/test_native_loop_engine.py tests/test_react_engine_phase_wiring.py`.
- [ ] **Step 5: Commit** — `git commit -m "fix: physical-evidence enrichment triggers on evidence, not output keywords"`

---

### Task 3: Report-layer honesty

Spec §3.5-adjacent P2 items, carried since the original failure analysis: a
failed build displayed "Blockers (0)", and recommendations were generic
ecosystem advice contradicting the surveyed coordinates.

**Files:**
- Modify: `src/sag/tools/report_tool.py` — the attention/blockers section (~:4379–4428) and the recommendations section (grep `pip install -e .` / `Recommended` in the module)
- Test: `tests/test_report_honesty.py` (new)

**Interfaces:**
- Produces: (a) when the sealed verdict is `failed`, or build evidence
  outcome is failed, or unresolved conflicts exist, the report NEVER renders
  `"### Blockers (0)"` / "✅ No blocking issues" — it renders the failure as
  a blocker line derived from the sealed evidence (verdict outcome + the
  failing phase + its recorded failure signature). (b) The recommendations
  section derives from surveyed facts when present (manifest
  `python_install_commands` / `build_recommendation` / verified smoke
  coordinates) instead of generic `pip install -e . && pytest` prose.

- [ ] **Step 1: red tests** — drive the report section builders directly
(read the module for the section entry points; construct the minimal inputs
they take). Test (a): failed-verdict inputs → output contains `### Blockers (`
with count ≥ 1 and not `Blockers (0)`. Test (b): with a manifest carrying
surveyed install commands and a smoke coordinate, recommendations quote
them; without survey facts, recommendations fall back to naming the
evidence gap, not generic commands.
- [ ] **Step 2–4: implement, green** — plus `tests/test_report_metrics.py`-adjacent suites (grep which test files cover report_tool and run them).
- [ ] **Step 5: Commit** — `git commit -m "fix: reports derive blockers and recommendations from sealed evidence"`

---

### Task 4: Advisor client, tool, config, telemetry

**Files:**
- Create: `src/sag/agent/advisor.py`
- Modify: `src/sag/config/settings.py` (+ `from_env`), `src/sag/agent/react_llm.py` (one method), `src/sag/agent/agent.py` (tool registration ~:417–446 + run-pin ~:243/:292), `src/sag/agent/react_engine.py` (consult callback + telemetry + per-phase counter reset), `src/sag/config/prompts/react_engine.yaml` (advisor system prompt key)
- Test: `tests/test_advisor_tool.py` (new)

**Interfaces (consumed by Task 5/6):**

```python
# settings.py
advisor_mode: str = "same-model"      # "off" | "same-model" | explicit litellm model name
advisor_max_tokens: int = 2048
advisor_phase_cap: int = 4
# env: SAG_ADVISOR_MODE / SAG_ADVISOR_MAX_TOKENS / SAG_ADVISOR_PHASE_CAP

# react_llm.py
def get_advisor_response(self, messages: list[dict], *, model: str, max_tokens: int) -> str:
    """Plain completion, no tools, no thinking config; token usage tracked
    under the label 'advisor'. Raises on provider error (caller degrades)."""

# advisor.py
class AdvisorTool(BaseTool):
    name = "advisor"
    # description embeds the trigger conditions (official measured guidance):
    #   consult before substantive work on a complex task, when stuck
    #   (recurring errors, non-converging approach), and before claiming a
    #   phase done/blocked after failures. No parameters: the harness
    #   forwards your entire phase transcript and evidence digest.
    # get_parameter_schema → {"type": "object", "properties": {}, "additionalProperties": False}
    consult_fn: Callable[[], ToolResult] | None   # bound post-engine-construction
    def execute(self, **params) -> ToolResult      # delegates to consult_fn; unbound → failure "advisor not wired"

# react_engine.py
def consult_advisor(self) -> ToolResult: ...
@property
def advisor_telemetry(self) -> dict   # {"mode": ..., "calls": [{"iteration", "phase", "advice_chars", "outcome"}...]}
```

`consult_advisor` behavior (write exactly):
1. `advisor_mode == "off"` → success-shape result: "advisor is disabled for this run — proceed with your best judgment", metadata `{"advisor": "off"}`; not counted.
2. Per-phase cap reached → success-shape result: "advisor cap reached for this phase — proceed with your best judgment", metadata `{"advisor": "cap"}`; not counted.
3. Assemble messages:
   - system: new yaml key `advisor_system` — "You are a senior reviewer advising a setup agent. You see its full transcript and evidence. Give strategic guidance only — what to do next and why, under ~120 words. You cannot call tools. Never advise giving up while a mechanical repair is untried."
   - user: (a) the phase transcript — reuse `render_messages(system_prompt, self.steps)` and flatten every non-system message to `"<ROLE>: <content>"` lines (tool messages keep their clamped content); (b) an evidence digest section: `PhaseHandoff.project_for(current_phase, char_budget=...)` text + `_untried_island_targets()` output when non-empty + the most recent LoopMemory recurrence guidance text if armed.
4. `get_advisor_response(...)` with resolved model (`advisor_mode == "same-model"` → the action model via `capabilities_for`, else the configured name); wrap in try/except → on exception, success-shape "advisor unavailable — proceed with your best judgment" with metadata `{"advisor": "error"}`, counted as a call.
5. Success: ToolResult success, output = advice text, metadata `{"advisor": "advice", "advisor_call_index": n, "advisor_model": model}`. Count it; record telemetry; set the Task-5 state bits (`_advisor_calls_in_phase += 1`, `_had_failure_since_consult = False`, `_advisor_redirect_armed = False`).
6. Counter reset: `_advisor_calls_in_phase = 0` inside `_apply_phase_decision` (with the other per-phase counters) and at native-loop start.

Wiring: `AdvisorTool()` is appended to the tools list in `agent.py`
(always registered — mode "off" answers via `consult_advisor`); after the
engine is constructed, the agent binds `advisor_tool.consult_fn =
engine.consult_advisor` (find the exact spot where the engine already
receives tools; PhaseTool's construction shows the pattern for
engine-adjacent wiring). Run-pin: after the loop finishes,
`_write_run_pin` gains an `"advisor"` key carrying
`engine.advisor_telemetry` (thread it the same way `target_repo_sha`
reaches the pin — grep `_record_target_repo_sha`).

- [ ] **Step 1: red tests** (`tests/test_advisor_tool.py`): (a) unbound tool
→ failure "advisor not wired"; (b) mode off → success-shape disabled text,
zero LLM calls (spy); (c) cap: with `advisor_phase_cap=1`, second consult
returns cap text, LLM called exactly once; (d) provider exception → success-shape
unavailable text, telemetry outcome "error"; (e) happy path: advice text in
output, telemetry records `{"phase", "advice_chars"}`, `_advisor_consulted_since_failure`
flipped True; (f) messages assembly includes a flattened `TOOL:` line and
the handoff digest header (spy on `get_advisor_response` capturing messages).
Engine fixture: house pattern from `tests/test_native_dispatch.py`.
- [ ] **Step 2–4: implement, green** — plus full suite.
- [ ] **Step 5: Commit** — `git commit -m "feat: advisor tool — fresh-context consult with cap, telemetry and ablation switch"`

---

### Task 5: The three mechanical guarantees + timing surfaces

**Files:**
- Modify: `src/sag/agent/react_engine.py` (pre-execution shim in `_execute_action_step`, state bits, LoopMemory arming), `src/sag/config/prompts/react_engine.yaml` (system-prompt timing block)
- Test: `tests/test_advisor_guarantees.py` (new)

**Interfaces:**

```python
_ADVISOR_EXEMPT_TOOLS = frozenset({"advisor", "search", "phase", "report"})  # file_io/project/bash decided per-action below
_READONLY_BASH_PREFIXES = ("ls", "cat", "head", "tail", "grep", "find", "pwd", "wc", "which", "env", "echo")

def _is_state_changing(self, tool_name: str, params: dict) -> bool:
    # file_io: only action in {"read","list"} is exempt → write IS state-changing
    # project: action == "analyze" exempt; clone/provision state-changing
    # bash: exempt iff first token of command is in _READONLY_BASH_PREFIXES
    # everything else not in _ADVISOR_EXEMPT_TOOLS: state-changing

def _advisor_redirect_for_call(self, call: ToolCall) -> ToolExecution | None:
    """Pre-execution advisor gate. None when the call may proceed. All
    redirects disabled when advisor_mode == 'off' or the phase cap is
    exhausted (a capped advisor must not dead-lock the run)."""
```

Redirect rules (each produces a failure-shape tool result that flows
through the SAME evidence-recording path as `_refusal_for_call` — Plan 2
Stage B precedent; metadata `{"advisor_redirect": "<rule>"}`); texts obey
§3.3 (concrete action, no closure coaching):

1. **before-acting** — `phase_machine.current_phase in {"build", "test"}`
   and `_advisor_calls_in_phase == 0` and `_is_state_changing(...)`:
   "Consult advisor() before this phase's first state-changing action —
   your full transcript is forwarded automatically. This call was not
   executed."
2. **before-giving-up** — call is `phase` with `action == "blocked"`, or
   `action == "done"` with `outcome == "failed"`, and
   `_had_failure_since_consult` is True: "A failure occurred since your
   last advisor consult. Call advisor() before closing the phase on a
   failure — it may know a repair. This claim was not evaluated."
3. **when-stuck** — `_advisor_redirect_armed` is True and
   `_is_state_changing(...)`: "You are repeating an action that has already
   failed without progress. Consult advisor() before retrying. This call
   was not executed."

State bits:
- `_had_failure_since_consult`: set True whenever an executed tool result
  has `operation_outcome == failed` (hook where LoopMemory events are built
  in `_execute_action_step`); set False in `consult_advisor` success paths
  (advice/error both count as consulted) and on phase transition.
- `_advisor_redirect_armed`: set True when the LoopDecision produced by
  `_apply_tool_execution_loop_effects` reports a recurrence chain of ≥ 2
  without state progress (read `LoopDecision`/`RecurrenceRecord` fields —
  grep `loop_memory.py:201–263` for the exact names); cleared on consult
  and on phase transition.
- Rule ordering in `_execute_action_step`: advisor redirect check runs
  BEFORE `_refusal_for_call` (a redirected call must not leak into
  closed-evidence refusal wording), and never applies to the advisor tool
  itself.

Timing surfaces:
- yaml system prompt gains an `advisor guidance` section (rendered by
  `build_initial_system_prompt`; add to `REACT_ENGINE_REQUIRED_PROMPT_KEYS`):
  "You have an advisor() tool backed by a reviewer with your full
  transcript. Consult it before substantive work in a phase, when stuck,
  and before closing a phase after failures. Weigh its advice seriously;
  if a step it suggests fails empirically, adapt."
- `tests/test_system_prompt_native.py` gains an assertion that the rendered
  system prompt names `advisor()` (extend, don't rewrite).

- [ ] **Step 1: red tests** (`tests/test_advisor_guarantees.py`), engine
fixture as in Task 4: (a) build-phase `build` call with zero consults →
redirect, observation carries the redirect text, call not executed (spy);
after one consult the same call executes. (b) `phase blocked` after a
failed execution without consult → redirect; after consult → passes to the
tool. (c) armed recurrence → state-changing call redirected; consult
disarms. (d) `advisor_mode == "off"` → all three rules inert. (e) cap
exhausted → rules inert (no dead-lock). (f) read-only bash (`ls -la`) and
`project analyze` pass rule 1; `bash rm -rf` and `file_io write` do not.
(g) provision phase exempt from rule 1.
- [ ] **Step 2–4: implement, green** — plus `tests/test_native_dispatch.py
tests/test_native_loop_engine.py tests/test_system_prompt_native.py` and
full suite.
- [ ] **Step 5: Commit** — `git commit -m "feat: advisor mechanical guarantees — before-acting, before-giving-up, when-stuck"`

---

### Task 6: Engine-level advisor flow test + ablation demonstration

**Files:**
- Test: `tests/test_advisor_engine_flow.py` (new)

Scripted-LLM engine run (harness from `tests/test_native_loop_engine.py`,
including its load-bearing `_missing_required_test_attempt` stub):

- [ ] **Step 1: write the test** — script a run where the executor (a) hits
the build phase and tries `build(action='compile')` first → gets the
before-acting redirect as a tool message; (b) calls `advisor()` → scripted
advisor client returns fixed advice; (c) retries `build` → executes; (d)
after a scripted failure, tries `phase(action='blocked')` → before-giving-up
redirect; (e) consults again, then closes honestly. Assertions: the exact
redirect metadata sequence (`before-acting`, `before-giving-up`), advisor
telemetry has 2 calls with correct phases, pairing invariant holds (zero
`_repair_pairing` mutations), and the run reaches COMPLETED. Second test:
identical script with `advisor_mode="off"` → zero redirects, zero advisor
LLM calls, run still completes (the ablation demonstration §3.7.6).
- [ ] **Step 2–3: red on missing wiring → fix wiring only (no new features), green** — plus full suite.
- [ ] **Step 4: Commit** — `git commit -m "test: advisor guarantees drive a full engine run; ablation switch demonstrated"`

---

### Task 7: Cross-phase evidence supersession (§3.5 codification)

**Files:**
- Modify: `src/sag/agent/evidence_state.py` (only if supersession lacks a conflict record — read first)
- Test: `tests/test_cross_phase_supersession.py` (new)

- [ ] **Step 1: read `RunEvidenceState.register_fact` / `ingest_tool_result`**
and determine what happens today when a later phase re-registers a fact key
verified by an earlier phase (e.g. `provision.venv_ready` re-verified during
build after an in-place repair).
- [ ] **Step 2: red test** — (a) re-registering a fact from a later phase
supersedes the value AND leaves an auditable trace: either the fact's
provenance updates to the later phase/ref while a conflict-or-history entry
names the superseded value, or — if the mechanism already exists — the test
simply locks it. (b) The claim ledger stays append-only (no mutation of
earlier phase claims). (c) A repair-typical flow (fact verified in
provision → invalidated → re-verified in build) leaves the final state
reflecting the build-phase value.
- [ ] **Step 3: implement the smallest mechanism that makes Step 2 pass**
(a `superseded` history list on the fact record, or a conflict entry —
match whatever `evidence_state.py` already shapes; do NOT invent a parallel
store).
- [ ] **Step 4: green** — plus `tests/test_evidence_ingestion.py tests/test_explicit_evidence_architecture.py` and full suite.
- [ ] **Step 5: Commit** — `git commit -m "feat: cross-phase fact supersession carries an auditable trace"`

---

### Task 8: §3.7 acceptance (orchestrator-run, NOT a lane)

After Stage A/B/C merge: commons-cli ×1, bigtop ×2, tvm ×2 (all `--record`,
same pins), plus one tvm run with `SAG_ADVISOR_MODE=off` as the live
ablation comparison against the churn baseline (64/80). Gates: spec §3.7
items 2–5 plus advisor telemetry present in run-pin, ceremony-churn share
materially below 64/80 on the advisor runs, and replay pairing/hash walk
green on every fresh transcript. Results go in a verification report under
`docs/superpowers/reports/`.
