import type * as React from "react"

import type { TrajectoryEnvelope, TrajectoryTurn } from "@/api/types"
import { truncationMarker } from "@/lib/trajectory"
import { cn } from "@/lib/utils"

import { RefDescent, type BytesStatus } from "./RefDescent"

export type EnvelopeStatus = "absent" | "loading" | "ready" | "error"

function Panel({
  title,
  hint,
  testId,
  className,
  children,
}: {
  title: string
  hint?: string
  testId: string
  className?: string
  children: React.ReactNode
}) {
  return (
    <section
      className={cn("min-w-0 rounded-lg border border-border bg-card p-3", className)}
      data-testid={testId}
    >
      <h4 className="text-[12px] font-semibold text-foreground">{title}</h4>
      {hint ? <p className="mt-0.5 text-[11px] text-muted-foreground">{hint}</p> : null}
      <div className="mt-2 space-y-2">{children}</div>
    </section>
  )
}

function Nothing({ label }: { label: string }) {
  return <p className="text-[11.5px] italic text-muted-foreground">{label}</p>
}

function field(label: string, value: React.ReactNode) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="font-sans text-[10px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
        {label}
      </span>
      {value}
    </div>
  )
}

function modelMessage(bytes: string | undefined): { role: string; preview: string } | null {
  if (bytes == null) {
    return null
  }
  try {
    const parsed = JSON.parse(bytes) as unknown
    if (typeof parsed !== "object" || parsed === null) {
      return null
    }
    const record = parsed as Record<string, unknown>
    if (typeof record.role !== "string" || typeof record.content !== "string") {
      return null
    }
    const compact = record.content.replace(/\s+/g, " ").trim()
    return {
      role: record.role,
      preview: compact.length > 180 ? `${compact.slice(0, 179)}…` : compact,
    }
  } catch {
    return null
  }
}

function evidenceRefLabel(refName: string): string {
  if (refName.startsWith("job:")) {
    return "Background job"
  }
  if (refName.startsWith("output_")) {
    return "Tool output reference"
  }
  return "Evidence reference"
}

function ContextRef({
  refName,
  outputs,
  status,
}: {
  refName: string
  outputs: Record<string, string> | null
  status: BytesStatus
}) {
  const message = modelMessage(outputs?.[refName])
  return (
    <div className="space-y-1.5">
      {message ? (
        <div className="flex min-w-0 items-start gap-2">
          <span className="shrink-0 rounded bg-accent px-1.5 py-0.5 font-mono text-[10.5px] text-muted-foreground">
            {message.role}
          </span>
          <span className="min-w-0 text-[11.5px] leading-relaxed text-foreground">
            {message.preview || "Empty message content"}
          </span>
        </div>
      ) : null}
      <RefDescent outputs={outputs} refName={refName} status={status} />
    </div>
  )
}

function CallParameters({
  envelopeId,
  envelope,
  status,
  error,
}: {
  envelopeId: string
  envelope: TrajectoryEnvelope | null
  status: EnvelopeStatus
  error: string | null
}) {
  return (
    <div className="space-y-2">
      {status === "loading" ? (
        <p className="text-[11.5px] text-muted-foreground">Reading exact parameters…</p>
      ) : null}
      {status === "error" ? (
        <p className="text-[11.5px] text-status-failed">
          {`Exact parameters could not be read${error ? `: ${error}` : "."}`}
        </p>
      ) : null}
      {envelope ? (
        <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-muted px-2.5 py-2 font-mono text-[11.5px] leading-relaxed text-foreground">
          {JSON.stringify(envelope.exact_params, null, 2)}
        </pre>
      ) : null}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[10.5px] text-muted-foreground">
        <span>Call envelope</span>
        <span className="font-mono">{envelopeId}</span>
        {envelope?.sequence != null ? (
          <span className="font-mono">{`seq ${envelope.sequence}`}</span>
        ) : null}
      </div>
    </div>
  )
}

/** The model context, tool call, and tool result for one turn. */
export function TurnQuad({
  turn,
  outputs,
  status,
  envelope,
  envelopeStatus = "absent",
  envelopeError = null,
}: {
  turn: TrajectoryTurn
  outputs: Record<string, string> | null
  status: BytesStatus
  envelope?: TrajectoryEnvelope | null
  envelopeStatus?: EnvelopeStatus
  envelopeError?: string | null
}) {
  const components = turn.window_components ?? []
  const handle = turn.window_ref == null ? -1 : components.lastIndexOf(turn.window_ref)

  return (
    <div className="grid gap-2.5 lg:grid-cols-2">
      <Panel
        className="lg:col-span-2"
        hint="Messages sent to the model, in order. The role and preview come from the stored message."
        testId={`quad-window-${turn.turn_id}`}
        title="Model context"
      >
        {turn.window_ref && handle < 0 ? (
          <ContextRef outputs={outputs} refName={turn.window_ref} status={status} />
        ) : null}
        {components.length ? (
          <ol aria-label="Model context messages" className="space-y-2">
            {components.map((component, index) => {
              const cut = truncationMarker(component)
              return (
                <li className="min-w-0" key={`${component}-${index}`}>
                  {cut != null ? (
                    <span className="text-[11.5px] text-status-attention">
                      {`${cut} older messages are omitted by this context window.`}
                    </span>
                  ) : (
                    <ContextRef outputs={outputs} refName={component} status={status} />
                  )}
                </li>
              )
            })}
          </ol>
        ) : turn.window_ref ? null : (
          <Nothing label="No model context is recorded for this turn." />
        )}
      </Panel>

      <Panel testId={`quad-call-${turn.turn_id}`} title="Tool call">
        {turn.call ? (
          <>
            {field(
              "tool",
              <span className="font-mono text-[11.5px] text-foreground">{turn.call.tool}</span>,
            )}
            {turn.call.params_ref ? (
              <CallParameters
                envelope={envelope ?? null}
                envelopeId={turn.call.params_ref}
                error={envelopeError}
                status={envelopeStatus}
              />
            ) : (
              <Nothing label="No parameter envelope is recorded for this call." />
            )}
          </>
        ) : (
          <Nothing label="This turn called no tool." />
        )}
      </Panel>

      <Panel
        hint="Exact tool message added to the model context. It may include status, evidence refs, and a bounded diagnostic tail."
        testId={`quad-observation-${turn.turn_id}`}
        title="Tool result"
      >
        {turn.observation ? (
          <>
            {turn.observation.error_code
              ? field(
                  "error",
                  <span className="font-mono text-[11.5px] text-status-failed">
                    {turn.observation.error_code}
                  </span>,
                )
              : null}
            {turn.observation.failure_signature
              ? field(
                  "signature",
                  <span className="break-all font-mono text-[11.5px] text-muted-foreground">
                    {turn.observation.failure_signature}
                  </span>,
                )
              : null}
            {turn.observation.ref ? (
              <RefDescent
                label="Model-visible result"
                outputs={outputs}
                refName={turn.observation.ref}
                status={status}
              />
            ) : (
              <Nothing label="No model-visible result is recorded for this turn." />
            )}
            {turn.observation.evidence_ref &&
            turn.observation.evidence_ref !== turn.observation.ref ? (
              <RefDescent
                label={evidenceRefLabel(turn.observation.evidence_ref)}
                outputs={outputs}
                refName={turn.observation.evidence_ref}
                status={status}
              />
            ) : null}
          </>
        ) : (
          <Nothing label="No tool result is recorded for this turn." />
        )}
      </Panel>
    </div>
  )
}
