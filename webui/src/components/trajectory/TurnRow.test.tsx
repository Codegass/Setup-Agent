import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { TrajectoryTurn } from "@/api/types"

import { TurnRow } from "./TurnRow"

/**
 * A turn the reducer can actually emit. Every string is one the corpus
 * produces: `verify mvn clean verify` is `action + command` from a build call,
 * and `exit 1 · MAVEN_VERSION_ERROR` is the failed-observation line that pairs
 * with that error code 14 times across the recorded runs.
 */
function turn(overrides: Partial<TrajectoryTurn> = {}): TrajectoryTurn {
  return {
    turn_id: 8,
    phase: "build",
    iteration: 5,
    actor: "model",
    call: { tool: "build", summary: "verify mvn clean verify", params_ref: "envelope-000065" },
    observation: {
      outcome: "failed",
      summary: "exit 1 · MAVEN_VERSION_ERROR",
      error_code: "MAVEN_VERSION_ERROR",
    },
    control_seq: [65, 66, 68],
    ...overrides,
  }
}

/** The five props every caller of TurnRow passes; a row reads nothing else. */
function row(over: Partial<TrajectoryTurn> = {}) {
  return (
    <TurnRow
      annotations={[]}
      outputs={null}
      phases={[]}
      status="absent"
      turn={turn(over)}
      warnings={[]}
    />
  )
}

describe("TurnRow", () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it("reads as one line: number, tool, what it asked, how it came out", () => {
    render(row())

    expect(screen.getByText("Turn 8")).toBeInTheDocument()
    expect(screen.getByText("build")).toBeInTheDocument()
    expect(screen.getByText("verify mvn clean verify")).toBeInTheDocument()
    expect(screen.getByText("failed")).toBeInTheDocument()
    expect(screen.getByText("exit 1 · MAVEN_VERSION_ERROR")).toBeInTheDocument()
  })

  it("states both bills a consult turn carries, each under its own name", () => {
    // The run paid two models on this turn: its own response, and the advice it
    // asked for. Unlabelled numbers side by side would read as one measure
    // stated twice, and added together they would be a bill nobody was sent.
    render(
      row({
        call: { tool: "advisor", summary: "consult" },
        tokens: { input: 8648, output: 199 },
        advisor_tokens: { input: 60239, output: 181 },
      }),
    )

    expect(screen.getByText("model 8648 in / 199 out")).toBeInTheDocument()
    expect(screen.getByText("advisor 60239 in / 181 out")).toBeInTheDocument()
  })

  it("says nothing about the advisor on a turn that consulted nobody", () => {
    render(row({ tokens: { input: 8648, output: 199 } }))

    expect(screen.getByText("model 8648 in / 199 out")).toBeInTheDocument()
    expect(screen.queryByText(/advisor/)).not.toBeInTheDocument()
  })

  it("marks a controller turn as the engine", () => {
    render(row({ turn_id: 9, actor: "controller" }))

    expect(screen.getByText("engine")).toBeInTheDocument()
    expect(screen.queryByText("controller")).not.toBeInTheDocument()
  })

  it("says a pending call is still running", () => {
    render(
      row({
        turn_id: 10,
        observation: { outcome: "pending", summary: "running · job 2c4d56b2fdca" },
      }),
    )

    expect(screen.getByText("pending")).toBeInTheDocument()
    expect(screen.getByText("running · job 2c4d56b2fdca")).toBeInTheDocument()
  })

  it("says when a turn called nothing, once", () => {
    render(row({ turn_id: 11, call: null, observation: null }))

    expect(screen.getByText(/no call/i)).toBeInTheDocument()
  })

  it("names the tool when the run recorded no line about the call", () => {
    render(row({ turn_id: 12, call: { tool: "project", params_ref: null } }))

    expect(screen.getByText("project")).toBeInTheDocument()
  })

  it("does not print the error code twice when the call summarised itself as one", () => {
    render(
      row({
        turn_id: 14,
        observation: {
          outcome: "failed",
          summary: "ENV_EXECUTABLE_NOT_FOUND",
          error_code: "ENV_EXECUTABLE_NOT_FOUND",
        },
      }),
    )

    expect(screen.getByText("ENV_EXECUTABLE_NOT_FOUND")).toBeInTheDocument()
  })

  it("expands into the model context, the call and the result", () => {
    render(row({ turn_id: 13 }))

    fireEvent.click(screen.getByRole("button", { name: /^Turn 13/ }))

    expect(screen.getByRole("heading", { name: "Model context" })).toBeInTheDocument()
    expect(screen.getByRole("heading", { name: "Tool call" })).toBeInTheDocument()
    expect(screen.getByRole("heading", { name: "Tool result" })).toBeInTheDocument()
  })

  it("shows an automatic close as a status line with inspectable original bytes", () => {
    const onNeedBytes = vi.fn()
    const close = turn({ turn_id: 17, phase: "report", actor: "controller", call: null,
      gate: { word: "success", accepted: true, code: "report_present", decision_id: "gate-report", reason: "Report bytes match the sealed snapshot." },
      observation: { ref: "output_close" }, control_seq: [106, 107],
      t0: "2026-10-21T08:44:05.555Z", t1: "2026-10-21T08:44:05.657Z" })
    const props = { annotations: [], warnings: [], phases: [], turn: close, onNeedBytes }
    const { rerender } = render(<TurnRow {...props} outputs={null} status="absent" />)
    const toggle = screen.getByRole("button", { name: "Report verified · automatic event 17 · 102ms" })
    expect(screen.queryByText("Turn 17")).not.toBeInTheDocument()
    expect(screen.queryByText("no call")).not.toBeInTheDocument()
    fireEvent.click(toggle)
    expect(onNeedBytes).toHaveBeenCalledOnce()
    expect(screen.getByText("Turn 17 · SAG controller")).toBeInTheDocument()
    expect(screen.getByText(/106.*107/)).toBeInTheDocument()
    expect(screen.queryByRole("heading", { name: "Model context" })).not.toBeInTheDocument()
    expect(screen.queryByRole("heading", { name: "Tool call" })).not.toBeInTheDocument()
    expect(screen.queryByRole("heading", { name: "Tool result" })).not.toBeInTheDocument()
    const bytes = "PHASE_CLOSED: the harness closed phase 'report' with validated outcome 'success' (report_present): persisted report bytes match the current sealed snapshot"
    rerender(<TurnRow {...props} outputs={{ output_close: bytes }} status="ready" />)
    fireEvent.click(screen.getByRole("button", { name: "output_close" }))
    expect(screen.getByTestId("ref-output-output_close")).toHaveTextContent(bytes)
  })

  it.each([
    [{ word: "failed", accepted: true }, "Report check failed"],
    [{ word: "partial", accepted: true }, "Report check incomplete"],
    [{ word: "success", accepted: false }, "Report check not accepted"],
    [{ word: "success" }, "Report check unresolved"],
    [null, "Automatic event"],
  ])("does not promote a failed or unconfirmed automatic check", (gate, label) => {
    render(row({ actor: "controller", phase: "report", call: null, observation: null, gate }))
    expect(screen.getByText(label)).toBeInTheDocument()
    expect(screen.queryByText("Report verified")).not.toBeInTheDocument()
  })

  it.each(["failed", "refused", "cancelled", "pending"] as const)("retains a controller %s outcome without a call envelope", outcome => {
    render(row({ actor: "controller", call: null, gate: null, observation: { outcome } }))
    expect(screen.getByText(`Automatic action ${outcome}`)).toBeInTheDocument()
  })
})
