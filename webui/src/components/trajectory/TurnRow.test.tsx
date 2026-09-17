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
})
