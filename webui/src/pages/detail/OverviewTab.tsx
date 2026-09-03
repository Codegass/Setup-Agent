import type { ExecutionSessionDetail } from "@/api/types"
import { ModuleTable } from "@/components/session/ModuleTable"
import { NeedsAttention } from "@/components/session/NeedsAttention"
import {
  presentBuild,
  presentDataNotes,
  presentDiagnostics,
  presentTestAccounting,
  presentTestRun,
} from "@/evidencePresentation"
import { cn } from "@/lib/utils"

function pct1(n: number): string {
  return `${n.toFixed(1).replace(/\.0$/, "")}%`
}

function progressText(progress: Record<string, number> | undefined): string | null {
  if (!progress) return null
  const done = Number.isFinite(progress.done) ? progress.done : null
  const total = Number.isFinite(progress.total) ? progress.total : 0
  if (done === null || total <= 0) return null
  return `${done} / ${total}`
}

function Tile({
  label,
  value,
  sub,
  valueClass,
}: {
  label: string
  value: string
  sub?: string | null
  valueClass?: string
}) {
  return (
    <div className="min-w-0 rounded-[10px] border border-border bg-card px-4 py-3.5">
      <div className="font-mono text-[11px] uppercase tracking-[0.06em] text-muted-foreground">{label}</div>
      <div className={cn("mt-1 text-[27px] font-bold leading-[1.1] tracking-[-0.02em] text-foreground", valueClass)}>
        {value}
      </div>
      {sub ? <div className="mt-1 text-[12px] leading-relaxed text-muted-foreground">{sub}</div> : null}
    </div>
  )
}

/**
 * Overview tab: the always-visible agent goal button (jumps to Flow), build and
 * test summaries, the per-module overview table,
 * and the "needs attention" card. Markup/styling mirrors WorkbenchDetail.dc.html
 * lines 100–200 (the Overview block in the AFTER template).
 */
export function OverviewTab({
  detail,
  onOpenFlow,
}: {
  detail: ExecutionSessionDetail
  onOpenFlow: () => void
}) {
  const test = detail.test
  const ms = detail.moduleSummary
  const modules = detail.modules ?? []
  const layers = test.evidenceLayers?.tests
  const run = presentTestRun(test)
  const accounting = presentTestAccounting(test)
  const diagnostics = presentDiagnostics(layers)
  const dataNotes = presentDataNotes(test.conflicts)

  const goal = detail.context?.trunk.goal
  const progress = progressText(detail.context?.trunk.progress)
  const build = presentBuild(detail.build, detail.rates)
  const hasModuleMetrics = ms != null || modules.length > 0
  const reportDeliveryCopy = detail.reportDeliveryStatus === "failed"
    ? {
        title: "Final report not delivered",
        body: "The sealed results above are available, but the final report could not be delivered.",
      }
    : detail.reportDeliveryStatus === "skipped"
      ? {
          title: "Final report delivery skipped",
          body: "The sealed results above are available. This run did not send a final report.",
        }
      : null
  const partialResult = detail.canonicalVerdict === "partial"
    || detail.status.trim().toLowerCase() === "partial"
    || test.state.trim().toLowerCase() === "partial"

  return (
    <div>
      {goal ? (
        <button
          type="button"
          onClick={onOpenFlow}
          className="mb-3 flex w-full items-center gap-3 rounded-[10px] border border-border bg-card px-4 py-2.5 text-left"
        >
          <span className="shrink-0 font-mono text-[10px] uppercase tracking-[0.06em] text-muted-foreground">Goal</span>
          <span className="min-w-0 flex-1 truncate text-[13px] leading-snug text-foreground">{goal}</span>
          {progress ? <span className="shrink-0 font-mono text-[12px] text-muted-foreground">{progress}</span> : null}
          <span className="shrink-0 text-[12px] font-semibold text-primary">View flow →</span>
        </button>
      ) : null}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Tile
          label="Build"
          value={build.value}
          sub={build.summary}
          valueClass={build.valueClass}
        />
        <Tile
          label="Tests"
          value={run.stateLabel === "Passed" ? "Success" : run.stateLabel}
          sub={accounting.summary}
          valueClass={run.valueClass}
        />
      </div>

      {ms?.lineRate != null || ms?.branchRate != null ? (
        <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
          {ms?.lineRate != null ? (
            <Tile
              label="Line coverage"
              value={pct1(ms.lineRate)}
              sub={
                ms.lineCovered != null && ms.lineTotal != null
                  ? `${ms.lineCovered.toLocaleString()} / ${ms.lineTotal.toLocaleString()} lines`
                  : null
              }
            />
          ) : null}
          {ms?.branchRate != null ? (
            <Tile
              label="Branch coverage"
              value={pct1(ms.branchRate)}
              sub={
                ms.branchCovered != null && ms.branchTotal != null
                  ? `${ms.branchCovered.toLocaleString()} / ${ms.branchTotal.toLocaleString()} branches`
                  : null
              }
            />
          ) : null}
        </div>
      ) : null}

      {layers && diagnostics.hasData ? (
        <section className="mt-3 flex flex-col gap-3 rounded-[10px] border border-border bg-muted/40 px-4 py-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <h2 className="text-[13px] font-semibold text-foreground">Diagnostic observations</h2>
            <p className="mt-0.5 max-w-[72ch] text-[12px] leading-relaxed text-muted-foreground">
              {diagnostics.summary}
            </p>
            {diagnostics.breakdown ? (
              <p className="mt-1 font-mono text-[11px] text-muted-foreground">{diagnostics.breakdown}</p>
            ) : null}
          </div>
          <div className="shrink-0 font-mono text-[20px] font-semibold tabular-nums text-foreground">
            {diagnostics.value}
          </div>
        </section>
      ) : null}

      {reportDeliveryCopy ? (
        <section className="mt-3 rounded-[10px] border border-status-attention-border bg-status-attention-soft/40 px-4 py-3">
          <h2 className="text-[13px] font-semibold text-status-attention">{reportDeliveryCopy.title}</h2>
          <p className="mt-0.5 text-[12px] leading-relaxed text-foreground">{reportDeliveryCopy.body}</p>
        </section>
      ) : null}

      {dataNotes.length > 0 ? (
        <details className="mt-3 rounded-[10px] border border-border bg-card px-4 py-3">
          <summary className="cursor-pointer select-none text-[12px] font-semibold text-muted-foreground hover:text-foreground">
            {partialResult ? "Why this result is partial" : "Data notes"}
          </summary>
          <ul className="mt-2 space-y-1.5 pl-4 text-[12px] leading-relaxed text-muted-foreground">
            {dataNotes.map((note) => <li key={note} className="list-disc">{note}</li>)}
          </ul>
        </details>
      ) : null}

      {modules.length > 0 ? (
        <section className="mt-5 overflow-hidden rounded-xl border border-border bg-card">
          <div className="flex items-center justify-between border-b border-border px-4 py-3">
            <h2 className="text-[14px] font-bold text-foreground">Per-module breakdown</h2>
            <span className="font-mono text-[12px] text-muted-foreground">
              {ms?.modulesTotal ?? modules.length} modules
              {ms?.coverageSource ? ` · ${ms.coverageSource}` : ""}
            </span>
          </div>
          <ModuleTable modules={modules} variant="overview" />
        </section>
      ) : !hasModuleMetrics ? (
        <section className="mt-5 rounded-xl border border-dashed border-border bg-muted/30 px-4 py-3">
          <h2 className="text-[13px] font-semibold text-foreground">Module details unavailable</h2>
          <p className="mt-0.5 text-[12px] leading-relaxed text-muted-foreground">
            Detailed module metrics were not produced for this run. The build result above remains the recorded result.
          </p>
        </section>
      ) : null}

      <div className="mt-5">
        <NeedsAttention modules={modules} warnings={detail.build.warnings ?? []} />
      </div>
    </div>
  )
}
