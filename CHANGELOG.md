# Changelog

All notable changes to Setup-Agent (SAG) are documented here.

## [Unreleased]

This iteration rebuilds every surface a run has — the terminal, the written
report, and the Workbench — on two shared derivations of one record, so the
three cannot describe the same run differently.

### The record and its derivations

- **One ledger, two derivations.** Everything a run states about itself now
  comes from `control_events.jsonl`, the append-only control ledger the engine
  writes on the host and mirrors into the container. The **trajectory**
  (`src/sag/trajectory/`) folds it into turns — call, result, grading, recorded
  span, token bill; the **result card** (`src/sag/result_card/`) states the run
  as seven rows in a fixed order. Console logs are no longer a source anywhere.
- **`sag trajectory`** prints a session as a table (one line per turn, as the
  run happened), as the trajectory document (`--format json`, `--detail full`),
  or tails a running session (`--follow`). **`sag result`** prints the card from
  a container or a recorded session directory (`--json` for programs).
  `sag inspect --turn N` shows one turn end to end.
- Each turn's `turn_record` carries the engine's own start and end for the turn
  and its token bill; phase calls carry the outcome the model stated beside the
  word the gate delivered.

### Terminal

- **Turn stream.** The console prints one readable line per turn, live, grouped
  under phase bands, in place of a log that was mostly docker-exec noise. A
  turn's outcome line is settled when the ledger records the turn, so the
  duration it prints is the one the trajectory, the report, and the Workbench
  print. A gate continuation appears only when the gate's word differs from what
  the model stated. Warnings written to the console no longer land inside an
  open turn line: the log sink lets the stream finish its line first.
- **Result block.** The run ends with the seven-row card. Exit code follows the
  Setup row.
- The console shows warnings only by default; `--verbose` opens the debug log.
  Validator probes whose negative answer is expected, and the orchestrator's
  head-and-tail output cut, are logged as facts at DEBUG rather than as failures.

### Report

- **Rendered on the host at run end**, from the record, after the run's last
  event — so it states the run that finished (the in-loop report was written one
  turn before the run ended and disagreed with the terminal by a turn and nine
  seconds). Written into the session directory and copied back into the
  container, best-effort. Sections: header, Result, What was set up, The run
  (one row per turn), Tokens, Evidence (counted, with the commands that open
  it), Evidence accounting; blockers directly under the Result table when
  there are any.
- The in-loop `report` tool now writes only the header and the Result table in
  setup mode; the report phase gate is unchanged.
- The Setup row's phase fraction counts the run's phases from the ledger rather
  than the judgment's records, which are written before the report phase
  exists; a phase that closed blocked is not counted as finished.

### Advisor accounting

- The advisor's own model calls are recorded on the turn that consulted it —
  in the ledger's `turn_record`, in `token_usage.csv`, on the trajectory's
  `advisor_tokens`, on the card's `advisor_tokens_in/out` — and shown apart
  from the executing model's spend on every surface. Where a total is shown it
  is labelled as the sum of the two. The advisor's model name resolves from the
  run pin's `same-model` mode.

### Workbench

- Result band (collapsible) above the tabs; Overview with time and token tiles
  and an attention list that appears only when something needs attention; a
  Turns tab with three per-turn strips (model tokens, advisor tokens, duration)
  whose hover panel says what the turn ran; an Official CI tab; evidence
  accounting; a header that names the model and the advisor. The disconnected
  `--ui` mode is retired.

### Development

- `scripts/ship_gate.py` runs vitest, the TypeScript build, the bundle, and
  pytest, and reports each leg. Archived real runs under
  `tests/fixtures/trajectory/` and `tests/fixtures/report_document/` replay
  end to end as fences for every surface.

## [0.3.0] - 2026-06-14

This release reworks how SAG drives a project setup. An engine-owned **phase
machine** replaces the model-managed task bookkeeping that caused most loop
waste; a single **verdict policy** makes the CLI banner, exit code, and report
always agree; long builds run **detached** instead of being killed by a
per-command timeout; the tool surface is consolidated from twelve tools to six
behind a uniform result envelope; and the Workbench gains a context trace,
a phase timeline, and workspace deletion.

### Agent loop and reliability

- **Phase machine for `sag project`.** Setup now runs as a fixed, engine-driven
  sequence: provision -> analyze -> build -> test -> report. The model works
  freely inside a phase and signals with one tool (`phase`: done / blocked /
  note); the engine validates the "done" claim against physical evidence,
  advances, and rebuilds a clean context window for the next phase. This retires
  the `manage_context` ceremony for setup runs (previously 18-26 calls per run).
- **`blocked` escape valve.** A phase that genuinely cannot finish is recorded
  honestly and the run degrades to partial/failed, instead of looping to the
  iteration cap.
- **Per-phase iteration floors.** No phase has a fixed quota; a phase is only
  cut short when continuing would starve the minimum needs of later phases, so a
  hard build can use the iterations an easy clone saved while the run still
  always reaches the report phase.
- **Dispatch-and-poll for long builds.** Long Maven/Gradle/bash commands run
  detached with output to an in-container log; if a command is still running
  when its soft window closes, the agent polls the log tail across iterations
  rather than the build being SIGKILLed. A global wall-clock cap
  (`SAG_MAX_WALL_CLOCK_SECONDS`, default 7200s) bounds the whole run. Builds that
  previously ran for hours against an unenforced timeout are now bounded.
- **Toolchain provisioning.** The detected JDK is installed for Gradle projects,
  and a Maven that satisfies a pom's enforced minimum is provisioned before the
  first build instead of after repeated failures.
- **Completion-integrity gates.** Build/test work cannot be marked complete
  without real artifacts or test reports; a documented-but-unremediated
  toolchain requirement blocks completion; the run cannot report success with a
  failed build task and zero artifacts.
- **Single verdict kernel.** One ordering (failed < partial < success) feeds the
  report header, CLI banner, exit code, and Workbench state, so they can no
  longer disagree. A build-green run with no test evidence is reported as
  partial, not a full success.
- **Collision-free task ids and idempotent re-planning.** Task ids come from a
  monotonic per-trunk sequence (never reused), and the analyzer no longer
  rewrites the plan when build evidence already identifies the project, ending
  the re-plan churn that could consume an entire run.

### Tools and context architecture

- **Six tools instead of twelve.** `bash`, `files`, `build` (Maven/Gradle behind
  one verb set: deps / compile / test / package), `project` (clone / provision /
  analyze / env), `search` (refs / files / job logs / web), and `report`. Legacy
  tool names alias to their successors so model drift degrades gracefully.
- **Uniform result envelope.** Every tool returns `verdict / facts / output /
  suggestions / refs`. Large output goes to a stored ref ("links, not dumps")
  retrievable via `search`, keeping the agent's context window lean.
- **Context journal.** Each iteration records what composed its prompt (segments,
  token counts, deltas, intro/ledger text) to an in-container journal, so a run
  can be replayed step by step.
- **Attempt-ledger compaction.** Long phases compact older history into a
  one-line-per-attempt ledger; failed approaches stay visible so the agent does
  not retry them blindly.
- **Evidence-driven reporting.** Tool results, task completion, and the final
  report are built from physical evidence (artifact and test-report parsing)
  rather than model-asserted prose.

### Web Workbench

- **Context trace.** A timeline view of a run: trunk goal -> phases ->
  iterations -> tool actions, with thoughts, observations, output refs, and the
  journal window per iteration. Long iteration lists paginate on demand.
- **Phases tab and API.** Phase histories and context journals are exposed at
  `/api/workspaces/{id}/phases` and `.../phases/{phase}/journal`.
- **Delete workspace.** Remove a workspace and its container from the dashboard,
  including stopped or already-gone containers, with a confirm dialog.
- **Dashboard redesign.** A documented design system (calm mission-control:
  status earns color, machine values in mono, flat-by-default), truthful
  build/test/report status, attention-ordered launches, and a first-run empty
  state.
- **Per-workspace session isolation** and a negative cache for unresolvable
  session ids, which removes a Docker-exec storm behind a stale "Workspace data
  unavailable" banner.

### CLI

- **`sag inspect`** renders phase timelines and per-iteration context windows
  from a live container or a recorded session, for debugging what the model saw.
- **Quieter logging** for non-agent CLI paths.

### Fixed

- Report header, dashboard, CLI banner, and exit code no longer report different
  verdicts for the same run.
- Dashboard build state showing "unknown" for completed setups.
- Gradle build validation no longer passes on the `.gradle` cache directory
  alone; it requires compiled outputs and excludes wrapper/tooling jars.
- A near-96% test pass rate is no longer overridden to a failure by a
  zero-tolerance gate; the documented threshold policy is the single source.

[0.3.0]: https://github.com/Codegass/Setup-Agent/compare/0.2.0...0.3.0
