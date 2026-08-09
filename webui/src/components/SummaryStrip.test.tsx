import { cleanup, render, screen, within } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import type { WorkspaceSummary } from "@/api/types"
import { rollup, SummaryStrip } from "./SummaryStrip"

afterEach(() => cleanup())

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

function evidenceLayers() {
  const unavailable = {
    executed: null, passed: null, failed: null, errors: null, skipped: null,
    availability: "unavailable" as const, reason: "module-qualified subject identity unavailable",
  }
  return {
    projectionStatus: "metrics-v2-artifact-unavailable" as const,
    tests: {
      claimed: {
        latestSubjects: unavailable,
        latestCases: unavailable,
        receiptExecutions: {
          executed: 2, passed: 2, failed: 0, errors: 0, skipped: 0, availability: "available" as const,
        },
      },
      quarantinedObservations: {
        executed: 2887, passed: 267, failed: 28, errors: 2481, skipped: 111, availability: "available" as const,
      },
      unattributedObservations: {
        executed: 0, passed: 0, failed: 0, errors: 0, skipped: 0, availability: "available" as const,
      },
      staleObservations: {
        executed: 0, passed: 0, failed: 0, errors: 0, skipped: 0, availability: "available" as const,
      },
    },
    evidence: {
      integrity: "complete" as const, receiptsExpected: 1, receiptsPersisted: 1,
      terminalReceiptsUnpersisted: 0, conflictCount: 0,
    },
  }
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

  it("does not merge legacy primary counts into claimed subject KPIs", () => {
    const modern = ws({
      test: {
        state: "success", pass: 2, fail: 0, skip: 0, total: 2,
        evidenceLayers: evidenceLayers(),
      },
    })
    const legacy = ws({
      id: "legacy",
      test: { state: "success", pass: 10, fail: 0, skip: 0, total: 10 },
    })
    const r = rollup([modern, legacy])

    expect(r.claimedSubjectWorkspaces).toBe(1)
    expect(r.claimedSubjectUnavailable).toBe(1)
    expect(r.claimedSubjectExecuted).toBe(0)
    expect(r.nonVerdictObservations).toBe(2887)
    expect(r.passed).toBe(10)

    render(<SummaryStrip workspaces={[modern, legacy]} />)
    const subjects = screen.getByText("Claimed subjects").parentElement
    const diagnostics = screen.getByText("Diagnostics").parentElement
    expect(subjects).not.toBeNull()
    expect(diagnostics).not.toBeNull()
    expect(within(subjects as HTMLElement).getByText("Unavailable")).toBeInTheDocument()
    expect(within(diagnostics as HTMLElement).getByText("2,887")).toBeInTheDocument()
    expect(screen.getByText("Legacy tests")).toBeInTheDocument()
    expect(screen.queryByText("Pass rate")).not.toBeInTheDocument()
  })
})
