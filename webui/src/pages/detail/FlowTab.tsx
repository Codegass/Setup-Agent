import { useMemo, useRef, useState } from "react"
import { ArrowUp, ChevronRight } from "lucide-react"

import type { ContextTrace, ExecutionSessionDetail, TrajectoryDocument, TrajectoryPhase, TrajectoryTurn } from "@/api/types"
import { fetchSessionOutput } from "@/api/client"
import { isLiveSessionStatus } from "@/components/common/status"
import { ActionDetailModal } from "@/components/session/ActionDetailModal"
import { IterationUsage } from "@/components/session/IterationUsage"
import { TrajectoryTimeline } from "@/components/trajectory/TrajectoryTimeline"
import type { BytesStatus } from "@/components/trajectory/RefDescent"
import { TurnSparkline } from "@/components/trajectory/TurnSparkline"
import { actionTurn, flowTurns } from "@/lib/flow-usage"
import { formatDuration, turnDurationMs } from "@/lib/trajectory"
import { cn } from "@/lib/utils"
import { useTrajectory } from "./useTrajectory"

type Phase = ContextTrace["phases"][number]
type PhaseTask = Phase["tasks"][number]
type PhaseIteration = PhaseTask["iterations"][number]
type PhaseAction = PhaseIteration["actions"][number]

/** Recorded phase progress, separate from the result card's build/test verdict. */
function phaseTone(status: string): "success" | "failed" | "attention" | "neutral" {
  const s = status.trim().toLowerCase()
  if (["completed", "complete", "success", "passed", "done", "ok"].includes(s)) return "success"
  if (["failed", "failure", "error", "blocked", "conflict"].includes(s)) return "failed"
  if (["partial", "repair"].includes(s)) return "attention"
  return "neutral"
}

/** A context records the plan; the ledger records which phases ran and how they
 *  were graded. A skipped planned phase must not inherit a parent's failure. */
function phaseStatus(phase: Phase, recorded: TrajectoryPhase | undefined, hasLedger: boolean, live: boolean): string {
  if (recorded) {
    const word = [...recorded.gates].reverse().find(gate => gate.word)?.word
    if (recorded.termination === "repair") return "repair"
    if (word) return word
    return recorded.termination ? "ended" : "in progress"
  }
  if (hasLedger) {
    const hasActions = phase.tasks.some(task => task.iterations.some(entry => entry.actions.length > 0))
    // A partial ledger cannot erase calls that the context did retain. Only an
    // explicit skip with no recorded actions supports "not run".
    if (hasActions) return "not graded"
    if (live) return "pending"
    return /^\[skipped\]/i.test(phase.keyResults ?? "") ? "not run" : "not recorded"
  }
  return phase.status === "completed" ? "ended" : phase.status
}

/** "3 iterations · 3 actions" from a phase progress record (falls back to the
 *  actual task/iteration counts when the progress map is missing). */
function phaseMeta(phase: Phase): string {
  const iterations =
    phase.progress.iterations ??
    phase.tasks.reduce((sum, task) => sum + task.iterations.length, 0)
  const actions =
    phase.progress.actions ??
    phase.tasks.reduce(
      (sum, task) =>
        sum + task.iterations.reduce((inner, it) => inner + it.actions.length, 0),
      0,
    )
  const iterLabel = `${iterations} iteration${iterations === 1 ? "" : "s"}`
  const actionLabel = `${actions} action${actions === 1 ? "" : "s"}`
  return `${iterLabel} · ${actionLabel}`
}

function progressPercent(progress: Record<string, number>): number | null {
  const done = Number.isFinite(progress.done) ? progress.done : null
  const total = Number.isFinite(progress.total) ? progress.total : 0
  if (done === null || total <= 0) return null
  return Math.max(0, Math.min(100, (done / total) * 100))
}

function stepsText(progress: Record<string, number>): string | null {
  const done = Number.isFinite(progress.done) ? progress.done : null
  const total = Number.isFinite(progress.total) ? progress.total : 0
  if (done === null || total <= 0) return null
  return `${done} / ${total} phases`
}

function iterationLabel(iteration: PhaseIteration): string {
  return iteration.iteration == null
    ? `Entry ${iteration.sequence}`
    : `Iteration ${iteration.iteration}`
}

function actionTone(action: PhaseAction): { label: string; tone: "success" | "running" | "failed" | "neutral" } {
  if (action.dispatchStatus === "pending") return { label: "running", tone: "running" }
  if (action.success === true) return { label: "success", tone: "success" }
  if (action.success === false) return { label: "failed", tone: "failed" }
  return { label: "not recorded", tone: "neutral" }
}

/** A single clickable tool call: tool badge + honest status + first line of output.
 *  Clicking opens the full output / observation modal. */
function ActionRow({ action, turn, onOpen }: { action: PhaseAction; turn?: TrajectoryTurn; onOpen: (trigger: HTMLButtonElement) => void }) {
  const status = actionTone(action)
  const advisor = action.toolName === "advisor"
  const duration = turn ? turnDurationMs(turn) : null
  return (
    <button
      type="button"
      aria-label={`${action.toolName} · ${status.label} · details`}
      onClick={(event) => onOpen(event.currentTarget)}
      className={cn("mt-1.5 w-full rounded-lg border px-3 py-2.5 text-left transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring", advisor ? "border-advisor/25 bg-advisor-soft hover:border-advisor/60" : "border-border bg-muted hover:bg-accent")}
    >
      <div className="flex items-center gap-2">
        <span className={cn("min-w-0 truncate rounded-md px-2.5 py-0.5 font-mono text-[11px] font-semibold", advisor ? "bg-advisor/15 text-advisor" : "bg-primary text-primary-foreground")}>
          {action.toolName}
        </span>
        <span
          className={cn(
            "inline-flex shrink-0 items-center gap-1.5 text-[11px] font-semibold",
            status.tone === "success" && "text-status-success",
            status.tone === "running" && "text-status-running",
            status.tone === "failed" && "text-status-failed",
            status.tone === "neutral" && "text-muted-foreground",
          )}
        >
          <span
            className={cn(
              "h-1.5 w-1.5 rounded-full",
              status.tone === "success" && "bg-status-success",
              status.tone === "running" && "bg-status-running",
              status.tone === "failed" && "bg-status-failed",
              status.tone === "neutral" && "bg-muted-foreground",
            )}
          />
          {status.label}
        </span>
        <span className="ml-auto flex shrink-0 items-center gap-2 text-[11px] text-muted-foreground">
          {duration != null && duration >= 0 ? <span className="font-mono tabular-nums">{formatDuration(Math.round(duration))}</span> : null}
          <ChevronRight size={14} aria-hidden="true" />
        </span>
      </div>
      {advisor ? <div className="mt-1.5 flex flex-wrap gap-x-2 font-mono text-[11px] text-advisor">
        <span>{turn?.advisor?.model ?? "Model not recorded"}</span>
        <span>· {turn?.advisor?.effort ? `${turn.advisor.effort} effort` : "Effort not recorded"}</span>
      </div> : null}
      {action.output ? (
        <div className="mt-1.5 truncate font-mono text-[12px] leading-relaxed text-foreground">
          {action.output}
        </div>
      ) : null}
      {action.observation ? (
        <div className="mt-1 truncate text-[12px] leading-relaxed text-muted-foreground">
          ↳ {action.observation}
        </div>
      ) : null}
    </button>
  )
}

/** One iteration inside a task card: the "ITERATION N" label, italic THINK rows,
 *  then the clickable action rows. */
function IterationBlock({
  iteration,
  onOpenAction,
  turns,
  selectedTurn,
  register,
  onBackToChart,
}: {
  iteration: PhaseIteration
  onOpenAction: (action: PhaseAction, trigger: HTMLButtonElement) => void
  turns: TrajectoryTurn[]
  selectedTurn: number | null
  register: (turns: TrajectoryTurn[], node: HTMLDivElement | null) => void
  onBackToChart: () => void
}) {
  const hasTrace = iteration.thoughts.length > 0 || iteration.actions.length > 0
  const active = turns.some(t => t.turn_id === selectedTurn)
  return (
    <div ref={node => register(turns, node)} tabIndex={-1} aria-label={iterationLabel(iteration)} data-flow-iteration={iteration.iteration} className={cn("scroll-mt-5 border-b border-border px-3.5 py-3 last:border-0 focus:outline-none", active && "bg-accent/60 ring-1 ring-inset ring-primary")}>
      <div className="mb-2 flex flex-wrap items-start gap-x-3 gap-y-2">
        <span className="text-[12px] font-semibold text-foreground">{iterationLabel(iteration)}</span>
        {active ? <button type="button" aria-label="Back to usage chart" title="Back to usage chart" className="rounded-sm text-primary focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring" onClick={onBackToChart}><ArrowUp size={14} /></button> : null}
        <div className="ml-auto min-w-0"><IterationUsage turns={turns} /></div>
      </div>
      {iteration.thoughts.map((thought, index) => (
        <div className="mb-2.5 flex items-start gap-2.5" key={`think-${index}`}>
          <span className="mt-px inline-flex h-[18px] shrink-0 items-center rounded-md bg-muted px-1.5 font-sans text-[10px] font-semibold uppercase tracking-[0.04em] text-muted-foreground">
            think
          </span>
          <span className="min-w-0 text-[13px] italic leading-relaxed text-muted-foreground">
            {thought}
          </span>
        </div>
      ))}
      {iteration.actions.map((action, index) => (
        <ActionRow
          action={action}
          turn={actionTurn(action, turns)}
          key={`${action.toolName}-${index}`}
          onOpen={(trigger) => onOpenAction(action, trigger)}
        />
      ))}
      {!hasTrace ? (
        <p className="text-[12px] leading-relaxed text-muted-foreground">
          No tool calls or notes were recorded for this entry.
        </p>
      ) : null}
    </div>
  )
}

/** A task within a phase: a bordered card with a header and its iterations. */
function TaskCard({
  task,
  onOpenAction,
  usage,
  selectedTurn,
  register,
  onBackToChart,
}: {
  task: PhaseTask
  onOpenAction: (action: PhaseAction, trigger: HTMLButtonElement) => void
  usage: Map<PhaseIteration, TrajectoryTurn[]>
  selectedTurn: number | null
  register: (turns: TrajectoryTurn[], node: HTMLDivElement | null) => void
  onBackToChart: () => void
}) {
  return (
    <div className="mt-2.5 overflow-hidden rounded-[10px] border border-border">
      <details className="group border-b border-border bg-muted px-3.5 py-2.5">
        <summary className="cursor-pointer text-[13px] font-semibold text-foreground">
          <span className="line-clamp-2 group-open:line-clamp-none">{task.title}</span>
        </summary>
      </details>
      {task.iterations.map((iteration, index) => (
        <IterationBlock
          iteration={iteration}
          key={`${iteration.sequence}-${iteration.iteration ?? index}`}
          onOpenAction={onOpenAction}
          turns={usage.get(iteration) ?? []}
          selectedTurn={selectedTurn}
          register={register}
          onBackToChart={onBackToChart}
        />
      ))}
    </div>
  )
}

/** One phase on the vertical rail: colored dot + connecting line, the phase
 *  header (title · name · status pill · meta), and its task cards. */
function PhaseRow({
  phase,
  recorded,
  hasLedger,
  live,
  last,
  onOpenAction,
  usage,
  selectedTurn,
  register,
  onBackToChart,
}: {
  phase: Phase
  recorded?: TrajectoryPhase
  hasLedger: boolean
  live: boolean
  last: boolean
  onOpenAction: (action: PhaseAction, trigger: HTMLButtonElement) => void
  usage: Map<PhaseIteration, TrajectoryTurn[]>
  selectedTurn: number | null
  register: (turns: TrajectoryTurn[], node: HTMLDivElement | null) => void
  onBackToChart: () => void
}) {
  const status = phaseStatus(phase, recorded, hasLedger, live)
  const tone = phaseTone(status)
  const skipped = status === "not run"
  return (
    <section aria-label={`Phase ${phase.name}`} className="grid grid-cols-[22px_1fr] gap-x-3.5">
      <div className="flex flex-col items-center pt-0.5">
        <span
          className={cn(
            "h-3.5 w-3.5 shrink-0 rounded-full ring-4",
            tone === "success" && "bg-status-success ring-status-success-soft",
            tone === "failed" && "bg-status-failed ring-status-failed-soft",
            tone === "attention" && "bg-status-attention ring-status-attention-soft",
            tone === "neutral" && "bg-accent ring-border",
          )}
        />
        {!last ? <div className="mt-1 w-0.5 flex-1 bg-accent" /> : null}
      </div>

      <div className={cn("min-w-0", last ? "pb-0" : "pb-5")}>
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-[14px] font-bold capitalize text-foreground">{phase.name}</h3>
          <span className={cn("rounded-full px-2 py-0.5 text-[11px] font-semibold",
            tone === "success" && "bg-status-success-soft text-status-success",
            tone === "failed" && "bg-status-failed-soft text-status-failed",
            tone === "attention" && "bg-status-attention-soft text-status-attention",
            tone === "neutral" && "bg-muted text-muted-foreground",
          )}>{status}</span>
          {!skipped ? <span className="ml-auto font-mono text-[11px] text-muted-foreground">{phaseMeta(phase)}</span> : null}
        </div>

        {!skipped && phase.tasks.map((task) => (
          <TaskCard key={task.id} onOpenAction={onOpenAction} task={task} usage={usage} selectedTurn={selectedTurn} register={register} onBackToChart={onBackToChart} />
        ))}
      </div>
    </section>
  )
}

/**
 * Flow tab: the agent's execution trace, flow-first. A goal trunk card leads, then
 * a vertical phase timeline whose actions open the output/observation modal. Markup
 * mirrors WorkbenchDetail.dc.html lines 204–262 (the FLOW block in the AFTER template).
 */
export function FlowTab({ detail, trajectory, loadOutput, bytes = "absent", onNeedBytes }: {
  detail: ExecutionSessionDetail
  trajectory?: TrajectoryDocument | null
  loadOutput?: (ref: string) => Promise<string>
  bytes?: BytesStatus
  onNeedBytes?: () => void
}) {
  const [selected, setSelected] = useState<PhaseAction | null>(null)
  const [selectedTurn, setSelectedTurn] = useState<number | null>(null)
  const selectedTrigger = useRef<HTMLButtonElement | null>(null)
  const chart = useRef<HTMLDivElement>(null)
  const entries = useRef(new Map<number, HTMLDivElement>())
  const context = detail.context
  const usage = useMemo(() => flowTurns(context, trajectory), [context, trajectory])
  const register = (turns: TrajectoryTurn[], node: HTMLDivElement | null) => {
    for (const turn of turns) {
      if (node) entries.current.set(turn.turn_id, node)
      else entries.current.delete(turn.turn_id)
    }
  }
  const onSelectTurn = (id: number) => {
    setSelectedTurn(id)
    const entry = entries.current.get(id)
    entry?.scrollIntoView({ block: "center" })
    entry?.focus({ preventScroll: true })
  }
  const onBackToChart = () => {
    chart.current?.scrollIntoView({ block: "start" })
    chart.current?.querySelector<SVGElement>(`[data-hit="${selectedTurn}"]`)?.focus()
  }
  const onOpenAction = (action: PhaseAction, trigger: HTMLButtonElement) => {
    selectedTrigger.current = trigger
    setSelected(action)
  }
  const graph = trajectory ? <div ref={chart} className="mb-5 scroll-mt-5"><TurnSparkline doc={trajectory} onSelectTurn={onSelectTurn} selectedTurn={selectedTurn} /></div> : null
  const timeline = trajectory ? <TrajectoryTimeline doc={trajectory} context={context} sessionId={detail.id} status={bytes} live={isLiveSessionStatus(detail.status)} onNeedBytes={onNeedBytes} onOpenAction={onOpenAction} register={register} selectedTurn={selectedTurn} onBackToChart={onBackToChart} /> : null

  if (!context) {
    if (trajectory) return <div>{graph}{timeline}</div>
    return (
      <div className="rounded-lg border border-dashed border-border px-4 py-8 text-center text-[12.5px] text-muted-foreground">
        Flow is not available for this run.
      </div>
    )
  }

  const trunk = context.trunk
  const phases = context.phases ?? []
  const stats = detail.resultCard?.stats
  const progress = stats?.phasesCompleted != null && stats?.phasesTotal != null
    ? { done: stats.phasesCompleted, total: stats.phasesTotal } : trunk.progress ?? {}
  const percent = progressPercent(progress)
  const steps = stepsText(progress)
  const result = detail.resultCard?.verdict
  const trunkTone = result ? phaseTone(result) : "neutral"
  // Do not call a completed workflow a successful setup. This word belongs to
  // the same result card the rest of the page displays.
  const stateLabel = result ? `Result: ${result}` : `Flow ${trunk.state}`
  const hasLedger = !!trajectory?.phases.length

  return (
    <div>
      {graph}
      <div className="rounded-xl border border-border bg-card px-[18px] py-4">
        <div className="flex items-center gap-2">
          <span className="font-sans text-[11px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
            Agent goal
          </span>
          <span
            aria-label={stateLabel}
            className={cn(
              "inline-flex items-center gap-1.5 text-[11px] font-semibold",
              trunkTone === "success" && "text-status-success",
              trunkTone === "failed" && "text-status-failed",
              trunkTone === "attention" && "text-status-attention",
              trunkTone === "neutral" && "text-muted-foreground",
            )}
          >
            <span
              className={cn(
                "h-1.5 w-1.5 rounded-full",
                trunkTone === "success" && "bg-status-success",
                trunkTone === "failed" && "bg-status-failed",
                trunkTone === "attention" && "bg-status-attention",
                trunkTone === "neutral" && "bg-muted-foreground",
              )}
            />
            {stateLabel}
          </span>
        </div>
        <details className="group mt-1.5">
          <summary aria-label="Agent goal" className="cursor-pointer text-[15px] font-semibold leading-snug text-foreground">
            <span className="line-clamp-3 group-open:line-clamp-none">{trunk.goal}</span>
          </summary>
        </details>
        {trunk.summary ? (
          <div className="mt-1.5 text-[13px] leading-relaxed text-muted-foreground">{trunk.summary}</div>
        ) : null}
        {percent != null ? (
          <div className="mt-3 flex items-center gap-2.5">
            <div role="progressbar" aria-label="Phase progress" aria-valuenow={percent} aria-valuemin={0} aria-valuemax={100} aria-valuetext={steps ?? undefined} className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
              <div
                className="h-full rounded-full bg-primary"
                style={{ width: `${percent}%` }}
              />
            </div>
            {steps ? (
              <span className="shrink-0 font-mono text-[12px] text-muted-foreground">{steps}</span>
            ) : null}
          </div>
        ) : null}
      </div>

      <div className="mt-[18px]">
        {trajectory?.turns.length ? timeline : phases.length > 0 ? (
          phases.map((phase, index) => (
            <PhaseRow
              key={phase.id}
              last={index === phases.length - 1}
              onOpenAction={onOpenAction}
              usage={usage}
              selectedTurn={selectedTurn}
              register={register}
              onBackToChart={onBackToChart}
              phase={phase}
              recorded={trajectory?.phases.filter(band => band.name === phase.name).at(-1)}
              hasLedger={hasLedger}
              live={isLiveSessionStatus(detail.status)}
            />
          ))
        ) : (
          <div className="rounded-lg border border-dashed border-border px-4 py-8 text-center text-[12.5px] text-muted-foreground">
            No phases were recorded for this run.
          </div>
        )}
      </div>

      {selected ? (
        <ActionDetailModal
          action={selected}
          loadOutput={loadOutput}
          onClose={() => setSelected(null)}
          onRestoreFocus={() => selectedTrigger.current?.focus({ preventScroll: true })}
        />
      ) : null}
    </div>
  )
}

/** Real runs use the same reader and interactive usage plots as Turns. */
export function RecordedFlowTab({ detail }: { detail: ExecutionSessionDetail }) {
  const { doc, error, loading, load, bytes, bytesError, needBytes } = useTrajectory(detail.id, isLiveSessionStatus(detail.status))
  return (
    <div className="space-y-5">
      {loading && !doc ? <p className="text-[12px] text-muted-foreground">Reading usage and phase records…</p> : null}
      {error ? (
        <div role="status" className="flex flex-wrap items-center gap-2 text-[12px] text-status-attention">
          <span>{error}</span>
          <button type="button" className="underline" onClick={() => void load({ whole: true, force: true })}>Retry</button>
        </div>
      ) : null}
      {bytesError ? <p role="status" className="text-[12px] text-status-attention">{bytesError}</p> : null}
      {doc?.warnings.some(w => w.turn_id == null) ? <details className="text-[12px] text-status-attention">
        <summary className="cursor-pointer">{doc.warnings.filter(w => w.turn_id == null).length} gaps in the record</summary>
        <ul className="mt-2 space-y-1">{doc.warnings.filter(w => w.turn_id == null).map((w, i) => <li key={i}>{w.detail}</li>)}</ul>
      </details> : null}
      <FlowTab detail={detail} trajectory={doc} bytes={bytes} onNeedBytes={needBytes} loadOutput={(ref) => fetchSessionOutput(detail.id, ref)} />
    </div>
  )
}
