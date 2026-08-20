import { cleanup, render, screen, within } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import type { EvidenceLayerProjectionSummary, WorkspaceSummary } from "@/api/types"
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

function evidenceLayers(): EvidenceLayerProjectionSummary {
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
    expect(r.errors).toBe(5)
    expect(r.executedNonSkip).toBe(110) // 90+5+5 and 10+0+0 — skips excluded
    expect(r.executed).toBe(115)
    expect(r.declared).toBe(250)
  })

  it("handles string build states", () => {
    const r = rollup([ws({ build: "success" }), ws({ build: "unknown" })])
    expect(r.buildKnown).toBe(1)
    expect(r.buildSuccess).toBe(1)
  })

  it("keeps sealed run results separate from verified identity coverage", () => {
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
    expect(r.passed).toBe(12)

    render(<SummaryStrip workspaces={[modern, legacy]} />)
    const identity = screen.getByText("Identity coverage").parentElement
    const diagnostics = screen.getByText("Diagnostics").parentElement
    expect(identity).not.toBeNull()
    expect(diagnostics).not.toBeNull()
    expect(within(identity as HTMLElement).getByText("0/2 measured")).toBeInTheDocument()
    expect(within(diagnostics as HTMLElement).getByText("2,887")).toBeInTheDocument()
    expect(screen.getByText("Run pass rate")).toBeInTheDocument()
    expect(screen.queryByText("Verified pass rate")).not.toBeInTheDocument()
  })

  it("computes the verified pass rate from measured workspaces only", () => {
    const measuredLayers = evidenceLayers()
    measuredLayers.tests.claimed.latestSubjects = {
      executed: 12, passed: 8, failed: 1, errors: 1, skipped: 2,
      availability: "available",
    }
    const measured = ws({
      id: "measured",
      test: { state: "success", pass: 8, fail: 1, errors: 1, skip: 2, total: 12, evidenceLayers: measuredLayers },
    })
    const unmeasured = ws({
      id: "unmeasured",
      test: { state: "success", pass: 20, fail: 0, errors: 0, skip: 0, total: 20, evidenceLayers: evidenceLayers() },
    })

    render(<SummaryStrip workspaces={[measured, unmeasured]} />)

    const identity = screen.getByText("Identity coverage").parentElement
    const verified = screen.getByText("Verified pass rate").parentElement
    expect(within(identity as HTMLElement).getByText("1/2 measured")).toBeInTheDocument()
    expect(within(verified as HTMLElement).getByText("80.0%")).toBeInTheDocument()
    expect(screen.getAllByText(/measured workspace(?:s)? only/i).length).toBeGreaterThan(0)
  })

  it("includes errors in the run-rate denominator and refuses invalid counts", () => {
    const valid = ws({
      test: { state: "failed", pass: 10, fail: 0, errors: 5, skip: 0, total: 15 },
    })
    const invalid = ws({
      id: "invalid",
      test: { state: "failed", pass: 100, fail: -1, errors: 0, skip: 0, total: 99 },
    })

    const { rerender } = render(<SummaryStrip workspaces={[valid]} />)
    expect(screen.getByText("66.7%")).toBeInTheDocument()
    rerender(<SummaryStrip workspaces={[invalid]} />)
    expect(screen.queryByText(/101(?:\.0)?%/)).not.toBeInTheDocument()
    expect(screen.queryByText("Run pass rate")).not.toBeInTheDocument()
  })

  it("counts only explicit sealed run states in run coverage", () => {
    render(<SummaryStrip workspaces={[
      ws({ id: "explicit-zero", test: { state: "success", pass: 0, fail: 0, errors: 0, skip: 0, total: 0 } }),
      ws({ id: "unknown-zero", test: { state: "unknown", pass: 0, fail: 0, errors: 0, skip: 0, total: 0 } }),
    ]} />)

    const coverage = screen.getByText("Run coverage").parentElement
    expect(within(coverage as HTMLElement).getByText("1/2 measured")).toBeInTheDocument()
    expect(screen.queryByText("Run pass rate")).not.toBeInTheDocument()
  })

  it("shows partial builds and lower-bounds incomplete diagnostics", () => {
    const layers = evidenceLayers()
    layers.tests.staleObservations = {
      executed: null, passed: null, failed: null, errors: null, skipped: null,
      availability: "unavailable", reason: "not countable",
    }
    render(<SummaryStrip workspaces={[
      ws({ build: { state: "partial", tool: "maven", time: "", note: "" }, test: { state: "success", pass: 1, fail: 0, skip: 0, total: 1, evidenceLayers: layers } }),
    ]} />)

    expect(screen.getByText("1 partial")).toBeInTheDocument()
    expect(screen.getByText("2,887+")).toBeInTheDocument()
    expect(screen.getByText(/at least 2,887 diagnostics/i)).toBeInTheDocument()
  })
})
