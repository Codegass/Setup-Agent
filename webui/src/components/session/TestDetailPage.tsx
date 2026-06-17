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

function ConclusionCard({ test }: { test: ExecutionSessionDetail["test"] }) {
  const total = Math.max(test.total, test.pass + test.fail)
  // passRate is a percentage (0-100) in this codebase (e.g. 99.9), matching
  // lineRate/branchRate — round to one decimal, don't re-scale.
  const rate =
    test.passRate != null
      ? Math.round(test.passRate * 10) / 10
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
