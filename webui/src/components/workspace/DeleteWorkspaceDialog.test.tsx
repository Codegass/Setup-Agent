import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { DeleteWorkspaceDialog } from "./DeleteWorkspaceDialog"

const target = { workspaceId: "sag-x", label: "apache/x", kind: "workspace" as const }

describe("DeleteWorkspaceDialog", () => {
  afterEach(() => {
    cleanup()
  })

  it("surfaces the server error message (no Error: prefix) and stays open", async () => {
    const onConfirm = vi
      .fn()
      .mockRejectedValue(new Error("Workspace has an active launch: sag-x"))

    render(
      <DeleteWorkspaceDialog target={target} onCancel={() => {}} onConfirm={onConfirm} />,
    )

    fireEvent.click(screen.getByRole("button", { name: "Delete workspace" }))

    expect(
      await screen.findByText("Workspace has an active launch: sag-x"),
    ).toBeInTheDocument()
    // The JS "Error: " prefix must not leak into the user-facing message.
    expect(screen.queryByText(/^Error: /)).not.toBeInTheDocument()
    // The dialog remains open so the user can read the message and retry/cancel.
    expect(screen.getByRole("button", { name: "Delete workspace" })).toBeInTheDocument()
  })

  it("calls onConfirm with the workspace id on confirm", async () => {
    const onConfirm = vi.fn().mockResolvedValue(undefined)

    render(
      <DeleteWorkspaceDialog target={target} onCancel={() => {}} onConfirm={onConfirm} />,
    )

    fireEvent.click(screen.getByRole("button", { name: "Delete workspace" }))

    expect(onConfirm).toHaveBeenCalledWith("sag-x")
  })
})
