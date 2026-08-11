import { describe, expect, it } from "vitest"

import type { ExecutionSessionDetail, TestSummary } from "./types"

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
})
