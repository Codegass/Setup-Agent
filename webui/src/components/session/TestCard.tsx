import type { TestSummary } from "@/api/types"
import { StatusBadge } from "@/components/common/Badge"
import { Card } from "@/components/common/Card"

function percent(value: number, total: number): string {
  return `${Math.max(0, Math.min(100, (value / total) * 100))}%`
}

export function TestCard({ test }: { test: TestSummary }) {
  const hasTests = test.total > 0
  const passRate = hasTests ? Math.round((test.pass / test.total) * 100) : 0

  return (
    <Card className="p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-slate-500">
          Tests
        </div>
        <StatusBadge status={test.state} />
      </div>
      <div className="mt-2 flex items-baseline gap-2">
        <span className="text-[26px] font-semibold tabular-nums text-slate-900">
          {hasTests ? test.pass : "-"}
        </span>
        {hasTests ? (
          <span className="text-[13px] text-slate-500">/ {test.total} passed</span>
        ) : (
          <span className="text-[13px] text-slate-500">no tests recorded</span>
        )}
      </div>
      {hasTests ? (
        <div className="mt-2 flex h-1.5 overflow-hidden rounded-full bg-slate-100">
          <div className="h-full bg-emerald-500" style={{ width: percent(test.pass, test.total) }} />
          <div className="h-full bg-red-500" style={{ width: percent(test.fail, test.total) }} />
        </div>
      ) : null}
      <div className="mt-2.5 flex flex-wrap items-center gap-3 font-mono text-[11px]">
        <span className="text-emerald-600">{test.pass} pass</span>
        <span className={test.fail ? "text-red-600" : "text-slate-500"}>{test.fail} fail</span>
        <span className="text-slate-500">{test.skip} skip</span>
        {hasTests ? <span className="text-slate-500">{passRate}% pass rate</span> : null}
      </div>
      {test.note ? <div className="mt-2 text-[12px] text-slate-500">{test.note}</div> : null}
    </Card>
  )
}
