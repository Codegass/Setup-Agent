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

  it("polls since the last ledger line it holds while the run is live", async () => {
    vi.useFakeTimers()
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementationOnce(() => Promise.resolve(json(doc())))
      .mockImplementation(() =>
        Promise.resolve(json(doc({ turns: [turn(2, { control_seq: [4, 5] })] }))),
      )

    render(<TimelineTab live sessionId="S1" />)
    await settle()
    await settle(5000)

    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/sessions/S1/trajectory?detail=summary&since_seq=1",
    )
    expect(screen.getByRole("button", { name: /^Turn 1/ })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /^Turn 2/ })).toBeInTheDocument()
  })

  it("holds the turn that was still open until the ledger restates it", async () => {
    // The desync this cut exists to end: a poll catches a turn between its
    // envelope and its result, and a turn-id cut (`since=<the last turn held>`)
    // drops that very turn from every later response. The row then stands
    // forever half-stated — no observation, no bill, a `control_seq` missing the
    // lines a reader would descend with — while the whole-state rule withdraws
    // the warnings that said so.
    vi.useFakeTimers()
    const open = turn(1, {
      phase: "unknown",
      call: { tool: "project", params_ref: "envelope-000003" },
      observation: null,
      control_seq: [3],
    })
    const filled = turn(1, {
      phase: "provision",
      iteration: 1,
      call: { tool: "project", params_ref: "envelope-000003" },
      observation: { ref: "output_6163859b019d" },
      control_seq: [3, 4, 6],
    })
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementationOnce(() =>
        Promise.resolve(
          json(
            doc({
              turns: [open],
              warnings: [{ code: "missing_tool_result", detail: "turn 1", turn_id: 1 }],
            }),
          ),
        ),
      )
      .mockImplementation(() =>
        Promise.resolve(json(doc({ turns: [filled, turn(2, { control_seq: [7] })] }))),
      )

    render(<TimelineTab live sessionId="S1" />)
    await settle()
    await settle(5000)

    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/sessions/S1/trajectory?detail=summary&since_seq=3",
    )
    expect(screen.getByTestId("turn-row-1")).toHaveTextContent("iter 1")
    expect(screen.getByTestId("turn-row-1")).toHaveTextContent("3·4·6")
    expect(screen.queryByText("missing_tool_result")).not.toBeInTheDocument()
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

  it("re-reads the run whole once it stops, even with a poll still on the wire", async () => {
    // The heartbeat skips while a read is in flight, and the run-stopped read
    // borrowed that discipline: a poll still on the wire at the moment `live`
    // flipped false made the one read that carries the exported token ledger
    // return without reading. Nothing issued it again — the poll interval is
    // gone with the run — so the last turns' bills never landed. It waits for
    // the read in flight and asks again.
    vi.useFakeTimers()
    const unbilled = doc({ turns: [turn(1, { control_seq: [1] })] })
    const billed = doc({
      turns: [turn(1, { control_seq: [1], tokens: { input: 4000, output: 205 } })],
    })
    let landPoll = () => {}
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementationOnce(() => Promise.resolve(json(unbilled)))
      .mockImplementationOnce(
        () =>
          new Promise<Response>((resolve) => {
            landPoll = () => resolve(json(unbilled))
          }),
      )
      .mockImplementation(() => Promise.resolve(json(billed)))

    const view = render(<TimelineTab live sessionId="S1" />)
    await settle()
    // The heartbeat fires and does not answer.
    await settle(5000)
    expect(fetchMock).toHaveBeenCalledTimes(2)

    // The run stops while that poll is still out.
    view.rerender(<TimelineTab live={false} sessionId="S1" />)
    await settle()
    expect(fetchMock).toHaveBeenCalledTimes(2)

    landPoll()
    await settle()

    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(fetchMock).toHaveBeenLastCalledWith("/api/sessions/S1/trajectory?detail=summary")
    expect(screen.queryByText(/no turn has been billed yet/i)).not.toBeInTheDocument()
    expect(screen.getByText("4.2k")).toBeInTheDocument()
  })

  it("reloads whole on demand even when the answer lands behind the ledger held", async () => {
    // The watermark orders the view's own two reads against each other. The
    // owner pressing "Reload whole" is not one of those reads: it is a person
    // asking for the server's whole current answer, and the guard swallowed it
    // whenever a poll had moved the document on while the reload was on the
    // wire — the button did nothing, and said nothing about doing nothing.
    vi.useFakeTimers()
    const ahead = doc({
      turns: [turn(1, { iteration: 4, control_seq: [1, 2, 3] })],
      warnings: [],
    })
    const server = doc({
      turns: [turn(1, { iteration: 1, control_seq: [1] })],
      warnings: [{ code: "missing_tool_result", detail: "turn 1", turn_id: 1 }],
    })
    vi.spyOn(globalThis, "fetch")
      .mockImplementationOnce(() => Promise.resolve(json(ahead)))
      .mockImplementation(() => Promise.resolve(json(server)))

    render(<TimelineTab live={false} sessionId="S1" />)
    await settle()
    expect(screen.getByTestId("turn-row-1")).toHaveTextContent("iter 4")

    fireEvent.click(screen.getByRole("button", { name: /reload whole/i }))
    await settle()

    expect(screen.getByTestId("turn-row-1")).toHaveTextContent("iter 1")
    expect(screen.getByText("missing_tool_result")).toBeInTheDocument()
  })

  it("fetches the full tier when a row is expanded, and only once", async () => {
    vi.useFakeTimers()
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementationOnce(() => Promise.resolve(json(doc())))
      .mockImplementation((input) =>
        String(input).includes("/envelopes/")
          ? Promise.resolve(
              json({
                sequence: 1,
                envelope_id: "envelope-1",
                tool: "project",
                exact_params: { action: "clone" },
              }),
            )
          : Promise.resolve(json(doc({ outputs: { output_1: "bash: mvn: not found" } }))),
      )

    render(<TimelineTab live={false} sessionId="S1" />)
    await settle()

    fireEvent.click(screen.getByRole("button", { name: /^Turn 1/ }))
    await settle()

    expect(fetchMock).toHaveBeenCalledWith("/api/sessions/S1/trajectory?detail=full")
    fireEvent.click(screen.getByRole("button", { name: /output_1/ }))
    expect(screen.getByText("bash: mvn: not found")).toBeInTheDocument()

    // Collapse and expand again: the bytes are held, so nothing is asked twice.
    fireEvent.click(screen.getByRole("button", { name: /^Turn 1/ }))
    fireEvent.click(screen.getByRole("button", { name: /^Turn 1/ }))
    await settle()
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it("never lets a slow byte read overwrite a newer state of the ledger", async () => {
    // The full tier is asked for outside the heartbeat's in-flight discipline,
    // because a reader expanding a row is waiting for those bytes. So a long
    // byte read can still be on the wire when the next poll lands, and the
    // answer it eventually brings is the run as it stood BEFORE that poll:
    // applying it whole put the turn back to half-stated, restored the warning
    // the newer state had withdrawn, and dropped nothing visibly — the row just
    // went backwards. The bytes are kept; the stale whole-state is not.
    vi.useFakeTimers()
    // The run as it stood when the row was expanded: turn 1 open, its hole
    // stated, nothing after it.
    const asItStood = doc({
      turns: [turn(1, { control_seq: [1] })],
      warnings: [{ code: "missing_tool_result", detail: "turn 1", turn_id: 1 }],
    })
    const stale = { ...asItStood, outputs: { output_1: "bash: mvn: not found" } }
    const fresh = doc({
      turns: [turn(1, { iteration: 4, control_seq: [1, 2, 3] }), turn(2, { control_seq: [4] })],
      warnings: [],
    })

    let landFull = () => {}
    let summaryReads = 0
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      if (String(input).includes("/envelopes/")) {
        return Promise.resolve(
          json({
            sequence: 1,
            envelope_id: "envelope-1",
            tool: "project",
            exact_params: { action: "clone" },
          }),
        )
      }
      if (String(input).includes("detail=full")) {
        return new Promise<Response>((resolve) => {
          landFull = () => resolve(json(stale))
        })
      }
      summaryReads += 1
      return Promise.resolve(json(summaryReads > 1 ? fresh : asItStood))
    })

    render(<TimelineTab live sessionId="S1" />)
    await settle()

    // A row is expanded: the full tier is asked for, and does not answer yet.
    fireEvent.click(screen.getByRole("button", { name: /^Turn 1/ }))
    await settle()
    expect(fetchMock).toHaveBeenCalledWith("/api/sessions/S1/trajectory?detail=full")

    // The ledger moves under it, and the poll lands first.
    await settle(5000)
    expect(screen.getByRole("button", { name: /^Turn 2/ })).toBeInTheDocument()

    landFull()
    await settle()

    expect(screen.getByRole("button", { name: /^Turn 2/ })).toBeInTheDocument()
    expect(screen.getByTestId("turn-row-1")).toHaveTextContent("1·2·3")
    expect(screen.getByTestId("turn-row-1")).toHaveTextContent("iter 4")
    expect(screen.queryByText("missing_tool_result")).not.toBeInTheDocument()
    // …and the bytes the reader was waiting for still landed.
    fireEvent.click(screen.getByRole("button", { name: /output_1/ }))
    expect(screen.getByText("bash: mvn: not found")).toBeInTheDocument()
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

  it("heads the tab with the per-turn cost of the turns it holds", async () => {
    vi.useFakeTimers()
    vi.spyOn(globalThis, "fetch").mockImplementation(() =>
      Promise.resolve(
        json(
          doc({
            turns: [
              turn(1, {
                tokens: { input: 4000, output: 205 },
                t0: "2026-08-14T11:28:36.000Z",
                t1: "2026-08-14T11:28:49.500Z",
              }),
            ],
          }),
        ),
      ),
    )

    render(<TimelineTab live={false} sessionId="S1" />)
    await settle()

    expect(screen.getByRole("img", { name: /model-response tokens per turn/i })).toBeInTheDocument()
    expect(screen.getByRole("img", { name: /action duration per turn/i })).toBeInTheDocument()
  })

  it("never stacks a poll on a poll that has not answered", async () => {
    // A read of a long ledger can outlast the 5s heartbeat. Firing anyway put
    // one request per tick on the wire and let an older answer land after a
    // newer one, which is a document going backwards.
    vi.useFakeTimers()
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementationOnce(() => Promise.resolve(json(doc())))
      .mockImplementation(() => new Promise<Response>(() => {}))

    render(<TimelineTab live sessionId="S1" />)
    await settle()
    await settle(5000)
    await settle(5000)
    await settle(5000)

    expect(fetchMock).toHaveBeenCalledTimes(2) // the mount read, and one poll
  })

  it("says it is retrying rather than following when a poll fails", async () => {
    // The pill is a claim about freshness. A green "following" over a poll that
    // last failed says the rows are current when they are the run as it stood.
    vi.useFakeTimers()
    vi.spyOn(globalThis, "fetch")
      .mockImplementationOnce(() => Promise.resolve(json(doc())))
      .mockImplementation(() => Promise.reject(new Error("connection reset")))

    render(<TimelineTab live sessionId="S1" />)
    await settle()
    expect(screen.getByText(/following/i)).toBeInTheDocument()

    await settle(5000)

    expect(screen.queryByText(/following/i)).not.toBeInTheDocument()
    expect(screen.getByText(/retrying/i)).toBeInTheDocument()
    expect(screen.getByText(/connection reset/)).toBeInTheDocument()
    // and the rows it already held are still on screen
    expect(screen.getByRole("button", { name: /^Turn 1/ })).toBeInTheDocument()
  })

  it("shows why a session could not be attributed to a run", async () => {
    // The endpoint refuses to serve one run's evidence under another run's id.
    // The refusal names what could not be decided; a bare "409 Conflict" would
    // leave the owner with a blank timeline and no idea what to look at.
    vi.useFakeTimers()
    const detail =
      "Session SETUP-kafka-20260814-072758 cannot be attributed to a run: 2 host " +
      "session directories ran 'kafka' (session_A, session_B) and nothing in the " +
      "container names which run this session is."
    vi.spyOn(globalThis, "fetch").mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ detail }), {
          headers: { "Content-Type": "application/json" },
          status: 409,
          statusText: "Conflict",
        }),
      ),
    )

    render(<TimelineTab live={false} sessionId="S1" />)
    await settle()

    expect(screen.getByText(/cannot be attributed to a run/)).toBeInTheDocument()
    expect(screen.getByText(/session_A, session_B/)).toBeInTheDocument()
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
