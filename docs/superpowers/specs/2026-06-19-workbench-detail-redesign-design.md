# Workbench Result-Detail redesign — design spec

**Date:** 2026-06-19
**Status:** Approved (brainstorming) → ready for implementation plan

## Context & goal

The SAG Workbench detail view (the right-hand pane showing one setup-agent run)
currently stacks facet sections under a scroll-spy pill nav, with a `SummaryBand`
of four stat cards (Build / Tests / Evidence / Report). Status is effectively
repeated 3–4× (header, band, each facet), the nav is "tabs + long scroll", and
the badges are ambiguous.

A redesign was produced in claude.ai/design (using the synced SAG component
library) and downloaded to `docs/Setup-Agent UI/templates/workbench-detail/`
(`WorkbenchDetail.dc.html` + `_review/*.png`). Its stated intent: **"agent
flow/context, one verdict, one tab nav, clean stats."** This spec adopts that
redesign.

## Decisions (locked in brainstorming)

- **Scope:** full adoption of the redesign, in one pass.
- **Data source:** frontend **+ backend parity** — extend the Python read model so
  the header/verdict match the design exactly (not frontend-only).
- **Verdict:** composed **server-side** (one source of truth, Python-testable).
- **Per-action badge:** **honest** ok/running/failed derived from existing
  per-action fields — NOT a claimed "verified" status (no per-action physical
  validation is recorded today).
- **Ships as source only:** `webui/src` + backend. Per standing rule, do NOT run
  `npm run build` or modify `src/sag/web/static/`; the user rebuilds + deploys the
  bundle. Backend changes are live on `sag ui` restart.

## §1 — Information architecture (new detail shell)

Fixed chrome + one scrolling panel:

1. **Header** (fixed): bold project + `setup`/entry tag; actions right — New task
   (primary) · Terminal · Settings · ⋯ menu (Delete + session switcher when >1
   session). Second row = mono metadata line.
2. **VerdictBand** (fixed, below header): one synthesized sentence, toned by
   outcome. Replaces the 4-card `SummaryBand`.
3. **Tab bar** (fixed): **Overview · Flow · Tests · Build · Files · Evidence ·
   Logs · Report** — a *real* switcher (only the active panel renders), inline
   counts (Tests `7` red, Evidence `2`). Tabs appear only when they have data
   (same gating as today's `buildDetailFacets`). **Overview is the default tab.**
4. **Panel** (scrolls): the active tab's content.

Tab → content:
- **Overview** *(new)*: goal→Flow button · KPI tiles · per-module table · Needs-attention.
- **Flow** *(new, supersedes the Context facet)*: agent-goal trunk · phase timeline · per-action detail modal.
- **Tests / Build / Files / Evidence / Logs / Report**: existing facet components, rendered as panels.

The shift: scroll-spy through stacked facets → click-to-switch tabs; repeated
status → **one** verdict.

## §2 — Header, VerdictBand, backend additions

**Header** (`DetailHeader` restyled): row 1 = bold project + `setup` tag + actions
(New task / Terminal / Settings / ⋯ = Delete + session switcher when >1 session).
Row 2 (mono) = `container · stack · commit · model · steps/budget · duration ·
finished Nago`, with null pieces omitted.

**VerdictBand** (`pages/detail/VerdictBand.tsx`, replaces `SummaryBand`): one
sentence toned via existing status tokens —
- *success* (green): "Build passed on all 4 modules · 1,205 tests passing."
- *partial* (amber): "Build passed on 3 of 4 modules. 7 of 1,205 tests failing across acme-cli and acme-web — review before promoting."
- *failed* (red): "Build failed in acme-cli — 1 module did not compile." + `blocker.hint` when present (today's dormant "Why").

**Backend read-model additions** (`ExecutionSessionDetail`), all nullable (graceful
degradation on older runs):
- `verdict: VerdictSummary | None` — new model `{ tone: "success"|"attention"|"failed", headline: str, detail: str | None }`, composed server-side from `build` / `test` / `moduleSummary` by a new testable helper.
- `model: str | None` — agent model used (from run agent config/metadata).
- `steps: int | None` + `stepBudget: int | None` — iterations used + max-iterations budget (from execution summary).

Degradation: any missing field → header drops that chip; band falls back to the
existing `outcome` label. Never a crash or a blank.

**Backend sourcing (confirm in plan):** `model` from the agent config / run
metadata; `steps`/`stepBudget` from the execution-summary metrics
(total iterations + max-iterations). If a session predates these being recorded,
the fields are null.

## §3 — Overview tab (default)

`pages/detail/OverviewTab.tsx`:
- **Goal→Flow button**: `GOAL` · goal text · `progressText` · "View flow →" (switches to Flow tab).
- **KPI tiles** (3-col grid), label + big number + sub, derived client-side, *conditional* (coverage tiles only when coverage exists):
  - Pass rate `98.4%` (green) · `1,186 passed · 12 skipped`
  - Failing tests `7` (red) · `across 2 modules`
  - Modules built `3 / 4` (amber) · `acme-cli failed`
  - Line coverage `79.2%` · `4,120 / 5,200 lines`
  - Branch coverage `67.8%` · `610 / 900 branches`
  - Build time `2m 41s` · `mvn -B -T1C verify`
- **Per-module table**: Module · Build · Tests (bar) · Line cov (bar) · Branch cov
  (bar) — implemented as a new **`overview` variant of `ModuleTable`** (combines
  the build/test/coverage cells it already renders; keeps row logic in one place).
- **NeedsAttention panel** (`components/session/NeedsAttention.tsx`, new): failing
  tests grouped by module (`module · N failing` + names, `+N more`), then build
  warnings. Sourced from `modules[].failingNames` + `build.warnings`.

KPI derivation: `test.passRate`, `test.fail`, `moduleSummary.modulesBuilt/Total`,
`moduleSummary.lineRate/branchRate`, `build.time`.

## §4 — Flow tab + ActionDetailModal

`pages/detail/FlowTab.tsx` (supersedes the Context facet; reuses `ContextTrace`
internals where they fit):
- **Agent-goal trunk** (card): `AGENT GOAL` + status dot · goal · summary · progress
  bar + `progressText`. From `context.trunk`.
- **Phase timeline** (vertical connected dots): per phase → dot (green done / red
  failed) + line; header = title · `name` (mono) · completed/failed badge ·
  `N iterations · N actions` (right). Each **task** card → **iterations**
  (`ITERATION N`) → **think** rows (badge + italic thought) → **action** rows
  (clickable): dark mono tool badge · success/failed dot · honest badge → opens modal.

`components/session/ActionDetailModal.tsx` (new): tool badge + status; **TOOL
OUTPUT · raw tool result** (mono block; "truncated … · open full output" expands
the full text from the action's stored `refs[].content`); **OBSERVATION · agent's
interpretation** (the `observation` field). Surfaces the `output` vs `observation`
split. All data already in the read model (`action.output`, `action.refs[].content`,
`action.observation`).

**Per-action badge (honest):** derive from existing fields — `success` → **ok**,
`dispatchStatus: pending` (detached build still running) → **running**, `!success`
→ **failed**. Do NOT label "verified" (no per-action physical validation recorded).

## §5 — Components, files, testing

**Frontend (`webui/src`):**
- `pages/detail/DetailPane.tsx` — rewrite shell (tab switcher, fixed chrome, render active panel, drop scroll-spy, `initialFacet`→`initialTab` deep-link).
- `pages/detail/DetailHeader.tsx` — single-row + metadata line + ⋯ menu.
- `pages/detail/VerdictBand.tsx` *(new)*; `SummaryBand.tsx` removed from the tab map.
- `pages/detail/facets.tsx` — extend the tab list (Overview + Flow + facets) with gating + counts; `FacetTabs` drives the switcher (`onChange`).
- `pages/detail/OverviewTab.tsx` *(new)*, `pages/detail/FlowTab.tsx` *(new)*.
- `components/session/ActionDetailModal.tsx` *(new)*, `components/session/NeedsAttention.tsx` *(new)*, `ModuleTable.tsx` + `overview` variant, a small KPI-tile (Overview-local or `common`).

**Backend (`src/sag/web`):**
- `models.py` — `ExecutionSessionDetail` + `verdict`/`model`/`steps`/`stepBudget`; new `VerdictSummary`.
- `read_model.py` / `session_registry.py` — verdict composition helper + source model/steps/budget; `demo_data.py` populated.

**Testing:**
- vitest: VerdictBand tones; OverviewTab (conditional KPIs + grouping); FlowTab (timeline + open action); ActionDetailModal (output/observation/expand); ModuleTable overview variant; DetailPane (tab switch + gating + deep-link).
- pytest: verdict composition (success/partial/failed + single-module/no-coverage/no-failures edges); model/steps null-fallback; camelCase serialization (`model_dump(by_alias=True)`).
- Live: vite-dev + temp `/api` proxy + Chrome screenshots vs the design (header / verdict / overview / flow / modal), with strict cleanup.

## Out of scope / non-goals

- No `npm run build`; no edits to `src/sag/web/static/`. PR ships source only.
- No real per-action physical-validation status (honest derived badge instead).
- The BEFORE/AFTER toggle in the design template is a comparison device, not shipped.
- No unrelated refactors beyond what the re-shell touches.

## Risks / open items (resolve in plan)

- **`model` / `steps` sourcing**: confirm where the agent records the model name +
  iteration counts and whether they reach the read model; if unavailable for a
  session, ship null (header degrades). This is the main backend unknown.
- **`ContextTrace` reuse vs rebuild** in FlowTab: prefer reusing its internals
  (iteration/think/action rendering) restyled to the timeline; fall back to a
  fresh FlowTab if the shapes diverge too much.
- **Bundle rebuild** is the user's step; verify visually via vite-dev before PR.
