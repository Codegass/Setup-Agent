# Phase 3 — Detail-Pane Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the separate Workspace (tabs) and Session (tabs) pages with one **Detail Pane**: a sticky header + session switcher, an always-visible Summary Band (Outcome · signal tiles · Why), and a two-pane body (left section-nav + right single continuous scroll of facet sections with scroll-spy) — porting `docs/Setup Agent Web UI/workbench/detail.jsx`.

**Architecture:** Presentation-layer only. The detail pane is a **shell**: each facet section reuses the existing renderer verbatim (`BuildDetailPage`, `TestDetailPage`, `ContextTrace`, `EvidenceTimeline`, `FilesDigest`, `ReportDoc`, `LogsView`) — **restyling those bodies is Phase 4/5, not this plan**. New code is the shell only (route merge, scroll-spy, summary band, header, section-nav, composition). The app stays green at every task because the new `DetailPane` is built and unit-tested in isolation first, and `App.tsx`'s route is flipped to it in the **last** implementation task, after which the old pages are retired.

**Tech Stack:** React + TypeScript (Vite), Tailwind v4 (Phase 1 `--status-*` tokens), Vitest + Testing Library, lucide-react icons.

**Branch:** `feature/webui-workbench-redesign` (Phase 1 + 2 merged to main; this branch is even with main). Continue on it.

**Project policy (do not violate):**
- Do **not** run `npm run build`; do **not** create/modify/`git add` anything under `src/sag/web/static/`.
- `docs/` is gitignored — force-add docs: `git add -f docs/...`. The `webui/src/lib/` and `webui/src/pages/` dirs are also covered by a bare `lib/`/ignore rule in `.gitignore`; if a `git add` of a new file there reports "ignored", re-add that **exact** path with `git add -f` (mirrors how `webui/src/lib/relativeTime.ts` was committed in Phase 2). Never `git add -A`/`git add .`.
- No `Co-Authored-By` trailer on commits.

**Per-task verification:**
- Targeted tests: `cd webui && npm test -- <test path>`
- Type check (end, and whenever types change): `cd webui && npx tsc -p tsconfig.app.json --noEmit`

---

## Reference & Data Mapping

Port target: `docs/Setup Agent Web UI/workbench/detail.jsx` (DetailHeader, SummaryBand, FacetSection, DetailPane, scroll-spy in `onScroll`/`jump`). Facet bodies in the prototype's `workbench/sections.jsx` are **superseded by our existing components** — do not port those bodies.

Selected-session `ExecutionSessionDetail` → facet body:

| Facet | Section body (reuse existing) | Count badge |
|---|---|---|
| Build | `<BuildDetailPage detail={d} />` (embeddable; see Task 1) | — |
| Test | `<TestDetailPage detail={d} />` (embeddable; see Task 1) | `d.test.fail` (red) when > 0 |
| Flow | `d.context ? <ContextTrace ctx={d.context} /> : <Empty/>` | — |
| Evidence | `<EvidenceTimeline groups={d.evidence} />` | `d.evidence.length` |
| Files | `<FilesDigest digest={d.files} />` | `d.files?.items.length` |
| Report | `<ReportDoc doc={d.reportDoc} />` | — |
| Logs | `<LogsView logs={d.logs} />` | — |

Summary Band data: Outcome `d.outcome`; tiles Build `d.build`, Tests `d.test`, Evidence `d.evidenceStatus`, Report `d.report`; Why `d.blocker` (currently always `null` in the read model — the Why block must render **nothing** when absent, and is dormant until a backend follow-up populates it).

Header actions: **New task** (reuse the extracted `NewTaskModal`), **Terminal** + **Settings** (open a slide-over reusing `TerminalPanel` and the extracted `WorkspaceSettings`), **Delete** (reuse `DeleteWorkspaceDialog`).

---

## File Structure

**Create:**
- `webui/src/pages/detail/scrollSpy.ts` — pure `pickActiveSection()` + `useScrollSpy()` hook.
- `webui/src/pages/detail/scrollSpy.test.ts`
- `webui/src/pages/detail/facets.tsx` — `buildDetailFacets(detail)` metadata + `FacetBody` renderer + `Empty`.
- `webui/src/pages/detail/facets.test.tsx`
- `webui/src/pages/detail/SummaryBand.tsx` + `SummaryBand.test.tsx`
- `webui/src/pages/detail/SectionNav.tsx` + `SectionNav.test.tsx`
- `webui/src/pages/detail/DetailHeader.tsx` + `DetailHeader.test.tsx`
- `webui/src/pages/detail/DetailPane.tsx` + `DetailPane.test.tsx`
- `webui/src/components/workspace/NewTaskModal.tsx` (extracted from `Workspace.tsx`)
- `webui/src/components/workspace/WorkspaceSettings.tsx` (extracted from `Workspace.tsx`'s `SettingsTab`)
- `webui/src/components/workspace/WorkspacePanels.tsx` — the Terminal/Settings slide-over wrapper.

**Modify:**
- `webui/src/components/session/BuildDetailPage.tsx`, `TestDetailPage.tsx` — make `onBack` optional; render the top Back/StatusBadge row only when `onBack` is provided (so they embed cleanly as facet bodies).
- `webui/src/App.tsx` — collapse the route union to `dashboard | detail`; fetch the selected session; render `DetailPane`; update the breadcrumb.
- `webui/src/test/App.test.tsx` — update routing assertions to the merged detail view.

**Delete (last task):**
- `webui/src/pages/Workspace.tsx`, `webui/src/pages/Workspace.test.tsx`
- `webui/src/pages/SessionDetail.tsx`, `webui/src/pages/SessionDetail.test.tsx`

---

## Task 1: Make Build/Test detail pages embeddable

So the Build/Test facets can reuse the full Features A/B detail content without a stray "Back" button.

**Files:**
- Modify: `webui/src/components/session/BuildDetailPage.tsx`, `webui/src/components/session/TestDetailPage.tsx`
- Test: `webui/src/components/session/BuildDetailPage.test.tsx`, `webui/src/components/session/TestDetailPage.test.tsx`

- [ ] **Step 1: Write the failing test**

Append to `webui/src/components/session/BuildDetailPage.test.tsx` (inside its top-level `describe`):

```tsx
  it("omits the back button when onBack is not provided (embedded mode)", () => {
    render(<BuildDetailPage detail={detail} />)
    expect(screen.queryByRole("button", { name: /back/i })).not.toBeInTheDocument()
  })
```

Append the analogous test to `webui/src/components/session/TestDetailPage.test.tsx`:

```tsx
  it("omits the back button when onBack is not provided (embedded mode)", () => {
    render(<TestDetailPage detail={detail} />)
    expect(screen.queryByRole("button", { name: /back/i })).not.toBeInTheDocument()
  })
```

> If the existing test file's fixture variable is not named `detail`, reuse whatever fixture the existing tests in that file already construct.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd webui && npm test -- src/components/session/BuildDetailPage.test.tsx src/components/session/TestDetailPage.test.tsx`
Expected: FAIL — `onBack` is currently required (TS) and/or the Back button always renders.

- [ ] **Step 3: Make `onBack` optional in both pages**

In `webui/src/components/session/BuildDetailPage.tsx`, change the signature and guard the top row. The component currently destructures `{ detail, onBack }` and renders a row:

```tsx
      <div className="flex items-center justify-between">
        <Button onClick={onBack} size="sm" type="button" variant="ghost">
          <ArrowLeft size={14} /> Back
        </Button>
        <StatusBadge status={...} />
      </div>
```

Change the prop type to `onBack?: () => void` and render that row only when `onBack` is truthy:

```tsx
      {onBack ? (
        <div className="flex items-center justify-between">
          <Button onClick={onBack} size="sm" type="button" variant="ghost">
            <ArrowLeft size={14} /> Back
          </Button>
          <StatusBadge status={detail.build.state} />
        </div>
      ) : null}
```

Apply the identical change in `webui/src/components/session/TestDetailPage.tsx` (its row uses `<StatusBadge status={t.state} />`):

```tsx
      {onBack ? (
        <div className="flex items-center justify-between">
          <Button onClick={onBack} size="sm" type="button" variant="ghost">
            <ArrowLeft size={14} /> Back
          </Button>
          <StatusBadge status={t.state} />
        </div>
      ) : null}
```

If `ArrowLeft`/`Button`/`StatusBadge` become unused when `onBack` is absent, they are still used in the guarded branch — keep the imports.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd webui && npm test -- src/components/session/BuildDetailPage.test.tsx src/components/session/TestDetailPage.test.tsx`
Expected: PASS (existing tests that pass `onBack` still pass; new embedded-mode tests pass).

- [ ] **Step 5: Commit**

```bash
git add webui/src/components/session/BuildDetailPage.tsx webui/src/components/session/TestDetailPage.tsx webui/src/components/session/BuildDetailPage.test.tsx webui/src/components/session/TestDetailPage.test.tsx
git commit -m "feat(webui): make Build/Test detail pages embeddable (optional onBack)"
```

---

## Task 2: Scroll-spy helper (pure `pickActiveSection` + hook)

The active-section math is pure and unit-tested; the hook wires it to real DOM offsets.

**Files:**
- Create: `webui/src/pages/detail/scrollSpy.ts`
- Test: `webui/src/pages/detail/scrollSpy.test.ts`

- [ ] **Step 1: Write the failing test**

`webui/src/pages/detail/scrollSpy.test.ts`:

```ts
import { describe, expect, it } from "vitest"

import { pickActiveSection } from "./scrollSpy"

const positions = [
  { id: "build", top: 0 },
  { id: "test", top: 400 },
  { id: "flow", top: 900 },
]

describe("pickActiveSection", () => {
  it("returns the first section at the top", () => {
    expect(pickActiveSection(positions, 0, 170)).toBe("build")
  })

  it("advances as scroll passes each section's top (minus offset)", () => {
    expect(pickActiveSection(positions, 250, 170)).toBe("test") // 400-170=230 <= 250
    expect(pickActiveSection(positions, 720, 170)).toBe("flow") // 900-170=730 > 720 -> still test
    expect(pickActiveSection(positions, 740, 170)).toBe("flow")
  })

  it("never returns past the last section and tolerates an empty list", () => {
    expect(pickActiveSection(positions, 99999, 170)).toBe("flow")
    expect(pickActiveSection([], 100, 170)).toBeNull()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd webui && npm test -- src/pages/detail/scrollSpy.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

`webui/src/pages/detail/scrollSpy.ts`:

```ts
import { useEffect, useRef, useState } from "react"

export interface SectionPosition {
  id: string
  top: number
}

/** Pick the section whose top (minus a sticky-header offset) is the last one at/above the scroll position. */
export function pickActiveSection(
  positions: SectionPosition[],
  scrollTop: number,
  offset: number,
): string | null {
  if (positions.length === 0) {
    return null
  }
  let active = positions[0].id
  for (const pos of positions) {
    if (pos.top - offset <= scrollTop) {
      active = pos.id
    }
  }
  return active
}

/**
 * Tracks which facet section is in view as the container scrolls.
 * Returns the active id plus a `jump(id)` that smooth-scrolls a section into view.
 * `offset` accounts for the sticky header + nav height.
 */
export function useScrollSpy(ids: string[], offset = 170) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const [active, setActive] = useState<string | null>(ids[0] ?? null)

  // Reset to the first section when the set of ids changes (e.g. session switch).
  useEffect(() => {
    setActive(ids[0] ?? null)
    if (containerRef.current) {
      containerRef.current.scrollTop = 0
    }
  }, [ids.join("|")])

  function recompute() {
    const container = containerRef.current
    if (!container) {
      return
    }
    const positions: SectionPosition[] = ids.map((id) => {
      const el = document.getElementById(`facet-${id}`)
      return { id, top: el ? el.offsetTop : 0 }
    })
    setActive(pickActiveSection(positions, container.scrollTop, offset))
  }

  function jump(id: string) {
    setActive(id)
    const el = document.getElementById(`facet-${id}`)
    el?.scrollIntoView?.({ behavior: "smooth", block: "start" })
  }

  return { containerRef, active, onScroll: recompute, jump }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd webui && npm test -- src/pages/detail/scrollSpy.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webui/src/pages/detail/scrollSpy.ts webui/src/pages/detail/scrollSpy.test.ts
git commit -m "feat(webui): scroll-spy helper for the detail pane"
```

---

## Task 3: Facet metadata + body renderer

`buildDetailFacets` returns the testable nav metadata; `FacetBody` maps an id to its existing renderer.

**Files:**
- Create: `webui/src/pages/detail/facets.tsx`
- Test: `webui/src/pages/detail/facets.test.tsx`

- [ ] **Step 1: Write the failing test**

`webui/src/pages/detail/facets.test.tsx`:

```tsx
import { describe, expect, it } from "vitest"

import type { ExecutionSessionDetail } from "@/api/types"

import { buildDetailFacets } from "./facets"

function detail(overrides: Partial<ExecutionSessionDetail> = {}): ExecutionSessionDetail {
  return {
    id: "CC-1",
    workspace: "sag-x",
    title: "Build and test",
    status: "completed",
    entry: "SAG",
    start: "now",
    duration: "1m",
    outcome: "Done.",
    build: { state: "success", tool: "Maven", time: "1s", note: "" },
    test: { state: "pass", pass: 10, fail: 0, skip: 0, total: 10 },
    report: "ready",
    evidence: [],
    logs: [],
    ...overrides,
  }
}

describe("buildDetailFacets", () => {
  it("returns the seven facets in order", () => {
    const ids = buildDetailFacets(detail()).map((f) => f.id)
    expect(ids).toEqual(["build", "test", "flow", "evidence", "files", "report", "logs"])
  })

  it("surfaces a red test-fail count and evidence/files counts, omitting zero counts", () => {
    const facets = buildDetailFacets(
      detail({
        test: { state: "partial", pass: 8, fail: 2, skip: 0, total: 10 },
        evidence: [{ source: "maven", summary: "", counts: "", status: "pass", records: [] }],
        files: { counts: {}, items: [{ path: "a.java", change: "modified", size: "", note: "" }] },
      }),
    )
    const byId = Object.fromEntries(facets.map((f) => [f.id, f]))
    expect(byId.test.count).toBe(2)
    expect(byId.test.countTone).toBe("red")
    expect(byId.evidence.count).toBe(1)
    expect(byId.files.count).toBe(1)
    expect(byId.build.count).toBeNull()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd webui && npm test -- src/pages/detail/facets.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

`webui/src/pages/detail/facets.tsx`:

```tsx
import { Activity, Box, FileText, Layers, Sparkles, Terminal } from "lucide-react"
import type { LucideIcon } from "lucide-react"

import type { ExecutionSessionDetail, Tone } from "@/api/types"
import { BuildDetailPage } from "@/components/session/BuildDetailPage"
import { ContextTrace } from "@/components/session/ContextTrace"
import { EvidenceTimeline } from "@/components/session/EvidenceTimeline"
import { FilesDigest } from "@/components/session/FilesDigest"
import { LogsView } from "@/components/session/LogsView"
import { ReportDoc } from "@/components/session/ReportDoc"
import { TestDetailPage } from "@/components/session/TestDetailPage"

export type FacetId = "build" | "test" | "flow" | "evidence" | "files" | "report" | "logs"

export interface FacetMeta {
  id: FacetId
  label: string
  icon: LucideIcon
  count: number | null
  countTone: Tone
}

function nonZero(n: number | null | undefined): number | null {
  return typeof n === "number" && n > 0 ? n : null
}

/** Nav/section metadata for the detail pane (order matters; bodies render via <FacetBody>). */
export function buildDetailFacets(d: ExecutionSessionDetail): FacetMeta[] {
  return [
    { id: "build", label: "Build", icon: Box, count: null, countTone: "neutral" },
    { id: "test", label: "Test", icon: Activity, count: nonZero(d.test.fail), countTone: "red" },
    { id: "flow", label: "Flow", icon: Layers, count: null, countTone: "neutral" },
    { id: "evidence", label: "Evidence", icon: Sparkles, count: nonZero(d.evidence.length), countTone: "neutral" },
    { id: "files", label: "Files", icon: FileText, count: nonZero(d.files?.items.length), countTone: "neutral" },
    { id: "report", label: "Report", icon: FileText, count: null, countTone: "neutral" },
    { id: "logs", label: "Logs", icon: Terminal, count: null, countTone: "neutral" },
  ]
}

export function Empty({ label }: { label: string }) {
  return (
    <div className="rounded-lg border border-dashed border-slate-200 px-4 py-8 text-center text-[12.5px] text-slate-500">
      {label}
    </div>
  )
}

/** Renders a facet body by reusing the existing session renderers (no restyle — Phase 4/5). */
export function FacetBody({ id, detail }: { id: FacetId; detail: ExecutionSessionDetail }) {
  switch (id) {
    case "build":
      return <BuildDetailPage detail={detail} />
    case "test":
      return <TestDetailPage detail={detail} />
    case "flow":
      return detail.context ? (
        <ContextTrace ctx={detail.context} />
      ) : (
        <Empty label="Context trace unavailable for this session." />
      )
    case "evidence":
      return <EvidenceTimeline groups={detail.evidence} />
    case "files":
      return <FilesDigest digest={detail.files} />
    case "report":
      return <ReportDoc doc={detail.reportDoc} />
    case "logs":
      return <LogsView logs={detail.logs} />
  }
}
```

> If `Tone` is not exported from `@/api/types`, import it from where `Badge` imports it (`@/api/types` per `Badge.tsx`); it is already used there.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd webui && npm test -- src/pages/detail/facets.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webui/src/pages/detail/facets.tsx webui/src/pages/detail/facets.test.tsx
git commit -m "feat(webui): detail-pane facet metadata + body renderer"
```

---

## Task 4: Summary Band

The always-visible Outcome callout + signal tiles + Why callout, ported with Phase 1 tokens.

**Files:**
- Create: `webui/src/pages/detail/SummaryBand.tsx`
- Test: `webui/src/pages/detail/SummaryBand.test.tsx`

- [ ] **Step 1: Write the failing test**

`webui/src/pages/detail/SummaryBand.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import type { ExecutionSessionDetail } from "@/api/types"

import { SummaryBand } from "./SummaryBand"

function detail(overrides: Partial<ExecutionSessionDetail> = {}): ExecutionSessionDetail {
  return {
    id: "CC-1",
    workspace: "sag-x",
    title: "t",
    status: "completed",
    entry: "SAG",
    start: "now",
    duration: "1m",
    outcome: "Setup completed and the report is ready.",
    build: { state: "success", tool: "Maven", time: "47s", note: "" },
    test: { state: "pass", pass: 10, fail: 0, skip: 0, total: 10 },
    report: "ready",
    evidence: [],
    logs: [],
    ...overrides,
  }
}

describe("SummaryBand", () => {
  it("renders the outcome and the four signal tiles", () => {
    render(<SummaryBand detail={detail()} />)
    expect(screen.getByText("Setup completed and the report is ready.")).toBeInTheDocument()
    expect(screen.getByText("Build")).toBeInTheDocument()
    expect(screen.getByText("Tests")).toBeInTheDocument()
    expect(screen.getByText("Evidence")).toBeInTheDocument()
    expect(screen.getByText("Report")).toBeInTheDocument()
  })

  it("renders the Why callout only when a blocker is present", () => {
    const { rerender } = render(<SummaryBand detail={detail()} />)
    expect(screen.queryByText(/^Why ·/)).not.toBeInTheDocument()

    rerender(
      <SummaryBand
        detail={detail({
          blocker: { code: "E_BUILD", title: "Build failed", detail: "javac error", hint: "fix imports" },
        })}
      />,
    )
    expect(screen.getByText(/Why · Build failed/)).toBeInTheDocument()
    expect(screen.getByText(/fix imports/)).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd webui && npm test -- src/pages/detail/SummaryBand.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

`webui/src/pages/detail/SummaryBand.tsx`:

```tsx
import { AlertTriangle, Check, Clock, Shield, Sparkles, X } from "lucide-react"
import type * as React from "react"

import type { ExecutionSessionDetail } from "@/api/types"
import { Badge, StatusBadge } from "@/components/common/Badge"
import { isUsefulEvidenceStatus, statusMeta } from "@/components/common/status"
import { TestBar } from "@/components/common/TestBar"
import { cn } from "@/lib/utils"

function MonoLabel({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div className={cn("font-mono text-[10px] uppercase tracking-[0.12em] text-slate-500", className)}>
      {children}
    </div>
  )
}

function Tile({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white px-3.5 py-3">
      <MonoLabel>{label}</MonoLabel>
      <div className="mt-1.5">{children}</div>
    </div>
  )
}

export function SummaryBand({ detail }: { detail: ExecutionSessionDetail }) {
  const buildNorm = detail.build.state.trim().toLowerCase()
  const testNorm = detail.test.state.trim().toLowerCase()
  const testTotal = Math.max(detail.test.total, detail.test.pass + detail.test.fail)
  const reportReady = detail.report?.trim().toLowerCase() === "ready"
  const blocker = detail.blocker
  const buildFailed = buildNorm === "failure" || buildNorm === "failed"
  const outcomeBad = Boolean(blocker) && buildFailed

  const callout = outcomeBad
    ? "border-status-failed-border bg-status-failed-soft/60"
    : blocker
      ? "border-status-attention-border bg-status-attention-soft/50"
      : "border-status-success-border bg-status-success-soft/50"
  const iconWrap = outcomeBad
    ? "bg-status-failed-soft text-status-failed"
    : blocker
      ? "bg-status-attention-soft text-status-attention"
      : "bg-status-success-soft text-status-success"
  const outcomeLabelTone = outcomeBad
    ? "text-status-failed"
    : blocker
      ? "text-status-attention"
      : "text-status-success"

  return (
    <div className="space-y-3">
      {/* Outcome */}
      <div className={cn("flex items-start gap-3 rounded-xl border px-4 py-3.5", callout)}>
        <div className={cn("mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full", iconWrap)}>
          {outcomeBad ? <X size={15} /> : blocker ? <AlertTriangle size={14} /> : <Check size={15} />}
        </div>
        <div className="min-w-0">
          <MonoLabel className={outcomeLabelTone}>Outcome</MonoLabel>
          <p className="mt-0.5 text-[14px] leading-snug text-slate-800" style={{ textWrap: "pretty" }}>
            {detail.outcome}
          </p>
        </div>
      </div>

      {/* Signal tiles */}
      <div className="grid grid-cols-2 gap-2.5 lg:grid-cols-4">
        <Tile label="Build">
          <div className="flex items-center gap-1.5">
            {buildNorm === "success" ? (
              <Check size={15} className="text-status-success" />
            ) : buildFailed ? (
              <X size={15} className="text-status-failed" />
            ) : (
              <Clock size={13} className="text-slate-400" />
            )}
            <span className="text-[13px] font-medium text-slate-700">{statusMeta(detail.build.state).label}</span>
          </div>
          <div className="mt-1 font-mono text-[10px] text-slate-500">
            {detail.build.time ? `${detail.build.tool} · ${detail.build.time}` : detail.build.note}
          </div>
        </Tile>
        <Tile label="Tests">
          {testNorm === "none" || testTotal <= 0 ? (
            <span className="text-[13px] text-slate-500">Not run</span>
          ) : (
            <TestBar fail={detail.test.fail} pass={detail.test.pass} total={testTotal} />
          )}
        </Tile>
        <Tile label="Evidence">
          {isUsefulEvidenceStatus(detail.evidenceStatus) ? (
            <StatusBadge status={detail.evidenceStatus ?? "unknown"} />
          ) : (
            <span className="text-[13px] text-slate-500">—</span>
          )}
        </Tile>
        <Tile label="Report">
          {reportReady ? <Badge tone="green">Ready</Badge> : <span className="text-[13px] text-slate-500">—</span>}
        </Tile>
      </div>

      {/* Why — the blocker, surfaced up front (dormant: read model sets blocker=null today) */}
      {blocker ? (
        <div
          className={cn(
            "rounded-xl border px-4 py-3.5",
            outcomeBad
              ? "border-status-failed-border bg-status-failed-soft/50"
              : "border-status-attention-border bg-status-attention-soft/40",
          )}
        >
          <div className="flex items-center gap-2">
            <Shield size={14} className={outcomeBad ? "text-status-failed" : "text-status-attention"} />
            <MonoLabel className={outcomeBad ? "text-status-failed" : "text-status-attention"}>
              Why · {blocker.title}
            </MonoLabel>
            <Badge className="ml-auto" mono tone={outcomeBad ? "red" : "amber"}>
              {blocker.code}
            </Badge>
          </div>
          <p className="mt-2 text-[13px] leading-relaxed text-slate-700">{blocker.detail}</p>
          <div className="mt-2 flex items-start gap-1.5 text-[12.5px] text-slate-500">
            <Sparkles size={13} className="mt-0.5 shrink-0 text-slate-400" />
            <span>
              <b className="font-medium text-slate-600">Suggested fix —</b> {blocker.hint}
            </span>
          </div>
        </div>
      ) : null}
    </div>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd webui && npm test -- src/pages/detail/SummaryBand.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webui/src/pages/detail/SummaryBand.tsx webui/src/pages/detail/SummaryBand.test.tsx
git commit -m "feat(webui): detail-pane Summary Band (outcome + tiles + why)"
```

---

## Task 5: Section nav (left rail)

The sticky left nav: facet list with active highlight and click-to-jump.

**Files:**
- Create: `webui/src/pages/detail/SectionNav.tsx`
- Test: `webui/src/pages/detail/SectionNav.test.tsx`

- [ ] **Step 1: Write the failing test**

`webui/src/pages/detail/SectionNav.test.tsx`:

```tsx
import { fireEvent, render, screen } from "@testing-library/react"
import { Box } from "lucide-react"
import { describe, expect, it, vi } from "vitest"

import type { FacetMeta } from "./facets"
import { SectionNav } from "./SectionNav"

const facets: FacetMeta[] = [
  { id: "build", label: "Build", icon: Box, count: null, countTone: "neutral" },
  { id: "test", label: "Test", icon: Box, count: 2, countTone: "red" },
]

describe("SectionNav", () => {
  it("renders each facet and marks the active one", () => {
    render(<SectionNav facets={facets} active="test" onJump={() => {}} />)
    const test = screen.getByRole("button", { name: /^Test/ })
    expect(test).toHaveAttribute("aria-current", "true")
    expect(screen.getByRole("button", { name: /^Build/ })).toHaveAttribute("aria-current", "false")
  })

  it("calls onJump with the facet id when clicked", () => {
    const onJump = vi.fn()
    render(<SectionNav facets={facets} active="build" onJump={onJump} />)
    fireEvent.click(screen.getByRole("button", { name: /^Test/ }))
    expect(onJump).toHaveBeenCalledWith("test")
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd webui && npm test -- src/pages/detail/SectionNav.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

`webui/src/pages/detail/SectionNav.tsx`:

```tsx
import { Badge } from "@/components/common/Badge"
import { cn } from "@/lib/utils"

import type { FacetId, FacetMeta } from "./facets"

export function SectionNav({
  facets,
  active,
  onJump,
}: {
  facets: FacetMeta[]
  active: string | null
  onJump: (id: FacetId) => void
}) {
  return (
    <nav className="flex flex-col gap-0.5" aria-label="Detail sections">
      {facets.map((f) => {
        const on = active === f.id
        return (
          <button
            key={f.id}
            aria-current={on}
            className={cn(
              "group flex items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-[12.5px] transition-colors",
              on ? "bg-status-running-soft font-medium text-status-running" : "text-slate-500 hover:bg-slate-100 hover:text-slate-700",
            )}
            onClick={() => onJump(f.id)}
            type="button"
          >
            <f.icon className={on ? "text-status-running" : "text-slate-400"} size={14} />
            <span className="flex-1">{f.label}</span>
            {f.count != null ? <Badge tone={f.countTone}>{f.count}</Badge> : null}
          </button>
        )
      })}
    </nav>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd webui && npm test -- src/pages/detail/SectionNav.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webui/src/pages/detail/SectionNav.tsx webui/src/pages/detail/SectionNav.test.tsx
git commit -m "feat(webui): detail-pane left section-nav"
```

---

## Task 6: Detail header + session switcher

Sticky header with project/container/status, action cluster, and session chips.

**Files:**
- Create: `webui/src/pages/detail/DetailHeader.tsx`
- Test: `webui/src/pages/detail/DetailHeader.test.tsx`

- [ ] **Step 1: Write the failing test**

`webui/src/pages/detail/DetailHeader.test.tsx`:

```tsx
import { fireEvent, render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import type { WorkspaceSummary } from "@/api/types"

import { DetailHeader } from "./DetailHeader"

const workspace: WorkspaceSummary = {
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
  release: "1.4.0",
  sessions: [
    { id: "CC-1", workspace: "sag-x", title: "first", status: "completed", entry: "SAG", start: "", duration: "", build: "success", test: { state: "pass", pass: 1, fail: 0, skip: 0, total: 1 }, report: "ready", files: 0, evidence: 0 },
    { id: "CC-2", workspace: "sag-x", title: "second", status: "running", entry: "SAG", start: "", duration: "", build: "pending", test: { state: "none", pass: 0, fail: 0, skip: 0, total: 0 }, report: "none", files: 0, evidence: 0 },
  ],
}

const actions = { onNewTask: () => {}, onTerminal: () => {}, onSettings: () => {}, onDelete: () => {} }

describe("DetailHeader", () => {
  it("renders project, container, status, and the session chips", () => {
    render(<DetailHeader workspace={workspace} sessionId="CC-1" onSession={() => {}} {...actions} />)
    expect(screen.getByRole("heading", { name: "owner/x" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /CC-1/ })).toHaveAttribute("aria-current", "true")
    expect(screen.getByRole("button", { name: /CC-2/ })).toBeInTheDocument()
  })

  it("switches session when a chip is clicked", () => {
    const onSession = vi.fn()
    render(<DetailHeader workspace={workspace} sessionId="CC-1" onSession={onSession} {...actions} />)
    fireEvent.click(screen.getByRole("button", { name: /CC-2/ }))
    expect(onSession).toHaveBeenCalledWith("CC-2")
  })

  it("hides the session switcher when there is a single session", () => {
    render(
      <DetailHeader
        workspace={{ ...workspace, sessions: [workspace.sessions![0]] }}
        sessionId="CC-1"
        onSession={() => {}}
        {...actions}
      />,
    )
    expect(screen.queryByText("Sessions")).not.toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd webui && npm test -- src/pages/detail/DetailHeader.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

`webui/src/pages/detail/DetailHeader.tsx`:

```tsx
import { GitBranch, Plus, Settings as SettingsIcon, Terminal, Trash2 } from "lucide-react"
import type { LucideIcon } from "lucide-react"

import type { WorkspaceSummary } from "@/api/types"
import { Badge, StatusBadge } from "@/components/common/Badge"
import { statusMeta } from "@/components/common/status"
import { cn } from "@/lib/utils"

function HeaderButton({
  icon: Icon,
  label,
  onClick,
  primary,
  danger,
  title,
}: {
  icon: LucideIcon
  label?: string
  onClick?: () => void
  primary?: boolean
  danger?: boolean
  title?: string
}) {
  if (primary) {
    return (
      <button
        className="inline-flex items-center gap-1.5 rounded-md bg-slate-900 px-2.5 py-1.5 text-[12px] font-medium text-white hover:bg-slate-800"
        onClick={onClick}
        type="button"
      >
        <Icon size={14} />
        {label}
      </button>
    )
  }
  return (
    <button
      aria-label={title}
      className={cn(
        "rounded-md p-1.5 text-slate-400",
        danger ? "hover:bg-red-50 hover:text-red-600" : "hover:bg-slate-100 hover:text-slate-700",
      )}
      onClick={onClick}
      title={title}
      type="button"
    >
      <Icon size={16} />
    </button>
  )
}

export function DetailHeader({
  workspace,
  sessionId,
  onSession,
  onNewTask,
  onTerminal,
  onSettings,
  onDelete,
}: {
  workspace: WorkspaceSummary
  sessionId: string
  onSession: (sessionId: string) => void
  onNewTask: () => void
  onTerminal: () => void
  onSettings: () => void
  onDelete: () => void
}) {
  const sessions = workspace.sessions ?? []
  const meta = [workspace.stack, workspace.commit, workspace.updated ? `updated ${workspace.updated}` : null]
    .filter(Boolean)
    .join(" · ")

  return (
    <div className="sticky top-0 z-[var(--z-sticky)] border-b border-slate-200 bg-white/85 px-5 py-3.5 backdrop-blur-md sm:px-7">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-slate-200 bg-slate-50 text-slate-500">
              <GitBranch size={16} />
            </div>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <h2 className="truncate text-[18px] font-semibold tracking-tight text-slate-900">
                  {workspace.project}
                </h2>
                {workspace.release ? (
                  <Badge className="border-slate-200 bg-slate-50 text-slate-500" mono>
                    {workspace.release}
                  </Badge>
                ) : null}
              </div>
              <div className="mt-0.5 truncate font-mono text-[10.5px] text-slate-500">
                <span className="text-slate-600">{workspace.container}</span>
                {meta ? ` · ${meta}` : ""}
              </div>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          <StatusBadge status={workspace.docker.status} />
          <div className="mx-1 h-5 w-px bg-slate-200" />
          <HeaderButton icon={Plus} label="New task" onClick={onNewTask} primary />
          <HeaderButton icon={Terminal} onClick={onTerminal} title="Terminal" />
          <HeaderButton icon={SettingsIcon} onClick={onSettings} title="Settings" />
          <HeaderButton danger icon={Trash2} onClick={onDelete} title="Delete" />
        </div>
      </div>

      {sessions.length > 1 ? (
        <div className="mt-3 flex items-center gap-1.5 overflow-x-auto pb-0.5">
          <span className="shrink-0 font-mono text-[10px] uppercase tracking-[0.12em] text-slate-500">
            Sessions
          </span>
          {sessions.map((s) => {
            const active = s.id === sessionId
            return (
              <button
                key={s.id}
                aria-current={active}
                className={cn(
                  "group flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11.5px] transition-colors",
                  active
                    ? "border-status-running-border bg-status-running-soft text-status-running"
                    : "border-slate-200 bg-white text-slate-500 hover:bg-slate-50",
                )}
                onClick={() => onSession(s.id)}
                title={s.title}
                type="button"
              >
                <span
                  className={cn(
                    "inline-flex h-1.5 w-1.5 rounded-full",
                    `bg-status-${toneToken(statusMeta(s.status).tone)}`,
                  )}
                />
                <span className="font-mono">{s.id}</span>
                <span className="hidden max-w-[150px] truncate sm:inline">{s.title}</span>
              </button>
            )
          })}
        </div>
      ) : null}
    </div>
  )
}

function toneToken(tone: string): string {
  return (
    { neutral: "idle", blue: "running", green: "success", red: "failed", amber: "attention" }[tone] ?? "idle"
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd webui && npm test -- src/pages/detail/DetailHeader.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webui/src/pages/detail/DetailHeader.tsx webui/src/pages/detail/DetailHeader.test.tsx
git commit -m "feat(webui): detail-pane sticky header + session switcher"
```

---

## Task 7: Extract NewTaskModal and WorkspaceSettings from Workspace.tsx

These must survive `Workspace.tsx`'s retirement (Task 10). Move them to `components/workspace/` unchanged.

**Files:**
- Create: `webui/src/components/workspace/NewTaskModal.tsx`, `webui/src/components/workspace/WorkspaceSettings.tsx`
- Modify: `webui/src/pages/Workspace.tsx` (import the extracted pieces instead of defining them — keeps Workspace working until Task 10)

- [ ] **Step 1: Create `NewTaskModal.tsx`**

Move the `NewTaskModal` component verbatim from `webui/src/pages/Workspace.tsx` into `webui/src/components/workspace/NewTaskModal.tsx`, exporting it. Copy the imports it needs (`FormEvent`/`useState` from react; `Box`, `Plus`, `Send` from lucide-react; `WorkspaceSummary` type; `Button`; the `Dialog*` primitives). Signature is unchanged:

```tsx
export function NewTaskModal({
  workspace,
  sourceSession,
  onClose,
  onSubmit,
}: {
  workspace: WorkspaceSummary
  sourceSession?: string
  onClose: () => void
  onSubmit: (task: string, sourceSession?: string) => Promise<void>
}) { /* …body copied verbatim from Workspace.tsx… */ }
```

- [ ] **Step 2: Create `WorkspaceSettings.tsx`**

Move `SettingsTab` + its helpers (`SettingsCard`, `SettingsRow`) and the local `normalizeWorkspaceBuild` it depends on into `webui/src/components/workspace/WorkspaceSettings.tsx`, exporting the top-level component as `WorkspaceSettings`:

```tsx
export function WorkspaceSettings({
  workspace,
  latest,
}: {
  workspace: WorkspaceSummary
  latest?: ExecutionSessionDetail | null
}) { /* …body copied verbatim from SettingsTab… */ }
```

Copy the imports it needs (`Activity`, `Box`, `GitBranch`, `Settings as SettingsIcon` from lucide-react; `StatusBadge`; `Card`; `cn`; types). Keep a local `normalizeWorkspaceBuild` here (Workspace.tsx keeps its own copy too — duplication is fine; both files are small and Workspace.tsx is deleted in Task 10).

- [ ] **Step 3: Point Workspace.tsx at the extracted modules**

In `webui/src/pages/Workspace.tsx`, delete the now-moved `NewTaskModal`, `SettingsTab`, `SettingsCard`, `SettingsRow` definitions, add imports:

```ts
import { NewTaskModal } from "@/components/workspace/NewTaskModal"
import { WorkspaceSettings } from "@/components/workspace/WorkspaceSettings"
```

and replace the `tab === "Settings"` render with `<WorkspaceSettings latest={latest} workspace={workspace} />`.

- [ ] **Step 4: Run the existing Workspace tests + tsc**

Run: `cd webui && npm test -- src/pages/Workspace.test.tsx && npx tsc -p tsconfig.app.json --noEmit`
Expected: PASS / clean — behavior is unchanged; only the definitions moved.

- [ ] **Step 5: Commit**

```bash
git add webui/src/components/workspace/NewTaskModal.tsx webui/src/components/workspace/WorkspaceSettings.tsx webui/src/pages/Workspace.tsx
git commit -m "refactor(webui): extract NewTaskModal + WorkspaceSettings for reuse"
```

---

## Task 8: Terminal/Settings slide-over

A single overlay that hosts either the terminal or the settings panel, opened from the header.

**Files:**
- Create: `webui/src/components/workspace/WorkspacePanels.tsx`
- Test: `webui/src/components/workspace/WorkspacePanels.test.tsx`

- [ ] **Step 1: Write the failing test**

`webui/src/components/workspace/WorkspacePanels.test.tsx`:

```tsx
import { fireEvent, render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import type { WorkspaceSummary } from "@/api/types"

import { WorkspacePanel } from "./WorkspacePanels"

const workspace: WorkspaceSummary = {
  id: "sag-x",
  project: "owner/x",
  container: "sag-x",
  stack: "Java · Maven",
  docker: { status: "exited", image: "sag/base" },
  task: "t",
  build: "success",
  test: { state: "pass", pass: 1, fail: 0, skip: 0, total: 1 },
  report: "ready",
  changed: 0,
  updated: "now",
}

describe("WorkspacePanel", () => {
  it("renders the settings panel and closes", () => {
    const onClose = vi.fn()
    render(<WorkspacePanel kind="settings" workspace={workspace} latest={null} onClose={onClose} />)
    expect(screen.getByRole("dialog", { name: /settings/i })).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: /close/i }))
    expect(onClose).toHaveBeenCalled()
  })

  it("shows a not-running message for the terminal when the container is stopped", () => {
    render(<WorkspacePanel kind="terminal" workspace={workspace} latest={null} onClose={() => {}} />)
    expect(screen.getByText(/not running/i)).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd webui && npm test -- src/components/workspace/WorkspacePanels.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

`webui/src/components/workspace/WorkspacePanels.tsx`:

```tsx
import { Terminal as TerminalIcon, X } from "lucide-react"

import type { ExecutionSessionDetail, WorkspaceSummary } from "@/api/types"
import { StatusBadge } from "@/components/common/Badge"
import { TerminalPanel } from "@/components/terminal/TerminalPanel"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"

import { WorkspaceSettings } from "./WorkspaceSettings"

export type WorkspacePanelKind = "terminal" | "settings"

export function WorkspacePanel({
  kind,
  workspace,
  latest,
  onClose,
}: {
  kind: WorkspacePanelKind
  workspace: WorkspaceSummary
  latest?: ExecutionSessionDetail | null
  onClose: () => void
}) {
  const running = workspace.docker.status.trim().toLowerCase() === "running"
  return (
    <Dialog open onOpenChange={(open) => (!open ? onClose() : undefined)}>
      <DialogContent className="w-[calc(100vw-2rem)] max-w-[760px] gap-0 border-slate-200 bg-white p-0 shadow-xl">
        <DialogHeader className="flex flex-row items-center justify-between border-b border-slate-100 px-4 py-3">
          <DialogTitle className="flex items-center gap-2 text-[13px] font-semibold text-slate-800">
            {kind === "terminal" ? <TerminalIcon className="text-slate-500" size={16} /> : null}
            {kind === "terminal" ? "Terminal" : "Settings"}
          </DialogTitle>
          {kind === "terminal" ? <StatusBadge status={workspace.docker.status} /> : null}
        </DialogHeader>
        <div className="max-h-[70vh] overflow-auto p-4">
          {kind === "settings" ? (
            <WorkspaceSettings latest={latest} workspace={workspace} />
          ) : running ? (
            <TerminalPanel workspaceId={workspace.id} />
          ) : (
            <div className="rounded-lg border border-slate-200 bg-slate-50 px-4 py-6">
              <div className="text-[13px] font-medium text-slate-700">Container is not running</div>
              <div className="mt-1 text-[12px] leading-relaxed text-slate-500">
                Start the workspace container before opening an interactive shell.
              </div>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}
```

> The shadcn `DialogContent` already renders an accessible Close button labelled "Close"; the settings test relies on it. If the project's `DialogContent` does not, add an explicit `<button aria-label="Close" onClick={onClose}>` with an `X` icon in the header.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd webui && npm test -- src/components/workspace/WorkspacePanels.test.tsx`
Expected: PASS. If the "Close" lookup fails because the primitive's close control isn't labelled, add the explicit close button noted above and re-run.

- [ ] **Step 5: Commit**

```bash
git add webui/src/components/workspace/WorkspacePanels.tsx webui/src/components/workspace/WorkspacePanels.test.tsx
git commit -m "feat(webui): terminal/settings slide-over panel"
```

---

## Task 9: Compose the Detail Pane

Assemble header + summary band + two-pane (nav + scroll-spy sections) + panels + new-task modal.

**Files:**
- Create: `webui/src/pages/detail/DetailPane.tsx`
- Test: `webui/src/pages/detail/DetailPane.test.tsx`

- [ ] **Step 1: Write the failing test**

`webui/src/pages/detail/DetailPane.test.tsx`:

```tsx
import { fireEvent, render, screen } from "@testing-library/react"
import { beforeAll, describe, expect, it, vi } from "vitest"

import type { ExecutionSessionDetail, WorkspaceSummary } from "@/api/types"

import { DetailPane } from "./DetailPane"

beforeAll(() => {
  // jsdom has no layout/scroll; stub so jump() doesn't throw.
  Element.prototype.scrollIntoView = vi.fn()
})

const workspace: WorkspaceSummary = {
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
}

const detail: ExecutionSessionDetail = {
  id: "CC-1",
  workspace: "sag-x",
  title: "Build and test",
  status: "completed",
  entry: "SAG",
  start: "now",
  duration: "1m",
  outcome: "All good.",
  build: { state: "success", tool: "Maven", time: "1s", note: "" },
  test: { state: "pass", pass: 10, fail: 0, skip: 0, total: 10 },
  report: "ready",
  evidence: [],
  logs: [],
}

const handlers = {
  sessionId: "CC-1",
  onSession: () => {},
  onSubmitTask: vi.fn().mockResolvedValue({ session_id: "CC-2" }),
  onDelete: vi.fn().mockResolvedValue(undefined),
}

describe("DetailPane", () => {
  it("renders the header, summary band, section nav, and all facet sections", () => {
    render(<DetailPane workspace={workspace} detail={detail} {...handlers} />)
    expect(screen.getByRole("heading", { name: "owner/x" })).toBeInTheDocument()
    expect(screen.getByText("All good.")).toBeInTheDocument()
    expect(screen.getByRole("navigation", { name: /detail sections/i })).toBeInTheDocument()
    for (const id of ["build", "test", "flow", "evidence", "files", "report", "logs"]) {
      expect(document.getElementById(`facet-${id}`)).toBeTruthy()
    }
  })

  it("opens the new-task modal from the header", () => {
    render(<DetailPane workspace={workspace} detail={detail} {...handlers} />)
    fireEvent.click(screen.getByRole("button", { name: "New task" }))
    expect(screen.getByRole("dialog", { name: /new task/i })).toBeInTheDocument()
  })

  it("opens the terminal panel from the header", () => {
    render(<DetailPane workspace={workspace} detail={detail} {...handlers} />)
    fireEvent.click(screen.getByRole("button", { name: /terminal/i }))
    expect(screen.getByRole("dialog", { name: /terminal/i })).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd webui && npm test -- src/pages/detail/DetailPane.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

`webui/src/pages/detail/DetailPane.tsx`:

```tsx
import { useMemo, useState } from "react"

import type { ExecutionSessionDetail, SubmitTaskResponse, WorkspaceSummary } from "@/api/types"
import { NewTaskModal } from "@/components/workspace/NewTaskModal"
import {
  WorkspacePanel,
  type WorkspacePanelKind,
} from "@/components/workspace/WorkspacePanels"
import {
  DeleteWorkspaceDialog,
  type DeleteWorkspaceTarget,
} from "@/components/workspace/DeleteWorkspaceDialog"

import { DetailHeader } from "./DetailHeader"
import { SectionNav } from "./SectionNav"
import { SummaryBand } from "./SummaryBand"
import { buildDetailFacets, FacetBody, type FacetId } from "./facets"
import { useScrollSpy } from "./scrollSpy"

export function DetailPane({
  workspace,
  detail,
  sessionId,
  onSession,
  onSubmitTask,
  onDelete,
}: {
  workspace: WorkspaceSummary
  detail: ExecutionSessionDetail
  sessionId: string
  onSession: (sessionId: string) => void
  onSubmitTask: (workspaceId: string, task: string, sourceSession?: string) => Promise<SubmitTaskResponse>
  onDelete: (workspaceId: string) => Promise<void>
}) {
  const facets = useMemo(() => buildDetailFacets(detail), [detail])
  const ids = useMemo(() => facets.map((f) => f.id), [facets])
  const { containerRef, active, onScroll, jump } = useScrollSpy(ids)

  const [panel, setPanel] = useState<WorkspacePanelKind | null>(null)
  const [taskOpen, setTaskOpen] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<DeleteWorkspaceTarget | null>(null)

  return (
    <div className="mx-auto flex h-[calc(100vh-3rem)] max-w-[1180px] flex-col">
      <DetailHeader
        onDelete={() =>
          setDeleteTarget({ workspaceId: workspace.id, label: workspace.project, kind: "workspace" })
        }
        onNewTask={() => setTaskOpen(true)}
        onSession={onSession}
        onSettings={() => setPanel("settings")}
        onTerminal={() => setPanel("terminal")}
        sessionId={sessionId}
        workspace={workspace}
      />

      <div className="flex min-h-0 flex-1">
        {/* Left section-nav (sticky within the flex row) */}
        <aside className="hidden w-44 shrink-0 overflow-y-auto border-r border-slate-200 px-3 py-6 lg:block">
          <SectionNav active={active} facets={facets} onJump={(id: FacetId) => jump(id)} />
        </aside>

        {/* Right continuous scroll */}
        <div ref={containerRef} className="min-h-0 flex-1 overflow-y-auto px-5 py-6 sm:px-7" onScroll={onScroll}>
          <SummaryBand detail={detail} />
          <div className="mt-7 space-y-7">
            {facets.map((f) => (
              <section key={f.id} className="scroll-mt-[150px]" id={`facet-${f.id}`}>
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
      </div>

      {panel ? (
        <WorkspacePanel kind={panel} latest={detail} onClose={() => setPanel(null)} workspace={workspace} />
      ) : null}

      {taskOpen ? (
        <NewTaskModal
          onClose={() => setTaskOpen(false)}
          onSubmit={async (task, sourceSession) => {
            await onSubmitTask(workspace.id, task, sourceSession)
            setTaskOpen(false)
          }}
          sourceSession={sessionId}
          workspace={workspace}
        />
      ) : null}

      {deleteTarget ? (
        <DeleteWorkspaceDialog
          onCancel={() => setDeleteTarget(null)}
          onConfirm={async (id) => {
            await onDelete(id)
            setDeleteTarget(null)
          }}
          target={deleteTarget}
        />
      ) : null}
    </div>
  )
}
```

> The NewTaskModal here prefills `sourceSession={sessionId}`. That matches the prototype's "New task" affordance. The "New task from this session" deep-link from the dashboard is handled in Task 10 (App passes the initial session).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd webui && npm test -- src/pages/detail/DetailPane.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webui/src/pages/detail/DetailPane.tsx webui/src/pages/detail/DetailPane.test.tsx
git commit -m "feat(webui): compose the two-pane Detail Pane"
```

---

## Task 10: Merge routes in App and retire the old pages

Flip `App.tsx` from `dashboard | workspace | session` to `dashboard | detail`, render `DetailPane`, and delete `Workspace.tsx` / `SessionDetail.tsx`.

**Files:**
- Modify: `webui/src/App.tsx`, `webui/src/test/App.test.tsx`
- Delete: `webui/src/pages/Workspace.tsx`, `webui/src/pages/Workspace.test.tsx`, `webui/src/pages/SessionDetail.tsx`, `webui/src/pages/SessionDetail.test.tsx`

- [ ] **Step 1: Update the App test (failing first)**

In `webui/src/test/App.test.tsx`, the test "opens workspace overview, submits a workspace task, and fetches session detail" (and any test that asserts the old tabbed Workspace/Session UI) must be updated to the merged detail view. Replace its post-click assertions so that, after clicking a workspace row, it asserts the Detail Pane renders:

```ts
    fireEvent.click((await screen.findAllByLabelText(/open workspace apache\/commons-cli/i))[0])

    // Merged detail pane: header heading + summary band + section nav.
    expect(await screen.findByRole("heading", { name: "apache/commons-cli" })).toBeInTheDocument()
    expect(screen.getByRole("navigation", { name: /detail sections/i })).toBeInTheDocument()
```

For the task-submission portion, open the New task modal from the header and submit:

```ts
    fireEvent.click(screen.getByRole("button", { name: "New task" }))
    const textarea = screen.getByPlaceholderText(/add a health check/i)
    fireEvent.change(textarea, { target: { value: "run smoke tests" } })
    fireEvent.click(screen.getByRole("button", { name: /submit task/i }))
    await waitFor(() => expect(submitCalls.length).toBeGreaterThan(0))
```

> Adapt variable names (`submitCalls`, fetch spies) to whatever the existing test already sets up. The key change: there is no "Overview"/"Sessions"/"Status" tab UI anymore — assert the detail pane instead. Remove or rewrite any assertion that depended on the retired tab labels.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd webui && npm test -- src/test/App.test.tsx`
Expected: FAIL — App still renders the old Workspace/Session routes.

- [ ] **Step 3: Rewrite the App route layer**

In `webui/src/App.tsx`:

3a. Replace the `Route` union:

```ts
type Route =
  | { view: "dashboard" }
  | { view: "detail"; workspaceId: string; sessionId?: string; facet?: string }
```

3b. Replace the page imports:

```ts
import { Dashboard } from "@/pages/Dashboard"
import { DashboardSkeleton } from "@/pages/DashboardSkeleton"
import { DetailPane } from "@/pages/detail/DetailPane"
```

(remove the `SessionDetail`, `Workspace`/`WorkspaceSessionRow` imports.)

3c. Replace the navigation helpers:

```ts
  const openDashboard = () => {
    setRouteError(null)
    setRoute({ view: "dashboard" })
  }
  const openDetail = (workspaceId: string, sessionId?: string, facet?: string) => {
    setRouteError(null)
    setRoute({ view: "detail", workspaceId, sessionId, facet })
  }
  // Dashboard row → detail (latest session). Dashboard report action → detail at that session.
  const openWorkspace = (workspaceId: string) => openDetail(workspaceId)
  const openSession = (workspaceId: string, sessionId: string, tab?: string) =>
    openDetail(workspaceId, sessionId, tab)
```

3d. Replace the session-detail loading + polling effects so they key off the **detail** route's selected session. The selected session is `route.sessionId ?? workspace.latestSession`:

```tsx
  const selectedWorkspace =
    route.view === "detail" ? dashboard?.workspaces.find((w) => w.id === route.workspaceId) : undefined
  const selectedSessionId =
    route.view === "detail" ? route.sessionId ?? selectedWorkspace?.latestSession ?? undefined : undefined

  useEffect(() => {
    if (!dashboard || route.view !== "detail" || !selectedSessionId) {
      return
    }
    void ensureSessionDetail(selectedSessionId)
  }, [dashboard, ensureSessionDetail, route.view, selectedSessionId])

  useEffect(() => {
    if (route.view !== "detail" || !selectedSessionId) {
      return
    }
    const detail = sessionDetails[selectedSessionId]
    if (detail && !isLiveSessionStatus(detail.status)) {
      return
    }
    const interval = window.setInterval(() => {
      void ensureSessionDetail(selectedSessionId, { silent: true })
    }, SESSION_DETAIL_POLL_MS)
    return () => window.clearInterval(interval)
  }, [ensureSessionDetail, route.view, selectedSessionId, sessionDetails])
```

(Delete the old `route.view === "session"` / `route.view === "workspace"` effects and the `WorkspaceRoute`/`SessionRoute` components + `sessionRows`/`fallbackSessionSummaries`/`normalizeSummaryBuild` helpers they used.)

3e. Replace the route render blocks (the `route.view === "dashboard"`, `"workspace"`, `"session"` sections) with dashboard + detail:

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

      {dashboard && route.view === "detail" ? (
        selectedWorkspace ? (
          selectedSessionId && sessionDetails[selectedSessionId] ? (
            <DetailPane
              detail={sessionDetails[selectedSessionId]}
              onDelete={deleteWorkspace}
              onSession={(sid) => openDetail(route.workspaceId, sid)}
              onSubmitTask={submitWorkspaceTask}
              sessionId={selectedSessionId}
              workspace={selectedWorkspace}
            />
          ) : (
            <main className="mx-auto max-w-[1180px] px-4 py-5 sm:px-6 lg:px-8 lg:py-7">
              <Card className="inline-flex px-3 py-2 text-[13px] text-slate-500">
                {selectedSessionId
                  ? `Loading session ${selectedSessionId}...`
                  : "This workspace has no execution session yet."}
              </Card>
            </main>
          )
        ) : (
          <main className="mx-auto max-w-[1180px] px-4 py-5 sm:px-6 lg:px-8 lg:py-7">
            <Card className="p-5">
              <div className="text-[15px] font-semibold text-slate-900">Workspace not found</div>
              <div className="mt-1 font-mono text-[12px] text-slate-500">{route.workspaceId}</div>
            </Card>
          </main>
        )
      ) : null}
```

3f. Update the `Breadcrumb` component to the two-level structure:

```tsx
function Breadcrumb({ route, onDashboard }: { route: Route; onDashboard: () => void }) {
  return (
    <nav className="flex min-w-0 items-center gap-2">
      <button
        className={`whitespace-nowrap text-[12.5px] ${
          route.view === "dashboard" ? "font-medium text-slate-700" : "text-slate-500 hover:text-slate-700"
        }`}
        disabled={route.view === "dashboard"}
        onClick={onDashboard}
        type="button"
      >
        dashboard
      </button>
      {route.view === "detail" ? (
        <>
          <span className="text-slate-200">/</span>
          <span className="truncate text-[12.5px] font-medium text-slate-700">{route.workspaceId}</span>
        </>
      ) : null}
    </nav>
  )
}
```

3g. Remove the now-unused `openTaskFromSession` and `initialTaskSourceSession` plumbing (the New task modal lives in `DetailPane` now). Keep `submitWorkspaceTask`, `deleteWorkspace`, launch handling, error/skeleton blocks unchanged. Ensure `isLiveSessionStatus` is still defined/used.

- [ ] **Step 4: Delete the retired pages**

```bash
git rm webui/src/pages/Workspace.tsx webui/src/pages/Workspace.test.tsx webui/src/pages/SessionDetail.tsx webui/src/pages/SessionDetail.test.tsx
```

- [ ] **Step 5: Run tests + tsc to verify green**

Run: `cd webui && npm test -- src/test/App.test.tsx && npx tsc -p tsconfig.app.json --noEmit`
Expected: PASS / clean. If `tsc` flags leftover references to deleted symbols (`WorkspaceSessionRow`, `SessionRoute`, etc.), remove them.

- [ ] **Step 6: Commit**

```bash
git add webui/src/App.tsx webui/src/test/App.test.tsx
git commit -m "feat(webui): merge workspace+session routes into the Detail Pane"
```

---

## Task 11: Full verification

**Files:** none.

- [ ] **Step 1: Full suite**

Run: `cd webui && npm test`
Expected: PASS — all suites green (the two deleted test files are gone; the new `detail/` suites + updated `App` suite pass).

- [ ] **Step 2: Type-check**

Run: `cd webui && npx tsc -p tsconfig.app.json --noEmit`
Expected: clean.

- [ ] **Step 3: Confirm no `static/` is staged**

Run: `git -C .. diff --cached --name-only -- src/sag/web/static`
Expected: empty.

- [ ] **Step 4: Confirm no dangling imports of the deleted pages**

Run: `cd webui && grep -rn "pages/Workspace\|pages/SessionDetail" src || echo "no dangling references"`
Expected: `no dangling references`.

---

## Self-Review

**Spec coverage (Phase 3 = Detail-pane shell, per the redesign spec):**
- Route merge `workspace`+`session` → `detail` → Task 10.
- Sticky header + actions cluster → Task 6.
- Session switcher → Task 6.
- Summary Band (Outcome + signal tiles + Why, dormant when `blocker=null`) → Task 4.
- Two-pane body: left section-nav + right single scroll + scroll-spy → Tasks 2, 5, 9.
- Facets Build/Test/Flow/Evidence/Files/Report/Logs wired to real data, Features A/B preserved via embeddable Build/Test pages → Tasks 1, 3, 9.
- Terminal + Settings as header actions → Tasks 7, 8, 9.
- Facet **restyle** explicitly deferred to Phases 4/5 (bodies reuse existing renderers).

**Type/name consistency:** `FacetId`/`FacetMeta`/`buildDetailFacets`/`FacetBody`/`Empty` (Task 3) used in Tasks 5, 9. `pickActiveSection`/`useScrollSpy` (Task 2) used in Task 9. `SummaryBand`(Task 4), `SectionNav`(Task 5), `DetailHeader`(Task 6), `NewTaskModal`/`WorkspaceSettings`(Task 7), `WorkspacePanel`/`WorkspacePanelKind`(Task 8) all composed in `DetailPane`(Task 9) and rendered by `App`(Task 10). `DeleteWorkspaceTarget` reused from the existing dialog.

**Placeholder scan:** none — every code step has complete content; facet bodies are explicit reuse of existing components.

**Risk notes:**
- jsdom has no layout, so scroll-spy auto-highlight can't be DOM-tested; the math is unit-tested via `pickActiveSection` (Task 2), and `scrollIntoView` is stubbed in the DetailPane test.
- Duplicate `aria-label` pitfall from Phase 2 does not recur here — the detail pane renders one tree; but any new test that queries workspace rows on the dashboard must still use `getAllByLabelText`.
- App test rewrites (Task 10) are the largest churn; adapt to the existing test's fetch-spy scaffolding rather than assuming variable names.
