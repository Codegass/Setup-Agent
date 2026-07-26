# SAG v2 — Plan 2: Native Executor Loop + Gen 1 Deletion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the THINK/ACTION dual-role + plan-lock scheduler protocol with a single-executor native multi-turn function-calling loop; delete the Gen 1 signal layer; make the clone verb side-effect free.

**Architecture:** Strangler inside one plan. `self.steps` remains the single
source of truth (all six downstream consumers untouched); a new renderer
derives the native `messages` array from steps every iteration. The native
loop lands behind a config flag, is proven by an engine-level test with a
scripted LLM, then the flag flips and the old protocol (scheduler, plan,
parser, evaluator) is deleted in one atomic task. Replay is rewritten to
verify the new tool_call-keyed event stream.

**Tech Stack:** Python 3.11+, litellm (OpenAI-normalized tool_calls wire shape for both providers), pytest.

**Spec:** `docs/superpowers/specs/2026-07-25-advisor-mode-harness-redesign.md` §3.1, §3.3, plus §3.4-1 full clone side-effect-freedom (deferred from Plan 1).
**Companion (MANDATORY pre-read for every task):** `docs/superpowers/reports/2026-07-26-engine-anatomy-map.md` — exact seams, coupling inventory, and the 8 risk items. Line numbers there and here are anchors, not gospel: verify with grep before editing; follow stated intent on drift and record the deviation.

## Global Constraints

- Never add `Co-Authored-By` trailers to git commits (repo owner rule).
- Full suite green after EVERY task (baseline at plan time: 2,458 passed / 1 skipped).
- Advisor tool is **Plan 3** — nothing in this plan implements advisor; the LoopMemory redirect keeps its current guidance wording until Plan 3 rewires it.
- Spec §3.3 rejection standard applies to any message text this plan touches: name the concrete machine-derived repair; never bundle a spelled-out closure call while a mechanical repair is untried.
- The pairing invariant is law: every assistant `tool_call.id` that enters `self.steps` receives exactly one `role:"tool"` reply in the rendered messages (real result, refusal, redirect, or synthetic cancellation). Anthropic-format models hard-400 otherwise (map, risk 5).
- `docs/` is gitignored; doc commits need `git add -f`. Source/test commits are normal.
- House test style: scripted fakes, plain dict returns, first-match rules (see `tests/test_test_attempt_policy.py`).

## Task DAG

Stage A (parallel, disjoint files): Task 1 (LLM layer) ∥ Task 2 (renderer) ∥ Task 3 (envelope re-key).
Stage B (sequential): Task 4 (native loop, flag-gated) → Task 5 (forced attempt re-expression) → Task 6 (scripted-LLM engine test).
Stage C (sequential): Task 7 (clone side-effect-freedom) → Task 8 (flip + Gen 1/protocol deletion) → Task 9 (replay rewrite) → Task 10 (suite + tripwires + run-pin).

---

### Task 1: Native turn API in the LLM layer

**Files:**
- Modify: `src/sag/agent/react_llm.py`
- Test: `tests/test_native_llm_turn.py` (new)

**Interfaces:**
- Produces (consumed by Task 4's loop):

```python
@dataclass(frozen=True)
class NativeToolCall:
    id: str          # provider id; never empty (synthesized "call_<n>" if provider omits)
    name: str
    arguments: dict  # parsed JSON arguments; {} on parse failure (raw kept in raw_arguments)
    raw_arguments: str

@dataclass(frozen=True)
class NativeTurn:
    text: str                      # assistant prose ("" if none)
    tool_calls: tuple[NativeToolCall, ...]
    model_used: str

class ReactLLMClient:
    def get_native_turn(self, messages: list[dict], *, include_tools: bool = True) -> NativeTurn: ...
```

- `messages` is the OpenAI-normalized array: `{"role":"system"|"user"|"assistant"|"tool", ...}`; assistant turns may carry `tool_calls=[{"id","type":"function","function":{"name","arguments":<json str>}}]`; tool turns carry `tool_call_id`. litellm translates this shape for anthropic targets too (map §4.4).
- The existing `get_response(prompt, mode)` is untouched until Task 8.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_native_llm_turn.py`:

```python
# tests/test_native_llm_turn.py
"""Native multi-turn tool-calling API (spec §3.1): messages array in,
structured turn out, tool_call ids preserved (the old path discarded them —
anatomy map §4.2)."""

import json
from types import SimpleNamespace

import sag.agent.react_llm as rl
from sag.agent.react_llm import NativeToolCall, NativeTurn


def _fake_completion(monkeypatch, message):
    captured = {}

    def fake_completion(**params):
        captured.update(params)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
            model=params.get("model", "gpt-5.4-mini"),
        )

    monkeypatch.setattr(rl.litellm, "completion", fake_completion)
    return captured


def _client():
    client = rl.ReactLLMClient.__new__(rl.ReactLLMClient)
    client.config = SimpleNamespace(
        action_model="gpt-5.4-mini",
        action_provider="openai",
        verbose=False,
        gpt5_reasoning_effort="medium",
        is_gpt5_model=lambda m: False,
        action_temperature=0.0,
        action_max_tokens=10000,
    )
    client.logger = SimpleNamespace(
        info=lambda *a, **k: None, debug=lambda *a, **k: None, warning=lambda *a, **k: None
    )
    client.token_tracker = None
    client.tools = {}
    return client


def test_native_turn_preserves_tool_call_ids(monkeypatch):
    message = SimpleNamespace(
        content="Cloning now.",
        tool_calls=[
            SimpleNamespace(
                id="call_abc123",
                function=SimpleNamespace(
                    name="project",
                    arguments=json.dumps({"action": "clone", "repo_url": "https://x"}),
                ),
            )
        ],
    )
    captured = _fake_completion(monkeypatch, message)
    client = _client()

    turn = client.get_native_turn(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
    )

    assert isinstance(turn, NativeTurn)
    assert turn.text == "Cloning now."
    assert turn.tool_calls[0] == NativeToolCall(
        id="call_abc123",
        name="project",
        arguments={"action": "clone", "repo_url": "https://x"},
        raw_arguments=json.dumps({"action": "clone", "repo_url": "https://x"}),
    )
    # the full messages array went out unmodified, with tools attached
    assert captured["messages"][0]["role"] == "system"
    assert len(captured["messages"]) == 2
    assert captured["tools"], "tools schema must be attached"


def test_textless_toolless_turn_is_normalized(monkeypatch):
    _fake_completion(monkeypatch, SimpleNamespace(content=None, tool_calls=None))
    turn = _client().get_native_turn([{"role": "user", "content": "go"}])
    assert turn.text == ""
    assert turn.tool_calls == ()


def test_malformed_arguments_yield_empty_dict_with_raw(monkeypatch):
    message = SimpleNamespace(
        content="",
        tool_calls=[
            SimpleNamespace(
                id="", function=SimpleNamespace(name="bash", arguments="{not json")
            )
        ],
    )
    _fake_completion(monkeypatch, message)
    turn = _client().get_native_turn([{"role": "user", "content": "go"}])
    call = turn.tool_calls[0]
    assert call.arguments == {}
    assert call.raw_arguments == "{not json"
    assert call.id.startswith("call_")  # synthesized id — never empty
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_native_llm_turn.py -v`
Expected: FAIL — ImportError on `NativeToolCall`/`NativeTurn`.

- [ ] **Step 3: Implement**

In `react_llm.py`, add near the top (after existing imports; `dataclass` import as needed):

```python
@dataclass(frozen=True)
class NativeToolCall:
    id: str
    name: str
    arguments: dict
    raw_arguments: str


@dataclass(frozen=True)
class NativeTurn:
    text: str
    tool_calls: tuple  # tuple[NativeToolCall, ...]
    model_used: str
```

Add the method to `ReactLLMClient` (reuse `build_tools_schema(ReactModelMode.ACTION)` and the action-model capability resolution the existing `get_response` uses; keep the GPT-5 param branch semantics from `_build_request_params` — anatomy map §4):

```python
    def get_native_turn(self, messages, *, include_tools: bool = True) -> NativeTurn:
        """One native multi-turn executor call: full message history in,
        structured (text, tool_calls) out. IDs are preserved so role=tool
        replies can be correlated (spec §3.1)."""
        model = self.config.action_model
        params = {"model": model, "messages": list(messages)}
        if self.config.is_gpt5_model(model):
            params["reasoning_effort"] = self.config.gpt5_reasoning_effort
            params["drop_params"] = True
        else:
            params["temperature"] = self.config.action_temperature
            params["max_tokens"] = self.config.action_max_tokens
        if include_tools:
            tools_schema = self.build_tools_schema(ReactModelMode.ACTION)
            if tools_schema:
                params["tools"] = tools_schema
                params["tool_choice"] = "auto"
        response = litellm.completion(**params)
        if self.token_tracker is not None:
            try:
                self.token_tracker.track_token_usage(response, model, "executor")
            except Exception:
                pass
        message = response.choices[0].message
        text = getattr(message, "content", None) or ""
        calls = []
        for index, raw in enumerate(getattr(message, "tool_calls", None) or ()):
            function = getattr(raw, "function", None)
            name = getattr(function, "name", "") or ""
            raw_arguments = getattr(function, "arguments", "") or ""
            try:
                arguments = json.loads(raw_arguments) if raw_arguments else {}
                if not isinstance(arguments, dict):
                    arguments = {}
            except (ValueError, TypeError):
                arguments = {}
            call_id = getattr(raw, "id", "") or f"call_{index}"
            calls.append(
                NativeToolCall(
                    id=call_id, name=name, arguments=arguments, raw_arguments=raw_arguments
                )
            )
        return NativeTurn(
            text=text, tool_calls=tuple(calls), model_used=getattr(response, "model", model)
        )
```

Adaptation notes for the implementer (verify against the real file):
- The real client resolves per-mode capabilities via helpers (`_model_type_for`, capability struct). Route through the SAME helpers the existing action path uses rather than reading `config.action_model` naively, if those helpers are cheap to call — intent: identical model/params selection as today's ACTION mode minus mode prompts. The test's `SimpleNamespace` config defines only what the naive path needs; extend the fake if you reuse the helpers.
- Ollama api_base handling (`_add_ollama_api_base`) must be applied if present.
- The `"executor"` usage label replaces `"thought"/"action"` for this path only.
- `anthropic` targets: `tool_choice` must be `{"type": "auto"}` — reuse the existing `tool_call_format` branch (map §4, react_llm.py:274-282).

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_native_llm_turn.py tests/ -q`
Expected: new tests PASS; full suite stays green.

- [ ] **Step 5: Commit**

```bash
git add src/sag/agent/react_llm.py tests/test_native_llm_turn.py
git commit -m "feat: native multi-turn tool-calling API with preserved call ids"
```

---

### Task 2: Steps→messages renderer with pairing invariant and error tails

`self.steps` stays the source of truth (anatomy map risk 2); this module
renders it into the native messages array each iteration.

**Files:**
- Modify: `src/sag/agent/react_types.py` — add native fields to `ReActStep`
- Create: `src/sag/agent/native_messages.py`
- Test: `tests/test_native_messages.py` (new)

**Interfaces:**
- `ReActStep` gains: `tool_call_id: Optional[str] = None` (ACTION and OBSERVATION steps), `native_text: Optional[str] = None` (assistant prose that accompanied the calls). Existing constructors/uses unaffected (defaults).
- Produces:

```python
def render_messages(
    system_prompt: str,
    steps: list,            # list[ReActStep] — the current phase window
) -> list[dict]:
    """[{"role":"system"},…] — OpenAI-normalized, pairing-safe."""
```

Rendering rules (write these as the module docstring):
1. `system_prompt` → one leading `{"role":"system"}` message.
2. SYSTEM_GUIDANCE steps (incl. the phase intro and the compaction ledger) → `{"role":"user","content": step.content}`.
3. THOUGHT steps → `{"role":"assistant","content": step.content}` (native loop produces none; legacy steps render harmlessly).
4. A run of consecutive ACTION steps sharing `native_text` provenance renders as ONE assistant message: `content` = the `native_text` of the first step in the run ("" → None), `tool_calls` = one entry per ACTION step: `{"id": step.tool_call_id, "type": "function", "function": {"name": step.tool_name, "arguments": json.dumps(step.tool_parameters or {})}}`. ACTION steps missing `tool_call_id` get `synthetic-<index>`.
5. Each OBSERVATION step with `tool_call_id` → `{"role":"tool","tool_call_id":…, "content": <clamped content>}`.
6. **Pairing repair (defense in depth):** after rendering, any assistant `tool_call.id` with no following `role:"tool"` reply gets a synthetic reply appended immediately after its assistant message: `{"role":"tool","tool_call_id": id, "content": "[cancelled by harness: no result was produced for this call]"}`. Orphan tool messages (no matching id) are dropped. The dispatcher (Task 4) should make this a no-op; the renderer guarantees it anyway.
7. **Clamping is tail-preserving for failures:** observation content over 5000 chars renders as first 2000 + `"\n…[{n} chars omitted]…\n"` + last 3000 — the audit's head-only truncation is the exact bug being fixed (map risk 7).
8. Legacy OBSERVATION steps without `tool_call_id` (e.g. forced-attempt observations pre-Task 5) → `{"role":"user","content": "[observation] " + clamped}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_native_messages.py`:

```python
# tests/test_native_messages.py
"""Steps→messages renderer: pairing invariant + tail-preserving clamps
(spec §3.1; anatomy map risks 5 and 7)."""

from sag.agent.native_messages import render_messages
from sag.agent.react_types import ReActStep, StepType


def _action(tool, params, call_id, text=""):
    step = ReActStep(step_type=StepType.ACTION, content=f"{tool}", tool_name=tool)
    step.tool_parameters = params
    step.tool_call_id = call_id
    step.native_text = text
    return step


def _observation(content, call_id=None):
    step = ReActStep(step_type=StepType.OBSERVATION, content=content)
    step.tool_call_id = call_id
    return step


def _guidance(content):
    return ReActStep(step_type=StepType.SYSTEM_GUIDANCE, content=content)


def test_action_and_observation_render_as_paired_native_turns():
    steps = [
        _guidance("=== PHASE: BUILD ==="),
        _action("build", {"action": "compile"}, "call_1", text="Compiling now."),
        _observation("BUILD SUCCESS", call_id="call_1"),
    ]
    messages = render_messages("SYS", steps)
    assert messages[0] == {"role": "system", "content": "SYS"}
    assert messages[1] == {"role": "user", "content": "=== PHASE: BUILD ==="}
    assistant = messages[2]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "Compiling now."
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert assistant["tool_calls"][0]["function"]["name"] == "build"
    tool = messages[3]
    assert tool == {"role": "tool", "tool_call_id": "call_1", "content": "BUILD SUCCESS"}


def test_unanswered_call_gets_synthetic_cancellation():
    steps = [_action("bash", {"command": "ls"}, "call_9")]
    messages = render_messages("SYS", steps)
    replies = [m for m in messages if m["role"] == "tool"]
    assert len(replies) == 1
    assert replies[0]["tool_call_id"] == "call_9"
    assert "cancelled by harness" in replies[0]["content"]


def test_failure_clamp_preserves_the_tail():
    body = "HEAD" + ("x" * 9000) + "FATAL: the real error"
    steps = [
        _action("build", {"action": "test"}, "call_2"),
        _observation(body, call_id="call_2"),
    ]
    messages = render_messages("SYS", steps)
    content = [m for m in messages if m["role"] == "tool"][0]["content"]
    assert len(content) < 5200
    assert content.startswith("HEAD")
    assert content.endswith("FATAL: the real error")
    assert "chars omitted" in content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_native_messages.py -v`
Expected: FAIL — no module `native_messages`; `ReActStep` lacks the fields.

- [ ] **Step 3: Implement**

1. In `react_types.py`, add to `ReActStep` (dataclass or class — match the
   real definition): `tool_call_id: Optional[str] = None` and
   `native_text: Optional[str] = None`.
2. Create `src/sag/agent/native_messages.py`:

```python
"""Render the phase step window into a native tool-calling messages array.

`self.steps` remains the engine's single source of truth (phase signals,
compaction, archive, journal, and reporting all consume it unchanged);
this module derives the provider conversation from it every iteration.
Rendering rules are documented in the plan (Plan 2 Task 2) and enforced by
tests; the pairing invariant is repaired defensively here even though the
dispatcher guarantees it."""

import json

from .react_types import ReActStep, StepType

_CLAMP_TOTAL = 5000
_CLAMP_HEAD = 2000
_CLAMP_TAIL = 3000


def _clamp(content: str) -> str:
    text = content or ""
    if len(text) <= _CLAMP_TOTAL:
        return text
    omitted = len(text) - _CLAMP_HEAD - _CLAMP_TAIL
    return (
        text[:_CLAMP_HEAD]
        + f"\n…[{omitted} chars omitted]…\n"
        + text[-_CLAMP_TAIL:]
    )


def render_messages(system_prompt: str, steps) -> list:
    messages = [{"role": "system", "content": system_prompt}]
    pending_assistant = None  # accumulates consecutive ACTION steps

    def flush_assistant():
        nonlocal pending_assistant
        if pending_assistant is not None:
            messages.append(pending_assistant)
            pending_assistant = None

    synthetic = 0
    for step in steps:
        step_type = getattr(step, "step_type", None)
        if step_type == StepType.ACTION:
            call_id = getattr(step, "tool_call_id", None)
            if not call_id:
                synthetic += 1
                call_id = f"synthetic-{synthetic}"
            entry = {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": getattr(step, "tool_name", "") or "",
                    "arguments": json.dumps(getattr(step, "tool_parameters", None) or {}),
                },
            }
            if pending_assistant is None:
                text = getattr(step, "native_text", None) or ""
                pending_assistant = {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [entry],
                }
            else:
                pending_assistant["tool_calls"].append(entry)
        elif step_type == StepType.OBSERVATION:
            flush_assistant()
            call_id = getattr(step, "tool_call_id", None)
            if call_id:
                messages.append(
                    {"role": "tool", "tool_call_id": call_id, "content": _clamp(step.content)}
                )
            else:
                messages.append(
                    {"role": "user", "content": "[observation] " + _clamp(step.content)}
                )
        elif step_type == StepType.THOUGHT:
            flush_assistant()
            messages.append({"role": "assistant", "content": step.content})
        else:  # SYSTEM_GUIDANCE and anything else
            flush_assistant()
            messages.append({"role": "user", "content": step.content})
    flush_assistant()
    return _repair_pairing(messages)


def _repair_pairing(messages: list) -> list:
    """Guarantee exactly one role=tool reply per emitted tool_call id."""
    repaired = []
    answered = set()
    known_ids = set()
    for message in messages:
        if message["role"] == "tool":
            if message.get("tool_call_id") not in known_ids:
                continue  # orphan reply — drop
            answered.add(message["tool_call_id"])
            repaired.append(message)
            continue
        repaired.append(message)
        if message["role"] == "assistant":
            for call in message.get("tool_calls") or ():
                known_ids.add(call["id"])
    result = []
    for message in repaired:
        result.append(message)
        if message["role"] == "assistant":
            for call in message.get("tool_calls") or ():
                if call["id"] not in answered:
                    result.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": "[cancelled by harness: no result was produced for this call]",
                        }
                    )
                    answered.add(call["id"])
    return result
```

Adaptation note: if `ReActStep` is a plain class whose `__init__` doesn't
take these kwargs, add them as class-level defaults set to `None` and keep
the tests' attribute-assignment style. Verify `StepType` member names by
grep (`grep -n "class StepType" src/sag/agent/react_types.py`).

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_native_messages.py tests/test_react_types.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sag/agent/react_types.py src/sag/agent/native_messages.py tests/test_native_messages.py
git commit -m "feat: steps-to-native-messages renderer with pairing repair and tail-preserving clamps"
```

---

### Task 3: Re-key the action envelope to tool_call identity

Highest-risk coupling (anatomy map risk 1): today
`_emit_control_action_envelope` (react_engine.py:2474–2512) requires an
active scheduler and a `plan_index`; without them every `tool_result`
control event silently disappears.

**Files:**
- Modify: `src/sag/agent/control_events.py` — `ActionEnvelopePayload` (~:437), `action_envelope_sha256` (~:477)
- Modify: `src/sag/agent/react_engine.py` — `_emit_control_action_envelope` (~:2474)
- Test: `tests/test_action_envelope_rekey.py` (new); existing `tests/test_control_layer_replay.py` must stay green

**Interfaces:**
- `ActionEnvelopePayload` gains `tool_call_id: str | None = None`; `plan_index` becomes `int | None = None` (optional, kept for old transcripts).
- `action_envelope_sha256(...)` accepts both identities; the hash input uses `plan_index` when present else `tool_call_id` (old transcripts keep verifying byte-identically).
- `_emit_control_action_envelope` emits whenever EITHER a scheduler plan step is active (old path, unchanged) OR the step carries `tool_call_id` (new path) — the scheduler-active gate is removed.

- [ ] **Step 1: Write the failing test**

Create `tests/test_action_envelope_rekey.py`:

```python
# tests/test_action_envelope_rekey.py
"""Action envelopes must survive the scheduler's removal: a step carrying a
native tool_call_id gets an envelope without any plan machinery (anatomy
map risk 1 — otherwise every tool_result control event vanishes)."""

from sag.agent.control_events import ActionEnvelopePayload, action_envelope_sha256


def test_payload_accepts_tool_call_identity():
    payload = ActionEnvelopePayload(
        tool="build",
        params={"action": "compile"},
        tool_call_id="call_7",
        plan_index=None,
    )
    assert payload.tool_call_id == "call_7"


def test_hash_is_stable_for_old_transcripts():
    with_plan = action_envelope_sha256(
        tool="build", params={"action": "compile"}, plan_index=3
    )
    again = action_envelope_sha256(
        tool="build", params={"action": "compile"}, plan_index=3
    )
    assert with_plan == again


def test_hash_accepts_tool_call_identity():
    a = action_envelope_sha256(
        tool="build", params={"action": "compile"}, tool_call_id="call_7"
    )
    b = action_envelope_sha256(
        tool="build", params={"action": "compile"}, tool_call_id="call_8"
    )
    assert a != b
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_action_envelope_rekey.py -v`
Expected: FAIL — unexpected keyword `tool_call_id`.

- [ ] **Step 3: Implement**

1. `control_events.py`: read the real `ActionEnvelopePayload` model and
   `action_envelope_sha256` signature first. Add `tool_call_id: str | None = None`;
   relax `plan_index` to optional. In the sha256 helper, keep the byte
   input EXACTLY as today when `plan_index is not None`; when it is None,
   substitute the string `f"tool_call:{tool_call_id}"` in the position
   `plan_index` occupied. Reject both-None with `ValueError`.
2. `react_engine.py` `_emit_control_action_envelope`: remove the
   `_active_reasoning_scheduler() is not None` early return; derive
   identity as `plan_index = getattr(step, "plan_index", None)` (unchanged
   when scheduler runs) and `tool_call_id = getattr(step, "tool_call_id", None)`;
   emit when either is present; thread both into the payload and hash.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_action_envelope_rekey.py tests/test_control_layer_replay.py tests/test_control_layer_ab_collector.py -v`
Expected: PASS — old-transcript hashing unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/sag/agent/control_events.py src/sag/agent/react_engine.py tests/test_action_envelope_rekey.py
git commit -m "feat: action envelopes keyed by tool_call_id, scheduler gate removed"
```

---

### Task 4: The native executor loop (flag-gated)

> **INTERFACE CORRECTIONS FROM STAGE A (binding, discovered against real code):**
> 1. `ReActStep` is a pydantic model; the params field is **`tool_params`**, not
>    `tool_parameters` (assigning an undeclared field raises). Construction
>    requires `timestamp: str`.
> 2. `get_native_turn` **propagates provider exceptions** (no swallow-to-None
>    like `get_response`). Wrap the call: on exception,
>    `return self.abort(f"LLM response unavailable: {exc}")`.
> 3. Envelope identity needs zero wiring if the dispatcher appends the ACTION
>    step (with `tool_call_id`) BEFORE `_execute_tool_call` — the emitter
>    tail-scans `self.steps`. `self._active_native_tool_call_id` is the
>    explicit override for out-of-tail executions.
> 4. The renderer merges consecutive ACTION steps into one assistant message
>    only when their `native_text` matches — set `step.native_text = turn.text`
>    on every call of the same turn.
> 5. A tool call with `name==""` is delivered (not dropped): route it through
>    the orchestrator's unknown-tool feedback so the pairing invariant holds.
> 6. Post-Stage-A full-suite baseline: 2,497 passed / 1 skipped (env-dependent
>    ±1 skip); re-measure your own clean baseline before claiming deltas.
> The sketch below is amended for (1); apply the rest during implementation.

**Files:**
- Modify: `src/sag/config.py` (or wherever `max_iterations` etc. live — grep `max_wall_clock_seconds`) — add `native_executor_loop: bool = False`
- Modify: `src/sag/agent/react_engine.py` — new `_run_native_loop(...)` + dispatch in `run_setup_loop`/`_run_react_loop` entry
- Test: Task 6 provides the engine-level test; this task carries unit tests for the dispatcher helper only: `tests/test_native_dispatch.py` (new)

**Interfaces:**
- Consumes: `get_native_turn` (Task 1), `render_messages` (Task 2), envelope re-key (Task 3), plus preserved machinery per the anatomy map §2e/§3 (all called by their existing names).
- Produces: `_run_native_loop(initial_prompt, max_iterations, completion_mode)` with the same return contract as `_run_react_loop`; `_execute_native_calls(turn) -> list[ReActStep]` (the dispatcher).

**Design (implement exactly; anchors in the anatomy map §1):**

The native loop mirrors `_run_react_loop`'s skeleton — same pre-loop setup
minus scheduler (skip :2142–2152), same wall-clock/floor/budget/exit
paths — with the turn body replaced:

```python
    def _run_native_loop(self, initial_prompt, max_iterations=None, completion_mode="setup"):
        """Single-executor native tool-calling loop (spec §3.1). One LLM call
        per iteration; the model sees [system] + rendered phase window."""
        max_iter = max_iterations or self._run_max_iterations
        phase_mode = completion_mode == "setup" and self.phase_machine is not None
        self.steps = [self._phase_intro_step()] if phase_mode else []
        # journal flags + branch init exactly as _run_react_loop :2155-2164
        self._journal_pending_reset = True
        self._journal_last_span = 0
        self._start_phase_branch()
        self.prompt_builder.invalidate_trunk_cache()
        system_prompt = self.prompt_builder.build_initial_system_prompt(
            completion_mode=completion_mode
        )
        if not phase_mode and initial_prompt:
            self.steps.append(
                ReActStep(step_type=StepType.SYSTEM_GUIDANCE, content=initial_prompt)
            )
        self.run_started_at = time.monotonic()
        wall_clock_cap = getattr(self.config, "max_wall_clock_seconds", 7200)
        try:
            while self.current_iteration < max_iter:
                if self.wall_clock_exceeded(wall_clock_cap):
                    return self.abort("wall clock cap exceeded")
                if phase_mode:
                    floor_closed = self._enforce_phase_floors()
                    if floor_closed and self.phase_machine.is_complete:
                        return self._close_flow(RunTermination.COMPLETED)
                self.current_iteration += 1
                self._phase_iterations += 1
                self.token_tracker.set_iteration(self.current_iteration)

                messages = render_messages(system_prompt, self.steps)
                turn = self.llm_client.get_native_turn(messages)

                if not turn.tool_calls:
                    # Toolless turn: record the prose, then a mechanical
                    # continuation cue (evidence-free, §3.3-safe: it names
                    # the two legal moves, spells out neither).
                    if turn.text.strip():
                        self.steps.append(
                            ReActStep(step_type=StepType.THOUGHT, content=turn.text)
                        )
                    if completion_mode != "setup":
                        return True  # run-task mode: a text answer ends the task
                    self.steps.append(
                        ReActStep(
                            step_type=StepType.SYSTEM_GUIDANCE,
                            content=(
                                "No tool was called. Continue with a tool call, "
                                "or close the phase honestly via phase(...)."
                            ),
                        )
                    )
                    self._maybe_record_journal(len(messages))
                    continue

                executed_steps = self._execute_native_calls(turn)

                if phase_mode:
                    self._handle_phase_signals(executed_steps)
                    if self.phase_machine.is_complete:
                        return self._close_flow(RunTermination.COMPLETED)
                    self._maybe_nudge_phase_done()

                self._compact_window_if_needed()
                self._maybe_record_journal(len(messages))
            return self.abort("iteration budget exhausted")
        except KeyboardInterrupt:
            return self.cancel("keyboard interrupt")
        except Exception as exc:  # same contract as _run_react_loop :2411
            logger.exception("native loop failure")
            return self.abort(f"engine failure: {exc}")
```

The dispatcher — the pairing-invariant owner:

```python
    def _execute_native_calls(self, turn) -> list:
        """Execute every tool call of one assistant turn in order. EVERY
        call id gets exactly one observation (real result, refusal, or
        cancellation) — Anthropic pairing is a hard 400 otherwise."""
        executed = []
        cancelled_reason = None
        for call in turn.tool_calls:
            step = ReActStep(
                step_type=StepType.ACTION,
                content=f"{call.name}",
                tool_name=call.name,
            )
            step.tool_params = dict(call.arguments)
            step.tool_call_id = call.id
            step.native_text = turn.text
            self.steps.append(step)

            if cancelled_reason is not None:
                self._append_native_observation(
                    call.id, f"[not executed: {cancelled_reason}]"
                )
                continue

            tool_call = self._build_tool_call_from_step(step)
            refusal = self._refusal_for_call(tool_call)  # closed-evidence/report shims
            if refusal is not None:
                step.tool_result = refusal
                self._append_native_observation(call.id, refusal.output or refusal.error)
                continue

            execution = self._execute_tool_call(tool_call)
            self._record_execution_bundle(step, execution)
            step.tool_result = execution.result
            self._emit_control_tool_result(step, execution)
            self._apply_tool_execution_loop_effects(step, execution)
            self._append_native_observation(call.id, execution.observation_text)
            executed.append(step)

            metadata = (execution.result.metadata or {}) if execution.result else {}
            if metadata.get("phase_signal"):
                cancelled_reason = "a phase transition is being processed"
            if self._loop_forced_close:
                cancelled_reason = "the loop breaker closed this phase"
        return executed
```

with the observation helper routing through the preserved enrichment:

```python
    def _append_native_observation(self, tool_call_id: str, text: str) -> None:
        self._add_observation_step(text)          # existing enrichment path
        self.steps[-1].tool_call_id = tool_call_id
```

Adaptation notes (mandatory reading of anatomy map §1 turn B, §3):
- `_refusal_for_call` is a thin extraction of the existing guard block at
  :3390–3394 (`_evidence_execution_closed` → `_refused_closed_evidence_execution`,
  `_report_execution_allowed` → `_refused_report_execution`) so both loops
  share it; keep the old inline block working until Task 8 or extract and
  call from both.
- `_loop_forced_close`: reuse however `_close_phase_for_loop` (:3273) and
  the batch-break at :3542–3549 signal today — read the real mechanism and
  mirror it; the invariant is that remaining calls get cancellation
  observations instead of being silently dropped.
- `_compact_window_if_needed` wraps the existing :2343–2366 compaction
  block verbatim (extract-and-share or duplicate; the ledger step renders
  as a user message via Task 2).
- `_maybe_record_journal(n_messages)` calls `_record_context_journal` with
  the segments dict reshaped as `{"system": 1, "ledger": …, "messages": n_messages}`
  — read the real signature and keep the JSONL keys backward-tolerant.
- Wire the dispatch: in `run_setup_loop`/`run_react_loop`, route to
  `_run_native_loop` when `getattr(self.config, "native_executor_loop", False)`.

- [ ] **Step 1: Write dispatcher unit tests** (`tests/test_native_dispatch.py`)

```python
# tests/test_native_dispatch.py
"""The dispatcher owns the pairing invariant: every tool_call id gets one
observation even when a phase signal or refusal interrupts the batch."""

from types import SimpleNamespace

from sag.agent.react_llm import NativeToolCall, NativeTurn
from sag.agent.react_types import StepType


def _turn(*calls):
    return NativeTurn(text="working", tool_calls=tuple(calls), model_used="m")


def _call(i, name="bash", args=None):
    return NativeToolCall(
        id=f"call_{i}", name=name, arguments=args or {"command": "ls"}, raw_arguments="{}"
    )


def test_phase_signal_cancels_remaining_calls_with_observations(native_engine):
    engine = native_engine(results={"phase": {"phase_signal": "done"}})
    engine._execute_native_calls(_turn(_call(1, "phase"), _call(2, "bash")))
    observations = [s for s in engine.steps if s.step_type == StepType.OBSERVATION]
    assert [o.tool_call_id for o in observations] == ["call_1", "call_2"]
    assert "not executed" in observations[1].content
```

`native_engine` is a pytest fixture the implementer writes in this file: a
`ReActEngine.__new__`-constructed engine with scripted
`_execute_tool_call`/`_record_execution_bundle`/`_emit_control_tool_result`/
`_apply_tool_execution_loop_effects`/`_add_observation_step` fakes
(SimpleNamespace executions whose `result.metadata` comes from the
`results` mapping keyed by tool name). Follow the existing engine-test
harness in `tests/test_react_engine_phase_wiring.py` for construction
patterns. The fixture must be real code in the test file — no stubs.

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_native_dispatch.py -v` → FAIL (no `_execute_native_calls`).

- [ ] **Step 3: Implement** per the design block above.

- [ ] **Step 4: Run** — `python -m pytest tests/test_native_dispatch.py tests/ -q` → green (flag default off; old path untouched).

- [ ] **Step 5: Commit**

```bash
git add src/sag/agent/react_engine.py src/sag/config.py tests/test_native_dispatch.py
git commit -m "feat: native executor loop behind native_executor_loop flag"
```

---

### Task 5: Re-express the forced test attempt natively

Anatomy map risk 4. `_force_required_test_attempt` (react_engine.py:1241–1362)
must produce a synthetic ACTION step + observation that render as a valid
assistant tool_call + role=tool pair, without losing the `forced_action`
control event (:1327) or the `harness_forced_test_attempt` metadata (:1136).

**Files:**
- Modify: `src/sag/agent/react_engine.py` — `_force_required_test_attempt`
- Test: `tests/test_forced_attempt_native.py` (new)

- [ ] **Step 1: Failing test**

```python
# tests/test_forced_attempt_native.py
"""A harness-forced test attempt must be a well-formed native turn: the
synthetic ACTION step carries a forced tool_call_id and its observation
answers it (pairing invariant for harness-authored calls)."""
```

Test body: drive `_force_required_test_attempt` on a scripted engine (same
fixture style as Task 4) with a `TestAttemptRequirement` for
`build(action='test')`; assert the appended ACTION step has
`tool_call_id == "forced-1"` (counter-based), the observation step carries
the same id, and the `forced_action` control event still fires (spy on the
emitter). Write it concretely against the real signatures (read
:1241–1362 first).

- [ ] **Step 2: Run** → FAIL (no ids today).

- [ ] **Step 3: Implement** — in `_force_required_test_attempt`: maintain
`self._forced_call_counter`; set `step.tool_call_id = f"forced-{n}"` and
`step.native_text = "[harness] executing the mandatory test attempt"` on
the synthetic step (:1339–1348), and stamp the same id on the observation
appended at :1358. Replace the `_request_scheduler_reasoning(PLAN_EXHAUSTED)`
call at :1356 with a no-op when the scheduler is absent (guard, deleted
fully in Task 8). No other behavior change.

- [ ] **Step 4: Run** — forced-attempt suites: `python -m pytest tests/test_forced_attempt_native.py tests/test_test_attempt_policy.py tests/test_react_engine_phase_wiring.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sag/agent/react_engine.py tests/test_forced_attempt_native.py
git commit -m "feat: forced test attempts render as paired native tool calls"
```

---

### Task 6: Engine-level test with a scripted native LLM

The proof that the flag-gated loop works end-to-end before the flip.

**Files:**
- Test: `tests/test_native_loop_engine.py` (new)

- [ ] **Step 1: Write the test** — construct a real `ReActEngine` the way
`tests/test_react_engine_phase_wiring.py` does (real phase machine, fake
orchestrator, real tools where that harness uses them), set
`config.native_executor_loop = True`, and replace `llm_client` with a
scripted client returning a fixed sequence of `NativeTurn`s:
1. turn: `phase(action='done', outcome='success', …)` for provision (or
   whatever minimal phase sequence that harness's gate fakes accept),
2. …through to machine completion.
Assert: `run_setup_loop` returns COMPLETED; every ACTION step has a
`tool_call_id`; `render_messages` over the final window raises no pairing
repair (spy: `_repair_pairing` output == input length); the context journal
got at least one record per phase. Also one toolless-turn script asserting
the continuation cue is injected and the loop proceeds.

This is the task where integration reality bites; expect to adapt engine
construction details. Keep the old-path suite green throughout.

- [ ] **Step 2–4: red → wire fixes → green** — `python -m pytest tests/test_native_loop_engine.py tests/ -q`.

- [ ] **Step 5: Commit**

```bash
git add tests/test_native_loop_engine.py src/sag/agent/react_engine.py
git commit -m "test: native loop drives the phase machine end-to-end under the flag"
```

---

### Task 7: Clone is side-effect free (spec §3.4-1, deferred from Plan 1)

**Files:**
- Modify: `src/sag/tools/internal/project_setup_tool.py` — the clone verb's dependency auto-install dispatch (the `_install_dependencies`-style branch calling `_install_python_dependencies` at ~:1089 and the Java branch above it — grep `Installing dependencies automatically`)
- Modify: clone output composition — replace the auto-install section with explicit next steps
- Test: `tests/test_clone_side_effect_free.py` (new); update `tests/test_clone_venv_ladder_reachable.py` (the ladder moves with the deps path — see note)

**Behavior:** `project(action='clone')` clones, inits submodules, detects
project type, and STOPS. No venv, no pip, no JDK install. Its output's
"Suggested next steps" names `project(action='provision', ...)` for the
toolchain and `build(action='deps')` for dependencies. The dependency
installer (`_install_python_dependencies`, WITH its Task-1-of-Plan-1 ladder
fix) remains and is reached via `project(action='provision')` and the
python deps path — verify with grep who calls it besides clone
(`grep -rn "_install_python_dependencies\|_install_dependencies" src/sag/`)
and keep those callers.

Tests: clone on a fake python project runs **zero** venv/pip/apt commands
(scripted orchestrator records commands; assert none match
`venv|pip|apt-get`); clone output contains `build(action='deps')` and no
"Installing dependencies automatically". Move/adjust the Plan-1 ladder
test only if its entry point changed (the ladder logic itself is
untouched).

- [ ] Steps: failing tests → implement → `python -m pytest tests/test_clone_side_effect_free.py tests/test_clone_venv_ladder_reachable.py tests/test_project_tool.py tests/ -q` → commit `"feat: clone verb is side-effect free — provisioning moves behind explicit actions"`.

---

### Task 8: Flip the flag; delete the old protocol and Gen 1

> **BINDING NOTES FROM STAGE B (discovered against real code):**
> 1. The shared ACTION machinery now lives in `_execute_action_step(step)`
>    (both loops call it). The engine-side deletion inside it is exactly two
>    blocks: the `_active_reasoning_scheduler()`/`scheduler.observe_result`
>    block and the legacy `_force_thinking_*` cadence. After the alias
>    collapse (`_run_react_loop` → `_run_native_loop`, currently a 5-line
>    guard at the top of `_run_react_loop` :2152), `_execute_steps` loses its
>    last caller — delete it then.
> 2. Real names (the plan sketch's were wrong): journal flags are
>    `_journal_intro_dirty` / `_journal_last_ledger`; `_close_flow` takes
>    `RunTerminationStatus`; `abort`/`cancel` are keyword-only
>    (`abort(reason=...)`); `wall_clock_exceeded(run_started_at, cap)` is a
>    module-level function; `build_initial_system_prompt(repository_url=,
>    repository_ref=, tool_calling_enabled=, workflow_mode=)`.
> 3. Run-task migration must de-duplicate the kickoff text: the native loop
>    currently folds `initial_prompt` into the system prompt AND appends it
>    as a SYSTEM_GUIDANCE step in non-phase mode (deliberate interim
>    duplication — remove the step, keep the system-prompt fold).
> 4. `_request_scheduler_reasoning` already no-ops without a scheduler;
>    delete it and the `Task 8` marker comments left in place.
> 5. `tests/test_native_loop_engine.py`'s stub of
>    `_missing_required_test_attempt` (→ None) is load-bearing: with a
>    non-surveyable orchestrator the real policy force-executes a
>    `project(action='analyze')` refresh. Keep the stub or wire a surveyable
>    orchestrator when extending that harness.
> 6. Post-Stage-B full-suite baseline: 2,510 passed / 1 skipped (env ±1
>    skip); measure your own clean baseline before claiming deltas.

The atomic deletion. Work strictly from the anatomy map §2 (coupling
inventory) and §6 (deletion surface); every line ref there is an anchor.

**Files (delete):** `src/sag/agent/reasoning_scheduler.py`, `src/sag/agent/current_plan.py`, `src/sag/agent/react_response_parser.py`, `src/sag/agent/agent_state_evaluator.py`; tests `test_reasoning_scheduler.py`, `test_current_plan.py`, `test_react_scheduler_integration.py`, `test_react_response_parser.py`.
**Files (modify):** `react_engine.py` (all §2a–§2d engine refs; `_run_react_loop` becomes a thin alias of `_run_native_loop`; run-task rides the native loop), `react_llm.py` (delete `_handle_function_calling_response` + JSON-salvage helpers + `get_response`'s mode machinery; keep `get_native_turn`, `build_tools_schema`, format dispatch), `react_prompt_builder.py` (delete `build_mode_prompt`, `build_next_prompt`, stuck-thinking nudge :239–262; keep `build_initial_system_prompt` — strip its THOUGHT/ACTION/CURRENT_PLAN format sections, keep identity/tools/workflow guidance), `react_engine.yaml` (delete `mode_prompts:` block; purge THINK/ACTION vocabulary from remaining prompt text), `src/sag/agent/__init__.py` (re-exports), `agent.py:277` (`feature_flags`: drop `reasoning_scheduler`, add `"native_loop": True`), config (remove the flag — native is the only path), partial test edits per map §6 list.
**Also in this task (§3.3 text fixes):** rewrite EVIDENCE CHECK (:1852–1868) to state gate state + the concrete missing item only — no spelled-out closure call; keep `NUDGE_EVERY` cadence. Keep `recent_tool_executions` (orchestrator still consumes it — map risk 8). Delete dead code :3853–3933.

- [ ] **Step 1: Flip default** — native loop unconditional; run full suite; triage failures INTO the deletion list (every failure should be an old-protocol test).
- [ ] **Step 2: Delete engine references** per map §2a–§2d, run suite after each file-level sweep.
- [ ] **Step 3: Delete the four modules + four test files**; partial-edit the listed mixed test files (each edit commented with `Plan 2 Task 8: old protocol removed`).
- [ ] **Step 4: Prompt surgery** — `build_initial_system_prompt` output must contain no THOUGHT/ACTION/CURRENT_PLAN instructions (assert in a new small test `tests/test_system_prompt_native.py`: render it and check banned strings absent, and that it still names the tools and the phase workflow).
- [ ] **Step 5: EVIDENCE CHECK rewrite** with test asserting the message contains the gate reason and does NOT contain `phase(action='done'`.
- [ ] **Step 6: Full suite** `python -m pytest tests/ -q` → 0 failures; tripwires:
  `grep -rn "SCHEDULER FAULT\|CURRENT_PLAN\|MALFORMED_PLAN\|ACTOR_MISMATCH" src/sag/` → only historical strings in replay-compat code (Task 9 decides) or nothing;
  `grep -rn "agent_state_evaluator\|AgentStateEvaluator" src/` → nothing.
- [ ] **Step 7: Commit** `"feat!: native executor loop is the only protocol; scheduler, plan-lock, parser and Gen 1 evaluator deleted"`.

---

### Task 9: Replay rewrite

Anatomy map risk 3: `replay.py` drives the production scheduler as its
verifier. Rewrite it to verify the native event stream.

**Files:**
- Modify: `src/sag/agent/replay.py`; `scripts/collect_control_layer_ab.py`
- Test: `tests/test_control_layer_replay.py` (rewrite the scheduler-mode assertions; keep envelope/tool_result hash verification)

**New verification contract:** a recorded run replays by walking
`control_events.jsonl` and asserting (1) every `tool_result` event's
`envelope_id` resolves to an `action_envelope` whose sha256 recomputes
byte-identically from `{tool, params, tool_call_id|plan_index}` (Task 3's
dual-key), (2) tool_result ordering is monotone per phase attempt, (3) the
pairing invariant holds (every envelope has exactly one tool_result), and
(4) gate events reference existing claims. Old transcripts (plan_index
envelopes, `scheduler_decision`/`planner_response` events) verify under the
same walk — unknown-but-well-formed event kinds are skipped with a counted
notice, not an error. Scheduler *re-execution* (mode/reason equality) is
gone; document that in the module docstring as the Plan 2 contract change.

- [ ] Steps: rewrite tests first (red on current code where they encode
scheduler re-execution) → implement → `python -m pytest tests/test_control_layer_replay.py tests/test_control_layer_ab_collector.py tests/ -q` → commit `"feat!: replay verifies the native tool_call event stream"`.

---

### Task 10: Full verification + run-pin telemetry

- [ ] **Step 1:** `python -m pytest tests/ -q` → 0 failures; record the new total.
- [ ] **Step 2:** Tripwires: Task 8's greps still clean; `grep -rn "native_executor_loop" src/` → gone (flag removed); `grep -n "reasoning_scheduler" src/sag/agent/agent.py` → only the removed-flag line's replacement.
- [ ] **Step 3:** Cold-run sanity smoke, same pins as Plan 1's smoke: `sag project https://github.com/apache/commons-cli.git --name plan2smoke-cli --ref afb0fd14… --record` **and** the TVM smoke (`--name plan2smoke-tvm-828d-r1`). Gate: commons-cli retains canonical success; TVM reaches at least the Plan-1 smoke frontier (real deps attempt) with zero provider-400s from message pairing, zero deleted-fault-class strings in logs. Full §3.7 acceptance (two repeats, bigtop) runs after Plan 3.
- [ ] **Step 4:** `git status --short` clean; report commits + test counts + smoke session dirs.
