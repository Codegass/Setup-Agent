# CLI Observability And Diagnosis Design

Date: 2026-06-02
Status: Draft for spec review

## Goal

Improve SAG's CLI/TUI maintainability and usefulness by making live agent
execution understandable first, then generating actionable post-run diagnostics
from the same event state.

This phase is CLI-first. It does not build a web UI, but it should avoid
terminal-specific state contracts so a future web surface can consume the same
run snapshots or event log later.

## Non-Goals

- Do not build a web dashboard in this phase.
- Do not add interactive controls such as pause, retry buttons, or command
  editing.
- Do not redesign the ReAct loop or tool orchestration contracts.
- Do not parse Rich-rendered terminal text to infer run results.
- Do not duplicate runtime state into a separate reporting-only data model.
- Do not make `UIManager` a larger renderer or state container.

## Constraints

- Keep commit messages free of Co-Authorship or similar authorship trailers.
- Preserve existing CLI behavior and `--ui` activation semantics.
- Keep non-UI agent and tool code unaware of Rich terminal rendering.
- Prefer small typed runtime contracts over loose shared dictionaries.
- Treat UI rendering as an observation layer; UI failures should not break the
  underlying agent run.

## Current Context

The current UI layer already has useful pieces:

- `src/sag/ui/events.py` defines `UIEvent`, `EventType`, `PhaseType`, and
  `UIEventEmitter`.
- `src/sag/ui/ui_manager.py` owns Rich `Live`, mutable phase state, current
  agent action fields, error/warning lists, report data, and rendering
  coordination.
- `src/sag/ui/components.py` creates reusable Rich panels and phase trees.
- `src/sag/main.py` exposes CLI commands and `--ui` options.

The main design problem is that event handling, state aggregation, and Rich
rendering are not separated enough. As the CLI becomes more informative, adding
more mutable fields and render logic directly to `UIManager` would make the UI
harder to test and harder to adapt for future surfaces.

## Proposed Architecture

Introduce a small state layer between emitted UI events and Rich rendering:

```text
Agent / ToolOrchestrator
  -> UIEventEmitter
  -> UIStateAggregator
  -> UIRunState snapshot
  -> Rich renderer during the run
  -> final diagnosis at the end
```

`UIManager` remains the lifecycle coordinator. It receives events, forwards
them to the aggregator, asks the renderer/components to display the latest
snapshot, and prints the final diagnosis once the run ends.

The aggregator owns event-to-state logic. Rich components own terminal layout.
The final diagnosis owns user-facing run summary text. These units should be
separately understandable and testable.

## Runtime State Contract

Add a typed snapshot model, tentatively named `UIRunState`, with fields for:

- project name, start time, elapsed time, completion status
- current phase and per-phase status
- active operation, including tool name, command/action, workdir, elapsed time,
  and visible parameters
- recent timeline entries for thought, action, observation, stream, recovery,
  warning, and evidence events
- latest user-visible warning and error
- recovery state, including retry count, strategy, fallback, and unresolved risk
- evidence records, including command summaries, report paths, validation
  results, and review checkpoints
- final outcome fields used by the post-run diagnosis

`UIRunState` should be a read model. Consumers should not mutate it directly.
The aggregator can either return a defensive copy or expose immutable dataclass
instances to prevent renderer-side state drift.

## Components

Recommended component boundaries:

- `UIStateAggregator`: consumes `UIEvent` objects and maintains `UIRunState`.
- `UIRunState`: typed snapshot/read model consumed by renderers and diagnosis.
- `UITimelineEntry` and `UIEvidenceRecord`: small typed records for history and
  evidence.
- Rich render helpers in `components.py` or focused submodules:
  `StatusHeader`, `PhaseTimeline`, `ActiveOperationPanel`, `RecoveryPanel`,
  `EvidencePanel`, and `FinalDiagnosisPanel`.
- `UIManager`: starts/stops Rich `Live`, handles events, coordinates rendering,
  and guards duplicate final summaries.

This should avoid a large-bang rewrite. Existing panels can migrate
incrementally as they start consuming `UIRunState`.

## Data Flow

Event handling should be single direction:

1. Agent, orchestrator, validation, Docker, and report code emit `UIEvent`.
2. `UIManager.handle_event()` gives the event to `UIStateAggregator`.
3. The aggregator updates the `UIRunState` snapshot.
4. Rich rendering reads the current snapshot and redraws.
5. On completion, final diagnosis reads the final snapshot and evidence records.

The final diagnosis must not scrape terminal output or reinterpret Rich display
text. It should summarize the state already collected during the run:

- outcome
- completed, failed, and skipped phases
- active failures and unresolved warnings
- recovery attempts and fallback decisions
- commands or tools that matter as evidence
- generated reports or artifacts
- concrete next actions

## Error Handling

Unknown, malformed, or incomplete events should not crash the UI. The
aggregator should preserve a warning timeline entry and continue with the last
known good state.

Failures should be classified into user-visible categories:

- tool failure
- command timeout
- parse or parameter normalization failure
- recovery attempt
- verification failure
- warning without immediate failure
- final run failure

During execution, the TUI should show only the most important current error or
recovery state. The final diagnosis can expand the complete failure and recovery
chain after the run is done.

## Testing

Testing should focus on state and behavior rather than terminal pixels:

- Aggregator unit tests: event sequence -> expected `UIRunState`.
- Diagnosis tests: final snapshot and evidence -> expected outcome, failures,
  recovery summary, evidence, and next actions.
- UI smoke tests: `UIManager` can receive normal, empty, unknown, warning, and
  failure events without crashing.

Full screenshot-style terminal tests are out of scope for this phase unless a
specific rendering regression appears.

## Acceptance Criteria

- Running with `--ui` makes live execution more understandable than the current
  state panel plus phase tree.
- Runtime state aggregation is separate from Rich rendering.
- `UIManager` is smaller or at least does not gain another broad responsibility.
- Final diagnosis is generated from the same event/snapshot/evidence model used
  by the live UI.
- Existing non-UI CLI behavior remains unchanged.
- Tests cover aggregator behavior, diagnosis behavior, and basic UI robustness.

## Implementation Boundary

This spec defines the design only. The next step is to write an implementation
plan that breaks the work into small changes, ideally starting with the state
model and aggregator before changing terminal rendering.
