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

  it("separates the sealed test run, verified identities, and canonical build", () => {
    render(<OverviewTab detail={makeDetail()} onOpenFlow={() => {}} />)
    const runTile = screen.getByText("Test run").parentElement
    expect(runTile).not.toBeNull()
    expect(within(runTile as HTMLElement).getByText("Failed")).toHaveClass("text-status-failed")
    expect(within(runTile as HTMLElement).getByText(/1,186 passed · 7 failed · 0 errors/)).toBeInTheDocument()
    expect(within(runTile as HTMLElement).getByText(/99.4% of non-skipped results passed/)).toBeInTheDocument()

    const identityTile = screen.getByText("Verified test identities").parentElement
    expect(within(identityTile as HTMLElement).getByText("Unavailable")).toBeInTheDocument()
    expect(within(identityTile as HTMLElement).getByText(/verified test identities were not produced/i)).toBeInTheDocument()

    const buildTile = screen.getAllByText("Build")[0].parentElement
    expect(within(buildTile as HTMLElement).getByText("Passed")).toHaveClass("text-status-success")
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
    expect(within(buildTile as HTMLElement).getByText(/16,221 compiled classes/)).toBeInTheDocument()
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

    const runTile = screen.getByText("Test run").parentElement
    expect(within(runTile as HTMLElement).getByText("Unavailable")).not.toHaveClass("text-status-success")
    expect(within(runTile as HTMLElement).getByText(/counts were not recorded/i)).toBeInTheDocument()
    const identityTile = screen.getByText("Verified test identities").parentElement
    expect(within(identityTile as HTMLElement).getByText("Unavailable")).toBeInTheDocument()
    const buildTile = screen.getAllByText("Build")[0].parentElement
    expect(within(buildTile as HTMLElement).getByText("Unavailable")).toBeInTheDocument()
    expect(screen.queryByText("Build time")).not.toBeInTheDocument()
  })

  it("shows a Jackrabbit-shaped 4,722-result run without inventing verified identities", () => {
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

    const runTile = screen.getByText("Test run").parentElement
    expect(within(runTile as HTMLElement).getByText("Passed")).toHaveClass("text-status-success")
    expect(within(runTile as HTMLElement).getByText(/sealed run results: 4,722 passed/i)).toBeInTheDocument()
    expect(within(runTile as HTMLElement).getByText(/100% of non-skipped results passed/i)).toBeInTheDocument()

    const identityTile = screen.getByText("Verified test identities").parentElement
    expect(within(identityTile as HTMLElement).getByText("Unavailable")).toBeInTheDocument()
    expect(within(identityTile as HTMLElement).getByText(/module-qualified test identities were not sealed/i)).toBeInTheDocument()
    expect(screen.getByText("Diagnostic observations")).toBeInTheDocument()
    expect(screen.getByText("2,887")).toBeInTheDocument()
    expect(screen.getByText(/2,481 errors/)).toBeInTheDocument()
  })

  it("shows the formal rate and failures when verified identities are available", () => {
    const evidenceLayers = unavailableSubjectLayers()
    evidenceLayers.tests.claimed.latestSubjects = {
      executed: 276,
      passed: 179,
      failed: 24,
      errors: 44,
      skipped: 29,
      availability: "available",
    } as any
    render(
      <OverviewTab
        detail={makeDetail({
          test: {
            state: "partial",
            pass: 179,
            fail: 24,
            errors: 44,
            skip: 29,
            total: 276,
            evidenceLayers,
          },
        })}
        onOpenFlow={() => {}}
      />,
    )

    const identityTile = screen.getByText("Verified test identities").parentElement
    expect(within(identityTile as HTMLElement).getByText("72.5%")).toHaveClass("text-status-failed")
    expect(within(identityTile as HTMLElement).getByText(/276 verified identities/)).toBeInTheDocument()
    expect(within(identityTile as HTMLElement).getByText(/24 failed · 44 errors · 29 skipped/)).toBeInTheDocument()
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
    expect(within(partialBuild as HTMLElement).getByText(/evidence covers 19 of 26 modules/i)).toBeInTheDocument()
    expect(within(partialBuild as HTMLElement).getByText(/scope incomplete/i)).toBeInTheDocument()
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
    expect(within(cleanBuild as HTMLElement).getByText("Passed")).toHaveClass("text-status-success")
    expect(within(cleanBuild as HTMLElement).getByText(/evidence covers 26 of 26 modules/i)).toBeInTheDocument()
    expect(within(cleanBuild as HTMLElement).queryByText(/scope incomplete/i)).not.toBeInTheDocument()
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
    expect(screen.getByText("Test run")).toBeInTheDocument()
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
