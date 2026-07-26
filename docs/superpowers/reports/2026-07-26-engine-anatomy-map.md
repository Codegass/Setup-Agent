# ReAct Engine Anatomical Map — protocol-replacement surface

**Date:** 2026-07-26. Produced for Plan 2 (native executor loop,
spec §3.1/§3.3) by a thorough read of the engine at commit `a0d51e6`.
Line numbers are anchors, not gospel — implementers verify with grep before
editing (files drift).

All refs are `file:line` in the repo root. Engine = `src/sag/agent/react_engine.py` (4,234 lines).

---

## 1. MAIN LOOP

### Entry points
| Method | Lines | Notes |
|---|---|---|
| `run_setup_loop` | 2093–2109 | setup mode; requires `phase_machine`; asserts `RunTermination` return |
| `run_react_loop` | 2110–2126 | legacy `run --task`; asserts `bool` return |
| `_run_react_loop` | 2128–2422 | the only real loop |

Callers: `agent.py:1177`, `agent.py:1199` (setup), `agent.py:922` (run_task).

### Pre-loop setup (2128–2188)
- 2134–2136 `max_iter` / `self._run_max_iterations`
- 2141 `phase_mode = completion_mode == "setup" and self.phase_machine is not None`
- 2142–2151 **rebuilds a fresh `ReasoningScheduler`** per run, sets `_scheduler_active`
- 2152 `self._scheduled_turn = None`
- 2155–2164 window init: `self.steps = [self._phase_intro_step()]`, journal dirty flags, `_start_phase_branch()`
- 2167 `prompt_builder.invalidate_trunk_cache()`
- 2170–2181 **`build_initial_system_prompt(...) + "\n\n" + initial_prompt`** → `current_prompt`. This is the *only* system-prompt render; it is overwritten at 2369 (`build_next_prompt`) from iteration 2 onward — the audit finding in spec §3.1.
- 2183–2184 `state_evaluator.completion_mode` save/set
- 2186–2187 `run_started_at`, `wall_clock_cap` (`config.max_wall_clock_seconds`, default 7200)

### One iteration (2190–2397)
| # | Line(s) | Action |
|---|---|---|
| 1 | 2190 | `while self.current_iteration < max_iter` |
| 2 | 2191–2200 | wall-clock cap → `abort("wall clock cap exceeded")` / `False` |
| 3 | 2205–2207 | `_enforce_phase_floors()` (1872–1974); if it completes the machine → `_close_flow(COMPLETED)` |
| 4 | 2209–2211 | `current_iteration += 1`, `_phase_iterations += 1` |
| 5 | 2214 | `token_tracker.set_iteration(...)` |
| 6 | 2217–2218 | **`_should_use_thinking_model()`** (2807–2874) → calls `scheduler.next_turn()` (2811), emits `scheduler_decision` (2812), stores `self._scheduled_turn` → mode = THINKING\|ACTION |
| 7 | 2221–2237 | `prompt_builder.build_mode_prompt(current_prompt, mode, planned_step=turn.step, reasoning_reasons=turn.reasons, scheduler_fault=str(turn.fault))` (`react_prompt_builder.py:304–380`) |
| 8 | 2238 | **`llm_client.get_response(wrapped_prompt, mode)`** → normalized ReAct *text* |
| 9 | 2240–2247 | empty response → `abort("LLM response unavailable")` |
| 10 | 2249–2254 | **`response_parser.parse(response, model_used, was_thinking_model)`** → `list[ReActStep]` |
| 11 | 2256–2262 | **`_prepare_scheduler_steps(response, parsed_steps, turn)`** (2701–2759): THINK → parse `CurrentPlan`, accept/reject; ACTION → byte-exact `validate_actor_action` |
| 12 | 2264–2268 | no steps → `continue` only if scheduler is None (with scheduler, falls through to execute an empty list) |
| 13 | 2271 | **`_execute_steps(parsed_steps)`** (3331–3557) |
| 14 | 2273–2288 | phase mode: `_handle_phase_signals(parsed_steps)` (1660–1727); if `phase_machine.is_complete` → `_close_flow(COMPLETED)`; else `_maybe_nudge_phase_done()` (1843–1870) |
| 15 | 2290–2302 | `state_evaluator.evaluate(...)` → `_add_system_guidance(...)` (Gen 1) |
| 16 | 2304–2314 | `state_analysis.is_task_complete` → ignored in phase mode (2306–2310) |
| 17 | 2316–2336 | no-physical-progress guard (`_check_progress_after_task`, 604–615) |
| 18 | 2343–2366 | attempt-ledger compaction: `compact_steps(tail, keep_recent=30)` → `self.steps = [intro, ledger_step] + kept` |
| 19 | 2369–2380 | **`build_next_prompt(steps=…)`** → new `current_prompt` (flat text rebuild) |
| 20 | 2382–2387 | `_record_context_journal(ledger, n_compacted, len(parsed_steps), len(current_prompt))` (1771–1807) |
| 21 | 2391–2397 | `steps_since_context_switch += 1` when any ACTION step |

Exits: 2399–2404 budget exhausted → `abort("iteration budget exhausted")`; 2406–2410 `KeyboardInterrupt` → `cancel`; 2411–2417 exception → `abort`; 2419–2422 `finally` resets `_scheduler_active`, `_scheduled_turn`, evaluator mode.

### Numbered call-graph: one full THINK → ACTION → observe cycle

**Turn A (THINK)**
1. `_should_use_thinking_model` :2809 → `_active_reasoning_scheduler` :2685 → `scheduler.next_turn()` (`reasoning_scheduler.py:126–187`) → `SchedulerTurn(mode=THINK, reasons=…)`
2. `_emit_control_scheduler_decision` :2439 → control event `scheduler_decision`
3. `build_mode_prompt` (`react_prompt_builder.py:325–345`) injects `REASONING TRIGGERS:` + `FAULT:` + `mode_prompts.thinking` (`react_engine.yaml:371`)
4. `llm_client.get_response(..., THINKING)` → `_build_request_params` (`react_llm.py:234–289`) — **no tools schema for THINKING** (247) — single `[{"role":"user"}]` message (243)
5. `response_parser.parse` (`react_response_parser.py:18–83`) → THOUGHT steps only
6. `_prepare_scheduler_steps` :2715–2739 → `CurrentPlan.from_thinking_response(response)` (`current_plan.py:145`) → `_canonicalize_scheduler_plan` :2761 → `scheduler.accept_plan` → `_emit_control_planner_response` :2449 (or `_emit_control_planner_rejection` :2462)
7. `_execute_steps` :3331 appends THOUGHT to `self.steps`, emits `AGENT_THOUGHT`, writes branch history :3354–3365
8. `_handle_phase_signals` → no signal → `_maybe_nudge_phase_done`
9. `state_evaluator.evaluate` → maybe guidance step
10. compaction + `build_next_prompt` + journal

**Turn B (ACTION)**
11. `scheduler.next_turn()` → `current_plan.resolve_step(idx, prior_results, available_tools)` (`current_plan.py:204`) → `ExecutablePlanStep`; `_active_step` set
12. `build_mode_prompt` :354–370 renders `STEP/TOOL/PARAMETERS/EXPECTED_EVIDENCE/SUCCESS_CRITERIA` — the byte-exact echo contract
13. `get_response(..., ACTION)` → tools schema attached (`react_llm.py:247–248, 274–282`) → `_handle_function_calling_response` (`react_llm.py:351–392`) **flattens native tool_calls back into `ACTION:`/`PARAMETERS:` text**
14. `response_parser.parse` → one `StepType.ACTION` step
15. `_prepare_scheduler_steps` :2740–2758 → `_canonicalize_scheduler_action` :2781 (ToolParameterNormalizer) → `scheduler.validate_actor_action` (`reasoning_scheduler.py:235–260`); mismatch → `SCHEDULER FAULT` guidance :2754 and `return []`
16. `_execute_steps` ACTION branch :3367 → `_build_tool_call_from_step` :2925 → `ToolCall`
17. Guards :3390–3394 → `_evidence_execution_closed` :877 / `_report_execution_allowed` :865
18. `_execute_tool_call` :2935 → `ToolOrchestrator.execute` (`tool_orchestration.py:378`) → `_execute` (389–753)
19. `_record_execution_bundle` :3396 (impl 2996–3046) → per-leaf `_record_tool_execution` :745–863 → **evidence ingestion into `RunEvidenceState`**
20. `step.tool_result = result` :3399; control envelope resolve :3400–3413; `_emit_control_tool_result` :3414 (impl 2553)
21. `_apply_tool_execution_loop_effects` :3422 (impl 3239–3271) → `_loop_event_for_execution` :3048 → `LoopMemory.observe` → `_emit_control_loop_decision` → `_loop_guidance` guidance step
22. `_add_observation_step(execution.observation_text)` :3429 (impl 3824–3851) → `_get_physical_validation_state` :3961 + `_enrich_observation_with_physical_state` :4005 → OBSERVATION step appended
23. `scheduler.observe_result(result)` :3433 (`reasoning_scheduler.py:262–303`) → sets next-turn reasoning triggers
24. `TEST_ATTEMPT_REQUIRED` check :3460–3466 → `_force_required_test_attempt` :1241
25. branch-history write :3469–3540 (includes ad-hoc `output_storage.store_output` at :3492)
26. loop-driven phase close :3542–3549 → `_close_phase_for_loop` :3273; `phase_signal` break :3550–3556
27. back in the loop: `_handle_phase_signals` :2278 → gate → `_apply_phase_decision` :1552 → `machine.apply` + **`self.steps = [self._phase_intro_step()]`** :1583

---

## 2. COUPLING INVENTORY

### 2a. `reasoning_scheduler` / `SchedulerTurn` / `ReasoningTrigger`

| Ref | Lines | What | Verdict |
|---|---|---|---|
| import | 68–73 | `ReasoningScheduler, ReasoningTrigger, SchedulerMode, SchedulerTurn` | **DELETE** |
| ctor | 344–349 | builds scheduler, `_scheduler_active`, `_scheduled_turn` | **DELETE** |
| loop init | 2142–2152 | per-run scheduler rebuild | **DELETE** |
| `_active_reasoning_scheduler` | 2685–2689 | activity predicate | **DELETE** |
| `_request_scheduler_reasoning` | 2691–2699 | trigger fan-in | **DELETE (shim)** — but its 6 call sites are semantic events, see below |
| `_prepare_scheduler_steps` | 2701–2759 | plan accept / actor byte-match | **DELETE** |
| `_canonicalize_scheduler_plan` | 2761–2779 | plan param normalization | **DELETE** |
| `_canonicalize_scheduler_action` | 2781–2805 | `ToolParameterNormalizer.resolve_legacy_alias` + `validate_and_fix` | **RE-HOME** — this is the alias/param normalization the spec wants at native dispatch (§3.4); also used by forced test attempts at :1258 |
| `_should_use_thinking_model` | 2807–2874 | mode selection | **DELETE** (whole dual-role concept) |
| `_emit_control_scheduler_decision` | 2439–2447 | control event | **DELETE** (event kind `scheduler_decision`) |
| `_emit_control_action_envelope` | 2474–2512 | **hard-gated on `_active_reasoning_scheduler() is not None` (:2482) and on `step.plan_index` (:2487–2496, :2506)** | **RE-HOME — highest risk.** Without a scheduler this returns `None`, and `_emit_control_tool_result` :2557 early-returns on falsy `envelope_id` → **all `tool_result` control events vanish**, which silently breaks replay + A/B collection |
| `scheduler.observe_result` | 3431–3433 | post-observation trigger | **DELETE**; the `else:` branch 3434–3455 (legacy cadence) also dies |
| `_request_scheduler_reasoning(PLAN_EXHAUSTED)` | 1356 | after forced test attempt | protocol-specific → delete |
| `…(PHASE_CHANGE)` | 1562 | in `_apply_phase_decision` | protocol-specific → delete (new protocol starts a fresh conversation) |
| `…(GATE_REJECTION)` | 1644, 1698 | repair/terminal gate rejection | **RE-HOME as behavior**: gate rejection must still surface to the model as a tool result / message |
| `…(LOOP_BREAKER)` | 3247, 3269 | `force_thinking_next`, `decision.request_thinking` | **RE-HOME**: becomes the advisor redirect (§3.2 guarantee 3) |
| `_force_thinking_next` / `_force_thinking_after_success` | 400, 405, 2822–2832, 3444–3455, 3248, 3270 | dual-role flags | **DELETE** |

### 2b. `current_plan`

| Ref | Lines | Verdict |
|---|---|---|
| import `CurrentPlan, PlanFault, PlanFaultCode` | 38 | **DELETE** |
| `_prepare_scheduler_steps` uses | 2718–2730 | DELETE |
| `_canonicalize_scheduler_plan` | 2761–2779 | DELETE |
| `_emit_control_planner_response(plan)` | 2449–2460 | DELETE (event kind `planner_response`) |
| `_emit_control_planner_rejection` | 2462–2472 | DELETE |
| module itself | `current_plan.py` (451 lines): `_PLACEHOLDER` :115, `_FULL_PLACEHOLDER` :116, `resolve_step` :204, decorative `invalidate_on` :128 / `expected_evidence` :70 / `success_criteria` :71 | **DELETE whole file** per §3.1 |

### 2c. `react_response_parser`

| Ref | Lines | Verdict |
|---|---|---|
| import | 66 | DELETE |
| ctor `self.response_parser = ReActResponseParser(...)` | 518 | DELETE |
| `response_parser.parse(...)` | 2250–2254 | DELETE — replaced by reading `message.tool_calls` directly |
| `react_response_parser.py:18–153` | THOUGHT/ACTION/PARAMETERS split, OBSERVATION stripping (`_strip_model_observations` :85), fallback "treat as thought" :58–81 | DELETE whole file |
| `react_llm.py:351–392` `_handle_function_calling_response` | flattens native tool_calls → text | **DELETE** (this is the lossy step: `tool_call.id` is discarded, so no `role=tool` correlation is possible today) |
| `react_llm.py:425–550` `_try_parse_json_function_calls` / `_parse_json_tool_object` / `_infer_tool_from_parameters` | text-protocol salvage heuristics | DELETE (or keep only for `tool_call_format == "prompt"` models) |

### 2d. `agent_state_evaluator`

| Ref | Lines | Verdict |
|---|---|---|
| import | 28 | DELETE |
| ctor | 426 | DELETE |
| `state_evaluator.physical_validator = …` | 498 | DELETE |
| `state_evaluator.phase_machine_active = …` | 503 | DELETE |
| `completion_mode` save/restore | 2183–2184, 2422 | DELETE |
| **`state_analysis = self.state_evaluator.evaluate(...)`** | 2291–2296 | **DELETE — the single evaluate call site** |
| guidance dispatch | 2299–2302 | DELETE |
| `is_task_complete` handling | 2304–2314 | DELETE |
| `src/sag/agent/__init__.py:4, 18–20` re-exports | | DELETE |

### 2e. Protocol-NEUTRAL logic that must be preserved and re-homed

| Concern | Lines | Note |
|---|---|---|
| Iteration budget | 2134–2136, 2190, 2209, 2399–2404 | 1 LLM call = 1 iteration; unchanged |
| Wall clock | 271–284 (`wall_clock_exceeded`), 2186–2187, 2191–2200 | pure function, reusable |
| Phase floors | 1024–1033 (`_phase_budget_numbers`), 1872–1974 | uses `effective_phase_floor`; unchanged |
| Token tracking | 511, 2214, 4202–4234 (`_export_token_usage_csv`), all 8 exit paths | unchanged |
| Control events | 2429–2683 | keep all *except* `scheduler_decision` / `planner_response`; **`action_envelope` needs a new identity source** (see §2a) |
| Journal writes | 1771–1807, 2382–2387; `context_journal.py:26–50` | segments schema is step/ledger-shaped → needs a message-array analogue |
| Forced test attempts | 1035–1362 (`_missing_required_test_attempt` … `_force_required_test_attempt`), call sites 1610–1625, 1701–1712, 1888–1908, 3283–3292, 3460–3466 | **preserve entirely**; only :1258 (`_canonicalize_scheduler_action`) and :1356 (`PLAN_EXHAUSTED`) need re-pointing |
| LoopMemory consultation | 3048–3085, 3181–3271, 3273–3329 | preserve; `request_thinking` semantics become advisor redirect |
| Physical-evidence enrichment | 3824–3851, 3961–4051 | keyword-triggered observation enrichment; protocol-neutral but attaches to `_add_observation_step` — must move to the tool-result message builder |
| Output storage refs | 415–423 (ctor), 3489–3510 (ad-hoc branch-history store), `_record_tool_execution` :798–813 (`attach_durable_output_ref`), `_output_refs_from_text` :3559 | preserve |
| `successful_states` | 389–401, 3572–3753, 3755–3807 | preserve (used by prompt builder, `_expects_build_artifacts`, orchestrator) |
| Archive counters / summary | 1729–1769, 4086–4158 | `self.steps`-shaped — must be recomputed from the new conversation representation |
| `recent_tool_executions` | 384–385, 3809–3822 | consumed only by the deleted evaluator's `_check_repetitive_execution` and by `ToolOrchestrator._get_repetition_level` (`tool_orchestration.py:871`) — audit before deleting |

### 2f. Dead code found (delete free)
- `_add_completion_guidance` :3853–3880 — **zero call sites**
- `_check_completion_suggestion` :3882–3921 — **zero call sites**
- `_has_report_been_generated` :3923–3933 — only called by the two above

---

## 3. TOOL EXECUTION PATH

```
ReActStep(ACTION)
 → _build_tool_call_from_step            react_engine.py:2925–2933   → ToolCall
 → _evidence_execution_closed :877 / _report_execution_allowed :865  (refusal shims 884–939)
 → _execute_tool_call                    :2935–2994  (OutputPersistenceError audit path)
   → ToolOrchestrator.execute            tool_orchestration.py:378–387
       bind_tool_result_output_storage(task_id, tool_name)
     → _execute                          tool_orchestration.py:389–753
        · legacy tool alias resolve       :417–426
        · tool_start lifecycle event      :431–442
        · unknown-tool feedback           :444–470
        · build workdir injection (ParameterFix) :470–488
        · parameter_normalizer.validate_and_fix  :490–537
        · tool_parameters_fixed event     :541–556
        · before_tool_execute hook → _emit_control_action_envelope :558–564
        · tools[name].safe_execute(**validated_params)  :567
        · _flatten_actual_execution       :581–585
        · recovery_handler.recover        :594–655  (ToolRecoveryHandler)
        · _track_tool_execution / _update_successful_states :657–659
        · observation_text = format_tool_result :662   (impl :171–262)
        · ToolExecution(...) + tool_result/tool_error events :675–752
 → _record_execution_bundle              react_engine.py:2996–3046
   → _record_tool_execution              :745–863
       state.record_attempt / attach_durable_output_ref / state.ingest_tool_result
       / state.record_phase_evidence
 → _emit_control_tool_result             :3414 (impl 2553–2613)
 → _apply_tool_execution_loop_effects    :3422 (impl 3239–3271)
 → _add_observation_step                 :3429 (impl 3824–3851)
```

**Reusable as-is from a native `tool_calls` loop** (no protocol assumptions):
- `ToolCall`/`ToolExecution`/`ActualToolExecution` dataclasses (`tool_orchestration.py:58–106`)
- `ToolOrchestrator.execute/_execute` in full, including `safe_execute` wrapping, recovery hook, `ParameterFix` recording, lifecycle events
- `format_tool_result` (`tool_orchestration.py:171–262`) → becomes the `role=tool` content
- `_record_execution_bundle`, `_record_tool_execution`, `_tool_evidence_scope/_roles/_action` (:678–743), evidence ingestion
- `_loop_event_for_execution` (:3048–3085) and all LoopMemory plumbing
- `_execute_tool_call`'s `OutputPersistenceError` audit branch (:2937–2993)
- refusal shims :884–939

**Needs change:** `ToolCall` has no `tool_call_id` field → add one so `role=tool` can carry `tool_call_id`. `call.source_step_index = self.current_iteration` (:2930) is the only "step" coupling.

**Note (spec §3.4-3):** `format_tool_result` :246 re-emits `result.suggestions[:3]` verbatim — this is where suggestion strings reach the model.

---

## 4. LLM LAYER

`react_llm.py` — `ReactLLMClient`.

### `get_response(prompt: str, mode)` :127–178
Signature takes **a single string**. Nothing in the client accepts a message array today.

`_build_request_params` :234–289:
- :243 `"messages": [{"role": "user", "content": prompt}]` — **hardcoded single user message; no system role, no history**
- :247–248 tools schema **only** for `mode == ACTION and supports_function_calling` → THINKING requests are toolless by construction
- :250–268 GPT-5 branch: `reasoning_effort` + `drop_params`, or traditional `temperature`/`max_tokens`
- :269–270 THINKING adds `_thinking_config_for_mode()` (`config.get_thinking_config()`, deepseek reasoner special-case :340–345)
- :272 ollama `api_base`
- :274–282 `params["tools"]`, `tool_choice = {"type":"auto"}` (anthropic) vs `"auto"` (openai)
- `_completion_with_gpt5_fallback` :291–320 — **rebuilds `messages` from the `prompt` string on fallback (:309)**; would need the array threaded through

`build_tools_schema(mode)` :99–125 — anthropic form `{name, description, input_schema}` (:107–112), openai form `{type:"function", function:{name, description, parameters}}` (:113–121). Source: `tool.get_parameter_schema()`.

`_tool_call_format_for_model` :216–232 — `anthropic` / `openai` / `prompt` dispatch.

### What's missing for multi-turn native calling
1. `get_response` must accept `messages: list[dict]` (system + history) instead of `prompt: str`; `_build_request_params` :243 and the fallback :309 both need it.
2. **Tool-call IDs are dropped.** `_extract_tool_call` :394–418 reads only `name`/`arguments` (openai) or `name`/`input` (anthropic); `tool_call.id` is never captured. Assistant turns must be echoed back with IDs so `role=tool` results can be correlated. Return type must become structured (`content`, `list[ToolCallRequest]`) instead of `str`.
3. `_handle_function_calling_response` :351–392 must be deleted, not adapted (it is the lossy text flattener).
4. litellm normalizes both providers to the OpenAI wire shape: replaying assistant turns as `{"role":"assistant","content":…,"tool_calls":[{"id","type":"function","function":{"name","arguments":json_str}}]}` and results as `{"role":"tool","tool_call_id":…,"content":…}` works for both `openai` and `anthropic` targets — the anthropic-only divergence today is the `tool_def` shape (:107) and `tool_choice` shape (:277), both already handled. Anthropic requires `tool_use`/`tool_result` pairing to be exact, so a dropped/reordered result is a hard 400; the sequencing invariant is new and must be tested.
5. `tool_call_format == "prompt"` models (:232) have no native path — the current fallback is `litellm.add_function_to_prompt = True` (:57). Decide whether they stay supported.
6. `token_tracker.track_token_usage(response, model, "thought"|"action")` :133–137 keys on mode — needs a single "executor" (plus "advisor") label.

**Answer to "does anything already support message arrays?": No.** Every request is a one-shot single-user-message call.

---

## 5. PHASE LIFECYCLE

- **Machine**: `phase_machine.py:188–434`. `PHASE_NAMES` order, `current_phase` :204, `current_attempt_id` :208, `_open_attempt` :234 / `_attempt_id` :262, `close_attempt(gate)` :267, `apply(decision)` :309, `mark_done` :359 / `mark_blocked` :374 / `record_abort` :389, `termination_state` :406, `digest_lines` :414.
- **Attempt ids**: minted in `_open_attempt`/`_attempt_id`; stamped onto every evidence ingest (`react_engine.py:759–761, 838–846`) and every control event (:2552–2556, 2607–2608, 2632).
- **Signal handling**: `_handle_phase_signals` :1660–1727 iterates executed steps looking for `result.metadata["phase_signal"]` ∈ {note, repair, done, blocked} (produced by `src/sag/tools/phase_tool.py:86, 141, 312`). Flow: rehydrate `PhaseClaim`/`GateResult` :1682–1692 → `_cap_unresolved_test_gate` :1693 (impl 1081–1115) → forced-test check :1701–1712 → `_emit_control_gate` :1713 → `_record_gate_facts` :1714 (impl 1520–1542) → `machine.close_attempt` :1715 → `transition_policy.decide` :1721 (`phase_transitions.py:235–299`) → `_apply_phase_decision` :1725.
- **Repair route**: `_handle_repair_signal` :1596–1658 → `policy.request_repair` (`phase_transitions.py:301–370`), budgets from `_repair_budgets` :1508 / `_consume_repair_budget` :1514.
- **`self.steps` on transition**: `_apply_phase_decision` :1552–1587 →
  - :1577 `self._phase_iterations = 0`, :1578 `steps_since_context_switch = 0`
  - :1580 **`_archive_window_steps()`** (1729–1769) — folds counters into `_archived_counts`
  - :1581 **`self.steps = [self._phase_intro_step()]`** — the entire window is discarded
  - :1582–1583 journal flags reset, :1584 `_start_phase_branch()` (2056–2091)
  This is exactly the "one conversation per phase" boundary the spec asks for.
- **Phase intro**: `_phase_intro_step` :1364–1445. Order: budget numbers :1367–1369 → `_ensure_project_facts()` for build/test :1379–1381 (impl 3087–3101) → `phase_objective(phase, _detected_build_system())` :1385 → header/digest/objective/budget lines :1386–1400 → `_recommended_build_line` :1417 (impl 1469) → `_native_smoke_guidance` :1426 (impl 3103) → **handoff projection** :1429–1433 `PhaseHandoff.project_for(phase, char_budget=config.phase_handoff_char_budget)` → rendered via `prompt_builder.build_phase_intro_guidance` :1436–1447 → single `SYSTEM_GUIDANCE` step.
- **Handoff injection points**: only :1429–1433 (constructed at :412–415 in ctor). This is the spec's "inject `HandoffProjection` at the top of the new conversation" hook — already isolated.
- **Budgets/floors**: `_phase_budget_numbers` :1024–1033 (`effective_phase_floor` from `config.phase_min_floors`); `_enforce_phase_floors` :1872–1974 called at :2205; forced test install at floor :1888–1908; auto-close claim construction :1928–1965.
- **Objectives**: `PHASE_OBJECTIVES` :106–145, `PYTHON_PHASE_OBJECTIVES` :154–172, `KICKOFF_PHASE_OBJECTIVES` :191–196, `phase_objective()` :254–268. Consumed by `_phase_intro_step` :1385, `_start_phase_branch` :2081, `agent.py:638`.

---

## 6. GEN 1 DELETION SURFACE

### `agent_state_evaluator` — engine surface
| Line | Item |
|---|---|
| 28 | `from .agent_state_evaluator import AgentStateEvaluator` |
| 426 | `self.state_evaluator = AgentStateEvaluator(self.context_manager)` |
| 498 | `.physical_validator = self.physical_validator` |
| 500–503 | `.phase_machine_active = …` |
| 2183–2184 | `.completion_mode` set |
| **2291–2296** | **`state_analysis = self.state_evaluator.evaluate(...)` — the only evaluate call** |
| 2298–2302 | `needs_guidance` → `_add_system_guidance` |
| 2304–2314 | `is_task_complete` handling |
| 2338–2341 | dead "DEPRECATED" comments |
| 2422 | restore in `finally` |
| 3850 | dead comment |

### `agent_state_evaluator.py` internals to delete (860 lines, whole file)
`completion_signals` dict :72–113 (substring table, never checks failure) · `evaluate` :118–215 · `_check_project_analysis_status` :217 · `_check_task2_project_analyzer_requirement` :251 · `_check_ghost_state` :305 · `_check_repetitive_execution` :387 (tool-name repetition counter) · `_check_task_completion_opportunity` :453 · `_check_idle_thinking` :531 · `_check_context_switch_needed` :579 · `_check_ready_for_report` :603 · `_is_task_complete` :642 · `_run_build_evidence_satisfied` :696 · `_is_run_task_complete` :750 · `_is_run_task_completion_marker` :776 · `get_completion_signals_for_task` :801 · `validate_build_state_physically` :819. Re-exports at `src/sag/agent/__init__.py:4, 18–20`.

### Other surface-pattern nudges in the engine
| Line(s) | Nudge | Disposition |
|---|---|---|
| 1852–1868 | **EVIDENCE CHECK** (`_maybe_nudge_phase_done`, `NUDGE_EVERY = 15` :1841) — gate-derived, spells out the full `phase(action='done', outcome='success', …)` call | **Evidence-derived, but violates §3.3 rejection-message standard** (bundles a spelled-out closure). Keep the gate probe, rewrite the text. |
| 2754–2757 | `SCHEDULER FAULT` guidance | DELETE with protocol |
| 3181–3210 | `_loop_guidance`: `PYTEST SELECTOR REJECTED` :3187, **ACTION DIVERSITY ADVISORY** :3194, `LOOP FORCE-BREAK ARMED` :3199, `RECURRENCE WITHOUT PROGRESS` :3206 | LoopMemory-derived → **keep**; §3.3 makes LoopMemory sole authority. Diversity advisory (`loop_memory.py:424–441`, `diversity_threshold=16`) is the weakest — count-based, not evidence-based. |
| 3135–3179 | `_untried_island_targets` | **keep and promote** — §3.4 gate must name untried islands |
| 3283–3292, 1610–1625, 1701–1712, 1888–1908 | TEST_ATTEMPT_REQUIRED guidance | keep |
| 3853–3880 | `_add_completion_guidance` ("Task completion detected!") | **dead code — delete** |
| 3882–3921 | `_check_completion_suggestion` (regex `Tests run: (\d+), Failures…`, `current_iteration >= 25`) | **dead code — delete** |
| 3923–3933 | `_has_report_been_generated` | dead chain — delete |
| 3961–4003 | `_get_physical_validation_state` — **keyword substring trigger** (`"build","compile","test","maven","gradle","success","fail"` :3974–3976) then runs a real physical probe | Trigger is Gen 1, payload is Gen 2. Re-home with an evidence trigger (tool name / `EvidenceRole`). |
| `react_prompt_builder.py:239–262` | `thoughts_without_actions >= 3` → stuck guidance | DELETE (structurally impossible in a native loop) |

### Deletable modules / tests
`reasoning_scheduler.py` (351) · `current_plan.py` (451) · `react_response_parser.py` (153) · `agent_state_evaluator.py` (860) · `react_engine.yaml` `mode_prompts:` block :370–589 (`thinking` :371, `action` :420; `run_task_*` variants :405, :469 only if run-task is also migrated).
Tests: `tests/test_reasoning_scheduler.py` (360) · `tests/test_current_plan.py` (216) · `tests/test_react_scheduler_integration.py` (390) · `tests/test_react_response_parser.py` (117); partial edits in `tests/test_react_engine_phase_wiring.py`, `tests/test_control_layer_replay.py`, `tests/test_control_layer_ab_collector.py`, `tests/test_prompt_vocabulary.py`, `tests/test_react_types.py`, `tests/test_result_state_contracts.py`, `tests/test_test_attempt_policy.py`, `tests/test_context_tool_completion.py`.
Also: `src/sag/agent/replay.py:31, 41, 236, 265–371, 486, 552, 555, 662, 726, 747, 752` (replay drives the production scheduler — **must be rewritten, not just trimmed**) and `scripts/collect_control_layer_ab.py`.

---

## 7. STATE & PERSISTENCE — survives a protocol swap

| Component | Engine touchpoints | Verdict |
|---|---|---|
| `RunEvidenceState` (`evidence_state.py`) | ctor 400–411; `_record_tool_execution` :745–863; `_record_current_phase_evidence` :734; `_record_gate_facts` :1520; `_record_phase_audit` :941; `_record_loop_blocker` :3212; `_resolve_progressed_loop_blockers` :3226; `_loop_event_for_execution` state vector :3053; `_untried_island_targets` reads `tool_observations` :3163 | **untouched** — none of it reads `self.steps` |
| `control_events` (`control_events.py`) | 2429–2683 | mostly untouched. **`action_envelope` (:2474–2512) is scheduler-gated** — needs a new identity (`tool_call.id` in place of `plan_index`); `action_envelope_sha256` (`control_events.py:477`) takes `plan_index=` and its payload model (`:437`) must change. Kinds `planner_response`/`scheduler_decision` (`control_events.py:25–26, 37–38, 435–436`) are removed from `CONTROL_EVENT_KINDS`. |
| `OutputStorageManager` (`output_storage.py`) | ctor 415–423; `attach_durable_output_ref` :798; ad-hoc `store_output` :3492–3510; `_output_refs_from_text` :3559; `is_output_storage_ref` :822 | **untouched**; the :3489–3510 branch-history block moves with the conversation writer |
| Phase-handoff files (`phase_handoff.py`) | ctor 412–415; `project_for` :1429–1433 | **untouched** — already the designed injection seam |
| `TokenTracker` (`token_tracker.py`) | ctor 511; `set_iteration` :2214; `update_last_tool_name` :3382; `track_token_usage` (`react_llm.py:133`); `_export_token_usage_csv` :4202 | untouched, except the `"thought"/"action"` label at `react_llm.py:136` |
| run-pin | `agent.py:238–290` (`_initialize_run_pin_template`), `_write_run_pin` :291+, `_record_target_repo_sha` fed from `react_engine.py:2606–2613` | untouched, except **`feature_flags["reasoning_scheduler"]: True` at `agent.py:277`** must change, and §3.7 adds advisor telemetry here |
| `VerdictFinalizer` / evidence sealing | `_finalize_evidence` :946; `_close_flow` :964; `abort` :996; `cancel` :1008; `_report_execution_allowed` :865 | untouched |
| `PhysicalValidator` | ctor 456–470; `_phase_gate_check` :1809; `_artifact_signal` :538; `_get_physical_validation_state` :3961 | untouched |
| Detached jobs (dispatch-and-poll) | `InvocationStatus.PENDING` in `format_tool_result` (`tool_orchestration.py:175–183`); `poll_ref`/`job_id`/`output_cursor` in `_loop_event_for_execution` :3062–3084; `dispatch_status` in history :3527–3530 | **untouched** — the only scheduler-side coupling is `_is_terminal_poll` (`reasoning_scheduler.py:330–343`), which dies with the protocol |
| Context journal | `context_journal.py:26–50`; `_record_context_journal` :1771–1807; flags :372–381, 2158–2159, 1582–1583 | schema is intro/ledger/steps-shaped → **needs a message-array analogue** |
| `attempt_ledger.compact_steps` | :29 import, :2353 | operates on `ReActStep`s → §3.1 wants it reused for tool-result compaction; **must be re-typed to messages and must preserve error tails** |

---

## Preserved vs. deleted

| Surface | file:line | Verdict |
|---|---|---|
| `_run_react_loop` iteration skeleton (budget, wall clock, floors, token export, exit paths) | react_engine.py:2128–2216, 2399–2422 | **preserve (re-home)** |
| Mode selection / dual role | :2217–2218, 2807–2874 | delete |
| `build_mode_prompt` wrapping | :2221–2237; react_prompt_builder.py:304–380 | delete |
| `build_initial_system_prompt` | :2170–2181; prompt_builder:56–177 | **preserve, promote to every-request system message** |
| `build_next_prompt` flat rebuild | :2369–2380; prompt_builder:179–290 | delete |
| `get_response(prompt, mode)` single-message | react_llm.py:127–178, 234–289 | **rewrite** |
| `build_tools_schema` + `_tool_call_format_for_model` | react_llm.py:99–125, 216–232 | **preserve** |
| `_handle_function_calling_response` text flattening | react_llm.py:351–392 | delete |
| `ReActResponseParser` | react_response_parser.py:1–153; engine :518, 2250 | delete |
| `ReasoningScheduler` + `SchedulerTurn` | reasoning_scheduler.py; engine :344, 2142, 2685–2699, 2807 | delete |
| `CurrentPlan` + placeholders + decorative fields | current_plan.py | delete |
| `_prepare_scheduler_steps` / actor byte-match | :2701–2759 | delete |
| `_canonicalize_scheduler_action` (alias + validate) | :2781–2805 | **preserve, rename, move to native dispatch** |
| `AgentStateEvaluator` + all Gen 1 detectors | agent_state_evaluator.py; engine :28, 426, 498, 503, 2291–2314 | delete |
| Dead completion guidance | :3853–3933 | delete |
| Prompt-builder "stuck thinking" nudge | prompt_builder:239–262 | delete |
| `ToolOrchestrator` (safe_execute, recovery, ParameterFix, lifecycle) | tool_orchestration.py:265–753 | **preserve as-is** |
| `format_tool_result` | tool_orchestration.py:171–262 | **preserve** |
| `_record_tool_execution` + evidence ingestion | :745–863 | **preserve** |
| `_record_execution_bundle` | :2996–3046 | **preserve** |
| LoopMemory pipeline + guidance + force-break close | :3048–3085, 3181–3271, 3273–3329 | **preserve** |
| Forced test attempts (all 5 triggers) | :1035–1362 + call sites | **preserve** |
| Phase machine / gates / transitions / handoff / intro | :1364–1445, 1508–1727, 1809–1974; phase_machine.py; phase_transitions.py; phase_gates.py | **preserve** |
| Evidence state / verdict sealing / report gating | :745–1022 | **preserve** |
| Control events | :2429–2683 | preserve minus `scheduler_decision`/`planner_response`; **`action_envelope` re-keyed** |
| Output storage, journal, token tracker, run pin | :415, 1771, 511, agent.py:238 | **preserve** (journal + `feature_flags` reshaped) |
| `_add_observation_step` + physical enrichment | :3824–3851, 3961–4051 | **preserve payload, replace substring trigger** |
| `_archive_window_steps` / `get_execution_summary` | :1729–1769, 4086–4158 | **preserve semantics, re-derive from messages** |
| `replay.py` scheduler-driven verification | replay.py:236–371, 486–760 | **rewrite** |

---

## Riskiest couplings for the rewrite

1. **`_emit_control_action_envelope` is scheduler-gated (`:2482`, `:2487–2496`).** Removing the scheduler makes it return `None`, and `_emit_control_tool_result` early-returns on falsy `envelope_id` (`:2557`) — **every `tool_result` control event silently disappears**, taking replay, the A/B collector, and the webui timeline with it. `action_envelope_sha256(plan_index=…)` (`control_events.py:477`) and `ActionEnvelopePayload` (`:437`) are typed on `plan_index`; a native loop has no plan index. Needs a designed identity substitution (`tool_call.id` / iteration+ordinal) before anything else lands.

2. **`self.steps` is a load-bearing data structure far beyond prompting.** It is the input to `_handle_phase_signals` (`:1660` iterates `executed_steps`), `compact_steps` (`:2353`), `_archive_window_steps` (`:1729`), `get_execution_summary` (`:4086`), `_record_context_journal` (`:1785`), and is exported to `agent.py:460` (`"steps": list(self.react_engine.steps)`) for reporting. A messages array must either coexist with `steps` or every consumer must be ported at once.

3. **`replay.py` runs the *production* `ReasoningScheduler` as its verifier** (`replay.py:265–371`), asserting mode/reasons/plan-index/envelope-hash equality. It is not an observer that can be trimmed — deleting the scheduler invalidates the entire determinism-verification story and all recorded transcripts. `scripts/collect_control_layer_ab.py` depends on the same events.

4. **`_force_required_test_attempt` reaches into two protocol internals** — `_canonicalize_scheduler_action` (`:1258`) and `_request_scheduler_reasoning(PLAN_EXHAUSTED)` (`:1356`) — while also appending a synthetic `ReActStep` (`:1339–1348`) and its own observation (`:1358`). It is invoked from five different triggers (`:1610`, `:1701`, `:1888`, `:3283`, `:3460`). Spec says keep it; it must be re-expressed as a harness-authored assistant tool_call + tool result pair without dropping the `forced_action` control event (`:1327`) or the `harness_forced_test_attempt` metadata stamp (`:1136`).

5. **Anthropic strict tool_use/tool_result pairing.** litellm normalizes both providers to the OpenAI wire shape, but Anthropic rejects any assistant `tool_use` without a matching `tool_result` in the next user turn. The harness *routinely* refuses/replaces tool executions — `_refused_closed_evidence_execution` (`:884`), `_refused_report_execution` (`:902`), the LoopMemory redirect, the advisor redirect — and breaks the batch early on `phase_signal` (`:3550`) and loop-close (`:3542`). Every such path must still emit exactly one `role=tool` per emitted `tool_call.id`, or runs die with a provider 400. `_completion_with_gpt5_fallback` (`:291–320`) also rebuilds `messages` from a string and would silently drop the history.

6. **Per-phase conversation reset must reuse `_apply_phase_decision`'s exact ordering** (`:1577–1584`: counters → archive → new intro → journal flags → branch). `_archive_window_steps` runs *before* the reset and is the only thing keeping the end-of-run summary whole-run rather than last-phase (comment at `:1732`). Getting this order wrong silently regresses reported metrics.

7. **`compact_steps` head-truncation vs. error tails.** `attempt_ledger.compact_steps(tail, keep_recent=30)` (`:2353`) is typed on `ReActStep` with `tool_result`. §3.1 requires reusing it for tool-result compaction *and* preserving error tails; `format_tool_result` already emits `error_tail_preview` (`tool_orchestration.py:252`) while the live 800-char branch-history truncation (`:3489–3510`) is head-only — the audit's exact finding.

8. **`recent_tool_executions`** (`:384`, `:3809`) is consumed both by the deleted evaluator's `_check_repetitive_execution` and by the surviving `ToolOrchestrator._get_repetition_level` (`tool_orchestration.py:871`) which feeds `_generate_unknown_tool_feedback`. Deleting the evaluator must not orphan the list.
