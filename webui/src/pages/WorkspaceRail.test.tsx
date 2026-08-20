import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { DashboardResponse, EvidenceLayerProjectionSummary, LaunchQueueItem, LaunchQueueState, WorkspaceSummary } from "@/api/types"
import { WorkspaceRail } from "./WorkspaceRail"

function queueItem(overrides: Partial<LaunchQueueItem>): LaunchQueueItem {
  return {
    id: "li-1", row_index: 0, repo_url: "https://github.com/a/b.git", workspace_id: "sag-b",
    ref: null, status: "queued", pid: null, exit_code: null, error: null, process_log: "", ...overrides,
  }
}

function queue(items: LaunchQueueItem[]): LaunchQueueState {
  return {
    default_concurrency: 2,
    summary: { queued: 0, launching: 0, running: 0, completed: 0, failed: 0 },
    batches: [{ id: "batch-1", status: "running", concurrency: 2, created: "now", items }],
  }
}

function ws(overrides: Partial<WorkspaceSummary>): WorkspaceSummary {
  return {
    id: "sag-x", project: "owner/x", container: "sag-x", stack: "Java · Maven",
    docker: { status: "running", image: "sag/base" }, task: "t",
    build: { state: "success", tool: "Maven", time: "1s", note: "" },
    test: { state: "pass", pass: 10, fail: 0, skip: 0, total: 10 },
    report: "ready", changed: 0, updated: "just now", ...overrides,
  }
}

function evidenceLayers(): EvidenceLayerProjectionSummary {
  const unavailable = {
    executed: null, passed: null, failed: null, errors: null, skipped: null,
    availability: "unavailable" as const, reason: "module-qualified subject identity unavailable",
  }
  return {
    projectionStatus: "metrics-v2-artifact-unavailable" as const,
    tests: {
      claimed: {
        latestSubjects: unavailable,
        latestCases: unavailable,
        receiptExecutions: {
          executed: 2, passed: 2, failed: 0, errors: 0, skipped: 0, availability: "available" as const,
        },
      },
      quarantinedObservations: {
        executed: 2887, passed: 267, failed: 28, errors: 2481, skipped: 111, availability: "available" as const,
      },
      unattributedObservations: {
        executed: 0, passed: 0, failed: 0, errors: 0, skipped: 0, availability: "available" as const,
      },
      staleObservations: {
        executed: 0, passed: 0, failed: 0, errors: 0, skipped: 0, availability: "available" as const,
      },
    },
    evidence: {
      integrity: "complete" as const, receiptsExpected: 1, receiptsPersisted: 1,
      terminalReceiptsUnpersisted: 0, conflictCount: 0,
    },
  }
}

const data: DashboardResponse = {
  docker: { status: "connected", version: "27.1.1" },
  workspaces: [
    ws({ id: "sag-healthy", project: "owner/healthy" }),
    ws({ id: "sag-broken", project: "owner/broken", build: { state: "failure", tool: "Maven", time: "", note: "" } }),
  ],
}

const props = {
  data, selectedId: "sag-healthy", onSelect: () => {}, onLaunchSetups: () => {},
  launchQueue: null, highlightedWorkspaces: [], lastUpdatedAt: Date.now(), pollFailed: false,
}

describe("WorkspaceRail", () => {
  afterEach(() => {
    cleanup()
  })

  it("renders a row per workspace and marks the selected one", () => {
    render(<WorkspaceRail {...props} />)
    expect(screen.getByRole("button", { name: /owner\/healthy/ })).toHaveAttribute("aria-current", "true")
    expect(screen.getByRole("button", { name: /owner\/broken/ })).toHaveAttribute("aria-current", "false")
  })

  it("shows sealed run results while explaining unavailable identity attribution", () => {
    const modern = ws({
      id: "sag-ignite",
      project: "apache/ignite",
      test: {
        state: "success", pass: 2, fail: 0, skip: 0, total: 2,
        evidenceLayers: evidenceLayers(),
      },
    })
    render(
      <WorkspaceRail
        {...props}
        data={{ ...data, workspaces: [modern] }}
        selectedId="sag-ignite"
      />,
    )

    expect(screen.getByRole("img", { name: /2 passed, 0 failed, 2 total/i })).toBeInTheDocument()
    expect(screen.getByText(/sealed run: 2 passed/i)).toBeInTheDocument()
    expect(screen.getByText(/stable module and test identities unavailable/i)).toBeInTheDocument()
    expect(screen.getAllByText(/2,887 additional diagnostics.*excluded from the sealed run result/i).length).toBeGreaterThan(0)
  })

  it("shows partial builds as partial and labels running workspace counts as containers", () => {
    render(
      <WorkspaceRail
        {...props}
        data={{
          ...data,
          workspaces: [ws({ build: { state: "partial", tool: "Maven", time: "", note: "" } })],
        }}
      />,
    )

    expect(screen.getByText("Containers")).toBeInTheDocument()
    expect(screen.getByText("Build partially completed")).toBeInTheDocument()
    expect(screen.getByText("1 partial")).toBeInTheDocument()
    expect(screen.queryByText(/no build result yet/i)).not.toBeInTheDocument()
  })

  it("does not print placeholder stack metadata", () => {
    render(
      <WorkspaceRail
        {...props}
        data={{ ...data, workspaces: [ws({ stack: "Unknown", commit: "abc123" })] }}
      />,
    )

    expect(screen.queryByText("Unknown")).not.toBeInTheDocument()
    expect(screen.getByText("abc123")).toBeInTheDocument()
  })

  it("reports measured identity coverage without discarding unmeasured workspaces", () => {
    const measuredLayers = evidenceLayers()
    measuredLayers.tests.claimed.latestSubjects = {
      executed: 10, passed: 8, failed: 1, errors: 1, skipped: 0,
      availability: "available",
    }
    render(
      <WorkspaceRail
        {...props}
        data={{
          ...data,
          workspaces: [
            ws({ id: "measured", test: { state: "failed", pass: 8, fail: 1, errors: 1, skip: 0, total: 10, evidenceLayers: measuredLayers } }),
            ws({ id: "unmeasured", test: { state: "success", pass: 3, fail: 0, skip: 0, total: 3, evidenceLayers: evidenceLayers() } }),
          ],
        }}
      />,
    )

    const identityCard = screen.getByText("Identity coverage").parentElement
    expect(within(identityCard as HTMLElement).getByText("1/2")).toBeInTheDocument()
    expect(within(identityCard as HTMLElement).getByText("80% measured subset")).toBeInTheDocument()
  })

  it("marks diagnostic totals as lower bounds when a source is unavailable", () => {
    const layers = evidenceLayers()
    layers.tests.staleObservations = {
      executed: null, passed: null, failed: null, errors: null, skipped: null,
      availability: "unavailable", reason: "not countable",
    }
    render(
      <WorkspaceRail
        {...props}
        data={{ ...data, workspaces: [ws({ test: { state: "success", pass: 1, fail: 0, skip: 0, total: 1, evidenceLayers: layers } })] }}
      />,
    )

    expect(screen.getByText("2,887+")).toBeInTheDocument()
    expect(screen.getByText(/at least 2,887 diagnostics/i)).toBeInTheDocument()
  })

  it("orders attention-needing workspaces first", () => {
    render(<WorkspaceRail {...props} />)
    const rows = screen.getAllByRole("button", { name: /owner\// })
    expect(rows[0].getAttribute("aria-label")).toContain("owner/broken")
  })

  it("selects a workspace when its row is clicked", () => {
    const onSelect = vi.fn()
    render(<WorkspaceRail {...props} onSelect={onSelect} />)
    fireEvent.click(screen.getByRole("button", { name: /owner\/broken/ }))
    expect(onSelect).toHaveBeenCalledWith("sag-broken")
  })

  it("calls onAfterSelect after selecting a workspace (drawer close hook)", () => {
    const onSelect = vi.fn()
    const onAfterSelect = vi.fn()
    render(<WorkspaceRail {...props} onAfterSelect={onAfterSelect} onSelect={onSelect} />)
    fireEvent.click(screen.getByRole("button", { name: /owner\/broken/ }))
    expect(onSelect).toHaveBeenCalledWith("sag-broken")
    expect(onAfterSelect).toHaveBeenCalled()
  })

  it("renders a workspace in a non-interactive deleting state", () => {
    const onSelect = vi.fn()
    render(<WorkspaceRail {...props} deletingIds={new Set(["sag-broken"])} onSelect={onSelect} />)
    const row = screen.getByRole("button", { name: /owner\/broken/ })
    expect(row).toBeDisabled()
    expect(screen.getByText(/deleting/i)).toBeInTheDocument()
    fireEvent.click(row)
    expect(onSelect).not.toHaveBeenCalled()
  })

  it("batch-deletes selected workspaces via select mode", async () => {
    const onDeleteMany = vi.fn().mockResolvedValue(undefined)
    render(<WorkspaceRail {...props} onDeleteMany={onDeleteMany} />)
    fireEvent.click(screen.getByRole("button", { name: /^select$/i }))
    fireEvent.click(screen.getByRole("checkbox", { name: /owner\/healthy/i }))
    fireEvent.click(screen.getByRole("checkbox", { name: /owner\/broken/i }))
    fireEvent.click(screen.getByRole("button", { name: /delete 2 selected/i }))
    fireEvent.click(screen.getByRole("button", { name: /delete 2 workspaces/i }))
    await waitFor(() => expect(onDeleteMany).toHaveBeenCalled())
    expect(onDeleteMany.mock.calls[0][0]).toEqual(
      expect.arrayContaining(["sag-healthy", "sag-broken"]),
    )
  })

  it("filters rows by the query input", () => {
    render(<WorkspaceRail {...props} />)
    fireEvent.change(screen.getByPlaceholderText(/filter/i), { target: { value: "broken" } })
    expect(screen.getByRole("button", { name: /owner\/broken/ })).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /owner\/healthy/ })).not.toBeInTheDocument()
  })

  it("fires the launch action", () => {
    const onLaunchSetups = vi.fn()
    render(<WorkspaceRail {...props} onLaunchSetups={onLaunchSetups} />)
    fireEvent.click(screen.getByRole("button", { name: /launch setups/i }))
    expect(onLaunchSetups).toHaveBeenCalled()
  })

  it("shows the updated stamp in the footer", () => {
    render(<WorkspaceRail {...props} />)
    expect(screen.getByText(/updated just now/i)).toBeInTheDocument()
  })

  it("renders pending launches as muted setting-up rows above workspaces", () => {
    render(
      <WorkspaceRail
        {...props}
        launchQueue={queue([
          queueItem({ id: "q1", workspace_id: "sag-queued", status: "queued" }),
          queueItem({ id: "r1", workspace_id: "sag-launching", status: "launching" }),
        ])}
      />,
    )
    expect(screen.getByText(/waiting for a free setup slot/i)).toBeInTheDocument()
    expect(screen.getByText(/setting up/i)).toBeInTheDocument()
  })

  it("renders a failed launch with its error and a remove button", () => {
    const onRemoveLaunch = vi.fn().mockResolvedValue(undefined)
    render(
      <WorkspaceRail
        {...props}
        launchQueue={queue([
          queueItem({ id: "f1", workspace_id: "sag-fail", status: "failed", error: "clone failed" }),
        ])}
        onRemoveLaunch={onRemoveLaunch}
      />,
    )
    expect(screen.getByText("clone failed")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /remove failed launch fail/i })).toBeInTheDocument()
  })

  it("does not show the empty state while launches are queued", () => {
    render(
      <WorkspaceRail
        {...props}
        data={{ ...data, workspaces: [] }}
        launchQueue={queue([queueItem({ id: "q1", workspace_id: "sag-queued", status: "queued" })])}
        selectedId={null}
      />,
    )
    expect(screen.queryByText(/no workspaces yet/i)).not.toBeInTheDocument()
    expect(screen.getByLabelText(/pending launch queued/i)).toBeInTheDocument()
  })

  it("distinguishes an unavailable workspace read from an empty dashboard", () => {
    render(
      <WorkspaceRail
        {...props}
        data={{
          ...data,
          readStatus: "unavailable",
          readError: "workspace registry read failed",
          workspaces: [],
        }}
        selectedId={null}
      />,
    )

    expect(screen.getByRole("alert")).toHaveTextContent("Workspace data unavailable")
    expect(screen.getByRole("button", { name: /retry dashboard/i })).toBeInTheDocument()
    expect(screen.queryByText(/no workspaces yet/i)).not.toBeInTheDocument()
  })
})
