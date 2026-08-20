import { describe, expect, it } from "vitest"

import type { TestEvidenceLayers, TestSummary } from "@/api/types"

import {
  buildScopeFromRates,
  completeEvidenceCounts,
  presentBuild,
  presentDataNotes,
  presentDiagnostics,
  presentTestRun,
  presentVerifiedIdentities,
  safeRate,
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
    expect(presentVerifiedIdentities(test).summary).toMatch(/module names and stable test identities/i)
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
      summary: "Evidence covers 19 of 26 modules · 4,230 compiled classes · Scope incomplete",
    })
  })

  it("turns internal conflict codes into concise data notes", () => {
    expect(presentDataNotes([
      "build_modules_incomplete",
      "rate_denominator_not_a_bound",
      "unknown_internal_code",
    ])).toEqual([
      "Build evidence covers only part of the discovered module set.",
      "A rate denominator did not bound the observed count, so no percentage is shown.",
      "An additional evidence consistency issue was recorded.",
    ])
  })
})
