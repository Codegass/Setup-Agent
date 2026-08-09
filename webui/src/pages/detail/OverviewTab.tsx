import type { EvidenceCountSummary, ExecutionSessionDetail, ObservationCountSummary } from "@/api/types"
import { ModuleTable } from "@/components/session/ModuleTable"
import { NeedsAttention } from "@/components/session/NeedsAttention"
import { cn } from "@/lib/utils"

function pct1(n: number): string {
  return `${n.toFixed(1).replace(/\.0$/, "")}%`
}

function passRate(pass: number, total: number): string | null {
  return total > 0 ? pct1((pass / total) * 100) : null
}

function completeCounts(counts: EvidenceCountSummary | undefined): counts is EvidenceCountSummary & {
  executed: number
  passed: number
  failed: number
  errors: number
  skipped: number
} {
  return !!counts
    && counts.availability !== "unavailable"
    && [counts.executed, counts.passed, counts.failed, counts.errors, counts.skipped]
      .every((value) => typeof value === "number" && Number.isFinite(value))
}

function observationNote(
  label: string,
  counts: ObservationCountSummary,
): string | null {
  if (typeof counts.executed === "number" && counts.executed > 0) {
    return `${counts.executed.toLocaleString()} ${label} observations · not verdict-bearing`
  }
  if (typeof counts.reportFileCount === "number" && counts.reportFileCount > 0) {
    return `${counts.reportFileCount.toLocaleString()} ${label} report files · not verdict-bearing`
  }
  return null
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
    <div className="rounded-[10px] border border-border bg-card px-4 py-3.5">
      <div className="font-mono text-[11px] uppercase tracking-[0.06em] text-muted-foreground">{label}</div>
      <div className={cn("mt-1 text-[27px] font-bold leading-[1.1] tracking-[-0.02em] text-foreground", valueClass)}>
        {value}
      </div>
      {sub ? <div className="mt-1 font-mono text-[12px] text-muted-foreground">{sub}</div> : null}
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
  const layers = test.evidenceLayers?.tests
  const claimedSubjects = layers?.claimed.latestSubjects
  const claimedAvailable = completeCounts(claimedSubjects)
  const claimedNonSkip = claimedAvailable
    ? claimedSubjects.passed + claimedSubjects.failed + claimedSubjects.errors
    : 0

  const rate = layers
    ? claimedAvailable ? passRate(claimedSubjects.passed, claimedNonSkip) : null
    : passRate(test.pass, test.total)
  const passSub = layers
    ? claimedAvailable
      ? [
          `${claimedSubjects.passed.toLocaleString()} passed`,
          claimedSubjects.skipped ? `${claimedSubjects.skipped.toLocaleString()} skipped` : null,
        ].filter(Boolean).join(" · ")
      : claimedSubjects?.reason ?? "module-qualified subject metric unavailable"
    : [
        test.pass ? `${test.pass.toLocaleString()} passed` : null,
        test.skip ? `${test.skip.toLocaleString()} skipped` : null,
      ].filter(Boolean).join(" · ")

  const claimedFailures = claimedAvailable
    ? claimedSubjects.failed + claimedSubjects.errors
    : null
  const observationSub = layers
    ? [
        observationNote("quarantined", layers.quarantinedObservations),
        observationNote("unattributed", layers.unattributedObservations),
        observationNote("stale", layers.staleObservations),
      ].filter(Boolean).join(" · ")
    : null

  const failingModules = modules.filter((m) => (m.failingCount ?? 0) > 0).length
  const failSub = layers
    ? observationSub || null
    : !singleModule && failingModules > 0
      ? `across ${failingModules} module${failingModules > 1 ? "s" : ""}`
      : null
  const passValueClass = layers
    ? claimedFailures === 0 && rate !== null ? "text-status-success" : undefined
    : "text-status-success"
  const failureValueClass = (layers ? claimedFailures ?? 0 : test.fail) > 0
    ? "text-status-failed"
    : undefined

  const goal = detail.context?.trunk.goal
  const progress = progressText(detail.context?.trunk.progress)

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

      <div className="grid grid-cols-3 gap-3">
        <Tile
          label={layers ? "Claimed subject pass rate" : "Pass rate"}
          value={rate ?? "—"}
          sub={passSub || null}
          valueClass={passValueClass}
        />
        <Tile
          label={layers ? "Claimed subject failures" : "Failing tests"}
          value={claimedFailures === null && layers ? "—" : String(layers ? claimedFailures : test.fail ?? 0)}
          sub={failSub}
          valueClass={failureValueClass}
        />
        {!singleModule && ms ? (
          <Tile
            label="Modules built"
            value={`${ms.modulesBuilt} / ${ms.modulesTotal}`}
            sub={ms.modulesFailed > 0 ? `${ms.modulesFailed} failed` : null}
            valueClass={
              ms.modulesFailed === 0 && ms.modulesBuilt >= ms.modulesTotal
                ? "text-status-success"
                : "text-status-attention"
            }
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
      ) : null}

      <div className="mt-5">
        <NeedsAttention modules={modules} warnings={detail.build.warnings ?? []} />
      </div>
    </div>
  )
}
