import { Check, ChevronDown, ChevronRight, CircleHelp, CircleX, Info } from "lucide-react"
import { useState } from "react"

import { fetchTrajectoryEnvelope } from "@/api/client"
import type {
  TrajectoryAnnotation,
  TrajectoryEnvelope,
  TrajectoryObservation,
  TrajectoryPhase,
  TrajectoryTurn,
  TrajectoryWarning,
} from "@/api/types"
import {
  anomalies,
  formatDuration,
  formatSeq,
  gateChain,
  recurrenceCount,
  turnDurationMs,
  unresolvedRefs,
} from "@/lib/trajectory"
import { cn } from "@/lib/utils"
import type { FlowAction } from "@/lib/flow-usage"

import { RefDescent, type BytesStatus } from "./RefDescent"
import { TurnQuad, type EnvelopeStatus } from "./TurnQuad"

interface EnvelopeState {
  sessionId: string
  id: string
  status: EnvelopeStatus
  envelope: TrajectoryEnvelope | null
  error: string | null
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** Gate words the run delivers, toned the way the rest of the app tones an
 *  outcome. An unknown word is neutral rather than guessed at. */
function gateTone(word: string): string {
  const normalized = word.trim().toLowerCase()
  if (["success", "pass", "passed"].includes(normalized)) {
    return "border-status-success-border bg-status-success-soft text-status-success"
  }
  if (["partial", "retry", "blocked"].includes(normalized)) {
    return "border-status-attention-border bg-status-attention-soft text-status-attention"
  }
  if (["failed", "failure", "fail"].includes(normalized)) {
    return "border-status-failed-border bg-status-failed-soft text-status-failed"
  }
  return "border-status-idle-border bg-status-idle-soft text-status-idle"
}

/** How a call came out, toned the way the rest of the app tones an outcome.
 *  The word itself is the run's — this only chooses a colour for it. */
const OUTCOME_CHIP: Record<NonNullable<TrajectoryObservation["outcome"]>, string> = {
  ok: "bg-status-success-soft text-status-success",
  failed: "bg-status-failed-soft text-status-failed",
  refused: "bg-status-attention-soft text-status-attention",
  cancelled: "bg-status-attention-soft text-status-attention",
  pending: "bg-status-running-soft text-status-running",
}

function durationLabel(turn: TrajectoryTurn): string | null {
  const ms = turnDurationMs(turn)
  return ms === null ? null : formatDuration(ms)
}

function automaticStatus(turn: TrajectoryTurn) {
  const phase = turn.phase === "unknown" ? "Automatic" : turn.phase[0].toUpperCase() + turn.phase.slice(1)
  const gate = turn.gate
  if (!gate) {
    const outcome = turn.observation?.outcome
    if (outcome === "failed") return { label: "Automatic action failed", Icon: CircleX, tone: "text-status-failed" }
    if (outcome === "refused" || outcome === "cancelled") return { label: `Automatic action ${outcome}`, Icon: CircleHelp, tone: "text-status-attention" }
    if (outcome === "pending") return { label: "Automatic action pending", Icon: Info, tone: "text-status-running" }
    return { label: "Automatic event", Icon: Info, tone: "text-muted-foreground" }
  }
  if (gate.accepted === false) return { label: `${phase} check not accepted`, Icon: CircleHelp, tone: "text-status-attention" }
  if (gate.word === "failed") return { label: `${phase} check failed`, Icon: CircleX, tone: "text-status-failed" }
  if (gate.word === "partial") return { label: `${phase} check incomplete`, Icon: CircleHelp, tone: "text-status-attention" }
  if (gate.accepted === true && gate.word === "success") return {
    label: turn.phase === "report" && gate.code === "report_present" ? "Report verified" : `${phase} check passed`,
    Icon: Check,
    tone: "text-status-success",
  }
  return { label: `${phase} check unresolved`, Icon: CircleHelp, tone: "text-muted-foreground" }
}

/** The gate word, and the chain it replaced when it replaced one. The chain is
 *  the phase band's own record of every grading made, so expanding it shows
 *  words the run actually delivered — never a reconstruction. */
function GateBadge({
  turn,
  phases,
}: {
  turn: TrajectoryTurn
  phases: TrajectoryPhase[]
}) {
  const [open, setOpen] = useState(false)
  const gate = turn.gate
  if (!gate) {
    return null
  }

  const chain = gateChain(gate, phases)
  const chainId = `gate-chain-${turn.turn_id}`

  return (
    <span className="inline-flex flex-col items-start gap-1">
      <button
        aria-controls={chainId}
        aria-expanded={open}
        className={cn(
          "inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] font-medium leading-none",
          gateTone(gate.word),
        )}
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        <span className="uppercase tracking-[0.06em]">gate</span>
        <span className="font-mono">{gate.word}</span>
      </button>
      {open ? (
        <ol
          aria-label="Supersedes chain"
          className="rounded-md border border-border bg-muted px-2 py-1.5"
          id={chainId}
        >
          {chain.map((step, index) => (
            <li
              className="flex flex-wrap items-center gap-2 py-0.5"
              key={`${step.decision_id ?? "anonymous"}-${index}`}
            >
              <span className="font-mono text-[11px] text-foreground">{step.word}</span>
              <span className="font-mono text-[10.5px] text-muted-foreground">
                {step.decision_id ?? "no decision id"}
              </span>
              <span className="text-[10.5px] uppercase tracking-[0.06em] text-muted-foreground">
                {index === 0 ? "delivered" : "replaced"}
              </span>
            </li>
          ))}
        </ol>
      ) : null}
    </span>
  )
}

/**
 * One turn, as a row.
 *
 * A model turn and a controller turn are drawn differently on purpose: the
 * harness taking a turn on its own is the fact a reader is usually hunting, and
 * a row that reads the same as the model's would hide it. Every row prints its
 * `control_seq`, so any row can be descended to the bytes it was folded from.
 */
export function TurnRow({
  turn,
  phases,
  annotations,
  warnings,
  outputs,
  status,
  onNeedBytes,
  sessionId,
  compact = false,
  sourceAction,
  onOpenAction,
}: {
  turn: TrajectoryTurn
  phases: TrajectoryPhase[]
  annotations: TrajectoryAnnotation[]
  warnings: TrajectoryWarning[]
  outputs: Record<string, string> | null
  status: BytesStatus
  onNeedBytes?: () => void
  sessionId?: string
  compact?: boolean
  sourceAction?: FlowAction
  onOpenAction?: (action: FlowAction, trigger: HTMLButtonElement) => void
}) {
  const [open, setOpen] = useState(false)
  const [envelopeState, setEnvelopeState] = useState<EnvelopeState | null>(null)
  const controller = turn.actor === "controller"
  const advisor = turn.call?.tool === "advisor"
  const recurrence = recurrenceCount(annotations)
  const marks = anomalies(annotations)
  const quadId = `turn-quad-${turn.turn_id}`
  const duration = durationLabel(turn)
  const envelopeId = turn.call?.params_ref
  const callEnvelopeState =
    envelopeState !== null &&
    envelopeState.sessionId === sessionId &&
    envelopeState.id === envelopeId
      ? envelopeState
      : null

  // The ask for bytes lives outside the state updater: an updater may be called
  // twice, and asking twice would poll the full tier twice for one expansion.
  // A row asks whenever it names a ref the held bytes cannot answer — including
  // a row that arrived after the last full read, which is the live case.
  const toggle = () => {
    if (!open && unresolvedRefs(turn, outputs).length) {
      onNeedBytes?.()
    }
    if (
      !open &&
      sessionId &&
      envelopeId &&
      (callEnvelopeState === null || callEnvelopeState.status === "error")
    ) {
      setEnvelopeState({
        sessionId,
        id: envelopeId,
        status: "loading",
        envelope: null,
        error: null,
      })
      void fetchTrajectoryEnvelope(sessionId, envelopeId)
        .then((envelope) => {
          setEnvelopeState({
            sessionId,
            id: envelopeId,
            status: "ready",
            envelope,
            error: null,
          })
        })
        .catch((error) => {
          setEnvelopeState({
            sessionId,
            id: envelopeId,
            status: "error",
            envelope: null,
            error: errorMessage(error),
          })
        })
    }
    setOpen((value) => !value)
  }

  // A controller event without a call is an inline checkpoint. It retains its
  // ledger identity and bytes, but has no model-context/tool-call panels.
  if (controller && !turn.call) {
    const { label, Icon, tone } = automaticStatus(turn)
    const refs = [...new Set([turn.observation?.ref, turn.observation?.evidence_ref].filter((ref): ref is string => !!ref))]
    return <article data-actor="controller" data-kind="automatic-event"
      data-control-seq={formatSeq(turn.control_seq)} data-testid={`turn-row-${turn.turn_id}`}>
      <button type="button" aria-expanded={open} aria-controls={quadId}
        aria-label={`${label} · automatic event ${turn.turn_id}${duration ? ` · ${duration}` : ""}`}
        onClick={toggle}
        className="flex min-h-9 w-full items-center gap-2 rounded-md px-3 py-1.5 text-left hover:bg-accent/60 focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring">
        <Icon size={15} aria-hidden="true" className={cn("shrink-0", tone)} />
        <span className="min-w-0 flex-1 text-[12px] font-medium text-foreground">{label}</span>
        {warnings.length || marks.length ? <span className="text-[11px] text-status-attention">Record warning</span> : null}
        {duration ? <span className="shrink-0 font-mono text-[10.5px] text-muted-foreground">{duration}</span> : null}
        {open ? <ChevronDown size={14} aria-hidden="true" className="shrink-0 text-muted-foreground" /> : <ChevronRight size={14} aria-hidden="true" className="shrink-0 text-muted-foreground" />}
      </button>
      {open ? <div id={quadId} className="space-y-2 px-3 pb-2 pt-1 text-[11.5px]">
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-muted-foreground">
          <span>{`Turn ${turn.turn_id} · SAG controller`}</span>
          <span className="font-mono">Control sequence: {formatSeq(turn.control_seq)}</span>
        </div>
        {turn.gate?.reason ? <p className="whitespace-pre-wrap break-words text-foreground">{turn.gate.reason}</p> : null}
        {turn.observation?.summary ? <p className="whitespace-pre-wrap break-words text-foreground">{turn.observation.summary}</p> : null}
        {turn.observation?.error_code ? <p className="break-all font-mono text-status-attention">{turn.observation.error_code}</p> : null}
        {turn.gate ? <div className="flex flex-wrap gap-x-3 gap-y-1 break-all font-mono text-[11px] text-muted-foreground">
          <span>{turn.gate.word}</span>
          {turn.gate.code ? <span>{turn.gate.code}</span> : null}
          {turn.gate.decision_id ? <span>{turn.gate.decision_id}</span> : null}
        </div> : null}
        {turn.gate?.supersedes ? <GateBadge turn={turn} phases={phases} /> : null}
        {turn.t0 || turn.t1 ? <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 font-mono text-[11px] text-muted-foreground">
          <dt>Start</dt><dd className="break-all">{turn.t0 ?? "not recorded"}</dd>
          <dt>End</dt><dd className="break-all">{turn.t1 ?? "not recorded"}</dd>
        </dl> : null}
        {refs.length ? refs.map(ref => <RefDescent key={ref} label="Recorded message" refName={ref} outputs={outputs} status={status} />) : <p className="text-muted-foreground">No recorded message is available.</p>}
        {marks.map(mark => <p key={`${mark.kind}-${mark.label}`} className="text-status-attention">{mark.label}: {mark.detail}</p>)}
        {warnings.map((warning, index) => <p key={`${warning.code}-${index}`} className="text-status-attention"><span className="font-mono">{warning.code}</span>: {warning.detail}</p>)}
      </div> : null}
    </article>
  }

  return (
    <article
      className={cn(
        "rounded-lg border px-3 py-2",
        advisor ? "border-advisor/25 bg-advisor-soft" : controller
          ? "border-dashed border-border bg-muted/40"
          : "border-border bg-card",
      )}
      data-actor={turn.actor}
      data-control-seq={formatSeq(turn.control_seq)}
      data-testid={`turn-row-${turn.turn_id}`}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <button
          aria-controls={quadId}
          aria-expanded={open}
          className="inline-flex min-w-0 max-w-full items-center gap-2 rounded-sm text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring"
          onClick={toggle}
          type="button"
        >
          {open ? (
            <ChevronDown className="shrink-0 text-muted-foreground" size={14} />
          ) : (
            <ChevronRight className="shrink-0 text-muted-foreground" size={14} />
          )}
          <span className="font-mono text-[12px] font-semibold text-foreground">
            {`Turn ${turn.turn_id}`}
          </span>
          {/* The harness taking a turn on its own is called what the terminal
              calls it. "controller" is the ledger's word for the same actor;
              a reader who has never read the ledger should not have to learn
              it to see who acted. */}
          {!advisor ? <span
            className={cn(
              "rounded-full px-2 py-0.5 text-[10.5px] font-semibold uppercase tracking-[0.06em]",
              controller
                ? "bg-accent text-muted-foreground"
                : "bg-accent text-muted-foreground",
            )}
          >
            {controller ? "engine" : turn.actor}
          </span> : null}
          {turn.call ? (
            <span className={cn("shrink-0 font-mono text-[12px]", advisor ? "font-semibold text-advisor" : "text-primary")}>
              {turn.call.tool}
            </span>
          ) : (
            <span className="truncate text-[12px] italic text-muted-foreground">no call</span>
          )}
          {/* What the call asked for, in the run's own line. A call the run
              wrote no line for shows its tool name and nothing more — there is
              no sentence to invent. */}
          {turn.call?.summary ? (
            <span className="min-w-0 flex-1 truncate text-[12px] text-foreground">
              {turn.call.summary}
            </span>
          ) : null}
        </button>

        {turn.observation?.outcome || turn.observation?.summary ? (
          <div className="flex min-w-0 flex-1 items-center gap-2">
            {turn.observation.outcome ? (
              <span
                className={cn(
                  "shrink-0 rounded-full px-2 py-0.5 text-[11px] font-semibold",
                  OUTCOME_CHIP[turn.observation.outcome],
                )}
              >
                {turn.observation.outcome}
              </span>
            ) : null}
            {/* A failed call often summarises itself as its own error code,
                which the badge beside it has already said. 13 of the 32 coded
                observations across the committed fixtures read that way, so
                the line is dropped rather than doubled — the same rule the
                result band uses for a headline equal to its status chip. */}
            {turn.observation.summary &&
            turn.observation.summary !== turn.observation.error_code ? (
              <span className="min-w-0 truncate text-[12px] text-muted-foreground">
                {turn.observation.summary}
              </span>
            ) : null}
          </div>
        ) : null}

        <div className="flex flex-wrap items-center gap-1.5">
          {/* Where the run BLED, in the ledger's own words. A mark is a fact
              about the turn, so it sits with the badges and never with the
              warnings underneath, which are this layer's word for a hole. */}
          {marks.map((mark) => (
            <span
              className="whitespace-nowrap rounded-full border border-status-failed-border bg-status-failed-soft px-2 py-0.5 font-mono text-[10.5px] font-semibold text-status-failed"
              key={`${mark.kind}-${mark.label}`}
              title={mark.detail}
            >
              {mark.label}
            </span>
          ))}
          {turn.observation?.error_code ? (
            <span className="whitespace-nowrap rounded-full border border-status-failed-border bg-status-failed-soft px-2 py-0.5 font-mono text-[10.5px] text-status-failed">
              {turn.observation.error_code}
            </span>
          ) : null}
          {!compact && turn.observation?.failure_signature ? (
            <span
              className="max-w-[22rem] truncate rounded-full border border-border bg-muted px-2 py-0.5 font-mono text-[10.5px] text-muted-foreground"
              title="failure signature"
            >
              {turn.observation.failure_signature}
            </span>
          ) : null}
          {recurrence != null ? (
            <span
              className="whitespace-nowrap rounded-full border border-status-attention-border bg-status-attention-soft px-2 py-0.5 font-mono text-[10.5px] text-status-attention"
              title="times the ladder had already read this failure"
            >
              {`×${recurrence}`}
            </span>
          ) : null}
          <GateBadge phases={phases} turn={turn} />
        </div>

        <div className="ml-auto flex flex-wrap items-center gap-x-3 gap-y-1">
          {!compact && turn.iteration != null ? (
            <span className="font-mono text-[10.5px] text-muted-foreground">
              {`iter ${turn.iteration}`}
            </span>
          ) : null}
          {/* Two models can answer on one turn — the executor's own response,
              and the advisor a consult paid for — so each bill is printed under
              the name of the model that sent it. Side by side and unlabelled
              they read as one measure stated twice; added together they are a
              bill nobody was ever sent. */}
          {!compact && turn.tokens ? (
            <span className="font-mono text-[10.5px] text-muted-foreground">
              {`model ${turn.tokens.input} in / ${turn.tokens.output} out`}
            </span>
          ) : null}
          {!compact && turn.advisor_tokens ? (
            <span className="font-mono text-[10.5px] text-muted-foreground">
              {`advisor ${turn.advisor_tokens.input} in / ${turn.advisor_tokens.output} out`}
            </span>
          ) : null}
          {duration ? (
            <span className="font-mono text-[10.5px] text-muted-foreground">{duration}</span>
          ) : null}
          {!compact ? <><span className="font-sans text-[10px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
            seq
          </span>
          <span className="font-mono text-[10.5px] text-foreground">
            {formatSeq(turn.control_seq)}
          </span></> : null}
          {sourceAction && onOpenAction ? <button type="button" aria-label={`${sourceAction.toolName} · ${sourceAction.success === true ? "success" : sourceAction.success === false ? "failed" : "not recorded"} · details`} className={cn("rounded-sm text-[11px] underline underline-offset-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring", advisor ? "text-advisor" : "text-primary")} onClick={e => onOpenAction(sourceAction, e.currentTarget)}>Output</button> : null}
        </div>
      </div>
      {advisor ? <div className="mt-1.5 flex flex-wrap gap-x-2 font-mono text-[11px] text-advisor">
        <span>{turn.advisor?.model ?? "Model not recorded"}</span>
        <span>· {turn.advisor?.effort ? `${turn.advisor.effort} effort` : "Effort not recorded"}</span>
      </div> : null}

      {warnings.length ? (
        <ul className="mt-1.5 space-y-0.5">
          {warnings.map((warning, index) => (
            <li className="flex flex-wrap items-baseline gap-2" key={`${warning.code}-${index}`}>
              <span className="font-mono text-[10.5px] font-semibold text-status-attention">
                {warning.code}
              </span>
              <span className="text-[11px] text-muted-foreground">{warning.detail}</span>
            </li>
          ))}
        </ul>
      ) : null}

      {open ? (
        <div className="mt-2.5" id={quadId}>
          {compact ? <p className="mb-2 font-mono text-[10.5px] text-muted-foreground">Control sequence: <span>{formatSeq(turn.control_seq)}</span></p> : null}
          <TurnQuad
            envelope={callEnvelopeState?.envelope ?? null}
            envelopeError={callEnvelopeState?.error ?? null}
            envelopeStatus={callEnvelopeState?.status ?? "absent"}
            outputs={outputs}
            status={status}
            turn={turn}
          />
        </div>
      ) : null}
    </article>
  )
}
