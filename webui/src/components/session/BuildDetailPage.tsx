import type * as React from "react"

import type { ExecutionSessionDetail, ModuleRollup } from "@/api/types"
import { Card } from "@/components/common/Card"
import { cn } from "@/lib/utils"

import { ModuleTable } from "./ModuleTable"

function fmtNum(n?: number | null): string {
  // Real 0 stays "0"; a genuinely-absent value renders "—" (no fake zeroes).
  return typeof n === "number" && Number.isFinite(n) ? n.toLocaleString() : "—"
}

function Stat({ label, value, tone }: { label: string; value: React.ReactNode; tone?: string }) {
  return (
    <div className="rounded-lg border border-border bg-card px-3 py-2">
      <div className={cn("text-[20px] font-semibold tabular-nums", tone ?? "text-foreground")}>{value}</div>
      <div className="font-mono text-[10px] uppercase tracking-[0.1em] text-muted-foreground">{label}</div>
    </div>
  )
}

function successRate(s?: ModuleRollup | null): number | null {
  const total = s?.modulesTotal ?? 0
  const built = s?.modulesBuilt ?? 0
  return total > 0 ? Math.round((built / total) * 100) : null
}

// Per-module build breakdown — the "detail page", shown in a modal from the Build facet.
export function BuildDetailPage({ detail }: { detail: ExecutionSessionDetail }) {
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
        <div className="font-mono text-[10px] text-muted-foreground">
          Gradle has no reactor summary — per-module build status is inferred from build outputs (best-effort).
        </div>
      ) : null}
      {(detail.modules?.length ?? 0) > 0 ? (
        <Card className="overflow-hidden">
          <ModuleTable modules={detail.modules ?? []} variant="build" />
        </Card>
      ) : (
        <Card className="p-4">
          <div className="font-mono text-[12px] text-muted-foreground">
            Single-module project — the conclusion and outputs cover the whole build.
          </div>
        </Card>
      )}
    </div>
  )
}
