import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { TrajectoryDocument } from "@/api/types"
import reduced from "@/test/fixtures/empty-segment/trajectory.json"

import { TrajectoryTimeline } from "./TrajectoryTimeline"

const doc: TrajectoryDocument = {
  schema_version: 1,
  session: { run_id: "run-1", project: "apache/kafka", verdict: "partial" },
  phases: [
    { name: "build", termination: "advance", gates: [] },
    {
      name: "test",
      termination: "evidence_close",
      gates: [
        { word: "failed", decision_id: "g1", supersedes: null },
        { word: "partial", decision_id: "g2", supersedes: "g1" },
      ],
    },
  ],
  turns: [
    {
      turn_id: 1,
      phase: "build",
      iteration: 4,
      actor: "model",
      window_ref: "output_win1",
      window_components: ["window_truncated:12", "output_sys", "output_obs1"],
      call: { tool: "project", params_ref: "envelope-000003" },
      observation: {
        ref: "output_obs1",
        evidence_ref: "output_ev1",
        error_code: "ENV_EXECUTABLE_NOT_FOUND",
        failure_signature: "ENV_EXECUTABLE_NOT_FOUND:836b0507d4ef8e5a",
      },
      gate: null,
      tokens: { input: 4134, output: 71 },
      t0: "2026-08-14T11:28:36.000Z",
      t1: "2026-08-14T11:28:49.500Z",
      control_seq: [11, 12, 13],
    },
    {
      turn_id: 2,
      phase: "test",
      iteration: 5,
      actor: "controller",
      window_ref: null,
      window_components: null,
      call: { tool: "phase", params_ref: null },
      observation: { ref: null, error_code: null, failure_signature: null },
      gate: { word: "partial", decision_id: "g2", supersedes: "g1" },
      tokens: null,
      t0: null,
      t1: null,
      control_seq: [24],
    },
  ],
  annotations: [
    { kind: "recurrence", turn_id: 1, data: { recurrence_count: 3 } },
    { kind: "forced", turn_id: 2, data: { policy: "phase_floor" } },
    {
      kind: "conflict",
      turn_id: 1,
      data: { anomaly: "killed_by_signal", exit_code: 137, signal: 9, stated_by: "tool_result" },
    },
  ],
  warnings: [
    { code: "missing_loop_decision", detail: "turn 2 called 'phase' and emitted no loop_decision", control_seq: 24, turn_id: 2 },
  ],
  outputs: null,
}

const withBytes: TrajectoryDocument = {
  ...doc,
  outputs: {
    output_win1: "SYSTEM PROMPT\nHISTORY",
    output_sys: "you are a setup agent",
    output_obs1: "bash: mvn: command not found",
    output_ev1: "exit status 127",
  },
}

describe("TrajectoryTimeline", () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it("draws one row per turn, each naming its control sequence", () => {
    render(<TrajectoryTimeline doc={doc} />)

    expect(screen.getByRole("button", { name: /^Turn 1/ })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /^Turn 2/ })).toBeInTheDocument()
    expect(screen.getByText("11·12·13")).toBeInTheDocument()
    expect(screen.getByText("24")).toBeInTheDocument()
  })

  it("bands the run by phase, naming how each band ended", () => {
    render(<TrajectoryTimeline doc={doc} />)

    const band = screen.getByRole("group", { name: /phase build/i })
    expect(within(band).getByText("advance")).toBeInTheDocument()
    expect(screen.getByRole("group", { name: /phase test/i })).toBeInTheDocument()
  })

  it("draws the reducer's own banding, segments no turn carries included", () => {
    // End to end over `sag trajectory`'s answer for the ledger committed at
    // `src/test/fixtures/empty-segment/`: the reducer states four segments —
    // provision, an EMPTY build banded by an orphan gate revision, analyze, and
    // the build the run actually worked in — and three bands' worth of turns.
    // The build band must show how the visit its turns are in ended.
    render(<TrajectoryTimeline doc={reduced as unknown as TrajectoryDocument} />)

    // A band's own disclosure is a <details>, which also carries role="group";
    // only the labelled ones are bands.
    const bands = screen
      .getAllByRole("group")
      .filter((band) => band.getAttribute("aria-label")?.startsWith("Phase "))
    expect(bands.map((band) => band.getAttribute("aria-label"))).toEqual([
      "Phase provision",
      "Phase analyze",
      "Phase build",
    ])

    const build = screen.getByRole("group", { name: /phase build/i })
    expect(within(build).getByText("evidence_close")).toBeInTheDocument()
    expect(within(build).getByText("1 gate")).toBeInTheDocument()
    expect(within(build).getByText("2 turns")).toBeInTheDocument()
    // No band claims a re-entry: the run entered `build` once with turns in it.
    expect(screen.queryByText(/re-entry/i)).not.toBeInTheDocument()
  })

  it("styles a controller turn differently from a model turn", () => {
    render(<TrajectoryTimeline doc={doc} />)

    const model = screen.getByTestId("turn-row-1")
    const controller = screen.getByTestId("turn-row-2")

    expect(model).toHaveAttribute("data-actor", "model")
    expect(controller).toHaveAttribute("data-actor", "controller")
    expect(model.className).not.toBe(controller.className)
    // The row says who acted in the words the terminal uses, not the ledger's.
    expect(within(controller).getByText("engine")).toBeInTheDocument()
    expect(within(controller).queryByText("controller")).not.toBeInTheDocument()
  })

  it("badges the error code, the failure signature and the recurrence count", () => {
    render(<TrajectoryTimeline doc={doc} />)

    const row = screen.getByTestId("turn-row-1")
    expect(within(row).getByText("ENV_EXECUTABLE_NOT_FOUND")).toBeInTheDocument()
    expect(within(row).getByText(/836b0507d4ef8e5a/)).toBeInTheDocument()
    expect(within(row).getByText("×3")).toBeInTheDocument()
  })

  it("badges an anomaly the reducer marked, saying what the ledger said", () => {
    render(<TrajectoryTimeline doc={doc} />)

    const row = screen.getByTestId("turn-row-1")
    const mark = within(row).getByText("killed · exit 137")
    expect(mark).toBeInTheDocument()
    expect(mark).toHaveAttribute("title", expect.stringContaining("killed by signal 9"))
    // A mark is a fact about the turn, not a hole in the ledger: it is never
    // drawn where the warnings are.
    expect(within(row).queryByText("conflict")).not.toBeInTheDocument()
  })

  it("badges the gate word and expands the chain it superseded", () => {
    render(<TrajectoryTimeline doc={doc} />)

    const gate = screen.getByRole("button", { name: /gate partial/i })
    expect(gate).toHaveAttribute("aria-expanded", "false")

    fireEvent.click(gate)

    expect(gate).toHaveAttribute("aria-expanded", "true")
    const chain = screen.getByRole("list", { name: /supersedes chain/i })
    expect(within(chain).getByText(/failed/)).toBeInTheDocument()
    expect(within(chain).getByText(/g1/)).toBeInTheDocument()
  })

  it("expands a row into the quad and asks for the bytes once", () => {
    const onNeedBytes = vi.fn()
    render(<TrajectoryTimeline doc={doc} onNeedBytes={onNeedBytes} />)

    fireEvent.click(screen.getByRole("button", { name: /^Turn 1/ }))

    expect(screen.getByRole("heading", { name: /model context/i })).toBeInTheDocument()
    expect(screen.getByRole("heading", { name: /tool call/i })).toBeInTheDocument()
    expect(screen.getByRole("heading", { name: /tool result/i })).toBeInTheDocument()
    expect(screen.queryByRole("heading", { name: /next/i })).not.toBeInTheDocument()
    expect(onNeedBytes).toHaveBeenCalledTimes(1)
  })

  it("lists every window component the record named, in render order", () => {
    render(<TrajectoryTimeline doc={withBytes} />)
    fireEvent.click(screen.getByRole("button", { name: /^Turn 1/ }))

    const window = screen.getByRole("list", { name: /model context messages/i })
    const items = within(window).getAllByRole("listitem")
    expect(items).toHaveLength(3)
    expect(items[1]).toHaveTextContent("output_sys")
  })

  it("descends a component ref to its bytes", () => {
    render(<TrajectoryTimeline doc={withBytes} />)
    fireEvent.click(screen.getByRole("button", { name: /^Turn 1/ }))

    expect(screen.queryByText("you are a setup agent")).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: /output_sys/ }))
    expect(screen.getByText("you are a setup agent")).toBeInTheDocument()
  })

  it("shows the stored role and a message preview before the opaque ref", () => {
    const messages = {
      ...withBytes,
      outputs: {
        ...withBytes.outputs,
        output_sys: JSON.stringify({ role: "system", content: "You are a setup agent." }),
      },
    }
    render(<TrajectoryTimeline doc={messages} />)
    fireEvent.click(screen.getByRole("button", { name: /^Turn 1/ }))

    const context = screen.getByTestId("quad-window-1")
    expect(within(context).getByText("system")).toBeInTheDocument()
    expect(within(context).getByText("You are a setup agent.")).toBeInTheDocument()
    expect(within(context).getByRole("button", { name: /output_sys/ })).toBeInTheDocument()
  })

  it("draws the window handle once inside the context list", () => {
    // The record's handle into [A] IS the last component it named — the newest
    // message, the one thing this turn did not share with the turn before it.
    // Drawing it above the list and again inside it put one ref on screen
    // twice and invited a reader to descend the same bytes from two places.
    const handled = {
      ...doc,
      turns: [
        {
          ...doc.turns[0],
          window_ref: "output_obs1",
          window_components: ["output_sys", "output_obs1"],
        },
        doc.turns[1],
      ],
      outputs: withBytes.outputs,
    }
    render(<TrajectoryTimeline doc={handled} />)
    fireEvent.click(screen.getByRole("button", { name: /^Turn 1/ }))

    const window = screen.getByTestId("quad-window-1")
    expect(within(window).getAllByRole("button", { name: /output_obs1/ })).toHaveLength(1)
    expect(within(window).getAllByRole("listitem")).toHaveLength(2)
  })

  it("still descends a handle the component list does not carry", () => {
    // A record may name a window whose components it could not state. The
    // handle is then the only way into [A], and it keeps its own descent.
    const handleOnly = {
      ...withBytes,
      turns: [
        { ...doc.turns[0], window_ref: "output_win1", window_components: null },
        doc.turns[1],
      ],
    }
    render(<TrajectoryTimeline doc={handleOnly} />)
    fireEvent.click(screen.getByRole("button", { name: /^Turn 1/ }))

    const window = screen.getByTestId("quad-window-1")
    expect(within(window).getByRole("button", { name: /output_win1/ })).toBeInTheDocument()
    expect(
      within(window).queryByRole("list", { name: /model context messages/i }),
    ).not.toBeInTheDocument()
  })

  it("declares a cut instead of drawing it as bytes nobody has", () => {
    render(<TrajectoryTimeline doc={withBytes} />)
    fireEvent.click(screen.getByRole("button", { name: /^Turn 1/ }))

    expect(screen.getByText(/12 older messages/i)).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /window_truncated/ })).not.toBeInTheDocument()
  })

  it("keeps the envelope id as secondary call metadata", () => {
    render(<TrajectoryTimeline doc={withBytes} />)
    fireEvent.click(screen.getByRole("button", { name: /^Turn 1/ }))

    const call = screen.getByTestId("quad-call-1")
    expect(within(call).getByText("envelope-000003")).toBeInTheDocument()
    expect(within(call).getByText(/call envelope/i)).toBeInTheDocument()
  })

  it("distinguishes the model-visible result from its evidence reference", () => {
    render(<TrajectoryTimeline doc={withBytes} />)
    fireEvent.click(screen.getByRole("button", { name: /^Turn 1/ }))

    const observation = screen.getByTestId("quad-observation-1")
    expect(within(observation).getByText(/model-visible result/i)).toBeInTheDocument()
    expect(within(observation).getByText("Tool output reference")).toBeInTheDocument()
    expect(within(observation).getByRole("button", { name: /output_ev1/ })).toBeInTheDocument()
  })

  it("labels a detached evidence ref as a background job", () => {
    const jobEvidence = {
      ...withBytes,
      turns: [
        {
          ...doc.turns[0],
          observation: { ...doc.turns[0].observation, evidence_ref: "job:58db946542a8" },
        },
        doc.turns[1],
      ],
    }
    render(<TrajectoryTimeline doc={jobEvidence} status="ready" />)
    fireEvent.click(screen.getByRole("button", { name: /^Turn 1/ }))

    const observation = screen.getByTestId("quad-observation-1")
    expect(within(observation).getByText("Background job")).toBeInTheDocument()
    fireEvent.click(within(observation).getByRole("button", { name: /job:58db946542a8/ }))
    expect(within(observation).getByText(/background-job handle/i)).toBeInTheDocument()
  })

  it("loads exact call parameters once when the row is expanded", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          sequence: 3,
          envelope_id: "envelope-000003",
          tool: "project",
          exact_params: { action: "clone", ref: "4.3.1" },
        }),
        { headers: { "Content-Type": "application/json" } },
      ),
    )
    render(<TrajectoryTimeline doc={withBytes} sessionId="S1" />)

    fireEvent.click(screen.getByRole("button", { name: /^Turn 1/ }))
    await waitFor(() => expect(screen.getByText(/"action": "clone"/)).toBeInTheDocument())
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/sessions/S1/trajectory/envelopes/envelope-000003",
    )

    fireEvent.click(screen.getByRole("button", { name: /^Turn 1/ }))
    fireEvent.click(screen.getByRole("button", { name: /^Turn 1/ }))
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("states a hole on the row it is about instead of hiding it", () => {
    render(<TrajectoryTimeline doc={doc} />)

    const row = screen.getByTestId("turn-row-2")
    expect(within(row).getByText("missing_loop_decision")).toBeInTheDocument()
  })

  it("shows what each phase's checks decided, and what it reported", () => {
    render(
      <TrajectoryTimeline
        doc={{
          ...doc,
          phases: [
            {
              name: "build",
              termination: "advance",
              gates: [{ word: "success" }],
              validator_state: "green",
              reason: "JVM build execution validated",
              key_results: "Executed mvn clean verify in /workspace/commons-cli",
            },
          ],
          turns: [doc.turns[0]],
          annotations: [],
          warnings: [],
        }}
      />,
    )

    const band = screen.getByRole("group", { name: /phase build/i })
    expect(within(band).getByText(/green/)).toBeInTheDocument()
    expect(within(band).getByText(/JVM build execution validated/)).toBeInTheDocument()

    // The phase's own report is one click away rather than filling the header.
    fireEvent.click(within(band).getByText("What this phase reported"))
    expect(
      within(band).getByText("Executed mvn clean verify in /workspace/commons-cli"),
    ).toBeInTheDocument()
  })

  it("states nothing about checks a phase segment did not record", () => {
    // `doc`'s own build segment carries no validator state, reason or report.
    render(<TrajectoryTimeline doc={doc} />)

    const band = screen.getByRole("group", { name: /phase build/i })
    expect(within(band).queryByText(/checks/i)).not.toBeInTheDocument()
    expect(within(band).queryByText("What this phase reported")).not.toBeInTheDocument()
  })

  it("says the run has no turns yet rather than drawing an empty frame", () => {
    render(<TrajectoryTimeline doc={{ ...doc, turns: [], annotations: [], warnings: [] }} />)
    expect(screen.getByText(/no turns/i)).toBeInTheDocument()
  })
})
