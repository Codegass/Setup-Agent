# Phase 4 — Build & Test Facet Restyle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restyle the Build and Test facet bodies in the detail pane to the prototype's calmer, conclusion-first card look (`workbench/sections.jsx` `BuildBody`/`TestBody`) — **while preserving Features A/B** (per-module build/test tables, coverage column, failing-method expand, no fake zeroes) — using the Phase 1 `--status-*` tokens, and fix the stale "see … on Overview" copy (there is no Overview tab anymore).

**Architecture:** Presentation-layer only. The Build/Test facets already render `BuildDetailPage` / `TestDetailPage` (embeddable, from Phase 3). This phase restyles those two components + the shared `ModuleTable`, replacing raw `emerald/red/amber` literals with semantic tokens and adding a conclusion card. No data-contract or backend changes; the same `ExecutionSessionDetail` fields are consumed.

**Tech Stack:** React + TypeScript (Vite), Tailwind v4 (Phase 1 tokens), Vitest + Testing Library, lucide-react.

**Branch:** `feature/webui-workbench-redesign` (Phases 1–3b + fixes merged to main; branch is even with main). Continue on it.

**Project policy (do not violate):**
- Do **not** run `npm run build`; do **not** create/modify/`git add` anything under `src/sag/web/static/`.
- `docs/` is gitignored — force-add docs. Stage exact paths; never `git add -A`/`.`.
- No `Co-Authored-By` trailer on commits.

**Per-task verification:** `cd webui && npm test -- <test path>`; type check `cd webui && npx tsc -p tsconfig.app.json --noEmit`.

**Hard invariants (must hold after the restyle — these are what Features A/B guarantee):**
- Real `0` renders as `0`; a genuinely-absent value renders `—` (no fake zeroes). Reuse the existing `fmtNum` rule.
- Multi-module projects show the per-module table; single-module shows a calm note (NOT referencing "Overview").
- Test facet keeps the coverage column (stacked line/branch bars) + the failing-method inline expand.
- Coverage tile keeps the `isValidCoverage`/branch-only behavior already in `TestDetailPage` (do not regress the >100% guard or branch-only headline).
- Use `--status-*` tokens (`text-status-success`/`failed`/`attention`, `bg-status-*-soft`, `border-status-*-border`) for meaning-bearing color; keep `slate-*` for structure.

---

## Reference & Mapping

Prototype bodies (visual target): `docs/Setup Agent Web UI/workbench/sections.jsx` — `BuildBody` (lines 16–63: conclusion card with status + system badge + KV Tool/Time/Command/Artifact, an Outputs card with class/JAR stats + warnings, and a modules list) and `TestBody` (lines 74–108: a big pass/total card with pass-rate badge + `TestBar` + pass/fail/skip tiles + note, then a failing-names list).

Our data is richer than the prototype's modules list, so **keep our `ModuleTable`** (per-module build status + class/JAR + error expand; per-module pass/fail/skip + rate + coverage + failing expand) for the multi-module case, and adopt the prototype's conclusion/outputs cards for the summary.

`detail.build` (`BuildSummary`): `state, tool, time, note, system?, classCount?, jarCount?, artifact?, warnings?, moduleOutputCount?`.
`detail.test` (`TestSummary`): `state, pass, fail, skip, total, uniqueTotal?, passRate?, note?, failingNames?`.
`detail.moduleSummary` (`ModuleRollup`): `singleModule?, modulesTotal?, modulesBuilt?, modulesFailed?, modulesSkipped?, modulesWithTestFailures?, lineRate?, branchRate?, coverageSource?, buildSystems?`.

---

## File Structure

**Modify:**
- `webui/src/components/session/BuildDetailPage.tsx` — conclusion card + outputs stats + per-module table; token-align; fix single-module copy.
- `webui/src/components/session/BuildDetailPage.test.tsx` — assert the conclusion + corrected copy.
- `webui/src/components/session/TestDetailPage.tsx` — conclusion card (pass/total + rate + bar + tiles + coverage) + per-module table; token-align; fix single-module copy.
- `webui/src/components/session/TestDetailPage.test.tsx` — assert conclusion + corrected copy (keep coverage-tile tests).
- `webui/src/components/session/ModuleTable.tsx` — swap meaning-bearing `emerald/red/amber` literals for `--status-*` tokens (keep structure/behavior).
- `webui/src/components/session/ModuleTable.test.tsx` — update any class-name assertions that referenced the old literals.

**Reuse unchanged:** `Card`, `Badge`/`StatusBadge`, `status.ts` (`statusMeta`), `TestBar`, `cn`, the tokens from `styles.css`.

---

## Task 1: Token-align ModuleTable

Replace raw status literals with semantic tokens; keep all behavior (sorting, expand, coverage bars, no fake zeroes).

**Files:** Modify `webui/src/components/session/ModuleTable.tsx`, `webui/src/components/session/ModuleTable.test.tsx`

- [ ] **Step 1: Check the existing tests for literal assertions**

Run: `cd webui && grep -n "emerald\|text-red\|amber\|bg-status\|text-status" src/components/session/ModuleTable.test.tsx`
If any test asserts a literal class (e.g. `text-emerald-700`), note it — Step 4 updates it to the token equivalent. If none, no test change is needed beyond the green run.

- [ ] **Step 2: Restyle the status helpers**

In `webui/src/components/session/ModuleTable.tsx`, replace the color helpers with token versions:

```tsx
function statusClass(s: string): string {
  if (s === "success") return "bg-status-success-soft text-status-success"
  if (s === "failure") return "bg-status-failed-soft text-status-failed"
  if (s === "skipped") return "bg-slate-100 text-slate-500"
  return "bg-status-attention-soft text-status-attention"
}
```

Update the coverage tone + bar color helpers to tokens:

```tsx
function covColor(rate: number): string {
  // bar fill uses the CSS var so it matches the badge tones
  return rate >= 80
    ? "var(--status-success)"
    : rate >= 50
      ? "var(--status-attention)"
      : "var(--status-failed)"
}

function covTextClass(rate: number): string {
  return rate >= 80 ? "text-status-success" : rate >= 50 ? "text-status-attention" : "text-status-failed"
}
```

In the test-variant cells, swap the remaining literals: the Pass count `text-emerald-700` → `text-status-success`; the Fail count `text-red-600` → `text-status-failed`; the failing-method "View N failures" button `text-red-600` → `text-status-failed`; the build-error "N errors" button `text-red-600` → `text-status-failed`. Leave `slate-*` structural classes and the `text-slate-300` "—" placeholders as-is.

- [ ] **Step 3: Run tests to verify they fail (only if a literal was asserted)**

Run: `cd webui && npm test -- src/components/session/ModuleTable.test.tsx`
Expected: PASS if no test asserted a literal; FAIL on the specific literal assertion(s) found in Step 1 (update them in Step 4).

- [ ] **Step 4: Update any literal assertions + re-run**

If Step 1 found literal assertions, change them to the token class (e.g. `text-emerald-700` → `text-status-success`). Run: `cd webui && npm test -- src/components/session/ModuleTable.test.tsx` → PASS.

- [ ] **Step 5: Commit**

```bash
git add webui/src/components/session/ModuleTable.tsx webui/src/components/session/ModuleTable.test.tsx
git commit -m "style(webui): token-align ModuleTable status + coverage colors"
```

---

## Task 2: Restyle the Build facet (conclusion-first)

**Files:** Modify `webui/src/components/session/BuildDetailPage.tsx`, `webui/src/components/session/BuildDetailPage.test.tsx`

- [ ] **Step 1: Write/adjust the failing test**

Append to `webui/src/components/session/BuildDetailPage.test.tsx` (reuse the file's existing fixture; if each test builds an inline `detail`, build one with `build: { state: "success", tool: "Maven", time: "47.2s", note: "mvn -q install", system: "Maven", classCount: 120, jarCount: 3 }` and `moduleSummary: { singleModule: true }`):

```tsx
  it("shows a conclusion card with the build system and command", () => {
    render(
      <BuildDetailPage
        detail={{
          ...baseDetail,
          build: { state: "success", tool: "Maven", time: "47.2s", note: "mvn -q install", system: "Maven", classCount: 120, jarCount: 3 },
          moduleSummary: { singleModule: true },
          modules: [],
        }}
      />,
    )
    expect(screen.getByText("Success")).toBeInTheDocument()
    expect(screen.getByText("mvn -q install")).toBeInTheDocument()
    expect(screen.getByText("120")).toBeInTheDocument() // classes stat
  })

  it("uses a single-module note that does not mention an Overview tab", () => {
    render(
      <BuildDetailPage
        detail={{ ...baseDetail, moduleSummary: { singleModule: true }, modules: [] }}
      />,
    )
    expect(screen.queryByText(/Overview/i)).not.toBeInTheDocument()
  })
```

> If the test file lacks a shared `baseDetail`, construct a minimal `ExecutionSessionDetail` inline in each test (mirror the fixtures already in the file). The assertions, not the fixture name, are what matter.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd webui && npm test -- src/components/session/BuildDetailPage.test.tsx` — FAIL (no conclusion card; current copy says "Overview").

- [ ] **Step 3: Restyle BuildDetailPage**

Replace `webui/src/components/session/BuildDetailPage.tsx` with:

```tsx
import { ArrowLeft, Check, Clock, X } from "lucide-react"
import type * as React from "react"

import type { BuildSummary, ExecutionSessionDetail } from "@/api/types"
import { Badge, StatusBadge } from "@/components/common/Badge"
import { Button } from "@/components/common/Button"
import { Card } from "@/components/common/Card"
import { statusMeta } from "@/components/common/status"
import { cn } from "@/lib/utils"

import { ModuleTable } from "./ModuleTable"

function fmtNum(n?: number | null): string {
  // Real 0 stays "0"; a genuinely-absent value renders "—" (no fake zeroes).
  return typeof n === "number" && Number.isFinite(n) ? n.toLocaleString() : "—"
}

function KV({ k, v }: { k: string; v?: string | null }) {
  if (!v) {
    return null
  }
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-slate-100 py-1.5 last:border-b-0">
      <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-slate-500">{k}</span>
      <span className="truncate text-right font-mono text-[12px] text-slate-700">{v}</span>
    </div>
  )
}

function Stat({ label, value, tone }: { label: string; value: React.ReactNode; tone?: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white px-3 py-2">
      <div className={cn("text-[20px] font-semibold tabular-nums", tone ?? "text-slate-900")}>{value}</div>
      <div className="font-mono text-[10px] uppercase tracking-[0.1em] text-slate-500">{label}</div>
    </div>
  )
}

function ConclusionCard({ build }: { build: BuildSummary }) {
  const norm = build.state.trim().toLowerCase()
  const ok = norm === "success"
  const bad = norm === "failure" || norm === "failed"
  return (
    <Card className="p-4">
      <div className="flex items-center justify-between gap-3">
        <span className="inline-flex items-center gap-2 text-[14px] font-medium text-slate-800">
          {ok ? (
            <Check className="text-status-success" size={16} />
          ) : bad ? (
            <X className="text-status-failed" size={16} />
          ) : (
            <Clock className="text-slate-400" size={15} />
          )}
          {statusMeta(build.state).label}
        </span>
        {build.system || build.tool ? (
          <Badge mono tone={ok ? "green" : bad ? "red" : "neutral"}>
            {build.system ?? build.tool}
          </Badge>
        ) : null}
      </div>
      <div className="mt-2 grid gap-x-6 sm:grid-cols-2">
        <KV k="Tool" v={build.tool} />
        <KV k="Time" v={build.time} />
        <KV k="Command" v={build.note} />
        <KV k="Artifact" v={build.artifact} />
      </div>
    </Card>
  )
}

export function BuildDetailPage({
  detail,
  onBack,
}: {
  detail: ExecutionSessionDetail
  onBack?: () => void
}) {
  const b = detail.build
  const s = detail.moduleSummary
  const single = s?.singleModule ?? (detail.modules?.length ?? 0) <= 1

  return (
    <div className="space-y-4">
      {onBack ? (
        <div className="flex items-center justify-between">
          <Button onClick={onBack} size="sm" type="button" variant="ghost">
            <ArrowLeft size={14} /> Back
          </Button>
          <StatusBadge status={b.state} />
        </div>
      ) : null}

      <ConclusionCard build={b} />

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        {!single ? (
          <>
            <Stat label="Modules" value={fmtNum(s?.modulesTotal)} />
            <Stat label="Built" value={fmtNum(s?.modulesBuilt)} tone="text-status-success" />
            <Stat label="Failed" value={fmtNum(s?.modulesFailed)} tone="text-status-failed" />
            <Stat label="Skipped" value={fmtNum(s?.modulesSkipped)} />
          </>
        ) : null}
        <Stat label="Classes" value={fmtNum(b.classCount)} />
        <Stat label="JARs" value={fmtNum(b.jarCount)} />
      </div>

      {!single && (s?.buildSystems ?? []).includes("gradle") ? (
        <div className="font-mono text-[10px] text-slate-500">
          Gradle has no reactor summary — per-module build status is inferred from build outputs (best-effort).
        </div>
      ) : null}

      {single ? (
        <Card className="p-4">
          <div className="font-mono text-[12px] text-slate-500">
            Single-module project — the conclusion and outputs above cover the whole build.
          </div>
        </Card>
      ) : (
        <Card className="overflow-hidden">
          <ModuleTable modules={detail.modules ?? []} variant="build" />
        </Card>
      )}
    </div>
  )
}
```

- [ ] **Step 4: Run tests + tsc**

Run: `cd webui && npm test -- src/components/session/BuildDetailPage.test.tsx && npx tsc -p tsconfig.app.json --noEmit` — PASS / clean. (The existing embedded-mode test — no Back button without `onBack` — still holds.)

- [ ] **Step 5: Commit**

```bash
git add webui/src/components/session/BuildDetailPage.tsx webui/src/components/session/BuildDetailPage.test.tsx
git commit -m "feat(webui): conclusion-first Build facet, token-aligned"
```

---

## Task 3: Restyle the Test facet (conclusion-first)

**Files:** Modify `webui/src/components/session/TestDetailPage.tsx`, `webui/src/components/session/TestDetailPage.test.tsx`

- [ ] **Step 1: Write/adjust the failing test**

Append to `webui/src/components/session/TestDetailPage.test.tsx` (keep the existing coverage-tile tests; reuse/extend the file's fixture):

```tsx
  it("shows a conclusion card with the pass rate", () => {
    render(
      <TestDetailPage
        detail={{
          ...baseDetail,
          test: { state: "partial", pass: 312, fail: 8, skip: 0, total: 320, uniqueTotal: 318, passRate: 0.975 },
          moduleSummary: { singleModule: true },
          modules: [],
        }}
      />,
    )
    expect(screen.getByText(/97\.5% pass/i)).toBeInTheDocument()
    expect(screen.getByText("312")).toBeInTheDocument()
  })

  it("uses a single-module note that does not mention an Overview tab", () => {
    render(
      <TestDetailPage
        detail={{ ...baseDetail, moduleSummary: { singleModule: true }, modules: [] }}
      />,
    )
    expect(screen.queryByText(/Overview/i)).not.toBeInTheDocument()
  })
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd webui && npm test -- src/components/session/TestDetailPage.test.tsx` — FAIL.

- [ ] **Step 3: Restyle TestDetailPage**

Replace `webui/src/components/session/TestDetailPage.tsx`. Keep the existing `CoverageTile` (and its `coverageTone`/`isValidCoverage` behavior) verbatim; add a conclusion card; token-align; fix the single-module copy:

```tsx
import { ArrowLeft } from "lucide-react"
import type * as React from "react"

import type { ExecutionSessionDetail, ModuleRollup } from "@/api/types"
import { Badge, StatusBadge } from "@/components/common/Badge"
import { Button } from "@/components/common/Button"
import { Card } from "@/components/common/Card"
import { TestBar } from "@/components/common/TestBar"
import { cn } from "@/lib/utils"

import { ModuleTable } from "./ModuleTable"

function fmtNum(n?: number | null): string {
  return typeof n === "number" && Number.isFinite(n) ? n.toLocaleString() : "—"
}

function coverageTone(rate: number): string {
  return rate >= 80 ? "text-status-success" : rate >= 50 ? "text-status-attention" : "text-status-failed"
}

function Tile({ label, value, tone, dashed }: {
  label: string; value: React.ReactNode; tone?: string; dashed?: boolean
}) {
  return (
    <div className={cn("rounded-lg border px-3 py-2", dashed ? "border-dashed bg-slate-50" : "border-slate-200")}>
      <div className={cn("text-[22px] font-semibold tabular-nums", tone ?? "text-slate-900")}>{value}</div>
      <div className="font-mono text-[10px] uppercase tracking-[0.1em] text-slate-500">{label}</div>
    </div>
  )
}

// Coverage tile: headline on whichever rate exists (line preferred), tone by
// that rate. Branch-only no longer renders a red "line —"; absent -> unavailable.
function CoverageTile({ summary }: { summary?: ModuleRollup | null }) {
  const line = summary?.lineRate
  const branch = summary?.branchRate
  if (line == null && branch == null) {
    return <Tile label="Coverage" value={<span className="text-slate-400">— not measured</span>} dashed />
  }
  const primary = line != null ? line : (branch as number)
  const primaryLabel = line != null ? "line" : "branch"
  const showBranchSub = line != null && branch != null
  const hasSub = showBranchSub || !!summary?.coverageSource
  return (
    <Tile
      label={`Coverage · ${primaryLabel}`}
      tone={coverageTone(primary)}
      value={
        <>
          {`${Math.round(primary)}%`}
          {hasSub ? (
            <span className="block font-mono text-[10px] font-normal text-slate-500">
              {showBranchSub ? `${Math.round(branch as number)}% branch` : ""}
              {showBranchSub && summary?.coverageSource ? " · " : ""}
              {summary?.coverageSource ? "jacoco" : ""}
            </span>
          ) : null}
        </>
      }
    />
  )
}

function ConclusionCard({ test }: { test: ExecutionSessionDetail["test"] }) {
  const total = Math.max(test.total, test.pass + test.fail)
  const rate =
    test.passRate != null
      ? Math.round(test.passRate * 1000) / 10
      : total > 0
        ? Math.round((test.pass / total) * 1000) / 10
        : null
  return (
    <Card className="p-4">
      <div className="flex items-end justify-between gap-4">
        <div>
          <div className="text-[26px] font-semibold tabular-nums text-slate-900">
            {fmtNum(test.pass)}
            <span className="text-[16px] font-normal text-slate-400"> / {fmtNum(total)}</span>
          </div>
          <div className="mt-0.5 font-mono text-[10px] uppercase tracking-[0.1em] text-slate-500">
            runner executions passed
          </div>
        </div>
        {rate != null ? (
          <Badge tone={rate >= 99 ? "green" : rate >= 90 ? "amber" : "red"}>{rate}% pass</Badge>
        ) : null}
      </div>
      {total > 0 ? (
        <div className="mt-3">
          <TestBar fail={test.fail} pass={test.pass} total={total} />
        </div>
      ) : null}
      <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 font-mono text-[12px]">
        <span className="text-status-success">{fmtNum(test.pass)} passed</span>
        <span className="text-status-failed">{fmtNum(test.fail)} failed</span>
        <span className="text-slate-500">{fmtNum(test.skip)} skipped</span>
        {test.note ? <span className="text-slate-400">· {test.note}</span> : null}
      </div>
    </Card>
  )
}

export function TestDetailPage({
  detail,
  onBack,
}: {
  detail: ExecutionSessionDetail
  onBack?: () => void
}) {
  const t = detail.test
  const s = detail.moduleSummary
  const single = s?.singleModule ?? (detail.modules?.length ?? 0) <= 1

  return (
    <div className="space-y-4">
      {onBack ? (
        <div className="flex items-center justify-between">
          <Button onClick={onBack} size="sm" type="button" variant="ghost">
            <ArrowLeft size={14} /> Back
          </Button>
          <StatusBadge status={t.state} />
        </div>
      ) : null}

      <ConclusionCard test={t} />

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Tile label="Unique methods" value={fmtNum(t.uniqueTotal)} />
        <Tile label="Modules w/ fails" value={fmtNum(s?.modulesWithTestFailures)} tone="text-status-failed" />
        <CoverageTile summary={s} />
      </div>

      {single ? (
        <Card className="p-4">
          <div className="font-mono text-[12px] text-slate-500">
            Single-module project — the conclusion and coverage above cover the whole suite.
          </div>
        </Card>
      ) : (
        <Card className="overflow-hidden">
          <ModuleTable modules={detail.modules ?? []} variant="test" />
        </Card>
      )}
    </div>
  )
}
```

> This keeps every Feature A/B element: `uniqueTotal`, `modulesWithTestFailures`, the coverage tile (line/branch + jacoco source, with the absent → "not measured" branch), and the per-module `ModuleTable` (coverage column + failing expand). The old 7-tile row is replaced by a conclusion card + a 3-tile row; verify no tile that carried real data was dropped (pass/fail/skip moved into the conclusion card; runner-exec total is the card headline).

- [ ] **Step 4: Run tests + tsc**

Run: `cd webui && npm test -- src/components/session/TestDetailPage.test.tsx && npx tsc -p tsconfig.app.json --noEmit` — PASS / clean. Keep the existing coverage-tile tests green (branch-only headline, absent → not measured).

- [ ] **Step 5: Commit**

```bash
git add webui/src/components/session/TestDetailPage.tsx webui/src/components/session/TestDetailPage.test.tsx
git commit -m "feat(webui): conclusion-first Test facet, token-aligned"
```

---

## Task 4: Full verification + live visual check

- [ ] **Step 1: Full suite** — `cd webui && npm test` — all green.
- [ ] **Step 2: Type-check** — `cd webui && npx tsc -p tsconfig.app.json --noEmit` — clean.
- [ ] **Step 3: No `static/` staged** — `git -C .. diff --cached --name-only -- src/sag/web/static` — empty.
- [ ] **Step 4: No "Overview" copy remains** — `cd webui && grep -rn "on Overview\|see the project-level" src/components/session` — no matches.
- [ ] **Step 5: Live visual (maintainer or via dev-server screenshot)** — drive the Build and Test facets for a single-module project (kafka) and a multi-module project (caffeine/commons with coverage): confirm the conclusion card, the stats row, the per-module table (failures-first), the coverage column (stacked line/branch bars), and the failing-method expand all render with token colors and no fake zeroes; confirm the single-module note no longer says "Overview".

---

## Self-Review

**Spec coverage (Phase 4 = Build & Test facets):**
- Conclusion-first Build summary (state/system/tool/time/command/artifact + class/JAR + module counts) → Task 2.
- Conclusion-first Test summary (pass/total + rate + bar + pass/fail/skip + unique methods + modules-with-fails + coverage tile) → Task 3.
- Features A/B preserved (per-module tables, coverage column, failing expand, no fake zeroes) → Tasks 1–3 + invariants.
- Token alignment → Tasks 1–3.
- Stale "Overview" copy fixed → Tasks 2, 3 (+ Step 4 grep gate).

**Type/name consistency:** `fmtNum`, `Tile`, `CoverageTile`, `coverageTone`, `ConclusionCard`, `KV`, `Stat` are defined within each file; `ModuleTable`/`TestBar`/`statusMeta` reused with unchanged signatures. `BuildDetailPage`/`TestDetailPage` props unchanged (`detail`, optional `onBack`) — the facets + any remaining callers keep working.

**Placeholder scan:** none — complete component code; tests have real assertions.

**Risk notes:**
- The Test facet drops the old separate Passed/Failed/Skipped/Runner-exec tiles into the conclusion card — verify the headline total uses `Math.max(total, pass+fail)` (handles partial data) and that no real value is lost.
- `ModuleTable` coverage bar fill switches from hex to `var(--status-*)` — confirm the inline `style={{ background: covColor(rate) }}` still renders (CSS vars are valid in inline styles).
- This is a visual restyle; tests lock structure/data but not pixels — the live check (Step 5) is required before merge.
