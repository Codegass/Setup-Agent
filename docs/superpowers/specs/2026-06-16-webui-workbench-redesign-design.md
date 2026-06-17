# SAG Workbench UI Redesign Design

## Purpose

Redesign the SAG web UI to match the new prototype the maintainer placed in `docs/Setup Agent Web UI/`, giving SAG a calm, glanceable "mission-control" workbench. The redesign reproduces the prototype's visual system (`src/`) and its two-pane detail layout (`workbench/`), wired to the real backend payloads, while preserving the recently shipped per-submodule build/test metrics and code coverage (Features A and B).

This is a large, multi-surface redesign. It is captured as one overarching design spec and implemented through phased plans (Foundations → Dashboard → Detail-pane shell → Build/Test facets → remaining facets → polish).

## Reference & Direction

- **Visual system:** `docs/Setup Agent Web UI/src/ui.jsx` — and it is already nearly identical to the live `webui/src/components/common` primitives (Badge/StatusBadge, Card/CardHead, Button, Tabs, MetaLabel, Progress, TestBar). The redesign reuses and refines those, not a new component library.
- **Layout / IA:** `docs/Setup Agent Web UI/workbench/` "scroll" mode — a unified **detail pane** that merges today's Workspace and Session pages, with a **two-pane body**: a left vertical **section-nav** and a right **single continuous scroll** of facet sections (scroll-spy keeps the nav in sync).
- **Density & accent:** fixed brand accent (`#2563eb`) and comfortable density. The prototype's live "tweaks panel" (accent/density switcher) is a prototype tool and is **not** shipped.
- **Fidelity:** match the prototype closely (layout, structure, spacing, type, color), adapting only where the real data or existing components require.

## Scope

In scope:

- Dashboard reshape (workspaces list: attention-first rows, summary strip, teaching empty state, skeleton loading).
- The unified two-pane Detail Pane replacing the separate Workspace (Overview/Sessions/Terminal/Settings) and Session (Status/Evidence/Context/Files/Report/Logs) tabbed pages.
- Facet sections: Build, Test, Flow (context), Evidence, Files, Report, Logs — restyled and wired to real data; Build/Test carry the Features A/B per-module + coverage views.
- A foundations pass (font loading, AA contrast, semantic state tokens, reduced-motion) the prototype assumes.

Out of scope (explicitly):

- Backend payload redesign. This is presentation-layer work; web models and the read model are consumed as-is. Where the prototype shows data the backend does not populate (see Data Mapping), the UI derives it from existing fields or omits it gracefully — never fabricates.
- The tweaks panel.
- New backend features. Populating `blocker` and context-pressure (if absent) are noted follow-ups, not part of this redesign.
- Changing the verdict kernel, phase machine, or Features A/B data contracts.

## Information Architecture

> **Correction (2026-06-16):** the original spec read "two-pane" as *Dashboard page ↔ Detail page (with a left facet rail)*. The actual prototype (`workbench/app.jsx` + `workbench/detail.jsx`) is a **master-detail workbench**, and that is the target. Phase 3 built the separate-page reading; Phase 3b realigns to this.

**One workbench screen, two panes** (`workbench/app.jsx`):

1. **Left — Workspace Rail** (persistent, ~320px, `workbench/app.jsx Rail`): the workspace list *is* the dashboard. Header carries the logo + docker version, a **Launch setups** button, summary chips (Workspaces · Running · Attention), and a filter input; below is the scrollable list of compact workspace rows (status dot, project + release, stack/commit, build glyph + test mini-bar, attention/selected indicator). Selecting a row sets the active workspace.
2. **Right — Detail Pane** (`workbench/detail.jsx DetailPane`): the selected workspace's detail — header, session switcher, Summary Band, **top facet pill nav**, and the single continuous facet scroll.

There is **no separate full-width Dashboard page** and **no top app bar/breadcrumb** — the rail header owns the global chrome. At narrow widths the rail collapses to a horizontal strip above the detail (`workbench/app.jsx` `horizontal` Rail / `stacked` layout). The detail pane is fed by the existing `ExecutionSessionDetail` payload plus the workspace summary and session list.

## Detail Pane

A single component composed of:

1. **Sticky header** — project name + container + `StatusBadge`, the release/commit chip, and an actions cluster (New task, Terminal, Settings, Delete).
2. **Session switcher** — a horizontal row of session chips (id + status dot + title) when a workspace has more than one session; selecting one re-points the facets. Mirrors `workbench/detail.jsx DetailHeader`.
3. **Summary Band (always visible)** — the reachability win:
   - **Outcome callout**: the run's verdict in one sentence, color-earned (emerald/amber/red), from `detail.outcome` + `evidence_status`.
   - **Signal tiles**: Build · Tests · Evidence · Report, from `detail.build` / `detail.test` / `detail.evidence_status` / `detail.report`.
   - **"Why" callout**: rendered from `detail.blocker` (code, title, detail, suggested fix) when present. Note: the read model currently sets `blocker=None`, so this is dormant until a backend follow-up populates it; it must render nothing (not an empty box) when absent.
4. **Facet body** (`workbench/detail.jsx`, `detailStyle: "scroll"`):
   - **Top facet pill nav** (sticky, horizontal): Build · Test · Flow · Evidence · Files · Report · Logs with a count where relevant; the active facet is highlighted via **scroll-spy** as the body scrolls, and clicking a pill smooth-scrolls to that section.
   - **Single continuous scroll**: the Summary Band followed by each facet section in order. Each section has an `id` anchor for the scroll-spy.

## Facets (sections) and data mapping

Each facet maps to existing `ExecutionSessionDetail` data and, where applicable, reuses today's renderers (restyled, not rebuilt):

- **Build** — conclusion-first build summary (`detail.build`: state, system, classes, JARs, artifacts) **plus the Feature A per-module build table** (`detail.modules` / `detail.module_summary`): failures-first, build status, class/JAR counts, error samples. This is today's `BuildDetailPage` content, rendered as a section.
- **Test** — conclusion-first test summary (`detail.test`: runner executions vs unique methods, pass/fail/skip, pass-rate bar) **plus the Features A/B per-module table**: pass/fail/skip, rate, the **coverage column** (stacked line/branch bars), failing-method inline expand. This is today's `TestDetailPage` content as a section, including the coverage tile in the summary tiles.
- **Flow** — the Context "command center": trunk goal + progress, the branch/task list with status tokens, and context-pressure when `ContextTrace` exposes it (else omitted). Reuses `ContextTrace.tsx` data, restyled to the prototype's command-center look.
- **Evidence** — `detail.evidence` groups, restyled (status token + grouped refs, mono chips with copy).
- **Files** — `detail.files` digest, restyled.
- **Report** — `detail.report_doc` rendered with capped prose width (65–75ch) and table hairlines.
- **Logs** — `detail.logs` as a mono block with line numbers.

Facets with no data render a small "not available / agent still working" empty state, never a blank section.

## Dashboard

Reshape `webui/src/pages/Dashboard.tsx` (maintainer-protection lifted for this redesign):

- **Summary strip**: Workspaces · Running · Need-attention (the last is a filter affordance). Keep it as a compact strip per `src/Dashboard.jsx`.
- **Workspaces table**: comfortable list rows (project + release chip + stack/commit/updated meta, container status, current task + active-session indicator, build cell, test bar, report badge, changed-files count, hover actions). **Attention-first ordering**: failed build/test or exited containers sort to top with a status tint; healthy rows stay quiet.
- **Empty state**: zero workspaces teaches what a workspace is + the primary "Launch setups" action + the paste-list hint.
- **Loading**: skeleton rows on first load; an "updated Ns ago" stamp; a quiet inline indicator when polling fails.

## Visual System & Foundations

Reuse the existing primitives; align the foundations the prototype assumes (the 2026-06-07 plan's Phase 2):

- Load **Inter** properly (self-hosted `@fontsource-variable/inter`, `font-display: swap`); keep the mono stack.
- **Contrast**: replace meaning-bearing `text-slate-400` with `slate-500/600` to hit AA; keep `slate-400` for decoration only.
- **Semantic state tokens** (`--status-running/queued/success/failed/attention` + soft backgrounds) consumed by `Badge`/`status.ts`, the section-nav, and callouts. One vocabulary.
- **Motion safety**: a global `prefers-reduced-motion` block (pulses → static dots, pops → instant fade, scroll-spy jump → instant when reduced).
- **Semantic z-index** scale (app-bar, sticky section-nav, dropdown, modal, toast).
- Fixed accent `#2563eb` wired to the existing CSS custom properties.

## Routing & Migration

- `App.tsx` route union: `{view:"dashboard"} | {view:"detail", workspaceId, sessionId?, facet?}`.
- The detail pane subsumes `Workspace.tsx` and `SessionDetail.tsx`; `BuildDetailPage`/`TestDetailPage` content moves into the Build/Test facet sections (the standalone pages can be retired or kept as thin wrappers — decided in the plan).
- Terminal and Settings become header actions opening their existing panels within the detail pane (tab or slide-over — decided in the Detail-pane plan).

## Constraints

- Dashboard.tsx and all `webui/src/` surfaces are in scope (protection lifted by the maintainer for this redesign).
- `src/sag/web/static/` is rebuilt but stays uncommitted per project policy (the live site is served from the `ui` branch).
- Presentation-layer only; no backend/data-contract changes.
- All existing frontend tests stay green or are updated with the redesign; `tsc` clean each phase.

## Decomposition & Sequencing (phased plans)

1. **Foundations** — fonts, contrast, semantic tokens, reduced-motion, z-index. App-wide, low-risk, everything builds on it.
2. **Dashboard** — list reshape, attention-first, summary strip, empty/skeleton/poll-failure states.
3. **Detail-pane shell** — the two-pane section-nav + scroll-spy, sticky header, session switcher, Summary Band; route merge (`workspace`+`session` → `detail`).
4. **Build & Test facets** — fold in Features A/B per-module + coverage as sections; retire/wrap the standalone detail pages.
5. **Flow / Evidence / Files / Report / Logs facets** — restyle to the prototype.
6. **Polish & harden** — full state vocabulary, responsive (narrow-width nav collapse), a11y/keyboard, before/after screenshots, rebuilt bundle.

Phases 2–5 are independent after Phase 1. Each ends with: tests green, `tsc` clean, bundle rebuilt (uncommitted), screenshots captured.

## Testing

- Component/RTL tests per restyled surface: dashboard reshape (attention-first ordering, empty state, skeletons), detail-pane scroll-spy + section-nav, session switcher, summary band (outcome/why/tiles), each facet renders its data + empty state, Build/Test facets keep Features A/B behavior (per-module tables, coverage column, failing-list expand, no fake zeroes).
- `tsc -p tsconfig.app.json --noEmit` clean.
- Manual: `sag ui` against real workspaces (a passing single-module, a multi-module Maven, a multi-module Gradle with coverage, a failed run) — verify glanceability ("what needs me?" in <1s on the dashboard), the two-pane scroll, and that Features A/B render in the facets. `sag ui --demo` renders all states. Visual before/after per surface.
