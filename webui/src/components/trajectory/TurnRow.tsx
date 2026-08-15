import { ChevronDown, ChevronRight } from "lucide-react"
import { useState } from "react"

import type {
  TrajectoryAnnotation,
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

import type { BytesStatus } from "./RefDescent"
import { TurnQuad } from "./TurnQuad"

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

function durationLabel(turn: TrajectoryTurn): string | null {
  const ms = turnDurationMs(turn)
  return ms === null ? null : formatDuration(ms)
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
  next,
  phases,
  annotations,
  warnings,
  outputs,
  status,
  onNeedBytes,
}: {
  turn: TrajectoryTurn
  next: TrajectoryTurn | null
  phases: TrajectoryPhase[]
  annotations: TrajectoryAnnotation[]
  warnings: TrajectoryWarning[]
  outputs: Record<string, string> | null
  status: BytesStatus
  onNeedBytes?: () => void
}) {
  const [open, setOpen] = useState(false)
  const controller = turn.actor === "controller"
  const recurrence = recurrenceCount(annotations)
  const marks = anomalies(annotations)
  const quadId = `turn-quad-${turn.turn_id}`
  const duration = durationLabel(turn)

  // The ask for bytes lives outside the state updater: an updater may be called
  // twice, and asking twice would poll the full tier twice for one expansion.
  // A row asks whenever it names a ref the held bytes cannot answer — including
  // a row that arrived after the last full read, which is the live case.
  const toggle = () => {
    if (!open && unresolvedRefs(turn, outputs).length) {
      onNeedBytes?.()
    }
    setOpen((value) => !value)
  }

  return (
    <article
      className={cn(
        "rounded-lg border px-3 py-2",
        controller
          ? "border-dashed border-status-attention-border bg-status-attention-soft/40"
          : "border-border bg-card",
      )}
      data-actor={turn.actor}
      data-testid={`turn-row-${turn.turn_id}`}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <button
          aria-controls={quadId}
          aria-expanded={open}
          className="inline-flex min-w-0 items-center gap-2 text-left"
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
          <span
            className={cn(
              "rounded-full px-2 py-0.5 text-[10.5px] font-semibold uppercase tracking-[0.06em]",
              controller
                ? "bg-status-attention-soft text-status-attention"
                : "bg-accent text-muted-foreground",
            )}
          >
            {turn.actor}
          </span>
          {turn.call ? (
            <span className="truncate font-mono text-[12px] text-foreground">
              {turn.call.tool}
            </span>
          ) : (
            <span className="truncate text-[12px] italic text-muted-foreground">no call</span>
          )}
        </button>

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
          {turn.observation?.failure_signature ? (
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
          {turn.iteration != null ? (
            <span className="font-mono text-[10.5px] text-muted-foreground">
              {`iter ${turn.iteration}`}
            </span>
          ) : null}
          {turn.tokens ? (
            <span className="font-mono text-[10.5px] text-muted-foreground">
              {`${turn.tokens.input} in / ${turn.tokens.output} out`}
            </span>
          ) : null}
          {duration ? (
            <span className="font-mono text-[10.5px] text-muted-foreground">{duration}</span>
          ) : null}
          <span className="font-sans text-[10px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
            seq
          </span>
          <span className="font-mono text-[10.5px] text-foreground">
            {formatSeq(turn.control_seq)}
          </span>
        </div>
      </div>

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
          <TurnQuad next={next} outputs={outputs} status={status} turn={turn} />
        </div>
      ) : null}
    </article>
  )
}
