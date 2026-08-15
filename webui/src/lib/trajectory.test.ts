import { describe, expect, it } from "vitest"

import type {
  TrajectoryAnnotation,
  TrajectoryDocument,
  TrajectoryTurn,
  TrajectoryWarning,
} from "@/api/types"

import {
  anomalies,
  bandTurns,
  formatSeq,
  gateChain,
  latestControlSeq,
  mergeTrajectory,
  recurrenceCount,
  rowsByTurn,
  seriesPeak,
  sparkColumns,
  truncationMarker,
  turnDurationMs,
  unresolvedRefs,
} from "./trajectory"

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
    session: { run_id: "r" },
    phases: [],
    turns: [],
    annotations: [],
    warnings: [],
    outputs: null,
    ...over,
  }
}

describe("mergeTrajectory", () => {
  it("upserts turns by id and keeps them in turn order", () => {
    const held = doc({ turns: [turn(1), turn(2)] })
    const polled = doc({ turns: [turn(3), turn(4)] })

    const merged = mergeTrajectory(held, polled)

    expect(merged.turns.map((t) => t.turn_id)).toEqual([1, 2, 3, 4])
  })

  it("replaces a turn the poll restated rather than doubling it", () => {
    const held = doc({ turns: [turn(1, { tokens: null })] })
    const polled = doc({ turns: [turn(1, { tokens: { input: 10, output: 2 } })] })

    const merged = mergeTrajectory(held, polled)

    expect(merged.turns).toHaveLength(1)
    expect(merged.turns[0].tokens).toEqual({ input: 10, output: 2 })
  })

  it("replaces warnings wholesale, so a filled hole is withdrawn", () => {
    const held = doc({
      turns: [turn(1)],
      warnings: [{ code: "missing_tool_result", detail: "turn 1 has no result", turn_id: 1 }],
    })
    const polled = doc({ turns: [turn(2)], warnings: [] })

    const merged = mergeTrajectory(held, polled)

    expect(merged.warnings).toEqual([])
  })

  it("replaces annotations, phases and session wholesale", () => {
    const held = doc({
      annotations: [{ kind: "forced", turn_id: 1, data: {} }],
      phases: [{ name: "build", termination: null, gates: [] }],
      session: { run_id: "r", verdict: null },
    })
    const polled = doc({
      annotations: [{ kind: "recurrence", turn_id: 2, data: { recurrence_count: 3 } }],
      phases: [{ name: "build", termination: "advance", gates: [] }],
      session: { run_id: "r", verdict: "partial" },
    })

    const merged = mergeTrajectory(held, polled)

    expect(merged.annotations).toEqual(polled.annotations)
    expect(merged.phases[0].termination).toBe("advance")
    expect(merged.session.verdict).toBe("partial")
  })

  it("merges outputs, because refs are content-addressed and shared", () => {
    const held = doc({ outputs: { output_a: "aaa" } })
    const polled = doc({ outputs: { output_b: "bbb" } })

    expect(mergeTrajectory(held, polled).outputs).toEqual({ output_a: "aaa", output_b: "bbb" })
  })

  it("keeps the bytes it holds when a summary poll carries none", () => {
    const held = doc({ outputs: { output_a: "aaa" } })
    const polled = doc({ outputs: null })

    expect(mergeTrajectory(held, polled).outputs).toEqual({ output_a: "aaa" })
  })

  it("takes the whole incoming document when nothing is held yet", () => {
    const polled = doc({ turns: [turn(7)] })
    expect(mergeTrajectory(null, polled)).toEqual(polled)
  })
})

describe("latestControlSeq", () => {
  it("is the last ledger line folded into any turn the document holds", () => {
    const held = doc({
      turns: [turn(1, { control_seq: [3, 4, 6] }), turn(2, { control_seq: [7] })],
    })
    expect(latestControlSeq(held)).toBe(7)
  })

  it("is the newest line even when it landed on an older turn", () => {
    // ignite's forced dispatch: the turn closed at seq 239 and the run's close
    // folded seq 240 onto it afterwards, while no newer turn ever opened.
    const held = doc({
      turns: [turn(1, { control_seq: [235, 238, 239, 240] }), turn(2, { control_seq: [236] })],
    })
    expect(latestControlSeq(held)).toBe(240)
  })

  it("ignores a turn that names no sequence — the endpoint restates those anyway", () => {
    expect(latestControlSeq(doc({ turns: [turn(1, { control_seq: [] })] }))).toBeNull()
  })

  it("is null before any turn — a poller must not cut at zero by accident", () => {
    expect(latestControlSeq(doc())).toBeNull()
  })
})

describe("bandTurns", () => {
  const banded = doc({
    phases: [
      { name: "provision", termination: "advance", gates: [] },
      { name: "build", termination: "evidence_close", gates: [{ word: "partial" }] },
    ],
    turns: [
      turn(1, { phase: "provision" }),
      turn(2, { phase: "build" }),
      turn(3, { phase: "build" }),
      turn(4, { phase: "provision" }),
    ],
  })

  it("bands consecutive turns of one phase together", () => {
    const bands = bandTurns(banded)
    expect(bands.map((b) => b.name)).toEqual(["provision", "build", "provision"])
    expect(bands[1].turns.map((t) => t.turn_id)).toEqual([2, 3])
  })

  it("numbers a re-entered phase instead of pretending it is the same band", () => {
    const bands = bandTurns(banded)
    expect(bands[0].ordinal).toBe(1)
    expect(bands[2].ordinal).toBe(2)
    expect(bands[0].key).not.toBe(bands[2].key)
  })

  it("carries the termination onto the LAST band of that phase only", () => {
    const bands = bandTurns(banded)
    expect(bands[0].termination).toBeNull()
    expect(bands[2].termination).toBe("advance")
    expect(bands[1].termination).toBe("evidence_close")
  })

  it("bands a phase the document never listed, rather than dropping its turns", () => {
    const bands = bandTurns(doc({ turns: [turn(1, { phase: "ghost" })] }))
    expect(bands).toHaveLength(1)
    expect(bands[0].name).toBe("ghost")
    expect(bands[0].termination).toBeNull()
  })
})

describe("gateChain", () => {
  const phases = [
    {
      name: "test",
      termination: null,
      gates: [
        { word: "failed", decision_id: "g1", supersedes: null },
        { word: "partial", decision_id: "g2", supersedes: "g1" },
        { word: "success", decision_id: "g3", supersedes: "g2" },
      ],
    },
  ]

  it("walks the words a grading replaced, newest first", () => {
    const chain = gateChain({ word: "success", decision_id: "g3", supersedes: "g2" }, phases)
    expect(chain.map((g) => g.word)).toEqual(["success", "partial", "failed"])
  })

  it("is the word alone when it superseded nothing", () => {
    const chain = gateChain({ word: "failed", decision_id: "g1", supersedes: null }, phases)
    expect(chain.map((g) => g.word)).toEqual(["failed"])
  })

  it("stops at a link no phase band holds instead of inventing one", () => {
    const chain = gateChain({ word: "success", decision_id: "gX", supersedes: "gone" }, phases)
    expect(chain.map((g) => g.word)).toEqual(["success"])
  })

  it("terminates on a cycle rather than walking forever", () => {
    const looped = [
      {
        name: "test",
        termination: null,
        gates: [
          { word: "a", decision_id: "ga", supersedes: "gb" },
          { word: "b", decision_id: "gb", supersedes: "ga" },
        ],
      },
    ]
    const chain = gateChain({ word: "a", decision_id: "ga", supersedes: "gb" }, looped)
    expect(chain.map((g) => g.word)).toEqual(["a", "b"])
  })

  it("is empty for a turn with no gate", () => {
    expect(gateChain(null, phases)).toEqual([])
  })
})

describe("rowsByTurn", () => {
  it("indexes annotations and warnings by the turn they are about", () => {
    const annotations: TrajectoryAnnotation[] = [
      { kind: "forced", turn_id: 2, data: {} },
      { kind: "recurrence", turn_id: 2, data: { recurrence_count: 3 } },
    ]
    const index = rowsByTurn(annotations)
    expect(index.get(2)).toHaveLength(2)
    expect(index.get(1)).toBeUndefined()
  })

  it("drops rows that name no turn — a run-wide statement is nobody's row", () => {
    const warnings: TrajectoryWarning[] = [
      { code: "missing_control_events", detail: "no ledger yet" },
    ]
    expect(rowsByTurn(warnings).size).toBe(0)
  })
})

describe("recurrenceCount", () => {
  it("is the highest count the recurrence annotations state", () => {
    expect(
      recurrenceCount([
        { kind: "recurrence", turn_id: 1, data: { recurrence_count: 2 } },
        { kind: "recurrence", turn_id: 1, data: { recurrence_count: 5 } },
      ]),
    ).toBe(5)
  })

  it("is null when nothing repeated", () => {
    expect(recurrenceCount([{ kind: "forced", turn_id: 1, data: {} }])).toBeNull()
  })
})

describe("truncationMarker", () => {
  it("reads the count of components a cut window could not name", () => {
    expect(truncationMarker("window_truncated:12")).toBe(12)
  })

  it("is null for a ref that names bytes", () => {
    expect(truncationMarker("output_6163859b019d")).toBeNull()
  })
})

describe("unresolvedRefs", () => {
  const rich = turn(1, {
    window_ref: "output_win",
    window_components: ["window_truncated:4", "output_sys", "output_obs"],
    call: { tool: "project", params_ref: "envelope-000003" },
    observation: { ref: "output_obs", evidence_ref: "output_ev" },
  })

  it("names every ref the held bytes cannot answer", () => {
    expect(unresolvedRefs(rich, { output_sys: "x" })).toEqual([
      "output_win",
      "output_obs",
      "output_ev",
    ])
  })

  it("never counts the cut marker — nothing resolves a cut", () => {
    expect(unresolvedRefs(rich, null)).not.toContain("window_truncated:4")
  })

  it("never counts the envelope id — the output store answers no envelope", () => {
    expect(unresolvedRefs(rich, null)).not.toContain("envelope-000003")
  })

  it("is empty when every ref is held", () => {
    expect(
      unresolvedRefs(rich, {
        output_win: "a",
        output_sys: "b",
        output_obs: "c",
        output_ev: "d",
      }),
    ).toEqual([])
  })
})

describe("formatSeq and turnDurationMs", () => {
  it("joins control sequence numbers so a row can be descended", () => {
    expect(formatSeq([11, 12, 13])).toBe("11·12·13")
  })

  it("says so when a turn folded no sequence at all", () => {
    expect(formatSeq([])).toBe("—")
  })

  it("measures a turn from its own timestamps", () => {
    expect(
      turnDurationMs(turn(1, { t0: "2026-08-14T11:28:36.000Z", t1: "2026-08-14T11:28:49.500Z" })),
    ).toBe(13500)
  })

  it("is null when either end is missing or unparseable", () => {
    expect(turnDurationMs(turn(1, { t0: "2026-08-14T11:28:36.000Z" }))).toBeNull()
    expect(turnDurationMs(turn(1, { t0: "nope", t1: "also nope" }))).toBeNull()
  })
})

describe("anomalies", () => {
  const mark = (data: Record<string, unknown>): TrajectoryAnnotation => ({
    kind: "conflict",
    turn_id: 1,
    data,
  })

  it("phrases a kill from the fields the reducer stated", () => {
    const [killed] = anomalies([
      mark({ anomaly: "killed_by_signal", exit_code: 137, signal: 9, stated_by: "tool_result" }),
    ])

    expect(killed.label).toBe("killed · exit 137")
    expect(killed.detail).toContain("it was killed by signal 9")
    expect(killed.detail).toContain("stated by tool_result")
  })

  it("phrases a job that never reported its end, naming the job", () => {
    const [job] = anomalies([
      mark({
        anomaly: "job_never_settled",
        job_id: "2c4d56b2fdca",
        close_reason: "deadline",
        stated_by: "job_live_at_close",
      }),
    ])

    expect(job.label).toBe("job never settled")
    expect(job.detail).toContain("2c4d56b2fdca")
    expect(job.detail).toContain("deadline")
  })

  it("draws a mark this build has never heard of under its own name", () => {
    // A reducer that learns to state something new must not be swallowed by an
    // older view that only knows two words.
    expect(anomalies([mark({ anomaly: "clock_went_backwards" })])[0]).toMatchObject({
      kind: "clock_went_backwards",
      label: "clock_went_backwards",
    })
  })

  it("ignores annotations that are not marks", () => {
    expect(anomalies([{ kind: "recurrence", turn_id: 1, data: { recurrence_count: 3 } }])).toEqual(
      [],
    )
  })
})

describe("sparkColumns and seriesPeak", () => {
  const billed = turn(1, {
    tokens: { input: 4134, output: 71 },
    t0: "2026-08-14T11:28:36.000Z",
    t1: "2026-08-14T11:28:49.500Z",
  })
  const unbilled = turn(2, { t0: "2026-08-14T11:29:00.000Z" })

  it("carries tokens and duration per turn, in turn order", () => {
    const columns = sparkColumns(doc({ turns: [unbilled, billed] }))

    expect(columns.map((c) => c.turnId)).toEqual([1, 2])
    expect(columns[0]).toMatchObject({ tokens: 4205, durationMs: 13500 })
  })

  it("carries an unstated bill as null, never as zero", () => {
    // The token ledger is exported at loop exit, so a live run's last turns are
    // routinely unbilled. A zero-height bar would read as a free turn.
    const [, open] = sparkColumns(doc({ turns: [billed, unbilled] }))
    expect(open.tokens).toBeNull()
    expect(open.durationMs).toBeNull()
  })

  it("hands each column the marks the reducer drew on its turn", () => {
    const columns = sparkColumns(
      doc({
        turns: [billed, unbilled],
        annotations: [{ kind: "conflict", turn_id: 2, data: { anomaly: "killed_by_signal" } }],
      }),
    )

    expect(columns[0].anomalies).toEqual([])
    expect(columns[1].anomalies[0].kind).toBe("killed_by_signal")
  })

  it("peaks over the values a series states, and is null when none does", () => {
    const columns = sparkColumns(doc({ turns: [billed, unbilled] }))

    expect(seriesPeak(columns, (c) => c.tokens)).toBe(4205)
    expect(seriesPeak([], (c) => c.tokens)).toBeNull()
    expect(seriesPeak(sparkColumns(doc({ turns: [unbilled] })), (c) => c.tokens)).toBeNull()
  })
})
