import { cleanup, fireEvent, render, screen } from "@testing-library/react"
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

const handlers = {
  sessionId: "CC-1",
  onSession: () => {},
  onSubmitTask: vi.fn().mockResolvedValue({ session_id: "CC-2" }),
  onDelete: vi.fn().mockResolvedValue(undefined),
}

describe("DetailPane", () => {
  afterEach(() => {
    cleanup()
  })

  it("renders the header, the result band, and the tab bar (Overview active by default)", () => {
    render(<DetailPane workspace={workspace} detail={detail} {...handlers} />)
    expect(screen.getByRole("heading", { name: "owner/x" })).toBeInTheDocument()
    // The result band states each of the card's seven rows.
    expect(screen.getByText("4/4 phases")).toBeInTheDocument()
    expect(screen.getByText("10 executed · 10 passed · 0 failed · 0 errors · 0 skipped")).toBeInTheDocument()
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
    // Overview content is visible up front; unsealed build duration is omitted.
    expect(screen.getByText(/did not record its test outcome totals/i)).toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: /^Build/ }))
    const build = screen.getByRole("button", { name: /^Build/ })
    expect(build).toHaveAttribute("aria-current", "true")
    // The Build facet now owns the panel; the overview result cards are gone.
    expect(screen.queryByText(/did not record its test outcome totals/i)).not.toBeInTheDocument()
    expect(screen.getByText("Compiled all modules")).toBeInTheDocument()
  })

  it("honors initialFacet by opening that tab first", () => {
    render(<DetailPane workspace={workspace} detail={detail} initialFacet="tests" {...handlers} />)
    expect(screen.getByRole("button", { name: /^Tests/ })).toHaveAttribute("aria-current", "true")
    expect(screen.getByRole("button", { name: /^Overview/ })).toHaveAttribute("aria-current", "false")
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
})
