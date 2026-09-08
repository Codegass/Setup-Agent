import { describe, expect, it } from "vitest"

import type { BuildSummary, EvidenceCountSummary, ExecutionSessionDetail, TestSummary } from "./types"

const verdictContract = {
  verdict: {
    tone: "attention",
    headline: "Setup verdict unknown",
    verdict: "unknown",
    source: "snapshot",
  },
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
  | "verdict"
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
    expect(verdictContract.verdict.source).toBe("snapshot")
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
