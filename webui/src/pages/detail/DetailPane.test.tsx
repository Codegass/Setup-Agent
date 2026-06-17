import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest"

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

beforeAll(() => {
  // jsdom has no layout/scroll; stub so jump() doesn't throw.
  Element.prototype.scrollIntoView = vi.fn()
})

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
  build: { state: "success", tool: "Maven", time: "1s", note: "" },
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

  it("renders the header, summary band, section nav, and all facet sections", () => {
    render(<DetailPane workspace={workspace} detail={detail} {...handlers} />)
    expect(screen.getByRole("heading", { name: "owner/x" })).toBeInTheDocument()
    expect(screen.getByText("All good.")).toBeInTheDocument()
    expect(screen.getByRole("navigation", { name: /detail sections/i })).toBeInTheDocument()
    // Top pills (one per facet) are buttons in the nav.
    expect(screen.getByRole("button", { name: /^Build/ })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /^Logs/ })).toBeInTheDocument()
    for (const id of ["build", "test", "flow", "evidence", "files", "report", "logs"]) {
      expect(document.getElementById(`facet-${id}`)).toBeTruthy()
    }
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
