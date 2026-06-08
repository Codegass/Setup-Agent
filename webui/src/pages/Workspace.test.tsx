import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { ExecutionSessionDetail, WorkspaceSummary } from "@/api/types"

import { Workspace, type WorkspaceSessionRow } from "./Workspace"

vi.mock("@/components/terminal/TerminalPanel", () => ({
  TerminalPanel: ({ workspaceId }: { workspaceId: string }) => (
    <div aria-label="Workspace terminal">Terminal for {workspaceId}</div>
  ),
}))

const workspace: WorkspaceSummary = {
  id: "sag-commons-cli",
  project: "apache/commons-cli",
  container: "sag-commons-cli",
  stack: "Java · Maven",
  docker: { status: "running" },
  task: "Build and test commons-cli",
  build: { state: "success", tool: "Maven", time: "47.2s", note: "" },
  test: { state: "partial", pass: 312, fail: 8, skip: 0, total: 320 },
  evidenceStatus: "partial",
  report: "ready",
  changed: 7,
  activeSession: "CC-3",
  latestSession: "CC-3",
  updated: "just now",
}

const latest: ExecutionSessionDetail = {
  id: "CC-3",
  workspace: "sag-commons-cli",
  title: "Build project and execute full test suite",
  status: "running",
  evidenceStatus: "conflict",
  entry: "CLI",
  start: "02:14:08",
  duration: "running · 2m 11s",
  outcome: "Build succeeds but evidence has a validator conflict.",
  build: { state: "success", tool: "Maven", time: "47.2s", note: "clean package" },
  test: { state: "partial", pass: 312, fail: 8, skip: 0, total: 320 },
  report: "ready",
  reportDoc: null,
  evidence: [],
  files: null,
  context: null,
  logs: [],
}

const sessions: WorkspaceSessionRow[] = [
  {
    id: "CC-3",
    title: "Build project and execute full test suite",
    status: "completed",
    evidenceStatus: "partial",
    entry: "CLI",
    start: "02:14:08",
    duration: "47s",
    build: { state: "success", tool: "Maven", time: "47.2s", note: "" },
    test: { state: "partial", pass: 312, fail: 8, skip: 0, total: 320 },
    evidenceCount: 3,
    filesCount: 7,
  },
]

describe("Workspace", () => {
  afterEach(() => {
    cleanup()
  })

  it("shows latest flow and evidence statuses separately in the overview", () => {
    render(
      <Workspace
        latest={latest}
        onBack={() => {}}
        onOpenSession={() => {}}
        onSubmitTask={vi.fn()}
        sessions={sessions}
        workspace={workspace}
      />,
    )

    expect(screen.getByText("Flow")).toBeInTheDocument()
    expect(screen.getByText("Evidence status")).toBeInTheDocument()
    expect(screen.getAllByText("Running")).not.toHaveLength(0)
    expect(screen.getByText("Conflict")).toBeInTheDocument()
  })

  it("shows session row evidence status distinct from flow status", () => {
    render(
      <Workspace
        latest={latest}
        onBack={() => {}}
        onOpenSession={() => {}}
        onSubmitTask={vi.fn()}
        sessions={sessions}
        workspace={workspace}
      />,
    )

    fireEvent.click(screen.getByRole("button", { name: "Sessions 1" }))

    expect(screen.getByText("Flow / evidence")).toBeInTheDocument()
    expect(screen.getByText("Completed")).toBeInTheDocument()
    expect(screen.getByText("Partial")).toBeInTheDocument()
  })

  it("suppresses default unknown evidence status noise", () => {
    render(
      <Workspace
        latest={{ ...latest, evidenceStatus: "unknown" }}
        onBack={() => {}}
        onOpenSession={() => {}}
        onSubmitTask={vi.fn()}
        sessions={[{ ...sessions[0], evidenceStatus: "unknown" }]}
        workspace={{ ...workspace, evidenceStatus: "unknown" }}
      />,
    )

    expect(screen.getByText("Flow")).toBeInTheDocument()
    expect(screen.queryByText("Evidence status")).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: "Sessions 1" }))

    expect(screen.getByText("Completed")).toBeInTheDocument()
    expect(screen.queryByText("Unknown")).not.toBeInTheDocument()
  })

  it("renders blocked evidence status in overview and session rows", () => {
    render(
      <Workspace
        latest={{ ...latest, evidenceStatus: "blocked" }}
        onBack={() => {}}
        onOpenSession={() => {}}
        onSubmitTask={vi.fn()}
        sessions={[{ ...sessions[0], evidenceStatus: "blocked" }]}
        workspace={workspace}
      />,
    )

    expect(screen.getByText("Evidence status")).toBeInTheDocument()
    expect(screen.getAllByText("Blocked")).not.toHaveLength(0)

    fireEvent.click(screen.getByRole("button", { name: "Sessions 1" }))

    expect(screen.getAllByText("Blocked")).not.toHaveLength(0)
  })
})
