import type * as React from "react"

import type { TrajectoryTurn } from "@/api/types"
import { formatSeq, truncationMarker } from "@/lib/trajectory"

import { RefDescent, type BytesStatus } from "./RefDescent"

function Panel({
  title,
  hint,
  testId,
  children,
}: {
  title: string
  hint?: string
  testId: string
  children: React.ReactNode
}) {
  return (
    <section
      className="min-w-0 rounded-lg border border-border bg-card p-3"
      data-testid={testId}
    >
      <h4 className="text-[11px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
        {title}
      </h4>
      {hint ? <p className="mt-0.5 text-[11px] text-muted-foreground">{hint}</p> : null}
      <div className="mt-2 space-y-2">{children}</div>
    </section>
  )
}

function Nothing({ label }: { label: string }) {
  return <p className="text-[11.5px] italic text-muted-foreground">{label}</p>
}

/**
 * The quad of one turn: [A] what the model saw, [B] what it asked, [C] what it
 * read back, [D] what happened next.
 *
 * Every panel resolves refs rather than restating bytes: the row's handles are
 * the authoritative record's own, so any panel can be descended from.
 */
export function TurnQuad({
  turn,
  next,
  outputs,
  status,
}: {
  turn: TrajectoryTurn
  next: TrajectoryTurn | null
  outputs: Record<string, string> | null
  status: BytesStatus
}) {
  const components = turn.window_components ?? []
  // The row's handle into [A] is the record's own, and the record makes it the
  // LAST component it named — the newest message, the one thing this turn did
  // not share with the turn before it. So it is normally already in the list
  // below, and drawing it above as well put one ref on screen twice and invited
  // a reader to descend the same bytes from two places. It is marked where it
  // lives instead, and keeps its own descent only when the list does not carry
  // it — a record that named a handle and no components.
  const handle = turn.window_ref == null ? -1 : components.lastIndexOf(turn.window_ref)

  return (
    <div className="grid gap-2.5 lg:grid-cols-2">
      <Panel
        hint="Every component the record named, in render order."
        testId={`quad-window-${turn.turn_id}`}
        title="[A] Window — what the model saw"
      >
        {turn.window_ref && handle < 0 ? (
          <RefDescent
            label="window handle"
            outputs={outputs}
            refName={turn.window_ref}
            status={status}
          />
        ) : null}
        {components.length ? (
          <ol aria-label="Window components" className="space-y-1.5">
            {components.map((component, index) => {
              const cut = truncationMarker(component)
              return (
                <li className="min-w-0" key={`${component}-${index}`}>
                  {cut != null ? (
                    <span className="text-[11.5px] text-status-attention">
                      {`${cut} older components the record could not name (window cut)`}
                    </span>
                  ) : (
                    <RefDescent
                      label={index === handle ? "window handle" : undefined}
                      outputs={outputs}
                      refName={component}
                      status={status}
                    />
                  )}
                </li>
              )
            })}
          </ol>
        ) : turn.window_ref ? null : (
          <Nothing label="No record stated a window for this turn." />
        )}
      </Panel>

      <Panel testId={`quad-call-${turn.turn_id}`} title="[B] Call — what was asked">
        {turn.call ? (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-sans text-[10px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
                tool
              </span>
              <span className="font-mono text-[11.5px] text-foreground">{turn.call.tool}</span>
            </div>
            {turn.call.params_ref ? (
              <>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-sans text-[10px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
                    envelope
                  </span>
                  <span className="font-mono text-[11.5px] text-foreground">
                    {turn.call.params_ref}
                  </span>
                </div>
                <p className="text-[11px] text-muted-foreground">
                  The exact params live in control_events.jsonl under this envelope id; the
                  output store was never built to answer one.
                </p>
              </>
            ) : (
              <Nothing label="No envelope was sealed for this call." />
            )}
          </>
        ) : (
          <Nothing label="This turn called nothing." />
        )}
      </Panel>

      <Panel testId={`quad-observation-${turn.turn_id}`} title="[C] Observation — what came back">
        {turn.observation ? (
          <>
            {turn.observation.error_code ? (
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-sans text-[10px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
                  error
                </span>
                <span className="font-mono text-[11.5px] text-status-failed">
                  {turn.observation.error_code}
                </span>
              </div>
            ) : null}
            {turn.observation.failure_signature ? (
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-sans text-[10px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
                  signature
                </span>
                <span className="break-all font-mono text-[11.5px] text-muted-foreground">
                  {turn.observation.failure_signature}
                </span>
              </div>
            ) : null}
            {turn.observation.ref ? (
              <RefDescent
                label="read by the model"
                outputs={outputs}
                refName={turn.observation.ref}
                status={status}
              />
            ) : (
              <Nothing label="No delivered observation is on the record for this turn." />
            )}
            {turn.observation.evidence_ref &&
            turn.observation.evidence_ref !== turn.observation.ref ? (
              <RefDescent
                label="written by the tool"
                outputs={outputs}
                refName={turn.observation.evidence_ref}
                status={status}
              />
            ) : null}
          </>
        ) : (
          <Nothing label="Nothing came back — the turn has no observation." />
        )}
      </Panel>

      <Panel testId={`quad-next-${turn.turn_id}`} title="[D] Next — what happened after">
        {next ? (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-[11.5px] font-semibold text-foreground">
                {`Turn ${next.turn_id}`}
              </span>
              <span className="text-[11.5px] text-muted-foreground">{next.actor}</span>
              {next.call ? (
                <span className="font-mono text-[11.5px] text-foreground">{next.call.tool}</span>
              ) : null}
            </div>
            {next.phase !== turn.phase ? (
              <p className="text-[11.5px] text-status-attention">
                {`The run left ${turn.phase} for ${next.phase} between these two turns.`}
              </p>
            ) : null}
            <p className="font-mono text-[11px] text-muted-foreground">
              {`seq ${formatSeq(next.control_seq)}`}
            </p>
          </>
        ) : (
          <Nothing label="The ledger states no turn after this one — it is the last turn folded so far." />
        )}
      </Panel>
    </div>
  )
}
