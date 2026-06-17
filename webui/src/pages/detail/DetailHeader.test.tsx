import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { WorkspaceSummary } from "@/api/types"

import { DetailHeader } from "./DetailHeader"

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
  release: "1.4.0",
  sessions: [
    { id: "CC-1", workspace: "sag-x", title: "first", status: "completed", entry: "SAG", start: "", duration: "", build: "success", test: { state: "pass", pass: 1, fail: 0, skip: 0, total: 1 }, report: "ready", files: 0, evidence: 0 },
    { id: "CC-2", workspace: "sag-x", title: "second", status: "running", entry: "SAG", start: "", duration: "", build: "pending", test: { state: "none", pass: 0, fail: 0, skip: 0, total: 0 }, report: "none", files: 0, evidence: 0 },
  ],
}

const actions = { onNewTask: () => {}, onTerminal: () => {}, onSettings: () => {}, onDelete: () => {} }

describe("DetailHeader", () => {
  afterEach(() => {
    cleanup()
  })

  it("renders project, container, status, and the session chips", () => {
    render(<DetailHeader workspace={workspace} sessionId="CC-1" onSession={() => {}} {...actions} />)
    expect(screen.getByRole("heading", { name: "owner/x" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /CC-1/ })).toHaveAttribute("aria-current", "true")
    expect(screen.getByRole("button", { name: /CC-2/ })).toBeInTheDocument()
  })

  it("switches session when a chip is clicked", () => {
    const onSession = vi.fn()
    render(<DetailHeader workspace={workspace} sessionId="CC-1" onSession={onSession} {...actions} />)
    fireEvent.click(screen.getByRole("button", { name: /CC-2/ }))
    expect(onSession).toHaveBeenCalledWith("CC-2")
  })

  it("hides the session switcher when there is a single session", () => {
    render(
      <DetailHeader
        workspace={{ ...workspace, sessions: [workspace.sessions![0]] }}
        sessionId="CC-1"
        onSession={() => {}}
        {...actions}
      />,
    )
    expect(screen.queryByText("Sessions")).not.toBeInTheDocument()
  })
})
