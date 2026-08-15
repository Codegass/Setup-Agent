# Observation trajectory — the ledger closes, the trajectory derives, the timeline follows

**Date:** 2026-08-14
**Status:** design approved in discussion; ready for planning
**Reference:** github.com/icesixgod/codex-trajectory (read-only derived ledger,
versioned schema, tiered detail, timeline UI). Adopted ideas: read-only
derivation, schema versioning with graceful unknown-event degradation, detail
tiers, list/get/show access verbs. Deliberately NOT adopted: privacy
redaction (single-owner research tool), MCP plugin form (we have our own web
app), approximate step inference (SAG has exact turns — an advantage the
reference lacks).
**Primary consumer (owner decision):** live webui observation of running
sessions. Post-hoc forensics (slice campaigns, #39 evidence, paper stats)
consume the same derivation.

## 0. The problem, evidenced

Two slice campaigns (d2r2 commit 2e9ebea, d2r3 commit 888ca04) proved the
observation gap is not missing data but two structural defects:

- **The ledger is not closed** (≥10 measured instances): calls with a
  `loop_decision` but no envelope and no `tool_result` (cassandra ×2,
  samza-hello, camel, tapestry-5); `forced_action` observations never
  persisted to branch history (cayenne, ignite, polaris, camel); phase-tool
  calls emitting no `loop_decision` at all (camel-quarkus d2r3: 10 calls,
  turn numbers recovered only by param-matching); `repair_intent` dropped
  between tool params and the control layer (seatunnel); history entries
  with no control event and iterations with no history entry (camel-quarkus
  d2r3). Every trajectory derived today inherits these holes, which is why
  each slice campaign needs per-project archaeology agents plus independent
  byte verification.
- **"What the model saw" ([A]) has no first-class record.** Branch history
  is a truncated, compaction-shaped approximation of the rendered window;
  every observation-poisoning investigation pays a reconstruction tax.

## 1. Architecture

Three layers, one-way data flow, ONE derivation pipeline serving both live
and post-hoc:

```
engine (authoritative, append-only)
  control_events.jsonl + turn records + full_outputs
        │  (tail-follow, incremental)
trajectory reducer (read-only derivation, src/sag/trajectory/)
  event → TrajectoryDelta → trajectory-v1 view
        │  (delta polling / snapshot)
webui timeline (incremental upgrade of the existing app)
```

**The incremental reducer is the only implementation.** Live mode feeds it
events by tailing the session; post-hoc mode replays the same reducer from
the first event. One code path, two feeds, no second derivation (P3). The
batch replay doubles as the idempotence fence: replaying any archived
session must produce a trajectory identical to what live accumulation would
have produced.

The reducer never writes to, reorders, or mutates any source file. Console
logs are never an input (authoritative-sources rule; render inflation burned
two campaign conclusions).

## 2. Pillar 1 — the closure contract (engine side; full scope, owner decision)

### 2.1 Turn records become first-class

Every assistant response seals one `turn_record` in the authoritative layer —
including a response that calls no tool (amendment 2):

- `turn_id` (monotone within the run), `phase`, `iteration`;
- `window_digest` — component-level references for the EXACT messages array
  sent to the model: system-prompt version hash, history slice refs,
  observation refs. Bytes are stored once, content-addressed, in a HOST
  full_outputs-style store beside the ledger (amendment 1); the turn record
  carries refs only. This turns "[A] what did the model see" from archaeology
  into a lookup. A turn taken before this run rendered anything carries
  `window_digest: None` (amendment 3), and a window longer than the record may
  name is cut with a marker naming the drop (amendment 6);
- envelope ref for the call, delivered-observation ref, the gate word if
  this turn carried a gate decision (with `decision_id`), token usage
  (in/out), start/end timestamps.

Controller-initiated actions (`forced_action`, engine-generated gates) seal
turn records with `actor: controller` in the same sequence.

### 2.2 Closure rules (each motivated by a measured gap)

1. Phase-tool calls emit `loop_decision` like every other call
   (camel-quarkus d2r3: ten silent calls).
2. `forced_action` observations persist to branch history AND carry an
   observation ref in their turn record (four projects measured missing).
3. `repair_intent` survives from tool params into the control layer
   (seatunnel d2r2).
4. A refused call gets a `tool_result` or an explicit typed refusal record —
   never silence (cassandra, tapestry-5, camel-quarkus seq 124/138/216).
5. **Conservation fence (as built).** The ledger balances per CALL, with one
   named exception:

   ```
   #action_envelope + #forced_action + #refusal_record
     ==  #tool_result + #refusal_record
     ==  #loop_decision + #cancelled
   ```

   A refusal record stands in both of the places its call never reached, which
   is why it appears on the first two sides. `#cancelled` counts the exception:
   a call CANCELLED by a batch break — a phase transition applied, a loop-driven
   close, a live job barrier — dispatched nothing, so it has no envelope and no
   `tool_result`, and it emits no `loop_decision` either, because the recurrence
   ladder reads outcomes and this call produced none. Fabricating one would feed
   the ladder an execution that never happened. Its record is a
   `refusal_record{refusal_code: CALL_NOT_EXECUTED}` plus the turn it took, and
   the refusal the model actually read is that turn's `observation_ref`.

   Turn records are counted by NOBODY in this formula. Every assistant response
   seals one, including a response that called nothing (amendment 2), so the
   turn sequence is checked for HOLES — `turn_id` monotone by one, spent at
   seal time — rather than against the call counts. Violation is a red test,
   and the reducer surfaces any live violation as a `warnings[]` entry rather
   than crashing.

RunEvidenceState stays engine-written; turn records are sealed by the
engine only, through the same publication authority as other control
events.

## 3. Pillar 2 — trajectory-v1 (derivation layer)

`src/sag/trajectory/`, read-only, versioned schema (`schema_version: 1`),
unknown event kinds degrade to `warnings[]` entries, never exceptions.

```
session:     {run_id, project, verdict?, rates?, wall_clock, warnings[]}
phases[]:    {name, termination?, gates[]}
turns[]:     {turn_id, phase, iteration, actor: model|controller,
              window_ref, call{tool, params_ref},
              observation{ref, error_code?, failure_signature?},
              gate?{word, decision_id, supersedes?},
              tokens{in, out}, t0, t1, control_seq[]}
annotations[]: {kind: refusal|forced|repair_context|recurrence|conflict,
              turn_id, data}
```

- **Detail tiers:** `summary` (names, codes, timing, tokens — the timeline's
  main view) and `full` (verbatim bytes resolved by ref — the [A]/[B]/[C]/[D]
  quad on expand). The full tier IS the slice-corpus shape; slice campaigns,
  #39 evidence collection, and paper statistics consume this output instead
  of re-deriving.
- **CLI:** `sag trajectory <session> [--follow]` — snapshot JSON, or
  tail-follow emitting deltas.
- Until Pillar 1 lands, the reducer consumes today's event types and states
  every known hole honestly in `warnings[]` (e.g. "call at seq N has no
  tool_result"); after Pillar 1, those warnings disappear because the holes
  do.

## 4. Pillar 3 — webui timeline (incremental upgrade, owner decision)

A session-timeline view inside the existing web app. Data comes ONLY from
the trajectory reducer (`session_registry`/`context_trace` stop assembling
contexts independently for this view — single derivation).

- Rows are turns; horizontal phase bands. Controller rows (forced actions,
  engine gates) visually distinct from model rows.
- Row badges: `error_code`, `failure_signature`, recurrence count, gate word
  (supersedes chain expandable).
- Row expansion = the quad: [A] window components (descendable to bytes),
  [B] call params, [C] observation, [D] next event.
- Header sparkline: per-turn tokens and duration; anomaly marks (exit 137,
  daemon death, 5xx retries).
- **Live mode:** frontend polls deltas (reusing session_mirror's follow
  mechanism); the timeline grows as the run progresses. Every row carries
  its `control_seq` so the owner can descend to the byte level at any time.
- No new frontend stack; the existing static app is extended.

## 5. Testing and migration

- **Golden fences from archived sessions:** replaying d2r2/d2r3 sessions
  through the reducer must mechanically reproduce facts the slice corpus
  already pinned (kafka d2r3's 24 calls; camel-quarkus's ten silent phase
  calls appear as warnings pre-Pillar-1 and as conserved turns after).
- Every closure rule lands TDD: red fence first, engine change, suite
  green (the gate-truth cadence).
- Reducer idempotence: live-accumulated == batch-replayed, per session.
- Webui tests extend the existing demo_data path.

## 6. Delivery order

Hard dependency 1→2→3 for completeness, but the live-first goal reorders
the cut: **Pillar 2's reducer starts in parallel with Pillar 1** (consuming
today's events, stating today's holes as warnings), so the timeline runs on
real sessions in the first increment; Pillar 1 then closes the holes under
it; Pillar 3 consumes the reducer. Implementation runs under ultracode with
Opus agents; the Pillar 1 engine contract is decision-gated by the owner
side.

## 7. Amendments

### 2026-08-15 — Stage B round 2 (owner decisions, as built)

1. **A record's bytes are HOST bytes.** Window components and delivered
   observations are written to `contexts/full_outputs.jsonl` of the SESSION
   directory — never through the container-backed output store. They are bytes
   the engine rendered; the container never had them, and routing them there
   cost a write plus an index read and rewrite per component, on a window that
   re-renders every turn. Measured on a 20-turn synthetic run, container-side
   against host-side: 150 → 0 execs for sealing (7.5/turn → 0), 55,184 → 0
   bytes of the container's `full_outputs.jsonl`, whole-run container execs
   362 → 102, `turn_record` ledger growth unchanged at 966 B/turn, host store
   56,224 B in 65 records for 422 refs named (one record per distinct body).
   Storage is content-addressed by sha256, so the system prompt is written
   once per run and an observation already written when it was delivered is
   referenced by every later window that carries it.

   ONE ref namespace: a session may hold two `full_outputs.jsonl` files — the
   engine's (host, live) and the container's (recorded under `.setup_agent/` by
   `--record`) — and the full tier asks every store the session has, because a
   ref never says which file answers it.

2. **Every assistant response seals a turn record**, thought-only turns
   included: `envelope_ref: None`, `observation_ref` naming the continuation
   cue the model read next. Conservation counts CALLS (§2.2 rule 5), so such a
   turn adds nothing to either side of the fence and breaks nothing.

3. **A turn taken before any model render carries `window_digest: None`**, not
   sha256 of the empty string: an empty-string hash is a 64-hex prompt identity
   nobody observed, which resolves to nothing and compares equal across every
   run that ever sealed one. A controller turn beside a rendered window still
   names that prompt, with no components.

4. **The row's [C] is what the model READ.** The reducer adopts
   `turn_record.observation_ref` (the delivered text) as `observation.ref`, and
   the tool's own `output_ref` moves to `observation.evidence_ref` rather than
   being dropped. The full tier resolves both copies.

5. **Arming discipline.** The outside-ladder guard in `LoopMemory.observe`
   runs before any state is read or written, so a call outside the ladder can
   never disarm `force_break_armed`/`_armed_key`. Membership is by TOOL for
   `phase`/`manage_context`/`report`, and by the engine's own statement
   (`LoopEvent.outside_ladder`, recorded in the event so replay re-derives it)
   for the phase-entry advisor consult — the harness authoring a question
   between two of the model's calls may not cancel the break the model's own
   repetition armed. An advisor call the MODEL made is read by the ladder as
   before.

6. **A window over 2,048 components is cut, never refused.** The record keeps
   the newest components that fit, in render order, and spends its first slot
   on a `window_truncated:<n>` marker naming how many older ones it could not
   name, with a warning-grade log. Sealing runs inside the loop's exception
   handler, so raising there ended the run — an observability record may never
   do that (§3).
