# Phase 2 — Dashboard Reshape Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reshape the dashboard into a calm, attention-first workspaces list — failures sort to the top with a status-token tint, the "Need attention" tile becomes a filter, first load shows skeleton rows, and a quiet "Updated Ns ago / couldn't refresh" footer replaces the loud refresh banner.

**Architecture:** Presentation-layer only. Pure logic (attention ordering, relative-time formatting) is extracted into small focused modules with unit tests; `Dashboard.tsx` is reshaped to consume them; `App.tsx` is rewired to pass a `lastUpdatedAt` timestamp + poll-failure flag and to render a skeleton on first load. Reuses the Phase 1 `--status-*` semantic tokens and the existing primitives (`Badge`/`StatusBadge`, `Card`, `Button`, `TestBar`, `status.ts`). No backend or data-contract changes.

**Tech Stack:** React + TypeScript (Vite), Tailwind v4 (CSS-first tokens), Vitest + Testing Library.

**Branch:** `feature/webui-workbench-redesign` (spec + Phase 1 already merged). Work continues on this branch.

**Project policy (do not violate):**
- Do **not** run `npm run build` and do **not** commit `src/sag/web/static/` (the live site is served from the `ui` branch).
- `docs/` is gitignored — force-add plan/spec files: `git add -f docs/...`.
- Stage exact paths; never `git add -A` / `git add .`.
- No `Co-Authored-By` trailer on commits.

**Per-task verification (run after each task):**
- Targeted tests: `npm test -- <test path>`
- Type check (end of plan, and any time types change): `npx tsc -p tsconfig.app.json --noEmit`

---

## File Structure

**Create:**
- `webui/src/lib/relativeTime.ts` — pure `formatAgo(elapsedMs)` → human string. One responsibility: relative-time formatting.
- `webui/src/lib/relativeTime.test.ts` — unit tests for `formatAgo`.
- `webui/src/pages/dashboardAttention.ts` — pure `needsAttention(ws)` + `sortByAttentionFirst(list)`. One responsibility: attention classification + ordering. Self-contained (inlines status normalization) so it has no React dependency and is trivially testable.
- `webui/src/pages/dashboardAttention.test.ts` — unit tests for the two helpers.
- `webui/src/pages/DashboardSkeleton.tsx` — first-load placeholder (kept separate so `App.tsx` can import it without pulling the whole `Dashboard`).

**Modify:**
- `webui/src/pages/Dashboard.tsx` — import the attention helpers (replace the local `needsAttention`); attention-first ordering + row/card tint; make the "Need attention" summary tile a filter affordance; add the "Updated Ns ago / couldn't refresh / Show all" footer; refine the empty-state paste hint + tokens. New optional props: `lastUpdatedAt`, `pollFailed`, `pollError`.
- `webui/src/pages/Dashboard.test.tsx` — add tests for ordering, tint, the filter, the footer stamp, the poll-failure indicator, and the paste hint.
- `webui/src/App.tsx` — track `lastUpdatedAt` on successful load; render `<DashboardSkeleton />` on first load (replacing the "Loading workspaces..." card); drop the loud red "Refresh failed" banner; pass `lastUpdatedAt` / `pollFailed` / `pollError` to `Dashboard`.
- `webui/src/test/App.test.tsx` — update the two assertions affected by the skeleton + footer changes.

**Reference (read-only, do not edit):**
- Prototype: `docs/Setup Agent Web UI/src/Dashboard.jsx` and `docs/Setup Agent Web UI/screenshots/`.
- Phase 1 tokens: `webui/src/styles.css` (`--status-{idle,running,success,failed,attention}{,-soft,-border}` → utilities `text-status-*`, `bg-status-*-soft`, `border/ring-status-*-border`).

---

## Task 1: Relative-time helper (`formatAgo`)

A pure function so the footer stamp is testable without timers.

**Files:**
- Create: `webui/src/lib/relativeTime.ts`
- Test: `webui/src/lib/relativeTime.test.ts`

- [ ] **Step 1: Write the failing test**

`webui/src/lib/relativeTime.test.ts`:

```ts
import { describe, expect, it } from "vitest"

import { formatAgo } from "./relativeTime"

describe("formatAgo", () => {
  it("treats the last few seconds as 'just now'", () => {
    expect(formatAgo(0)).toBe("just now")
    expect(formatAgo(4_000)).toBe("just now")
  })

  it("formats seconds, minutes, and hours", () => {
    expect(formatAgo(5_000)).toBe("5s ago")
    expect(formatAgo(59_000)).toBe("59s ago")
    expect(formatAgo(60_000)).toBe("1m ago")
    expect(formatAgo(59 * 60_000)).toBe("59m ago")
    expect(formatAgo(60 * 60_000)).toBe("1h ago")
    expect(formatAgo(3 * 60 * 60_000)).toBe("3h ago")
  })

  it("never returns a negative value", () => {
    expect(formatAgo(-10_000)).toBe("just now")
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test -- src/lib/relativeTime.test.ts`
Expected: FAIL — cannot resolve `./relativeTime` / `formatAgo` not defined.

- [ ] **Step 3: Write minimal implementation**

`webui/src/lib/relativeTime.ts`:

```ts
/** Human-friendly "time ago" string from an elapsed-milliseconds value. */
export function formatAgo(elapsedMs: number): string {
  const seconds = Math.max(0, Math.floor(elapsedMs / 1000))
  if (seconds < 5) {
    return "just now"
  }
  if (seconds < 60) {
    return `${seconds}s ago`
  }
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) {
    return `${minutes}m ago`
  }
  const hours = Math.floor(minutes / 60)
  return `${hours}h ago`
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm test -- src/lib/relativeTime.test.ts`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add webui/src/lib/relativeTime.ts webui/src/lib/relativeTime.test.ts
git commit -m "feat(webui): add formatAgo relative-time helper"
```

---

## Task 2: Attention helpers (classification + ordering)

Extract the existing `needsAttention` logic into a pure module and add a stable attention-first sort. This is the data behind both the ordering and the filter.

**Files:**
- Create: `webui/src/pages/dashboardAttention.ts`
- Test: `webui/src/pages/dashboardAttention.test.ts`

- [ ] **Step 1: Write the failing test**

`webui/src/pages/dashboardAttention.test.ts`:

```ts
import { describe, expect, it } from "vitest"

import type { WorkspaceSummary } from "@/api/types"

import { needsAttention, sortByAttentionFirst } from "./dashboardAttention"

function ws(overrides: Partial<WorkspaceSummary>): WorkspaceSummary {
  return {
    id: "sag-x",
    project: "owner/x",
    container: "sag-x",
    stack: "Java · Maven",
    docker: { status: "running", image: "sag/base" },
    task: "Build and test",
    build: { state: "success", tool: "Maven", time: "1s", note: "" },
    test: { state: "pass", pass: 10, fail: 0, skip: 0, total: 10 },
    report: "ready",
    changed: 0,
    updated: "just now",
    ...overrides,
  }
}

describe("needsAttention", () => {
  it("flags a failed build", () => {
    expect(needsAttention(ws({ build: { state: "failure", tool: "", time: "", note: "" } }))).toBe(true)
  })

  it("flags failed tests and a partial run with real failures", () => {
    expect(needsAttention(ws({ test: { state: "fail", pass: 0, fail: 3, skip: 0, total: 3 } }))).toBe(true)
    expect(needsAttention(ws({ test: { state: "partial", pass: 8, fail: 2, skip: 0, total: 10 } }))).toBe(true)
  })

  it("flags a stopped/exited container but not a freshly created one", () => {
    expect(needsAttention(ws({ docker: { status: "exited", image: "sag/base" } }))).toBe(true)
    expect(needsAttention(ws({ docker: { status: "created", image: "sag/base" } }))).toBe(false)
  })

  it("treats a string build value the same as the object form", () => {
    expect(needsAttention(ws({ build: "failure" }))).toBe(true)
    expect(needsAttention(ws({ build: "success" }))).toBe(false)
  })

  it("keeps a healthy workspace quiet", () => {
    expect(needsAttention(ws({}))).toBe(false)
  })
})

describe("sortByAttentionFirst", () => {
  it("moves attention-needing workspaces to the top, preserving order within groups", () => {
    const healthyA = ws({ id: "a" })
    const failing = ws({ id: "b", build: { state: "failure", tool: "", time: "", note: "" } })
    const healthyC = ws({ id: "c" })

    const ordered = sortByAttentionFirst([healthyA, failing, healthyC])

    expect(ordered.map((w) => w.id)).toEqual(["b", "a", "c"])
  })

  it("does not mutate the input array", () => {
    const list = [ws({ id: "a" }), ws({ id: "b", docker: { status: "exited", image: "x" } })]
    sortByAttentionFirst(list)
    expect(list.map((w) => w.id)).toEqual(["a", "b"])
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test -- src/pages/dashboardAttention.test.ts`
Expected: FAIL — cannot resolve `./dashboardAttention`.

- [ ] **Step 3: Write minimal implementation**

`webui/src/pages/dashboardAttention.ts`:

```ts
import type { WorkspaceSummary } from "@/api/types"

function normalize(value: string | null | undefined): string {
  return value?.trim().toLowerCase() ?? ""
}

function buildState(build: WorkspaceSummary["build"]): string {
  return normalize(typeof build === "string" ? build : build.state)
}

/** A workspace needs attention if its build failed, tests failed, or its container stopped unexpectedly. */
export function needsAttention(workspace: WorkspaceSummary): boolean {
  const build = buildState(workspace.build)
  const test = normalize(workspace.test.state)
  const docker = normalize(workspace.docker.status)

  const buildFailed = build === "failure" || build === "failed"
  const testFailed =
    test === "fail" ||
    test === "failed" ||
    (test === "partial" && workspace.test.fail > 0)
  // Any container that isn't running or freshly created has stopped unexpectedly.
  const containerDown = docker !== "" && docker !== "running" && docker !== "created"

  return buildFailed || testFailed || containerDown
}

/** Stable sort: attention-needing workspaces first, original order preserved within each group. */
export function sortByAttentionFirst(workspaces: WorkspaceSummary[]): WorkspaceSummary[] {
  return [...workspaces].sort(
    (a, b) => Number(needsAttention(b)) - Number(needsAttention(a)),
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm test -- src/pages/dashboardAttention.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webui/src/pages/dashboardAttention.ts webui/src/pages/dashboardAttention.test.ts
git commit -m "feat(webui): extract attention classification + attention-first sort"
```

---

## Task 3: Attention-first ordering + status tint in `Dashboard.tsx`

Use the new helpers: drop the local `needsAttention`, sort rows/cards attention-first, and tint attention rows with the failed-status token. Healthy rows stay quiet.

**Files:**
- Modify: `webui/src/pages/Dashboard.tsx`
- Test: `webui/src/pages/Dashboard.test.tsx`

- [ ] **Step 1: Write the failing test**

Append to `webui/src/pages/Dashboard.test.tsx` (inside the `describe("Dashboard", …)` block):

```ts
  const twoWorkspaces: DashboardResponse = {
    docker: { status: "connected", version: "27.1.1" },
    workspaces: [
      {
        id: "sag-healthy",
        project: "owner/healthy",
        container: "sag-healthy",
        stack: "Java · Maven",
        docker: { status: "running", image: "sag/base" },
        task: "All good",
        build: { state: "success", tool: "Maven", time: "1s", note: "" },
        test: { state: "pass", pass: 10, fail: 0, skip: 0, total: 10 },
        report: "ready",
        changed: 0,
        updated: "just now",
      },
      {
        id: "sag-broken",
        project: "owner/broken",
        container: "sag-broken",
        stack: "Java · Gradle",
        docker: { status: "running", image: "sag/base" },
        task: "Build failed",
        build: { state: "failure", tool: "Gradle", time: "2s", note: "" },
        test: { state: "pending", pass: 0, fail: 0, skip: 0, total: 0 },
        report: "none",
        changed: 0,
        updated: "just now",
      },
    ],
  }

  it("orders attention-needing workspaces ahead of healthy ones", () => {
    render(
      <Dashboard data={twoWorkspaces} onOpenWorkspace={() => {}} onOpenSession={() => {}} />,
    )

    const rows = screen.getAllByLabelText(/open workspace owner\//i)
    // The failing workspace row renders before the healthy one in the DOM.
    const brokenIndex = rows.findIndex((r) => r.getAttribute("aria-label")?.includes("owner/broken"))
    const healthyIndex = rows.findIndex((r) => r.getAttribute("aria-label")?.includes("owner/healthy"))
    expect(brokenIndex).toBeGreaterThanOrEqual(0)
    expect(brokenIndex).toBeLessThan(healthyIndex)
  })

  it("tints rows that need attention and leaves healthy rows quiet", () => {
    render(
      <Dashboard data={twoWorkspaces} onOpenWorkspace={() => {}} onOpenSession={() => {}} />,
    )

    // NOTE: Dashboard renders BOTH a desktop row and a mobile card with the same
    // aria-label; jsdom ignores the responsive hide/show, so use getAllByLabelText
    // and assert on the first (desktop) match — mirroring the "highlights newly
    // launched workspaces" test. Singular getByLabelText throws "multiple elements".
    const broken = screen.getAllByLabelText("Open workspace owner/broken")[0]
    const healthy = screen.getAllByLabelText("Open workspace owner/healthy")[0]
    expect(broken.className).toContain("bg-status-failed-soft")
    expect(healthy.className).not.toContain("bg-status-failed-soft")
  })
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test -- src/pages/Dashboard.test.tsx`
Expected: FAIL — rows are in source order (broken after healthy) and no tint class present.

- [ ] **Step 3: Implement ordering + tint**

In `webui/src/pages/Dashboard.tsx`:

3a. Add the `cn` import and the attention-helper import; **remove** the local `normalize`-based `needsAttention` function (lines 87–102) — keep the `normalize` helper (still used elsewhere in the file). At the top of the imports block add:

```ts
import { cn } from "@/lib/utils"
import { needsAttention, sortByAttentionFirst } from "./dashboardAttention"
```

Delete this whole local function (it now lives in `dashboardAttention.ts`):

```ts
function needsAttention(workspace: WorkspaceSummary): boolean {
  const buildState = normalize(buildDetails(workspace.build).state)
  const testState = normalize(workspace.test.state)
  const dockerState = normalize(workspace.docker.status)

  const buildFailed = buildState === "failure" || buildState === "failed"
  const testFailed =
    testState === "fail" ||
    testState === "failed" ||
    (testState === "partial" && workspace.test.fail > 0)
  // Any container that isn't running or freshly created has stopped unexpectedly.
  const containerDown =
    dockerState !== "" && dockerState !== "running" && dockerState !== "created"

  return buildFailed || testFailed || containerDown
}
```

3b. In the `Dashboard` component body, compute the ordered list. Replace:

```ts
  const workspaces = data.workspaces
```

with:

```ts
  const workspaces = data.workspaces
  const orderedWorkspaces = sortByAttentionFirst(workspaces)
```

3c. Render the ordered list. In the desktop `Card` table, change the `.map` source from `workspaces.map` to `orderedWorkspaces.map`:

```tsx
            {orderedWorkspaces.map((workspace) => (
              <WorkspaceRow
                key={workspace.id}
                attention={needsAttention(workspace)}
                highlighted={highlightedWorkspaces.includes(workspace.id)}
                onDelete={setDeleteTarget}
                onOpenSession={onOpenSession}
                onOpenWorkspace={onOpenWorkspace}
                workspace={workspace}
              />
            ))}
```

And likewise the mobile grid:

```tsx
            {orderedWorkspaces.map((workspace) => (
              <WorkspaceCard
                key={workspace.id}
                attention={needsAttention(workspace)}
                highlighted={highlightedWorkspaces.includes(workspace.id)}
                onDelete={setDeleteTarget}
                onOpenSession={onOpenSession}
                onOpenWorkspace={onOpenWorkspace}
                workspace={workspace}
              />
            ))}
```

3d. Add an `attention` prop to `WorkspaceRow` and apply the tint. Update its signature and `className`. The precedence is: recent-launch highlight (blue) wins over the attention tint, which wins over the default. Replace the `WorkspaceRow` props type + the wrapper `div` className:

```tsx
function WorkspaceRow({
  workspace,
  onOpenWorkspace,
  onOpenSession,
  onDelete,
  highlighted = false,
  attention = false,
}: {
  workspace: WorkspaceSummary
  onOpenWorkspace: (workspaceId: string) => void
  onOpenSession: (workspaceId: string, sessionId: string, tab?: string) => void
  onDelete: (target: DeleteWorkspaceTarget) => void
  highlighted?: boolean
  attention?: boolean
}) {
```

```tsx
    <div
      aria-label={`Open workspace ${workspace.project}`}
      className={cn(
        "group grid",
        tableColumns,
        "cursor-pointer items-center gap-3 border-b border-slate-100 px-4 py-3 text-left transition-colors duration-700 last:border-b-0 hover:bg-slate-50/70 focus-visible:bg-slate-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/30",
        attention && "bg-status-failed-soft/50",
        highlighted && "bg-blue-50/60",
      )}
```

3e. Add the same `attention` prop to `WorkspaceCard`. Update its signature and the `Card` className:

```tsx
function WorkspaceCard({
  workspace,
  onOpenWorkspace,
  onOpenSession,
  onDelete,
  highlighted = false,
  attention = false,
}: {
  workspace: WorkspaceSummary
  onOpenWorkspace: (workspaceId: string) => void
  onOpenSession: (workspaceId: string, sessionId: string, tab?: string) => void
  onDelete: (target: DeleteWorkspaceTarget) => void
  highlighted?: boolean
  attention?: boolean
}) {
```

```tsx
    <Card
      aria-label={`Open workspace ${workspace.project}`}
      className={cn(
        "cursor-pointer p-4 transition-colors duration-700 hover:bg-slate-50/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/30",
        attention && "border-status-failed-border bg-status-failed-soft/50",
        highlighted && "border-blue-200 bg-blue-50/60",
      )}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npm test -- src/pages/Dashboard.test.tsx`
Expected: PASS — new ordering + tint tests pass, all existing Dashboard tests still pass (the single-workspace fixture is unaffected; the `highlightedWorkspaces` test still finds `bg-blue-50/60`).

- [ ] **Step 5: Commit**

```bash
git add webui/src/pages/Dashboard.tsx webui/src/pages/Dashboard.test.tsx
git commit -m "feat(webui): attention-first ordering + status tint on dashboard rows"
```

---

## Task 4: "Need attention" tile becomes a filter affordance

Make the summary tile interactive: clicking it filters the list to attention-only (workspaces + failed launches), and clicking again clears it. The tile reflects pressed state; the count turns red when > 0.

**Files:**
- Modify: `webui/src/pages/Dashboard.tsx`
- Test: `webui/src/pages/Dashboard.test.tsx`

- [ ] **Step 1: Write the failing test**

Append to `webui/src/pages/Dashboard.test.tsx`:

```ts
  it("filters to attention-only when the Need attention tile is clicked, and clears on toggle", () => {
    render(
      <Dashboard data={twoWorkspaces} onOpenWorkspace={() => {}} onOpenSession={() => {}} />,
    )

    // Both visible initially. Use queryAllByLabelText (count > 0) because each
    // workspace renders in both the desktop and mobile trees in jsdom.
    expect(screen.queryAllByLabelText("Open workspace owner/healthy").length).toBeGreaterThan(0)
    expect(screen.queryAllByLabelText("Open workspace owner/broken").length).toBeGreaterThan(0)

    const tile = screen.getByRole("button", { name: /filter: need attention/i })
    fireEvent.click(tile)

    // Healthy hidden (zero matches in either tree), failing kept.
    expect(screen.queryAllByLabelText("Open workspace owner/healthy")).toHaveLength(0)
    expect(screen.queryAllByLabelText("Open workspace owner/broken").length).toBeGreaterThan(0)
    expect(tile).toHaveAttribute("aria-pressed", "true")

    // Toggle back.
    fireEvent.click(tile)
    expect(screen.queryAllByLabelText("Open workspace owner/healthy").length).toBeGreaterThan(0)
    expect(tile).toHaveAttribute("aria-pressed", "false")
  })

  it("does not make the Need attention tile a filter when nothing needs attention", () => {
    const allHealthy: DashboardResponse = {
      docker: { status: "connected" },
      workspaces: [twoWorkspaces.workspaces[0]],
    }
    render(
      <Dashboard data={allHealthy} onOpenWorkspace={() => {}} onOpenSession={() => {}} />,
    )
    expect(screen.queryByRole("button", { name: /filter: need attention/i })).not.toBeInTheDocument()
  })
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test -- src/pages/Dashboard.test.tsx`
Expected: FAIL — the tile is not a button, no filtering happens.

- [ ] **Step 3: Implement the filter**

3a. Add `useState` is already imported. In the `Dashboard` component body, after `const [deleteTarget, …]`, add filter state and derive the visible lists. Replace the block that computes `attention` and the list rendering inputs:

```ts
  const [deleteTarget, setDeleteTarget] = useState<DeleteWorkspaceTarget | null>(null)
  const [attentionOnly, setAttentionOnly] = useState(false)
  const workspaces = data.workspaces
  const orderedWorkspaces = sortByAttentionFirst(workspaces)
  const running = workspaces.filter((w) => normalize(w.docker.status) === "running").length
  const pendingLaunches = pendingLaunchItems(launchQueue, workspaces)
  const failedLaunches = pendingLaunches.filter(
    (item) => normalize(item.status) === "failed",
  ).length
  const attention = workspaces.filter(needsAttention).length + failedLaunches
  const filterActive = attentionOnly && attention > 0
  const visibleWorkspaces = filterActive
    ? orderedWorkspaces.filter(needsAttention)
    : orderedWorkspaces
  const visiblePending = filterActive
    ? pendingLaunches.filter((item) => normalize(item.status) === "failed")
    : pendingLaunches
```

3b. Update the desktop table + mobile grid to render `visiblePending` / `visibleWorkspaces` instead of `pendingLaunches` / `orderedWorkspaces`. Four `.map` call sites — change their sources:
- `pendingLaunches.map((item) => (<PendingLaunchRow …`  → `visiblePending.map(...)`
- `orderedWorkspaces.map((workspace) => (<WorkspaceRow …` → `visibleWorkspaces.map(...)`
- `pendingLaunches.map((item) => (<PendingLaunchCard …` → `visiblePending.map(...)`
- `orderedWorkspaces.map((workspace) => (<WorkspaceCard …` → `visibleWorkspaces.map(...)`

Also update the empty-state guard so it keys off the full lists (not the filtered ones):

```tsx
      {workspaces.length === 0 && pendingLaunches.length === 0 ? (
        <EmptyState onLaunchSetups={onLaunchSetups} />
      ) : (
```

(unchanged — it already references `workspaces` and `pendingLaunches`; verify it is not switched to the `visible*` lists).

3c. Make the "Need attention" `SummaryCard` interactive. Replace the three-card summary strip block:

```tsx
      <div className="mt-5 grid gap-3 sm:grid-cols-3">
        <SummaryCard label="Workspaces" value={workspaces.length} sub="managed by SAG" />
        <SummaryCard
          icon={<Activity size={14} className="text-blue-500" />}
          label="Running"
          value={running}
          sub="active containers"
        />
        <SummaryCard
          active={filterActive}
          icon={attention ? <AlertTriangle size={14} className="text-status-failed" /> : null}
          interactive={attention > 0}
          label="Need attention"
          onClick={attention > 0 ? () => setAttentionOnly((value) => !value) : undefined}
          sub="failed, partial, or stopped"
          value={attention}
          valueTone={attention > 0 ? "text-status-failed" : undefined}
        />
      </div>
```

3d. Extend `SummaryCard` to support the interactive/active/tone props:

```tsx
function SummaryCard({
  label,
  value,
  sub,
  icon,
  onClick,
  active = false,
  interactive = false,
  valueTone,
}: {
  label: string
  value: number
  sub: string
  icon?: ReactNode
  onClick?: () => void
  active?: boolean
  interactive?: boolean
  valueTone?: string
}) {
  const clickable = interactive && Boolean(onClick)
  return (
    <Card
      aria-label={clickable ? `Filter: ${label}` : undefined}
      aria-pressed={clickable ? active : undefined}
      className={cn(
        "px-4 py-3.5",
        clickable &&
          "cursor-pointer transition-colors hover:bg-slate-50/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-status-failed-border",
        active && "bg-status-failed-soft/50 ring-1 ring-status-failed-border",
      )}
      onClick={clickable ? onClick : undefined}
      onKeyDown={
        clickable
          ? (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault()
                onClick?.()
              }
            }
          : undefined
      }
      role={clickable ? "button" : undefined}
      tabIndex={clickable ? 0 : undefined}
    >
      <div className="flex items-center justify-between gap-3">
        <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-slate-500">
          {label}
        </div>
        {icon}
      </div>
      <div className="mt-1.5 flex items-baseline gap-2">
        <span className={cn("text-[26px] font-semibold tabular-nums text-slate-900", valueTone)}>
          {value}
        </span>
        <span className="min-w-0 text-[12px] text-slate-500">{sub}</span>
      </div>
    </Card>
  )
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npm test -- src/pages/Dashboard.test.tsx`
Expected: PASS — filter tests pass; existing tests unaffected (the single-workspace fixture has `attention === 0`, so the tile stays non-interactive and the `text-amber` icon change is cosmetic).

- [ ] **Step 5: Commit**

```bash
git add webui/src/pages/Dashboard.tsx webui/src/pages/Dashboard.test.tsx
git commit -m "feat(webui): make Need attention tile a list filter affordance"
```

---

## Task 5: Teaching empty state — explicit paste-list hint + token cleanup

The empty state already explains what a workspace is and offers the launch action. Make the paste-list hint a distinct, glanceable line and align the icon color to tokens.

**Files:**
- Modify: `webui/src/pages/Dashboard.tsx`
- Test: `webui/src/pages/Dashboard.test.tsx`

- [ ] **Step 1: Write the failing test**

Append to `webui/src/pages/Dashboard.test.tsx`:

```ts
  it("shows the paste-many hint in the empty state", () => {
    render(
      <Dashboard
        data={{ docker: { status: "connected" }, workspaces: [] }}
        onLaunchSetups={() => {}}
        onOpenSession={() => {}}
        onOpenWorkspace={() => {}}
      />,
    )
    expect(screen.getByText(/paste a list of repo URLs/i)).toBeInTheDocument()
  })
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test -- src/pages/Dashboard.test.tsx`
Expected: FAIL — the hint is currently part of the paragraph prose; `getByText(/paste a list of repo URLs/i)` matches a substring inside a larger node, so it may pass already. If it PASSES here, still complete Step 3 to make the hint a standalone element (the assertion stays valid), then continue.

- [ ] **Step 3: Refine the empty state**

Replace the `EmptyState` component body:

```tsx
function EmptyState({ onLaunchSetups }: { onLaunchSetups?: () => void }) {
  return (
    <Card className="mt-5 flex flex-col items-center px-6 py-14 text-center">
      <div className="flex h-11 w-11 items-center justify-center rounded-lg border border-slate-200 bg-slate-50 text-slate-500">
        <GitBranch size={20} />
      </div>
      <h2 className="mt-4 text-[15px] font-semibold text-slate-800">No workspaces yet</h2>
      <p className="mt-1.5 max-w-[440px] text-[13px] leading-relaxed text-slate-500">
        A workspace is a SAG-managed container set up from a repository — its build,
        tests, evidence, and report in one place. Launch one to get started.
      </p>
      {onLaunchSetups ? (
        <Button className="mt-5" onClick={onLaunchSetups} type="button">
          <Rocket size={14} />
          Launch your first setup
        </Button>
      ) : null}
      <p className="mt-4 font-mono text-[11px] text-slate-500">
        Tip: paste a list of repo URLs to queue many setups at once.
      </p>
    </Card>
  )
}
```

(Note the icon color changed from `text-slate-400` to `text-slate-500` for AA per the Phase 1 contrast rule.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `npm test -- src/pages/Dashboard.test.tsx`
Expected: PASS — the existing `teaches a first-run empty state…` test still finds "No workspaces yet" + "Launch your first setup", and the new hint test passes.

- [ ] **Step 5: Commit**

```bash
git add webui/src/pages/Dashboard.tsx webui/src/pages/Dashboard.test.tsx
git commit -m "feat(webui): clarify dashboard empty-state paste-many hint"
```

---

## Task 6: Skeleton rows on first load

Replace the App-level "Loading workspaces..." card with a skeleton that mirrors the dashboard's shape, so first load doesn't flash a bare card.

**Files:**
- Create: `webui/src/pages/DashboardSkeleton.tsx`
- Modify: `webui/src/App.tsx`
- Test: `webui/src/test/App.test.tsx`

- [ ] **Step 1: Update the App test (failing first)**

In `webui/src/test/App.test.tsx`, change the assertion in the "fetches and renders dashboard data after loading" test (currently line ~96):

Replace:

```ts
    expect(screen.getByText("Loading workspaces...")).toBeInTheDocument()
```

with:

```ts
    expect(screen.getByRole("status", { name: /loading workspaces/i })).toBeInTheDocument()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test -- src/test/App.test.tsx`
Expected: FAIL — no element with role `status` named "loading workspaces" exists yet.

- [ ] **Step 3: Create the skeleton component**

`webui/src/pages/DashboardSkeleton.tsx`:

```tsx
import { Card } from "@/components/common/Card"

const ROW_KEYS = ["r1", "r2", "r3", "r4"]
const TILE_KEYS = ["t1", "t2", "t3"]

/** First-load placeholder that mirrors the dashboard's summary strip + list. */
export function DashboardSkeleton() {
  return (
    <main
      aria-label="Loading workspaces"
      className="mx-auto max-w-[1180px] px-4 py-5 sm:px-6 lg:px-8 lg:py-7"
      role="status"
    >
      <div className="h-7 w-44 animate-pulse rounded bg-slate-100" />
      <div className="mt-5 grid gap-3 sm:grid-cols-3">
        {TILE_KEYS.map((key) => (
          <div key={key} className="h-[72px] animate-pulse rounded-lg bg-slate-100" />
        ))}
      </div>
      <Card className="mt-5 overflow-hidden">
        {ROW_KEYS.map((key) => (
          <div
            key={key}
            className="flex items-center gap-3 border-b border-slate-100 px-4 py-3 last:border-b-0"
          >
            <div className="h-7 w-7 shrink-0 animate-pulse rounded-md bg-slate-100" />
            <div className="flex-1 space-y-2">
              <div className="h-3 w-1/3 animate-pulse rounded bg-slate-100" />
              <div className="h-2.5 w-1/2 animate-pulse rounded bg-slate-100" />
            </div>
          </div>
        ))}
      </Card>
      <span className="sr-only">Loading workspaces…</span>
    </main>
  )
}
```

- [ ] **Step 4: Wire it into App**

In `webui/src/App.tsx`:

4a. Add the import near the other page imports:

```ts
import { DashboardSkeleton } from "@/pages/DashboardSkeleton"
```

4b. Replace the first-load loading block:

```tsx
      {loading && !dashboard ? (
        <main className="mx-auto max-w-[1180px] px-4 py-5 sm:px-6 lg:px-8 lg:py-7">
          <Card className="inline-flex px-3 py-2 text-[13px] text-slate-500">
            Loading workspaces...
          </Card>
        </main>
      ) : null}
```

with:

```tsx
      {loading && !dashboard ? <DashboardSkeleton /> : null}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `npm test -- src/test/App.test.tsx`
Expected: PASS — the skeleton is found by role/name on first render, then data renders.

- [ ] **Step 6: Commit**

```bash
git add webui/src/pages/DashboardSkeleton.tsx webui/src/App.tsx webui/src/test/App.test.tsx
git commit -m "feat(webui): show skeleton rows on first dashboard load"
```

---

## Task 7: "Updated Ns ago" stamp + quiet poll-failure indicator

Replace the loud red "Refresh failed" banner with a calm footer: a ticking "Updated Ns ago" stamp, a quiet "couldn't refresh" marker when polling fails, and a "Show all" affordance when the attention filter is active.

**Files:**
- Modify: `webui/src/pages/Dashboard.tsx`
- Modify: `webui/src/App.tsx`
- Test: `webui/src/pages/Dashboard.test.tsx`
- Test: `webui/src/test/App.test.tsx`

- [ ] **Step 1: Write the failing Dashboard tests**

Append to `webui/src/pages/Dashboard.test.tsx`:

```ts
  it("shows an 'Updated just now' stamp when given a fresh timestamp", () => {
    render(
      <Dashboard
        data={dashboard}
        lastUpdatedAt={Date.now()}
        onOpenSession={() => {}}
        onOpenWorkspace={() => {}}
      />,
    )
    expect(screen.getByText(/updated just now/i)).toBeInTheDocument()
  })

  it("shows a quiet inline indicator when polling fails", () => {
    render(
      <Dashboard
        data={dashboard}
        lastUpdatedAt={Date.now()}
        onOpenSession={() => {}}
        onOpenWorkspace={() => {}}
        pollError="Error: refresh down"
        pollFailed
      />,
    )
    expect(screen.getByText(/couldn't refresh/i)).toBeInTheDocument()
    // The full-page "unavailable" state must NOT appear when data is present.
    expect(screen.queryByText(/dashboard unavailable/i)).not.toBeInTheDocument()
  })
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test -- src/pages/Dashboard.test.tsx`
Expected: FAIL — props not accepted; footer shows the old static text.

- [ ] **Step 3: Implement the footer in Dashboard**

3a. Add `useEffect` to the React import at the top of `Dashboard.tsx`:

```ts
import { useEffect, useState } from "react"
```

Add the `formatAgo` import:

```ts
import { formatAgo } from "@/lib/relativeTime"
```

3b. Extend `DashboardProps`:

```ts
interface DashboardProps {
  data: DashboardResponse
  onOpenWorkspace: (workspaceId: string) => void
  onOpenSession: (workspaceId: string, sessionId: string, tab?: string) => void
  onRefresh?: () => void
  refreshing?: boolean
  onLaunchSetups?: () => void
  onDeleteWorkspace?: (workspaceId: string) => Promise<void>
  launchQueue?: LaunchQueueState | null
  highlightedWorkspaces?: string[]
  lastUpdatedAt?: number | null
  pollFailed?: boolean
  pollError?: string | null
}
```

3c. Accept the new props in the component signature (add to the destructured params, with defaults):

```tsx
  launchQueue = null,
  highlightedWorkspaces = [],
  lastUpdatedAt = null,
  pollFailed = false,
  pollError = null,
}: DashboardProps) {
```

3d. Add a ticking clock inside the component body (after the `filterActive`/`visible*` derivations from Task 4):

```ts
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])
```

3e. Replace the static footer paragraph:

```tsx
      <p className="mt-3 px-1 font-mono text-[10px] text-slate-500">
        Refreshes automatically · or use Refresh
      </p>
```

with:

```tsx
      <div className="mt-3 flex flex-wrap items-center gap-x-2 gap-y-1 px-1 font-mono text-[10px] text-slate-500">
        <span>
          {lastUpdatedAt != null ? `Updated ${formatAgo(now - lastUpdatedAt)}` : "Updating…"}
        </span>
        {pollFailed ? (
          <span
            className="inline-flex items-center gap-1 text-status-attention"
            title={pollError ?? undefined}
          >
            <span aria-hidden="true" className="inline-block h-1.5 w-1.5 rounded-full bg-status-attention" />
            couldn't refresh
          </span>
        ) : null}
        {filterActive ? (
          <button
            className="underline decoration-dotted hover:text-slate-700"
            onClick={() => setAttentionOnly(false)}
            type="button"
          >
            · Showing {visibleWorkspaces.length} needing attention · Show all
          </button>
        ) : (
          <span>· refreshes automatically</span>
        )}
      </div>
```

- [ ] **Step 4: Run the Dashboard tests**

Run: `npm test -- src/pages/Dashboard.test.tsx`
Expected: PASS — stamp + poll-failure tests pass; existing tests unaffected.

- [ ] **Step 5: Update the App refresh-failure test (failing first)**

In `webui/src/test/App.test.tsx`, the test "keeps stale dashboard data visible when refresh fails" (currently lines ~110–132). Replace its two failure-banner assertions:

```ts
    expect(await screen.findByText("Refresh failed")).toBeInTheDocument()
    expect(screen.getByText("Error: refresh down")).toBeInTheDocument()
```

with:

```ts
    expect(await screen.findByText(/couldn't refresh/i)).toBeInTheDocument()
```

(Keep the remaining assertions: data still visible, "Dashboard unavailable" absent.)

- [ ] **Step 6: Run test to verify it fails**

Run: `npm test -- src/test/App.test.tsx`
Expected: FAIL — App does not yet render "couldn't refresh" (it still renders the red "Refresh failed" banner) and does not pass `pollFailed`/`lastUpdatedAt`.

- [ ] **Step 7: Wire App**

In `webui/src/App.tsx`:

7a. Add the timestamp state (near the other `useState` declarations):

```ts
  const [lastUpdatedAt, setLastUpdatedAt] = useState<number | null>(null)
```

7b. Stamp it on a successful dashboard load. In `loadDashboard`, after `setDashboard(nextDashboard)`:

```ts
      const nextDashboard = await fetchDashboard()
      setDashboard(nextDashboard)
      setLastUpdatedAt(Date.now())
```

7c. Remove the loud refresh-failed banner block entirely:

```tsx
      {dashboard && dashboardError ? (
        <div className="mx-auto max-w-[1180px] px-4 pt-5 sm:px-6 lg:px-8">
          <Card className="flex flex-col gap-3 border-red-100 bg-red-50/50 px-4 py-3 text-[13px] sm:flex-row sm:items-center sm:justify-between">
            <div>
              <div className="font-semibold text-red-700">Refresh failed</div>
              <div className="mt-0.5 font-mono text-[12px] text-red-600">{dashboardError}</div>
            </div>
            <Button onClick={() => void loadDashboard()} type="button" variant="outline">
              Retry
            </Button>
          </Card>
        </div>
      ) : null}
```

(Delete the whole block. The full-page "Dashboard unavailable" state for the no-data case stays.)

7d. Pass the new props to `Dashboard`:

```tsx
      {dashboard && route.view === "dashboard" ? (
        <Dashboard
          data={dashboard}
          highlightedWorkspaces={highlightedWorkspaces}
          lastUpdatedAt={lastUpdatedAt}
          launchQueue={launchQueue}
          onDeleteWorkspace={deleteWorkspace}
          onLaunchSetups={() => setLaunchDialogOpen(true)}
          onOpenSession={openSession}
          onOpenWorkspace={openWorkspace}
          onRefresh={() => void loadDashboard()}
          pollError={dashboardError}
          pollFailed={Boolean(dashboardError)}
          refreshing={loading}
        />
      ) : null}
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `npm test -- src/test/App.test.tsx src/pages/Dashboard.test.tsx`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add webui/src/pages/Dashboard.tsx webui/src/pages/Dashboard.test.tsx webui/src/App.tsx webui/src/test/App.test.tsx
git commit -m "feat(webui): quiet updated/poll-failure footer; drop loud refresh banner"
```

---

## Task 8: Full verification

**Files:** none (verification only).

- [ ] **Step 1: Run the entire frontend test suite**

Run: `npm test`
Expected: PASS — all suites green (no regressions in `App`, `Dashboard`, `ModuleTable`, `status`, etc.).

- [ ] **Step 2: Type-check**

Run: `npx tsc -p tsconfig.app.json --noEmit`
Expected: no errors.

- [ ] **Step 3: Visual smoke check (manual, do NOT commit the bundle)**

Build/serve only to eyeball; **do not** `git add` anything under `src/sag/web/static/`:
- `sag ui --demo` and confirm: attention rows sort to the top with the failed tint; healthy rows are quiet; the "Need attention" tile toggles the filter and shows pressed state; the empty state (zero workspaces) teaches + shows the paste hint; the footer reads "Updated just now" and ticks; a simulated poll failure shows the quiet "couldn't refresh" marker rather than a red banner.
- Confirm AA contrast on the footer indicator and tinted rows (Phase 1 tokens already meet AA).

- [ ] **Step 4: Confirm the working tree has no staged `static/` changes**

Run: `git status --short src/sag/web/static`
Expected: changes here, if any, remain unstaged (they are a side effect of the local build and are served from the `ui` branch).

---

## Self-Review

**Spec coverage (Dashboard section of the design):**
- Summary strip (Workspaces · Running · Need-attention) with need-attention as a **filter affordance** → Task 4.
- Comfortable list rows with **attention-first ordering** + status-token tint, healthy rows quiet → Task 3.
- **Teaching empty state** (what a workspace is + Launch action + paste-list hint) → Task 5.
- **Skeleton rows** on first load → Task 6.
- **"Updated Ns ago" stamp** → Task 7.
- **Quiet inline indicator when polling fails** → Task 7 (replaces the loud banner).
- Phase 1 semantic tokens for tints (`bg-status-failed-soft`, `text-status-failed`, `text/bg-status-attention`, `ring-status-failed-border`) → Tasks 3, 4, 7.
- Presentation-layer only, reuse existing primitives, tests green, `tsc` clean, do not commit `static/` → Tasks 1–8 + project policy header.

**Type consistency:**
- `needsAttention` / `sortByAttentionFirst` defined in Task 2, imported in Task 3 — names match.
- `formatAgo(elapsedMs)` defined in Task 1, called in Task 7 — signature matches.
- `SummaryCard` props (`onClick`, `active`, `interactive`, `valueTone`) defined and used consistently in Task 4.
- `Dashboard` new props (`lastUpdatedAt`, `pollFailed`, `pollError`) declared in the interface (Task 7) and supplied by App (Task 7) — names match; all optional, so existing `Dashboard` test call sites stay valid.

**Placeholder scan:** none — every code step contains complete content.

**Risk notes:**
- The attention tint uses `bg-status-failed-soft/50`; the recent-launch highlight (`bg-blue-50/60`) is applied after it in the `cn(...)` list so it wins for freshly launched rows (preserves the existing highlight test).
- The filter guards on `attention > 0`, so it auto-relaxes if a failure resolves between polls; `attentionOnly` state is intentionally left set (re-applies if a new failure appears) — acceptable and documented.
