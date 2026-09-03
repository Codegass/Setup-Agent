import type { BuildSummary, WorkspaceSummary } from "@/api/types"
import { Tooltip } from "@/components/ui/tooltip"
import { completeEvidenceCounts, lowerBoundEvidenceCounts } from "@/evidencePresentation"
import { cn } from "@/lib/utils"

/** Fleet-wide rollup band shown above the detail pane. Metrics-v2 subject KPIs
 * and legacy test aggregates are accumulated in separate grains. */

interface Rollup {
  total: number
  running: number
  buildSuccess: number
  buildPartial: number
  buildFailed: number
  buildUnavailable: number
  buildKnown: number
  passed: number
  failed: number
  errors: number
  executedNonSkip: number
  claimedSubjectWorkspaces: number
  claimedSubjectMeasured: number
  claimedSubjectUnavailable: number
  claimedSubjectPartial: number
  claimedSubjectLowerBound: number
  claimedSubjectPassed: number
  claimedSubjectFailed: number
  claimedSubjectErrors: number
  claimedSubjectExecutedNonSkip: number
  claimedSubjectExecuted: number
  runResultMeasured: number
  nonVerdictObservations: number
  nonVerdictReportFiles: number
  nonVerdictIncomplete: boolean
}

function buildState(build: BuildSummary | string): string {
  return (typeof build === "string" ? build : build.state).trim().toLowerCase()
}

function isFiniteCount(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
}

function buildBucket(state: string): "success" | "partial" | "failed" | "unavailable" {
  if (["success", "green", "passed", "pass"].includes(state)) return "success"
  if (["partial", "incomplete"].includes(state)) return "partial"
  if (["failure", "failed", "red"].includes(state)) return "failed"
  return "unavailable"
}

function hasSealedRunResult(workspace: WorkspaceSummary): boolean {
  const state = workspace.test.state.trim().toLowerCase()
  if (!state || ["none", "unknown", "unavailable", "pending", "not-run", "not_run"].includes(state)) {
    return false
  }
  return [workspace.test.pass, workspace.test.fail, workspace.test.errors ?? 0, workspace.test.skip, workspace.test.total]
    .every(isFiniteCount)
}

export function rollup(workspaces: WorkspaceSummary[]): Rollup {
  const r: Rollup = {
    total: workspaces.length,
    running: 0,
    buildSuccess: 0,
    buildPartial: 0,
    buildFailed: 0,
    buildUnavailable: 0,
    buildKnown: 0,
    passed: 0,
    failed: 0,
    errors: 0,
    executedNonSkip: 0,
    claimedSubjectWorkspaces: 0,
    claimedSubjectMeasured: 0,
    claimedSubjectUnavailable: 0,
    claimedSubjectPartial: 0,
    claimedSubjectLowerBound: 0,
    claimedSubjectPassed: 0,
    claimedSubjectFailed: 0,
    claimedSubjectErrors: 0,
    claimedSubjectExecutedNonSkip: 0,
    claimedSubjectExecuted: 0,
    runResultMeasured: 0,
    nonVerdictObservations: 0,
    nonVerdictReportFiles: 0,
    nonVerdictIncomplete: false,
  }
  for (const w of workspaces) {
    if (w.docker.status === "running") r.running += 1
    const bucket = buildBucket(buildState(w.build))
    if (bucket === "success") {
      r.buildKnown += 1
      r.buildSuccess += 1
    } else if (bucket === "partial") {
      r.buildKnown += 1
      r.buildPartial += 1
    } else if (bucket === "failed") {
      r.buildKnown += 1
      r.buildFailed += 1
    } else {
      r.buildUnavailable += 1
    }
    const t = w.test
    const layers = t?.evidenceLayers?.tests
    if (layers) {
      r.claimedSubjectWorkspaces += 1
      const subjects = layers.claimed.latestSubjects
      const exactSubjects = completeEvidenceCounts(subjects)
      const boundedSubjects = lowerBoundEvidenceCounts(subjects)
      if (boundedSubjects) {
        r.claimedSubjectPartial += 1
        r.claimedSubjectLowerBound += boundedSubjects.executed
      } else if (!exactSubjects) {
        r.claimedSubjectUnavailable += 1
      } else {
        r.claimedSubjectMeasured += 1
        r.claimedSubjectPassed += exactSubjects.passed
        r.claimedSubjectFailed += exactSubjects.failed
        r.claimedSubjectErrors += exactSubjects.errors
        r.claimedSubjectExecutedNonSkip += exactSubjects.passed + exactSubjects.failed + exactSubjects.errors
        r.claimedSubjectExecuted += exactSubjects.executed
      }
      for (const observations of [
        layers.quarantinedObservations,
        layers.unattributedObservations,
        layers.staleObservations,
      ]) {
        if (isFiniteCount(observations.executed)) {
          r.nonVerdictObservations += observations.executed
          if (observations.availability === "partial" || observations.bound === "lower") {
            r.nonVerdictIncomplete = true
          }
        } else if (observations.availability === "unavailable" || observations.executed == null) {
          r.nonVerdictIncomplete = true
        }
        if (isFiniteCount(observations.reportFileCount)) {
          r.nonVerdictReportFiles += observations.reportFileCount
        }
      }
    }
    if (t && hasSealedRunResult(w)) {
      r.runResultMeasured += 1
      r.passed += t.pass
      r.failed += t.fail
      r.errors += t.errors ?? 0
      // Skips are "not run". Errors are negative run results and stay in the denominator.
      r.executedNonSkip += t.pass + t.fail + (t.errors ?? 0)
    }
  }
  return r
}

function pct(numerator: number, denominator: number): string | null {
  if (!Number.isFinite(numerator) || !Number.isFinite(denominator)) return null
  if (denominator <= 0 || numerator < 0 || numerator > denominator) return null
  return `${((100 * numerator) / denominator).toFixed(1)}%`
}

function buildDistribution(r: Rollup): string {
  const parts = [
    r.buildSuccess ? `${r.buildSuccess} passed` : null,
    r.buildPartial ? `${r.buildPartial} partial` : null,
    r.buildFailed ? `${r.buildFailed} failed` : null,
    r.buildUnavailable ? `${r.buildUnavailable} unavailable` : null,
  ].filter(Boolean)
  return parts.join(" · ") || "Unavailable"
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

  const hasEvidenceLayers = r.claimedSubjectWorkspaces > 0
  const claimedRate = pct(r.claimedSubjectPassed, r.claimedSubjectExecutedNonSkip)
  const runPassRate = pct(r.passed, r.executedNonSkip)
  const measuredIdentityLabel = `${r.claimedSubjectMeasured} measured workspace${r.claimedSubjectMeasured === 1 ? "" : "s"}`
  const measuredRunLabel = `${r.runResultMeasured} measured workspace${r.runResultMeasured === 1 ? "" : "s"}`
  const diagnosticValue = r.nonVerdictObservations > 0
    ? `${r.nonVerdictObservations.toLocaleString()}${r.nonVerdictIncomplete ? "+" : ""}`
    : r.nonVerdictIncomplete ? "Incomplete" : `${r.nonVerdictReportFiles.toLocaleString()} files`
  const identityHint = [
    `${r.claimedSubjectMeasured} of ${r.total} workspaces recorded complete per-test results.`,
    r.claimedSubjectPartial
      ? `${r.claimedSubjectPartial} more kept only part of their per-test results, with at least ${r.claimedSubjectLowerBound.toLocaleString()} tests retained.`
      : null,
    r.claimedSubjectUnavailable ? `${r.claimedSubjectUnavailable} were unavailable.` : null,
  ].filter(Boolean).join(" ")
  const diagnosticHint = r.nonVerdictIncomplete
    ? `At least ${r.nonVerdictObservations.toLocaleString()} diagnostics; some sources were not countable. Excluded from run results.`
    : `${r.nonVerdictObservations.toLocaleString()} diagnostics${r.nonVerdictReportFiles ? ` across ${r.nonVerdictReportFiles.toLocaleString()} files` : ""}. Excluded from run results.`

  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-border bg-background px-5 py-2.5 sm:px-6">
      <span className="mr-1 font-mono text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
        Fleet
      </span>
      <Stat
        label="Workspaces"
        value={`${r.total}`}
        hint={`${r.total} workspaces on the dashboard, ${r.running} running containers`}
      />
      <Stat
        label="Build states"
        value={buildDistribution(r)}
        tone={r.buildFailed || r.buildPartial ? "warn" : r.buildSuccess ? "good" : "neutral"}
        hint={`${r.buildSuccess} passed, ${r.buildPartial} partial, ${r.buildFailed} failed, and ${r.buildUnavailable} unavailable`}
      />
      {hasEvidenceLayers ? (
        <Stat
          label="Per-test results"
          value={`${r.claimedSubjectMeasured}/${r.total} measured`}
          tone={r.claimedSubjectMeasured === r.total ? "good" : "warn"}
          hint={identityHint}
        />
      ) : null}
      {hasEvidenceLayers && claimedRate !== null ? (
        <Stat
          label="Verified pass rate"
          value={claimedRate}
          tone={rateTone(claimedRate)}
          hint={`${r.claimedSubjectPassed.toLocaleString()} of ${r.claimedSubjectExecutedNonSkip.toLocaleString()} passed across ${measuredIdentityLabel} only; failures and errors are in the denominator`}
        />
      ) : null}
      {hasEvidenceLayers && r.claimedSubjectPartial > 0 ? (
        <Stat
          label="Tests counted (min)"
          value={`≥${r.claimedSubjectLowerBound.toLocaleString()}`}
          tone="warn"
          hint={`${r.claimedSubjectPartial} workspace${r.claimedSubjectPartial === 1 ? "" : "s"} kept only part of their per-test results; this is a minimum count, not a pass rate`}
        />
      ) : null}
      <Stat
        label="Run coverage"
        value={`${r.runResultMeasured}/${r.total} measured`}
        tone={r.runResultMeasured === r.total ? "good" : "warn"}
        hint={`${r.runResultMeasured} of ${r.total} workspaces have a sealed run result, including explicit zero-result runs`}
      />
      {runPassRate !== null ? (
        <Stat
          label="Run pass rate"
          value={runPassRate}
          tone={rateTone(runPassRate)}
          hint={`${r.passed.toLocaleString()} passed, ${r.failed.toLocaleString()} failed, and ${r.errors.toLocaleString()} errors across ${measuredRunLabel} only; skips excluded`}
        />
      ) : null}
      {hasEvidenceLayers && (r.nonVerdictObservations > 0 || r.nonVerdictReportFiles > 0 || r.nonVerdictIncomplete) ? (
        <Stat
          label="Diagnostics"
          value={diagnosticValue}
          hint={diagnosticHint}
        />
      ) : null}
    </div>
  )
}
