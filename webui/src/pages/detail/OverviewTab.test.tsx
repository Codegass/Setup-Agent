import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import type { AttentionItem, ExecutionSessionDetail, ResultCard, ResultRow } from "@/api/types"

import { OverviewTab } from "./OverviewTab"

afterEach(() => cleanup())

/** The seven rows in the order the card always carries them. Only the three the
 *  Overview reads carry realistic strings; the rest are here so `rows` is the
 *  shape the API actually serves. */
function rows(overrides: Partial<Record<ResultRow["key"], Partial<ResultRow>>> = {}): ResultRow[] {
  const base: ResultRow[] = [
    { key: "setup", label: "Setup", status: "success", tone: "success", headline: "5/5 phases · 24 turns" },
    { key: "task", label: "Required task", status: "not supplied", tone: "neutral", headline: "no required task was supplied" },
    {
      key: "build",
      label: "Build",
      status: "success",
      tone: "success",
      headline: "1/1 modules built",
      detail: "1,284 class files · counts are diagnostic, CI defines scope",
    },
    {
      key: "tests",
      label: "Tests",
      status: "executed",
      tone: "success",
      headline: "994 executed · 933 passed · 0 failed · 0 errors · 61 skipped",
      detail: "100% of non-skipped passed",
    },
    { key: "coverage", label: "Coverage", status: "not collected", tone: "neutral", headline: "not collected" },
    { key: "ci", label: "Official CI", status: "not compared", tone: "neutral", headline: "not compared" },
    { key: "report", label: "Report", status: "delivered", tone: "neutral", headline: "written inside the container" },
  ]
  return base.map((row) => ({ ...row, ...(overrides[row.key] ?? {}) }))
}

function card(overrides: Partial<ResultCard> = {}): ResultCard {
  return {
    schemaVersion: 1,
    runId: "SETUP-commons-cli-20260913-154428",
    project: "commons-cli",
    goal: "Set up commons-cli and run its test suite",
    commit: "1a2b3c4",
    container: "sag-commons-cli",
    sessionDir: null,
    verdict: "success",
    verdictSource: "snapshot",
    rows: rows(),
    stats: {},
    attention: [],
    notes: [],
    ...overrides,
  }
}

function detail(overrides: Partial<ExecutionSessionDetail> = {}): ExecutionSessionDetail {
  return {
    id: "S1",
    workspace: "sag-commons-cli",
    title: "t",
    status: "success",
    entry: "SAG",
    start: "now",
    duration: "8m 01s",
    outcome: "✅ SUCCESS",
    resultCard: card(),
    build: { state: "success", tool: "maven", time: "2m 41s", note: "mvn -B verify" },
    test: { state: "success", pass: 933, fail: 0, skip: 61, total: 994 },
    modules: [],
    report: "ready",
    evidence: [],
    logs: [],
    ...overrides,
  }
}

function cleanDetail(): ExecutionSessionDetail {
  return detail()
}

const FAILING: AttentionItem = {
  kind: "failing_tests",
  title: "commons-cli · 2 failing",
  detail: "cli.LoginTest.shouldA, cli.LoginTest.shouldB",
  refs: ["ev-7742", "ev-7743"],
}

function detailWithAttention(): ExecutionSessionDetail {
  return detail({
    resultCard: card({
      verdict: "partial",
      attention: [
        FAILING,
        { kind: "report", title: "The setup report was not written", detail: "the report tool never ran" },
      ],
    }),
  })
}

function detailWithNotes(): ExecutionSessionDetail {
  return detail({
    resultCard: card({
      notes: [
        "Some test executions could not be linked to their recorded tool executions and were set aside.",
      ],
    }),
  })
}

describe("OverviewTab", () => {
  it("leads with what needs attention", () => {
    render(<OverviewTab detail={detailWithAttention()} />)
    const headings = screen.getAllByRole("heading").map((node) => node.textContent)
    expect(headings[0]).toMatch(/Needs attention/)
    expect(screen.getByText("commons-cli · 2 failing")).toBeInTheDocument()
    expect(screen.getByText("cli.LoginTest.shouldA, cli.LoginTest.shouldB")).toBeInTheDocument()
    expect(screen.getByText("ev-7742")).toBeInTheDocument()
  })

  it("shows no attention box at all when nothing needs attention", () => {
    // A bordered box whose only content was "Nothing needs attention." took a
    // band across the top of the tab on every clean run and said nothing a
    // reader could act on. A section with no items is not drawn.
    render(<OverviewTab detail={cleanDetail()} />)
    expect(screen.queryByText("Nothing needs attention.")).toBeNull()
    expect(screen.queryByRole("heading", { name: "Needs attention" })).toBeNull()
  })

  it("does not restate the result band's Build and Tests rows", () => {
    const d = cleanDetail()
    render(<OverviewTab detail={d} />)
    // The band renders these rows above the tab bar, on screen the whole time.
    // Saying them again here printed the same sentence twice on one screen.
    const buildRow = d.resultCard!.rows.find((row) => row.key === "build")!
    const testsRow = d.resultCard!.rows.find((row) => row.key === "tests")!
    expect(screen.queryByText(buildRow.headline)).toBeNull()
    expect(screen.queryByText(buildRow.detail!)).toBeNull()
    expect(screen.queryByText(testsRow.headline)).toBeNull()
    expect(screen.queryByText(testsRow.detail!)).toBeNull()
  })

  it("shows the run's notes in a disclosure", () => {
    const { container } = render(<OverviewTab detail={detailWithNotes()} />)
    const disclosure = container.querySelector("details")
    expect(disclosure).not.toBeNull()
    expect(disclosure!.open).toBe(false)
    fireEvent.click(screen.getByText("Data notes"))
    expect(disclosure!.open).toBe(true)
    expect(screen.getByText(/were set aside/)).toBeInTheDocument()
  })

  it("omits the notes disclosure when the run recorded none", () => {
    const { container } = render(<OverviewTab detail={cleanDetail()} />)
    expect(container.querySelector("details")).toBeNull()
  })

  it("leaves the no-result sentence to the band and still says what it can", () => {
    // The band prints "No result was recorded for this run yet." above the tab
    // bar. Seen live on an archived run with no card, the Overview printed the
    // identical sentence two inches below it.
    render(<OverviewTab detail={{ ...cleanDetail(), resultCard: null }} />)
    expect(screen.queryByText(/No result was recorded/)).toBeNull()
    expect(screen.getByText("Module details are not available for this run.")).toBeInTheDocument()
  })

  it("keeps the run's goal on the page", () => {
    render(<OverviewTab detail={cleanDetail()} />)
    expect(screen.getByText("Set up commons-cli and run its test suite")).toBeInTheDocument()
  })

  it("renders the per-module breakdown when the run measured modules", () => {
    render(
      <OverviewTab
        detail={detail({
          modules: [
            {
              name: "commons-cli",
              path: ".",
              buildStatus: "success",
              buildSource: "reactor",
              testSource: "runner_xml",
              testsTotal: 994,
              testsPassed: 933,
              testsFailed: 0,
              failingNames: [],
              failingCount: 0,
              lineRate: 86.4,
              branchRate: 74.1,
            },
          ],
        })}
      />,
    )
    const table = screen.getByRole("region", { name: "Per-module breakdown" })
    expect(within(table).getByText("Line cov")).toBeInTheDocument()
    expect(within(table).getByText("Branch cov")).toBeInTheDocument()
  })

  it("says module details are absent rather than showing an empty table", () => {
    render(<OverviewTab detail={cleanDetail()} />)
    expect(screen.getByText("Module details are not available for this run.")).toBeInTheDocument()
  })
})
