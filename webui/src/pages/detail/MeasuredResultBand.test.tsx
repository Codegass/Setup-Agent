import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { ResultCard, ResultDimensions, ResultRow } from "@/api/types"
import { ResultBand } from "./ResultBand"

function dimensions(): ResultDimensions {
  return {
    ciCommand: {
      result: "passed",
      stepsPassed: 1,
      stepsTotal: 1,
      firstUnpassedStep: null,
      toolchainAsPinned: "yes",
      pinnedJava: [17],
      pinnedMaven: ["3.9.16"],
      toolchainReason: null,
      sourceUnchanged: "yes",
      sourceDetail: null,
    },
    officialCi: {
      basis: "ci-target cell summary",
      reason: null,
      cell: "Build (17)",
      ciTestsExecuted: 933,
      ciTestsRed: 0,
      ciModules: ["core", "docs"],
      agentTestsExecuted: 933,
      agentModulesBuilt: ["core", "docs"],
      agentFailures: 0,
      agentErrors: 0,
    },
    sagChecks: [
      { check: "java requirement decided", confirmed: true, reason: null },
      { check: "built modules confirmed", confirmed: true, reason: null },
      { check: "test outcome graded", confirmed: true, reason: null },
      {
        check: "test reports tied to the command",
        confirmed: true,
        reason: null,
      },
    ],
    runControl: { ended: "normally", commandsStillRunning: 0, detail: null },
  }
}

function card(d = dimensions()): ResultCard {
  const rows: ResultRow[] = [
    {
      key: "ci_command",
      label: "CI command",
      status: "passed",
      tone: "success",
      headline: "1 of 1 steps passed",
      items: ["build: complete — mvn verify → exit 0"],
    },
    {
      key: "official_ci",
      label: "Official CI",
      status: "compared",
      tone: "success",
      headline: "counts",
      detail: "119 class files · 1 jar",
    },
    {
      key: "unconfirmed",
      label: "Not confirmed by SAG",
      status: "none",
      tone: "success",
      headline: "confirmed",
    },
    {
      key: "run",
      label: "Run",
      status: "ended normally",
      tone: "success",
      headline: "ended normally",
    },
    {
      key: "summary",
      label: "Summary",
      status: "success",
      tone: "success",
      headline: "derived",
    },
    {
      key: "coverage",
      label: "Coverage",
      status: "not collected",
      tone: "neutral",
      headline: "not collected",
    },
    {
      key: "report",
      label: "Report",
      status: "delivered",
      tone: "neutral",
      headline: "setup-report.md",
    },
  ]
  return {
    schemaVersion: 1,
    runId: "r",
    verdict: "success",
    verdictSource: "snapshot",
    rows,
    stats: {},
    attention: [],
    notes: [],
    dimensions: d,
  }
}

afterEach(cleanup)

describe("measured result card", () => {
  it("keeps a passed CI command and an unconfirmed SAG check visibly separate", () => {
    const d = dimensions()
    d.sagChecks[0] = {
      check: "java requirement decided",
      confirmed: false,
      reason: "The project's Java requirement was not resolved.",
    }
    const { container } = render(
      <ResultBand card={card(d)} onOpenTab={vi.fn()} />,
    )
    expect(screen.getByText("Passed")).toBeVisible()
    expect(screen.getByText("success")).toBeVisible()
    expect(screen.getByText("1 not confirmed")).toBeVisible()
    expect(
      screen.getByText("The project's Java requirement was not resolved."),
    ).toBeVisible()
    expect(screen.getByRole("region", { name: "Run result" })).toHaveAttribute(
      "data-tone",
      "neutral",
    )
    expect(
      [...container.querySelectorAll("[data-row]")].map((el) =>
        el.getAttribute("data-row"),
      ),
    ).toEqual([
      "ci_command",
      "official_ci",
      "unconfirmed",
      "run",
      "summary",
      "coverage",
      "report",
    ])
  })

  it("compares the intersection of module names, not all modules built", () => {
    const d = dimensions()
    d.officialCi.agentModulesBuilt = ["core", "extra", "extra"]
    render(<ResultBand card={card(d)} onOpenTab={vi.fn()} />)
    const modules = screen.getByText("Modules").closest("summary")!
    expect(modules).toHaveTextContent("1 / 2")
    fireEvent.click(modules)
    const disclosure = modules.closest("details")!
    expect(
      within(disclosure).getByText("docs").parentElement,
    ).toHaveTextContent("not confirmed built")
  })

  it("distinguishes absent counts from recorded zero, without dividing by zero", () => {
    const d = dimensions()
    d.officialCi.agentTestsExecuted = null
    d.officialCi.ciTestsExecuted = 0
    d.officialCi.agentModulesBuilt = null
    const { container, rerender } = render(
      <ResultBand card={card(d)} onOpenTab={vi.fn()} />,
    )
    expect(screen.getByText("Tests run").closest("summary")).toHaveTextContent(
      "— / 0",
    )
    expect(screen.getByText("Modules").closest("summary")).toHaveTextContent(
      "— / 2",
    )
    expect(container.innerHTML).not.toMatch(/NaN|Infinity/)
    d.officialCi.agentTestsExecuted = 933
    d.officialCi.ciTestsExecuted = null
    rerender(<ResultBand card={card(d)} onOpenTab={vi.fn()} />)
    const tests = screen.getByText("Tests run").closest("summary")!
    expect(tests).toHaveTextContent("933 / —")
    // Removing the CI count must not turn the bar into apparent completion.
    expect(tests.querySelector("[style]")).toBeNull()
  })

  it("shows the actual count above CI without claiming test identity parity", () => {
    const d = dimensions()
    d.officialCi.agentTestsExecuted = 1200
    render(<ResultBand card={card(d)} onOpenTab={vi.fn()} />)
    const tests = screen.getByText("Tests run").closest("summary")!
    expect(tests).toHaveTextContent("1,200 / 933")
    fireEvent.click(tests)
    expect(
      screen.getByText("Counts do not establish matching test identities."),
    ).toBeVisible()
    expect(screen.queryByText(/100%/)).toBeNull()
  })

  it("keeps command receipts, class facts, and the report accessible without dead links", () => {
    const open = vi.fn()
    render(
      <ResultBand
        card={card()}
        onOpenTab={open}
        availableTabs={["build", "tests", "report"]}
      />,
    )
    fireEvent.click(screen.getByText("Task verification"))
    expect(
      screen.getByText("build: complete — mvn verify → exit 0"),
    ).toBeVisible()
    expect(
      screen.queryByRole("button", { name: "Command receipts" }),
    ).toBeNull()
    fireEvent.click(screen.getByText("Build (17)"))
    expect(screen.getByText("119 class files · 1 jar")).toBeVisible()
    fireEvent.click(screen.getByText("Tests run"))
    fireEvent.click(screen.getByRole("button", { name: "Test results" }))
    expect(open).toHaveBeenCalledWith("tests")
  })

  it("does not ask a task-less run for a prescribed command", () => {
    const d = dimensions()
    d.ciCommand = null
    const c = card(d)
    c.rows[0].tone = "neutral"
    render(<ResultBand card={c} onOpenTab={vi.fn()} />)
    expect(screen.queryByText("No bound task")).toBeNull()
    expect(screen.queryByText("Task verification")).toBeNull()
    expect(screen.queryByRole("progressbar")).toBeNull()
  })

  it("leads with local build and test facts without requiring a task or CI report", () => {
    const d = dimensions()
    d.ciCommand = null
    d.hasCiReference = false
    d.officialCi = {
      ...d.officialCi,
      basis: "unavailable",
      cell: null,
      agentTestsExecuted: null,
      agentModulesBuilt: null,
    }
    d.build = {
      fact: {
        key: "build",
        label: "Build",
        status: "success",
        tone: "success",
        headline: "1/1 modules built",
      },
      modulesBuilt: 1,
      modulesTotal: 1,
      classFiles: 119,
      jars: 4,
    }
    d.tests = {
      fact: {
        key: "tests",
        label: "Tests",
        status: "executed",
        tone: "attention",
        headline: "933 passed · 2 failed · 1 error · 61 skipped",
      },
      counts: { executed: 997, passed: 933, failed: 2, errors: 1, skipped: 61 },
      rawExecutions: 1001,
      lowerBound: false,
      flakyCount: 1,
    }
    const open = vi.fn()
    render(<ResultBand card={card(d)} onOpenTab={open} />)
    expect(
      screen.getByRole("region", { name: "Build result" }),
    ).toHaveTextContent("119class files4JARs")
    const tests = screen.getByRole("region", { name: "Tests result" })
    expect(tests).toHaveTextContent("997recorded")
    expect(within(tests).getByRole("img")).toHaveAttribute(
      "aria-label",
      "933 passed, 2 failed, 1 errors, 61 skipped",
    )
    expect(within(tests).getByText("2").parentElement).toHaveClass(
      "text-status-failed",
    )
    expect(screen.queryByText("Task verification")).toBeNull()
    expect(screen.queryByText("CI comparison")).toBeNull()
    expect(screen.queryByText(/No bound task|CI.*not available/i)).toBeNull()
    fireEvent.click(within(tests).getByText("Details"))
    fireEvent.click(screen.getByRole("button", { name: "Open tests details" }))
    expect(open).toHaveBeenCalledWith("tests")
  })

  it("keeps a supplied task independent of an official CI reference", () => {
    const d = dimensions()
    d.hasCiReference = false
    render(<ResultBand card={card(d)} onOpenTab={vi.fn()} />)
    expect(screen.getByText("Task verification")).toBeVisible()
    expect(screen.queryByText("CI command")).toBeNull()
    expect(screen.queryByText("CI comparison")).toBeNull()
  })

  it("discloses an unusable supplied CI reference rather than hiding it", () => {
    const d = dimensions()
    d.hasCiReference = true
    d.officialCi.basis = "unavailable"
    d.officialCi.reason = "None of the supplied jobs matches this run."
    render(<ResultBand card={card(d)} onOpenTab={vi.fn()} />)
    expect(screen.getByText("CI comparison")).toBeVisible()
    expect(screen.getByText(d.officialCi.reason)).toBeVisible()
  })

  it("keeps the failure and stop reason visible even while details are closed", () => {
    const d = dimensions()
    d.ciCommand = {
      ...d.ciCommand!,
      result: "failed",
      stepsPassed: 1,
      stepsTotal: 3,
      firstUnpassedStep: { id: "test", status: "failed", exitCode: 1 },
    }
    d.officialCi.agentErrors = 4
    d.runControl = {
      ended: "time_limit",
      commandsStillRunning: 1,
      detail: null,
    }
    const c = card(d)
    c.verdict = "failed"
    c.rows[0].tone = c.rows[4].tone = "failed"
    c.rows[3].headline = "time limit · 1 command still running"
    c.rows[3].tone = "attention"
    render(<ResultBand card={c} onOpenTab={vi.fn()} />)
    expect(screen.getByText("test · failed · exit 1")).toBeVisible()
    expect(screen.getByText("0 failures · 4 errors")).toBeVisible()
    expect(
      screen.getByText("time limit · 1 command still running", {
        selector: "span",
      }),
    ).toBeVisible()
    expect(screen.getByRole("progressbar")).toHaveAttribute(
      "aria-valuenow",
      "1",
    )
    expect(screen.getByRole("progressbar")).toHaveAttribute(
      "aria-valuemax",
      "3",
    )
  })
})
