import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { DashboardResponse, WorkspaceSummary } from "@/api/types"
import { WorkspaceRail } from "./WorkspaceRail"

function ws(overrides: Partial<WorkspaceSummary>): WorkspaceSummary {
  return {
    id: "sag-x", project: "owner/x", container: "sag-x", stack: "Java · Maven",
    docker: { status: "running", image: "sag/base" }, task: "t",
    build: { state: "success", tool: "Maven", time: "1s", note: "" },
    test: { state: "pass", pass: 10, fail: 0, skip: 0, total: 10 },
    report: "ready", changed: 0, updated: "just now", ...overrides,
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
})
