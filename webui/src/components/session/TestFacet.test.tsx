import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
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

  it("files what the run counted under one Evidence accounting block", () => {
    render(<TestFacet detail={igniteShape} />)

    expect(screen.getByText("Test execution")).toBeInTheDocument()
    expect(screen.getByRole("img", { name: /2 passed, 0 failed, 2 total/i })).toBeInTheDocument()

    // One block, opened on demand, in place of the three that said the same
    // things in three vocabularies.
    expect(screen.getByText("Evidence accounting")).toBeInTheDocument()
    expect(screen.queryByText("Evidence details")).not.toBeInTheDocument()
    expect(screen.queryByText("Verified per-test results")).not.toBeInTheDocument()
    expect(screen.queryByText("Diagnostic observations")).not.toBeInTheDocument()

    fireEvent.click(screen.getByText("Evidence accounting"))
    expect(screen.getByText("Results bound to this run's receipts")).toBeVisible()
    expect(
      screen.getByText("2 executed · 2 passed · 0 failed · 0 errors · 0 skipped"),
    ).toBeVisible()
    expect(
      screen.getByText("2,887 executed · 267 passed · 28 failed · 2,481 errors · 111 skipped"),
    ).toBeVisible()
  })

  it("never puts the payload's own reason wording on the screen", () => {
    // `igniteShape`'s reason is the one 434 of the 674 archived runs state, and
    // it contains two words this UI may not print. The old panel printed it
    // twice: once as an identities summary, once as a "Source note".
    const { container } = render(<TestFacet detail={igniteShape} />)
    fireEvent.click(screen.getByText("Evidence accounting"))
    const text = (container.textContent ?? "").toLowerCase()
    expect(text).not.toContain("sealed")
    expect(text).not.toContain("subject")
    expect(text).not.toContain("source note")
    expect(screen.getAllByText(/the run did not record module and test names/)[0]).toBeVisible()
  })

  it("states the headline the result card states, rather than working it out again", () => {
    const carded = {
      ...igniteShape,
      resultCard: {
        schemaVersion: 1,
        runId: "r",
        verdict: "partial",
        verdictSource: "snapshot",
        rows: [
          {
            key: "tests",
            label: "Tests",
            status: "unavailable",
            tone: "attention",
            headline: "≥2,688 executed · 2,662 passed · 2 failed · 11 errors · 13 skipped",
            detail: "99.5% of non-skipped passed · 2,692 raw executions",
            reason: "the run recorded test outcomes but no judgment about them",
          },
        ],
        stats: {},
        attention: [],
        notes: [],
      },
    } as any

    render(<TestFacet detail={carded} />)
    expect(
      screen.getByText("≥2,688 executed · 2,662 passed · 2 failed · 11 errors · 13 skipped"),
    ).toBeInTheDocument()
    expect(
      screen.getByText("99.5% of non-skipped passed · 2,692 raw executions"),
    ).toBeInTheDocument()
    expect(
      screen.getByText("the run recorded test outcomes but no judgment about them"),
    ).toBeInTheDocument()
    // The card said `unavailable`; the tab may not say `Executed` beside it.
    expect(screen.queryByText("Executed")).not.toBeInTheDocument()
  })

  it("shows the failing names whether or not the run recorded evidence layers", () => {
    const failing = {
      ...igniteShape,
      test: { ...igniteShape.test, failingNames: ["BugCLI162Test.testInfiniteLoop"] },
    } as any
    render(<TestFacet detail={failing} />)
    expect(screen.getByText("BugCLI162Test.testInfiniteLoop")).toBeInTheDocument()
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

  it("lists the commands that wrote test reports, and only those", () => {
    // `testsReported` alone was the plan's filter. It is null on 955 of the
    // 1,195 archived receipts and on five of the six real sessions checked
    // here — including runs whose commands plainly wrote test reports — so a
    // tab filtered on it says "nothing was recorded" about a run that recorded
    // 78 commands. A command that wrote a test report is the real division.
    const withReceipts = {
      ...igniteShape,
      receipts: [
        {
          receiptId: "wrote-reports",
          tool: "maven",
          argv: "mvn --fail-at-end -Dmaven.test.failure.ignore=true test",
          actualCwd: "/workspace/commons-cli",
          exitCode: 0,
          outcome: "completed",
          toolchain: { executable: "mvn", version: "Apache Maven 3.9.9" },
          jdkMajor: "8",
          jdkVersion: null,
          reportsNew: 47,
          reportsChanged: 0,
          testsReported: null,
        },
        {
          receiptId: "compiled-only",
          tool: "maven",
          argv: "mvn --fail-at-end compile",
          actualCwd: "/workspace/commons-cli",
          exitCode: 0,
          outcome: "completed",
          toolchain: { executable: "mvn", version: "Apache Maven 3.9.9" },
          jdkMajor: "8",
          jdkVersion: null,
          reportsNew: 0,
          reportsChanged: 0,
          testsReported: null,
        },
      ],
    } as any

    render(<TestFacet detail={withReceipts} />)
    const table = screen.getByRole("table", { name: "Commands that wrote test reports." })
    expect(within(table).getByText(/maven.test.failure.ignore/)).toBeInTheDocument()
    expect(within(table).queryByText("mvn --fail-at-end compile")).not.toBeInTheDocument()
  })

  it("says what it found none of when no command wrote a test report", () => {
    render(<TestFacet detail={igniteShape} />)
    expect(screen.getByText("No command in this run wrote a test report.")).toBeInTheDocument()
  })
})

describe("a run whose record could not be read", () => {
  // "Test counts were not recorded" is a finding about the run. When the
  // record could not be read, nothing was looked at — so the only true thing
  // to say is that.
  it("does not report the counts as unrecorded when nothing checked", () => {
    render(<TestFacet detail={{
      test: { state: "unknown", pass: 0, fail: 0, skip: 0, total: 0 },
      modules: [],
      snapshotStatus: "untrusted",
    } as any} />)
    expect(screen.queryByText(/Test counts were not recorded/i)).toBeNull()
    expect(screen.getByText(/result record could not be read here/i)).toBeInTheDocument()
  })

  it("still reports unrecorded counts when the record was read", () => {
    render(<TestFacet detail={{
      test: { state: "unknown", pass: 0, fail: 0, skip: 0, total: 0 },
      modules: [],
      snapshotStatus: "valid",
    } as any} />)
    expect(screen.getByText(/Test counts were not recorded/i)).toBeInTheDocument()
  })
})
