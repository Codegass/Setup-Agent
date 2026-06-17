# Plan 2 — Build Facet Layout + Stats Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (inline). Steps use checkbox (`- [ ]`) syntax.

**Goal:** Restyle the Build facet to the prototype's **two-card grid** (left = conclusion with Tool/Time/Command/Artifact; right = "Outputs" with classes/JARs + warnings), and add a **module success rate** (+ counts) for multi-module projects — matching `docs/Setup Agent Web UI/workbench/sections.jsx BuildBody`.

**Architecture:** Presentation-layer only, one file (`BuildDetailPage.tsx`) + its test. Consumes existing `BuildSummary`/`ModuleRollup` fields. Build time/command/artifact populate automatically once Plan 3 (backend capture) lands; until then they render only when present (KV rows hide absent/`unknown`).

**Tech Stack:** React + TS (Vite), Tailwind v4 (Phase 1 tokens), Vitest + Testing Library, lucide-react.

**Branch:** `feature/webui-workbench-redesign` (even with main).

**Project policy:** no `npm run build`; never stage `static/`; force-add docs; exact paths; no `Co-Authored-By`.

**Per-task verification:** `cd webui && npm test -- <path>`; `cd webui && npx tsc -p tsconfig.app.json --noEmit`.

---

## Task 1: Two-card Build layout + Outputs card + success rate

**Files:** Modify `webui/src/components/session/BuildDetailPage.tsx`, `webui/src/components/session/BuildDetailPage.test.tsx`

- [ ] **Step 1: Write the failing tests**

Append to `webui/src/components/session/BuildDetailPage.test.tsx`:

```tsx
it("shows an Outputs card with classes/JARs and warnings", () => {
  render(<BuildDetailPage detail={{
    build: { state: "success", system: "Maven", tool: "Maven 3.9.6", time: "47.2s",
             note: "clean package", artifact: "target/commons-cli-1.6.0.jar",
             classCount: 115, jarCount: 1, warnings: ["2 deprecation warnings in HelpFormatter.java"] },
    moduleSummary: { singleModule: true },
    modules: [],
  } as any} />)
  expect(screen.getByText(/outputs/i)).toBeInTheDocument()
  expect(screen.getByText("115")).toBeInTheDocument()
  expect(screen.getByText(/clean package/)).toBeInTheDocument()           // conclusion KV
  expect(screen.getByText(/HelpFormatter\.java/)).toBeInTheDocument()     // warning
})

it("shows the module success rate for a multi-module build", () => {
  render(<BuildDetailPage detail={{
    build: { state: "success", system: "maven", classCount: 1300, jarCount: 12 },
    moduleSummary: { modulesTotal: 24, modulesBuilt: 21, modulesFailed: 1, modulesSkipped: 2,
                     buildSystems: ["maven"], singleModule: false },
    modules: [{ name: "core", path: "core", buildStatus: "success", buildSource: "reactor" }],
  } as any} />)
  expect(screen.getByText("24")).toBeInTheDocument()        // Modules stat
  expect(screen.getByText(/88%/)).toBeInTheDocument()        // success rate 21/24
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd webui && npm test -- src/components/session/BuildDetailPage.test.tsx` — FAIL (no Outputs card / no success rate).

- [ ] **Step 3: Rewrite BuildDetailPage to the two-card layout**

Replace `webui/src/components/session/BuildDetailPage.tsx` with:

```tsx
import { ArrowLeft, Check, Clock, X } from "lucide-react"
import type * as React from "react"

import type { BuildSummary, ExecutionSessionDetail, ModuleRollup } from "@/api/types"
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

function MonoLabel({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div className={cn("font-mono text-[10px] uppercase tracking-[0.12em] text-slate-500", className)}>
      {children}
    </div>
  )
}

function KV({ k, v }: { k: string; v?: string | null }) {
  // Drop empty and placeholder "unknown" values so the card carries only signal.
  if (!v || v.trim().toLowerCase() === "unknown") {
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
  const system = build.system ?? build.tool
  const showBadge = Boolean(system) && system!.trim().toLowerCase() !== "unknown"
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
        {showBadge ? (
          <Badge mono tone={ok ? "green" : bad ? "red" : "neutral"}>
            {system}
          </Badge>
        ) : null}
      </div>
      <div className="mt-2">
        <KV k="Tool" v={build.tool} />
        <KV k="Time" v={build.time} />
        <KV k="Command" v={build.note} />
        <KV k="Artifact" v={build.artifact} />
      </div>
    </Card>
  )
}

function OutputsCard({ build }: { build: BuildSummary }) {
  const warnings = build.warnings ?? []
  return (
    <Card className="p-4">
      <MonoLabel>Outputs</MonoLabel>
      <div className="mt-2 grid grid-cols-2 gap-2">
        <Stat label="classes" value={fmtNum(build.classCount)} />
        <Stat label="JARs" value={fmtNum(build.jarCount)} />
      </div>
      {warnings.length ? (
        <div className="mt-3">
          <MonoLabel className="text-status-attention">
            {warnings.length} warning{warnings.length > 1 ? "s" : ""}
          </MonoLabel>
          <ul className="mt-1.5 space-y-1">
            {warnings.map((w) => (
              <li key={w} className="text-[12px] text-slate-500">· {w}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </Card>
  )
}

function successRate(s?: ModuleRollup | null): number | null {
  const total = s?.modulesTotal ?? 0
  const built = s?.modulesBuilt ?? 0
  return total > 0 ? Math.round((built / total) * 100) : null
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
  const rate = successRate(s)

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

      <div className="grid gap-4 sm:grid-cols-2">
        <ConclusionCard build={b} />
        <OutputsCard build={b} />
      </div>

      {single ? (
        <Card className="p-4">
          <div className="font-mono text-[12px] text-slate-500">
            Single-module project — the conclusion and outputs above cover the whole build.
          </div>
        </Card>
      ) : (
        <>
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
          <Card className="overflow-hidden">
            <ModuleTable modules={detail.modules ?? []} variant="build" />
          </Card>
        </>
      )}
    </div>
  )
}
```

- [ ] **Step 4: Run tests + tsc**

Run: `cd webui && npm test -- src/components/session/BuildDetailPage.test.tsx && npx tsc -p tsconfig.app.json --noEmit`
Expected: PASS / clean. Existing tests still hold: "24" is the Modules stat; the absent-counts test gets its two "—" from the Outputs card; the embedded-mode (no back button) test is unchanged; the single-module note has no "Overview".

- [ ] **Step 5: Commit**

```bash
git add webui/src/components/session/BuildDetailPage.tsx webui/src/components/session/BuildDetailPage.test.tsx
git commit -m "feat(webui): two-card Build facet (conclusion + outputs) with module success rate"
```

---

## Task 2: Full verification + live check

- [ ] **Step 1: Full suite** — `cd webui && npm test` — all green.
- [ ] **Step 2: Type-check** — `cd webui && npx tsc -p tsconfig.app.json --noEmit` — clean.
- [ ] **Step 3: No `static/` staged** — `git -C .. diff --cached --name-only -- src/sag/web/static` — empty.
- [ ] **Step 4: Live** — drive Chrome (dev server proxied to demo): the Build facet shows the conclusion card (left) beside the Outputs card (right) with classes/JARs (+ warnings when present); on a multi-module project the success-rate stat + module table render. Time/Command/Artifact appear once Plan 3 populates them; absent values stay hidden (no "—" noise in the KV).

---

## Self-Review

**Spec coverage (B1 + B2):** two-card layout (conclusion + Outputs) → Task 1; module success rate + counts (multi-module) → Task 1. Single-module keeps the calm note (no "Overview").

**Type/name consistency:** `ConclusionCard`, `OutputsCard`, `KV`, `Stat`, `MonoLabel`, `successRate`, `fmtNum` defined in-file; `ModuleTable`/`statusMeta`/`Badge` reused unchanged. Props unchanged (`detail`, optional `onBack`).

**Risk notes:**
- Build Time/Command/Artifact are still backend-empty until Plan 3; the KV rows hide absent values, so the conclusion card looks clean now and fills in after Plan 3 with no further frontend change.
- Existing tests assert "24" (Modules stat, standalone), `/built/i`, "connect:runtime" (ModuleTable), the two "—" (Outputs classes/JARs), and a real "0" — all preserved by the new layout.
