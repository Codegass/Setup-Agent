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
  verdict: { tone: "success", headline: "Build passed. 10 tests passing", detail: null },
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

  it("renders the header, the verdict band, and the tab bar (Overview active by default)", () => {
    render(<DetailPane workspace={workspace} detail={detail} {...handlers} />)
    expect(screen.getByRole("heading", { name: "owner/x" })).toBeInTheDocument()
    // VerdictBand renders the server-composed headline.
    expect(screen.getByText(/Build passed\. 10 tests passing/)).toBeInTheDocument()
    // Tab bar.
    expect(screen.getByRole("navigation", { name: /detail tabs/i })).toBeInTheDocument()
    const overview = screen.getByRole("button", { name: /^Overview/ })
    expect(overview).toHaveAttribute("aria-current", "true")
    expect(screen.getByRole("button", { name: /^Tests/ })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /^Build/ })).toBeInTheDocument()
  })

  it("switches panels when a tab is clicked (real switch, not scroll)", () => {
    render(<DetailPane workspace={workspace} detail={detail} {...handlers} />)
    // Overview content (the build-time KPI tile) is visible up front.
    expect(screen.getByText("Build time")).toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: /^Build/ }))
    const build = screen.getByRole("button", { name: /^Build/ })
    expect(build).toHaveAttribute("aria-current", "true")
    // The Build facet now owns the panel; the overview KPI tile is gone.
    expect(screen.queryByText("Build time")).not.toBeInTheDocument()
    expect(screen.getByText("Compiled all modules")).toBeInTheDocument()
  })

  it("honors initialFacet by opening that tab first", () => {
    const withContext: ExecutionSessionDetail = {
      ...detail,
      context: {
        trunk: { state: "completed", goal: "Set up the project", summary: "", progress: { done: 1, total: 1 } },
        phases: [],
        debug: {},
      },
    }
    render(
      <DetailPane workspace={workspace} detail={withContext} initialFacet="flow" {...handlers} />,
    )
    expect(screen.getByRole("button", { name: /^Flow/ })).toHaveAttribute("aria-current", "true")
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
