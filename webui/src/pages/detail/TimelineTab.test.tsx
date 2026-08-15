import { act, cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { TrajectoryDocument, TrajectoryTurn } from "@/api/types"

import { TimelineTab } from "./TimelineTab"

function turn(id: number, over: Partial<TrajectoryTurn> = {}): TrajectoryTurn {
  return {
    turn_id: id,
    phase: "build",
    actor: "model",
    call: { tool: "project", params_ref: `envelope-${id}` },
    observation: { ref: `output_${id}` },
    control_seq: [id],
    ...over,
  }
}

function doc(over: Partial<TrajectoryDocument> = {}): TrajectoryDocument {
  return {
    schema_version: 1,
    session: { run_id: "run-1", project: "apache/kafka" },
    phases: [{ name: "build", termination: null, gates: [] }],
    turns: [turn(1)],
    annotations: [],
    warnings: [],
    outputs: null,
    ...over,
  }
}

const json = (payload: unknown) =>
  new Response(JSON.stringify(payload), {
    headers: { "Content-Type": "application/json" },
    status: 200,
  })

/** Flush the promises a fetch resolution chains, without leaving fake timers. */
async function settle(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

describe("TimelineTab", () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it("reads the summary tier whole on mount and draws the turns", async () => {
    vi.useFakeTimers()
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(() => Promise.resolve(json(doc())))

    render(<TimelineTab live={false} sessionId="S1" />)
    await settle()

    expect(fetchMock).toHaveBeenCalledWith("/api/sessions/S1/trajectory?detail=summary")
    expect(screen.getByRole("button", { name: /^Turn 1/ })).toBeInTheDocument()
  })

  it("polls since the last turn it holds while the run is live", async () => {
    vi.useFakeTimers()
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementationOnce(() => Promise.resolve(json(doc())))
      .mockImplementation(() => Promise.resolve(json(doc({ turns: [turn(2)] }))))

    render(<TimelineTab live sessionId="S1" />)
    await settle()
    await settle(5000)

    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/sessions/S1/trajectory?detail=summary&since=1",
    )
    expect(screen.getByRole("button", { name: /^Turn 1/ })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /^Turn 2/ })).toBeInTheDocument()
  })

  it("does not poll a session that is no longer running", async () => {
    vi.useFakeTimers()
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(() => Promise.resolve(json(doc())))

    render(<TimelineTab live={false} sessionId="S1" />)
    await settle()
    await settle(20000)

    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("re-reads the run whole once it stops, so the exported token ledger lands", async () => {
    vi.useFakeTimers()
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(() => Promise.resolve(json(doc())))

    const view = render(<TimelineTab live sessionId="S1" />)
    await settle()
    view.rerender(<TimelineTab live={false} sessionId="S1" />)
    await settle()

    expect(fetchMock).toHaveBeenLastCalledWith("/api/sessions/S1/trajectory?detail=summary")
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it("fetches the full tier when a row is expanded, and only once", async () => {
    vi.useFakeTimers()
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementationOnce(() => Promise.resolve(json(doc())))
      .mockImplementation(() =>
        Promise.resolve(json(doc({ outputs: { output_1: "bash: mvn: not found" } }))),
      )

    render(<TimelineTab live={false} sessionId="S1" />)
    await settle()

    fireEvent.click(screen.getByRole("button", { name: /^Turn 1/ }))
    await settle()

    expect(fetchMock).toHaveBeenLastCalledWith("/api/sessions/S1/trajectory?detail=full")
    fireEvent.click(screen.getByRole("button", { name: /output_1/ }))
    expect(screen.getByText("bash: mvn: not found")).toBeInTheDocument()

    // Collapse and expand again: the bytes are held, so nothing is asked twice.
    fireEvent.click(screen.getByRole("button", { name: /^Turn 1/ }))
    fireEvent.click(screen.getByRole("button", { name: /^Turn 1/ }))
    await settle()
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it("states the holes the reducer reported for the run as a whole", async () => {
    vi.useFakeTimers()
    vi.spyOn(globalThis, "fetch").mockImplementation(() =>
      Promise.resolve(
        json(
          doc({
            turns: [],
            warnings: [{ code: "missing_control_events", detail: "no ledger has been written yet" }],
          }),
        ),
      ),
    )

    render(<TimelineTab live sessionId="S1" />)
    await settle()

    expect(screen.getByText("missing_control_events")).toBeInTheDocument()
    expect(screen.getByText(/no ledger has been written yet/)).toBeInTheDocument()
  })

  it("surfaces a failed read and retries on demand", async () => {
    vi.useFakeTimers()
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementationOnce(() =>
        Promise.resolve(new Response("nope", { status: 404, statusText: "Not Found" })),
      )
      .mockImplementation(() => Promise.resolve(json(doc())))

    render(<TimelineTab live={false} sessionId="S1" />)
    await settle()

    expect(screen.getByText(/404 Not Found/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: /retry/i }))
    await settle()

    expect(screen.getByRole("button", { name: /^Turn 1/ })).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })
})
