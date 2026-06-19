import type { ExecutionSessionDetail } from "@/api/types"
import { ModuleTable } from "@/components/session/ModuleTable"
import { NeedsAttention } from "@/components/session/NeedsAttention"
import { cn } from "@/lib/utils"

function pct1(n: number): string {
  return `${n.toFixed(1).replace(/\.0$/, "")}%`
}

function passRate(pass: number, total: number): string | null {
  return total > 0 ? pct1((pass / total) * 100) : null
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
    <div className="rounded-[10px] border border-slate-200 bg-white px-4 py-3.5">
      <div className="font-mono text-[11px] uppercase tracking-[0.06em] text-slate-400">{label}</div>
      <div className={cn("mt-1 text-[27px] font-bold leading-[1.1] tracking-[-0.02em] text-slate-800", valueClass)}>
        {value}
      </div>
      {sub ? <div className="mt-1 font-mono text-[12px] text-slate-500">{sub}</div> : null}
    </div>
  )
}

/**
 * Overview tab: the always-visible agent goal button (jumps to Flow), KPI tiles
 * synthesized from the test/build/module summaries, the per-module overview table,
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
  const singleModule = ms?.singleModule ?? modules.length <= 1

  const rate = passRate(test.pass, test.total)
  const passSub = [
    test.pass ? `${test.pass.toLocaleString()} passed` : null,
    test.skip ? `${test.skip.toLocaleString()} skipped` : null,
  ]
    .filter(Boolean)
    .join(" · ")

  const failingModules = modules.filter((m) => (m.failingCount ?? 0) > 0).length
  const failSub = !singleModule && failingModules > 0
    ? `across ${failingModules} module${failingModules > 1 ? "s" : ""}`
    : null

  const goal = detail.context?.trunk.goal
  const progress = progressText(detail.context?.trunk.progress)

  return (
    <div>
      {goal ? (
        <button
          type="button"
          onClick={onOpenFlow}
          className="mb-3 flex w-full items-center gap-3 rounded-[10px] border border-slate-200 bg-white px-4 py-2.5 text-left"
        >
          <span className="shrink-0 font-mono text-[10px] uppercase tracking-[0.06em] text-slate-400">Goal</span>
          <span className="min-w-0 flex-1 truncate text-[13px] leading-snug text-slate-700">{goal}</span>
          {progress ? <span className="shrink-0 font-mono text-[12px] text-slate-400">{progress}</span> : null}
          <span className="shrink-0 text-[12px] font-semibold text-primary">View flow →</span>
        </button>
      ) : null}

      <div className="grid grid-cols-3 gap-3">
        <Tile
          label="Pass rate"
          value={rate ?? "—"}
          sub={passSub || null}
          valueClass="text-status-success"
        />
        <Tile
          label="Failing tests"
          value={String(test.fail ?? 0)}
          sub={failSub}
          valueClass={test.fail > 0 ? "text-status-failed" : undefined}
        />
        {!singleModule && ms ? (
          <Tile
            label="Modules built"
            value={`${ms.modulesBuilt} / ${ms.modulesTotal}`}
            sub={ms.modulesFailed > 0 ? `${ms.modulesFailed} failed` : null}
            valueClass="text-status-attention"
          />
        ) : (
          <Tile
            label="Build"
            value={detail.build.state === "success" ? "Passed" : detail.build.state === "failed" || detail.build.state === "failure" ? "Failed" : "—"}
            sub={detail.build.tool || null}
            valueClass={detail.build.state === "success" ? "text-status-success" : "text-status-failed"}
          />
        )}
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
        {detail.build.time ? (
          <Tile label="Build time" value={detail.build.time} sub={detail.build.note || null} />
        ) : null}
      </div>

      {!singleModule && modules.length > 0 ? (
        <section className="mt-5 overflow-hidden rounded-xl border border-slate-200 bg-white">
          <div className="flex items-center justify-between border-b border-slate-100 px-4 py-3">
            <h2 className="text-[14px] font-bold text-slate-800">Per-module breakdown</h2>
            <span className="font-mono text-[12px] text-slate-400">
              {ms?.modulesTotal ?? modules.length} modules
              {ms?.coverageSource ? ` · ${ms.coverageSource}` : ""}
            </span>
          </div>
          <ModuleTable modules={modules} variant="overview" />
        </section>
      ) : null}

      <div className="mt-5">
        <NeedsAttention modules={modules} warnings={detail.build.warnings ?? []} />
      </div>
    </div>
  )
}
