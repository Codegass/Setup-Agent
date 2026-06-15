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

function Tile({ label, value, tone, dashed }: {
  label: string; value: React.ReactNode; tone?: string; dashed?: boolean
}) {
  return (
    <div className={`rounded-lg border px-3 py-2 ${dashed ? "border-dashed bg-slate-50" : "border-slate-200"}`}>
      <div className={`text-[22px] font-semibold tabular-nums ${tone ?? "text-slate-900"}`}>{value}</div>
      <div className="font-mono text-[10px] uppercase tracking-[0.1em] text-slate-500">{label}</div>
    </div>
  )
}

export function TestDetailPage({
  detail, onBack,
}: { detail: ExecutionSessionDetail; onBack: () => void }) {
  const t = detail.test
  const s = detail.moduleSummary
  const single = s?.singleModule ?? (detail.modules?.length ?? 0) <= 1
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <Button onClick={onBack} size="sm" type="button" variant="ghost">
          <ArrowLeft size={14} /> Back
        </Button>
        <StatusBadge status={t.state} />
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-7">
        <Tile label="Runner exec" value={fmtNum(t.total)} />
        <Tile label="Passed" value={fmtNum(t.pass)} tone="text-emerald-700" />
        <Tile label="Failed" value={fmtNum(t.fail)} tone="text-red-600" />
        <Tile label="Skipped" value={fmtNum(t.skip)} />
        <Tile label="Unique methods" value={fmtNum(t.uniqueTotal)} />
        <Tile label="Modules w/ fails" value={fmtNum(s?.modulesWithTestFailures)} tone="text-red-600" />
        <Tile label="Coverage" value={<>— <span className="rounded bg-amber-50 px-1 font-mono text-[9px] text-amber-700">Feature B</span></>} dashed />
      </div>
      <Card className="p-4">
        {single ? (
          <div className="font-mono text-[12px] text-slate-500">Single module project — see the project-level test summary on Overview.</div>
        ) : (
          <ModuleTable modules={detail.modules ?? []} variant="test" />
        )}
      </Card>
    </div>
  )
}
