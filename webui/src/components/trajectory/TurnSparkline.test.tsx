import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
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
    expect(screen.getByRole("img", { name: /model-response tokens per turn/i })).toBeInTheDocument()
    expect(screen.getByRole("img", { name: /action duration per turn/i })).toBeInTheDocument()
    expect(screen.getByText("4.2k")).toBeInTheDocument()
    expect(screen.getByText("13.5s")).toBeInTheDocument()
    expect(screen.getAllByText(/Max at Turn 1/i)).toHaveLength(2)
    expect(screen.getByText(/Gray ticks mean no value is attributed/i)).toBeInTheDocument()
  })

  it("gives every column an SVG title, so a hover names the turn it is about", () => {
    // `<title>` inside the mark is the hover layer an SVG chart gets natively,
    // and it carries the EXACT value the axis label had to round.
    render(<TurnSparkline doc={doc()} />)

    expect(screen.getByText(/turn 1 · 4,205 tokens/i)).toBeInTheDocument()
    expect(screen.getByText(/turn 2 · 840 tokens/i)).toBeInTheDocument()
    expect(screen.getByText(/turn 1 · 13.5s/i)).toBeInTheDocument()
  })

  it("answers for the column being pointed at, and returns to the peak on the way out", () => {
    // The readout already had the shape a hover wants — a value and the turn it
    // belongs to — so pointing at a column answers there. A 400ms turn is drawn
    // 1.5px tall, which is why the target is the column and not the bar.
    const { container } = render(<TurnSparkline doc={doc()} />)
    expect(screen.getAllByText(/Max at Turn 1/i)).toHaveLength(2)

    fireEvent.mouseEnter(container.querySelectorAll('rect[data-hit="2"]')[0])
    expect(screen.getByText("840 tokens")).toBeInTheDocument()
    expect(screen.queryByText(/Max at Turn 1/i)).not.toBeInTheDocument()

    fireEvent.mouseLeave(container.querySelector(".overflow-x-auto") as Element)
    expect(screen.getAllByText(/Max at Turn 1/i)).toHaveLength(2)
    expect(screen.queryByText("840 tokens")).not.toBeInTheDocument()
  })

  it("names the same turn in both charts, because they are drawn on one set of columns", () => {
    const { container } = render(<TurnSparkline doc={doc()} />)

    // Pointing at turn 2 in the tokens chart.
    fireEvent.mouseEnter(container.querySelectorAll('rect[data-hit="2"]')[0])

    // Each chart keeps its own number beside its own plot, and both are turn
    // 2's: its bill, and its wall time.
    expect(screen.getByText("840 tokens")).toBeInTheDocument()
    expect(screen.getByText("400ms")).toBeInTheDocument()
  })

  it("says what the pointed-at turn actually did", () => {
    // The two readouts carry one number each. What the turn ran, and how it came
    // out, is the question a reader is really asking of a tall bar.
    const { container } = render(
      <TurnSparkline
        doc={doc({
          turns: [
            turn(1, { tokens: { input: 10, output: 1 } }),
            turn(2, {
              call: { tool: "build", summary: "verify mvn clean verify" },
              observation: { outcome: "failed", summary: "exit 1 · MAVEN_VERSION_ERROR" },
              gate: { word: "partial" },
              tokens: { input: 800, output: 40 },
              t0: "2026-08-14T11:29:00.000Z",
              t1: "2026-08-14T11:29:00.400Z",
            }),
          ],
        })}
      />,
    )

    fireEvent.mouseEnter(container.querySelectorAll('rect[data-hit="2"]')[0])
    const panel = screen.getByText("Turn 2").parentElement?.parentElement as HTMLElement
    expect(within(panel).getByText(/verify mvn clean verify/)).toBeInTheDocument()
    expect(within(panel).getByText("failed")).toBeInTheDocument()
    expect(within(panel).getByText(/exit 1 · MAVEN_VERSION_ERROR/)).toBeInTheDocument()
    expect(within(panel).getByText("gate: partial")).toBeInTheDocument()
    expect(within(panel).getByText("840 tokens · 400ms")).toBeInTheDocument()

    // And it goes away with the pointer.
    fireEvent.mouseLeave(container.querySelector(".overflow-x-auto") as Element)
    expect(screen.queryByText("gate: partial")).not.toBeInTheDocument()
  })

  it("says a pointed-at column stated nothing rather than showing a number for it", () => {
    const { container } = render(
      <TurnSparkline doc={doc({ turns: [turn(1, { tokens: { input: 10, output: 1 } }), turn(2)] })} />,
    )

    fireEvent.mouseEnter(container.querySelectorAll('rect[data-hit="2"]')[0])
    // Case-sensitive on purpose: the readout says "Turn 2", the column's own
    // `<title>` says "turn 2", and both are on the page.
    expect(screen.getByText("Turn 2 · no tokens stated")).toBeInTheDocument()
  })

  it("draws an unstated bill as an absence, not as a free turn", () => {
    // The token ledger is exported at loop exit, so the turns of a live run are
    // routinely unbilled. A zero-height bar is indistinguishable from a turn
    // that cost nothing, and this run's most interesting turn is the open one.
    render(<TurnSparkline doc={doc({ turns: [turn(1), turn(2, { tokens: null })] })} />)

    const tokens = screen.getByRole("img", { name: /model-response tokens per turn/i })
    expect(within(tokens).getAllByText(/no tokens stated/i)).toHaveLength(2)
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

    // The bars only: each column also draws a transparent full-height target a
    // reader can point at, and its geometry is a constant rather than a scale.
    const bars = [...container.querySelectorAll("rect[data-bar]")]
    expect(bars).toHaveLength(4) // two columns, two series
    for (const rect of bars) {
      expect(Number(rect.getAttribute("height"))).toBeGreaterThan(0)
      expect(Number(rect.getAttribute("y"))).toBeGreaterThanOrEqual(0)
    }
    // Every column is reachable: a bar this short is 1.5px tall and could not
    // be hovered on its own.
    expect(container.querySelectorAll("rect[data-hit]")).toHaveLength(4)
    // And a stated zero still reads as a zero, not as an absent bill.
    expect(screen.getByText(/turn 1 · 0 tokens/i)).toBeInTheDocument()
    expect(screen.queryByText(/no tokens stated/i)).not.toBeInTheDocument()
  })

  it("draws the advisor's spend as a third strip on its own scale", () => {
    // A 60,420-token consult on the model's axis flattens every model bar to a
    // hairline — two measures, two scales, the same rule the duration strip
    // already follows. The strip is drawn only on runs that consulted anyone.
    render(
      <TurnSparkline
        doc={doc({
          turns: [
            turn(1, { tokens: { input: 8648, output: 199 } }),
            turn(2, {
              call: { tool: "advisor", summary: "consult" },
              advisor_tokens: { input: 60239, output: 181 },
            }),
          ],
        })}
      />,
    )

    const advisor = screen.getByRole("img", { name: /advisor tokens per consult/i })
    expect(within(advisor).getByText(/turn 2 · advisor 60,420 tokens/i)).toBeInTheDocument()
    // Its own peak, and the model strip keeps its own.
    expect(screen.getByText("60.4k")).toBeInTheDocument()
    expect(screen.getByText("8.8k")).toBeInTheDocument()
    // A turn that consulted nobody states nothing here, and says so.
    expect(within(advisor).getByText(/turn 1 · no advisor tokens stated/i)).toBeInTheDocument()
  })

  it("leaves the third strip off a run that consulted nobody", () => {
    render(<TurnSparkline doc={doc()} />)
    expect(screen.queryByRole("img", { name: /advisor tokens per consult/i })).not.toBeInTheDocument()
  })

  it("says what the advisor cost on the turn being pointed at", () => {
    const { container } = render(
      <TurnSparkline
        doc={doc({
          turns: [
            turn(1, { tokens: { input: 10, output: 1 } }),
            turn(2, {
              call: { tool: "advisor", summary: "consult" },
              advisor_tokens: { input: 8645, output: 147 },
              t0: "2026-08-14T11:29:00.000Z",
              t1: "2026-08-14T11:29:00.400Z",
            }),
          ],
        })}
      />,
    )

    fireEvent.mouseEnter(container.querySelectorAll('rect[data-hit="2"]')[0])
    const panel = screen.getByText("Turn 2").parentElement?.parentElement as HTMLElement
    // Its own line, under its own name: the turn's own response and the advice
    // it paid for are two bills, and one sum would report neither.
    // The strip's own readout and the panel say it the same way.
    expect(within(panel).getByText("advisor 8,792 tokens")).toBeInTheDocument()
    expect(screen.getAllByText("advisor 8,792 tokens")).toHaveLength(2)
    expect(within(panel).getByText("no tokens stated · 400ms")).toBeInTheDocument()

    // A turn that consulted nobody says nothing about the advisor in its panel.
    // Scoped to the panel: the advisor strip still draws a column for turn 1,
    // and that column's own title is about the strip, not about this turn.
    fireEvent.mouseEnter(container.querySelectorAll('rect[data-hit="1"]')[0])
    const quiet = container.querySelector(".bg-popover") as HTMLElement
    expect(within(quiet).getByText("Turn 1")).toBeInTheDocument()
    expect(within(quiet).queryByText(/advisor/)).not.toBeInTheDocument()
  })

  it("draws nothing at all before the first turn is on the record", () => {
    const { container } = render(<TurnSparkline doc={doc({ turns: [] })} />)
    expect(container).toBeEmptyDOMElement()
  })
})
