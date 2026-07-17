import type { TestSummary } from "@/api/types"
import { StatusBadge } from "@/components/common/Badge"
import { Card } from "@/components/common/Card"
import { Tooltip } from "@/components/ui/tooltip"

// Method coverage = executed / declared methods. Only a valid coverage figure
// when the static catalog is a complete denominator (rate in (0, 100]); a rate
// above 100% means the catalog undercounts, not real >100% coverage.
function isValidCoverage(rate?: number | null): rate is number {
  return typeof rate === "number" && Number.isFinite(rate) && rate >= 0 && rate <= 100
}

function barWidth(value: number, total: number): string {
  return `${Math.max(0, Math.min(100, (value / total) * 100))}%`
}
function num(n?: number | null): string | null {
  return typeof n === "number" && Number.isFinite(n) ? n.toLocaleString() : null
}
function pct(n?: number | null): string | null {
  return typeof n === "number" && Number.isFinite(n) ? `${n.toFixed(1)}%` : null
}

export function TestCard({
  test,
  onOpenDetail,
}: {
  test: TestSummary
  onOpenDetail?: () => void
}) {
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
  const rawExecutions = num(test.rawExecutions)
  const snapshotUniquePrimary = test.rawExecutions != null

  if (!hasTests) {
    return (
      <Card className="p-4">
        <div className="flex items-center justify-between gap-3">
          <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">Tests</div>
          <StatusBadge status={test.state} />
        </div>
        <div className="mt-2 text-[14px] font-semibold text-foreground">No test evidence</div>
        <div className="mt-1 font-mono text-[11px] text-muted-foreground">Runner XML not found</div>
      </Card>
    )
  }

  return (
    <Card className="p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">Tests</div>
        <StatusBadge status={test.state} />
      </div>

      <div className="mt-2 text-[22px] font-semibold tabular-nums text-foreground">
        {passRate ? `${passRate} passed` : `${test.pass.toLocaleString()} passed`}
      </div>

      <div
        className="mt-2 flex h-1.5 overflow-hidden rounded-full bg-muted"
        aria-label={snapshotUniquePrimary ? "unique test pass rate" : "runner pass rate"}
      >
        <div className="h-full bg-status-success" style={{ width: barWidth(test.pass, test.total) }} />
        <div className="h-full bg-status-failed" style={{ width: barWidth(failed, test.total) }} />
      </div>

      <div className="mt-2.5 space-y-1 font-mono text-[11px] text-muted-foreground">
        <div>
          {test.pass.toLocaleString()} / {test.total.toLocaleString()}{" "}
          {snapshotUniquePrimary ? "unique tests" : "runner executions"} passed
        </div>
        <div>
          <span className={failed ? "text-status-failed" : ""}>{failed} failed</span>
          {errors ? <span className="text-status-failed">{" · "}{errors} errors</span> : null}
          {" · "}{test.skip} skipped
          {test.reportFileCount != null ? <> · {test.reportFileCount.toLocaleString()} XML reports</> : null}
        </div>
        {rawExecutions ? (
          <div className="text-muted-foreground">
            {rawExecutions} raw executions (diagnostic)
          </div>
        ) : null}
        {uniqueTotal && !snapshotUniquePrimary ? (
          <div className="text-muted-foreground">
            {uniqueTotal} unique methods{methodCoverage ? ` · ${methodCoverage} method coverage` : ""}
          </div>
        ) : null}
      </div>

      {(test.conflicts ?? []).length ? (
        <div className="mt-2 inline-flex rounded bg-status-attention-soft px-1.5 py-0.5 font-mono text-[10px] text-status-attention">
          {(test.conflicts ?? []).length} conflict{(test.conflicts ?? []).length > 1 ? "s" : ""}
        </div>
      ) : null}

      {onOpenDetail ? (
        <Tooltip className="mt-3" label="Open the full test breakdown">
          <button
            aria-label="Open test details"
            className="inline-flex items-center gap-1 font-mono text-[10.5px] text-muted-foreground transition-colors hover:text-foreground"
            onClick={onOpenDetail}
            type="button"
          >
            Details <span aria-hidden>→</span>
          </button>
        </Tooltip>
      ) : null}
    </Card>
  )
}
