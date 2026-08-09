import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { ExecutionSessionDetail } from "@/api/types"

import { OverviewTab } from "./OverviewTab"

afterEach(() => cleanup())

function makeDetail(overrides: Partial<ExecutionSessionDetail> = {}): ExecutionSessionDetail {
  return {
    id: "S1",
    workspace: "sag-acme",
    title: "t",
    status: "partial",
    entry: "SAG",
    start: "now",
    duration: "8m 01s",
    outcome: "⚠️ PARTIAL",
    build: { state: "success", tool: "maven", time: "2m 41s", note: "mvn -B -T1C verify" },
    test: { state: "partial", pass: 1186, fail: 7, skip: 12, total: 1205 },
    modules: [
      {
        name: "acme-core",
        path: "modules/acme-core",
        buildStatus: "success",
        buildSource: "reactor",
        testSource: "runner_xml",
        testsTotal: 542,
        testsPassed: 540,
        testsFailed: 2,
        failingNames: [],
        failingCount: 0,
        lineRate: 86.4,
        branchRate: 74.1,
      },
      {
        name: "acme-cli",
        path: "modules/acme-cli",
        buildStatus: "failure",
        buildSource: "reactor",
        testSource: "runner_xml",
        testsTotal: 85,
        testsPassed: 67,
        testsFailed: 18,
        failingNames: ["cli.LoginTest.shouldA", "cli.LoginTest.shouldB"],
        failingCount: 6,
      },
    ],
    moduleSummary: {
      modulesTotal: 4,
      modulesBuilt: 3,
      modulesFailed: 1,
      modulesSkipped: 0,
      modulesWithTestFailures: 2,
      buildSystems: ["maven"],
      singleModule: false,
      lineRate: 79.2,
      branchRate: 67.8,
    },
    report: "ready",
    evidence: [],
    logs: [],
    context: {
      trunk: { goal: "Build and test acme-platform", state: "partial", progress: { done: 4, total: 5 }, summary: "" },
      phases: [],
      debug: {},
    },
    ...overrides,
  }
}

function unavailableSubjectLayers() {
  const unavailable = {
    executed: null,
    passed: null,
    failed: null,
    errors: null,
    skipped: null,
    availability: "unavailable" as const,
    reason: "module-qualified subject/case identity was not sealed",
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
      retriedCases: null,
      flakyCases: null,
    },
    evidence: {
      integrity: "complete" as const,
      receiptsExpected: 1,
      receiptsPersisted: 1,
      terminalReceiptsUnpersisted: 0,
      conflictCount: 0,
    },
  }
}

describe("OverviewTab", () => {
  it("invokes onOpenFlow when the goal button is clicked", () => {
    const onOpenFlow = vi.fn()
    render(<OverviewTab detail={makeDetail()} onOpenFlow={onOpenFlow} />)
    fireEvent.click(screen.getByRole("button", { name: /view flow/i }))
    expect(onOpenFlow).toHaveBeenCalledTimes(1)
  })

  it("renders KPI tiles for pass rate, failing tests and modules built", () => {
    render(<OverviewTab detail={makeDetail()} onOpenFlow={() => {}} />)
    expect(screen.getByText(/pass rate/i)).toBeInTheDocument()
    expect(screen.getByText("98.4%")).toBeInTheDocument() // 1186/1205
    expect(screen.getByText(/failing tests/i)).toBeInTheDocument()
    expect(screen.getByText("7")).toBeInTheDocument()
    expect(screen.getByText(/modules built/i)).toBeInTheDocument()
    expect(screen.getByText("3 / 4")).toBeInTheDocument()
  })

  it("does not turn an Ignite-shaped primary 2/2 into a global green KPI", () => {
    render(
      <OverviewTab
        detail={makeDetail({
          test: {
            state: "success",
            pass: 2,
            fail: 0,
            skip: 0,
            total: 2,
            evidenceLayers: unavailableSubjectLayers(),
          },
        })}
        onOpenFlow={() => {}}
      />,
    )

    const passTile = screen.getByText("Claimed subject pass rate").parentElement
    const failuresTile = screen.getByText("Claimed subject failures").parentElement
    expect(passTile).not.toBeNull()
    expect(failuresTile).not.toBeNull()
    expect(within(passTile as HTMLElement).getByText("—")).not.toHaveClass("text-status-success")
    expect(within(failuresTile as HTMLElement).getByText("—")).toBeInTheDocument()
    expect(screen.queryByText("100%" )).not.toBeInTheDocument()
    expect(screen.getByText(/module-qualified subject\/case identity was not sealed/i)).toBeInTheDocument()
    expect(screen.getByText(/2,887 quarantined observations · not verdict-bearing/i)).toBeInTheDocument()
  })

  it("colors the modules-built tile amber on a partial build and green on a clean build", () => {
    const { rerender } = render(<OverviewTab detail={makeDetail()} onOpenFlow={() => {}} />)
    // 3 / 4 with one failure → amber (attention)
    expect(screen.getByText("3 / 4")).toHaveClass("text-status-attention")

    rerender(
      <OverviewTab
        detail={makeDetail({
          moduleSummary: {
            modulesTotal: 4,
            modulesBuilt: 4,
            modulesFailed: 0,
            modulesSkipped: 0,
            modulesWithTestFailures: 0,
            buildSystems: ["maven"],
            singleModule: false,
            lineRate: 79.2,
            branchRate: 67.8,
          },
        })}
        onOpenFlow={() => {}}
      />,
    )
    // 4 / 4 with no failures → green (success)
    expect(screen.getByText("4 / 4")).toHaveClass("text-status-success")
  })

  it("renders coverage tiles when the module summary carries rates", () => {
    render(<OverviewTab detail={makeDetail()} onOpenFlow={() => {}} />)
    expect(screen.getByText(/line coverage/i)).toBeInTheDocument()
    expect(screen.getByText("79.2%")).toBeInTheDocument()
  })

  it("omits coverage tiles when the module summary has no rates", () => {
    render(
      <OverviewTab
        detail={makeDetail({
          moduleSummary: {
            modulesTotal: 4,
            modulesBuilt: 3,
            modulesFailed: 1,
            modulesSkipped: 0,
            modulesWithTestFailures: 2,
            buildSystems: ["maven"],
            singleModule: false,
            lineRate: null,
            branchRate: null,
          },
        })}
        onOpenFlow={() => {}}
      />,
    )
    expect(screen.queryByText(/line coverage/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/branch coverage/i)).not.toBeInTheDocument()
  })

  it("renders the per-module overview table and the needs-attention card", () => {
    render(<OverviewTab detail={makeDetail()} onOpenFlow={() => {}} />)
    // overview ModuleTable header columns
    expect(screen.getByText("Line cov")).toBeInTheDocument()
    expect(screen.getByText("Branch cov")).toBeInTheDocument()
    // NeedsAttention surfaces the failing module + names
    expect(screen.getByText(/needs attention/i)).toBeInTheDocument()
    expect(screen.getByText("cli.LoginTest.shouldA")).toBeInTheDocument()
  })
})
