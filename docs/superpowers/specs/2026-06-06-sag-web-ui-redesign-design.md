# SAG Web UI Redesign Design

Date: 2026-06-06
Status: Draft for spec review

## Goal

Design a web-based SAG workbench that makes container state, agent execution
state, setup evidence, reports, context, build/test results, and follow-up work
easy to inspect without forcing users to read raw logs by default.

The UI should help users answer three questions quickly:

- What SAG workspaces/containers exist, and which one needs attention?
- What did the latest or active agent execution accomplish, with evidence?
- What should I do next inside this workspace after setup succeeds or fails?

This is a design spec only. It does not implement the web UI.

## Non-Goals

- Do not build a chat-first interface.
- Do not show every raw log line by default.
- Do not treat human terminal commands as session evidence by themselves.
- Do not make the web UI rewrite project-owned configuration files.
- Do not expose all host Docker containers; show only SAG-managed workspaces.
- Do not replace the CLI, existing Rich UI, or current session artifacts in this
  phase.
- Do not add login, multi-user permissions, or remote deployment semantics.

## Product Model

The web UI is a persistent local workbench launched with `sag ui`.

The core objects are:

- `Workspace`: a SAG-managed Docker container and workspace volume for one
  project.
- `ExecutionSession`: one agent run inside a workspace, created by setup,
  follow-up tasks, CLI runs, or UI-submitted tasks.
- `TerminalConnection`: an independent interactive shell into a workspace
  container.
- `FileChangeDigest`: a per-session summary of user or agent file changes
  detected from workspace snapshots.

The important semantic boundary is:

- Workspaces are where users choose projects and submit new tasks.
- Sessions are factual records of one agent execution.
- Terminal is a manual workspace interface, not a chat thread and not a session.
- File changes are the bridge between manual user work and the next agent
  execution.

## Information Architecture

### Dashboard

The default page lists SAG-managed workspaces/containers, not all Docker
containers.

Recommended columns:

- Project
- Container status
- Current or latest task
- Build status
- Test status
- Report status
- Changed files count
- Actions

Dashboard actions should be conservative:

- Open workspace
- Start or reconnect container when supported
- Open latest report preview
- Remove workspace only through an explicit confirmation flow

The dashboard should favor scanability over detail. It should show enough state
to pick the next workspace, then push detailed evidence into workspace/session
views.

### Workspace

Workspace-level tabs:

- `Overview`
- `Sessions`
- `Terminal`
- `Settings`

Workspace `Overview` is the main result page. It should show the active or
latest session first, because users come here to inspect outcomes:

- Current status and task
- Outcome summary
- Build and test summary
- Latest report preview
- Evidence timeline preview
- File change digest preview
- Context map preview
- New Task entry point

`New Task` is a workspace-level action. Submitting it creates a new
`ExecutionSession`. This keeps setup follow-up work semantically separate from
the previous execution record.

If a user starts from a failed session, the UI may offer a "create follow-up
task from this failure" flow. That flow should prefill useful references from
the failed session, then create a new workspace-level execution. It should not
turn the failed session into a chat continuation.

### Sessions

The `Sessions` tab lists current and historical executions for the workspace.

Each row should include:

- Task title
- Status
- Start and finish time
- Build/test result
- Report availability
- File change count
- Evidence count
- Entry point, such as CLI, UI, or external discovery

Opening a session shows a session detail page.

### Session Detail

Session detail is scoped to one execution.

Recommended tabs:

- `Status`
- `Evidence`
- `Context`
- `Files`
- `Report`
- `Logs`

`Status` is the default. It should be result-first:

- Outcome summary
- Build and test cards
- Report preview
- File change digest
- Evidence timeline
- Failure or blocker summary when present

There should be no generic "Recommended action" panel. Follow-up work belongs
to workspace `New Task`, optionally prefilled from the current session.

### Terminal

The terminal is a workspace-level shell into the selected SAG container.

It should use a full terminal frontend, preferably `xterm.js`, with a backend
adapter that bridges to Docker exec TTY. The first implementation should keep a
Python backend adapter as the default and allow a Node sidecar only if the
Python bridge cannot provide reliable terminal behavior.

Terminal commands are not associated with the active session. They do not
become agent evidence just because they were typed. This matters because users
may run large, noisy, random shell workflows, change directories often, or work
outside the official terminal through an IDE or `docker exec`.

The meaningful artifact of manual work is the workspace file change state. That
state is captured separately by the file tracker at the start of each new agent
execution.

### Settings

Settings should stay narrow in the first version:

- Container identity and Docker metadata
- Workspace path and volume information
- Model/provider configuration summary without secrets
- File tracker watch scope
- Debug links to raw SAG artifact locations

Do not make settings a project configuration editor.

## Context Map

Context UI should be a beautified abstraction of SAG's trunk/branch context
theory, not a raw context file browser.

The `Context` tab should show:

- Trunk Command Center: project goal, overall setup state, task list progress,
  and latest high-level summary.
- Task Flow: TODO items with statuses such as pending, active branch,
  completed, failed, or skipped.
- Active Branch Focus: current task, why it exists, recent branch memory,
  last tool/evidence references, and context pressure when available.
- Completed Branch Summaries: concise summaries inline in Task Flow, expandable
  on demand for key results and raw context references.
- Debug Drawer: raw trunk/branch JSON and context file paths for advanced
  inspection only.

This makes the trunk/branch design visible as a navigable mental model while
keeping raw context files out of the primary experience.

## Evidence Timeline

Evidence should come from trusted runtime sources such as tool results,
validators, reports, file trackers, and session registries. It should not be a
raw thought stream or every terminal log line.

The default view should group and collapse evidence by source, for example:

- Project analyzer
- Build tool
- Test validator
- Env overlay
- Physical validator
- Report
- File tracker
- Session registry

Each group should show:

- Source name
- Status
- Success/failure counts
- Latest update time
- Short summary

Expanding a group should reveal evidence records with links to raw output,
artifact paths, report sections, or context references. This keeps the default
view calm while preserving traceability.

## File Change Tracking

File change tracking should not rely on the official web terminal.

At the start of each new agent execution, the backend should compare the
workspace against the last known snapshot for that workspace. It should produce
a `FileChangeDigest` and attach it to the new session as startup context and
evidence.

Default watch scope:

- The cloned project directory
- SAG-managed runtime files such as `.setup_agent/env_overlay.json`
- Additional workspace paths configured per container

Default snapshot mode:

- Lightweight metadata snapshot with path, type, size, and modification time
- No full content hashing by default
- On-demand content diff when a user expands a changed file

The digest should classify files as added, modified, deleted, or renamed when
the backend can infer it cheaply. It should not try to interpret every change as
important. The agent can decide which changes matter in the new session.

## Backend Read Model

The web UI should consume typed read models instead of parsing rendered CLI
text.

Recommended read models:

- `WorkspaceSummary`: project name, container id/name, Docker status, latest
  session id, active session id, build/test/report summaries, changed file
  count, and timestamps.
- `ExecutionSessionSummary`: task, status, lifecycle timestamps, entry point,
  evidence count, file digest status, report status, and failure summary.
- `ExecutionSessionDetail`: status snapshot, task plan, context map, evidence
  groups, file digest, build/test result, report metadata, and raw artifact
  references.
- `EvidenceGroup` and `EvidenceRecord`: grouped trusted evidence with links to
  raw references.
- `ContextMap`: trunk summary, task flow, active branch focus, completed branch
  summaries, and raw debug references.
- `FileChangeDigest`: snapshot ids, changed file list, counts, and on-demand
  diff references.
- `TerminalConnectionState`: container, working directory when known,
  connection status, and TTY size.

These models should be backend-owned. The frontend should render them, not
reconstruct SAG semantics from log strings.

## Backend Components

Keep the web backend as a set of deep modules with small interfaces:

- `WorkspaceRegistry`: discovers SAG-managed containers and normalizes Docker
  metadata.
- `SessionRegistry`: discovers active and historical executions from UI-started
  runs, CLI logs, `.setup_agent` artifacts, and container metadata.
- `SessionReadModelBuilder`: builds session summaries and details from runtime
  events, reports, contexts, file digests, and logs.
- `EvidenceIndex`: normalizes evidence records and groups them by trusted
  source.
- `ContextMapBuilder`: turns trunk/branch context files into the abstract
  Context Map model.
- `FileChangeTracker`: owns workspace snapshots and produces file digests.
- `TerminalAdapter`: provides an interactive Docker exec TTY bridge.
- `WebServer`: serves REST APIs, SSE streams, terminal WebSocket, and static
  frontend assets.

The agent runtime should not need to know React or shadcn exists. The frontend
should not need to know Docker internals.

## Protocols

Use three protocol types:

- REST for operations and initial data fetches.
- SSE for dashboard, workspace, and session state streams.
- WebSocket only for interactive terminal I/O.

REST examples:

- `GET /api/workspaces`
- `GET /api/workspaces/{workspace_id}`
- `GET /api/workspaces/{workspace_id}/sessions`
- `GET /api/sessions/{session_id}`
- `POST /api/workspaces/{workspace_id}/tasks`
- `GET /api/sessions/{session_id}/files/{file_id}/diff`

SSE examples:

- `GET /api/stream/dashboard`
- `GET /api/workspaces/{workspace_id}/stream`
- `GET /api/sessions/{session_id}/stream`

WebSocket example:

- `/api/workspaces/{workspace_id}/terminal`

## Session Discovery

The UI should support both UI-started and externally-started executions.

UI-started executions can stream directly through the web server. CLI/external
executions should be discovered from available runtime sources:

- Session logs under `logs/session_*`
- Container and workspace metadata
- `.setup_agent/contexts`
- `.setup_agent` output and report artifacts
- Docker container status

External discovery may be partial. The read model should mark partial state
explicitly instead of inventing data.

## Frontend Direction

Use React/Vite with shadcn-ui and Radix primitives.

The visual language should borrow from `website/`:

- White base
- Thin borders
- Technical editorial grid
- Space Mono style metadata
- Inter-like body text
- Restrained blue accent
- Dense but calm control-console composition

The app should not look like a marketing landing page. It should be a practical
operations workbench with strong information hierarchy:

- Dashboard for scanability
- Workspace overview for outcomes
- Session detail for traceability
- Terminal for manual operation
- Settings for narrow runtime metadata

Avoid decorative gradients, nested card walls, oversized heroes, or large
illustrative sections.

## Error Handling

The UI should make uncertainty explicit:

- Docker unavailable: show a dashboard-level unavailable state with the exact
  Docker connection failure and recovery hint.
- Container stopped: show last known session state and available actions.
- Stream disconnected: keep the last snapshot and show reconnect status.
- External session partially discovered: label missing evidence as partial.
- Terminal connection failed: isolate the terminal failure from the session
  read model.
- File snapshot missing: start a new baseline and mark the digest as
  unavailable for that execution.
- Huge logs or diffs: show summaries and provide on-demand raw artifact access.

UI failures should not affect the underlying agent run.

## Testing

Recommended test coverage:

- Backend unit tests for workspace discovery, session discovery, evidence
  grouping, context map building, and file change digest generation.
- API contract tests for REST responses and SSE event shapes.
- Terminal adapter smoke tests for connect, resize, command input, disconnect,
  and stopped-container failure.
- Frontend component tests for Dashboard, Workspace Overview, Session Detail,
  Evidence Timeline, Context Map, and File Change Digest rendering with partial
  and failed states.
- Browser smoke tests for desktop and mobile layout, especially text overflow,
  tab navigation, and empty states.

Testing should prioritize read models and state transitions over pixel-perfect
screenshots.

## Acceptance Criteria

- `sag ui` can show all SAG-managed workspaces and their current/latest state.
- A user can open a workspace and understand the latest execution outcome
  without reading raw logs.
- A user can create a new workspace-level task after setup, producing a new
  execution/session.
- A user can inspect evidence, context, file changes, reports, and logs for a
  specific session.
- The terminal works as an independent workspace shell and does not pollute
  session evidence.
- File changes made by terminal, IDE, or external shell can be summarized at
  the next execution start.
- Context visualization reflects trunk/branch semantics without requiring users
  to browse raw JSON.
- The backend exposes stable read models rather than requiring the frontend to
  parse CLI output.

## Implementation Boundary

This spec defines the product and architecture direction for the web UI. The
next step is an implementation plan that breaks the work into small phases:

- backend read models and discovery
- file change tracker
- web server protocols
- terminal bridge
- frontend shell and dashboard
- workspace/session pages
- evidence, context, file, and report views

No implementation should start until this written spec is reviewed and
approved.
