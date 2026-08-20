import { Check, Clock, X } from "lucide-react"
import { useState } from "react"
import type * as React from "react"

import type { BuildSummary, ExecutionSessionDetail } from "@/api/types"
import { Badge } from "@/components/common/Badge"
import { Card } from "@/components/common/Card"
import { statusMeta } from "@/components/common/status"
import { cn } from "@/lib/utils"

import { BuildDetailPage } from "./BuildDetailPage"
import { ModuleBreakdownDialog } from "./ModuleBreakdownDialog"

function fmtNum(n?: number | null): string {
  return typeof n === "number" && Number.isFinite(n) ? n.toLocaleString() : "—"
}

function MonoLabel({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div className={cn("font-mono text-[10px] uppercase tracking-[0.12em] text-muted-foreground", className)}>
      {children}
    </div>
  )
}

function KV({ k, v }: { k: string; v?: string | null }) {
  if (!v || ["unknown", "none", "unavailable", "sealed snapshot", "—", "-"].includes(v.trim().toLowerCase())) {
    return null
  }
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-border py-1.5 last:border-b-0">
      <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-muted-foreground">{k}</span>
      <span className="truncate text-right font-mono text-[12px] text-foreground">{v}</span>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-border bg-card px-3 py-2">
      <div className="text-[20px] font-semibold tabular-nums text-foreground">{value}</div>
      <div className="font-mono text-[10px] uppercase tracking-[0.1em] text-muted-foreground">{label}</div>
    </div>
  )
}

function ConclusionCard({ build }: { build: BuildSummary }) {
  const norm = build.state.trim().toLowerCase()
  const ok = norm === "success"
  const bad = norm === "failure" || norm === "failed"
  const partial = norm === "partial"
  const system = build.system ?? build.tool
  const normalizedSystem = system?.trim().toLowerCase()
  const showBadge = Boolean(system)
    && !["unknown", "sealed snapshot", "—", "-"].includes(normalizedSystem ?? "")
  return (
    <Card className="p-4">
      <div className="flex items-center justify-between gap-3">
        <span className="inline-flex items-center gap-2 text-[14px] font-medium text-foreground">
          {ok ? (
            <Check className="text-status-success" size={16} />
          ) : bad ? (
            <X className="text-status-failed" size={16} />
          ) : partial ? (
            <Clock className="text-status-attention" size={15} />
          ) : (
            <Clock className="text-muted-foreground" size={15} />
          )}
          {statusMeta(build.state).label}
        </span>
        {showBadge ? (
          <Badge mono tone={ok ? "green" : bad ? "red" : partial ? "amber" : "neutral"}>
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
  const outputs = [
    build.classCount != null ? { label: "classes", value: build.classCount } : null,
    build.jarCount != null ? { label: "JARs", value: build.jarCount } : null,
  ].filter((item): item is { label: string; value: number } => item !== null)
  return (
    <Card className="p-4">
      <MonoLabel>Outputs</MonoLabel>
      {outputs.length ? (
        <div className={cn("mt-2 grid gap-2", outputs.length > 1 && "grid-cols-2")}>
          {outputs.map((output) => (
            <Stat key={output.label} label={output.label} value={fmtNum(output.value)} />
          ))}
        </div>
      ) : (
        <p className="mt-2 text-[12px] leading-relaxed text-muted-foreground">
          Artifact totals were not measured for this run.
        </p>
      )}
      {warnings.length ? (
        <div className="mt-3">
          <MonoLabel className="text-status-attention">
            {warnings.length} warning{warnings.length > 1 ? "s" : ""}
          </MonoLabel>
          <ul className="mt-1.5 space-y-1">
            {warnings.map((w) => (
              <li key={w} className="text-[12px] text-muted-foreground">· {w}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </Card>
  )
}

export function BuildFacet({ detail }: { detail: ExecutionSessionDetail }) {
  const [open, setOpen] = useState(false)
  const s = detail.moduleSummary
  const moduleRecords = detail.modules ?? []
  const hasModuleMetrics = Boolean(s) || moduleRecords.length > 0
  const single = s?.singleModule ?? moduleRecords.length === 1
  const moduleCount = s?.modulesTotal ?? moduleRecords.length

  return (
    <div className="space-y-4">
      <div className="grid gap-4 sm:grid-cols-2">
        <ConclusionCard build={detail.build} />
        <OutputsCard build={detail.build} />
      </div>
      {hasModuleMetrics ? (
        <button
          className="font-mono text-[11px] text-status-running hover:underline"
          onClick={() => setOpen(true)}
          type="button"
        >
          {single ? "View build details →" : `View per-module breakdown (${moduleCount} modules) →`}
        </button>
      ) : (
        <div className="rounded-lg border border-dashed border-border px-3 py-2.5 text-[12px] text-muted-foreground">
          Detailed module metrics were not produced for this run.
        </div>
      )}
      {open && hasModuleMetrics ? (
        <ModuleBreakdownDialog
          onClose={() => setOpen(false)}
          title={single ? "Build details" : "Per-module build breakdown"}
        >
          <BuildDetailPage detail={detail} />
        </ModuleBreakdownDialog>
      ) : null}
    </div>
  )
}
