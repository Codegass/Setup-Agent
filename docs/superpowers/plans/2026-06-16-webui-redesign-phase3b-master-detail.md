# Phase 3b — Master-Detail Realignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Realign the workbench to the prototype's master-detail layout: a **persistent left workspace rail** (the workspace list *is* the dashboard) + a **right detail pane** whose facet nav is a **top horizontal pill bar** (not a left rail). Port `docs/Setup Agent Web UI/workbench/app.jsx` (Rail + shell) and switch `DetailPane` to the top-pill nav from `workbench/detail.jsx` (`detailStyle: "scroll"`).

**Why:** Phase 3 read "two-pane" as *separate Dashboard page ↔ Detail page with a left facet rail*. The actual reference is master-detail (left workspace rail + right detail + top facet pills). The spec IA was corrected on 2026-06-16. This phase realigns; the Phase 3 components (SummaryBand, facets, scroll-spy, DetailHeader, panels) carry over largely unchanged.

**Architecture:** Presentation-layer only. The app shell becomes `flex h-screen` → `WorkspaceRail` (left, ~320px) + `main` (right) holding `DetailPane` or an empty/placeholder state. The top app bar + breadcrumb are removed (the rail header owns the global chrome: logo, docker, launch). The separate `Dashboard` page and the detail's left `SectionNav` are retired. The app stays green at each task: new components are built and unit-tested in isolation, and `App.tsx` is rewired in the last implementation task.

**Tech Stack:** React + TypeScript (Vite), Tailwind v4 (Phase 1 `--status-*` tokens), Vitest + Testing Library, lucide-react.

**Branch:** `feature/webui-workbench-redesign` (Phase 1 + 2 merged to main; Phase 3 + the ContextTrace crash fix are on this branch, unmerged). Continue on it.

**Project policy (do not violate):**
- Do **not** run `npm run build`; do **not** create/modify/`git add` anything under `src/sag/web/static/`.
- `docs/` is gitignored — force-add docs (`git add -f docs/...`). New files under `webui/src/lib`/`webui/src/pages` may be ignored by a bare `lib/` rule — re-add the exact path with `git add -f` if `git add` reports it ignored. Never `git add -A`/`.`.
- No `Co-Authored-By` trailer on commits.

**Per-task verification:** `cd webui && npm test -- <test path>`; type check `cd webui && npx tsc -p tsconfig.app.json --noEmit`.

---

## Reference & Mapping

- **Rail** ← `workbench/app.jsx` `Rail` (lines 70–130) + `RailRow` (16–57). Header: logo + "SAG Workbench" + docker version, **Launch setups** button, summary chips (Workspaces · Running · Attention), filter input. Body: attention-first workspace rows. Footer: "Refreshes automatically" + the Phase 2 "Updated Ns ago"/poll-failure stamp.
- **Top facet pills** ← `workbench/detail.jsx` lines 206–219 (the sticky horizontal pill bar shown when `detailStyle !== "summary"`).
- **Selection/attention affordance:** the reference uses a 3px left stripe (`absolute inset-y-0 left-0 w-[3px]`). The project's design rules ban side-stripes > 1px; **use a full-row background tint instead** (`bg-status-running-soft` for selected, `bg-status-failed-soft/40` for attention) + the existing leading status dot. Document this as an intentional deviation.

Data per `RailRow` (from `WorkspaceSummary`): status dot `docker.status` (pulse when running), `project` + `release`, `stack`/`commit` meta, build glyph (`build.state` → Check/X/Clock), a compact test bar (`test` via `TestBar`), `needsAttention(ws)` indicator, selected when `ws.id === selectedId`. Pending launches render as muted "setting up…" rows.

---

## File Structure

**Create:**
- `webui/src/pages/detail/FacetTabs.tsx` + `FacetTabs.test.tsx` — top horizontal pill nav (replaces `SectionNav`).
- `webui/src/pages/WorkspaceRail.tsx` + `WorkspaceRail.test.tsx` — the left rail (replaces `Dashboard`).
- `webui/src/pages/RailSkeleton.tsx` + `RailSkeleton.test.tsx` — first-load rail placeholder (replaces `DashboardSkeleton`).
- `webui/src/components/launch/launchRows.ts` **only if** `pendingLaunchItems` is not already there — extract it from `Dashboard.tsx` (there is already a `launchRows.ts` with launch helpers + tests; add `pendingLaunchItems` to it if absent).

**Modify:**
- `webui/src/pages/detail/DetailPane.tsx` — replace the left `aside`/`SectionNav` with a sticky top `FacetTabs`; keep scroll-spy.
- `webui/src/pages/detail/DetailPane.test.tsx` — assert the top pills instead of the nav landmark.
- `webui/src/App.tsx` — master-detail shell; remove the top app bar + breadcrumb + `Dashboard` route; selection state + auto-select; wire rail.
- `webui/src/test/App.test.tsx` — update to the master-detail shell.

**Delete (last task):**
- `webui/src/pages/Dashboard.tsx`, `webui/src/pages/Dashboard.test.tsx`
- `webui/src/pages/DashboardSkeleton.tsx`
- `webui/src/pages/detail/SectionNav.tsx`, `webui/src/pages/detail/SectionNav.test.tsx`

**Reuse unchanged:** `dashboardAttention.ts` (`needsAttention`, `sortByAttentionFirst`), `relativeTime.ts` (`formatAgo`), `SummaryBand`, `facets.tsx`, `scrollSpy.ts`, `DetailHeader`, `NewTaskModal`, `WorkspaceSettings`, `WorkspacePanels`, `DeleteWorkspaceDialog`, `LaunchSetupsDialog`, `TestBar`, `Badge`/`StatusBadge`, `status.ts`.

---

## Task 1: FacetTabs (top pill nav)

**Files:** Create `webui/src/pages/detail/FacetTabs.tsx`, `webui/src/pages/detail/FacetTabs.test.tsx`

- [ ] **Step 1: Write the failing test**

`webui/src/pages/detail/FacetTabs.test.tsx`:

```tsx
import { fireEvent, render, screen } from "@testing-library/react"
import { Box } from "lucide-react"
import { describe, expect, it, vi } from "vitest"

import type { FacetMeta } from "./facets"
import { FacetTabs } from "./FacetTabs"

const facets: FacetMeta[] = [
  { id: "build", label: "Build", icon: Box, count: null, countTone: "neutral" },
  { id: "test", label: "Test", icon: Box, count: 2, countTone: "red" },
]

describe("FacetTabs", () => {
  it("renders each facet pill and marks the active one", () => {
    render(<FacetTabs facets={facets} active="test" onJump={() => {}} />)
    expect(screen.getByRole("button", { name: /^Test/ })).toHaveAttribute("aria-current", "true")
    expect(screen.getByRole("button", { name: /^Build/ })).toHaveAttribute("aria-current", "false")
  })

  it("calls onJump with the facet id when a pill is clicked", () => {
    const onJump = vi.fn()
    render(<FacetTabs facets={facets} active="build" onJump={onJump} />)
    fireEvent.click(screen.getByRole("button", { name: /^Test/ }))
    expect(onJump).toHaveBeenCalledWith("test")
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd webui && npm test -- src/pages/detail/FacetTabs.test.tsx` — FAIL (module not found).

- [ ] **Step 3: Implement**

`webui/src/pages/detail/FacetTabs.tsx`:

```tsx
import { cn } from "@/lib/utils"

import type { FacetId, FacetMeta } from "./facets"

export function FacetTabs({
  facets,
  active,
  onJump,
}: {
  facets: FacetMeta[]
  active: string | null
  onJump: (id: FacetId) => void
}) {
  return (
    <nav
      aria-label="Detail sections"
      className="sticky top-0 z-[var(--z-sticky)] flex items-center gap-1 overflow-x-auto border-b border-slate-200 bg-white/85 px-5 py-2 backdrop-blur-md sm:px-7"
    >
      {facets.map((f) => {
        const on = active === f.id
        return (
          <button
            key={f.id}
            aria-current={on}
            className={cn(
              "inline-flex shrink-0 items-center gap-1.5 rounded-full px-3 py-1 text-[12px] font-medium transition-colors",
              on ? "bg-slate-900 text-white" : "text-slate-500 hover:bg-slate-100",
            )}
            onClick={() => onJump(f.id)}
            type="button"
          >
            <f.icon size={13} />
            {f.label}
            {f.count != null ? (
              <span
                className={cn(
                  "rounded-full px-1.5 text-[10px] tabular-nums",
                  on ? "bg-white/20" : f.countTone === "red" ? "bg-status-failed-soft text-status-failed" : "bg-slate-200 text-slate-600",
                )}
              >
                {f.count}
              </span>
            ) : null}
          </button>
        )
      })}
    </nav>
  )
}
```

- [ ] **Step 4: Run test to verify it passes** — `cd webui && npm test -- src/pages/detail/FacetTabs.test.tsx` — PASS.

- [ ] **Step 5: Commit**

```bash
git add webui/src/pages/detail/FacetTabs.tsx webui/src/pages/detail/FacetTabs.test.tsx
git commit -m "feat(webui): top pill facet nav for the detail pane"
```

---

## Task 2: DetailPane → top pill nav (drop the left rail)

**Files:** Modify `webui/src/pages/detail/DetailPane.tsx`, `webui/src/pages/detail/DetailPane.test.tsx`; delete `SectionNav` + test.

- [ ] **Step 1: Update the DetailPane test (failing first)**

In `webui/src/pages/detail/DetailPane.test.tsx`, the first test asserts the nav landmark and the facet anchors. Keep the `navigation` assertion (FacetTabs still uses `aria-label="Detail sections"`) but also assert the pills render as buttons. Replace the facet-sections test body's nav assertion block with:

```tsx
    expect(screen.getByRole("navigation", { name: /detail sections/i })).toBeInTheDocument()
    // Top pills (one per facet) are buttons in the nav.
    expect(screen.getByRole("button", { name: /^Build/ })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /^Logs/ })).toBeInTheDocument()
```

- [ ] **Step 2: Run test to verify current passes still hold** — `cd webui && npm test -- src/pages/detail/DetailPane.test.tsx` — the new button assertions FAIL (SectionNav renders left-rail buttons with the same names, so they may pass; if they pass, that's fine — proceed). The intent is enforced after Step 3.

- [ ] **Step 3: Restructure DetailPane**

Replace the imports + body layout in `webui/src/pages/detail/DetailPane.tsx`. Swap `SectionNav` for `FacetTabs`, and change the body from a flex-row (aside + scroll) to a column (sticky tabs + scroll):

Imports — replace the `SectionNav` import:

```ts
import { FacetTabs } from "./FacetTabs"
```

(remove `import { SectionNav } from "./SectionNav"`; `FacetId` is still imported from `./facets`.)

Body — replace the `<div className="flex min-h-0 flex-1"> … </div>` block (the aside + scroll) with:

```tsx
      <FacetTabs active={active} facets={facets} onJump={(id: FacetId) => jump(id)} />

      {/* Single continuous scroll. `relative` makes this the offsetParent so
          section positions are measured within the scroll container. */}
      <div
        ref={containerRef}
        className="relative min-h-0 flex-1 overflow-y-auto px-5 py-6 sm:px-7"
        onScroll={onScroll}
      >
        <SummaryBand detail={detail} />
        <div className="mt-7 space-y-7">
          {facets.map((f) => (
            <section key={f.id} className="scroll-mt-[120px]" id={`facet-${f.id}`}>
              <div className="mb-2.5 flex items-center gap-2">
                <f.icon className="text-slate-400" size={14} />
                <h3 className="text-[13px] font-semibold tracking-tight text-slate-700">{f.label}</h3>
                <div className="ml-1 h-px flex-1 bg-slate-100" />
              </div>
              <FacetBody detail={detail} id={f.id} />
            </section>
          ))}
        </div>
        <div className="h-16" />
      </div>
```

The outer wrapper stays `<div className="flex h-[calc(100vh-3rem)] ... flex-col">` — but since the top app bar is removed in Task 5, change it to fill its pane: replace the outer wrapper className with `flex h-full min-h-0 flex-col` (the `main` in App provides the height). Keep `DetailHeader`, the panels, the modal, and the delete dialog exactly as they are.

- [ ] **Step 4: Delete SectionNav**

```bash
git rm webui/src/pages/detail/SectionNav.tsx webui/src/pages/detail/SectionNav.test.tsx
```

- [ ] **Step 5: Run tests + tsc** — `cd webui && npm test -- src/pages/detail/DetailPane.test.tsx && npx tsc -p tsconfig.app.json --noEmit` — PASS / clean.

- [ ] **Step 6: Commit**

```bash
git add webui/src/pages/detail/DetailPane.tsx webui/src/pages/detail/DetailPane.test.tsx
git commit -m "feat(webui): detail pane uses top pill nav (retire left section-nav)"
```

---

## Task 3: WorkspaceRail + RailRow

**Files:** Create `webui/src/pages/WorkspaceRail.tsx`, `webui/src/pages/WorkspaceRail.test.tsx`. If `pendingLaunchItems` is defined in `Dashboard.tsx` (not in `components/launch/launchRows.ts`), move it to `launchRows.ts` first and import it in both places.

- [ ] **Step 1: Write the failing test**

`webui/src/pages/WorkspaceRail.test.tsx`:

```tsx
import { fireEvent, render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import type { DashboardResponse, WorkspaceSummary } from "@/api/types"
import { WorkspaceRail } from "./WorkspaceRail"

function ws(overrides: Partial<WorkspaceSummary>): WorkspaceSummary {
  return {
    id: "sag-x", project: "owner/x", container: "sag-x", stack: "Java · Maven",
    docker: { status: "running", image: "sag/base" }, task: "t",
    build: { state: "success", tool: "Maven", time: "1s", note: "" },
    test: { state: "pass", pass: 10, fail: 0, skip: 0, total: 10 },
    report: "ready", changed: 0, updated: "just now", ...overrides,
  }
}

const data: DashboardResponse = {
  docker: { status: "connected", version: "27.1.1" },
  workspaces: [
    ws({ id: "sag-healthy", project: "owner/healthy" }),
    ws({ id: "sag-broken", project: "owner/broken", build: { state: "failure", tool: "Maven", time: "", note: "" } }),
  ],
}

const props = {
  data, selectedId: "sag-healthy", onSelect: () => {}, onLaunchSetups: () => {},
  launchQueue: null, highlightedWorkspaces: [], lastUpdatedAt: Date.now(), pollFailed: false,
}

describe("WorkspaceRail", () => {
  it("renders a row per workspace and marks the selected one", () => {
    render(<WorkspaceRail {...props} />)
    expect(screen.getByRole("button", { name: /owner\/healthy/ })).toHaveAttribute("aria-current", "true")
    expect(screen.getByRole("button", { name: /owner\/broken/ })).toHaveAttribute("aria-current", "false")
  })

  it("orders attention-needing workspaces first", () => {
    render(<WorkspaceRail {...props} />)
    const rows = screen.getAllByRole("button", { name: /owner\// })
    expect(rows[0].getAttribute("aria-label")).toContain("owner/broken")
  })

  it("selects a workspace when its row is clicked", () => {
    const onSelect = vi.fn()
    render(<WorkspaceRail {...props} onSelect={onSelect} />)
    fireEvent.click(screen.getByRole("button", { name: /owner\/broken/ }))
    expect(onSelect).toHaveBeenCalledWith("sag-broken")
  })

  it("filters rows by the query input", () => {
    render(<WorkspaceRail {...props} />)
    fireEvent.change(screen.getByPlaceholderText(/filter/i), { target: { value: "broken" } })
    expect(screen.getByRole("button", { name: /owner\/broken/ })).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /owner\/healthy/ })).not.toBeInTheDocument()
  })

  it("fires the launch action", () => {
    const onLaunchSetups = vi.fn()
    render(<WorkspaceRail {...props} onLaunchSetups={onLaunchSetups} />)
    fireEvent.click(screen.getByRole("button", { name: /launch setups/i }))
    expect(onLaunchSetups).toHaveBeenCalled()
  })

  it("shows the updated stamp in the footer", () => {
    render(<WorkspaceRail {...props} />)
    expect(screen.getByText(/updated just now/i)).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails** — `cd webui && npm test -- src/pages/WorkspaceRail.test.tsx` — FAIL (module not found).

- [ ] **Step 3: Implement**

`webui/src/pages/WorkspaceRail.tsx` — port `workbench/app.jsx` `Rail`/`RailRow`, reusing our primitives + helpers. Selection/attention via background tint (not a side-stripe).

```tsx
import { Activity, AlertTriangle, Check, Clock, GitBranch, Rocket, Search, X } from "lucide-react"
import { useState } from "react"

import type { DashboardResponse, LaunchQueueState, WorkspaceSummary } from "@/api/types"
import { TestBar } from "@/components/common/TestBar"
import { statusMeta } from "@/components/common/status"
import { formatAgo } from "@/lib/relativeTime"
import { cn } from "@/lib/utils"

import { needsAttention, sortByAttentionFirst } from "./dashboardAttention"

function normalize(value: string | null | undefined): string {
  return value?.trim().toLowerCase() ?? ""
}

function buildState(build: WorkspaceSummary["build"]): string {
  return normalize(typeof build === "string" ? build : build.state)
}

const DOT_TONE: Record<string, string> = {
  neutral: "bg-status-idle", blue: "bg-status-running", green: "bg-status-success",
  red: "bg-status-failed", amber: "bg-status-attention",
}

function RailRow({
  workspace,
  selected,
  highlighted,
  onSelect,
}: {
  workspace: WorkspaceSummary
  selected: boolean
  highlighted: boolean
  onSelect: (id: string) => void
}) {
  const dockerNorm = normalize(workspace.docker.status)
  const dot = DOT_TONE[statusMeta(workspace.docker.status).tone] ?? DOT_TONE.neutral
  const build = buildState(workspace.build)
  const attention = needsAttention(workspace)
  const total = Math.max(workspace.test.total, workspace.test.pass + workspace.test.fail)
  return (
    <button
      aria-current={selected}
      aria-label={`Open workspace ${workspace.project}`}
      className={cn(
        "group flex w-full items-center gap-3 border-b border-slate-100 px-3.5 py-2.5 text-left transition-colors last:border-b-0",
        selected ? "bg-status-running-soft" : attention ? "bg-status-failed-soft/40 hover:bg-status-failed-soft/60" : "hover:bg-slate-50/80",
        highlighted && !selected ? "bg-blue-50/60" : "",
      )}
      onClick={() => onSelect(workspace.id)}
      type="button"
    >
      <span className={cn("relative inline-flex h-1.5 w-1.5 shrink-0 rounded-full", dot)}>
        {dockerNorm === "running" || dockerNorm === "launching" ? (
          <span className={cn("absolute inline-flex h-full w-full animate-ping rounded-full opacity-75", dot)} />
        ) : null}
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5">
          <span className={cn("truncate text-[13px] font-medium", selected ? "text-status-running" : "text-slate-800")}>
            {workspace.project}
          </span>
          {workspace.release ? <span className="shrink-0 font-mono text-[9.5px] text-slate-500">{workspace.release}</span> : null}
          {workspace.activeSession ? <Activity className="shrink-0 text-status-running" size={11} /> : null}
        </span>
        <span className="mt-0.5 block truncate font-mono text-[10px] text-slate-500">
          {[workspace.stack, workspace.commit].filter(Boolean).join(" · ")}
        </span>
      </span>
      <span className="flex shrink-0 items-center gap-2">
        {build === "success" ? <Check className="text-status-success" size={13} /> : build === "failure" || build === "failed" ? <X className="text-status-failed" size={13} /> : <Clock className="text-slate-400" size={12} />}
        {normalize(workspace.test.state) !== "none" && total > 0 ? (
          <TestBar fail={workspace.test.fail} pass={workspace.test.pass} total={total} />
        ) : (
          <span className="w-10 text-right font-mono text-[10px] text-slate-400">—</span>
        )}
      </span>
    </button>
  )
}

function Chip({ label, value, tone }: { label: string; value: number; tone?: "blue" | "red" }) {
  return (
    <div className="flex-1 rounded-lg border border-slate-200 bg-white px-3 py-2">
      <div className={cn("text-[18px] font-semibold tabular-nums", tone === "red" ? "text-status-failed" : tone === "blue" ? "text-status-running" : "text-slate-900")}>
        {value}
      </div>
      <div className="font-mono text-[9px] uppercase tracking-[0.12em] text-slate-500">{label}</div>
    </div>
  )
}

export function WorkspaceRail({
  data,
  selectedId,
  onSelect,
  onLaunchSetups,
  highlightedWorkspaces = [],
  lastUpdatedAt = null,
  pollFailed = false,
}: {
  data: DashboardResponse
  selectedId: string | null
  onSelect: (id: string) => void
  onLaunchSetups: () => void
  highlightedWorkspaces?: string[]
  lastUpdatedAt?: number | null
  pollFailed?: boolean
}) {
  const [query, setQuery] = useState("")
  const ordered = sortByAttentionFirst(data.workspaces)
  const q = query.trim().toLowerCase()
  const rows = q
    ? ordered.filter((w) => w.project.toLowerCase().includes(q) || (w.stack ?? "").toLowerCase().includes(q))
    : ordered
  const running = data.workspaces.filter((w) => normalize(w.docker.status) === "running").length
  const attention = data.workspaces.filter(needsAttention).length

  return (
    <aside className="flex h-full min-h-0 w-[320px] shrink-0 flex-col border-r border-slate-200 bg-white">
      <div className="border-b border-slate-200 px-4 pb-3 pt-4">
        <div className="flex items-center gap-2">
          <span className="flex h-6 w-6 items-center justify-center rounded bg-slate-900 font-mono text-[11px] font-bold text-white">S</span>
          <div className="min-w-0">
            <div className="text-[13px] font-semibold tracking-tight text-slate-900">SAG Workbench</div>
            <div className="flex items-center gap-1 font-mono text-[9px] uppercase tracking-[0.14em] text-slate-500">
              <span className="inline-flex h-1 w-1 rounded-full bg-status-success" /> docker {data.docker.version ?? data.docker.status}
            </div>
          </div>
        </div>
        <button
          className="mt-3 inline-flex w-full items-center justify-center gap-1.5 rounded-md bg-slate-900 px-3 py-2 text-[12.5px] font-medium text-white hover:bg-slate-800"
          onClick={onLaunchSetups}
          type="button"
        >
          <Rocket size={14} /> Launch setups
        </button>
        <div className="mt-3 flex gap-2">
          <Chip label="Workspaces" value={data.workspaces.length} />
          <Chip label="Running" value={running} tone="blue" />
          <Chip label="Attention" value={attention} tone={attention ? "red" : undefined} />
        </div>
        <div className="relative mt-3">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400" size={13} />
          <input
            className="w-full rounded-md border border-slate-200 bg-slate-50/60 py-1.5 pl-8 pr-2 text-[12.5px] text-slate-700 placeholder:text-slate-400 focus:border-blue-300 focus:bg-white focus:outline-none focus:ring-2 focus:ring-blue-500/20"
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter workspaces…"
            value={query}
          />
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {rows.length ? (
          rows.map((w) => (
            <RailRow
              key={w.id}
              highlighted={highlightedWorkspaces.includes(w.id)}
              onSelect={onSelect}
              selected={w.id === selectedId}
              workspace={w}
            />
          ))
        ) : data.workspaces.length === 0 ? (
          <div className="flex flex-col items-center px-4 py-12 text-center">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg border border-slate-200 bg-slate-50 text-slate-500">
              <GitBranch size={18} />
            </div>
            <div className="mt-3 text-[13px] font-medium text-slate-700">No workspaces yet</div>
            <p className="mt-1 text-[12px] leading-relaxed text-slate-500">
              Launch a setup to add one. Paste a list of repo URLs to queue many at once.
            </p>
          </div>
        ) : (
          <div className="px-4 py-10 text-center text-[12px] text-slate-500">No matches</div>
        )}
      </div>

      <div className="flex items-center gap-2 border-t border-slate-100 px-4 py-2 font-mono text-[9px] text-slate-500">
        <span>{lastUpdatedAt != null ? `Updated ${formatAgo(Date.now() - lastUpdatedAt)}` : "Updating…"}</span>
        {pollFailed ? (
          <span className="inline-flex items-center gap-1 text-status-attention">
            <AlertTriangle size={10} /> couldn't refresh
          </span>
        ) : (
          <span>· refreshes automatically</span>
        )}
      </div>
    </aside>
  )
}
```

> Note: the footer's "Updated …" uses `Date.now()` once per render (no ticking interval here — the dashboard poll re-renders it every 5s, which is enough for a rail footer). If a live tick is wanted later, lift the `now` state in like `Dashboard` did. Keep it simple now.

- [ ] **Step 4: Run test to verify it passes** — `cd webui && npm test -- src/pages/WorkspaceRail.test.tsx` — PASS.

- [ ] **Step 5: Commit**

```bash
git add webui/src/pages/WorkspaceRail.tsx webui/src/pages/WorkspaceRail.test.tsx
git commit -m "feat(webui): persistent left workspace rail"
```

---

## Task 4: RailSkeleton

**Files:** Create `webui/src/pages/RailSkeleton.tsx`, `webui/src/pages/RailSkeleton.test.tsx`

- [ ] **Step 1: Write the failing test**

`webui/src/pages/RailSkeleton.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { RailSkeleton } from "./RailSkeleton"

describe("RailSkeleton", () => {
  it("exposes an accessible loading status", () => {
    render(<RailSkeleton />)
    expect(screen.getByRole("status", { name: /loading workspaces/i })).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails** — `cd webui && npm test -- src/pages/RailSkeleton.test.tsx` — FAIL.

- [ ] **Step 3: Implement**

`webui/src/pages/RailSkeleton.tsx`:

```tsx
const ROW_KEYS = ["r1", "r2", "r3", "r4", "r5", "r6"]

/** First-load placeholder shaped like the workspace rail. */
export function RailSkeleton() {
  return (
    <aside
      aria-label="Loading workspaces"
      className="flex h-full w-[320px] shrink-0 flex-col border-r border-slate-200 bg-white"
      role="status"
    >
      <div className="space-y-3 border-b border-slate-200 px-4 pb-3 pt-4">
        <div className="h-6 w-40 animate-pulse rounded bg-slate-100" />
        <div className="h-9 w-full animate-pulse rounded-md bg-slate-100" />
        <div className="flex gap-2">
          <div className="h-12 flex-1 animate-pulse rounded-lg bg-slate-100" />
          <div className="h-12 flex-1 animate-pulse rounded-lg bg-slate-100" />
          <div className="h-12 flex-1 animate-pulse rounded-lg bg-slate-100" />
        </div>
      </div>
      <div className="flex-1 space-y-px overflow-hidden px-3.5 py-2">
        {ROW_KEYS.map((key) => (
          <div key={key} className="flex items-center gap-3 py-2">
            <div className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-slate-200" />
            <div className="flex-1 space-y-1.5">
              <div className="h-3 w-2/3 animate-pulse rounded bg-slate-100" />
              <div className="h-2.5 w-1/2 animate-pulse rounded bg-slate-100" />
            </div>
          </div>
        ))}
      </div>
      <span className="sr-only">Loading workspaces…</span>
    </aside>
  )
}
```

- [ ] **Step 4: Run test to verify it passes** — PASS.

- [ ] **Step 5: Commit**

```bash
git add webui/src/pages/RailSkeleton.tsx webui/src/pages/RailSkeleton.test.tsx
git commit -m "feat(webui): rail-shaped first-load skeleton"
```

---

## Task 5: App shell → master-detail

**Files:** Modify `webui/src/App.tsx`, `webui/src/test/App.test.tsx`

- [ ] **Step 1: Update the App test (failing first)**

Rewrite `webui/src/test/App.test.tsx`'s structural expectations:
- "fetches and renders dashboard data after loading": assert the **rail skeleton** on first paint then the rail rows:
  ```ts
  expect(screen.getByRole("status", { name: /loading workspaces/i })).toBeInTheDocument()
  expect((await screen.findAllByRole("button", { name: /apache\/commons-cli/i })).length).toBeGreaterThan(0)
  ```
- The detail flow test: after the rail auto-selects (or after clicking the row), assert the detail header + top pills:
  ```ts
  fireEvent.click((await screen.findAllByRole("button", { name: /apache\/commons-cli/i }))[0])
  expect(await screen.findByRole("heading", { name: "apache/commons-cli" })).toBeInTheDocument()
  expect(screen.getByRole("navigation", { name: /detail sections/i })).toBeInTheDocument()
  ```
- New-task submit: open from the detail header `New task` button, fill the textarea (`/add a health check/i`), submit (`/submit task/i`), assert the submit fetch fired.
- The "keeps stale dashboard data visible when refresh fails" test: the loud banner is already gone; assert the rail still shows rows and the footer shows `/couldn't refresh/i`.
- Remove assertions tied to the deleted top breadcrumb / `docker · connected` top chip; the docker label now lives in the rail header as `docker {version|status}` — assert that text if needed.

Adapt to the file's existing fetch-spy scaffolding; keep the data fixtures.

- [ ] **Step 2: Run test to verify it fails** — `cd webui && npm test -- src/test/App.test.tsx` — FAIL.

- [ ] **Step 3: Rewrite the App shell**

In `webui/src/App.tsx`:

3a. Replace imports: drop `Dashboard`, `DashboardSkeleton`, `StatusBadge` (top chip), `Breadcrumb`. Add:

```ts
import { WorkspaceRail } from "@/pages/WorkspaceRail"
import { RailSkeleton } from "@/pages/RailSkeleton"
import { DetailPane } from "@/pages/detail/DetailPane"
```

3b. Replace the `Route` union + selection with explicit selection state:

```ts
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState<string | null>(null)
  const [selectedSessionId, setSelectedSessionIdState] = useState<string | undefined>(undefined)
  const [selectedFacet, setSelectedFacet] = useState<string | undefined>(undefined)
```

Derive the selected workspace + session id (default to the workspace's latest session):

```ts
  const selectedWorkspace = dashboard?.workspaces.find((w) => w.id === selectedWorkspaceId) ?? null
  const sessionId = selectedSessionId ?? selectedWorkspace?.latestSession ?? undefined
```

Auto-select the first attention-first workspace once the dashboard loads and nothing is selected:

```ts
  useEffect(() => {
    if (!dashboard || selectedWorkspaceId) {
      return
    }
    const first = sortByAttentionFirst(dashboard.workspaces)[0]
    if (first) {
      setSelectedWorkspaceId(first.id)
    }
  }, [dashboard, selectedWorkspaceId])
```

(import `sortByAttentionFirst` from `@/pages/dashboardAttention`.)

Selection handlers:

```ts
  const selectWorkspace = (id: string) => {
    setSelectedWorkspaceId(id)
    setSelectedSessionIdState(undefined) // fall back to that workspace's latest session
    setSelectedFacet(undefined)
    setRouteError(null)
  }
  const selectSession = (id: string) => setSelectedSessionIdState(id)
```

3c. Update the session-load + poll effects to key off `sessionId` (same logic as before, with `route.view === "detail"` checks removed — there is only one view now):

```ts
  useEffect(() => {
    if (!dashboard || !sessionId) return
    void ensureSessionDetail(sessionId)
  }, [dashboard, ensureSessionDetail, sessionId])

  useEffect(() => {
    if (!sessionId) return
    const detail = sessionDetails[sessionId]
    if (detail && !isLiveSessionStatus(detail.status)) return
    const interval = window.setInterval(() => void ensureSessionDetail(sessionId, { silent: true }), SESSION_DETAIL_POLL_MS)
    return () => window.clearInterval(interval)
  }, [ensureSessionDetail, sessionId, sessionDetails])
```

3d. `deleteWorkspaceFromDetail` clears the selection so the auto-select picks another:

```ts
  const deleteWorkspaceFromDetail = async (workspaceId: string): Promise<void> => {
    await deleteWorkspace(workspaceId)
    setSelectedWorkspaceId(null)
    setSelectedSessionIdState(undefined)
  }
```

3e. Replace the entire `return (...)` JSX with the master-detail shell:

```tsx
  return (
    <div className="flex h-screen min-h-0 w-full overflow-hidden bg-[#fbfbfc] text-slate-900">
      {loading && !dashboard ? (
        <RailSkeleton />
      ) : dashboard ? (
        <WorkspaceRail
          data={dashboard}
          highlightedWorkspaces={highlightedWorkspaces}
          lastUpdatedAt={lastUpdatedAt}
          onLaunchSetups={() => setLaunchDialogOpen(true)}
          onSelect={selectWorkspace}
          pollFailed={Boolean(dashboardError)}
          selectedId={selectedWorkspaceId}
        />
      ) : null}

      <main className="min-h-0 flex-1 overflow-hidden bg-white">
        {!dashboard && !loading && dashboardError ? (
          <div className="p-6">
            <Card className="max-w-xl p-5">
              <div className="text-[15px] font-semibold text-slate-900">Dashboard unavailable</div>
              <div className="mt-2 font-mono text-[12px] text-red-600">{dashboardError}</div>
              <Button className="mt-4" onClick={() => void loadDashboard()} type="button" variant="outline">Retry</Button>
            </Card>
          </div>
        ) : dashboard && selectedWorkspace && sessionId && sessionDetails[sessionId] ? (
          <DetailPane
            key={sessionId}
            detail={sessionDetails[sessionId]}
            initialFacet={selectedFacet}
            onDelete={deleteWorkspaceFromDetail}
            onSession={selectSession}
            onSubmitTask={submitWorkspaceTask}
            sessionId={sessionId}
            workspace={selectedWorkspace}
          />
        ) : dashboard && selectedWorkspace && sessionId && sessionErrors[sessionId] ? (
          <div className="p-6">
            <Card className="max-w-xl p-5">
              <div className="text-[15px] font-semibold text-slate-900">Session {sessionId} unavailable</div>
              <div className="mt-2 font-mono text-[12px] text-red-600">{sessionErrors[sessionId]}</div>
              <Button className="mt-4" onClick={() => void ensureSessionDetail(sessionId)} type="button" variant="outline">Retry</Button>
            </Card>
          </div>
        ) : dashboard && selectedWorkspace ? (
          <div className="p-6 font-mono text-[13px] text-slate-500">
            {sessionId ? `Loading session ${sessionId}…` : "This workspace has no execution session yet."}
          </div>
        ) : dashboard ? (
          <div className="flex h-full items-center justify-center p-6 text-center">
            <div>
              <div className="text-[15px] font-semibold text-slate-800">Select a workspace</div>
              <p className="mt-1 text-[13px] text-slate-500">Pick a workspace from the rail, or launch a new setup.</p>
            </div>
          </div>
        ) : null}
      </main>

      {launchNotice ? (
        <div className="fixed bottom-4 left-1/2 z-[var(--z-toast)] -translate-x-1/2">
          <Card className="flex items-center gap-3 border-blue-100 bg-blue-50/90 px-4 py-3 text-[13px] shadow-lg backdrop-blur">
            <span className="text-blue-700">{launchNotice}</span>
            <Button onClick={() => setLaunchNotice(null)} type="button" variant="outline">Dismiss</Button>
          </Card>
        </div>
      ) : null}

      {launchDialogOpen ? (
        <LaunchSetupsDialog
          defaultConcurrency={launchQueue?.default_concurrency ?? 1}
          onClose={() => setLaunchDialogOpen(false)}
          onSubmit={submitProjectBatch}
          onSubmitted={handleBatchSubmitted}
        />
      ) : null}
    </div>
  )
```

3f. Delete the now-unused `openDashboard`/`openDetail`/`openWorkspace`/`openSession` helpers and the `Breadcrumb` component. Keep `isLiveSessionStatus`, `submitWorkspaceTask`, `deleteWorkspace`, `handleBatchSubmitted`, launch state, `routeError` (still set by `ensureSessionDetail`). `submitWorkspaceTask` no longer needs to be passed `openSession`. If `routeError` is now only used inside the session-error branch, keep it; otherwise remove cleanly. Ensure no unused imports remain (`tsc` will flag them).

> Note: `z-toast` token — confirm `--z-toast` exists in `styles.css` (Phase 1 added `--z-tooltip:70`; if `--z-toast` is absent, use `z-[var(--z-modal)]` or add `--z-toast`). Pick an existing token to avoid an undefined var.

- [ ] **Step 4: Run tests + tsc** — `cd webui && npm test -- src/test/App.test.tsx && npx tsc -p tsconfig.app.json --noEmit` — PASS / clean. Fix any unused-symbol errors.

- [ ] **Step 5: Commit**

```bash
git add webui/src/App.tsx webui/src/test/App.test.tsx
git commit -m "feat(webui): master-detail app shell (workspace rail + detail pane)"
```

---

## Task 6: Retire the standalone Dashboard + DashboardSkeleton

**Files:** Delete `Dashboard.tsx`, `Dashboard.test.tsx`, `DashboardSkeleton.tsx`. (`SectionNav` was deleted in Task 2.)

- [ ] **Step 1: Confirm nothing imports them**

Run: `cd webui && grep -rn "pages/Dashboard\b\|pages/DashboardSkeleton\|pages/Dashboard\"\|DashboardSkeleton\|from \"@/pages/Dashboard\"" src` — only the files being deleted (and their own tests) should appear. If `pendingLaunchItems` or any helper is still imported from `Dashboard.tsx`, move it to `components/launch/launchRows.ts` first (Task 3 should have handled this) and update importers.

- [ ] **Step 2: Delete**

```bash
git rm webui/src/pages/Dashboard.tsx webui/src/pages/Dashboard.test.tsx webui/src/pages/DashboardSkeleton.tsx
```

- [ ] **Step 3: Run full suite + tsc** — `cd webui && npm test && npx tsc -p tsconfig.app.json --noEmit` — PASS / clean. Resolve any dangling references.

- [ ] **Step 4: Commit**

```bash
git add -A webui/src/pages
git commit -m "refactor(webui): retire standalone Dashboard page (replaced by the rail)"
```

> Exception to the no-`git add -A` rule: scope it to `webui/src/pages` only, and only after confirming (Step 1/3) that the sole changes there are the deletions. Prefer `git rm` (Step 2) which already stages the removals; then a plain `git commit` suffices and Step 4's add is a no-op safety net — if unsure, run `git commit` without the add.

---

## Task 7: Full verification

- [ ] **Step 1: Full suite** — `cd webui && npm test` — all green.
- [ ] **Step 2: Type-check** — `cd webui && npx tsc -p tsconfig.app.json --noEmit` — clean.
- [ ] **Step 3: No `static/` staged** — `git -C .. diff --cached --name-only -- src/sag/web/static` — empty.
- [ ] **Step 4: No dangling imports** — `cd webui && grep -rn "pages/Dashboard\|pages/DashboardSkeleton\|detail/SectionNav" src || echo "clean"` — `clean`.
- [ ] **Step 5: Visual (maintainer)** — the maintainer rebuilds (`npm run build`) + `sag ui --demo` and confirms: persistent left rail with workspace rows (attention-first, selected tint, filter, launch, chips), selecting a row updates the right detail pane, the detail pane shows the top pill nav + summary band + facet sections, scroll-spy highlights pills, and **no runtime crash** clicking through workspaces (the ContextTrace guard from `0658b7e` holds).

---

## Self-Review

**Spec coverage (corrected IA, 2026-06-16):**
- Persistent left workspace rail (list = dashboard; logo + docker + launch + chips + filter + attention-first rows + footer stamp) → Task 3.
- Right detail pane with top facet pill nav → Tasks 1, 2.
- Master-detail shell, no top app bar/breadcrumb, auto-select, empty state → Task 5.
- Retire separate Dashboard page + left SectionNav → Tasks 2, 6.
- Reuse SummaryBand/facets/scroll-spy/DetailHeader/panels and the ContextTrace crash fix → carried over.

**Type/name consistency:** `FacetTabs`(T1) used in `DetailPane`(T2); `WorkspaceRail`(T3) + `RailSkeleton`(T4) used in `App`(T5); `needsAttention`/`sortByAttentionFirst`/`formatAgo` reused; `FacetMeta`/`FacetId` from `facets.tsx`. `DetailPane` props unchanged (App already passes them).

**Placeholder scan:** none — complete code for new components; reused renderers referenced explicitly.

**Risk notes:**
- Reference uses a side-stripe selection indicator; we use a background tint per the project's anti-stripe rule (Task 3) — intentional, documented.
- App test churn is the largest (Task 5); adapt to the existing fetch-spy scaffolding, don't assume variable names.
- Rail rows and the dashboard row both used `aria-label="Open workspace …"`; the rail renders ONE tree (no desktop/mobile duplication), so singular `getByRole`/`getByLabelText` is safe here.
- Narrow-width responsive (rail → horizontal strip) is deferred to Phase 6 polish; the rail is fixed-width for now.
