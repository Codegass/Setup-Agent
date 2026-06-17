import { fireEvent, render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import type { WorkspaceSummary } from "@/api/types"

import { WorkspacePanel } from "./WorkspacePanels"

const workspace: WorkspaceSummary = {
  id: "sag-x",
  project: "owner/x",
  container: "sag-x",
  stack: "Java · Maven",
  docker: { status: "exited", image: "sag/base" },
  task: "t",
  build: "success",
  test: { state: "pass", pass: 1, fail: 0, skip: 0, total: 1 },
  report: "ready",
  changed: 0,
  updated: "now",
}

describe("WorkspacePanel", () => {
  it("renders the settings panel and closes", () => {
    const onClose = vi.fn()
    render(<WorkspacePanel kind="settings" workspace={workspace} latest={null} onClose={onClose} />)
    expect(screen.getByRole("dialog", { name: /settings/i })).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: /close/i }))
    expect(onClose).toHaveBeenCalled()
  })

  it("shows a not-running message for the terminal when the container is stopped", () => {
    render(<WorkspacePanel kind="terminal" workspace={workspace} latest={null} onClose={() => {}} />)
    expect(screen.getByText(/not running/i)).toBeInTheDocument()
  })
})
