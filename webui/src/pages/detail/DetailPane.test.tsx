import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { ExecutionSessionDetail, WorkspaceSummary } from "@/api/types"

import { DetailPane } from "./DetailPane"

// xterm.js needs a real browser; stub the terminal body so jsdom doesn't crash
// (mirrors the repo-wide pattern in App.test.tsx). The dialog title under test
// comes from WorkspacePanel, not from this body, so the assertion is unaffected.
vi.mock("@/components/terminal/TerminalPanel", () => ({
  TerminalPanel: ({ workspaceId }: { workspaceId: string }) => (
    <div aria-label="Workspace terminal">Terminal for {workspaceId}</div>
  ),
}))

const workspace: WorkspaceSummary = {
  id: "sag-x",
  project: "owner/x",
  container: "sag-x",
  stack: "Java · Maven",
  docker: { status: "running", image: "sag/base" },
  task: "Build and test",
  build: { state: "success", tool: "Maven", time: "1s", note: "" },
  test: { state: "pass", pass: 10, fail: 0, skip: 0, total: 10 },
  report: "ready",
  changed: 0,
  updated: "just now",
}

const detail: ExecutionSessionDetail = {
  id: "CC-1",
  workspace: "sag-x",
  title: "Build and test",
  status: "completed",
  entry: "SAG",
  start: "now",
  duration: "1m",
  outcome: "All good.",
  resultCard: {
    schemaVersion: 1,
    runId: "CC-1",
    verdict: "success",
    verdictSource: "snapshot",
    rows: [
      { key: "setup", label: "Setup", status: "success", tone: "success", headline: "4/4 phases" },
      {
        key: "task",
        label: "Required task",
        status: "not supplied",
        tone: "neutral",
        headline: "no required task was supplied",
      },
      { key: "build", label: "Build", status: "success", tone: "success", headline: "1/1 modules built" },
      {
        key: "tests",
        label: "Tests",
        status: "executed",
        tone: "success",
        headline: "10 executed · 10 passed · 0 failed · 0 errors · 0 skipped",
      },
      { key: "coverage", label: "Coverage", status: "not collected", tone: "neutral", headline: "not collected" },
      { key: "ci", label: "Official CI", status: "not compared", tone: "neutral", headline: "not compared" },
      { key: "report", label: "Report", status: "delivered", tone: "neutral", headline: "setup-report.md" },
    ],
    stats: {},
    attention: [],
    notes: [],
  },
  build: { state: "success", tool: "Maven", time: "1s", note: "Compiled all modules" },
  test: { state: "pass", pass: 10, fail: 0, skip: 0, total: 10 },
  report: "ready",
  evidence: [],
  logs: [],
}

/** A run whose evidence groups are empty but whose commands are not — the
 *  shape three of the six real sessions checked against a running API take.
 *  The Evidence tab existed for them and said "Evidence is not available for
 *  this session" while the payload carried 78 recorded commands. */
const withReceipts: ExecutionSessionDetail = {
  ...detail,
  receipts: [
    {
      receiptId: "inv-maven-1-f006510e44c8-0001",
      tool: "maven",
      argv: "/opt/apache-maven-3.9.9/bin/mvn clean verify",
      workingDirectory: "/workspace/gson",
      actualCwd: "/workspace/gson",
      exitCode: 0,
      outcome: "completed",
      lifecycleState: "finished",
      toolchain: { executable: "/opt/apache-maven-3.9.9/bin/mvn", version: "Apache Maven 3.9.9" },
      jdkMajor: "17",
      jdkVersion: "17.0.20",
      reportsNew: 138,
      reportsChanged: 0,
      testsReported: 4866,
    },
  ],
  evidence: [
    {
      source: "Build evidence",
      status: "success",
      counts: "11 references",
      time: "10:27",
      summary: "Build outputs recorded by the run",
      records: [],
    },
  ],
}

/** Every run the API serves a comparison for; the tab is where it is read. */
const withCI: ExecutionSessionDetail = {
  ...detail,
  ciComparison: {
    schema_version: 1,
    status: "no_matched_cell",
    run_id: "CC-1",
    attainment: null,
    receipt_ids: [],
    commands: [],
    reasons: ["no cell in the workflow matched this run"],
  },
}

const handlers = {
  sessionId: "CC-1",
  onSession: () => {},
  onSubmitTask: vi.fn().mockResolvedValue({ session_id: "CC-2" }),
  onDelete: vi.fn().mockResolvedValue(undefined),
}

describe("DetailPane", () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it("renders the header, the result band, and the tab bar (Overview active by default)", () => {
    const { container } = render(<DetailPane workspace={workspace} detail={detail} {...handlers} />)
    expect(screen.getByRole("heading", { name: "owner/x" })).toBeInTheDocument()
    // The result band states each of the card's seven rows. Scoped to the band's
    // own row: the Overview below it restates the tests row word for word, so an
    // unscoped query would find two and say nothing about which surface it read.
    expect(screen.getByText("4/4 phases")).toBeInTheDocument()
    const testsRow = container.querySelector('[data-row="tests"]') as HTMLElement
    expect(testsRow).not.toBeNull()
    expect(
      within(testsRow).getByText("10 executed · 10 passed · 0 failed · 0 errors · 0 skipped"),
    ).toBeInTheDocument()
    expect(screen.getByText("Official CI")).toBeInTheDocument()
    // Tab bar.
    expect(screen.getByRole("navigation", { name: /detail tabs/i })).toBeInTheDocument()
    const overview = screen.getByRole("button", { name: /^Overview/ })
    expect(overview).toHaveAttribute("aria-current", "true")
    expect(screen.getByRole("button", { name: /^Tests/ })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /^Build/ })).toBeInTheDocument()
  })

  it("switches panels when a tab is clicked (real switch, not scroll)", () => {
    render(<DetailPane workspace={workspace} detail={detail} {...handlers} />)
    // Overview content is visible up front.
    expect(screen.getByText("Module details are not available for this run.")).toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: /^Build/ }))
    const build = screen.getByRole("button", { name: /^Build/ })
    expect(build).toHaveAttribute("aria-current", "true")
    // The Build facet now owns the panel; the Overview's own sections are gone.
    expect(screen.queryByText("Module details are not available for this run.")).not.toBeInTheDocument()
    expect(screen.getByText("Compiled all modules")).toBeInTheDocument()
  })

  it("honors initialFacet by opening that tab first", () => {
    render(<DetailPane workspace={workspace} detail={detail} initialFacet="tests" {...handlers} />)
    expect(screen.getByRole("button", { name: /^Tests/ })).toHaveAttribute("aria-current", "true")
    expect(screen.getByRole("button", { name: /^Overview/ })).toHaveAttribute("aria-current", "false")
  })

  it("opens the turns from the result band's Setup row", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          schema_version: 1,
          session: { run_id: "CC-1" },
          phases: [],
          turns: [],
          annotations: [],
          warnings: [],
        }),
        { headers: { "Content-Type": "application/json" }, status: 200 },
      ),
    )
    render(<DetailPane workspace={workspace} detail={detail} {...handlers} />)

    fireEvent.click(screen.getByRole("button", { name: "Open the evidence behind Setup" }))

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^Turns/ })).toHaveAttribute(
        "aria-current",
        "true",
      ),
    )
  })

  it("offers official CI as its own tab, named apart from the row that links to it", () => {
    render(<DetailPane workspace={workspace} detail={withCI} {...handlers} />)

    // The tab and the band row do the same job and must not read the same to a
    // screen reader: the tab is named for the subject, the row for the action.
    const tab = screen.getByRole("button", { name: "Official CI" })
    const row = screen.getByRole("button", { name: "Open the evidence behind Official CI" })
    expect(tab).not.toBe(row)

    fireEvent.click(row)
    expect(tab).toHaveAttribute("aria-current", "true")
    expect(screen.getByText("no cell in the workflow matched this run")).toBeInTheDocument()
  })

  it("opens the new-task modal from the header", () => {
    render(<DetailPane workspace={workspace} detail={detail} {...handlers} />)
    fireEvent.click(screen.getByRole("button", { name: "New task" }))
    expect(screen.getByRole("dialog", { name: /new task/i })).toBeInTheDocument()
  })

  it("opens the terminal panel from the header", () => {
    render(<DetailPane workspace={workspace} detail={detail} {...handlers} />)
    fireEvent.click(screen.getByRole("button", { name: /terminal/i }))
    expect(screen.getByRole("dialog", { name: /terminal/i })).toBeInTheDocument()
  })

  it("shows the commands above the evidence timeline on the Evidence tab", () => {
    // Nothing in this repo renders `TabBody`, so a change to the Evidence case
    // lands committed and unread. This mounts the pane and clicks the tab.
    const { container } = render(
      <DetailPane workspace={workspace} detail={withReceipts} {...handlers} />,
    )
    fireEvent.click(screen.getByRole("button", { name: /^Evidence/ }))

    const table = screen.getByRole("table", { name: "Every command this run dispatched." })
    expect(within(table).getByText("/opt/apache-maven-3.9.9/bin/mvn clean verify")).toBeInTheDocument()

    const body = container.querySelector("main")?.textContent ?? ""
    expect(body.indexOf("mvn clean verify")).toBeLessThan(body.indexOf("Build evidence"))
  })

  it("does not tell a run with recorded commands that it has no evidence", () => {
    render(
      <DetailPane
        workspace={workspace}
        detail={{ ...withReceipts, evidence: [] }}
        {...handlers}
      />,
    )
    fireEvent.click(screen.getByRole("button", { name: /^Evidence/ }))
    expect(
      screen.getByRole("table", { name: "Every command this run dispatched." }),
    ).toBeInTheDocument()
    expect(screen.queryByText(/Evidence is not available for this session/i)).not.toBeInTheDocument()
  })
})
