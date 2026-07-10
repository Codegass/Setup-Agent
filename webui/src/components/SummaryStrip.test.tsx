import { describe, expect, it } from "vitest"

import type { WorkspaceSummary } from "@/api/types"
import { rollup } from "./SummaryStrip"

function ws(over: Partial<WorkspaceSummary>): WorkspaceSummary {
  return {
    id: "sag-x",
    project: "x",
    container: "sag-x",
    stack: "maven",
    docker: { status: "exited" },
    task: "",
    build: "none",
    test: { state: "none", pass: 0, fail: 0, skip: 0, total: 0 },
    report: "none",
    changed: 0,
    updated: "",
    ...over,
  } as WorkspaceSummary
}

describe("SummaryStrip rollup", () => {
  it("aggregates builds, pass rate (skips excluded) and execution rate", () => {
    const r = rollup([
      ws({
        docker: { status: "running" },
        build: { state: "success", tool: "maven", time: "", note: "" },
        test: { state: "passed", pass: 90, fail: 5, skip: 10, total: 105, errors: 5, declaredTotal: 200 },
      }),
      ws({
        build: { state: "failed", tool: "maven", time: "", note: "" },
        test: { state: "failed", pass: 10, fail: 0, skip: 0, total: 10, declaredTotal: 50 },
      }),
      ws({}), // no build info, no tests — excluded from denominators
    ])
    expect(r.total).toBe(3)
    expect(r.running).toBe(1)
    expect(r.buildSuccess).toBe(1)
    expect(r.buildKnown).toBe(2)
    expect(r.passed).toBe(100)
    expect(r.executedNonSkip).toBe(110) // 90+5+5 and 10+0+0 — skips excluded
    expect(r.executed).toBe(115)
    expect(r.declared).toBe(250)
  })

  it("handles string build states", () => {
    const r = rollup([ws({ build: "success" }), ws({ build: "unknown" })])
    expect(r.buildKnown).toBe(1)
    expect(r.buildSuccess).toBe(1)
  })
})
