import { ArrowLeft } from "lucide-react"
import type * as React from "react"

import type { ExecutionSessionDetail } from "@/api/types"
import { StatusBadge } from "@/components/common/Badge"
import { Button } from "@/components/common/Button"
import { Card } from "@/components/common/Card"

import { ModuleTable } from "./ModuleTable"

function fmtNum(n?: number | null): string {
  // Real 0 stays "0"; a genuinely-absent value renders "—" (no fake zeroes).
  return typeof n === "number" && Number.isFinite(n) ? n.toLocaleString() : "—"
}

function Tile({ label, value, tone }: { label: string; value: React.ReactNode; tone?: string }) {
  return (
    <div className="rounded-lg border border-slate-200 px-3 py-2">
      <div className={`text-[22px] font-semibold tabular-nums ${tone ?? "text-slate-900"}`}>{value}</div>
      <div className="font-mono text-[10px] uppercase tracking-[0.1em] text-slate-500">{label}</div>
    </div>
  )
}

export function BuildDetailPage({
  detail, onBack,
}: { detail: ExecutionSessionDetail; onBack: () => void }) {
  const b = detail.build
  const s = detail.moduleSummary
  const single = s?.singleModule ?? (detail.modules?.length ?? 0) <= 1
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <Button onClick={onBack} size="sm" type="button" variant="ghost">
          <ArrowLeft size={14} /> Back
        </Button>
        <StatusBadge status={b.state} />
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-6">
        <Tile label="Modules" value={fmtNum(s?.modulesTotal)} />
        <Tile label="Built" value={fmtNum(s?.modulesBuilt)} tone="text-emerald-700" />
        <Tile label="Failed" value={fmtNum(s?.modulesFailed)} tone="text-red-600" />
        <Tile label="Skipped" value={fmtNum(s?.modulesSkipped)} />
        <Tile label="Classes" value={fmtNum(b.classCount)} />
        <Tile label="JARs" value={fmtNum(b.jarCount)} />
      </div>
      <Card className="p-4">
        {single ? (
          <div className="font-mono text-[12px] text-slate-500">Single module project — see the project-level build summary on Overview.</div>
        ) : (
          <ModuleTable modules={detail.modules ?? []} variant="build" />
        )}
      </Card>
    </div>
  )
}
