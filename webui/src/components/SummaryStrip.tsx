import type { BuildSummary, WorkspaceSummary } from "@/api/types"
import { Tooltip } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"

/** Fleet-wide rollup band shown above the detail pane. Metrics-v2 subject KPIs
 * and legacy test aggregates are accumulated in separate grains. */

interface Rollup {
  total: number
  running: number
  buildSuccess: number
  buildKnown: number
  passed: number
  executedNonSkip: number
  executed: number
  declared: number
  claimedSubjectWorkspaces: number
  claimedSubjectUnavailable: number
  claimedSubjectPassed: number
  claimedSubjectExecutedNonSkip: number
  claimedSubjectExecuted: number
  nonVerdictObservations: number
  nonVerdictReportFiles: number
}

function buildState(build: BuildSummary | string): string {
  return (typeof build === "string" ? build : build.state).trim().toLowerCase()
}

export function rollup(workspaces: WorkspaceSummary[]): Rollup {
  const r: Rollup = {
    total: workspaces.length,
    running: 0,
    buildSuccess: 0,
    buildKnown: 0,
    passed: 0,
    executedNonSkip: 0,
    executed: 0,
    declared: 0,
    claimedSubjectWorkspaces: 0,
    claimedSubjectUnavailable: 0,
    claimedSubjectPassed: 0,
    claimedSubjectExecutedNonSkip: 0,
    claimedSubjectExecuted: 0,
    nonVerdictObservations: 0,
    nonVerdictReportFiles: 0,
  }
  for (const w of workspaces) {
    if (w.docker.status === "running") r.running += 1
    const state = buildState(w.build)
    if (state && state !== "none" && state !== "unknown") {
      r.buildKnown += 1
      if (state === "success" || state === "green" || state === "passed") r.buildSuccess += 1
    }
    const t = w.test
    const layers = t?.evidenceLayers?.tests
    if (layers) {
      r.claimedSubjectWorkspaces += 1
      const subjects = layers.claimed.latestSubjects
      const counts = [subjects.executed, subjects.passed, subjects.failed, subjects.errors, subjects.skipped]
      if (subjects.availability === "unavailable" || !counts.every((value) => typeof value === "number" && Number.isFinite(value))) {
        r.claimedSubjectUnavailable += 1
      } else {
        r.claimedSubjectPassed += subjects.passed as number
        r.claimedSubjectExecutedNonSkip += (subjects.passed as number) + (subjects.failed as number) + (subjects.errors as number)
        r.claimedSubjectExecuted += subjects.executed as number
      }
      for (const observations of [
        layers.quarantinedObservations,
        layers.unattributedObservations,
        layers.staleObservations,
      ]) {
        if (typeof observations.executed === "number") {
          r.nonVerdictObservations += observations.executed
        }
        if (typeof observations.reportFileCount === "number") {
          r.nonVerdictReportFiles += observations.reportFileCount
        }
      }
    } else if (t && t.total > 0) {
      r.passed += t.pass
      // Skips are "not run", not failures — matches SAG's own pass-rate rule.
      r.executedNonSkip += t.pass + t.fail + (t.errors ?? 0)
      r.executed += t.total
      if (t.declaredTotal && t.declaredTotal > 0) r.declared += t.declaredTotal
    }
  }
  return r
}

function pct(numerator: number, denominator: number): string | null {
  if (denominator <= 0) return null
  return `${((100 * numerator) / denominator).toFixed(1)}%`
}

function Stat({
  label,
  value,
  hint,
  tone = "neutral",
}: {
  label: string
  value: string
  hint: string
  tone?: "neutral" | "good" | "warn"
}) {
  return (
    <Tooltip label={hint} side="bottom">
      <div className="flex flex-col rounded-lg border border-border bg-card px-3 py-1.5">
        <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-muted-foreground">
          {label}
        </span>
        <span
          className={cn(
            "text-[15px] font-bold leading-tight",
            tone === "good" && "text-status-success",
            tone === "warn" && "text-status-attention",
            tone === "neutral" && "text-foreground",
          )}
        >
          {value}
        </span>
      </div>
    </Tooltip>
  )
}

function rateTone(rate: string | null): "good" | "warn" | "neutral" {
  if (rate === null) return "neutral"
  return parseFloat(rate) >= 80 ? "good" : "warn"
}

export function SummaryStrip({ workspaces }: { workspaces: WorkspaceSummary[] }) {
  if (workspaces.length === 0) return null
  const r = rollup(workspaces)

  const buildRate = pct(r.buildSuccess, r.buildKnown)
  const hasEvidenceLayers = r.claimedSubjectWorkspaces > 0
  const claimedRate = r.claimedSubjectUnavailable === 0
    ? pct(r.claimedSubjectPassed, r.claimedSubjectExecutedNonSkip)
    : null
  const legacyPassRate = pct(r.passed, r.executedNonSkip)
  const legacyExecRate = pct(r.executed, r.declared)
  const diagnosticValue = r.nonVerdictObservations > 0
    ? r.nonVerdictObservations.toLocaleString()
    : `${r.nonVerdictReportFiles.toLocaleString()} files`
  const claimedSubjectValue = r.claimedSubjectUnavailable > 0
    ? "Unavailable"
    : claimedRate ?? "No executions"
  const claimedSubjectHint = r.claimedSubjectUnavailable > 0
    ? `${r.claimedSubjectUnavailable} of ${r.claimedSubjectWorkspaces} workspaces lack module-qualified subject counts`
    : claimedRate === null
      ? "No non-skipped module-qualified latest subjects were executed"
      : `${r.claimedSubjectPassed.toLocaleString()} passed of ${r.claimedSubjectExecutedNonSkip.toLocaleString()} module-qualified latest subjects (skips excluded)`

  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-border bg-background px-5 py-2.5 sm:px-6">
      <span className="mr-1 font-mono text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
        Fleet
      </span>
      <Stat
        label="Workspaces"
        value={`${r.total}`}
        hint={`${r.total} workspaces on the dashboard, ${r.running} running`}
      />
      {buildRate !== null ? (
        <Stat
          label="Build success"
          value={buildRate}
          tone={rateTone(buildRate)}
          hint={`${r.buildSuccess} of ${r.buildKnown} workspaces with a known build state built successfully`}
        />
      ) : null}
      {hasEvidenceLayers ? (
        <Stat
          label="Claimed subjects"
          value={claimedSubjectValue}
          tone={rateTone(claimedRate)}
          hint={claimedSubjectHint}
        />
      ) : legacyPassRate !== null ? (
        <Stat
          label="Pass rate"
          value={legacyPassRate}
          tone={rateTone(legacyPassRate)}
          hint={`${r.passed.toLocaleString()} passed of ${r.executedNonSkip.toLocaleString()} executed (pass+fail+errors; skips excluded)`}
        />
      ) : null}
      {hasEvidenceLayers && (r.nonVerdictObservations > 0 || r.nonVerdictReportFiles > 0) ? (
        <Stat
          label="Diagnostics"
          value={diagnosticValue}
          hint={`${r.nonVerdictObservations.toLocaleString()} report observations across ${r.nonVerdictReportFiles.toLocaleString()} files; not verdict-bearing`}
        />
      ) : null}
      {hasEvidenceLayers && legacyPassRate !== null ? (
        <Stat
          label="Legacy tests"
          value={legacyPassRate}
          hint="Legacy workspace test aggregates; kept separate from metrics-v2 claimed subjects"
        />
      ) : null}
      {legacyExecRate !== null ? (
        <Stat
          label={hasEvidenceLayers ? "Legacy execution" : "Execution rate"}
          value={legacyExecRate}
          tone={hasEvidenceLayers ? "neutral" : rateTone(legacyExecRate)}
          hint={`${r.executed.toLocaleString()} legacy tests executed of ${r.declared.toLocaleString()} declared test methods`}
        />
      ) : null}
    </div>
  )
}
