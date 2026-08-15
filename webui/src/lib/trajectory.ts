/**
 * Reading a trajectory-v1 document — the pure half of the timeline.
 *
 * Everything here is a derivation over what `GET /api/sessions/{id}/trajectory`
 * already said. Nothing fetches, nothing renders, nothing mutates the document
 * it is handed: the reducer is the only derivation (spec §1), and this file's
 * job is to arrange its output for rows and bands, never to infer a fact the
 * ledger did not state.
 */

import type {
  TrajectoryActor,
  TrajectoryAnnotation,
  TrajectoryDocument,
  TrajectoryGate,
  TrajectoryPhase,
  TrajectoryTurn,
} from "@/api/types"

/**
 * Fold a polled response into the document already held.
 *
 * The endpoint's cut contract, applied: `turns` are upserted by `turn_id` — a
 * turn's state changes after it is first sent, which is the whole reason the
 * poll cuts by ledger watermark and not by turn id — and `session`, `phases`,
 * `annotations` and `warnings` are REPLACED, because they arrive whole on every
 * response and a cut has no channel for a retraction. `outputs` is merged: refs
 * are content-addressed and shared, so a map already holding the system prompt
 * keeps it when a later poll does not mention it.
 */
export function mergeTrajectory(
  current: TrajectoryDocument | null,
  incoming: TrajectoryDocument,
): TrajectoryDocument {
  if (!current) {
    return incoming
  }

  const turns = new Map<number, TrajectoryTurn>()
  for (const turn of current.turns) {
    turns.set(turn.turn_id, turn)
  }
  for (const turn of incoming.turns) {
    turns.set(turn.turn_id, turn)
  }

  const outputs =
    current.outputs || incoming.outputs
      ? { ...(current.outputs ?? {}), ...(incoming.outputs ?? {}) }
      : null

  return {
    ...incoming,
    turns: [...turns.values()].sort((a, b) => a.turn_id - b.turn_id),
    outputs,
  }
}

/**
 * The cut to poll from next: the last ledger line this document was folded from.
 *
 * A turn id is the wrong watermark and cannot be made into the right one. A turn
 * is OPENED by one control event and finished by later ones, so the newest turn
 * a poll sees is routinely still in flight; cutting at its id drops it from
 * every later response, and it stands half-stated forever. Older turns move too
 * — a gate regraded after the model already acted on the first word, a detached
 * job whose end is only known at the close of the run.
 *
 * Every one of those is a control event with a sequence, folded into the turn's
 * `control_seq`, so the highest sequence held is the one watermark that names
 * what this document has already seen. A turn naming no sequence contributes
 * nothing to it — and the endpoint restates such a turn on every response,
 * because nothing about it can be shown unchanged.
 */
export function latestControlSeq(doc: TrajectoryDocument): number | null {
  let latest: number | null = null
  for (const turn of doc.turns) {
    for (const seq of turn.control_seq) {
      if (latest === null || seq > latest) {
        latest = seq
      }
    }
  }
  return latest
}

/** One horizontal band: a stretch of consecutive turns spent in one phase. */
export interface PhaseBand {
  key: string
  name: string
  /** 1 the first time the run entered this phase, 2 the next time, … */
  ordinal: number
  /** How the phase ended — carried on its LAST band only, since the reducer
   *  states one termination per phase name and a re-entry has not ended yet. */
  termination: string | null
  gates: TrajectoryGate[]
  turns: TrajectoryTurn[]
}

/**
 * Group the document's turns into phase bands, in turn order.
 *
 * A phase the run re-enters opens a SECOND band rather than being folded back
 * into the first: the turns are not contiguous, and drawing them as one band
 * would claim an ordering the run did not have. A phase no `phases[]` entry
 * names still gets its band — a turn is never dropped for want of a header.
 */
export function bandTurns(doc: TrajectoryDocument): PhaseBand[] {
  const meta = new Map<string, TrajectoryPhase>()
  for (const phase of doc.phases) {
    meta.set(phase.name, phase)
  }

  const bands: PhaseBand[] = []
  const seen = new Map<string, number>()

  for (const turn of [...doc.turns].sort((a, b) => a.turn_id - b.turn_id)) {
    const last = bands[bands.length - 1]
    if (last && last.name === turn.phase) {
      last.turns.push(turn)
      continue
    }
    const ordinal = (seen.get(turn.phase) ?? 0) + 1
    seen.set(turn.phase, ordinal)
    bands.push({
      key: `${turn.phase}#${ordinal}`,
      name: turn.phase,
      ordinal,
      termination: null,
      gates: meta.get(turn.phase)?.gates ?? [],
      turns: [turn],
    })
  }

  const lastOrdinal = seen
  for (const band of bands) {
    if (band.ordinal === lastOrdinal.get(band.name)) {
      band.termination = meta.get(band.name)?.termination ?? null
    }
  }

  return bands
}

/**
 * The chain of words this grading replaced, newest first.
 *
 * Walkable from the document alone because a phase band keeps EVERY grading it
 * made, superseded ones included. A link no band holds ends the walk — the
 * chain states what the record states and guesses at no missing grading — and a
 * decision id seen twice ends it too, so a malformed cycle cannot hang the row.
 */
export function gateChain(
  gate: TrajectoryGate | null | undefined,
  phases: TrajectoryPhase[],
): TrajectoryGate[] {
  if (!gate) {
    return []
  }

  const byId = new Map<string, TrajectoryGate>()
  for (const phase of phases) {
    for (const banded of phase.gates) {
      if (banded.decision_id && !byId.has(banded.decision_id)) {
        byId.set(banded.decision_id, banded)
      }
    }
  }

  const chain: TrajectoryGate[] = [gate]
  const walked = new Set<string>(gate.decision_id ? [gate.decision_id] : [])
  let cursor = gate.supersedes

  while (cursor && !walked.has(cursor)) {
    const older = byId.get(cursor)
    if (!older) {
      break
    }
    walked.add(cursor)
    chain.push(older)
    cursor = older.supersedes ?? null
  }

  return chain
}

/** Index annotations or warnings by the turn they are about. Rows that name no
 *  turn (a statement about the run, or about one ledger line) are not any
 *  row's, and are left to the document-level view. */
export function rowsByTurn<T extends { turn_id?: number | null }>(rows: T[]): Map<number, T[]> {
  const index = new Map<number, T[]>()
  for (const row of rows) {
    if (row.turn_id == null) {
      continue
    }
    const held = index.get(row.turn_id)
    if (held) {
      held.push(row)
    } else {
      index.set(row.turn_id, [row])
    }
  }
  return index
}

/** How many times the ladder had already read this failure, per the annotations
 *  on one turn — the highest count stated, or null if nothing repeated. */
export function recurrenceCount(annotations: TrajectoryAnnotation[]): number | null {
  let count: number | null = null
  for (const annotation of annotations) {
    if (annotation.kind !== "recurrence") {
      continue
    }
    const stated = annotation.data.recurrence_count
    if (typeof stated === "number" && (count === null || stated > count)) {
      count = stated
    }
  }
  return count
}

/**
 * The count a `window_truncated:<n>` component names, or null for a real ref.
 *
 * A window over the record's limit is CUT, never refused: the newest components
 * that fit are kept in render order and the first slot states how many older
 * ones could not be named. The marker names that cut — there are no bytes
 * behind it, and nothing should try to resolve one.
 */
export function truncationMarker(ref: string): number | null {
  const match = /^window_truncated:(\d+)$/.exec(ref)
  return match ? Number(match[1]) : null
}

/**
 * Refs this turn names that the held bytes cannot answer.
 *
 * Two handles are never counted, because asking the full tier for either would
 * ask forever: a `window_truncated:<n>` marker names a cut rather than bytes,
 * and `call.params_ref` names an envelope in `control_events.jsonl`, which the
 * output store was never built to resolve.
 */
export function unresolvedRefs(
  turn: TrajectoryTurn,
  outputs: Record<string, string> | null,
): string[] {
  const named = [
    turn.window_ref,
    ...(turn.window_components ?? []),
    turn.observation?.ref,
    turn.observation?.evidence_ref,
  ]

  const missing: string[] = []
  for (const ref of named) {
    if (!ref || truncationMarker(ref) !== null) {
      continue
    }
    if (outputs && ref in outputs) {
      continue
    }
    if (!missing.includes(ref)) {
      missing.push(ref)
    }
  }
  return missing
}

/** The turn's control-event sequence numbers, as the row prints them. */
export function formatSeq(seq: number[]): string {
  return seq.length ? seq.join("·") : "—"
}

/** Wall time between the turn's own two timestamps, or null if it has only one. */
export function turnDurationMs(turn: TrajectoryTurn): number | null {
  if (!turn.t0 || !turn.t1) {
    return null
  }
  const start = Date.parse(turn.t0)
  const end = Date.parse(turn.t1)
  if (Number.isNaN(start) || Number.isNaN(end)) {
    return null
  }
  return end - start
}

/** A turn's wall time, as every view of it prints it. */
export function formatDuration(ms: number): string {
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`
}

/** A token count, short enough for an axis label. Exact counts live in titles. */
export function formatTokens(tokens: number): string {
  return tokens < 1000 ? `${tokens}` : `${(tokens / 1000).toFixed(1)}k`
}

/** One mark the reducer drew on a turn: where the run bled, in its own words. */
export interface TurnAnomaly {
  /** The reducer's `data.anomaly`, or `"conflict"` when the mark names none. */
  kind: string
  /** What the badge says. */
  label: string
  /** What the badge's tooltip says. */
  detail: string
}

function text(value: unknown): string | null {
  return typeof value === "string" && value ? value : null
}

function count(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null
}

/**
 * The anomaly marks on one turn, phrased for a badge.
 *
 * The reducer states these as `conflict` annotations carrying an `anomaly`
 * discriminator (a killed process, a detached job that never reported its end).
 * Nothing is inferred here: the phrasing arranges fields the annotation already
 * carries, and a mark whose `anomaly` this build has never heard of is drawn
 * under its own name rather than dropped — a future reducer must be able to
 * state something new without this view silently swallowing it.
 */
export function anomalies(annotations: TrajectoryAnnotation[]): TurnAnomaly[] {
  const marks: TurnAnomaly[] = []
  for (const annotation of annotations) {
    if (annotation.kind !== "conflict") {
      continue
    }
    const data = annotation.data ?? {}
    const kind = text(data.anomaly) ?? "conflict"
    const statedBy = text(data.stated_by)
    const job = text(data.job_id)
    const source = statedBy ? ` (stated by ${statedBy})` : ""

    if (kind === "killed_by_signal") {
      const exit = count(data.exit_code)
      const signal = count(data.signal)
      marks.push({
        kind,
        label: exit == null ? "killed" : `killed · exit ${exit}`,
        detail:
          `the process did not exit, it was killed` +
          (signal == null ? "" : ` by signal ${signal}`) +
          (job ? `; job ${job}` : "") +
          source,
      })
      continue
    }

    if (kind === "job_never_settled") {
      const reason = text(data.close_reason)
      marks.push({
        kind,
        label: "job never settled",
        detail:
          `job ${job ?? "(unnamed)"} never reported a terminal exit` +
          (reason ? `; the run closed on ${reason}` : "") +
          source,
      })
      continue
    }

    marks.push({ kind, label: kind, detail: `the ledger marked this turn ${kind}${source}` })
  }
  return marks
}

/** One turn's column in the header sparkline. */
export interface SparkColumn {
  turnId: number
  phase: string
  actor: TrajectoryActor
  /** Prompt + completion tokens, or null when no bill has been stated yet. */
  tokens: number | null
  /** Wall time, or null when the turn has only one of its two stamps. */
  durationMs: number | null
  /** The marks the reducer drew on this turn, if any. */
  anomalies: TurnAnomaly[]
}

/**
 * The document as columns, in turn order — the shape the sparkline draws.
 *
 * `null` is carried rather than collapsed to zero. A turn whose bill has not
 * landed (the token ledger is exported at loop exit) and a turn that cost
 * nothing are different facts, and a zero-height bar tells them apart from
 * neither. The view draws an absence as an absence.
 */
export function sparkColumns(doc: TrajectoryDocument): SparkColumn[] {
  const marks = rowsByTurn(doc.annotations)
  return [...doc.turns]
    .sort((a, b) => a.turn_id - b.turn_id)
    .map((turn) => ({
      turnId: turn.turn_id,
      phase: turn.phase,
      actor: turn.actor,
      tokens: turn.tokens ? turn.tokens.input + turn.tokens.output : null,
      durationMs: turnDurationMs(turn),
      anomalies: anomalies(marks.get(turn.turn_id) ?? []),
    }))
}

/** The largest value a column series states, or null when none states one. */
export function seriesPeak(columns: SparkColumn[], pick: (c: SparkColumn) => number | null) {
  let peak: number | null = null
  for (const column of columns) {
    const value = pick(column)
    if (value != null && (peak === null || value > peak)) {
      peak = value
    }
  }
  return peak
}
