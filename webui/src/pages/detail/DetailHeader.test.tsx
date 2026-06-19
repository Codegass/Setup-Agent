import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { ExecutionSessionDetail, WorkspaceSummary } from "@/api/types"

import { DetailHeader } from "./DetailHeader"

const workspace: WorkspaceSummary = {
  id: "sag-acme",
  project: "acme-platform",
  container: "sag-acme",
  stack: "maven",
  commit: "9f8e7d6",
  docker: { status: "running", image: "sag/base" },
  task: "Build and test",
  build: { state: "success", tool: "Maven", time: "1s", note: "" },
  test: { state: "pass", pass: 10, fail: 0, skip: 0, total: 10 },
  report: "ready",
  changed: 0,
  updated: "2m ago",
  sessions: [
    { id: "CC-1", workspace: "sag-acme", title: "first", status: "completed", entry: "SAG", start: "", duration: "", build: "success", test: { state: "pass", pass: 1, fail: 0, skip: 0, total: 1 }, report: "ready", files: 0, evidence: 0 },
    { id: "CC-2", workspace: "sag-acme", title: "second", status: "running", entry: "SAG", start: "", duration: "", build: "pending", test: { state: "none", pass: 0, fail: 0, skip: 0, total: 0 }, report: "none", files: 0, evidence: 0 },
  ],
}

const noopHandlers = { onSession: () => {}, onNewTask: () => {}, onTerminal: () => {}, onSettings: () => {}, onDelete: () => {} }

function makeDetail(overrides: Partial<ExecutionSessionDetail> = {}): ExecutionSessionDetail {
  return {
    id: "CC-1",
    workspace: "sag-acme",
    title: "first",
    status: "running",
    entry: "setup",
    start: "now",
    duration: "8m 01s",
    outcome: "Working.",
    build: { state: "success", tool: "Maven", time: "1s", note: "" },
    test: { state: "pass", pass: 1, fail: 0, skip: 0, total: 1 },
    report: "ready",
    evidence: [],
    logs: [],
    ...overrides,
  }
}

describe("DetailHeader", () => {
  afterEach(() => {
    cleanup()
  })

  it("renders the project heading and the entry tag", () => {
    render(<DetailHeader workspace={workspace} detail={makeDetail()} sessionId="CC-1" {...noopHandlers} />)
    expect(screen.getByRole("heading", { name: "acme-platform" })).toBeInTheDocument()
    expect(screen.getByText("setup")).toBeInTheDocument()
  })

  it("shows model and steps in the metadata line", () => {
    render(
      <DetailHeader
        workspace={{ id: "sag-acme", project: "acme-platform", stack: "maven", commit: "9f8e7d6" } as WorkspaceSummary}
        detail={{ model: "claude-sonnet-4.5", steps: 6, stepBudget: 40, duration: "8m 01s" } as ExecutionSessionDetail}
        sessionId="S1"
        {...noopHandlers}
      />,
    )
    expect(screen.getByText(/claude-sonnet-4\.5/)).toBeInTheDocument()
    expect(screen.getByText(/6\s*\/\s*40 steps/)).toBeInTheDocument()
  })

  it("falls back to a bare step count when no budget is present", () => {
    render(
      <DetailHeader
        workspace={{ id: "sag-acme", project: "acme-platform" } as WorkspaceSummary}
        detail={{ steps: 6 } as ExecutionSessionDetail}
        sessionId="S1"
        {...noopHandlers}
      />,
    )
    expect(screen.getByText(/\b6 steps\b/)).toBeInTheDocument()
    expect(screen.queryByText(/\/\s*\d+\s*steps/)).not.toBeInTheDocument()
  })

  it("renders the primary New task action and labeled secondary actions", () => {
    const onNewTask = vi.fn()
    render(<DetailHeader workspace={workspace} detail={makeDetail()} sessionId="CC-1" {...noopHandlers} onNewTask={onNewTask} />)
    fireEvent.click(screen.getByRole("button", { name: /new task/i }))
    expect(onNewTask).toHaveBeenCalledTimes(1)
    expect(screen.getByRole("button", { name: /terminal/i })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /settings/i })).toBeInTheDocument()
  })

  it("opens the overflow menu and exposes Delete + the session switcher", () => {
    const onDelete = vi.fn()
    const onSession = vi.fn()
    render(<DetailHeader workspace={workspace} detail={makeDetail()} sessionId="CC-1" {...noopHandlers} onDelete={onDelete} onSession={onSession} />)

    // Menu is closed initially.
    expect(screen.queryByRole("menuitem", { name: /delete/i })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: /more/i }))
    fireEvent.click(screen.getByRole("menuitem", { name: /delete/i }))
    expect(onDelete).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole("button", { name: /more/i }))
    fireEvent.click(screen.getByRole("menuitemradio", { name: /CC-2/ }))
    expect(onSession).toHaveBeenCalledWith("CC-2")
  })

  it("hides the session switcher in the menu when there is a single session", () => {
    render(
      <DetailHeader
        workspace={{ ...workspace, sessions: [workspace.sessions![0]] }}
        detail={makeDetail()}
        sessionId="CC-1"
        {...noopHandlers}
      />,
    )
    fireEvent.click(screen.getByRole("button", { name: /more/i }))
    expect(screen.queryByRole("menuitemradio", { name: /CC-2/ })).not.toBeInTheDocument()
  })
})
