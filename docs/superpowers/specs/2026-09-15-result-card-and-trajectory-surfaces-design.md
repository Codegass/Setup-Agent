# One result card, one trajectory: the CLI and Workbench surfaces after metrics-v2 — design

- **Date:** 2026-09-15
- **Status:** Approved in brainstorming (four owner decisions recorded in §0); ready for an implementation plan.
- **Relationship:** presentation-layer successor to
  [2026-06-19 Workbench detail redesign](2026-06-19-workbench-detail-redesign-design.md),
  [2026-08-14 observation trajectory](2026-08-14-observation-trajectory-design.md) and
  [2026-08-10 rate-banded verdict](2026-08-10-rate-banded-verdict-design.md) §5 ("CLI banner and report lead with the rate lines").
  It changes no judgment, no sealed format, and no evidence rule. The normative measurement vocabulary stays
  [SAG-MS-1](2026-08-27-sag-ms-1-measurement-standard.md) and the
  [CI-defined build scope](2026-09-07-ci-defined-build-scope-design.md) note.

## 0. Decisions locked in brainstorming

| # | Decision | Owner choice |
|---|---|---|
| D1 | `sag project --ui` (rich Live full-screen mode) | **Retire.** `src/sag/ui/` and its four test files are deleted. The default console becomes the turn stream (§2.2). |
| D2 | Workbench Flow (ContextTrace) vs Timeline (trajectory-v1) | **Merge into one Trajectory tab driven only by trajectory-v1.** `FlowTab`, `ContextTrace`, `ActionDetailModal` are deleted from the frontend; the reducer gains human summaries (§3). |
| D3 | metrics-v2 evidence layers (claimed subjects/cases, receipt executions, quarantined/unattributed/stale observations) | **Collapsed by default with plain-English labels; complete in JSON.** They leave the CLI end-of-run block and the report header. |
| D4 | Scope of the shared result card | **CLI + Workbench + the Markdown setup report summary.** Three surfaces render one model. |

## 1. Problem, evidenced

The sealed result is one artifact (`/workspace/.setup_agent/verdict.json`, schema v5) carrying four independent measurements: the setup verdict, required-task completion, build/test outcomes, and the official-CI comparison. Three surfaces translate it three different ways:

- **CLI** prints `render_snapshot_metric_lines` + `render_ci_comparison_lines` + `render_task_completion_lines` + `format_evidence_layer_lines` (`src/sag/main.py:75-91`), and the agent prints an overlapping `Setup Summary` panel first (`src/sag/agent/agent.py:1891-1968`) with the same evidence-layer lines under different bullets, followed by `Connect with: setup-agent connect <project>`, a command that does not exist. On a clean commons-cli run the user reads seven `unavailable (module-qualified subject/case identity was not sealed)` lines twice.
- **Workbench** composes its own headline in `src/sag/web/verdict.py` from `BuildSummary`/`TestSummary` rollups ("Build passed on 3 of 3 modules"), a scan-based module ratio the scope note demoted to diagnostic. The structured `ciComparison`, `taskCompletion` and `rates` objects are shipped but never read (`webui/src/api/types.ts:284-290` types them as `{status}`); the UI renders pre-baked strings instead. Two run models coexist: Flow reads `ContextTrace` iterations, Timeline reads trajectory turns, and nothing maps "iteration 7" to "Turn 23". The rail's "Build coverage" / "Run results" stat cards are invented concepts.
- **Report** prints the same bold header lines and a "Metrics-v2 Evidence Layers" section (`src/sag/tools/report_tool.py:909-919`, `:5185-5192`).

During a run the console is a log firehose: on a measured 36-iteration session, 2,813 of ~3,100 INFO lines are `Executing command in container: …` (88%). `--ui` mode is structurally disconnected: its phase vocabulary is `setup/build/test/verification` while the engine runs `provision/analyze/build/test/report`, and the engine emits only two of the event kinds it consumes.

## 2. The result card (backend, shared)

### 2.1 Definition

New module `src/sag/result_card.py`. A pure presentation model, derived once, rendered three times. It never recomputes a judgment: every `status` is copied from the seal, every number is copied from the seal or a sealed sibling artifact, and every absence is stated with its reason.

```python
RowKey = Literal["setup", "task", "build", "tests", "coverage", "ci", "report"]
Tone   = Literal["success", "attention", "failed", "neutral"]

class ResultRow(BaseModel):
    key: RowKey
    label: str                       # "Setup" | "Required task" | "Build" | "Tests" | "Coverage" | "Official CI" | "Report"
    status: str                      # machine word copied from the seal (table below)
    tone: Tone
    headline: str                    # ≤ 72 chars, the one thing to read
    detail: str | None = None        # ≤ 120 chars, denominators and provenance
    reason: str | None = None        # present iff the row is unavailable / not compared / not supplied
    items: tuple[str, ...] = ()      # sub-lines: task steps, CI findings, coverage sources
    refs: tuple[str, ...] = ()       # receipt ids, evidence refs, host paths

class ResultStats(BaseModel):        # every field nullable; absence is stated by the renderer
    phases_completed: int | None; phases_total: int | None
    turns: int | None; tool_calls: int | None; tool_failures: int | None
    tokens_in: int | None; tokens_out: int | None
    wall_clock_seconds: float | None
    model: str | None; advisor_model: str | None

class AttentionItem(BaseModel):
    kind: Literal["failing_tests", "task_step", "blocked_phase", "ci_finding", "build_warning", "report"]
    title: str; detail: str | None; refs: tuple[str, ...] = ()

class RunResultCard(BaseModel):
    schema_version: Literal[1] = 1
    run_id: str
    project: str | None; goal: str | None
    commit: str | None               # full SHA; renderers shorten to 7
    container: str | None; session_dir: str | None
    verdict: Literal["success", "partial", "failed", "unknown"]
    verdict_source: Literal["snapshot", "legacy", "unavailable"]
    rows: tuple[ResultRow, ...]      # exactly the seven keys, in RowKey order, always present
    stats: ResultStats
    attention: tuple[AttentionItem, ...]
    notes: tuple[str, ...]           # plain-English data notes from sealed conflicts
```

`build_result_card(snapshot, *, module_metrics=None, report_metrics=None, run_pin=None, token_usage=None, control_events_path=None, termination=None, project=None, container=None, session_dir=None, report_path=None, goal=None) -> RunResultCard`. `snapshot` is a `RunVerdictSnapshot` or its `model_dump(mode="json")` dict (the report tool holds the dict). Every keyword input is optional; a missing input degrades the affected fields to `None` and, where a row depends on it, to a stated reason. The card is built on the host after `read_live_verdict_snapshot` (CLI), in `session_registry` (Workbench), and inside the report tool from the canonical snapshot dict (report).

### 2.2 Row semantics

| key | `status` (copied from) | headline | detail | tone |
|---|---|---|---|---|
| setup | `snapshot.verdict` | `{phases_completed}/{phases_total} phases · {turns} turns · {tool_calls} tool calls · {duration}` (each piece dropped when null) | termination when not `completed`: `aborted during test: {reason}`; blocked phase: `blocked at {phase}: {gate reason}` | success→success, partial→attention, failed→failed, unknown→attention |
| task | `task_completion.status`; `not supplied` when the key is absent | `{status} {n}/{m} steps` (`step definition unavailable` when steps are empty) | first step's `{command} → exit {code} · JDK {major}.{minor} · Maven {version}` when a receipt is bound | complete→success, incomplete→failed, unavailable→attention, not supplied→neutral |
| build | `build_evidence.judgment` | `{succeeded}/{total} modules built` when `reactor_modules_*` are present, else `{judgment}` | `{compiled_classes} class files · {jars} jars · counts are diagnostic, CI defines scope` (jars from `module_summary`) | success→success, partial→attention, failed→failed, unknown→attention |
| tests | `test_stats.judgment` mapped to the words `executed` / `interrupted` / `failed to run` / `unavailable` | `{executed} executed · {passed} passed · {failed} failed · {errors} errors · {skipped} skipped` from `test_stats.unique`; `≥N` prefix when the sealed count is a lower bound | `{rate} of non-skipped passed` with the `<100%` guard, omitted when the non-skipped denominator is 0; `{raw.executed} raw executions` appended when raw ≠ unique | `failed to run`→failed; `interrupted`→attention; executed with failed+errors > 0→attention; executed clean→success; unavailable→attention |
| coverage | `rates.coverage.status` (`collected` / `unavailable`); `not collected` is the word shown for unavailable | `{line_rate}% line coverage` / `not collected` | `source {source}` / `run with --coverage` | neutral |
| ci | `ci_comparison.attainment.verdict` when status is `evaluated`; else `not compared` | evaluated: `{verdict} {alpha.n}/{alpha.d}` or `{verdict} · scope score unavailable`; not compared: `not compared` | evaluated: `cell "{cell_id}" · lifecycle {parity.status}` (+ ` · missing {…}` / ` · extra {…}`); not compared: the plain-English gloss of the first reason | met/exceeded→success, partial→attention, not_met/invalid→failed, not compared→neutral |
| report | `termination.report_delivery_status` (`delivered` / `failed` / `skipped`); `unavailable` when no termination is known | host path of `setup-report-*.md` when recorded, else the container path | — | delivered→neutral, failed→attention, skipped→neutral |

Sub-lines (`items`):

- task: one per step, `{id}: {status} — {command} → exit {code}` + `; {reason}` when present.
- ci: one per reason code, `{CODE}: {gloss}`; and when `unexpected_red_ids` is non-empty, `red beyond CI: {n} tests` followed by up to 10 ids.
- tests: failing test names grouped by module are not items; they are `attention` entries (below).

`refs`: task → receipt ids; build/tests → evidence refs from the seal; report → the report path; ci → `receipt_ids` and `target_record_sha256`.

Rules the card must obey (each is a unit test):

1. **No coercion.** A `None` count is never rendered as `0`. An `unavailable` row always has a non-empty `reason`.
2. **Every fraction names its denominator** in `detail`, never in the headline alone.
3. **Bounds and guards** survive: `≥N` for `availability: partial, bound: lower`; `<100%` when a rounded rate would otherwise print `100%` with red results present (reuse `verdict_rates.format_rate` semantics).
4. **The card copies, it does not judge.** `verdict`, every `status`, and every number come from the seal or a sealed sibling. Conflicts become `notes`, never a different word.
5. **Row order and count are fixed.** Seven rows, always, so the CLI block, the Workbench band and the report table line up across runs.
6. **Tone of the whole is the setup row's tone.** Row tones are independent.

### 2.3 Reason glosses

`REASON_GLOSS: Mapping[str, str]` in `result_card.py` maps every code in these vocabularies to one plain-English sentence:

- `metrics/attainment.py` gate and finding codes (`CERTIFICATE_AUTHORITY_UNAVAILABLE`, `COUNTS_NOT_RECEIPT_BOUND`, `TARGET_WITHOUT_COUNTS`, `TARGET_TEST_UNIVERSE_EMPTY`, `TARGET_BUILD_UNIVERSE_UNAVAILABLE`, `COMPARISON_SUBJECT_UNAVAILABLE`, `COMPARISON_SUBJECT_MISMATCH`, `NEW_RED_BEYOND_TARGET`, `CLEAN_BY_COUNTS_ONLY`, `MODULES_BELOW_TARGET`, `BUILD_AXIS_NOT_SUCCESSFUL`, `EXECUTION_BELOW_TARGET`);
- `agent/ci_comparison.py` snapshot reasons (`official_ci_target_not_supplied`, `official_ci_cell_not_matched`, `certificate_adapter_unavailable`, `CI_TEST_IDENTITIES_NOT_COMPARABLE`, `CI_TEST_SCOPE_OVERLAP_UNRESOLVED`, `current_run_checkout_unavailable`, `current_checkout_differs_from_run_pin`, `fixed_task_completion_unavailable`, `fixed_task_repository_or_revision_mismatch`, `fixed_task_certificate_requires_jvm_runner`, `accepted_execution_plan_unavailable`, `current_receipt_or_assessment_publication_unavailable`);
- `agent/acceptance_task.py` snapshot reasons (`task_execution_scope_unavailable`, `task_run_pin_unavailable`, `task_definition_missing_or_changed`, `task_repository_or_revision_mismatch`, `task_current_checkout_mismatch`, `task_source_changed_or_unverified`, `task_receipt_publication_unavailable`);
- `agent/java_success_certificates.py` typed reason codes and blocked-axis codes;
- the sealed conflict ids listed in `verdict.py`, `verdict_rates.py`, `case_census.py` and `verdict_finalizer.py` (today's `DATA_NOTE_COPY` in `webui/src/evidencePresentation.ts:74-83` moves here and grows to the full list).

A code without a gloss renders as the bare code; it is never dropped. A test enumerates the code constants from each source module and asserts the gloss table covers them, so a new code cannot ship unglossed.

Example glosses: `official_ci_cell_not_matched` → "no CI job on this commit matches the run's JDK and OS"; `TARGET_TEST_UNIVERSE_EMPTY` → "the CI job recorded no test identities to compare against"; `NEW_RED_BEYOND_TARGET` → "tests failed here that pass in CI"; `task_current_checkout_mismatch` → "the workspace is not at the task's frozen commit".

### 2.4 Attention and notes

`attention` is the ordered list the Overview leads with and the CLI prints under the block when non-empty:

1. `task_step` for every step whose status is not `complete`;
2. `blocked_phase` for a phase record with `termination != "completed"` or a gate `signal == "blocked"`;
3. `failing_tests` per module with `failing_count > 0` (from `module_metrics.json`; `title = "{module} · {n} failing"`, `detail` = up to 5 names + `+N more`);
4. `ci_finding` per finding code (`NEW_RED_BEYOND_TARGET`, `MODULES_BELOW_TARGET`, `EXECUTION_BELOW_TARGET`, `BUILD_AXIS_NOT_SUCCESSFUL`);
5. `build_warning` per build warning; `report` when delivery failed.

`notes` are the glosses of `snapshot.conflicts`, in seal order, deduplicated.

### 2.5 Sourcing

| Card field | Source | Availability |
|---|---|---|
| verdict, rows setup/build/tests/coverage/ci/task/report status and numbers | `verdict.json` v5 (`RunVerdictSnapshot`) | v4 snapshots: task and ci rows read `not supplied` / `not compared` with reason `sealed before the run recorded it`; v3 (legacy) sets `verdict_source = legacy` and every row except setup carries reason `legacy report reconstruction` |
| jars, failing names, per-module rollup | `module_metrics.json` | optional |
| lower-bound counts, evidence accounting | `report_metrics.json` (metrics-v2) | optional; used only for `≥N` and the collapsed accounting block (§4.5) |
| model, advisor model | `run-pin.json` (`action_model`, `advisor`) | optional |
| tokens | `token_usage.csv` (same join as `trajectory/builder.py`) | optional |
| turns, tool calls, failures, phases, duration | the trajectory session (`build_trajectory(session_dir).session` + turn count; `turns` with `observation.outcome == "failed"`) | optional; absent inside the container (report) |
| goal | trunk context `goal` (as `session_registry` reads it today) | optional |
| report path | `_save_setup_artifacts` result (host) or `/workspace/setup-report-*.md` (container) | optional |

## 3. Trajectory summaries (reducer, shared)

The trajectory-v1 summary tier carries refs, codes and timing but nothing a person can scan. The CLI turn stream and the Workbench Trajectory rows both need a one-line call and a one-line outcome. They are derived in `TrajectoryReducer` from payloads the ledger already holds, so both surfaces show identical rows.

Schema additions in `src/sag/trajectory/schema.py` (all optional; `schema_version` stays 1; the docstring records the addition and the date):

```python
class CallInfo:        summary: str | None = None            # ≤ 80 chars
class ObservationInfo: outcome: Literal["ok", "failed", "refused", "pending", "cancelled"] | None = None
                       summary: str | None = None            # ≤ 80 chars
class PhaseInfo:       validator_state: Literal["green", "partial", "red", "unavailable"] | None = None
                       reason: str | None = None
                       key_results: str | None = None        # ≤ 400 chars, from the sealing gate_decision
```

`call.summary` from `ActionEnvelopePayload.exact_params`, per tool:

| tool | summary |
|---|---|
| `build` | `{action} {command}` (e.g. `verify mvn clean verify`); `command` first line, whitespace collapsed |
| `bash` | the command, first line, whitespace collapsed |
| `project` | `clone {owner/name}@{ref7}`, `provision {java_distribution or "jdk"} {java_version}` / `provision maven {maven_version}`, `analyze`, `env {keys}` |
| `files` | `{action} {path}` |
| `search` | `{target}` + ` /{pattern}/` when a pattern is present, else `{query}` |
| `phase` | `{signal} {phase}` (`done build`, `blocked test`) |
| `advisor` | `consult` |
| `report` | `generate` |
| other / forced action | `{k}={v}` for the first three scalar params |

Truncation appends `…` at 80 characters. Secrets are not a concern: params are already bounded and sealed in the ledger.

`observation.outcome` and `observation.summary` from `ToolResultPayload.result`, refusal records, and cancellation:

| condition | outcome | summary |
|---|---|---|
| `refusal_record` | `refused` | `{refusal_code}` + reason |
| cancelled call (`CALL_NOT_EXECUTED`) | `cancelled` | `cancelled: {reason}` |
| `invocation_status` in `{dispatched, running}` or a job id without a terminal exit | `pending` | `running · job {id}` |
| `operation_outcome == "success"` | `ok` | tool-specific facts: build → `exit {code} · {n} tests · {f} F · {e} E · {s} S · {jars} jars`; project provision → `{tool} {version}`; project clone → `{sha7} → {path}`; phase → `gate {word} · {reason}`; advisor → `advice delivered`; else `exit {code}` or `ok` |
| otherwise | `failed` | `exit {code} · {error_code or first line of the diagnostic tail}` |

Duration is not a field: renderers compute it from `t0`/`t1` as `lib/trajectory.ts` already does.

Golden fixtures under `tests/fixtures/trajectory/*-d2r3/` are re-pinned with the new fields; the batch/follow idempotence identity and every existing count fence stay.

## 4. CLI

### 4.1 End-of-run block

`_render_setup_cli_result` builds the card and renders `render_result_block(card, width)` from the new `src/sag/console/result_block.py`. Plain text with Rich markup for tone only (status words colored; nothing else). Exact layout at width 78:

```
── commons-cli · e171117 · sag-commons-cli ─────────────────────────────────
 Setup         success        5/5 phases · 12 turns · 20 tool calls · 6m 30s
 Required task complete 1/1   mvn clean verify → exit 0 · JDK 17.0.20 · Maven 3.9.9
 Build         success        1/1 modules built · 119 class files · 4 jars
 Tests         executed       994 executed · 933 passed · 0 failed · 0 errors · 61 skipped
                              100% of non-skipped passed
 Coverage      not collected  run with --coverage
 Official CI   not compared   no CI job on this commit matches the run's JDK and OS
                              (official_ci_cell_not_matched)
 Report        delivered      logs/session_20260914_210609_…/setup-report-20260914-211444.md
────────────────────────────────────────────────────────────────────────────
 Evidence      logs/session_20260914_210609_965730_e39856b237f2_9183
               verdict.json · control_events.jsonl · 2 receipts
 Next          uv run sag ui        uv run sag inspect sag-commons-cli --phase build
```

- Column 1 is the label (14 wide), column 2 the status word (14 wide), column 3 headline then detail on the next line when present; `reason` prints under the headline with the code in parentheses. `items` print as indented lines under their row (task steps always; CI findings always; capped at 12 lines per row with `+N more`).
- `attention` prints as a block titled ` Needs attention` between the rule and Evidence when non-empty, one line per item.
- `notes` print as ` Notes` lines under Evidence when non-empty.
- Non-success verdicts print exactly one closing line: `Setup verdict: partial · exit 1`. Success prints nothing after the block. The `✅ Project 'X' setup completed!`, `Next steps`, `⚠️ Project setup needs attention`, and the agent's `Setup Summary` panel, `Project setup completed successfully.` and `Connect with: setup-agent connect …` lines are removed (`agent.py:_provide_setup_summary` is deleted; the CLI owns the end of the run).
- Exit code: unchanged rule, `0` iff `snapshot.verdict == "success"`.
- `sag run` (task mode) renders a two-row card (`task`, `report`) from the legacy task outcome via the same renderer and exits `1` when the task is reported incomplete (today it always exits `0`).

The evidence-layer lines (`Claimed latest subjects` … `Evidence transport`) no longer print here (D3). They remain in `report_metrics.json` and in `sag result --json`.

### 4.2 Turn stream (live console)

New `src/sag/console/turn_stream.py`: `TurnStreamRenderer(out, *, tty: bool, clock)` consumes control-event lines, feeds a `TrajectoryReducer`, and writes one line per turn. It is attached in `main.py` for `project` and `run` through a second observer on the session's `ControlEventSink` (the existing `mirror` slot keeps feeding the container mirror; the sink gains an `observers` tuple). Console logs are never an input.

Line grammar (`#` column 4 wide, tool 9 wide, summary 44 wide, duration right-aligned 7 wide):

```
▸ provision
  #1   project   clone apache/commons-cli@e171117               3.0s  ok · e171117 → /workspace/commons-cli
  #2   project   provision openjdk 17                          35.8s  ok · openjdk 17.0.20
  #3   phase     done provision                                 0.4s  gate success · workspace /workspace/commons-cli exists
  ✓ provision advanced
▸ build
  #7   advisor   consult                                       12.1s  advice delivered
  #8   build     verify mvn clean verify                       41.0s  exit 1 · Maven 3.8.7 below enforced minimum 3.9
  #9   project   provision maven 3.9                            8.2s  ok · Apache Maven 3.9.9
  #10  build     verify mvn clean verify                      2m10s  exit 0 · 994 tests · 0 F · 0 E · 61 S · 4 jars
  #11  phase     done build                                     0.5s  gate success
  ✓ build advanced
▸ test
  #12  ⚙ engine  evidence close                                       test terminated · receipt inv-maven-1-…-0002 reused
```

- `▸ {phase}` opens a band on the first turn of a phase (or a re-entry, shown as `▸ build (re-entry 2)`); `✓ {phase} advanced` / `✗ {phase} blocked · {reason}` / `→ {phase} repair · {reason}` closes it from `phase_transition`.
- The dispatch line is written **without a trailing newline** when the envelope is seen; the outcome is appended on the same line when the turn seals. If anything else was printed in between (a job progress line, a warning), the outcome is printed as a fresh indented line `      ↳ exit 0 · …`. This yields one line per turn in the common case and never loses an outcome. Both TTY and non-TTY streams use the same rule; TTY additionally colors the outcome by tone.
- Controller turns print `⚙ engine` in the tool column.
- Detached jobs: `job_barrier_wait` events print `      … still running 5m00s · log 1.2 MB · progressing` at most once per 60 s per job; `job_live_at_close` prints `      ! job {id} still live at close`.
- Reducer `warnings` print once each as `  ! {code}: {detail}` (dim), so a ledger hole is visible live, not only in the Workbench.
- Refusals: `#n  {tool}  {summary}   —  refused · {code}` in the attention tone.
- Width adapts to the terminal (`shutil.get_terminal_size`), minimum 80; summaries truncate with `…`.

Logging changes (`src/sag/config/logger.py`): the console sink defaults to **WARNING** and above; `--verbose` sets it to DEBUG with the source-location format. `SessionLogger._setup_loggers` and the module-level `setup_console_logging` share one implementation so the quiet setting is no longer undone when agent logging starts. `suppress_console_logging` and `ui_mode` are deleted with `src/sag/ui/`. The duplicated `🔧 ACTION:` line (`react_engine.py:8716-8717`) and the untruncated `👁️ OBSERVATION:` INFO line move to the `AGENT_TRACE` file sink only. All file sinks are unchanged.

### 4.3 Commands

| command | change |
|---|---|
| `sag project` | turn stream + result block; `--ui` removed; `--verbose` unchanged in meaning |
| `sag run` | turn stream + two-row card; exit `1` when the task is reported incomplete |
| `sag result <container \| session_dir> [--json]` | **new.** Reads the sealed verdict from a live container (`read_live_verdict_snapshot`) or a recorded session (`<dir>/.setup_agent/verdict.json` or `<dir>/verdict.json`), builds the card with every sibling artifact the location offers, and prints the block; `--json` prints `RunResultCard.model_dump(mode="json")`. Exit `1` only when nothing sealed can be read. |
| `sag trajectory <session_dir> [--format table\|json] [--follow] [--detail summary\|full]` | `--format table` (default) prints the turn stream renderer over the whole session, preceded by one session line (`{project} · {run_id} · verdict {verdict} · {turns} turns · {duration}`); `--format json` prints today's document; `--follow` works with both (table appends lines; json emits deltas). `--detail` applies to json only; combining it with table is a usage error (exit 2). |
| `sag inspect <target> [--phase] [--iter] [--session]` | unchanged output; the welcome panel is no longer printed (it prints only for `project` and `run`). New `--turn N`: prints one turn's exact parameters (`read_call_envelope`), its outcome line, the model-visible result bytes at the full tier, the gate if any, and evidence refs, from the session directory (`--session`) or the container's latest recorded session as `session_registry` resolves it. |
| `sag list` | columns `Project · Container · State · Setup · Required task · Tests · Updated`; `Setup` is the verdict word, `Required task` is `n/m` or `—`, `Tests` is `{passed}/{executed}` with `+{f+e} red` when non-zero, `Updated` is the latest session finish. Rows come from `ReadModelBuilder().dashboard()` so the CLI and the rail agree; the `Last Comment` column and the footer hint are removed. |
| `sag version`, `sag shell`, `sag remove`, `sag ui` | unchanged |

Stale strings removed everywhere: `setup-agent connect`, `setup-agent continue`, `Thinking model calls`, the `Final TODO List Status` block, `manage_context` in `ui/`.

## 5. Workbench

### 5.1 Read model

`ExecutionSessionDetail` gains `result_card: RunResultCard | None` (`resultCard`) built in `session_registry` from the same inputs as §2.5 (the registry already reads the seal, `module_metrics.json`, `report_metrics.json` and the trunk goal). It loses `verdict`, `ci_comparison_lines`, `task_completion_lines`; `src/sag/web/verdict.py` and `VerdictSummary` are deleted. `ci_comparison`, `task_completion`, `rates`, `test`, `build`, `modules`, `module_summary`, `evidence`, `logs`, `report`, `report_doc`, `snapshot_status`, `legacy`, `report_delivery_status`, `model`, `steps`, `step_budget` stay. `context` (ContextTrace) stays in the API for now and is no longer read by the frontend; its removal is a separate follow-up. `files` and `FileChangeDigest` are removed from the API and the UI (always `None` in the real path).

`WorkspaceSummary` gains `result: WorkspaceResult | None` = `{verdict, task: {status, completed, required} | None, tests: {executed, passed, failed, errors, skipped} | None, ci: {status, verdict} | None}` for the rail, derived from the latest session's card.

`demo_data.py` produces a card and a small trajectory document (three phases, ~12 turns with summaries, one refusal, one controller turn) for every demo session so the Trajectory tab renders in `--demo` and in vitest.

### 5.2 TypeScript types

`api/types.ts` mirrors `RunResultCard`, `ResultRow`, `AttentionItem`, `WorkspaceResult`; replaces the `{status}` stubs with full `CIComparisonSnapshot` (`attainment` with `alpha`/`alpha_test`/`alpha_build`/`lifecycle_parity`/`reason_codes`/`unexpected_red_ids`/`cell_id`/`cell_grade`/`modules_basis`/`build_form`, `reasons`, `receipt_ids`, `commands`, `acceptance_command`, `test_identity_basis`) and `TaskCompletionSnapshot` (`steps[]` with `id`/`command`/`status`/`receipt_id`/`exit_code`/`reason`, `reasons`); types `rates` as `{build: {modules: GrainRate, classes: GrainRate}, test: {cases: GrainRate, modules: GrainRate}, coverage: CoverageRate}` with `GrainRate` the three-shape union from `verdict_rates.GrainRate.payload()`. Trajectory types gain the §3 fields.

### 5.3 Rail

- The three chips and two stat cards are replaced by one status strip: `6 workspaces · 1 running · 1 needs attention`; "needs attention" is a toggle that filters the list. `SummaryStrip.tsx` and `rollup()` are deleted.
- Each row: project (bold) · container (mono) · state dot; a right-aligned four-cell result strip from `workspace.result`: Setup word (toned), Required task `1/1` or `—`, Tests `933/994` with a red `+2` when failed+errors > 0, CI `met` / `—`. Attention ordering (`dashboardAttention.ts`) adds: task incomplete, CI `not_met`/`invalid`, verdict `failed`.

### 5.4 Detail pane

Header keeps its anatomy; the metadata line reads from the card (`container · stack · commit7 · model · {turns} turns · {duration} · finished {ago}`), dropping nulls as today.

**Result band** replaces `VerdictBand`: the seven rows rendered as a compact list — label (fixed 112px column), status chip (toned), headline, detail in muted text, reason in muted italic with the code; `items` collapsed under a `Show N steps` / `Show N findings` disclosure. Band tone (background tint) is the setup row's tone; the hairline rule holds (no side stripes). Each row is a link: setup → Trajectory, task → Evidence (scrolled to the step's receipt), build → Build, tests → Tests, ci → Official CI, report → Report.

Tabs: **Overview · Trajectory · Tests · Build · Official CI · Evidence · Logs · Report**.

| tab | content | gating |
|---|---|---|
| Overview (default) | `Needs attention` list (empty state: `Nothing needs attention.`); Build and Tests tiles reading the card rows (same strings as the band's headline/detail); per-module table (`overview` variant); `Data notes` disclosure with `card.notes` | always |
| Trajectory | replaces Timeline + Flow. Phase bands (name, termination, gate word, `validator_state`, `reason`; `key_results` in a disclosure). Rows: `#turn` · actor glyph · tool · `call.summary` · outcome chip (`ok`/`failed`/`refused`/`pending`/`cancelled`) · `observation.summary` · gate badge · duration · tokens · seq. Expansion keeps `TurnQuad` (model context / tool call / tool result). `TurnSparkline` stays as the header. Live polling and `since_seq` merge unchanged | always for real sessions; demo via `demo_data` |
| Tests | (1) unique counts headline + raw note; (2) failing tests grouped by module with names and Copy; (3) per-module table; (4) test invocations from receipts: command, cwd, tool + version, JDK, exit, duration, report files; (5) `Evidence accounting` disclosure (§5.5) | always |
| Build | build invocations from receipts (same columns), then the module diagnostic table with the caption `Module counts are diagnostic. The project's CI defines the build scope.`, then warnings | always |
| Official CI | Target: repo, sha7, cell id, grade, CI command, JDK. Attainment: verdict; the three fractions each with what the denominator is (`523 of CI's 523 tests executed`, `1 of CI's 1 modules matched · basis: job log`); `scope score unavailable` with reason when `alpha` is null. Lifecycle parity: CI command vs SAG commands, reach, missing/extra. Red beyond CI: `unexpected_red_ids`. Reasons: code + gloss. When `status != evaluated`: one sentence (`Official CI was not compared: {gloss}`) and the reasons list | shown when `ciComparison` is non-null |
| Evidence | receipts table first (id, tool, command, cwd, exit, JDK, Maven, reports new/changed, tests reported), then today's evidence groups | when receipts or groups exist |
| Logs, Report | unchanged (the report banner copy stays) | as today |

Removed: `FlowTab`, `ContextTrace`, `ActionDetailModal`, `TimelineTab` (renamed `TrajectoryTab`), `FacetTabs`, `scrollSpy`, `SummaryStrip`, `BuildCard`, `TestCard`, `FilesDigest`, `VerdictBand` (replaced by `ResultBand`), the Files tab, and their tests.

### 5.5 Evidence accounting labels (D3)

The metrics-v2 layers render only inside the Tests tab's collapsed `Evidence accounting` block, one row each, counts as `{passed} / {executed} passed · {failed} failed · {errors} errors · {skipped} skipped`, `≥N kept` for lower bounds, or `unavailable: {reason}`:

| metrics-v2 field | label |
|---|---|
| `claimed.receiptExecutions` | Results bound to this run's receipts |
| `claimed.latestCases` | Tests identified by module and name |
| `claimed.latestSubjects` | Test classes identified by module and name |
| `quarantinedObservations` | Set aside: not from this run's receipts |
| `unattributedObservations` | Set aside: no module or test name recorded |
| `staleObservations` | Set aside: from an earlier run |
| `evidence.integrity` | Evidence records: complete / degraded / unavailable |

### 5.6 Copy and type rules

- Labels are sentence case. Uppercase tracked text is allowed only in table headers and status chips. Minimum text size 11px.
- Forbidden in user-visible strings: `sealed`, `canonical`, `claimed`, `quarantined`, `subject`, `snapshot`, `metrics-v2`, `verdict-bearing`, `promoting`. Replacements: "recorded", "bound to a receipt", "set aside", "test class", "result". A vitest test greps JSX/TSX string literals for the forbidden list.
- Status words are the seal's words, lowercased, never re-synonymed (`success`, `partial`, `failed`, `complete`, `incomplete`, `met`, `not_met` shown as `not met`).
- `DATA_NOTE_COPY` and the CI/task line parsing in `evidencePresentation.ts` are deleted; the frontend renders `card.notes` and `card.rows` verbatim.

## 6. Report

`_generate_snapshot_report` replaces the bold header lines (Build/Tests/Coverage/Official CI/Required task/Verdict) and `_render_summary_dashboard` with `## Result`, a Markdown table `| | Status | Detail |` of the seven rows (`headline`, then `detail` and `reason` joined with ` · `), followed by `Needs attention` bullets when non-empty. The `## 🧾 Metrics-v2 Evidence Layers` section moves to the end of the report as `## Evidence accounting` using the §5.5 labels. The `Evidence refs` line stays. `render_result_card_markdown(card)` lives in `result_card.py` so the report and the other two surfaces cannot drift. Everything else in the report is untouched.

## 7. Testing

- `tests/test_result_card.py`: every status branch of every row; `not supplied` / `not compared` / legacy; lower bounds; `<100%`; raw ≠ unique; alpha absent; report delivery states; v4 and v3 snapshots; gloss coverage over the enumerated code constants; attention ordering; notes dedupe.
- `tests/test_snapshot_surface_agreement.py` (extended): from one fixture snapshot, the CLI block, `resultCard` in the session detail, and the report table carry identical `status` and `headline` per row.
- `tests/test_turn_stream.py`: the three golden ledgers render to pinned text; same-line vs fresh-line outcome rule; refusal; controller turn; blocked gate; job progress throttling; warnings printed once; width 80 truncation.
- Trajectory: reducer summary rules per tool; outcome mapping; golden fixtures re-pinned; schema round-trip with and without the new fields; `test_trajectory_api.py` sees the fields.
- CLI: `test_cli_project_exit_codes.py`, `test_cli_report_verdict_mirror.py`, `test_agent_final_status.py`, `test_report_honesty.py` re-pinned to the block and the removed lines; `test_trajectory_cli.py` for `--format`; new `test_result_command.py`, `test_list_command.py`; `test_inspect_command.py` gains `--turn` and the missing welcome panel; `test_ui_*.py` deleted; logger tests for the WARNING default and single console implementation.
- Web: `test_web_session_detail_fields.py`, `test_web_api.py`, `test_web_demo_data.py`, `test_web_read_model.py` updated; `test_web_verdict.py` deleted.
- Frontend (vitest): `ResultBand`, rail strip and rows, `TrajectoryTab` (summaries, phase headers, key results disclosure, live merge), `OfficialCITab` (evaluated / not compared / alpha null), `TestFacet` (accounting labels, failing groups), `BuildFacet` (receipts), `App` tab gating, forbidden-term lint, `styles.test.ts` 11px floor. Tests of removed components are deleted.
- Live check before the frontend lands: `sag ui --demo` screenshots at 1180 / 768 / 400 px, light and dark; then `sag ui` against one recorded run through the web mirror to confirm the Trajectory tab on a real ledger. `npm run build` is run at the end and the bundle under `src/sag/web/static` is committed, as the recent repair rounds did.

## 8. Compatibility

- Sessions with no `control_events.jsonl` print no turn stream; the block still renders from the seal.
- v4 and v3 snapshots render (§2.5). An unreadable seal renders a seven-row card with `verdict unknown` and reason `verdict.json could not be read: {detail}`, exit `1`.
- The trajectory document stays `schema_version: 1`; readers that ignore unknown optional fields keep working, and our own strict models accept them.
- `sag trajectory` without `--format` changes from JSON to table. Scripts that pipe it to `jq` must add `--format json`; the `README` example is updated.
- `ExecutionSessionDetail` drops `verdict`, `ciComparisonLines`, `taskCompletionLines`, `files`; the bundled frontend is the only consumer.

## 9. Non-goals

Cross-run campaign comparison views; frontend routing; new per-measurement API routes; any change to judgments, rate bands, sealing, receipts, or the acceptance/CI comparison logic; backend removal of `context_trace.py` (follow-up); coverage collection.

## 10. Delivery order

1. **Card and glosses** (backend): `result_card.py`, tests, `_render_setup_cli_result`, agent panel removal, report `## Result` + accounting move, `resultCard` on the session detail, `web/verdict.py` deletion, surface-agreement test.
2. **Trajectory summaries and the console**: schema/reducer fields and golden re-pin; `console/turn_stream.py` + sink observer; logger defaults; `src/sag/ui/` and `--ui` deletion; `sag trajectory --format`; `sag result`; `sag list`; `sag run` exit code; `inspect --turn`; README.
3. **Workbench**: types, `ResultBand`, rail strip and rows, `TrajectoryTab` merge, `OfficialCITab`, Tests/Build/Evidence restructure, removals, copy pass and lint, demo data, vitest, screenshots, bundle rebuild.

Phases 1 and 2 are independent of each other after the reducer fields land; phase 3 depends on both.
