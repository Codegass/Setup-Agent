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
