# Workbench Improvements Design

## Purpose

Four maintainer-reported improvements to the SAG web workbench, after the redesign (Phases 1–6) shipped:

1. **Deleting freezes the UI** — the `DELETE` removes a Docker container + volume (slow) and the confirm dialog `await`s it before closing.
2. **No batch delete** — the rail can only delete one workspace at a time.
3. **Build stats are thin** — no build success rate, module count, or build running time.
4. **Build layout diverges from the prototype** — the redesign shipped a single conclusion card + a stats row; the prototype (`docs/Setup Agent Web UI/workbench/sections.jsx` `BuildBody`) is a two-card grid (conclusion + Outputs).

This is two independent subsystems — **Delete** (frontend) and **Build facet** (frontend layout + backend data) — captured as three independently shippable plans.

## Subsystem A — Delete

### A1. Non-freezing delete (inline "deleting…" state)

- `App.tsx` holds `deletingIds: Set<string>`.
- A delete (rail-row trash **or** detail-header delete) opens the existing `DeleteWorkspaceDialog`. On confirm the dialog **closes immediately** (does not await); the id is added to `deletingIds` and `deleteWorkspaceRequest(id)` runs in the background.
- While an id is in `deletingIds`, its rail row renders a **dimmed, non-interactive "deleting…" state** (status replaced by a small spinner/label; select disabled).
- On success: reload the dashboard (the workspace is gone) and drop the id. On failure: drop the id, leave the workspace, and surface the server message via the existing toast/notice banner (reuses the `launchNotice` pattern).
- Detail-header delete additionally clears the selection on success so the pane doesn't dead-end.
- The dialog no longer needs its inline error path for the slow case (errors move to the toast); it keeps the confirm/cancel + the `kind: "launch"` failed-launch removal.

### A2. Batch delete (checkboxes + action bar)

- The rail header gains a **"Select"** toggle. In select mode:
  - Each workspace row shows a **checkbox** (the row no longer selects-to-open; the checkbox toggles membership in a `selectedForDelete: Set<string>`).
  - A sticky bottom **action bar** shows "Delete N selected" + "Cancel".
  - Pending/launch rows are excluded from batch selection (failed-launch removal stays its own per-row action).
- "Delete N selected" opens a batch confirm dialog; confirming loops `startDelete(id)` for each selected id (all enter the `deletingIds` state via A1), then exits select mode.
- No new backend endpoint — the existing single `DELETE` is looped client-side; the optimistic/inline state keeps it responsive.

## Subsystem B — Build facet

### B1. Two-card layout (frontend)

Port `workbench/sections.jsx BuildBody` to `BuildDetailPage.tsx`:

- `grid gap-4 sm:grid-cols-2`:
  - **Left — conclusion card**: status icon + `statusMeta(state).label` + system/tool `Badge`; a divided KV list of **Tool / Time / Command / Artifact** (each row hidden when its value is absent or `"unknown"`).
  - **Right — Outputs card**: `Outputs` label + two stats (**classes**, **JARs**) + a warnings list (`build.warnings`) when present.
- Below (`sm:col-span-2`): for **multi-module**, the module stats + the existing `ModuleTable` (variant `build`); for **single-module**, the calm note (no "Overview").

### B2. Success rate + module count (frontend)

- For multi-module, show **Built X / Y (Z%)** derived from `moduleSummary` (`modulesBuilt`/`modulesTotal`), alongside the existing Built/Failed/Skipped stats. Single-module shows no rate (not applicable).

### B3. Build time + command + artifact (backend capture)

Today `_build_payload_from_metrics` / `_build_payload_from_report` hardcode `time: "—"`, and the metrics path omits command/artifact. `CommandTracker.track_build_command` records a `timestamp` but no duration.

- **Time:** measure the build command's wall time where it executes and record `duration` on the tracked build-command entry; format and surface it as `build.time`.
- **Command:** surface the tracked build command string as `build.note`.
- **Artifact:** surface the primary built artifact (the existing `artifact_samples[0]` or the validator's expected-artifact) as `build.artifact`.
- These flow into `module_metrics` build (and/or the read model's build payload) so `_build_payload_from_metrics` returns real `time`/`note`/`artifact` instead of `"—"`. Frontend already consumes these (`BuildSummary` has the fields) — B1's KV rows light up automatically.

## Data Mapping (no contract changes)

`BuildSummary` already declares `tool, time, note, system, classCount, jarCount, artifact, artifactSamples, warnings, moduleOutputCount`. `ModuleRollup` declares `modulesTotal/modulesBuilt/modulesFailed/modulesSkipped`. B1/B2 consume existing fields; B3 fills `time`/`note`/`artifact` that are currently empty.

## Scope & Constraints

- A1, A2, B1, B2 are presentation-layer (frontend). B3 is a backend data-capture change (no API contract change — same `BuildSummary` shape, just populated).
- Reuse existing primitives (`Card`, `Badge`/`StatusBadge`, `status.ts`, `ModuleTable`, `DeleteWorkspaceDialog`) and Phase 1 `--status-*` tokens.
- `src/sag/web/static/` stays uncommitted (rebuilt by the maintainer). `docs/` force-added. No `Co-Authored-By` trailer.
- All frontend tests stay green + `tsc` clean; backend changes carry pytest coverage.

## Decomposition (three plans)

1. **Delete improvements** (frontend) — A1 inline deleting + A2 batch delete.
2. **Build facet layout + stats** (frontend) — B1 two-card layout + B2 success rate; consumes whatever build data exists (Time shows when present).
3. **Build-time capture** (backend) — B3 duration/command/artifact into `module_metrics` → read model; after it lands, the B1 KV rows populate with real values.

Plans 1 and 2 are independent. Plan 3 is independent of the layout but completes the "build running time" ask; sequence it after Plan 2 so the layout is in place to show the new values.

## Testing

- **Frontend:** RTL — deleting state renders + is non-interactive; delete is optimistic/non-blocking; batch select + action bar deletes N; build two-card layout (conclusion KV + Outputs + warnings); success rate for multi-module; single-module note has no "Overview"; no fake zeroes. `tsc` clean.
- **Backend:** pytest — `track_build_command` records `duration`; `_build_payload_from_metrics` surfaces real `time`/`note`/`artifact` from metrics; absent → graceful (`"—"`).
- **Live:** drive Chrome — delete a workspace (UI stays live, row shows "deleting…"), batch-delete two, and view the Build facet two-card layout on a single- and multi-module project with real time/command/artifact.
