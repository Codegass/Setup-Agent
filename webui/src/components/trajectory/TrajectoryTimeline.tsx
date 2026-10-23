import { ArrowUp } from "lucide-react"
import { Fragment } from "react"
import type { ContextTrace, TrajectoryDocument, TrajectoryTurn } from "@/api/types"
import { IterationUsage } from "@/components/session/IterationUsage"
import { actionTurn, flowTurns, type FlowAction } from "@/lib/flow-usage"
import { bandTurns, rowsByTurn } from "@/lib/trajectory"
import { cn } from "@/lib/utils"

import type { BytesStatus } from "./RefDescent"
import { TurnRow } from "./TurnRow"

/**
 * The run as a timeline: horizontal phase bands, one row per turn.
 *
 * Every fact here was stated by the trajectory reducer — this component decides
 * arrangement and emphasis, never content. A hole in the ledger is drawn on the
 * row it is about rather than swallowed, which is the whole reason the reducer
 * carries warnings instead of raising.
 */
export function TrajectoryTimeline({
  doc,
  status = "absent",
  onNeedBytes,
  sessionId,
  context,
  onOpenAction,
  register,
  selectedTurn,
  onBackToChart,
  live = false,
}: {
  doc: TrajectoryDocument
  status?: BytesStatus
  onNeedBytes?: () => void
  sessionId?: string
  context?: ContextTrace | null
  onOpenAction?: (action: FlowAction, trigger: HTMLButtonElement) => void
  register?: (turns: TrajectoryTurn[], node: HTMLDivElement | null) => void
  selectedTurn?: number | null
  onBackToChart?: () => void
  live?: boolean
}) {
  const bands = bandTurns(doc)
  const annotations = rowsByTurn(doc.annotations)
  const warnings = rowsByTurn(doc.warnings)
  const entries = flowTurns(context, doc)
  const contextByTurn = new Map([...entries].flatMap(([entry, turns]) => turns.map(t => [t.turn_id, entry] as const)))
  const unvisited = context?.phases.filter(p => !bands.some(b => b.name === p.name)) ?? []
  // Place an unexecuted planned phase before its next recorded neighbour.
  // Actual ledger bands retain their order, including repair re-entries.
  const following = (phase: ContextTrace["phases"][number]) => context?.phases
    .slice(context.phases.indexOf(phase) + 1).find(p => bands.some(b => b.name === p.name))?.name
  const renderUnvisited = (p: ContextTrace["phases"][number]) => <section key={p.id} aria-label={`Phase ${p.name}`} className="rounded-lg border border-dashed border-border px-3 py-2 text-[12px] text-muted-foreground">
    <h3 className="inline font-semibold capitalize">{p.name}</h3>
    <span className="ml-3">{p.tasks.some(t => t.iterations.some(i => i.actions.length)) ? "not graded" : live ? "pending" : /^\[skipped\]/i.test(p.keyResults ?? "") ? "not run" : "not recorded"}</span>
    {onOpenAction && p.tasks.flatMap(t => t.iterations.flatMap(i => i.actions)).map((a, i) => <button key={i} className="ml-3 underline" type="button" onClick={e => onOpenAction(a, e.currentTarget)}>Archived output · {a.toolName}</button>)}
  </section>
  const groups = (turns: TrajectoryTurn[]) => {
    const result: TrajectoryTurn[][] = []
    for (const turn of turns) {
      const last = result.at(-1)
      if (turn.iteration != null && last?.[0].iteration === turn.iteration) last.push(turn)
      else result.push([turn])
    }
    return result
  }

  if (!doc.turns.length) {
    return (
      <div className="rounded-lg border border-dashed border-border px-4 py-8 text-center text-[12.5px] text-muted-foreground">
        No turns yet. The first turn appears here as soon as the run records one.
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {bands.map((band) => {
        const phase = context?.phases.find(p => p.name === band.name)
        const word = band.termination === "repair" ? "repair" : band.gates.at(-1)?.word
        return (
        <Fragment key={band.key}>
        {band.ordinal === 1 ? unvisited.filter(p => following(p) === band.name).map(renderUnvisited) : null}
        <div
          aria-label={`Phase ${band.name}`}
          className="rounded-xl border border-border bg-muted/40"
          key={band.key}
          role="group"
        >
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border px-3 py-2">
            <h3 className="text-[14px] font-semibold capitalize text-foreground">
              {band.name}
            </h3>
            {band.ordinal > 1 ? (
              <span className="rounded-full bg-accent px-2 py-0.5 text-[10.5px] text-muted-foreground">
                {`re-entry ${band.ordinal}`}
              </span>
            ) : null}
            <span className="text-[11px] text-muted-foreground">
              {`${band.turns.length} turn${band.turns.length === 1 ? "" : "s"}`}
            </span>
            {/* How the phase's checks came out, and the phase's own word for
                why. A segment that recorded neither shows neither — a band
                never fills in a state or a reason the run did not state. */}
            {band.validatorState && !word ? (
              <span className="text-[11px] text-muted-foreground">
                {`checks ${band.validatorState}`}
              </span>
            ) : null}
            {word || band.termination ? (
              <span
                className={cn(
                  "ml-auto rounded-full px-2 py-0.5 text-[11px] font-semibold",
                  word === "success" ? "bg-status-success-soft text-status-success" : word === "failed" ? "bg-status-failed-soft text-status-failed" : word === "partial" || word === "repair" ? "bg-status-attention-soft text-status-attention" : "bg-muted text-muted-foreground",
                )}
                title="Recorded phase outcome; separate from the final run verdict"
              >
                {word ?? "ended"}
              </span>
            ) : null}
          </div>

          {band.keyResults || phase?.title || band.reason || band.termination || band.validatorState ? (
            <details className="border-b border-border px-3 py-1.5">
              <summary className="cursor-pointer text-[11px] text-muted-foreground">
                Phase details
              </summary>
              <p className="mt-1 whitespace-pre-wrap text-[11.5px] text-foreground">
                {phase?.title}
                {band.keyResults ? `\n\n${band.keyResults}` : ""}
                {band.reason ? `\n\n${band.reason}` : ""}
              </p>
              <div className="mt-2 flex flex-wrap gap-3 text-[11px] text-muted-foreground">
                {band.termination ? <span>{band.termination}</span> : null}
                {band.validatorState ? <span>checks {band.validatorState}</span> : null}
                {band.gates.length ? <span>{band.gates.length} gate{band.gates.length === 1 ? "" : "s"}</span> : null}
              </div>
            </details>
          ) : null}

          <div className="divide-y divide-border">
            {groups(band.turns).map(turns => {
              const entry = contextByTurn.get(turns[0].turn_id)
              const linked = new Map<number, FlowAction>()
              for (const action of entry?.actions ?? []) {
                const matched = actionTurn(action, turns)
                if (matched) linked.set(matched.turn_id, action)
              }
              const active = turns.some(t => t.turn_id === selectedTurn)
              return <div key={turns[0].turn_id} ref={node => register?.(turns, node)} tabIndex={-1}
                aria-label={turns[0].iteration == null ? `Turn ${turns[0].turn_id}` : `Iteration ${turns[0].iteration}`}
                data-flow-iteration={turns[0].iteration}
                className={cn("scroll-mt-5 space-y-2 p-3 focus:outline-none", active && "bg-accent/50 ring-1 ring-inset ring-primary")}>
                <div className="flex flex-wrap items-start gap-x-3 gap-y-2">
                  <span className="text-[12px] font-semibold">{turns[0].iteration == null ? "Recorded step" : `Iteration ${turns[0].iteration}`}</span>
                  {active && onBackToChart ? <button type="button" aria-label="Back to usage chart" title="Back to usage chart" className="text-primary focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring" onClick={onBackToChart}><ArrowUp size={14} /></button> : null}
                  <div className="ml-auto min-w-0"><IterationUsage turns={turns} /></div>
                </div>
                {entry?.thoughts.length ? <details className="text-[12px] text-muted-foreground">
                  <summary className="cursor-pointer">Actor notes</summary>
                  <div className="mt-1 space-y-1">{entry.thoughts.map((thought, i) => <p key={i} className="whitespace-pre-wrap">{thought}</p>)}</div>
                </details> : null}
                {turns.map((turn) => (
              <TurnRow
                annotations={annotations.get(turn.turn_id) ?? []}
                key={turn.turn_id}
                onNeedBytes={onNeedBytes}
                outputs={doc.outputs ?? null}
                phases={doc.phases}
                sessionId={sessionId}
                status={status}
                turn={turn}
                warnings={warnings.get(turn.turn_id) ?? []}
                compact
                sourceAction={linked.get(turn.turn_id)}
                onOpenAction={onOpenAction}
              />
                ))}
                {onOpenAction && entry?.actions.filter(a => ![...linked.values()].includes(a)).map((action, i) => <button key={i} type="button" className="text-[12px] text-primary underline" onClick={e => onOpenAction(action, e.currentTarget)}>Archived output · {action.toolName}</button>)}
              </div>
            })}
          </div>
        </div>
        </Fragment>
      )})}
      {unvisited.filter(p => !following(p)).map(renderUnvisited)}
    </div>
  )
}
