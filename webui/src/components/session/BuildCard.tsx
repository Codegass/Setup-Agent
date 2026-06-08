import { Box, Check, X } from "lucide-react"

import type { BuildSummary } from "@/api/types"
import { StatusBadge } from "@/components/common/Badge"
import { Card } from "@/components/common/Card"

function normalized(status: string): string {
  return status.trim().toLowerCase()
}

export function BuildCard({ build }: { build: BuildSummary }) {
  const state = normalized(build.state)
  const success = state === "success"
  const failure = state === "failure" || state === "failed"
  const label = success ? "BUILD SUCCESS" : failure ? "BUILD FAILURE" : "Build state"
  const meta = [build.tool, build.time].filter(Boolean).join(" / ")

  return (
    <Card className="p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-slate-500">
          Build
        </div>
        <StatusBadge status={build.state} />
      </div>
      <div className="mt-2.5 flex items-center gap-2.5">
        <div
          className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-md ${
            success
              ? "bg-emerald-50 text-emerald-600"
              : failure
                ? "bg-red-50 text-red-600"
                : "bg-slate-100 text-slate-500"
          }`}
        >
          {success ? <Check size={18} /> : failure ? <X size={18} /> : <Box size={16} />}
        </div>
        <div className="min-w-0">
          <div className="truncate text-[14px] font-semibold text-slate-800">{label}</div>
          {meta ? <div className="font-mono text-[11px] text-slate-500">{meta}</div> : null}
        </div>
      </div>
      {build.note ? <div className="mt-3 text-[12px] text-slate-500">{build.note}</div> : null}
      {build.artifact ? (
        <div className="mt-2 flex items-center gap-1.5 font-mono text-[11px] text-slate-500">
          <Box size={12} className="text-slate-500" />
          <span className="truncate">{build.artifact}</span>
        </div>
      ) : null}
    </Card>
  )
}
