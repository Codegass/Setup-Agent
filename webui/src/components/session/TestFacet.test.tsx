import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { TestFacet } from "./TestFacet"

afterEach(() => cleanup())

const single = {
  test: {
    state: "partial", pass: 312, fail: 8, skip: 0, total: 320, passRate: 97.5, uniqueTotal: 318,
    failingNames: ["HelpFormatterTest.testWrappedWidth", "BugCLI162Test.testInfiniteLoop"],
  },
  moduleSummary: { singleModule: true },
  modules: [],
} as any

const multi = {
  test: { state: "partial", pass: 3838, fail: 3, skip: 0, total: 3841, passRate: 99.9, uniqueTotal: 2907, failingNames: [] },
  moduleSummary: { modulesTotal: 24, modulesWithTestFailures: 2, singleModule: false },
  modules: [{ name: "streams", path: "streams", buildStatus: "success", testSource: "runner_xml", testsPassed: 1, testsFailed: 0 }],
} as any

const igniteShape = {
  test: {
    state: "success",
    pass: 2,
    fail: 0,
    skip: 0,
    total: 2,
    evidenceLayers: {
      projectionStatus: "metrics-v2-artifact-unavailable",
      tests: {
        claimed: {
          latestSubjects: {
            executed: null, passed: null, failed: null, errors: null, skipped: null,
            availability: "unavailable", reason: "module-qualified subject/case identity was not sealed",
          },
          latestCases: {
            executed: null, passed: null, failed: null, errors: null, skipped: null,
            availability: "unavailable", reason: "module-qualified subject/case identity was not sealed",
          },
          receiptExecutions: {
            executed: 2, passed: 2, failed: 0, errors: 0, skipped: 0, availability: "available",
          },
        },
        quarantinedObservations: {
          executed: 2887, passed: 267, failed: 28, errors: 2481, skipped: 111, availability: "available",
        },
        unattributedObservations: {
          executed: 0, passed: 0, failed: 0, errors: 0, skipped: 0, availability: "available",
        },
        staleObservations: {
          executed: 0, passed: 0, failed: 0, errors: 0, skipped: 0, availability: "available",
        },
      },
      evidence: {
        integrity: "complete", receiptsExpected: 1, receiptsPersisted: 1,
        terminalReceiptsUnpersisted: 0, conflictCount: 0,
      },
    },
    failingNames: [],
  },
  moduleSummary: { singleModule: true },
  modules: [],
} as any

describe("TestFacet", () => {
  it("shows conclusion + FAILING + a 'View test details' detail for single-module", () => {
    render(<TestFacet detail={single} />)
    expect(screen.getByText(/97\.5% pass/i)).toBeInTheDocument()
    expect(screen.getByText(/Failing · 2/)).toBeInTheDocument()
    expect(screen.getByText("HelpFormatterTest.testWrappedWidth")).toBeInTheDocument()
    // Single-module gets a "View test details" affordance (not "per-module breakdown").
    expect(screen.queryByRole("button", { name: /per-module breakdown/i })).not.toBeInTheDocument()
    const btn = screen.getByRole("button", { name: /view test details/i })
    fireEvent.click(btn)
    expect(screen.getByRole("dialog", { name: /test details/i })).toBeInTheDocument()
  })

  it("opens the per-module breakdown modal for a multi-module project", () => {
    render(<TestFacet detail={multi} />)
    fireEvent.click(screen.getByRole("button", { name: /per-module breakdown/i }))
    expect(screen.getByRole("dialog", { name: /per-module test breakdown/i })).toBeInTheDocument()
    expect(screen.getByText("streams")).toBeInTheDocument()
  })

  it("separates claimed grains from non-verdict-bearing observations", () => {
    render(<TestFacet detail={igniteShape} />)

    expect(screen.getByText("Claimed latest subjects")).toBeInTheDocument()
    expect(screen.getAllByText("Unavailable").length).toBeGreaterThan(0)
    expect(screen.queryByText(/100% pass/i)).not.toBeInTheDocument()
    expect(screen.queryByRole("img", { name: /2 passed, 0 failed, 2 total/i })).not.toBeInTheDocument()
    expect(screen.getByText("Receipt executions")).toBeInTheDocument()
    expect(screen.getByText("2 / 2 passed")).toBeInTheDocument()
    expect(screen.getByText("Quarantined observations · not verdict-bearing")).toBeInTheDocument()
    expect(screen.getByText("267 / 2,887 passed")).toBeInTheDocument()
    expect(screen.getByText("Evidence transport")).toBeInTheDocument()
    expect(screen.getByText("complete")).toBeInTheDocument()
  })
})
