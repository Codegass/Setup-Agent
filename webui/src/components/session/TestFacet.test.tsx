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
  it("prefers local outcomes to a different CI-bound receipt total", () => {
    render(<TestFacet detail={{
      ...single,
      resultCard: {
        rows: [],
        dimensions: {
          officialCi: { agentTestsExecuted: 933, agentFailures: 0, agentErrors: 0 },
          sagChecks: [],
          tests: {
            fact: { key: "tests", status: "executed", tone: "attention", headline: "320 executed · 312 passed · 8 failed" },
            counts: { executed: 320, passed: 312, failed: 8, errors: 0, skipped: 0 },
            lowerBound: true, rawExecutions: 340, flakyCount: 0,
          },
        },
      },
    }} />)
    const results = screen.getByRole("region", { name: "Test results" })
    expect(within(results).getByText("≥ 320")).toHaveClass("text-foreground")
    expect(within(results).getByText("8")).toHaveClass("text-status-failed")
    expect(within(results).queryByText("933")).toBeNull()
  })

  it("keeps local test facts when no CI-bound counts were supplied", () => {
    render(<TestFacet detail={{
      ...single,
      resultCard: {
        rows: [{ key: "official_ci", status: "not compared", detail: "this run: 320 executed · 312 passed · 8 failed" }],
        dimensions: {
          officialCi: { agentTestsExecuted: null, agentFailures: null, agentErrors: null },
          sagChecks: [],
        },
      },
    }} />)
    expect(screen.getByText("this run: 320 executed · 312 passed · 8 failed")).toBeVisible()
    expect(screen.queryByText("not compared")).toBeNull()
  })

  it("does not present a CI comparison as a test-pass verdict", () => {
    const measured = {
      ...single,
      resultCard: {
        rows: [{ key: "official_ci", label: "Official CI", status: "compared", tone: "success", headline: "ran 933 of 933 tests", detail: "933 executed · 4 errors" }],
        dimensions: {
          officialCi: { agentTestsExecuted: 933, agentFailures: 0, agentErrors: 4 },
          sagChecks: [{ check: "test outcome graded", confirmed: false, reason: "test outcome evidence is incomplete" }],
        },
      },
    }
    render(<TestFacet detail={measured} />)
    const results = screen.getByRole("region", { name: "Test results" })
    expect(within(results).getByText("933")).toBeVisible()
    expect(within(results).getByText("4")).toHaveClass("text-status-failed")
    expect(within(results).getByText(/Test outcome not confirmed/)).toBeVisible()
    expect(screen.queryByText("compared")).toBeNull()
    expect(screen.queryByText(/97\.5% of non-skipped results passed/)).toBeNull()
  })

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

  it("states the lines the result card states, rather than working them out again", () => {
    // The card states this run's tests on its Official CI row, and on its "Not
    // confirmed by SAG" row when the outcome could not be graded; the tab
    // quotes both, in the card's words.
    const carded = {
      ...igniteShape,
      resultCard: {
        schemaVersion: 1,
        runId: "r",
        verdict: "failed",
        verdictSource: "snapshot",
        rows: [
          {
            key: "official_ci",
            label: "Official CI",
            status: "compared",
            tone: "attention",
            headline:
              "built 7 of 9 modules, ran 2,679 of 2,762 tests (CI cell 'Apache Jenkins httpcomponents-client Linux JDK 17 #225')",
            detail: "0 failures, 4 errors in this run · 2,692 raw executions",
          },
          {
            key: "unconfirmed",
            label: "Not confirmed by SAG",
            status: "2 of 4",
            tone: "attention",
            headline: "SAG could not confirm these from its own records",
            items: [
              "which modules the build produced: no successful build of the whole project listed its modules, and the project's own module list could not be read in full",
              "whether the tests passed: test execution completion lacks bound task, assessment or outcome evidence",
            ],
          },
        ],
        stats: {},
        attention: [],
        notes: [],
      },
    } as any

    render(<TestFacet detail={carded} />)
    expect(screen.getByText("compared")).toBeInTheDocument()
    expect(screen.getByText("Official CI")).toBeInTheDocument()
    expect(
      screen.getByText(
        "built 7 of 9 modules, ran 2,679 of 2,762 tests (CI cell 'Apache Jenkins httpcomponents-client Linux JDK 17 #225')",
      ),
    ).toBeInTheDocument()
    expect(
      screen.getByText("0 failures, 4 errors in this run · 2,692 raw executions"),
    ).toBeInTheDocument()
    expect(
      screen.getByText(
        "Not confirmed by SAG: whether the tests passed: test execution completion lacks bound task, assessment or outcome evidence",
      ),
    ).toBeInTheDocument()
    // Only the tests line of that row belongs on this tab.
    expect(screen.queryByText(/which modules the build produced/)).not.toBeInTheDocument()
    // The card did not say `executed`; the tab may not say `Executed` beside it.
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
