# Observation Trajectory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A closed control ledger, an incremental read-only trajectory-v1 reducer with CLI, and a live webui timeline — one derivation pipeline serving live observation and post-hoc forensics.

**Architecture:** Engine seals first-class turn records and closes the four measured ledger gaps (spec §2); `src/sag/trajectory/` derives a versioned trajectory purely from the authoritative layer with one incremental reducer used by both tail-follow and batch replay (spec §1/§3); the existing FastAPI app + `webui/` Vite frontend gain a timeline view fed only by that reducer (spec §4).

**Tech Stack:** Python 3.12 (`.venv`), pydantic v2 (`_StrictPayload` pattern in `src/sag/agent/control_events.py`), click CLI (`src/sag/main.py`), FastAPI (`src/sag/web/app.py`), Vite+React+TS (`webui/` → built into `src/sag/web/static/`).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-14-observation-trajectory-design.md`. Where this plan and the spec disagree, the spec wins.
- The reducer NEVER writes, reorders, or mutates any source file; console logs are NEVER an input (spec §1).
- RunEvidenceState and all control events are engine-written only; turn records go through the same publication path as other control events (spec §2.1).
- Detail tiers are exactly `summary` and `full` (spec §3).
- Test runner: `.venv/bin/python -m pytest` from repo root (system python3 is 3.14 without pytest). Full suite green before every commit; baseline 5,317 passed / 23 skipped / 0 errors.
- TDD: failing fence first, minimal change, targeted green, full suite green, commit. NEVER `git stash`. NEVER add attribution trailers to commits. `docs/` and `logs/` are gitignored — use `git add -f` for files the plan commits there.
- Unknown event kinds and ledger holes degrade to `warnings[]`, never exceptions (spec §3).

## File Structure

```
src/sag/trajectory/__init__.py     — public API re-exports
src/sag/trajectory/schema.py       — trajectory-v1 pydantic models (Task 1)
src/sag/trajectory/reducer.py      — TrajectoryReducer: event → deltas (Task 2)
src/sag/trajectory/builder.py      — session-dir batch/follow assembly (Task 3)
src/sag/agent/control_events.py    — TurnRecordPayload (+ typed refusal) (Tasks 6, 9)
src/sag/agent/react_engine.py      — turn sealing, forced observations (Tasks 7, 8)
src/sag/tools/phase_tool.py        — loop_decision emission parity (Task 9)
src/sag/main.py                    — `sag trajectory` click command (Task 5)
src/sag/web/app.py                 — /api/sessions/{id}/trajectory (Task 12)
webui/src/…                        — timeline view (Tasks 13, 14)
tests/fixtures/trajectory/         — committed golden session fixtures (Task 4)
tests/test_trajectory_schema.py, test_trajectory_reducer.py,
tests/test_trajectory_builder.py, test_trajectory_golden.py,
tests/test_trajectory_cli.py, test_turn_records.py,
tests/test_ledger_closure.py, test_trajectory_api.py
```

Stage A = Tasks 1–5 (reducer on today's events, holes stated as warnings).
Stage B = Tasks 6–11 (closure contract; warnings disappear for new sessions).
Stage C = Tasks 12–14 (webui timeline).

---

### Task 1: trajectory-v1 schema module

**Files:**
- Create: `src/sag/trajectory/__init__.py`, `src/sag/trajectory/schema.py`
- Test: `tests/test_trajectory_schema.py`

**Interfaces:**
- Consumes: nothing (pure models).
- Produces: `SCHEMA_VERSION = 1`; pydantic models `Trajectory{session: SessionInfo, phases: list[PhaseInfo], turns: list[Turn], annotations: list[Annotation], warnings: list[Warning]}`, `Turn{turn_id: int, phase: str, iteration: int | None, actor: Literal["model","controller"], window_ref: str | None, call: CallInfo | None, observation: ObservationInfo | None, gate: GateInfo | None, tokens: TokenUsage | None, t0: str | None, t1: str | None, control_seq: list[int]}`, `CallInfo{tool: str, params_ref: str | None}`, `ObservationInfo{ref: str | None, error_code: str | None, failure_signature: str | None}`, `GateInfo{word: str, decision_id: str | None, supersedes: str | None}`, `TokenUsage{input: int, output: int}`, `Annotation{kind: Literal["refusal","forced","repair_context","recurrence","conflict"], turn_id: int, data: dict}`, `Warning{code: str, detail: str, control_seq: int | None}`, `TrajectoryDelta{turns: list[Turn], annotations: list[Annotation], warnings: list[Warning], session_patch: dict}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trajectory_schema.py
"""trajectory-v1 is a versioned, strict, JSON-round-trippable read model."""
from sag.trajectory.schema import SCHEMA_VERSION, Trajectory, Turn, Warning


def test_schema_version_is_one():
    assert SCHEMA_VERSION == 1


def test_a_minimal_trajectory_round_trips_through_json():
    turn = Turn(turn_id=1, phase="build", iteration=7, actor="model", control_seq=[41, 43])
    doc = Trajectory(
        session={"run_id": "r", "project": "kafka", "wall_clock_seconds": None},
        phases=[], turns=[turn], annotations=[],
        warnings=[Warning(code="missing_tool_result", detail="seq 124", control_seq=124)],
    )
    again = Trajectory.model_validate_json(doc.model_dump_json())
    assert again.schema_version == 1 and again.turns[0].control_seq == [41, 43]


def test_unknown_fields_are_rejected_not_swallowed():
    import pytest
    with pytest.raises(Exception):
        Turn(turn_id=1, phase="build", actor="model", control_seq=[], invented_field=1)
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_trajectory_schema.py -q` — FAIL (module missing).
- [ ] **Step 3: Implement** `schema.py`: pydantic v2 models with `model_config = ConfigDict(extra="forbid")`, `Trajectory.schema_version: int = SCHEMA_VERSION`; `SessionInfo` carries `run_id, project, verdict: str | None, rates: dict | None, wall_clock_seconds: float | None`; export everything from `__init__.py`. Follow `_StrictPayload`'s style in `control_events.py` (extra="forbid", Literal enums).
- [ ] **Step 4: Run the test file** — PASS; run full suite — green.
- [ ] **Step 5: Commit** `feat: trajectory-v1 schema — the versioned read model of a run`

### Task 2: incremental reducer core

**Files:**
- Create: `src/sag/trajectory/reducer.py`
- Test: `tests/test_trajectory_reducer.py`

**Interfaces:**
- Consumes: Task 1 models; raw `control_events.jsonl` lines (each line is one JSON event; parse `kind`/`sequence` fields exactly as found in `logs/session_20260814_072758_651456_4d81644227c3_24117/control_events.jsonl` — pin the field names from those real bytes, do not invent).
- Produces: `class TrajectoryReducer: def __init__(self, *, detail: str = "summary") ...; def feed(self, raw_line: str) -> TrajectoryDelta; def snapshot(self) -> Trajectory`. Turn assembly rule: a `loop_decision` opens a turn; its envelope/tool_result events join by their correlation fields; `forced_action` opens an `actor="controller"` turn; a turn with a missing pairing when the next turn opens seals with a `Warning(code="missing_tool_result" | "missing_envelope")`. `gate_decision`/`gate_outcome_revised` attach `GateInfo` to the owning turn; `phase_transition` updates phase; unknown kinds → `Warning(code="unknown_event_kind")`.

- [ ] **Step 1: Write the failing test** — use REAL lines lifted from the archived kafka d2r3 session (three-line happy path: one loop_decision + its action_envelope + its tool_result, pasted verbatim as fixture strings inside the test), plus a synthetic unknown-kind line:

```python
# tests/test_trajectory_reducer.py (shape; lift the three real lines in Step 1)
from sag.trajectory.reducer import TrajectoryReducer

def test_a_decision_envelope_result_triple_becomes_one_closed_turn():
    r = TrajectoryReducer()
    for line in REAL_TRIPLE:          # verbatim from the kafka session
        r.feed(line)
    snap = r.snapshot()
    assert len(snap.turns) == 1
    t = snap.turns[0]
    assert t.actor == "model" and t.call is not None and t.observation is not None
    assert snap.warnings == []

def test_a_decision_with_no_result_seals_a_warning_when_the_next_turn_opens():
    ...  # feed loop_decision A, then loop_decision B: turn A sealed + warning

def test_an_unknown_kind_is_a_warning_never_an_exception():
    r = TrajectoryReducer()
    delta = r.feed('{"sequence": 999, "kind": "totally_new_event", "payload": {}}')
    assert delta.warnings and delta.warnings[0].code == "unknown_event_kind"
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** the reducer as a plain state machine (no I/O, no file paths — `feed` takes strings, which is what makes live and batch one code path). **Step 4: Test file green, full suite green.** **Step 5: Commit** `feat: one incremental reducer — the same events fold live or replayed`

### Task 3: session builder (batch + follow) and token join

**Files:**
- Create: `src/sag/trajectory/builder.py`
- Test: `tests/test_trajectory_builder.py`

**Interfaces:**
- Consumes: `TrajectoryReducer`; session dir layout (`control_events.jsonl`, `token_usage.csv` with header `iteration,timestamp,type,tool_name,model,total_tokens,prompt_tokens,completion_tokens,...`, `verdict.json` optional while live).
- Produces: `build_trajectory(session_dir: Path, *, detail: str = "summary") -> Trajectory` (batch replay); `follow_trajectory(session_dir: Path, *, poll_seconds: float = 1.0) -> Iterator[TrajectoryDelta]` (tail-follow; yields as lines append; injectable sleep for tests). Token join: executor rows join turns by `iteration`; verdict.json (when present) fills `session.verdict`/`session.rates`.

- [ ] **Step 1: failing test** — build a tmp session dir with 4 real event lines + a 2-row token_usage.csv; assert `build_trajectory` joins tokens (`turns[0].tokens.input == 4134`) and that `follow_trajectory` with an injected clock yields the same turns when the file is written incrementally (idempotence seed: batch == accumulated-follow).
- [ ] **Step 2: FAIL. Step 3: implement (read-only opens, no locks). Step 4: green + full suite. Step 5: Commit** `feat: batch and follow are the same fold over a session directory`

### Task 4: golden fences from archived d2r2/d2r3 sessions

**Files:**
- Create: `tests/fixtures/trajectory/kafka-d2r3/control_events.jsonl` (+`token_usage.csv`), `tests/fixtures/trajectory/camel-quarkus-d2r3/control_events.jsonl` — copied verbatim (`cp`) from `logs/session_20260814_072758_651456_4d81644227c3_24117/` and `logs/session_20260814_093336_763203_12dba3497295_26013/`; committed with `git add -f` so CI does not depend on untracked logs.
- Test: `tests/test_trajectory_golden.py`

**Interfaces:** consumes Tasks 2–3. Produces the two spec §5 anchors.

- [ ] **Step 1: failing tests**

```python
def test_kafka_d2r3_replays_to_exactly_24_calls():
    snap = build_trajectory(FIXTURES / "kafka-d2r3")
    assert sum(1 for t in snap.turns if t.call is not None) == 24  # pinned by slice corpus

def test_camel_quarkus_ten_silent_phase_calls_surface_as_warnings():
    snap = build_trajectory(FIXTURES / "camel-quarkus-d2r3")
    silent = [w for w in snap.warnings if w.code in ("missing_loop_decision", "missing_tool_result")]
    assert len(silent) >= 10  # the ledger holes the slice campaign measured

def test_live_accumulation_equals_batch_replay():
    r = TrajectoryReducer()
    for line in (FIXTURES / "kafka-d2r3" / "control_events.jsonl").read_text().splitlines():
        r.feed(line)
    assert r.snapshot().model_dump() == build_trajectory(FIXTURES / "kafka-d2r3").model_dump() \
        or _tokenless(r.snapshot()) == _tokenless(build_trajectory(FIXTURES / "kafka-d2r3"))
```

(The third assert pins reducer idempotence; `_tokenless` strips builder-only joins so the comparison is over the reducer's own output.)
- [ ] **Steps 2–5:** FAIL → adjust reducer/builder until the pinned facts hold (the slice files `logs/d2r3-serial-20260814/slices/*.md` are the arbiter if a count disagrees — the SLICE is right) → full suite green → Commit `test: the archived campaigns are the trajectory's golden anchors`

### Task 5: `sag trajectory` CLI

**Files:**
- Modify: `src/sag/main.py` (click group at :403; add a command following the existing pattern)
- Test: `tests/test_trajectory_cli.py` (use `click.testing.CliRunner`)

**Interfaces:** `sag trajectory SESSION_DIR [--follow] [--detail summary|full]` → snapshot JSON to stdout (`--follow`: one JSON delta per line until interrupted). Full tier resolves refs through `contexts/full_outputs.jsonl` via the existing reader in `src/sag/agent/output_storage.py`.

- [ ] Steps: failing CliRunner test on the kafka fixture asserting stdout parses as `Trajectory` with 24 calls → FAIL → implement → green + full suite → Commit `feat: sag trajectory — list, get, follow`

### Task 6: TurnRecordPayload in the control schema

**Files:**
- Modify: `src/sag/agent/control_events.py` (add beside `LoopDecisionPayload` at :991)
- Test: `tests/test_turn_records.py`

**Interfaces:** `class TurnRecordPayload(_StrictPayload)` with `turn_id: int`, `phase: str`, `iteration: int | None`, `actor: Literal["model","controller"]`, `window_digest: WindowDigest{system_prompt_sha256: str, component_refs: list[str]}`, `envelope_ref: str | None`, `observation_ref: str | None`, `gate_decision_id: str | None`, `tokens_in: int | None`, `tokens_out: int | None`, `t0: str`, `t1: str`. Event kind string: `"turn_record"`. The reducer (Task 11) will consume it; nothing emits it yet.

- [ ] Steps: failing validation-shape test (extra="forbid"; turn_id ≥ 1; actor literal) → implement payload class + register the kind wherever `control_events.py` enumerates payload kinds (follow how `LoopDecisionPayload` is wired) → green + full suite → Commit `feat: a turn is a sealed record, not an inference`

### Task 7: the engine seals model turns

**Files:**
- Modify: `src/sag/agent/react_engine.py` (the native-turn loop; the same neighborhood as `_native_turn_with_retry`)
- Test: extend `tests/test_turn_records.py`

**Interfaces:** after each model turn completes (envelope + result known), the engine emits one `turn_record` event through the same emission path as `loop_decision`. `window_digest.component_refs` = the refs of the messages-array components the prompt builder used (system prompt version hash + history slice refs + observation refs); bytes for components not already in `full_outputs.jsonl` are stored once via `output_storage` and referenced. `tokens_in/out` read from the same accounting that writes `token_usage.csv` rows (join in-process, not by re-reading the CSV).

- [ ] Steps: failing test drives one engine turn through the existing test harness for engine loops (see `tests/test_native_loop_engine.py` for the house pattern of stubbing the client) and asserts exactly one `turn_record` sealed with monotone `turn_id`, a `window_digest` whose `component_refs` are resolvable, and `t1 >= t0` → implement → green + full suite → Commit `feat: every model turn seals what it saw, said, and heard`

### Task 8: controller turns + forced observations persist

**Files:**
- Modify: `src/sag/agent/react_engine.py` (forced_action path; engine-gate close paths)
- Test: extend `tests/test_turn_records.py`

**Interfaces:** `forced_action` and engine-generated gate closes emit `turn_record{actor: "controller"}`; the forced action's observation is BOTH persisted to branch history (closing the cayenne/ignite/polaris/camel gap, spec §2.2 rule 2) and carried as `observation_ref`.

- [ ] Steps: failing test forces a phase-floor action through the existing forced-action test route and asserts (a) a controller turn record, (b) the observation present in the phase context history (the exact assertion the slice campaigns showed failing) → implement → green + full suite → Commit `feat: the controller's turns are on the record, observations included`

### Task 9: phase-tool parity and typed refusals

**Files:**
- Modify: `src/sag/tools/phase_tool.py`, `src/sag/agent/control_events.py` (typed refusal record), `src/sag/agent/react_engine.py` (emission)
- Test: `tests/test_ledger_closure.py`

**Interfaces:** phase-tool calls emit `loop_decision` like every other tool (camel-quarkus's ten silent calls); a refused call that produces no `tool_result` emits `refusal_record{refusal_code: str, exact_params_sha256: str}` (cassandra/tapestry/camel-quarkus seq 124/138/216 shapes). Reducer maps refusals to `Annotation(kind="refusal")` and stops warning about those turns.

- [ ] Steps: failing tests reconstruct both shapes through the phase tool and a refused call → implement → the camel-quarkus GOLDEN fence flips (warnings for silent calls no longer appear on NEW sessions — the archived fixture keeps its warnings, which is itself asserted) → green + full suite → Commit `feat: no call is silent — a refusal is a record, not an absence`

### Task 10: repair_intent transport closure

**Files:**
- Modify: wherever Task 9's trace shows `repair_intent` dropping between tool params and the control layer (seatunnel shape; start at `phase_tool.py`'s param handling and the envelope builder in `react_engine.py`)
- Test: extend `tests/test_ledger_closure.py`

- [ ] Steps: failing test sends a call with a `repair_intent` block and asserts it appears in the sealed envelope payload → implement → green + full suite → Commit `feat: repair intent survives from the model's hand to the sealed record`

### Task 11: conservation fence + reducer consumes turn records

**Files:**
- Modify: `src/sag/trajectory/reducer.py`; Create: conservation assertions in `tests/test_ledger_closure.py`
- Test: extend golden tests

**Interfaces:** reducer prefers `turn_record` events when present (exact turns; `window_ref` from the record) and falls back to inference for pre-closure sessions (the archived fixtures — their warnings stay, asserted). Conservation fence: for any post-closure session, `#loop_decision == #envelope == #(tool_result ∪ refusal_record)` and `turn_id` has no holes; violation → red test in-engine, `Warning(code="conservation_violation")` in-reducer.

- [ ] Steps: failing conservation test over a freshly recorded synthetic session (drive the engine harness for 3 turns incl. one refusal, then replay through the reducer asserting zero warnings) → implement → green + full suite → Commit `feat: the ledger balances — every decision has its envelope and its answer`

### Task 12: trajectory API endpoints

**Files:**
- Modify: `src/sag/web/app.py` (beside `get_session` at :110-111), `src/sag/web/session_registry.py` (only to expose the session dir path it already knows)
- Test: `tests/test_trajectory_api.py` (FastAPI TestClient, existing house pattern in web tests)

**Interfaces:** `GET /api/sessions/{session_id}/trajectory` → snapshot (query `detail=summary|full`); `GET /api/sessions/{session_id}/trajectory?since=<turn_id>` → `{turns: [...], annotations: [...], warnings: [...]}` delta for live polling. Live sessions read through `session_mirror.ensure_mirror` (the existing no-exec host mirror); the endpoint never `docker exec`s.

- [ ] Steps: failing TestClient test against the kafka fixture mounted as a mirrored session (follow how existing web tests fake `session_registry` entries) asserting 24 calls and a working `since=` cut → implement → green + full suite → Commit `feat: the trajectory is one GET away, whole or since a turn`

### Task 13: webui timeline view

**Files:**
- Create: `webui/src/` timeline route + components (follow the existing router/component conventions in `webui/src`; shadcn components available per `components.json`)
- Modify: webui nav to add the view; rebuild `webui` → refresh `src/sag/web/static/` (the repo's existing build flow: `npm run build` in `webui/`, output copied per `vite.config.ts`)

**Interfaces:** consumes Task 12's endpoints. Rows = turns (model vs controller styling); phase bands; badges for `error_code` / `failure_signature` / recurrence count / gate word with expandable supersedes chain; row expansion renders the quad ([A] window components with per-ref descent, [B] params, [C] observation, [D] next event) fetched at `detail=full`; live mode polls `since=` on the dashboard's existing cadence; every row shows its `control_seq`.

- [ ] Steps: component-level test if the webui has a test setup (check `webui/package.json` scripts; if none exists, the acceptance is a documented manual walkthrough recorded in the commit message — do NOT bolt a new test framework onto the frontend in this task) → implement view → `npm run build`, commit BOTH `webui/src` and rebuilt `src/sag/web/static` assets in one commit → Commit `feat(webui): the run as a timeline — every turn, its badges, its bytes`

### Task 14: sparkline, anomaly marks, and live-follow polish

**Files:**
- Modify: `webui/src` timeline (header sparkline: per-turn tokens + duration from summary tier; anomaly badges for exit 137 / daemon-disappeared / 5xx-retry patterns surfaced by reducer annotations), `src/sag/trajectory/reducer.py` (emit those `Annotation(kind="conflict"| "recurrence")` rows if Task 2 did not already)
- Test: reducer-side assertions in `tests/test_trajectory_reducer.py`; frontend per Task 13's acceptance mode

- [ ] Steps: reducer test for anomaly annotations (an exit-137 tool_result line from the ignite fixture yields an annotation) → implement → rebuild webui → full suite green → Commit `feat: the timeline warns where the run bled — tokens, time, and anomalies`

---

## Self-Review

- **Spec coverage:** §1 pipeline → Tasks 2/3/12; §2.1 turn records → 6/7/8; §2.2 rules 1–4 → 9/10/8; rule 5 → 11; §3 schema/tiers/CLI → 1/5; warnings-not-crashes → 2; §4 timeline items → 13/14; §5 golden fences → 4, TDD throughout, idempotence → 3/4; §6 order → stages A→B→C with A shippable alone. No gaps found.
- **Placeholder scan:** Tasks 3/5/7–12 compress repeated Step 2/4/5 boilerplate into one line each — deliberate; each still names its failing test, implementation site, and commit. No TBDs.
- **Type consistency:** `TrajectoryDelta`/`Trajectory`/`Turn` names match across Tasks 1–5, 11–12; `TurnRecordPayload` fields in Task 6 match what Tasks 7/8 emit and Task 11 consumes.
