# Build/Test Facet Summary + Detail Modal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (inline). Steps use checkbox (`- [ ]`) syntax.

**Goal:** Facets show the prototype summary (Test = conclusion + FAILING; Build = two-card); the per-module Features A/B (module stats + `ModuleTable` + coverage) move to a **modal "detail page"** opened from a "View per-module breakdown" button. Matches `workbench/sections.jsx` + Image #6; keeps the build/test detail page.

**Architecture:** Presentation-layer only. New `BuildFacet`/`TestFacet` (stateful: summary + breakdown button + modal). `BuildDetailPage`/`TestDetailPage` slim to the per-module breakdown (the modal body — the kept detail page). `FacetBody` (facets.tsx) renders `BuildFacet`/`TestFacet`.

**Branch:** `feature/webui-workbench-redesign` (even with main). Policy: no `npm run build`; never stage `static/`; force-add docs; exact paths; no `Co-Authored-By`.

**Verify:** `cd webui && npm test -- <path>`; `npx tsc -p tsconfig.app.json --noEmit`.

**Invariants:** Features A/B preserved in the modal (module tables, coverage column, failing-method expand, no fake zeroes, CoverageTile branch-only/>100% guards). FAILING card uses `detail.test.failingNames` (already populated). Phase-1 tokens.

---

## Task 1: FailingCard

**Files:** Create `webui/src/components/session/FailingCard.tsx` + test.

- [ ] **Step 1 (test):** `FailingCard.test.tsx` — renders "Failing · 2" + the names when `names` non-empty; renders nothing when empty.

```tsx
import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"
import { FailingCard } from "./FailingCard"

describe("FailingCard", () => {
  it("lists failing test names", () => {
    render(<FailingCard names={["A.testX", "B.testY"]} />)
    expect(screen.getByText(/Failing · 2/)).toBeInTheDocument()
    expect(screen.getByText("A.testX")).toBeInTheDocument()
  })
  it("renders nothing when there are none", () => {
    const { container } = render(<FailingCard names={[]} />)
    expect(container).toBeEmptyDOMElement()
  })
})
```

- [ ] **Step 2:** run → FAIL (module missing).
- [ ] **Step 3 (impl):** `FailingCard.tsx`:

```tsx
import { X } from "lucide-react"

export function FailingCard({ names, hiddenCount = 0, evidenceRef }: {
  names: string[]; hiddenCount?: number; evidenceRef?: string | null
}) {
  if (!names.length) {
    return null
  }
  return (
    <div className="overflow-hidden rounded-lg border border-status-failed-border">
      <div className="border-b border-status-failed-border bg-status-failed-soft/60 px-4 py-2">
        <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-status-failed">
          Failing · {names.length + hiddenCount}
        </span>
      </div>
      <div className="divide-y divide-status-failed-border/40">
        {names.map((n) => (
          <div key={n} className="flex items-center gap-2 px-4 py-2">
            <X className="shrink-0 text-status-failed" size={13} />
            <span className="truncate font-mono text-[12px] text-slate-700">{n}</span>
          </div>
        ))}
        {hiddenCount > 0 ? (
          <div className="px-4 py-2 font-mono text-[10px] text-slate-500">
            +{hiddenCount} more{evidenceRef ? ` — full list at ${evidenceRef}` : ""}
          </div>
        ) : null}
      </div>
    </div>
  )
}
```

- [ ] **Step 4:** run → PASS.
- [ ] **Step 5:** commit `feat(webui): FailingCard for the Test facet`.

---

## Task 2: ModuleBreakdownDialog (modal shell)

**Files:** Create `webui/src/components/session/ModuleBreakdownDialog.tsx`.

- [ ] **Step 1 (impl):** a Dialog shell reusing the project's `Dialog` primitive:

```tsx
import type * as React from "react"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"

export function ModuleBreakdownDialog({ title, onClose, children }: {
  title: string; onClose: () => void; children: React.ReactNode
}) {
  return (
    <Dialog open onOpenChange={(open) => (!open ? onClose() : undefined)}>
      <DialogContent className="w-[calc(100vw-2rem)] max-w-[900px] gap-0 border-slate-200 bg-white p-0 shadow-xl">
        <DialogHeader className="border-b border-slate-100 px-4 py-3">
          <DialogTitle className="text-[13px] font-semibold text-slate-800">{title}</DialogTitle>
        </DialogHeader>
        <div className="max-h-[72vh] overflow-auto p-4">{children}</div>
      </DialogContent>
    </Dialog>
  )
}
```

- [ ] **Step 2:** `tsc` clean (covered by the facet tests that open it).
- [ ] **Step 3:** commit `feat(webui): module breakdown dialog shell`.

---

## Task 3: Slim BuildDetailPage / TestDetailPage to the per-module breakdown

These become the modal body (the kept detail page) — drop the conclusion/outputs (now in the facet).

**Files:** Modify `BuildDetailPage.tsx`, `TestDetailPage.tsx` + their tests.

- [ ] **Step 1 (BuildDetailPage):** remove `ConclusionCard`/`OutputsCard` rendering (and the optional `onBack` row); keep `fmtNum`, `Stat`, `successRate`, and render the module stats row + gradle note + `ModuleTable` (multi) / a single-module note. Export the conclusion/outputs cards so the facet can import them (move `ConclusionCard`/`OutputsCard`/`KV`/`MonoLabel` to a shared `buildCards.tsx`, imported by both BuildDetailPage-was and BuildFacet). New `BuildDetailPage(detail)` body:

```tsx
export function BuildDetailPage({ detail }: { detail: ExecutionSessionDetail }) {
  const b = detail.build
  const s = detail.moduleSummary
  const rate = successRate(s)
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
        <Stat label="Modules" value={fmtNum(s?.modulesTotal)} />
        <Stat label="Built" value={fmtNum(s?.modulesBuilt)} tone="text-status-success" />
        <Stat label="Failed" value={fmtNum(s?.modulesFailed)} tone="text-status-failed" />
        <Stat label="Skipped" value={fmtNum(s?.modulesSkipped)} />
        <Stat label="Success rate" value={rate != null ? `${rate}%` : "—"} />
      </div>
      {(s?.buildSystems ?? []).includes("gradle") ? (
        <div className="font-mono text-[10px] text-slate-500">
          Gradle has no reactor summary — per-module build status is inferred from build outputs (best-effort).
        </div>
      ) : null}
      <Card className="overflow-hidden"><ModuleTable modules={detail.modules ?? []} variant="build" /></Card>
    </div>
  )
}
```

- [ ] **Step 2 (TestDetailPage):** symmetric — drop `ConclusionCard` (move it + `Tile`/`CoverageTile`/`coverageTone` to a shared `testCards.tsx`); render the tiles row + `ModuleTable` (variant test):

```tsx
export function TestDetailPage({ detail }: { detail: ExecutionSessionDetail }) {
  const s = detail.moduleSummary
  const t = detail.test
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Tile label="Unique methods" value={fmtNum(t.uniqueTotal)} />
        <Tile label="Modules w/ fails" value={fmtNum(s?.modulesWithTestFailures)} tone="text-status-failed" />
        <CoverageTile summary={s} />
      </div>
      <Card className="overflow-hidden"><ModuleTable modules={detail.modules ?? []} variant="test" /></Card>
    </div>
  )
}
```

- [ ] **Step 3:** update `BuildDetailPage.test.tsx`/`TestDetailPage.test.tsx`: drop the conclusion/outputs assertions (moved to the facet tests); keep the module-table + stats + coverage-tile + no-fake-zeroes assertions; drop the `onBack`/embedded-mode tests (no `onBack` now). Move the conclusion/outputs/CoverageTile tests that still apply to the facet test files.
- [ ] **Step 4:** run both → PASS; `tsc` clean.
- [ ] **Step 5:** commit `refactor(webui): slim Build/Test detail pages to per-module breakdown`.

---

## Task 4: BuildFacet + TestFacet (summary + breakdown modal)

**Files:** Create `BuildFacet.tsx`, `TestFacet.tsx` + tests; modify `webui/src/pages/detail/facets.tsx`.

- [ ] **Step 1 (TestFacet):** summary (shared test `ConclusionCard` + `FailingCard`) + breakdown button (multi-module) + modal:

```tsx
import { useState } from "react"
import type { ExecutionSessionDetail } from "@/api/types"
import { ConclusionCard } from "./testCards"
import { FailingCard } from "./FailingCard"
import { ModuleBreakdownDialog } from "./ModuleBreakdownDialog"
import { TestDetailPage } from "./TestDetailPage"

export function TestFacet({ detail }: { detail: ExecutionSessionDetail }) {
  const [open, setOpen] = useState(false)
  const s = detail.moduleSummary
  const single = s?.singleModule ?? (detail.modules?.length ?? 0) <= 1
  const failing = detail.test.failingNames ?? []
  return (
    <div className="space-y-4">
      <ConclusionCard test={detail.test} />
      <FailingCard names={failing} />
      {!single ? (
        <button
          className="font-mono text-[11px] text-status-running hover:underline"
          onClick={() => setOpen(true)}
          type="button"
        >
          View per-module breakdown ({s?.modulesTotal ?? detail.modules?.length} modules) →
        </button>
      ) : null}
      {open ? (
        <ModuleBreakdownDialog onClose={() => setOpen(false)} title="Per-module test breakdown">
          <TestDetailPage detail={detail} />
        </ModuleBreakdownDialog>
      ) : null}
    </div>
  )
}
```

- [ ] **Step 2 (BuildFacet):** symmetric with `ConclusionCard`+`OutputsCard` from `buildCards` and `BuildDetailPage` in the modal.
- [ ] **Step 3 (wire):** in `webui/src/pages/detail/facets.tsx` `FacetBody`, replace `case "build": return <BuildDetailPage .../>` with `<BuildFacet detail={detail} />`, and `case "test"` with `<TestFacet detail={detail} />`. Update imports.
- [ ] **Step 4 (tests):** `TestFacet.test.tsx` — renders conclusion + FAILING (from failingNames) + (multi-module) the breakdown button; clicking it opens the modal (assert a dialog + a module name). `BuildFacet.test.tsx` — two-card + breakdown button + modal. Single-module → no button.
- [ ] **Step 5:** run all → PASS; `tsc` clean.
- [ ] **Step 6:** commit `feat(webui): Build/Test facet summaries with per-module breakdown modal`.

---

## Task 5: Full verification + live

- [ ] **Step 1:** `cd webui && npm test` — green.
- [ ] **Step 2:** `npx tsc -p tsconfig.app.json --noEmit` — clean.
- [ ] **Step 3:** `git -C .. diff --cached --name-only -- src/sag/web/static` — empty.
- [ ] **Step 4 (live):** Chrome — Test facet = conclusion + FAILING (Image #6); "View per-module breakdown" opens the modal with the module table + coverage; Build facet = two-card + breakdown button + modal; single-module shows no button.

---

## Self-Review

**Spec coverage:** Test facet = conclusion + FAILING (Task 1, 4); Build facet two-card kept (Task 4); per-module → modal detail page (Tasks 2-4); Features A/B preserved in the modal (Task 3 keeps ModuleTable/coverage). FAILING from `failingNames`.

**Risk:** test churn (conclusion/coverage assertions move from detail-page tests to facet tests); the shared `buildCards`/`testCards` extraction must keep the exact ConclusionCard/CoverageTile logic (branch-only headline, >100% guard, no fake zeroes). Single-module path: no button/modal; FAILING still shows project failing names.
