import type * as React from "react"

import type { ExecutionSessionDetail, ModuleRollup } from "@/api/types"
import { Card } from "@/components/common/Card"
import { cn } from "@/lib/utils"

import { ModuleTable } from "./ModuleTable"

function fmtNum(n?: number | null): string {
  // Real 0 stays "0"; a genuinely-absent value renders "—" (no fake zeroes).
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

// Per-module test breakdown — the "detail page", shown in a modal from the Test facet.
export function TestDetailPage({ detail }: { detail: ExecutionSessionDetail }) {
  const t = detail.test
  const s = detail.moduleSummary
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Tile label="Unique methods" value={fmtNum(t.uniqueTotal)} />
        <Tile label="Modules w/ fails" value={fmtNum(s?.modulesWithTestFailures)} tone="text-status-failed" />
        <CoverageTile summary={s} />
      </div>
      {(detail.modules?.length ?? 0) > 0 ? (
        <Card className="overflow-hidden">
          <ModuleTable modules={detail.modules ?? []} variant="test" />
        </Card>
      ) : (
        <Card className="p-4">
          <div className="font-mono text-[12px] text-slate-500">
            Single-module project — the coverage and counts above are project-wide.
          </div>
        </Card>
      )}
    </div>
  )
}
