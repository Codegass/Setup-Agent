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
  it.each([
    ["evaluated", "Official CI: met; scope score 1/1; cell maven-17"],
    ["evaluated", "Official CI: partial; scope score unavailable; cell maven-17"],
    ["unavailable", "Official CI: unavailable (authorization missing)"],
  ] as const)("shows sealed CI comparison %s independently of local execution", (status, line) => {
    render(<OverviewTab detail={makeDetail({
      canonicalVerdict: "success",
      ciComparison: { status },
      ciComparisonLines: [line, "CI lifecycle parity: unavailable"],
    })} onOpenFlow={() => {}} />)
    const comparison = screen.getByRole("region", { name: "Official CI comparison" })
    expect(within(comparison).getByText(line)).toBeInTheDocument()
    expect(within(comparison).getByText("CI lifecycle parity: unavailable")).toBeInTheDocument()
    expect(comparison).not.toHaveClass("text-status-success")
  })

  it("does not invent a CI result for a historical session", () => {
    render(<OverviewTab detail={makeDetail()} onOpenFlow={() => {}} />)
    expect(screen.getByText("Official CI: unavailable (no sealed comparison)")).toBeInTheDocument()
  })

  it("invokes onOpenFlow when the goal button is clicked", () => {
    const onOpenFlow = vi.fn()
    render(<OverviewTab detail={makeDetail()} onOpenFlow={onOpenFlow} />)
    fireEvent.click(screen.getByRole("button", { name: /view flow/i }))
    expect(onOpenFlow).toHaveBeenCalledTimes(1)
  })

  it("shows the Commons CLI source and receipt outcome accounting", () => {
    const evidenceLayers = unavailableSubjectLayers()
    evidenceLayers.tests.claimed.receiptExecutions = {
      executed: 987,
      passed: 926,
      failed: 0,
      errors: 0,
      skipped: 61,
      availability: "available",
    }
    render(
      <OverviewTab
        detail={makeDetail({
          build: {
            state: "success",
            tool: "sealed snapshot",
            time: "—",
            note: "Canonical build evidence from verdict.json",
            classCount: 56,
          },
          rates: {
            build: { modules: { numerator: 1, denominator: 1, rate: 100, band: "fully" } },
          },
          test: {
            state: "success",
            pass: 926,
            fail: 0,
            errors: 0,
            skip: 61,
            total: 987,
            evidenceLayers,
          },
        })}
        onOpenFlow={() => {}}
      />,
    )

    const buildTile = screen.getAllByText("Build")[0].parentElement
    expect(within(buildTile as HTMLElement).getByText("Success")).toHaveClass("text-status-success")
    expect(within(buildTile as HTMLElement).getByText(/Modules built 1 of 1 declared on disk \(diagnostic\)/)).toBeInTheDocument()
    expect(within(buildTile as HTMLElement).getByText(/Class files 56 \(diagnostic\)/i)).toBeInTheDocument()

    const testTile = screen.getAllByText("Tests")[0].parentElement
    expect(within(testTile as HTMLElement).getByText("Executed")).toHaveClass("text-status-success")
    expect(within(testTile as HTMLElement).getByText(/Test outcomes recorded 987 \/ 987/)).toBeInTheDocument()
    expect(within(testTile as HTMLElement).getByText(/Non-skipped passed 926 \/ 926/)).toBeInTheDocument()
    expect(within(testTile as HTMLElement).getByText(/Skipped 61/)).toBeInTheDocument()
    expect(within(testTile as HTMLElement).getByText(/Failed \/ errors 0 \/ 0/)).toBeInTheDocument()
    expect(screen.queryByText("Verified per-test results")).not.toBeInTheDocument()
  })

  it("shows partial sealed build evidence instead of empty build tiles", () => {
    render(
      <OverviewTab
        detail={makeDetail({
          build: {
            state: "partial",
            tool: "sealed snapshot",
            time: "—",
            note: "Canonical build evidence from verdict.json",
            classCount: 16221,
          },
          moduleSummary: undefined,
          modules: [],
        })}
        onOpenFlow={() => {}}
      />,
    )

    const buildTile = screen.getAllByText("Build")[0].parentElement
    expect(buildTile).not.toBeNull()
    expect(within(buildTile as HTMLElement).getByText("Partial")).toHaveClass(
      "text-status-attention",
    )
    expect(within(buildTile as HTMLElement).getByText("Build stopped before completion.")).toBeInTheDocument()
    expect(within(buildTile as HTMLElement).queryByText(/16,221 compiled classes/)).not.toBeInTheDocument()
    expect(screen.queryByText("Build time")).not.toBeInTheDocument()
    expect(screen.getByText("Module details unavailable")).toBeInTheDocument()
  })

  it("does not present unknown test counts as zero failures", () => {
    render(
      <OverviewTab
        detail={makeDetail({
          build: { state: "unknown", tool: "—", time: "—", note: "" },
          moduleSummary: undefined,
          modules: [],
          test: { state: "unknown", pass: 0, fail: 0, skip: 0, total: 0 },
        })}
        onOpenFlow={() => {}}
      />,
    )

    const runTile = screen.getAllByText("Tests")[0].parentElement
    expect(within(runTile as HTMLElement).getByText("Unavailable")).not.toHaveClass("text-status-success")
    expect(within(runTile as HTMLElement).getByText(/did not record its test outcome totals/i)).toBeInTheDocument()
    expect(screen.queryByText("Verified per-test results")).not.toBeInTheDocument()
    const buildTile = screen.getAllByText("Build")[0].parentElement
    expect(within(buildTile as HTMLElement).getByText("Unavailable")).toBeInTheDocument()
    expect(screen.queryByText("Build time")).not.toBeInTheDocument()
  })

  it("keeps diagnostic observations secondary to receipt outcome accounting", () => {
    render(
      <OverviewTab
        detail={makeDetail({
          test: {
            state: "success",
            pass: 4722,
            fail: 0,
            skip: 0,
            total: 4722,
            evidenceLayers: unavailableSubjectLayers(),
          },
        })}
        onOpenFlow={() => {}}
      />,
    )

    const runTile = screen.getAllByText("Tests")[0].parentElement
    expect(within(runTile as HTMLElement).getByText("Executed")).toHaveClass("text-status-success")
    expect(within(runTile as HTMLElement).getByText(/Test outcomes recorded 2 \/ 2/i)).toBeInTheDocument()
    expect(screen.queryByText("Verified per-test results")).not.toBeInTheDocument()
    expect(screen.getByText("Diagnostic observations")).toBeInTheDocument()
    expect(screen.getByText("2,887")).toBeInTheDocument()
    expect(screen.getByText(/2,481 errors/)).toBeInTheDocument()
  })

  it("shows observed module scope as counts and keeps a partial build amber", () => {
    const { rerender } = render(
      <OverviewTab
        detail={makeDetail({
          build: { state: "partial", tool: "sealed snapshot", time: "—", note: "", classCount: 4230 },
          rates: {
            build: { modules: { numerator: 19, denominator: 26, rate: 73.1, band: "most" } },
          },
        })}
        onOpenFlow={() => {}}
      />,
    )
    const partialBuild = screen.getAllByText("Build")[0].parentElement
    expect(within(partialBuild as HTMLElement).getByText("Partial")).toHaveClass("text-status-attention")
    expect(within(partialBuild as HTMLElement).getByText("Build stopped before completion.")).toBeInTheDocument()
    expect(within(partialBuild as HTMLElement).queryByText("73.1%")).not.toBeInTheDocument()

    rerender(
      <OverviewTab
        detail={makeDetail({
          build: { state: "success", tool: "maven", time: "—", note: "", classCount: 4230 },
          rates: {
            build: { modules: { numerator: 26, denominator: 26, rate: 100, band: "fully" } },
          },
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
    const cleanBuild = screen.getAllByText("Build")[0].parentElement
    expect(within(cleanBuild as HTMLElement).getByText("Success")).toHaveClass("text-status-success")
    expect(within(cleanBuild as HTMLElement).getByText(/Modules built 26 of 26 declared on disk/i)).toBeInTheDocument()
    expect(within(cleanBuild as HTMLElement).queryByText(/scope incomplete/i)).not.toBeInTheDocument()
  })

  it("keeps line and branch coverage in a secondary row", () => {
    render(<OverviewTab detail={makeDetail()} onOpenFlow={() => {}} />)
    const lineCoverage = screen.getByText(/line coverage/i)
    expect(lineCoverage).toBeInTheDocument()
    expect(screen.getByText(/branch coverage/i)).toBeInTheDocument()
    expect(screen.getByText("79.2%")).toBeInTheDocument()
    expect(lineCoverage.parentElement?.parentElement).toHaveClass("mt-3")
  })

  it("marks diagnostic observation counts as a lower bound when one group is unavailable", () => {
    const evidenceLayers = unavailableSubjectLayers()
    evidenceLayers.tests.unattributedObservations = {
      executed: null,
      passed: null,
      failed: null,
      errors: null,
      skipped: null,
      availability: "unavailable",
      reason: "observation identity unavailable",
    } as any
    render(
      <OverviewTab
        detail={makeDetail({
          test: {
            state: "success",
            pass: 4722,
            fail: 0,
            errors: 0,
            skip: 0,
            total: 4722,
            evidenceLayers,
          },
        })}
        onOpenFlow={() => {}}
      />,
    )

    expect(screen.getByText("2,887+")).toBeInTheDocument()
    expect(screen.getByText(/at least 2,887 observations were excluded/i)).toBeInTheDocument()
  })

  it.each([
    ["failed", "Final report not delivered", /could not be delivered/i],
    ["skipped", "Final report delivery skipped", /did not send a final report/i],
  ] as const)("surfaces %s report delivery without hiding the sealed results", (status, title, body) => {
    render(
      <OverviewTab
        detail={makeDetail({ reportDeliveryStatus: status })}
        onOpenFlow={() => {}}
      />,
    )

    expect(screen.getByText(title)).toBeInTheDocument()
    expect(screen.getByText(body)).toBeInTheDocument()
    expect(screen.getAllByText("Tests")[0]).toBeInTheDocument()
  })

  it("translates internal conflicts into secondary data notes", () => {
    render(
      <OverviewTab
        detail={makeDetail({
          canonicalVerdict: "partial",
          test: {
            state: "partial",
            pass: 1186,
            fail: 7,
            skip: 12,
            total: 1205,
            conflicts: [
              "build_modules_incomplete",
              "test_executions_unattributed_to_receipts",
              "rate_denominator_not_a_bound",
            ],
          },
        })}
        onOpenFlow={() => {}}
      />,
    )

    expect(screen.getByText("Why this result is partial")).toBeInTheDocument()
    expect(screen.getByText(/build evidence covers only part of the discovered module set/i)).toBeInTheDocument()
    expect(screen.getByText(/could not be linked to their recorded tool executions/i)).toBeInTheDocument()
    expect(screen.getByText(/no percentage is shown/i)).toBeInTheDocument()
    expect(screen.queryByText("build_modules_incomplete")).not.toBeInTheDocument()
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
