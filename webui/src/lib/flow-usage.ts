import type { ContextTrace, TrajectoryDocument, TrajectoryTokens, TrajectoryTurn } from "@/api/types"

export type FlowIteration = ContextTrace["phases"][number]["tasks"][number]["iterations"][number]
export type FlowAction = FlowIteration["actions"][number]

/** Iterations are scoped to a phase. A phase-entry advisor can reuse the last
 * actor iteration. Ambiguous context buckets never borrow each other's bills. */
export function flowTurns(context: ContextTrace | null | undefined, doc?: TrajectoryDocument | null) {
  const buckets = new Map<string, FlowIteration[]>()
  const result = new Map<FlowIteration, TrajectoryTurn[]>()
  for (const phase of context?.phases ?? []) {
    for (const task of phase.tasks) for (const entry of task.iterations) {
      if (entry.iteration == null) continue
      const key = JSON.stringify([phase.name, entry.iteration])
      buckets.set(key, [...(buckets.get(key) ?? []), entry])
    }
  }
  const seen = new Set<number>()
  for (const turn of doc?.turns ?? []) {
    if (seen.has(turn.turn_id) || turn.iteration == null) continue
    seen.add(turn.turn_id)
    const entries = buckets.get(JSON.stringify([turn.phase, turn.iteration]))
    if (entries?.length !== 1) continue
    result.set(entries[0], [...(result.get(entries[0]) ?? []), turn])
  }
  return result
}

export function actionTurn(action: FlowAction, turns: TrajectoryTurn[]): TrajectoryTurn | undefined {
  const candidates = turns.filter(t => t.call?.tool === action.toolName)
  const refs = new Set(action.refs.map(ref => typeof ref === "string" ? ref : ref.ref))
  const matched = candidates.filter(t => [t.observation?.ref, t.observation?.evidence_ref].some(ref => ref && refs.has(ref)))
  // Phase/report calls may not retain their own output ref. Use a single call
  // of that tool in this already-bound iteration; never pick one by position.
  return matched.length === 1 ? matched[0] : matched.length === 0 && candidates.length === 1 ? candidates[0] : undefined
}

export function iterationUsage(turns: TrajectoryTurn[]) {
  function sum(field: "tokens" | "advisor_tokens"): TrajectoryTokens | null {
    const stated = turns.flatMap(t => t[field] ? [t[field]!] : [])
    return stated.length ? stated.reduce((a, b) => ({ input: a.input + b.input, output: a.output + b.output }), { input: 0, output: 0 }) : null
  }
  // Union of recorded intervals, not a sum of overlapping parallel calls and
  // not the time from first to last (which would include unmeasured waiting).
  const intervals = turns.map(t => [Date.parse(t.t0 ?? ""), Date.parse(t.t1 ?? "")])
  const valid = intervals.filter(([a, b]) => Number.isFinite(a) && Number.isFinite(b) && b >= a).sort((a, b) => a[0] - b[0])
  let duration = 0, end = -Infinity
  for (const [a, b] of valid) {
    duration += Math.max(0, b - Math.max(a, end))
    end = Math.max(end, b)
  }
  return {
    actor: sum("tokens"), advisor: sum("advisor_tokens"),
    durationMs: valid.length ? duration : null,
    partialTime: valid.length < turns.length,
    hasActor: turns.some(t => t.actor === "model"),
    hasAdvisor: turns.some(t => t.call?.tool === "advisor"),
  }
}
