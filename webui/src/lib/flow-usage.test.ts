import { describe, expect, it } from "vitest"
import type { ContextTrace, TrajectoryDocument, TrajectoryTurn } from "@/api/types"
import { actionTurn, flowTurns, iterationUsage, type FlowIteration } from "./flow-usage"

const entry = (): FlowIteration => ({ iteration: 6, sequence: 1, thoughts: [], actions: [] })
const turn = (id: number, over: Partial<TrajectoryTurn> = {}): TrajectoryTurn => ({ turn_id: id, phase: "build", iteration: 6, actor: "model", control_seq: [], ...over })

describe("Flow usage attribution", () => {
  it("keeps a phase-entry advisor separate from the actor that just ended the previous phase", () => {
    const a = entry(), b = entry()
    const context: ContextTrace = { trunk: { goal: "g", state: "done", progress: {}, summary: "" }, debug: {}, phases: ["build", "test"].map((name, i) => ({ id: name, name, title: name, status: "completed", refs: [], progress: {}, tasks: [{ id: name, title: name, status: "completed", iterations: [i ? b : a] }] })) }
    const turns = [turn(1, { tokens: { input: 100, output: 20 } }), turn(2, { phase: "test", actor: "controller", call: { tool: "advisor" }, advisor_tokens: { input: 50, output: 8 } })]
    const usage = flowTurns(context, { turns } as TrajectoryDocument)
    expect(iterationUsage(usage.get(a)!)).toMatchObject({ actor: { input: 100, output: 20 }, advisor: null })
    expect(iterationUsage(usage.get(b)!)).toMatchObject({ actor: null, advisor: { input: 50, output: 8 } })
    context.phases[0].tasks[0].iterations.push(entry())
    expect(flowTurns(context, { turns } as TrajectoryDocument).has(a)).toBe(false)
  })

  it("counts a multi-tool response once and takes the union of concurrent action intervals", () => {
    const a = turn(1, { tokens: { input: 100, output: 20 }, t0: "2026-10-21T12:00:00Z", t1: "2026-10-21T12:00:03Z" })
    const b = turn(2, { t0: "2026-10-21T12:00:01Z", t1: "2026-10-21T12:00:05Z" })
    expect(iterationUsage([a, b])).toMatchObject({ actor: { input: 100, output: 20 }, durationMs: 5000, partialTime: false })
    expect(iterationUsage([a, turn(3)])).toMatchObject({ durationMs: 3000, partialTime: true })
    expect(iterationUsage([turn(1)])).toMatchObject({ actor: null, durationMs: null })
  })

  it("binds repeated tools by their output reference, never by list position", () => {
    const a = { toolName: "bash", output: "", observation: "", refs: [{ ref: "output_b", label: "out" }] }
    const turns = [turn(1, { call: { tool: "bash" }, observation: { evidence_ref: "output_a" } }), turn(2, { call: { tool: "bash" }, observation: { evidence_ref: "output_b" } })]
    expect(actionTurn(a, turns)?.turn_id).toBe(2)
    expect(actionTurn({ ...a, refs: [] }, turns)).toBeUndefined()
  })
})
