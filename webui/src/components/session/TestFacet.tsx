import { useState } from "react"

import type { ExecutionSessionDetail } from "@/api/types"
import { Badge } from "@/components/common/Badge"
import { Card } from "@/components/common/Card"
import { TestBar } from "@/components/common/TestBar"

import { FailingCard } from "./FailingCard"
import { ModuleBreakdownDialog } from "./ModuleBreakdownDialog"
import { TestDetailPage } from "./TestDetailPage"

function fmtNum(n?: number | null): string {
  return typeof n === "number" && Number.isFinite(n) ? n.toLocaleString() : "—"
}

// Conclusion-first test summary (prototype workbench/sections.jsx TestBody).
export function TestConclusionCard({ test }: { test: ExecutionSessionDetail["test"] }) {
  const total = Math.max(test.total, test.pass + test.fail)
  // passRate is a percentage (0-100) in this codebase — round, don't re-scale.
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
          <div className="text-[26px] font-semibold tabular-nums text-foreground">
            {fmtNum(test.pass)}
            <span className="text-[16px] font-normal text-muted-foreground"> / {fmtNum(total)}</span>
          </div>
          <div className="mt-0.5 font-mono text-[10px] uppercase tracking-[0.1em] text-muted-foreground">
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
        <span className="text-muted-foreground">{fmtNum(test.skip)} skipped</span>
        {test.note ? <span className="text-muted-foreground">· {test.note}</span> : null}
      </div>
    </Card>
  )
}

export function TestFacet({ detail }: { detail: ExecutionSessionDetail }) {
  const [open, setOpen] = useState(false)
  const s = detail.moduleSummary
  const single = s?.singleModule ?? (detail.modules?.length ?? 0) <= 1
  const moduleCount = s?.modulesTotal ?? detail.modules?.length ?? 0
  const failing = detail.test.failingNames ?? []

  return (
    <div className="space-y-4">
      <TestConclusionCard test={detail.test} />
      <FailingCard names={failing} />
      <button
        className="font-mono text-[11px] text-status-running hover:underline"
        onClick={() => setOpen(true)}
        type="button"
      >
        {single ? "View test details →" : `View per-module breakdown (${moduleCount} modules) →`}
      </button>
      {open ? (
        <ModuleBreakdownDialog
          onClose={() => setOpen(false)}
          title={single ? "Test details" : "Per-module test breakdown"}
        >
          <TestDetailPage detail={detail} />
        </ModuleBreakdownDialog>
      ) : null}
    </div>
  )
}
