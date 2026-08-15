import { cleanup, render, screen, within } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import type { TrajectoryDocument, TrajectoryTurn } from "@/api/types"

import { TurnSparkline } from "./TurnSparkline"

function turn(id: number, over: Partial<TrajectoryTurn> = {}): TrajectoryTurn {
  return {
    turn_id: id,
    phase: "build",
    actor: "model",
    control_seq: [id],
    ...over,
  }
}

function doc(over: Partial<TrajectoryDocument> = {}): TrajectoryDocument {
  return {
    schema_version: 1,
    session: { run_id: "run-1" },
    phases: [],
    turns: [
      turn(1, {
        tokens: { input: 4000, output: 205 },
        t0: "2026-08-14T11:28:36.000Z",
        t1: "2026-08-14T11:28:49.500Z",
      }),
      turn(2, {
        tokens: { input: 800, output: 40 },
        t0: "2026-08-14T11:29:00.000Z",
        t1: "2026-08-14T11:29:00.400Z",
      }),
    ],
    annotations: [],
    warnings: [],
    outputs: null,
    ...over,
  }
}

describe("TurnSparkline", () => {
  afterEach(() => cleanup())

  it("draws tokens and duration as two charts, never one with two scales", () => {
    render(<TurnSparkline doc={doc()} />)

    // Two measures of different units share no axis: each is its own chart,
    // scaled to its own peak and labelled with it.
    expect(screen.getByRole("img", { name: /tokens per turn/i })).toBeInTheDocument()
    expect(screen.getByRole("img", { name: /duration per turn/i })).toBeInTheDocument()
    expect(screen.getByText("4.2k")).toBeInTheDocument()
    expect(screen.getByText("13.5s")).toBeInTheDocument()
  })

  it("gives every column an SVG title, so a hover names the turn it is about", () => {
    // `<title>` inside the mark is the hover layer an SVG chart gets natively,
    // and it carries the EXACT value the axis label had to round.
    render(<TurnSparkline doc={doc()} />)

    expect(screen.getByText(/turn 1 · 4,205 tokens/i)).toBeInTheDocument()
    expect(screen.getByText(/turn 2 · 840 tokens/i)).toBeInTheDocument()
    expect(screen.getByText(/turn 1 · 13.5s/i)).toBeInTheDocument()
  })

  it("draws an unstated bill as an absence, not as a free turn", () => {
    // The token ledger is exported at loop exit, so the turns of a live run are
    // routinely unbilled. A zero-height bar is indistinguishable from a turn
    // that cost nothing, and this run's most interesting turn is the open one.
    render(<TurnSparkline doc={doc({ turns: [turn(1), turn(2, { tokens: null })] })} />)

    expect(screen.getAllByText(/no tokens stated/i)).toHaveLength(2)
    expect(screen.getByText(/no turn has been billed yet/i)).toBeInTheDocument()
  })

  it("marks the turns the reducer said the run bled on", () => {
    render(
      <TurnSparkline
        doc={doc({
          annotations: [
            {
              kind: "conflict",
              turn_id: 2,
              data: { anomaly: "killed_by_signal", exit_code: 137, signal: 9 },
            },
          ],
        })}
      />,
    )

    const marks = screen.getByRole("img", { name: /1 anomaly/i })
    expect(within(marks).getByText(/turn 2 · killed · exit 137/i)).toBeInTheDocument()
    // A run with nothing to mark gets no strip at all rather than an empty one.
    cleanup()
    render(<TurnSparkline doc={doc()} />)
    expect(screen.queryByRole("img", { name: /anomaly/i })).not.toBeInTheDocument()
  })

  it("draws a series whose peak is zero as a flat baseline, never as NaN", () => {
    // A run states real zeroes: a cached turn billed nothing, a turn whose two
    // stamps fall in the same millisecond. Scaling a column by a peak of 0 is
    // 0/0, and the NaN it produced went straight into the rect's height and y —
    // geometry no browser draws, so the whole plot vanished silently.
    const { container } = render(
      <TurnSparkline
        doc={doc({
          turns: [
            turn(1, {
              tokens: { input: 0, output: 0 },
              t0: "2026-08-14T11:28:36.000Z",
              t1: "2026-08-14T11:28:36.000Z",
            }),
            turn(2, {
              tokens: { input: 0, output: 0 },
              t0: "2026-08-14T11:29:00.000Z",
              t1: "2026-08-14T11:29:00.000Z",
            }),
          ],
        })}
      />,
    )

    const rects = [...container.querySelectorAll("rect")]
    expect(rects).toHaveLength(4) // two columns, two series
    for (const rect of rects) {
      expect(Number(rect.getAttribute("height"))).toBeGreaterThan(0)
      expect(Number(rect.getAttribute("y"))).toBeGreaterThanOrEqual(0)
    }
    // And a stated zero still reads as a zero, not as an absent bill.
    expect(screen.getByText(/turn 1 · 0 tokens/i)).toBeInTheDocument()
    expect(screen.queryByText(/no tokens stated/i)).not.toBeInTheDocument()
  })

  it("draws nothing at all before the first turn is on the record", () => {
    const { container } = render(<TurnSparkline doc={doc({ turns: [] })} />)
    expect(container).toBeEmptyDOMElement()
  })
})
