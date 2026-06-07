import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { DashboardResponse } from "@/api/types"

import { Dashboard } from "./Dashboard"

const dashboard: DashboardResponse = {
  docker: { status: "connected", version: "27.1.1" },
  workspaces: [
    {
      id: "sag-commons-cli",
      project: "apache/commons-cli",
      container: "sag-commons-cli",
      stack: "Java · Maven",
      docker: { status: "running", image: "sag/base" },
      task: "Build project and run full test suite",
      build: { state: "success", tool: "Maven", time: "47.2s", note: "" },
      test: { state: "partial", pass: 312, fail: 8, skip: 0, total: 320 },
      report: "ready",
      changed: 7,
      activeSession: "CC-3",
      latestSession: "CC-3",
      updated: "just now",
    },
  ],
}

describe("Dashboard", () => {
  afterEach(() => {
    cleanup()
  })

  it("renders workspace status and task summary", () => {
    render(
      <Dashboard
        data={dashboard}
        onOpenWorkspace={() => {}}
        onOpenSession={() => {}}
      />,
    )

    expect(screen.getByRole("heading", { name: "Workspaces" })).toBeInTheDocument()
    expect(screen.getAllByText("apache/commons-cli")).not.toHaveLength(0)
    expect(screen.getAllByText("Build project and run full test suite")).not.toHaveLength(0)
    expect(screen.getAllByText("Maven · 47.2s")).not.toHaveLength(0)
    expect(screen.getAllByText(/CC-3/)).not.toHaveLength(0)
  })

  it("opens a workspace when its row is clicked", () => {
    const onOpenWorkspace = vi.fn()

    render(
      <Dashboard
        data={dashboard}
        onOpenWorkspace={onOpenWorkspace}
        onOpenSession={() => {}}
      />,
    )

    fireEvent.click(screen.getAllByRole("button", { name: /open workspace apache\/commons-cli/i })[0])

    expect(onOpenWorkspace).toHaveBeenCalledWith("sag-commons-cli")
  })

  it("opens the latest report without triggering the workspace row", () => {
    const onOpenWorkspace = vi.fn()
    const onOpenSession = vi.fn()

    render(
      <Dashboard
        data={dashboard}
        onOpenWorkspace={onOpenWorkspace}
        onOpenSession={onOpenSession}
      />,
    )

    fireEvent.click(
      screen.getAllByRole("button", { name: /open latest report for apache\/commons-cli/i })[0],
    )

    expect(onOpenSession).toHaveBeenCalledWith("sag-commons-cli", "CC-3", "report")
    expect(onOpenWorkspace).not.toHaveBeenCalled()
  })

  it("keeps report action keyboard events from opening the workspace row", () => {
    const onOpenWorkspace = vi.fn()
    const onOpenSession = vi.fn()

    render(
      <Dashboard
        data={dashboard}
        onOpenWorkspace={onOpenWorkspace}
        onOpenSession={onOpenSession}
      />,
    )

    const reportButton = screen.getAllByRole("button", {
      name: /open latest report for apache\/commons-cli/i,
    })[0]

    fireEvent.keyDown(reportButton, { key: "Enter" })
    fireEvent.click(reportButton)

    expect(onOpenSession).toHaveBeenCalledWith("sag-commons-cli", "CC-3", "report")
    expect(onOpenWorkspace).not.toHaveBeenCalled()
  })

  it("keeps details action keyboard events from also opening the parent row", () => {
    const onOpenWorkspace = vi.fn()

    render(
      <Dashboard
        data={dashboard}
        onOpenWorkspace={onOpenWorkspace}
        onOpenSession={() => {}}
      />,
    )

    const detailsButton = screen.getAllByRole("button", {
      name: /open workspace details for apache\/commons-cli/i,
    })[0]

    fireEvent.keyDown(detailsButton, { key: " " })
    fireEvent.click(detailsButton)

    expect(onOpenWorkspace).toHaveBeenCalledTimes(1)
    expect(onOpenWorkspace).toHaveBeenCalledWith("sag-commons-cli")
  })

  it("renders the launch setups action and reports clicks", () => {
    const onLaunchSetups = vi.fn()
    render(
      <Dashboard
        data={dashboard}
        onLaunchSetups={onLaunchSetups}
        onOpenSession={() => {}}
        onOpenWorkspace={() => {}}
      />,
    )

    fireEvent.click(screen.getByRole("button", { name: "Launch setups" }))

    expect(onLaunchSetups).toHaveBeenCalled()
  })

  it("renders the launch queue panel when queue data has batches", () => {
    render(
      <Dashboard
        data={dashboard}
        launchQueue={{
          default_concurrency: 4,
          summary: { queued: 1, launching: 0, running: 0, completed: 0, failed: 0 },
          batches: [
            {
              id: "BATCH-20260607-abcdef",
              status: "running",
              concurrency: 2,
              created: "2026-06-07T02:30:00",
              items: [],
            },
          ],
        }}
        onOpenSession={() => {}}
        onOpenWorkspace={() => {}}
      />,
    )

    expect(screen.getByText("Launch queue")).toBeInTheDocument()
  })

  it("highlights newly launched workspaces", () => {
    render(
      <Dashboard
        data={dashboard}
        highlightedWorkspaces={["sag-commons-cli"]}
        onOpenSession={() => {}}
        onOpenWorkspace={() => {}}
      />,
    )

    const rows = screen.getAllByLabelText(/open workspace/i)
    const highlighted = rows.filter((row) => row.className.includes("bg-blue-50"))
    expect(highlighted.length).toBeGreaterThan(0)
  })
})
