import { ChevronDown } from "lucide-react"
import { useState } from "react"

import type { TestSummary } from "@/api/types"
import { StatusBadge } from "@/components/common/Badge"
import { Card } from "@/components/common/Card"
import { cn } from "@/lib/utils"

import { TestDetails, isValidCoverage } from "./TestDetails"

function barWidth(value: number, total: number): string {
  return `${Math.max(0, Math.min(100, (value / total) * 100))}%`
}
function num(n?: number | null): string | null {
  return typeof n === "number" && Number.isFinite(n) ? n.toLocaleString() : null
}
function pct(n?: number | null): string | null {
  return typeof n === "number" && Number.isFinite(n) ? `${n.toFixed(1)}%` : null
}

export function TestCard({ test }: { test: TestSummary }) {
  const [open, setOpen] = useState(false)
  const hasTests = test.total > 0
  const errors = test.errors ?? 0
  // Errors are failures for display purposes: fold them into the red bar and the
  // "failed" line so the card body never contradicts a non-success badge. The
  // markdown read path already folds errors into fail; this keeps both paths in
  // agreement. Errors are also surfaced explicitly below.
  const failed = test.fail + errors
  const passRate = pct(test.passRate) ?? (hasTests ? `${((test.pass / test.total) * 100).toFixed(1)}%` : null)
  // "Method coverage" is only a meaningful figure when the static catalog is a
  // complete denominator (rate <= 100). When more unique methods ran than were
  // statically declared (e.g. parameterized/inherited tests the catalog missed),
  // the rate exceeds 100% and reads as a bug -- so we omit it on the card and
  // explain the discrepancy in the details panel instead.
  const methodCoverage = isValidCoverage(test.methodExecutionRate) ? pct(test.methodExecutionRate) : null
  const uniqueTotal = num(test.uniqueTotal)

  if (!hasTests) {
    return (
      <Card className="p-4">
        <div className="flex items-center justify-between gap-3">
          <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-slate-500">Tests</div>
          <StatusBadge status={test.state} />
        </div>
        <div className="mt-2 text-[14px] font-semibold text-slate-700">No test evidence</div>
        <div className="mt-1 font-mono text-[11px] text-slate-400">Runner XML not found</div>
      </Card>
    )
  }

  return (
    <Card className="p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-slate-500">Tests</div>
        <StatusBadge status={test.state} />
      </div>

      <div className="mt-2 text-[22px] font-semibold tabular-nums text-slate-900">
        {passRate ? `${passRate} passed` : `${test.pass.toLocaleString()} passed`}
      </div>

      <div className="mt-2 flex h-1.5 overflow-hidden rounded-full bg-slate-100" aria-label="runner pass rate">
        <div className="h-full bg-emerald-500" style={{ width: barWidth(test.pass, test.total) }} />
        <div className="h-full bg-red-500" style={{ width: barWidth(failed, test.total) }} />
      </div>

      <div className="mt-2.5 space-y-1 font-mono text-[11px] text-slate-600">
        <div>{test.pass.toLocaleString()} / {test.total.toLocaleString()} runner executions passed</div>
        <div>
          <span className={failed ? "text-red-600" : ""}>{failed} failed</span>
          {errors ? <span className="text-red-600">{" · "}{errors} errors</span> : null}
          {" · "}{test.skip} skipped
          {test.reportFileCount != null ? <> · {test.reportFileCount.toLocaleString()} XML reports</> : null}
        </div>
        {uniqueTotal ? (
          <div className="text-slate-500">
            {uniqueTotal} unique methods{methodCoverage ? ` · ${methodCoverage} method coverage` : ""}
          </div>
        ) : null}
      </div>

      {(test.conflicts ?? []).length ? (
        <div className="mt-2 inline-flex rounded bg-amber-50 px-1.5 py-0.5 font-mono text-[10px] text-amber-700">
          {(test.conflicts ?? []).length} conflict{(test.conflicts ?? []).length > 1 ? "s" : ""}
        </div>
      ) : null}

      <button
        aria-expanded={open}
        className="mt-3 inline-flex items-center gap-1 font-mono text-[10.5px] text-slate-500 transition-colors hover:text-slate-700"
        onClick={() => setOpen((v) => !v)}
        type="button"
      >
        <ChevronDown aria-hidden className={cn("transition-transform", open && "rotate-180")} size={12} />
        {open ? "Hide details" : "Details"}
      </button>
      {open ? <div className="mt-2 border-t border-slate-100 pt-3"><TestDetails test={test} /></div> : null}
    </Card>
  )
}
