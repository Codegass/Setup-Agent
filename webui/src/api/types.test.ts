import { describe, expect, it } from "vitest"

import type {
  Attainment,
  BuildSummary,
  CIComparison,
  EvidenceCountSummary,
  ExecutionSessionDetail,
  ResultCard,
  RunRates,
  TaskCompletion,
  TestSummary,
  TrajectoryTurn,
} from "./types"

const verdictContract = {
  canonicalVerdict: "unknown",
  rates: {
    build: {
      modules: { numerator: 2, denominator: 2, rate: 100, band: "fully" },
    },
  },
  snapshotStatus: "corrupt",
  legacy: false,
  reportDeliveryStatus: "failed",
} satisfies Pick<
  ExecutionSessionDetail,
  | "canonicalVerdict"
  | "rates"
  | "snapshotStatus"
  | "legacy"
  | "reportDeliveryStatus"
>

const testContract = {
  state: "partial",
  pass: 320,
  fail: 8,
  skip: 0,
  total: 328,
  rawExecutions: 987,
} satisfies TestSummary

const boundedCountContract = {
  executed: 0,
  passed: 0,
  failed: 0,
  errors: 0,
  skipped: 0,
  availability: "partial",
  bound: "lower",
  basis: "bounded identity sample",
  reason: "sample truncated",
} satisfies EvidenceCountSummary

const buildContract = {
  state: "success",
  tool: "sealed snapshot",
  time: "—",
  note: "Canonical build evidence from verdict.json",
  classCount: 56,
} satisfies BuildSummary

describe("sealed verdict API types", () => {
  it("models canonical authority and legacy labeling fields", () => {
    expect(verdictContract.rates.build.modules.band).toBe("fully")
    expect(verdictContract.snapshotStatus).toBe("corrupt")
    expect(verdictContract.legacy).toBe(false)
  })

  it("models raw execution diagnostics separately from the primary total", () => {
    expect(testContract.total).toBe(328)
    expect(testContract.rawExecutions).toBe(987)
  })

  it("models retained counts as an explicit lower bound", () => {
    expect(boundedCountContract.availability).toBe("partial")
    expect(boundedCountContract.bound).toBe("lower")
  })

  it("models class outputs as diagnostic build counts", () => {
    expect(buildContract.classCount).toBe(56)
  })
})

describe("result card types", () => {
  it("names all seven rows in reading order", () => {
    const card: ResultCard = {
      schemaVersion: 1,
      runId: "r",
      verdict: "success",
      verdictSource: "snapshot",
      rows: [
        { key: "setup", label: "Setup", status: "success", tone: "success", headline: "5/5 phases" },
        { key: "task", label: "Required task", status: "complete", tone: "success", headline: "complete 1/1 steps" },
        { key: "build", label: "Build", status: "success", tone: "success", headline: "1/1 modules built" },
        { key: "tests", label: "Tests", status: "executed", tone: "success", headline: "994 executed" },
        { key: "coverage", label: "Coverage", status: "not collected", tone: "neutral", headline: "not collected" },
        { key: "ci", label: "Official CI", status: "not compared", tone: "neutral", headline: "not compared" },
        { key: "report", label: "Report", status: "delivered", tone: "neutral", headline: "report.md" },
      ],
      stats: {},
      attention: [],
      notes: [],
    }
    expect(card.rows.map((row) => row.key)).toEqual([
      "setup",
      "task",
      "build",
      "tests",
      "coverage",
      "ci",
      "report",
    ])
  })

  it("names the card's own fields the way the API serves them", () => {
    const stats: ResultCard["stats"] = {
      phasesCompleted: 5,
      phasesTotal: 5,
      toolCalls: 24,
      wallClockSeconds: 156,
      advisorModel: "claude-opus-4.1",
      tokensIn: 133007,
      tokensOut: 2062,
      advisorTokensIn: 81516,
      advisorTokensOut: 477,
    }
    expect(stats.wallClockSeconds).toBe(156)
    expect(stats.advisorModel).toBe("claude-opus-4.1")
    // Two models answered, so two bills. The card never states their sum: one
    // number would say the executor spent what the advisor spent.
    expect(stats.tokensIn).toBe(133007)
    expect(stats.advisorTokensIn).toBe(81516)
  })

  it("carries the whole attainment payload, not just a verdict word", () => {
    const attainment: Attainment = {
      verdict: "met",
      cell_id: "jenkins-17",
      cell_grade: "A",
      valid: true,
      target_usable: true,
      clean: true,
      clean_form: "ids",
      built: true,
      alpha: { numerator: 523, denominator: 523 },
      alpha_test: { numerator: 523, denominator: 523 },
      alpha_build: { numerator: 1, denominator: 1 },
      executed_observed: 523,
      executed_target: 523,
      red_observed: 0,
      red_target: 0,
      modules_matched: 1,
      modules_target: 1,
      missing_module_ids: [],
      build_form: "modules",
      modules_basis: "log",
      unmatched_observed_module_ids: [],
      lifecycle_parity: {
        status: "equivalent",
        form: "maven_phases",
        ci_command: "mvn -V test",
        sag_commands: ["mvn test"],
        ci_reach: "test",
        sag_reach: "test",
        missing: [],
        extra: [],
      },
      unexpected_red_ids: [],
      reason_codes: [],
    }
    const comparison: CIComparison = {
      schema_version: 1,
      status: "evaluated",
      run_id: "r",
      repo: "apache/commons-cli",
      target_sha: "e171117",
      attainment,
      reasons: [],
      receipt_ids: [],
      commands: [],
    }
    expect(comparison.attainment?.alpha?.denominator).toBe(523)
    expect(comparison.attainment?.lifecycle_parity?.missing).toEqual([])
  })

  it("carries each required-task step, not just the rollup status", () => {
    const completion: TaskCompletion = {
      run_id: "r",
      status: "incomplete",
      steps: [
        {
          id: "ci-step-1",
          command: "mvn verify",
          status: "failed",
          receipt_id: "inv-1",
          exit_code: 1,
          reason: "Required command returned a nonzero exit code.",
        },
      ],
      reasons: [],
    }
    expect(completion.steps[0].exit_code).toBe(1)
  })

  it("models a rate grain's three shapes", () => {
    const rates: RunRates = {
      build: {
        modules: { rate: 100, band: "fully", numerator: 1, denominator: 1 },
        classes: { band: "unavailable", reason: "counts are diagnostic" },
      },
      test: {
        cases: { band: "unbounded", reason: "cannot bound", numerator: 994, denominator: 472 },
        modules: { band: "unavailable", reason: "receipts identify domains" },
      },
      coverage: { status: "unavailable", reason: "coverage pass not run" },
    }
    expect(rates.build?.modules?.band).toBe("fully")
    expect(rates.test?.cases?.numerator).toBe(994)
  })

  it("models the truncated rates the session detail actually ships", () => {
    const rates: RunRates = {
      build: { modules: { rate: 100, band: "fully", numerator: 3, denominator: 3 } },
    }
    expect(rates.build?.classes).toBeUndefined()
    expect(rates.coverage).toBeUndefined()
  })

  it("lets a turn state how its call came out", () => {
    const turn: TrajectoryTurn = {
      turn_id: 8,
      phase: "build",
      actor: "model",
      call: { tool: "build", summary: "verify mvn clean verify" },
      observation: { outcome: "failed", summary: "exit 1 · MAVEN_VERSION_ERROR" },
      control_seq: [65, 66],
    }
    expect(turn.observation?.outcome).toBe("failed")
    expect(turn.call?.summary).toContain("mvn clean verify")
  })
})
