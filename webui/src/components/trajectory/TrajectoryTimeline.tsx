import type { TrajectoryDocument } from "@/api/types"
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
}: {
  doc: TrajectoryDocument
  status?: BytesStatus
  onNeedBytes?: () => void
  sessionId?: string
}) {
  const bands = bandTurns(doc)
  const annotations = rowsByTurn(doc.annotations)
  const warnings = rowsByTurn(doc.warnings)

  if (!doc.turns.length) {
    return (
      <div className="rounded-lg border border-dashed border-border px-4 py-8 text-center text-[12.5px] text-muted-foreground">
        No turns yet. The first turn appears here as soon as the run records one.
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {bands.map((band) => (
        <div
          aria-label={`Phase ${band.name}`}
          className="rounded-xl border border-border bg-muted/40"
          key={band.key}
          role="group"
        >
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border px-3 py-2">
            <span className="font-mono text-[12px] font-semibold text-foreground">
              {band.name}
            </span>
            {band.ordinal > 1 ? (
              <span className="rounded-full bg-accent px-2 py-0.5 text-[10.5px] text-muted-foreground">
                {`re-entry ${band.ordinal}`}
              </span>
            ) : null}
            <span className="text-[11px] text-muted-foreground">
              {`${band.turns.length} turn${band.turns.length === 1 ? "" : "s"}`}
            </span>
            {band.gates.length ? (
              <span className="text-[11px] text-muted-foreground">
                {`${band.gates.length} gate${band.gates.length === 1 ? "" : "s"}`}
              </span>
            ) : null}
            {/* How the phase's checks came out, and the phase's own word for
                why. A segment that recorded neither shows neither — a band
                never fills in a state or a reason the run did not state. */}
            {band.validatorState ? (
              <span className="text-[11px] text-muted-foreground">
                {`checks ${band.validatorState}`}
              </span>
            ) : null}
            {band.reason ? (
              <span className="min-w-0 truncate text-[11px] text-muted-foreground">
                {band.reason}
              </span>
            ) : null}
            {band.termination ? (
              <span
                className={cn(
                  "ml-auto rounded-full border border-border bg-card px-2 py-0.5 font-mono text-[10.5px] text-muted-foreground",
                )}
                title="how this phase ended"
              >
                {band.termination}
              </span>
            ) : null}
          </div>

          {band.keyResults ? (
            <details className="border-b border-border px-3 py-1.5">
              <summary className="cursor-pointer text-[11px] text-muted-foreground">
                What this phase reported
              </summary>
              <p className="mt-1 whitespace-pre-wrap text-[11.5px] text-foreground">
                {band.keyResults}
              </p>
            </details>
          ) : null}

          <div className="space-y-1.5 p-2">
            {band.turns.map((turn) => (
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
              />
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}
