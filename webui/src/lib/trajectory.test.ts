import { describe, expect, it } from "vitest"

import type {
  TrajectoryAnnotation,
  TrajectoryDocument,
  TrajectoryTurn,
  TrajectoryWarning,
} from "@/api/types"

import reduced from "@/test/fixtures/empty-segment/trajectory.json"

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

  it("takes only the bytes from a response that lands behind the ledger held", () => {
    // The full tier is read outside the poll's in-flight discipline — a reader
    // expanding a row is waiting for those bytes — so a slow byte read can land
    // after a poll that already moved the document forward. Applying it whole
    // would restore the warning the newer state withdrew and put the turn back
    // the way it stood before its result arrived. The watermark orders them:
    // a response behind it contributes its bytes and nothing else.
    const held = doc({ turns: [turn(1, { control_seq: [1, 2, 3] })], warnings: [] })
    const late = doc({
      turns: [turn(1, { control_seq: [1] })],
      warnings: [{ code: "missing_tool_result", detail: "turn 1", turn_id: 1 }],
      outputs: { output_a: "bash: mvn: not found" },
    })

    const merged = mergeTrajectory(held, late)

    expect(merged.turns[0].control_seq).toEqual([1, 2, 3])
    expect(merged.warnings).toEqual([])
    expect(merged.outputs).toEqual({ output_a: "bash: mvn: not found" })
  })

  it("applies a forced reload whole, even from behind the watermark it holds", () => {
    // The watermark orders two reads the VIEW issued against each other. It was
    // never a rule about what the owner may ask for: "Reload whole" is a person
    // saying take the server's answer over whatever you are holding, and a
    // guard built for a byte-read race swallowed it whenever a poll had landed
    // while the reload was on the wire — one button, no effect, nothing said.
    const held = doc({
      turns: [turn(1, { control_seq: [1, 2, 3] })],
      warnings: [],
    })
    const behind = doc({
      turns: [turn(1, { control_seq: [1] })],
      warnings: [{ code: "missing_tool_result", detail: "turn 1", turn_id: 1 }],
    })

    const merged = mergeTrajectory(held, behind, { force: true })

    expect(merged.turns[0].control_seq).toEqual([1])
    expect(merged.warnings).toHaveLength(1)
  })

  it("forces nothing unless asked: a stale response still contributes bytes only", () => {
    const held = doc({ turns: [turn(1, { control_seq: [1, 2, 3] })], warnings: [] })
    const late = doc({
      turns: [turn(1, { control_seq: [1] })],
      warnings: [{ code: "missing_tool_result", detail: "turn 1", turn_id: 1 }],
      outputs: { output_a: "bash: mvn: not found" },
    })

    for (const options of [undefined, { force: false }]) {
      const merged = mergeTrajectory(held, late, options)
      expect(merged.turns[0].control_seq).toEqual([1, 2, 3])
      expect(merged.warnings).toEqual([])
      expect(merged.outputs).toEqual({ output_a: "bash: mvn: not found" })
    }
  })

  it("applies a response the watermark cannot order — a poll that saw no turn", () => {
    // A cut poll over a ledger that has not moved carries no turn at all, so it
    // names no sequence. That is not a stale response: it is the current whole
    // state of everything a cut does not remove, and it is how a warning is
    // withdrawn once its hole fills.
    const held = doc({
      turns: [turn(1, { control_seq: [1, 2, 3] })],
      warnings: [{ code: "missing_tool_result", detail: "turn 1", turn_id: 1 }],
    })
    const polled = doc({ turns: [], warnings: [] })

    const merged = mergeTrajectory(held, polled)

    expect(merged.warnings).toEqual([])
    expect(merged.turns[0].control_seq).toEqual([1, 2, 3])
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
  // The reducer states one `phases[]` entry per SEGMENT, not per phase name: a
  // phase the run re-enters is appended a second time, and each entry carries
  // the gates decided inside that stretch and the termination that ended it.
  // provision is entered twice here, and the second visit is still open.
  const banded = doc({
    phases: [
      {
        name: "provision",
        termination: "advance",
        gates: [{ word: "failed", decision_id: "g1", supersedes: null }],
      },
      { name: "build", termination: "evidence_close", gates: [] },
      {
        name: "provision",
        termination: null,
        gates: [{ word: "partial", decision_id: "g2", supersedes: "g1" }],
      },
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

  it("gives each band the termination of its OWN segment", () => {
    // Keying the metadata by phase NAME showed the last visit's ending on the
    // first band and left the visit that actually ended looking unfinished.
    const bands = bandTurns(banded)
    expect(bands[0].termination).toBe("advance")
    expect(bands[1].termination).toBe("evidence_close")
    expect(bands[2].termination).toBeNull() // the run is still in the re-entry
  })

  it("gives each band the gates decided inside it, not another visit's", () => {
    const bands = bandTurns(banded)
    expect(bands[0].gates.map((g) => g.word)).toEqual(["failed"])
    expect(bands[1].gates).toEqual([])
    expect(bands[2].gates.map((g) => g.word)).toEqual(["partial"])
  })

  it("bands a phase the document never listed, rather than dropping its turns", () => {
    const bands = bandTurns(doc({ turns: [turn(1, { phase: "ghost" })] }))
    expect(bands).toHaveLength(1)
    expect(bands[0].name).toBe("ghost")
    expect(bands[0].termination).toBeNull()
    expect(bands[0].gates).toEqual([])
  })

  it("passes over a segment no turn carries, on the reducer's own output", () => {
    // Not a hand-built shape: `test/fixtures/empty-segment/trajectory.json` is
    // what `sag trajectory` answers for the ledger committed beside it, where
    // an orphan gate revision bands `build` while the run is still in
    // `provision` — the reducer's own "a gate can band a phase no turn has
    // entered". The banding it states is
    //   0 provision(advance)  1 build(EMPTY)  2 analyze(advance)  3 build(evidence_close)
    // and the run's only `build` band is turns 4 and 5, which segment 3 ended.
    // Counting segments by name handed that band segment 1: no termination at
    // all, and a word decided for a visit that carried no turn.
    const document = reduced as unknown as TrajectoryDocument
    const bands = bandTurns(document)

    expect(bands.map((b) => b.name)).toEqual(["provision", "analyze", "build"])
    expect(bands.map((b) => b.turns.map((t) => t.turn_id))).toEqual([[1, 2], [3], [4, 5]])

    const build = bands[2]
    expect(build.termination).toBe("evidence_close")
    expect(build.gates.map((g) => g.word)).toEqual(["success"])
    // The empty segment's word belongs to no band on screen at all.
    expect(bands.flatMap((b) => b.gates.map((g) => g.decision_id))).not.toContain(
      "gate-build-0-revised",
    )
    // …and the bands before it are still their own segments, not shifted along.
    expect(bands[0].termination).toBe("advance")
    expect(bands[0].gates.map((g) => g.decision_id)).toEqual(["gate-provision-1"])
    expect(bands[1].termination).toBe("advance")
    expect(bands[1].gates).toEqual([])
  })

  it("gives a band the segment its turns are in, not the Nth of that name", () => {
    // The same shift, in the small: `test` is stated twice and carries turns
    // once, and the visit that carried them is the one a walk in document order
    // reaches after `analyze` — the earlier one belongs to no band at all.
    const bands = bandTurns(
      doc({
        phases: [
          { name: "build", termination: "advance", gates: [] },
          { name: "test", termination: "repair", gates: [] },
          { name: "analyze", termination: "advance", gates: [] },
          {
            name: "test",
            termination: "evidence_close",
            gates: [{ word: "partial", decision_id: "g9", supersedes: null }],
          },
        ],
        turns: [
          turn(1, { phase: "build" }),
          turn(2, { phase: "analyze" }),
          turn(3, { phase: "test" }),
        ],
      }),
    )

    expect(bands.map((b) => b.termination)).toEqual(["advance", "advance", "evidence_close"])
    expect(bands[2].gates.map((g) => g.word)).toEqual(["partial"])
  })

  it("states nothing for a band the document names no segment for", () => {
    // The turns show two visits and the document states one segment: whatever
    // the second band is, it is not the first visit's ending repeated.
    const bands = bandTurns(
      doc({
        phases: [{ name: "build", termination: "advance", gates: [] }],
        turns: [
          turn(1, { phase: "build" }),
          turn(2, { phase: "test" }),
          turn(3, { phase: "build" }),
        ],
      }),
    )
    expect(bands[0].termination).toBe("advance")
    expect(bands[2].termination).toBeNull()
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

  it("carries the advisor's own bill as its own measure, never inside the model's", () => {
    // A consult the run paid a second model for. Folding it into `tokens` would
    // put 60,420 tokens on a turn whose own response cost 8,648 — and would
    // flatten every other column against a peak nothing in that series reached.
    const consulted = turn(3, {
      call: { tool: "advisor" },
      tokens: { input: 8648, output: 199 },
      advisor_tokens: { input: 60239, output: 181 },
    })
    const columns = sparkColumns(doc({ turns: [billed, consulted] }))

    expect(columns.map((c) => c.tokens)).toEqual([4205, 8847])
    expect(columns.map((c) => c.advisorTokens)).toEqual([null, 60420])
    expect(seriesPeak(columns, (c) => c.advisorTokens)).toBe(60420)
    expect(seriesPeak(columns, (c) => c.tokens)).toBe(8847)
  })
})
