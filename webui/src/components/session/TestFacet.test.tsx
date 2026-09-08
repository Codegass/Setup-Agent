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
    expect(screen.getByText(/97\.5% of non-skipped results passed/i)).toBeInTheDocument()
    expect(screen.getByText(/test results: 312 passed · 8 failed · 0 errors/i)).toBeInTheDocument()
    expect(screen.getByText(/Failing · 2/)).toBeInTheDocument()
    expect(screen.getByText("HelpFormatterTest.testWrappedWidth")).toBeInTheDocument()
    // Single-module gets a "View test details" affordance (not "per-module breakdown").
    expect(screen.queryByRole("button", { name: /per-module breakdown/i })).not.toBeInTheDocument()
    const btn = screen.getByRole("button", { name: /view test details/i })
    fireEvent.click(btn)
    expect(screen.getByRole("dialog", { name: /test details/i })).toBeInTheDocument()
  })

  it("shows completed execution beside red project outcomes", () => {
    render(<TestFacet detail={{ ...single, test: { ...single.test, state: "success" } }} />)
    expect(screen.getByText("Executed")).toHaveClass("text-status-success")
    expect(screen.getByText(/test results: 312 passed · 8 failed · 0 errors/i)).toBeInTheDocument()
    expect(screen.queryByText("Failed")).not.toBeInTheDocument()
  })

  it("opens the per-module breakdown modal for a multi-module project", () => {
    render(<TestFacet detail={multi} />)
    fireEvent.click(screen.getByRole("button", { name: /per-module breakdown/i }))
    expect(screen.getByRole("dialog", { name: /per-module test details/i })).toBeInTheDocument()
    expect(screen.getByText("streams")).toBeInTheDocument()
  })

  it("separates a sealed run from verified identities and diagnostic observations", () => {
    render(<TestFacet detail={igniteShape} />)

    expect(screen.getByText("Test execution")).toBeInTheDocument()
    expect(screen.getByText("Executed")).toHaveClass("text-status-success")
    expect(screen.getByText(/test results: 2 passed · 0 failed · 0 errors/i)).toBeInTheDocument()
    expect(screen.getByText(/100% of non-skipped results passed/i)).toBeInTheDocument()
    expect(screen.getByRole("img", { name: /2 passed, 0 failed, 2 total/i })).toBeInTheDocument()

    expect(screen.getByText("Verified per-test results")).toBeInTheDocument()
    expect(screen.getAllByText("Unavailable").length).toBeGreaterThan(0)
    expect(screen.getByText(/module and test names were not sealed/i)).toBeInTheDocument()

    expect(screen.getByText("Evidence details")).toBeInTheDocument()
    expect(screen.getByText("Recorded test executions")).toBeInTheDocument()
    expect(screen.getByText("2 / 2 passed")).toBeInTheDocument()
    expect(screen.getByText("Excluded observations")).toBeInTheDocument()
    expect(screen.getByText("267 / 2,887 passed")).toBeInTheDocument()
    expect(screen.getByText("Diagnostic observations")).toBeInTheDocument()
    expect(screen.getByText("2,887")).toBeInTheDocument()
    expect(screen.getByText("Evidence records")).toBeInTheDocument()
    expect(screen.getByText("Complete")).toBeInTheDocument()
  })

  it("shows incomplete suite totals as a named lower bound, not an exact tool-run rate", () => {
    const incompleteTotals = JSON.parse(JSON.stringify(igniteShape))
    incompleteTotals.test.evidenceLayers.tests.claimed.receiptExecutions = {
      executed: 2048,
      passed: 2040,
      failed: 8,
      errors: 0,
      skipped: 0,
      availability: "partial",
      bound: "lower",
      basis: "gradle suite totals over the claimed reports the read reached",
      reason: "gradle suite totals were incomplete (disclosed bounds: unsummarized_files)",
    }

    render(<TestFacet detail={incompleteTotals} />)

    expect(screen.getByText("Recorded test executions")).toBeInTheDocument()
    expect(screen.getByText("≥2,048 retained")).toHaveClass("text-status-attention")
    expect(screen.getByText(/lower bound; complete total unavailable/i)).toBeInTheDocument()
    expect(screen.getByText(/disclosed bounds: unsummarized_files/i)).toBeInTheDocument()
    expect(screen.queryByText("2,040 / 2,048 passed")).not.toBeInTheDocument()
  })

  it("counts errors as negative non-skipped results", () => {
    render(
      <TestFacet
        detail={{
          ...single,
          test: {
            state: "failed",
            pass: 80,
            fail: 5,
            errors: 15,
            skip: 10,
            total: 110,
            failingNames: [],
          },
        }}
      />,
    )

    expect(screen.getByText(/80% of non-skipped results passed/i)).toBeInTheDocument()
    expect(screen.getByText(/80 passed · 5 failed · 15 errors · 10 skipped/i)).toBeInTheDocument()
    expect(screen.getByRole("img", { name: /80 passed, 20 failed, 100 total/i })).toBeInTheDocument()
  })

  it("does not call absent module metrics a single-module project", () => {
    render(<TestFacet detail={{ ...single, moduleSummary: undefined, modules: [] }} />)

    expect(screen.getByText(/detailed module test metrics were not produced/i)).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /view test details/i })).not.toBeInTheDocument()
  })
})
