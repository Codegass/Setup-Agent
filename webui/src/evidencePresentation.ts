import type {
  BuildSummary,
  EvidenceCountSummary,
  ObservationCountSummary,
  TestEvidenceLayers,
  TestSummary,
} from "@/api/types"

export type ResultTone = "green" | "red" | "amber" | "neutral"

export interface CompleteEvidenceCounts {
  executed: number
  passed: number
  failed: number
  errors: number
  skipped: number
}

export interface TestRunPresentation extends CompleteEvidenceCounts {
  countsAvailable: boolean
  stateLabel: "Passed" | "Failed" | "Partial" | "Not run" | "Unavailable"
  tone: ResultTone
  valueClass?: string
  nonSkipped: number
  negative: number
  passRate: number | null
  summary: string
}

export interface TestAccountingPresentation {
  available: boolean
  counts: CompleteEvidenceCounts | null
  accounted: number | null
  nonSkipped: number | null
  summary: string
}

export interface IdentityPresentation {
  available: boolean
  partial: boolean
  value: string
  tone: ResultTone
  valueClass?: string
  counts: CompleteEvidenceCounts | null
  nonSkipped: number | null
  negative: number | null
  passRate: number | null
  summary: string
  rawReason: string | null
}

export interface DiagnosticPresentation {
  hasData: boolean
  exact: boolean
  knownTotal: number
  value: string
  breakdown: string | null
  summary: string
}

export interface BuildScopePresentation {
  observed: number
  expected: number
  incomplete: boolean
}

export interface SourceScopePresentation {
  covered: number
  total: number
}

export interface BuildPresentation {
  value: "Success" | "Failed" | "Partial" | "Not run" | "Unavailable"
  tone: ResultTone
  valueClass?: string
  summary: string
  scope: BuildScopePresentation | null
  sourceScope: SourceScopePresentation | null
}

const DATA_NOTE_COPY: Record<string, string> = {
  build_modules_incomplete: "Build evidence covers only part of the discovered module set.",
  build_coverage_scope_unverified: "Build coverage could not be verified across the full project scope.",
  test_primary_coordinate_unresolved: "The primary test project or module could not be identified.",
  test_executions_unattributed_to_receipts: "Some test runs could not be linked to their recorded tool executions.",
  test_execution_interrupted: "The test runner stopped before the declared scope completed; shown counts are the sealed prefix.",
  rate_denominator_not_a_bound: "A rate denominator did not bound the observed count, so no percentage is shown.",
  metrics_conflict: "Conflicting test or build metrics were recorded.",
  reactor_scope_narrowed: "The build ran against a narrower module scope than the full project.",
}

function finiteCount(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
}

function normalizedState(value: string | undefined): string {
  return value?.trim().toLowerCase() ?? ""
}

export function formatCount(value: number): string {
  return value.toLocaleString()
}

export function formatRate(value: number): string {
  return `${value.toFixed(1).replace(/\.0$/, "")}%`
}

export function safeRate(numerator: number, denominator: number): number | null {
  if (
    !Number.isFinite(numerator)
    || !Number.isFinite(denominator)
    || numerator < 0
    || denominator <= 0
    || numerator > denominator
  ) {
    return null
  }
  return (numerator / denominator) * 100
}

export function presentDataNotes(conflicts: string[] | null | undefined): string[] {
  if (!conflicts?.length) return []
  const notes = conflicts.map((conflict) => {
    const code = conflict.trim().toLowerCase().split(":", 1)[0]
    return DATA_NOTE_COPY[code] ?? "An additional evidence consistency issue was recorded."
  })
  return [...new Set(notes)]
}

/**
 * A metric advertised as available must include one internally consistent set
 * of counts. Returning null is intentional: the UI must not turn missing or
 * contradictory evidence into zeroes.
 */
export function completeEvidenceCounts(
  counts: EvidenceCountSummary | ObservationCountSummary | null | undefined,
): CompleteEvidenceCounts | null {
  if (
    !counts
    || counts.availability === "unavailable"
    || counts.availability === "partial"
    || counts.bound === "lower"
  ) return null
  if (![counts.executed, counts.passed, counts.failed, counts.errors, counts.skipped].every(finiteCount)) {
    return null
  }

  const complete = {
    executed: counts.executed as number,
    passed: counts.passed as number,
    failed: counts.failed as number,
    errors: counts.errors as number,
    skipped: counts.skipped as number,
  }
  if (complete.executed !== complete.passed + complete.failed + complete.errors + complete.skipped) {
    return null
  }
  return complete
}

/**
 * Return the exact retained counts only when the artifact explicitly says
 * they are a lower bound on a larger population.  Keeping this separate from
 * completeEvidenceCounts prevents a truncated sample from producing a pass
 * percentage or a green status.
 */
export function lowerBoundEvidenceCounts(
  counts: EvidenceCountSummary | ObservationCountSummary | null | undefined,
): CompleteEvidenceCounts | null {
  if (!counts || counts.availability !== "partial" || counts.bound !== "lower") return null
  if (![counts.executed, counts.passed, counts.failed, counts.errors, counts.skipped].every(finiteCount)) {
    return null
  }
  const retained = {
    executed: counts.executed as number,
    passed: counts.passed as number,
    failed: counts.failed as number,
    errors: counts.errors as number,
    skipped: counts.skipped as number,
  }
  return retained.executed === retained.passed + retained.failed + retained.errors + retained.skipped
    ? retained
    : null
}

function resultState(value: string | undefined): {
  label: TestRunPresentation["stateLabel"]
  tone: ResultTone
  valueClass?: string
} {
  const state = normalizedState(value)
  if (["success", "pass", "passed"].includes(state)) {
    return { label: "Passed", tone: "green", valueClass: "text-status-success" }
  }
  if (["failed", "failure", "fail"].includes(state)) {
    return { label: "Failed", tone: "red", valueClass: "text-status-failed" }
  }
  if (state === "partial") {
    return { label: "Partial", tone: "amber", valueClass: "text-status-attention" }
  }
  if (state === "not_attempted") {
    return { label: "Not run", tone: "neutral" }
  }
  return { label: "Unavailable", tone: "neutral" }
}

export function presentTestRun(test: TestSummary): TestRunPresentation {
  const reportedState = resultState(test.state)
  const errors = finiteCount(test.errors) ? test.errors : 0
  const numericCounts = [test.pass, test.fail, test.skip].every(finiteCount)
  const accounted = numericCounts ? test.pass + test.fail + errors + test.skip : 0
  const countsAvailable = reportedState.label !== "Not run"
    && numericCounts
    && (reportedState.label !== "Unavailable" || accounted > 0)
  const passed = countsAvailable ? test.pass : 0
  const failed = countsAvailable ? test.fail : 0
  const skipped = countsAvailable ? test.skip : 0
  const observedErrors = countsAvailable ? errors : 0
  const negative = failed + observedErrors
  const nonSkipped = passed + negative
  // A recorded success cannot override concrete failed or errored results.
  // Present the run as failed while retaining the original counts below it.
  const state = countsAvailable && negative > 0
    ? { label: "Failed" as const, tone: "red" as const, valueClass: "text-status-failed" }
    : reportedState
  const executed = countsAvailable && finiteCount(test.total)
    ? Math.max(test.total, accounted)
    : accounted
  const passRate = countsAvailable ? safeRate(passed, nonSkipped) : null

  let summary = state.label === "Not run"
    ? "Tests were not run."
    : "Sealed run counts were not recorded."
  if (state.label !== "Not run" && countsAvailable && accounted === 0) {
    summary = "No sealed test results were recorded."
  } else if (countsAvailable) {
    const parts = [
      `${formatCount(passed)} passed`,
      `${formatCount(failed)} failed`,
      `${formatCount(observedErrors)} errors`,
      skipped > 0 ? `${formatCount(skipped)} skipped` : null,
    ].filter((part): part is string => part !== null)
    summary = `Sealed run results: ${parts.join(" · ")}`
  }

  return {
    countsAvailable,
    stateLabel: state.label,
    tone: state.tone,
    valueClass: state.valueClass,
    executed,
    passed,
    failed,
    errors: observedErrors,
    skipped,
    nonSkipped,
    negative,
    passRate,
    summary,
  }
}

/**
 * Present the receipt-scoped outcome ledger. This is an accounting check, not
 * a claim about discovery coverage: ratios are shown only for one complete,
 * internally reconciled receipt count set.
 */
export function presentTestAccounting(test: TestSummary): TestAccountingPresentation {
  const receiptExecutions = test.evidenceLayers?.tests.claimed.receiptExecutions
  if (!receiptExecutions) {
    return {
      available: false,
      counts: null,
      accounted: null,
      nonSkipped: null,
      summary: "This run did not record its test outcome totals.",
    }
  }

  const counts = completeEvidenceCounts(receiptExecutions)
  if (!counts) {
    const bounded = receiptExecutions.availability === "partial"
      || receiptExecutions.bound === "lower"
    return {
      available: false,
      counts: null,
      accounted: null,
      nonSkipped: null,
      summary: bounded
        ? "The recorded test totals are a minimum, not the complete count; no completion ratio is shown."
        : receiptExecutions.availability === "unavailable"
          ? "Recorded test totals are unavailable for this run."
          : "The recorded test totals are incomplete or inconsistent; no completion ratio is shown.",
    }
  }
  if (counts.executed <= 0) {
    return {
      available: false,
      counts: null,
      accounted: null,
      nonSkipped: null,
      summary: "No test outcomes were recorded.",
    }
  }

  const accounted = counts.passed + counts.failed + counts.errors + counts.skipped
  const nonSkipped = counts.passed + counts.failed + counts.errors
  const nonSkippedSummary = nonSkipped > 0
    ? `Non-skipped passed ${formatCount(counts.passed)} / ${formatCount(nonSkipped)}`
    : "Non-skipped passed unavailable"
  return {
    available: true,
    counts,
    accounted,
    nonSkipped,
    summary: [
      `Test outcomes recorded ${formatCount(accounted)} / ${formatCount(counts.executed)}`,
      nonSkippedSummary,
      `Skipped ${formatCount(counts.skipped)}`,
      `Failed / errors ${formatCount(counts.failed)} / ${formatCount(counts.errors)}`,
    ].join(" · "),
  }
}

export function humanizeIdentityGap(reason: string | null | undefined): string {
  const normalized = reason?.trim().toLowerCase() ?? ""
  if (normalized.includes("module-qualified") || normalized.includes("subject/case identity")) {
    return "Per-test results with module and test names were not sealed for this run."
  }
  if (normalized.includes("receipt") && normalized.includes("identity")) {
    return "Some test results were missing the module and test name needed for verification."
  }
  if (normalized.includes("artifact") || normalized.includes("metrics")) {
    return "Verified per-test metrics were not produced for this run."
  }
  return "Verified per-test results were not recorded for this run."
}

export function presentVerifiedIdentities(test: TestSummary): IdentityPresentation {
  const subjects = test.evidenceLayers?.tests.claimed.latestSubjects
  if (!subjects) {
    return {
      available: false,
      partial: false,
      value: "Unavailable",
      tone: "neutral",
      counts: null,
      nonSkipped: null,
      negative: null,
      passRate: null,
      summary: "Verified per-test results were not produced for this run.",
      rawReason: null,
    }
  }

  const lowerBound = lowerBoundEvidenceCounts(subjects)
  if (lowerBound) {
    const negative = lowerBound.failed + lowerBound.errors
    const reason = subjects.reason?.trim() || "Only part of the per-test list was kept."
    const retained = [
      `${formatCount(lowerBound.passed)} passed`,
      `${formatCount(lowerBound.failed)} failed`,
      `${formatCount(lowerBound.errors)} errors`,
      lowerBound.skipped > 0 ? `${formatCount(lowerBound.skipped)} skipped` : null,
    ].filter((part): part is string => part !== null).join(" · ")
    return {
      available: true,
      partial: true,
      value: `≥${formatCount(lowerBound.executed)}`,
      tone: "amber",
      valueClass: "text-status-attention",
      counts: lowerBound,
      nonSkipped: null,
      negative,
      passRate: null,
      summary: `At least these per-test results were kept: ${retained}. ${reason}`,
      rawReason: reason,
    }
  }

  const counts = completeEvidenceCounts(subjects)
  if (!counts) {
    return {
      available: false,
      partial: false,
      value: "Unavailable",
      tone: "neutral",
      counts: null,
      nonSkipped: null,
      negative: null,
      passRate: null,
      summary: humanizeIdentityGap(subjects.reason),
      rawReason: subjects.reason?.trim() || null,
    }
  }

  const negative = counts.failed + counts.errors
  const nonSkipped = counts.passed + negative
  const passRate = safeRate(counts.passed, nonSkipped)
  const value = passRate == null ? "Recorded" : formatRate(passRate)
  const tone: ResultTone = negative > 0 ? "red" : passRate == null ? "neutral" : "green"
  const summary = [
    `${formatCount(counts.executed)} tests verified by name`,
    `${formatCount(counts.failed)} failed`,
    `${formatCount(counts.errors)} errors`,
    counts.skipped > 0 ? `${formatCount(counts.skipped)} skipped` : null,
  ].filter((part): part is string => part !== null).join(" · ")

  return {
    available: true,
    partial: false,
    value,
    tone,
    valueClass: tone === "green"
      ? "text-status-success"
      : tone === "red"
        ? "text-status-failed"
        : undefined,
    counts,
    nonSkipped,
    negative,
    passRate,
    summary,
    rawReason: null,
  }
}

const OBSERVATION_BUCKETS: Array<keyof Pick<
  TestEvidenceLayers,
  "quarantinedObservations" | "unattributedObservations" | "staleObservations"
>> = ["quarantinedObservations", "unattributedObservations", "staleObservations"]

export function presentDiagnostics(layers: TestEvidenceLayers | null | undefined): DiagnosticPresentation {
  if (!layers) {
    return {
      hasData: false,
      exact: false,
      knownTotal: 0,
      value: "Unavailable",
      breakdown: null,
      summary: "Diagnostic observation counts were not produced for this run.",
    }
  }

  let exact = true
  let knownTotal = 0
  let passed = 0
  let failed = 0
  let errors = 0
  let skipped = 0
  let breakdownExact = true

  for (const key of OBSERVATION_BUCKETS) {
    const bucket = layers[key]
    if (bucket.availability === "unavailable" || !finiteCount(bucket.executed)) {
      exact = false
    } else {
      knownTotal += bucket.executed
      if (bucket.availability === "partial" || bucket.bound === "lower") exact = false
    }

    const complete = completeEvidenceCounts(bucket)
    if (!complete) {
      breakdownExact = false
      continue
    }
    passed += complete.passed
    failed += complete.failed
    errors += complete.errors
    skipped += complete.skipped
  }

  const knownBreakdown = passed + failed + errors + skipped
  const breakdown = knownBreakdown > 0
    ? `${breakdownExact ? "" : "Known results: "}${formatCount(passed)} passed · ${formatCount(failed)} failed · ${formatCount(errors)} errors${skipped > 0 ? ` · ${formatCount(skipped)} skipped` : ""}`
    : null
  const hasData = knownTotal > 0 || !exact
  const value = exact
    ? formatCount(knownTotal)
    : knownTotal > 0
      ? `${formatCount(knownTotal)}+`
      : "Unavailable"
  const summary = exact
    ? "Excluded from the verified per-test results."
    : knownTotal > 0
      ? `At least ${formatCount(knownTotal)} observations were excluded; one or more groups could not be counted.`
      : "One or more diagnostic observation groups could not be counted."

  return { hasData, exact, knownTotal, value, breakdown, summary }
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

export function buildScopeFromRates(
  rates: Record<string, unknown> | null | undefined,
  buildState: string,
): BuildScopePresentation | null {
  const build = objectValue(rates?.build)
  const modules = objectValue(build?.modules)
  const observed = modules?.numerator
  const expected = modules?.denominator
  if (
    !Number.isInteger(observed)
    || !Number.isInteger(expected)
    || (observed as number) < 0
    || (expected as number) <= 0
    || (observed as number) > (expected as number)
  ) {
    return null
  }
  const band = typeof modules?.band === "string" ? modules.band.toLowerCase() : ""
  return {
    observed: observed as number,
    expected: expected as number,
    incomplete: normalizedState(buildState) !== "success"
      || observed !== expected
      || band !== "fully",
  }
}

export function sourceScopeFromBuild(build: BuildSummary): SourceScopePresentation | null {
  const sourceScope = build.sourceScope
  const covered = sourceScope?.covered
  const total = sourceScope?.total
  if (
    sourceScope?.availability !== "available"
    || !Number.isInteger(covered)
    || !Number.isInteger(total)
    || (covered as number) < 0
    || (total as number) <= 0
    || (covered as number) > (total as number)
  ) {
    return null
  }
  return { covered: covered as number, total: total as number }
}

export function presentBuild(
  build: BuildSummary,
  rates?: Record<string, unknown> | null,
): BuildPresentation {
  const state = resultState(build.state)
  if (state.label === "Not run") {
    return {
      value: "Not run",
      tone: "neutral",
      summary: "Build was not run.",
      scope: null,
      sourceScope: null,
    }
  }
  const scope = buildScopeFromRates(rates, build.state)
  const sourceScope = sourceScopeFromBuild(build)
  const facts = [
    sourceScope
      ? `Production Java sources ${formatCount(sourceScope.covered)} / ${formatCount(sourceScope.total)}`
      : null,
    scope ? `Modules ${formatCount(scope.observed)} / ${formatCount(scope.expected)}` : null,
    scope?.incomplete || state.label === "Partial" ? "Scope incomplete" : null,
  ].filter((fact): fact is string => fact !== null)

  return {
    value: state.label === "Passed" ? "Success" : state.label,
    tone: state.tone,
    valueClass: state.valueClass,
    sourceScope,
    scope,
    summary: facts.join(" · ") || "No comparable build-scope counts were recorded.",
  }
}
