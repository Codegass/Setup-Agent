import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { DashboardResponse, LaunchQueueItem, LaunchQueueState } from "@/api/types"

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

  it("teaches a first-run empty state when there are no workspaces or launches", () => {
    const onLaunchSetups = vi.fn()
    render(
      <Dashboard
        data={{ docker: { status: "connected" }, workspaces: [] }}
        onLaunchSetups={onLaunchSetups}
        onOpenSession={() => {}}
        onOpenWorkspace={() => {}}
      />,
    )

    expect(screen.getByText(/No workspaces yet/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Launch your first setup" }))
    expect(onLaunchSetups).toHaveBeenCalled()
  })

  const queueWith = (items: LaunchQueueItem[]): LaunchQueueState => ({
    default_concurrency: 4,
    summary: { queued: 0, launching: 0, running: 0, completed: 0, failed: 0 },
    batches: [
      {
        id: "BATCH-20260607-abcdef",
        status: "running",
        concurrency: 2,
        created: "2026-06-07T02:30:00",
        items,
      },
    ],
  })

  const queueItem = (overrides: Partial<LaunchQueueItem>): LaunchQueueItem => ({
    id: "LAUNCH-00000001",
    row_index: 0,
    repo_url: "https://github.com/apache/dubbo.git",
    workspace_id: "sag-dubbo",
    ref: "dubbo-3.3.3",
    status: "queued",
    pid: null,
    exit_code: null,
    error: null,
    process_log: "logs/project_launches/BATCH-20260607-abcdef/LAUNCH-00000001.log",
    ...overrides,
  })

  it("shows queued launches as greyed placeholder rows in the list", () => {
    render(
      <Dashboard
        data={dashboard}
        launchQueue={queueWith([queueItem({})])}
        onOpenSession={() => {}}
        onOpenWorkspace={() => {}}
      />,
    )

    const rows = screen.getAllByLabelText("Pending launch dubbo")
    expect(rows.length).toBeGreaterThan(0)
    expect(screen.getAllByText("Queued").length).toBeGreaterThan(0)
    expect(
      screen.getAllByText("Waiting for a free setup slot").length,
    ).toBeGreaterThan(0)
  })

  it("does not duplicate launches whose workspace is already discovered", () => {
    render(
      <Dashboard
        data={dashboard}
        launchQueue={queueWith([
          queueItem({ workspace_id: "sag-commons-cli", status: "running" }),
        ])}
        onOpenSession={() => {}}
        onOpenWorkspace={() => {}}
      />,
    )

    expect(screen.queryByLabelText(/pending launch/i)).not.toBeInTheDocument()
  })

  it("shows failed launches inline with their error", () => {
    render(
      <Dashboard
        data={dashboard}
        launchQueue={queueWith([
          queueItem({ status: "failed", error: "sag project exited with code 1" }),
        ])}
        onOpenSession={() => {}}
        onOpenWorkspace={() => {}}
      />,
    )

    expect(
      screen.getAllByText("sag project exited with code 1").length,
    ).toBeGreaterThan(0)
    expect(screen.getAllByText("Failed").length).toBeGreaterThan(0)
  })

  it("opens the delete confirm dialog without triggering the workspace row", () => {
    const onOpenWorkspace = vi.fn()

    render(
      <Dashboard
        data={dashboard}
        onDeleteWorkspace={() => Promise.resolve()}
        onOpenSession={() => {}}
        onOpenWorkspace={onOpenWorkspace}
      />,
    )

    const deleteButton = screen.getAllByRole("button", {
      name: /delete workspace apache\/commons-cli/i,
    })[0]

    fireEvent.keyDown(deleteButton, { key: "Enter" })
    fireEvent.click(deleteButton)

    expect(onOpenWorkspace).not.toHaveBeenCalled()
    expect(screen.getByRole("dialog", { name: "Delete workspace" })).toBeInTheDocument()
  })

  it("confirms a workspace delete with the correct id", async () => {
    const onDeleteWorkspace = vi.fn().mockResolvedValue(undefined)

    render(
      <Dashboard
        data={dashboard}
        onDeleteWorkspace={onDeleteWorkspace}
        onOpenSession={() => {}}
        onOpenWorkspace={() => {}}
      />,
    )

    fireEvent.click(
      screen.getAllByRole("button", {
        name: /delete workspace apache\/commons-cli/i,
      })[0],
    )

    fireEvent.click(screen.getByRole("button", { name: "Delete workspace" }))

    await waitFor(() => {
      expect(onDeleteWorkspace).toHaveBeenCalledWith("sag-commons-cli")
    })
  })

  it("exposes a remove action on failed pending launches", () => {
    render(
      <Dashboard
        data={dashboard}
        launchQueue={queueWith([
          queueItem({ status: "failed", error: "sag project exited with code 1" }),
        ])}
        onDeleteWorkspace={() => Promise.resolve()}
        onOpenSession={() => {}}
        onOpenWorkspace={() => {}}
      />,
    )

    const removeButton = screen.getAllByRole("button", {
      name: /remove failed launch dubbo/i,
    })[0]

    fireEvent.click(removeButton)

    expect(screen.getByRole("dialog", { name: "Remove launch" })).toBeInTheDocument()
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
    const highlighted = rows.filter((row) => row.className.includes("bg-blue-50/60"))
    expect(highlighted.length).toBeGreaterThan(0)
  })

  const twoWorkspaces: DashboardResponse = {
    docker: { status: "connected", version: "27.1.1" },
    workspaces: [
      {
        id: "sag-healthy",
        project: "owner/healthy",
        container: "sag-healthy",
        stack: "Java · Maven",
        docker: { status: "running", image: "sag/base" },
        task: "All good",
        build: { state: "success", tool: "Maven", time: "1s", note: "" },
        test: { state: "pass", pass: 10, fail: 0, skip: 0, total: 10 },
        report: "ready",
        changed: 0,
        updated: "just now",
      },
      {
        id: "sag-broken",
        project: "owner/broken",
        container: "sag-broken",
        stack: "Java · Gradle",
        docker: { status: "running", image: "sag/base" },
        task: "Build failed",
        build: { state: "failure", tool: "Gradle", time: "2s", note: "" },
        test: { state: "pending", pass: 0, fail: 0, skip: 0, total: 0 },
        report: "none",
        changed: 0,
        updated: "just now",
      },
    ],
  }

  it("orders attention-needing workspaces ahead of healthy ones", () => {
    render(
      <Dashboard data={twoWorkspaces} onOpenWorkspace={() => {}} onOpenSession={() => {}} />,
    )

    const rows = screen.getAllByLabelText(/open workspace owner\//i)
    // The failing workspace row renders before the healthy one in the DOM.
    const brokenIndex = rows.findIndex((r) => r.getAttribute("aria-label")?.includes("owner/broken"))
    const healthyIndex = rows.findIndex((r) => r.getAttribute("aria-label")?.includes("owner/healthy"))
    expect(brokenIndex).toBeGreaterThanOrEqual(0)
    expect(brokenIndex).toBeLessThan(healthyIndex)
  })

  it("tints rows that need attention and leaves healthy rows quiet", () => {
    render(
      <Dashboard data={twoWorkspaces} onOpenWorkspace={() => {}} onOpenSession={() => {}} />,
    )

    const broken = screen.getAllByLabelText("Open workspace owner/broken")[0]
    const healthy = screen.getAllByLabelText("Open workspace owner/healthy")[0]
    expect(broken.className).toContain("bg-status-failed-soft")
    expect(healthy.className).not.toContain("bg-status-failed-soft")
  })

  it("filters to attention-only when the Need attention tile is clicked, and clears on toggle", () => {
    render(
      <Dashboard data={twoWorkspaces} onOpenWorkspace={() => {}} onOpenSession={() => {}} />,
    )

    // Both visible initially.
    expect(screen.getAllByLabelText("Open workspace owner/healthy").length).toBeGreaterThan(0)
    expect(screen.getAllByLabelText("Open workspace owner/broken").length).toBeGreaterThan(0)

    const tile = screen.getByRole("button", { name: /filter: need attention/i })
    fireEvent.click(tile)

    // Healthy hidden, failing kept.
    expect(screen.queryByLabelText("Open workspace owner/healthy")).not.toBeInTheDocument()
    expect(screen.getAllByLabelText("Open workspace owner/broken").length).toBeGreaterThan(0)
    expect(tile).toHaveAttribute("aria-pressed", "true")

    // Toggle back.
    fireEvent.click(tile)
    expect(screen.getAllByLabelText("Open workspace owner/healthy").length).toBeGreaterThan(0)
    expect(tile).toHaveAttribute("aria-pressed", "false")
  })

  it("does not make the Need attention tile a filter when nothing needs attention", () => {
    const allHealthy: DashboardResponse = {
      docker: { status: "connected" },
      workspaces: [twoWorkspaces.workspaces[0]],
    }
    render(
      <Dashboard data={allHealthy} onOpenWorkspace={() => {}} onOpenSession={() => {}} />,
    )
    expect(screen.queryByRole("button", { name: /filter: need attention/i })).not.toBeInTheDocument()
  })

  it("shows the paste-many hint in the empty state", () => {
    render(
      <Dashboard
        data={{ docker: { status: "connected" }, workspaces: [] }}
        onLaunchSetups={() => {}}
        onOpenSession={() => {}}
        onOpenWorkspace={() => {}}
      />,
    )
    expect(screen.getByText(/paste a list of repo URLs/i)).toBeInTheDocument()
  })

  it("shows an 'Updated just now' stamp when given a fresh timestamp", () => {
    render(
      <Dashboard
        data={dashboard}
        lastUpdatedAt={Date.now()}
        onOpenSession={() => {}}
        onOpenWorkspace={() => {}}
      />,
    )
    expect(screen.getByText(/updated just now/i)).toBeInTheDocument()
  })

  it("shows a quiet inline indicator when polling fails", () => {
    render(
      <Dashboard
        data={dashboard}
        lastUpdatedAt={Date.now()}
        onOpenSession={() => {}}
        onOpenWorkspace={() => {}}
        pollError="Error: refresh down"
        pollFailed
      />,
    )
    expect(screen.getByText(/couldn't refresh/i)).toBeInTheDocument()
    // The full-page "unavailable" state must NOT appear when data is present.
    expect(screen.queryByText(/dashboard unavailable/i)).not.toBeInTheDocument()
  })
})
