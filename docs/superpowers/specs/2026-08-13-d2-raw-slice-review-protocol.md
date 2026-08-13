# D2 re-run raw-slice review protocol

**Date:** 2026-08-13
**Owner requirement (verbatim intent):** every log reviewed by an Opus/Sonnet
agent; failures collected with the ORIGINAL failure paths — what context the
model faced, what tool call it made, what observation came back — as
unprocessed raw material, so the owner can form an independent judgment.
Summaries alone are explicitly not acceptable.

## 1. Sources of truth

All extraction comes from the authoritative layer only:

- `logs/session_*/.setup_agent/control_events.jsonl` — one event per line:
  `loop_decision` (the model's call + args), `tool_result` (what came back),
  `gate_decision`, `phase_transition`, `evidence_close`.
- `logs/session_*/.setup_agent/verdict.json` — sealed outcome;
  `phase_records[].termination/reason` is the true end-of-run cause.
- `logs/session_*/.setup_agent/contexts/phase_*.json` — the branch history
  whose observation entries are what the model actually had in context.
- `logs/session_*/.setup_agent/contexts/full_outputs.jsonl` — complete tool
  outputs by `output_ref`, for observations the context window truncated.

Console logs are prose, never data: they re-render history on every save and
have already produced two false campaign conclusions (memory:
authoritative-sources-not-renders).

## 2. Per-project review agents

One agent per project, 23 total: Opus 5 for every project whose verdict is
not `success` or whose run carries an abort/blocked phase; Sonnet is
acceptable for clean successes. Each agent writes
`logs/<campaign>/slices/<project>.md` and MUST NOT paraphrase inside a slice.

## 3. The slice format — verbatim or it does not count

For every failure point (aborted phase, blocked phase, failed verdict, and
each distinct tool-failure signature), one slice:

```markdown
### Slice <n>: <one-line locator, e.g. "build phase, turn 14, env register refused">

**Where:** session <dir>, control_events sequence <id>, phase <p>, turn <k>

**[A] What the model had in context immediately before the call**
(verbatim from the branch history observation entries / prior tool_result;
fenced, unedited, truncation marked with `[...N chars omitted, see <ref>]`)

**[B] The call the model made**
(verbatim `loop_decision` args JSON)

**[C] What came back**
(verbatim `tool_result` — error, error_code, suggestions, facts; if the
inline output was truncated, the full text from full_outputs.jsonl by ref)

**[D] What happened next** (one line, mechanical: the next event kind only)
```

Rules: [A]-[C] are quotations, not descriptions. No adjectives, no diagnosis
inside a slice. Every slice carries the event ids and file paths so the owner
can descend to the byte level. An agent that cannot locate [A] for a slice
says "context entry not persisted for this turn" rather than reconstructing.

## 4. Aggregation

`docs/superpowers/reports/<campaign>-failure-slices.md`:

1. **Index** — one row per project: verdict, phase terminations, slice count,
   link to its slice file.
2. **The raw corpus** — every project's slices, concatenated unmodified.
3. **Judgment — clearly fenced off** in a final section marked "以下是我的
   判断,以上是原料": per-failure attribution (model / harness-observation /
   harness-availability / environment / project), cross-project patterns,
   and proposed tasks. Nothing from this section may leak upward into the
   slice sections.

## 5. Verification pass

Before the aggregate is handed to the owner, one independent agent samples 3
random slices per failed project and byte-compares every quoted block against
the source files. A slice that fails the byte-compare disqualifies its
project's file back to its review agent. This is the fence against the
review layer itself paraphrasing — the failure mode this protocol exists to
prevent.
