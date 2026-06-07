import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { LaunchBatchResult } from "@/api/types"

import { LaunchSetupsDialog } from "./LaunchSetupsDialog"

const accepted = (workspaceId: string, rowIndex: number) => ({
  launch_id: `LAUNCH-0000000${rowIndex}`,
  row_index: rowIndex,
  workspace_id: workspaceId,
  status: "queued",
})

function renderDialog(overrides?: {
  onSubmit?: (payload: unknown) => Promise<LaunchBatchResult>
  onSubmitted?: (result: LaunchBatchResult) => void
  onClose?: () => void
}) {
  const onSubmit =
    overrides?.onSubmit ??
    vi.fn().mockResolvedValue({
      status: 202,
      batch_id: "BATCH-20260607-abcdef",
      concurrency: 2,
      accepted: [accepted("sag-commons-cli", 0)],
      rejected: [],
    })
  const onSubmitted = overrides?.onSubmitted ?? vi.fn()
  const onClose = overrides?.onClose ?? vi.fn()

  render(
    <LaunchSetupsDialog
      defaultConcurrency={2}
      onClose={onClose}
      onSubmit={onSubmit}
      onSubmitted={onSubmitted}
    />,
  )

  return { onSubmit, onSubmitted, onClose }
}

describe("LaunchSetupsDialog", () => {
  afterEach(() => {
    cleanup()
  })

  it("opens with one empty row and the optional columns", () => {
    renderDialog()

    expect(screen.getByLabelText("Repository URL row 1")).toHaveValue("")
    expect(screen.getByLabelText("Name row 1")).toHaveValue("")
    expect(screen.getByLabelText("Ref row 1")).toHaveValue("")
    expect(screen.getByLabelText("Goal row 1")).toHaveValue("")
    expect(screen.getByLabelText("Record row 1")).not.toBeChecked()
    expect(screen.getByLabelText("Concurrency")).toHaveValue(2)
  })

  it("adds and removes rows", () => {
    renderDialog()

    fireEvent.click(screen.getByRole("button", { name: "Add row" }))
    expect(screen.getByLabelText("Repository URL row 2")).toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: "Remove row 2" }))
    expect(screen.queryByLabelText("Repository URL row 2")).not.toBeInTheDocument()
  })

  it("creates one row per pasted line and fills refs", () => {
    renderDialog()

    fireEvent.paste(screen.getByLabelText("Repository URL row 1"), {
      clipboardData: {
        getData: () =>
          "https://github.com/apache/commons-cli.git rel/commons-cli-1.11.0\n" +
          "https://github.com/apache/dubbo.git dubbo-3.2.19",
      },
    })

    expect(screen.getByLabelText("Repository URL row 1")).toHaveValue(
      "https://github.com/apache/commons-cli.git",
    )
    expect(screen.getByLabelText("Ref row 1")).toHaveValue("rel/commons-cli-1.11.0")
    expect(screen.getByLabelText("Repository URL row 2")).toHaveValue(
      "https://github.com/apache/dubbo.git",
    )
    expect(screen.getByLabelText("Ref row 2")).toHaveValue("dubbo-3.2.19")
  })

  it("submits trimmed rows and reports the result", async () => {
    const { onSubmit, onSubmitted } = renderDialog()

    fireEvent.change(screen.getByLabelText("Repository URL row 1"), {
      target: { value: " https://github.com/apache/commons-cli.git " },
    })
    fireEvent.change(screen.getByLabelText("Ref row 1"), {
      target: { value: "v1.0" },
    })
    fireEvent.click(screen.getByLabelText("Record row 1"))
    fireEvent.click(screen.getByRole("button", { name: "Launch setups" }))

    await waitFor(() => expect(onSubmitted).toHaveBeenCalled())
    expect(onSubmit).toHaveBeenCalledWith({
      concurrency: 2,
      projects: [
        {
          repo_url: "https://github.com/apache/commons-cli.git",
          name: null,
          ref: "v1.0",
          goal: null,
          record: true,
        },
      ],
    })
  })

  it("keeps input and shows row-level errors when every row conflicts", async () => {
    const onSubmit = vi.fn().mockResolvedValue({
      status: 409,
      batch_id: null,
      concurrency: 2,
      accepted: [],
      rejected: [
        {
          row_index: 0,
          workspace_id: "sag-existing",
          status: "conflict",
          message: "Workspace already exists: sag-existing",
        },
      ],
    } satisfies LaunchBatchResult)
    const { onSubmitted } = renderDialog({ onSubmit })

    fireEvent.change(screen.getByLabelText("Repository URL row 1"), {
      target: { value: "https://github.com/x/existing.git" },
    })
    fireEvent.click(screen.getByRole("button", { name: "Launch setups" }))

    await waitFor(() =>
      expect(
        screen.getByText(/Workspace already exists: sag-existing/),
      ).toBeInTheDocument(),
    )
    expect(onSubmitted).not.toHaveBeenCalled()
    expect(screen.getByLabelText("Repository URL row 1")).toHaveValue(
      "https://github.com/x/existing.git",
    )
  })

  it("flags non-empty rows that are missing a repo url", async () => {
    const { onSubmit } = renderDialog()

    fireEvent.change(screen.getByLabelText("Ref row 1"), {
      target: { value: "v1.0" },
    })
    fireEvent.click(screen.getByRole("button", { name: "Launch setups" }))

    expect(await screen.findByText(/Repository URL is required/)).toBeInTheDocument()
    expect(onSubmit).not.toHaveBeenCalled()
  })
})
