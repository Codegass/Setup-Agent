import { AlertTriangle, Check, Clock, Shield, ShieldAlert, Sparkles, X } from "lucide-react"
import type * as React from "react"

import type { ExecutionSessionDetail } from "@/api/types"
import { Badge, StatusBadge } from "@/components/common/Badge"
import { isUsefulEvidenceStatus, statusMeta } from "@/components/common/status"
import { TestBar } from "@/components/common/TestBar"
import { cn } from "@/lib/utils"

function MonoLabel({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div className={cn("font-mono text-[10px] uppercase tracking-[0.12em] text-slate-500", className)}>
      {children}
    </div>
  )
}

function Tile({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white px-3.5 py-3">
      <MonoLabel>{label}</MonoLabel>
      <div className="mt-1.5">{children}</div>
    </div>
  )
}

export function SummaryBand({ detail }: { detail: ExecutionSessionDetail }) {
  const buildNorm = detail.build.state.trim().toLowerCase()
  const testNorm = detail.test.state.trim().toLowerCase()
  const testTotal = Math.max(detail.test.total, detail.test.pass + detail.test.fail)
  const reportReady = detail.report?.trim().toLowerCase() === "ready"
  const blocker = detail.blocker
  const buildFailed = buildNorm === "failure" || buildNorm === "failed"
  const outcomeBad = Boolean(blocker) && buildFailed

  const callout = outcomeBad
    ? "border-status-failed-border bg-status-failed-soft/60"
    : blocker
      ? "border-status-attention-border bg-status-attention-soft/50"
      : "border-status-success-border bg-status-success-soft/50"
  const iconWrap = outcomeBad
    ? "bg-status-failed-soft text-status-failed"
    : blocker
      ? "bg-status-attention-soft text-status-attention"
      : "bg-status-success-soft text-status-success"
  const outcomeLabelTone = outcomeBad
    ? "text-status-failed"
    : blocker
      ? "text-status-attention"
      : "text-status-success"

  return (
    <div className="space-y-3">
      {/* Partial discovery — runtime artifacts were recovered but data may be incomplete */}
      {detail.partial ? (
        <div className="flex items-start gap-2.5 rounded-xl border border-status-attention-border bg-status-attention-soft/50 px-4 py-3.5">
          <ShieldAlert size={15} className="mt-0.5 shrink-0 text-status-attention" />
          <div className="min-w-0">
            <div className="text-[13px] font-semibold text-status-attention">
              Partially discovered session
            </div>
            <p className="mt-0.5 text-[12.5px] leading-relaxed text-slate-600">
              Some runtime artifacts were recovered, but evidence, context, or file digests may be
              incomplete.
            </p>
          </div>
        </div>
      ) : null}

      {/* Outcome */}
      <div className={cn("flex items-start gap-3 rounded-xl border px-4 py-3.5", callout)}>
        <div className={cn("mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full", iconWrap)}>
          {outcomeBad ? <X size={15} /> : blocker ? <AlertTriangle size={14} /> : <Check size={15} />}
        </div>
        <div className="min-w-0">
          <MonoLabel className={outcomeLabelTone}>Outcome</MonoLabel>
          <p className="mt-0.5 text-[14px] leading-snug text-slate-800" style={{ textWrap: "pretty" }}>
            {detail.outcome}
          </p>
        </div>
      </div>

      {/* Signal tiles */}
      <div className="grid grid-cols-2 gap-2.5 lg:grid-cols-4">
        <Tile label="Build">
          <div className="flex items-center gap-1.5">
            {buildNorm === "success" ? (
              <Check size={15} className="text-status-success" />
            ) : buildFailed ? (
              <X size={15} className="text-status-failed" />
            ) : (
              <Clock size={13} className="text-slate-400" />
            )}
            <span className="text-[13px] font-medium text-slate-700">{statusMeta(detail.build.state).label}</span>
          </div>
          <div className="mt-1 font-mono text-[10px] text-slate-500">
            {detail.build.time ? `${detail.build.tool} · ${detail.build.time}` : detail.build.note}
          </div>
        </Tile>
        <Tile label="Tests">
          {testNorm === "none" || testTotal <= 0 ? (
            <span className="text-[13px] text-slate-500">Not run</span>
          ) : (
            <TestBar fail={detail.test.fail} pass={detail.test.pass} total={testTotal} />
          )}
        </Tile>
        <Tile label="Evidence">
          {isUsefulEvidenceStatus(detail.evidenceStatus) ? (
            <StatusBadge status={detail.evidenceStatus ?? "unknown"} />
          ) : (
            <span className="text-[13px] text-slate-500">—</span>
          )}
        </Tile>
        <Tile label="Report">
          {reportReady ? <Badge tone="green">Ready</Badge> : <span className="text-[13px] text-slate-500">—</span>}
        </Tile>
      </div>

      {/* Why — the blocker, surfaced up front (dormant: read model sets blocker=null today) */}
      {blocker ? (
        <div
          className={cn(
            "rounded-xl border px-4 py-3.5",
            outcomeBad
              ? "border-status-failed-border bg-status-failed-soft/50"
              : "border-status-attention-border bg-status-attention-soft/40",
          )}
        >
          <div className="flex items-center gap-2">
            <Shield size={14} className={outcomeBad ? "text-status-failed" : "text-status-attention"} />
            <MonoLabel className={outcomeBad ? "text-status-failed" : "text-status-attention"}>
              Why · {blocker.title}
            </MonoLabel>
            <Badge className="ml-auto" mono tone={outcomeBad ? "red" : "amber"}>
              {blocker.code}
            </Badge>
          </div>
          <p className="mt-2 text-[13px] leading-relaxed text-slate-700">{blocker.detail}</p>
          <div className="mt-2 flex items-start gap-1.5 text-[12.5px] text-slate-500">
            <Sparkles size={13} className="mt-0.5 shrink-0 text-slate-400" />
            <span>
              <b className="font-medium text-slate-600">Suggested fix —</b> {blocker.hint}
            </span>
          </div>
        </div>
      ) : null}
    </div>
  )
}
