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

export interface IdentityPresentation {
  available: boolean
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

export interface BuildPresentation {
  value: "Passed" | "Failed" | "Partial" | "Not run" | "Unavailable"
  tone: ResultTone
  valueClass?: string
  summary: string
  scope: BuildScopePresentation | null
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
  if (!counts || counts.availability === "unavailable") return null
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

export function humanizeIdentityGap(reason: string | null | undefined): string {
  const normalized = reason?.trim().toLowerCase() ?? ""
  if (normalized.includes("module-qualified") || normalized.includes("subject/case identity")) {
    return "Module-qualified test identities were not sealed for this run."
  }
  if (normalized.includes("receipt") && normalized.includes("identity")) {
    return "Some test results were missing the module and stable test identity needed for verification."
  }
  if (normalized.includes("artifact") || normalized.includes("metrics")) {
    return "Verified test identity metrics were not produced for this run."
  }
  return "Verified test identities were not recorded for this run."
}

export function presentVerifiedIdentities(test: TestSummary): IdentityPresentation {
  const subjects = test.evidenceLayers?.tests.claimed.latestSubjects
  if (!subjects) {
    return {
      available: false,
      value: "Unavailable",
      tone: "neutral",
      counts: null,
      nonSkipped: null,
      negative: null,
      passRate: null,
      summary: "Verified test identities were not produced for this run.",
      rawReason: null,
    }
  }

  const counts = completeEvidenceCounts(subjects)
  if (!counts) {
    return {
      available: false,
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
    `${formatCount(counts.executed)} verified identities`,
    `${formatCount(counts.failed)} failed`,
    `${formatCount(counts.errors)} errors`,
    counts.skipped > 0 ? `${formatCount(counts.skipped)} skipped` : null,
  ].filter((part): part is string => part !== null).join(" · ")

  return {
    available: true,
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
    ? "Excluded from the verified test identity metric."
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
    }
  }
  const scope = buildScopeFromRates(rates, build.state)
  const facts = [
    scope ? `Evidence covers ${formatCount(scope.observed)} of ${formatCount(scope.expected)} modules` : null,
    finiteCount(build.classCount) ? `${formatCount(build.classCount)} compiled classes` : null,
    build.tool && !["—", "-", "sealed snapshot", "unknown"].includes(build.tool.trim().toLowerCase())
      ? build.tool.trim()
      : null,
    scope?.incomplete || state.label === "Partial" ? "Scope incomplete" : null,
  ].filter((fact): fact is string => fact !== null)

  return {
    value: state.label,
    tone: state.tone,
    valueClass: state.valueClass,
    scope,
    summary: facts.join(" · ") || "No verified build result was recorded.",
  }
}
