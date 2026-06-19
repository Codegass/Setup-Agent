import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { ActionDetailModal } from "./ActionDetailModal"

afterEach(() => cleanup())

describe("ActionDetailModal", () => {
  it("shows tool output and observation separately", () => {
    render(
      <ActionDetailModal
        onClose={() => {}}
        action={
          {
            toolName: "build",
            success: true,
            output: "$ mvn verify\nBUILD SUCCESS",
            observation: "All 4 modules compiled.",
            refs: [],
            dispatchStatus: null,
          } as never
        }
      />,
    )
    expect(screen.getByText(/raw tool result/i)).toBeInTheDocument()
    expect(screen.getByText(/BUILD SUCCESS/)).toBeInTheDocument()
    expect(screen.getByText(/agent's interpretation/i)).toBeInTheDocument()
    expect(screen.getByText(/All 4 modules compiled/)).toBeInTheDocument()
  })

  it("renders the tool badge and an honest success status", () => {
    render(
      <ActionDetailModal
        onClose={() => {}}
        action={
          {
            toolName: "build",
            success: true,
            output: "ok",
            observation: "",
            refs: [],
            dispatchStatus: null,
          } as never
        }
      />,
    )
    expect(screen.getByText("build")).toBeInTheDocument()
    expect(screen.getByText("success")).toBeInTheDocument()
  })

  it("shows a running status when the dispatch is still pending", () => {
    render(
      <ActionDetailModal
        onClose={() => {}}
        action={
          {
            toolName: "run_tests",
            success: null,
            output: "",
            observation: "",
            refs: [],
            dispatchStatus: "pending",
          } as never
        }
      />,
    )
    expect(screen.getByText("running")).toBeInTheDocument()
  })

  it("reveals the full ref content behind an open-full-output toggle", () => {
    render(
      <ActionDetailModal
        onClose={() => {}}
        action={
          {
            toolName: "build",
            success: true,
            output: "truncated head",
            observation: "done",
            refs: [
              { ref: "out_5f3a9c", label: "out_5f3a9c", content: "FULL OUTPUT BODY", contentLength: 1840 },
            ],
            dispatchStatus: null,
          } as never
        }
      />,
    )
    expect(screen.getByText(/open full output/i)).toBeInTheDocument()
    expect(screen.queryByText("FULL OUTPUT BODY")).not.toBeInTheDocument()
    fireEvent.click(screen.getByText(/open full output/i))
    expect(screen.getByText("FULL OUTPUT BODY")).toBeInTheDocument()
  })

  it("calls onClose when the dialog requests it", () => {
    const onClose = vi.fn()
    render(
      <ActionDetailModal
        onClose={onClose}
        action={
          {
            toolName: "build",
            success: false,
            output: "boom",
            observation: "",
            refs: [],
            dispatchStatus: null,
          } as never
        }
      />,
    )
    fireEvent.click(screen.getByText(/close/i))
    expect(onClose).toHaveBeenCalled()
  })
})
