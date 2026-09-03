import { describe, expect, it } from "vitest"

import type { TestEvidenceLayers, TestSummary } from "@/api/types"

import {
  buildScopeFromRates,
  completeEvidenceCounts,
  lowerBoundEvidenceCounts,
  presentBuild,
  presentDataNotes,
  presentDiagnostics,
  presentTestAccounting,
  presentTestRun,
  presentVerifiedIdentities,
  safeRate,
  sourceScopeFromBuild,
} from "./evidencePresentation"

function unavailable(reason = "module-qualified subject/case identity was not sealed") {
  return {
    executed: null,
    passed: null,
    failed: null,
    errors: null,
    skipped: null,
    availability: "unavailable" as const,
    reason,
  }
}

function layers(overrides: Partial<TestEvidenceLayers> = {}): TestEvidenceLayers {
  const zero = {
    executed: 0, passed: 0, failed: 0, errors: 0, skipped: 0, availability: "available" as const,
  }
  return {
    claimed: {
      latestSubjects: unavailable(),
      latestCases: unavailable(),
      receiptExecutions: zero,
    },
    quarantinedObservations: zero,
    unattributedObservations: zero,
    staleObservations: zero,
    ...overrides,
  }
}

function withLayers(test: Partial<TestSummary>, testLayers: TestEvidenceLayers): TestSummary {
  return {
    state: "success",
    pass: 0,
    fail: 0,
    errors: 0,
    skip: 0,
    total: 0,
    ...test,
    evidenceLayers: {
      projectionStatus: "metrics-v2-artifact-unavailable",
      tests: testLayers,
      evidence: {
        integrity: "complete",
        receiptsExpected: 1,
        receiptsPersisted: 1,
        terminalReceiptsUnpersisted: 0,
        conflictCount: 0,
      },
    },
  }
}

describe("evidence presentation", () => {
  it("keeps the sealed runner result visible when verified identities are unavailable", () => {
    const test = withLayers({ state: "success", pass: 4722, total: 4722 }, layers())

    expect(presentTestRun(test)).toMatchObject({
      stateLabel: "Passed",
      passed: 4722,
      failed: 0,
      errors: 0,
      passRate: 100,
    })
    expect(presentVerifiedIdentities(test)).toMatchObject({
      available: false,
      value: "Unavailable",
    })
    expect(presentVerifiedIdentities(test).summary).toMatch(/module and test names were not sealed/i)
    expect(presentVerifiedIdentities(test).summary).not.toMatch(/tests ran/i)
  })

  it("includes errors in the non-skipped denominator and negative count", () => {
    const run = presentTestRun({
      state: "failed", pass: 80, fail: 5, errors: 15, skip: 10, total: 110,
    })
    expect(run.negative).toBe(20)
    expect(run.nonSkipped).toBe(100)
    expect(run.passRate).toBe(80)

    const claimed = presentVerifiedIdentities(withLayers({}, layers({
      claimed: {
        latestSubjects: {
          executed: 110, passed: 80, failed: 5, errors: 15, skipped: 10, availability: "available",
        },
        latestCases: unavailable(),
        receiptExecutions: {
          executed: 0, passed: 0, failed: 0, errors: 0, skipped: 0, availability: "available",
        },
      },
    })))
    expect(claimed).toMatchObject({ available: true, value: "80%", negative: 20 })
  })

  it("accounts for every Commons CLI receipt outcome without treating skips as failures", () => {
    const receiptExecutions = {
      executed: 987,
      passed: 926,
      failed: 0,
      errors: 0,
      skipped: 61,
      availability: "available" as const,
    }
    const accounting = presentTestAccounting(withLayers({}, layers({
      claimed: {
        latestSubjects: unavailable(),
        latestCases: unavailable(),
        receiptExecutions,
      },
    })))

    expect(accounting).toMatchObject({
      available: true,
      accounted: 987,
      nonSkipped: 926,
      counts: {
        executed: 987,
        passed: 926,
        failed: 0,
        errors: 0,
        skipped: 61,
      },
    })
    expect(accounting.summary).toBe(
      "Test outcomes recorded 987 / 987 · Non-skipped passed 926 / 926 · Skipped 61 · Failed / errors 0 / 0",
    )
  })

  it("does not turn an all-skipped receipt into a non-skipped 0 / 0 ratio", () => {
    const accounting = presentTestAccounting(withLayers({}, layers({
      claimed: {
        latestSubjects: unavailable(),
        latestCases: unavailable(),
        receiptExecutions: {
          executed: 7,
          passed: 0,
          failed: 0,
          errors: 0,
          skipped: 7,
          availability: "available",
        },
      },
    })))

    expect(accounting.available).toBe(true)
    expect(accounting.accounted).toBe(7)
    expect(accounting.nonSkipped).toBe(0)
    expect(accounting.summary).toBe(
      "Test outcomes recorded 7 / 7 · Non-skipped passed unavailable · Skipped 7 · Failed / errors 0 / 0",
    )
    expect(accounting.summary).not.toContain("Non-skipped passed 0 / 0")
  })

  it.each([
    {
      label: "bounded",
      receipt: {
        executed: 500,
        passed: 500,
        failed: 0,
        errors: 0,
        skipped: 0,
        availability: "partial" as const,
        bound: "lower" as const,
      },
      copy: /minimum, not the complete count/i,
    },
    {
      label: "unavailable",
      receipt: unavailable("receipt outcomes unavailable"),
      copy: /unavailable/i,
    },
    {
      label: "invalid",
      receipt: {
        executed: 2,
        passed: 2,
        failed: 1,
        errors: 0,
        skipped: 0,
        availability: "available" as const,
      },
      copy: /incomplete or inconsistent/i,
    },
  ])("does not fabricate a $label receipt ratio", ({ receipt, copy }) => {
    const accounting = presentTestAccounting(withLayers({}, layers({
      claimed: {
        latestSubjects: unavailable(),
        latestCases: unavailable(),
        receiptExecutions: receipt,
      },
    })))

    expect(accounting.available).toBe(false)
    expect(accounting.accounted).toBeNull()
    expect(accounting.nonSkipped).toBeNull()
    expect(accounting.summary).toMatch(copy)
    expect(accounting.summary).not.toMatch(/\d[\d,]* \/ \d/)
  })

  it("never presents a Kogito-shaped run with failed results as passed", () => {
    const run = presentTestRun({
      state: "success",
      pass: 165,
      fail: 24,
      errors: 39,
      skip: 0,
      total: 228,
    })

    expect(run).toMatchObject({
      stateLabel: "Failed",
      tone: "red",
      valueClass: "text-status-failed",
      negative: 63,
      nonSkipped: 228,
    })
    expect(run.passRate).toBeCloseTo(72.37, 2)
    expect(run.summary).toMatch(/165 passed · 24 failed · 39 errors/i)
  })

  it("rejects missing, contradictory, and unbounded rates instead of manufacturing a percentage", () => {
    expect(completeEvidenceCounts({
      executed: 2, passed: 2, failed: 0, errors: 1, skipped: 0, availability: "available",
    })).toBeNull()
    expect(safeRate(3, 2)).toBeNull()
    expect(safeRate(0, 0)).toBeNull()
  })

  it("renders a bounded-empty identity sample as an amber zero floor, never a clean zero", () => {
    const boundedEmpty = {
      executed: 0,
      passed: 0,
      failed: 0,
      errors: 0,
      skipped: 0,
      availability: "partial" as const,
      bound: "lower" as const,
      basis: "latest module-qualified subjects (bounded identity sample)",
      reason: "identity rows were bounded and truncated",
    }
    expect(completeEvidenceCounts(boundedEmpty)).toBeNull()
    expect(lowerBoundEvidenceCounts(boundedEmpty)).toEqual({
      executed: 0, passed: 0, failed: 0, errors: 0, skipped: 0,
    })

    const presented = presentVerifiedIdentities(withLayers({}, layers({
      claimed: {
        latestSubjects: boundedEmpty,
        latestCases: unavailable(),
        receiptExecutions: boundedEmpty,
      },
    })))
    expect(presented).toMatchObject({
      available: true,
      partial: true,
      value: "≥0",
      tone: "amber",
      passRate: null,
    })
    expect(presented.summary).toMatch(/at least these per-test results were kept/i)
  })

  it("marks a diagnostic total as a lower bound when any bucket is unavailable", () => {
    const result = presentDiagnostics(layers({
      quarantinedObservations: {
        executed: 266, passed: 13, failed: 0, errors: 253, skipped: 0, availability: "available",
      },
      unattributedObservations: unavailable("unattributed observations unavailable"),
    }))
    expect(result).toMatchObject({ exact: false, knownTotal: 266, value: "266+" })
    expect(result.summary).toMatch(/at least 266/i)
  })

  it("presents partial build scope as counts, never as a percentage", () => {
    const rates = {
      build: { modules: { numerator: 19, denominator: 26, rate: 73.1, band: "most" } },
    }
    expect(buildScopeFromRates(rates, "partial")).toEqual({
      observed: 19, expected: 26, incomplete: true,
    })
    expect(presentBuild({
      state: "partial", tool: "sealed snapshot", time: "—", note: "", classCount: 4230,
    }, rates)).toMatchObject({
      value: "Partial",
      summary: "Modules 19 / 26 · Scope incomplete",
    })
  })

  it("shows comparable production sources but never substitutes compiled classes", () => {
    const build = {
      state: "success",
      tool: "sealed snapshot",
      time: "—",
      note: "",
      classCount: 56,
      sourceScope: {
        covered: 36,
        total: 36,
        availability: "available" as const,
        basis: "sealed physical build success over validated full module scope",
      },
    }
    expect(sourceScopeFromBuild(build)).toEqual({ covered: 36, total: 36 })
    expect(presentBuild(build, {
      build: { modules: { numerator: 1, denominator: 1, rate: 100, band: "fully" } },
    })).toMatchObject({
      value: "Success",
      summary: "Production Java sources 36 / 36 · Modules 1 / 1",
    })
    expect(presentBuild(build).summary).not.toMatch(/class/i)
  })

  it.each([
    { availability: "unavailable" as const, covered: null, total: 36 },
    { availability: "available" as const, covered: 37, total: 36 },
    { availability: "available" as const, covered: 0, total: 0 },
  ])("does not show an invalid or unavailable source-scope ratio", (sourceScope) => {
    const build = {
      state: "success",
      tool: "sealed snapshot",
      time: "—",
      note: "",
      classCount: 56,
      sourceScope,
    }
    expect(sourceScopeFromBuild(build)).toBeNull()
    expect(presentBuild(build).summary).toBe("No comparable build-scope counts were recorded.")
  })

  it("presents phases that were never entered without showing zero coverage", () => {
    expect(presentBuild({
      state: "not_attempted", tool: "—", time: "—", note: "", classCount: 0,
    }, {
      build: { modules: { numerator: 0, denominator: 37, rate: 0, band: "none" } },
    })).toEqual({
      value: "Not run",
      tone: "neutral",
      summary: "Build was not run.",
      scope: null,
      sourceScope: null,
    })

    expect(presentTestRun({
      state: "not_attempted", pass: 0, fail: 0, errors: 0, skip: 0, total: 0,
    })).toMatchObject({
      stateLabel: "Not run",
      countsAvailable: false,
      summary: "Tests were not run.",
    })
  })

  it("turns internal conflict codes into concise data notes", () => {
    expect(presentDataNotes([
      "build_modules_incomplete",
      "test_execution_interrupted",
      "rate_denominator_not_a_bound",
      "unknown_internal_code",
    ])).toEqual([
      "Build evidence covers only part of the discovered module set.",
      "The test runner stopped before the declared scope completed; shown counts are the sealed prefix.",
      "A rate denominator did not bound the observed count, so no percentage is shown.",
      "An additional evidence consistency issue was recorded.",
    ])
  })
})
