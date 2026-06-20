import { useState } from "react"

import type { ContextTrace, ExecutionSessionDetail } from "@/api/types"
import { ActionDetailModal } from "@/components/session/ActionDetailModal"
import { cn } from "@/lib/utils"

type Phase = ContextTrace["phases"][number]
type PhaseTask = Phase["tasks"][number]
type PhaseIteration = PhaseTask["iterations"][number]
type PhaseAction = PhaseIteration["actions"][number]

/** Phase status → the rail dot + the completed/failed pill. Anything that isn't a
 *  clear pass is treated as failed/attention so the verdict stays honest. */
function phaseTone(status: string): "success" | "failed" | "neutral" {
  const s = status.trim().toLowerCase()
  if (["completed", "complete", "success", "passed", "done", "ok"].includes(s)) return "success"
  if (["failed", "failure", "error", "blocked", "conflict"].includes(s)) return "failed"
  return "neutral"
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
  return `${done} / ${total} steps`
}

function iterationLabel(iteration: PhaseIteration): string {
  return iteration.iteration == null
    ? `Entry ${iteration.sequence}`
    : `Iteration ${iteration.iteration}`
}

function actionTone(action: PhaseAction): { label: string; tone: "success" | "running" | "failed" } {
  if (action.dispatchStatus === "pending") return { label: "running", tone: "running" }
  if (action.success === true) return { label: "success", tone: "success" }
  return { label: "failed", tone: "failed" }
}

/** A single clickable tool call: tool badge + honest status + first line of output.
 *  Clicking opens the full output / observation modal. */
function ActionRow({ action, onOpen }: { action: PhaseAction; onOpen: () => void }) {
  const status = actionTone(action)
  return (
    <button
      type="button"
      onClick={onOpen}
      className="mt-1.5 w-full rounded-lg border border-slate-100 bg-slate-50 px-3 py-2.5 text-left transition-colors hover:border-slate-200 hover:bg-slate-100"
    >
      <div className="flex items-center gap-2">
        <span className="inline-flex h-5 items-center rounded-md bg-slate-700 px-2.5 font-mono text-[11px] font-semibold text-slate-200">
          {action.toolName}
        </span>
        <span
          className={cn(
            "inline-flex items-center gap-1.5 text-[11px] font-semibold",
            status.tone === "success" && "text-status-success",
            status.tone === "running" && "text-status-running",
            status.tone === "failed" && "text-status-failed",
          )}
        >
          <span
            className={cn(
              "h-1.5 w-1.5 rounded-full",
              status.tone === "success" && "bg-status-success",
              status.tone === "running" && "bg-status-running",
              status.tone === "failed" && "bg-status-failed",
            )}
          />
          {status.label}
        </span>
        <span className="ml-auto text-[11px] font-semibold text-primary">Details ↗</span>
      </div>
      {action.output ? (
        <div className="mt-1.5 truncate font-mono text-[12px] leading-relaxed text-slate-700">
          {action.output}
        </div>
      ) : null}
      {action.observation ? (
        <div className="mt-1 truncate text-[12px] leading-relaxed text-slate-500">
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
}: {
  iteration: PhaseIteration
  onOpenAction: (action: PhaseAction) => void
}) {
  const hasTrace = iteration.thoughts.length > 0 || iteration.actions.length > 0
  return (
    <div className="px-3.5 py-3">
      <div className="mb-2 font-mono text-[10px] font-semibold uppercase tracking-[0.06em] text-slate-400">
        {iterationLabel(iteration)}
      </div>
      {iteration.thoughts.map((thought, index) => (
        <div className="mb-2.5 flex items-start gap-2.5" key={`think-${index}`}>
          <span className="mt-px inline-flex h-[18px] shrink-0 items-center rounded-md bg-slate-100 px-1.5 font-sans text-[10px] font-semibold uppercase tracking-[0.04em] text-slate-500">
            think
          </span>
          <span className="min-w-0 text-[13px] italic leading-relaxed text-slate-600">
            {thought}
          </span>
        </div>
      ))}
      {iteration.actions.map((action, index) => (
        <ActionRow
          action={action}
          key={`${action.toolName}-${index}`}
          onOpen={() => onOpenAction(action)}
        />
      ))}
      {!hasTrace ? (
        <p className="text-[12px] leading-relaxed text-slate-400">
          No action taken — reasoning step.
        </p>
      ) : null}
    </div>
  )
}

/** A task within a phase: a bordered card with a header and its iterations. */
function TaskCard({
  task,
  onOpenAction,
}: {
  task: PhaseTask
  onOpenAction: (action: PhaseAction) => void
}) {
  return (
    <div className="mt-2.5 overflow-hidden rounded-[10px] border border-slate-200">
      <div className="flex items-center gap-2 border-b border-slate-100 bg-slate-50 px-3.5 py-2.5">
        <span className="text-[13px] font-semibold text-slate-700">{task.title}</span>
      </div>
      {task.iterations.map((iteration, index) => (
        <IterationBlock
          iteration={iteration}
          key={`${iteration.sequence}-${iteration.iteration ?? index}`}
          onOpenAction={onOpenAction}
        />
      ))}
    </div>
  )
}

/** One phase on the vertical rail: colored dot + connecting line, the phase
 *  header (title · name · status pill · meta), and its task cards. */
function PhaseRow({
  phase,
  last,
  onOpenAction,
}: {
  phase: Phase
  last: boolean
  onOpenAction: (action: PhaseAction) => void
}) {
  const tone = phaseTone(phase.status)
  return (
    <div className="grid grid-cols-[22px_1fr] gap-x-3.5">
      <div className="flex flex-col items-center pt-0.5">
        <span
          className={cn(
            "h-3.5 w-3.5 shrink-0 rounded-full ring-4",
            tone === "success" && "bg-status-success ring-status-success-soft",
            tone === "failed" && "bg-status-failed ring-status-failed-soft",
            tone === "neutral" && "bg-slate-300 ring-slate-100",
          )}
        />
        {!last ? <div className="mt-1 w-0.5 flex-1 bg-slate-200" /> : null}
      </div>

      <div className={cn("min-w-0", last ? "pb-0" : "pb-5")}>
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[14px] font-bold text-slate-800">{phase.title}</span>
          <span className="font-mono text-[11px] text-slate-400">{phase.name}</span>
          {tone === "success" ? (
            <span className="inline-flex h-[18px] items-center rounded-full bg-status-success-soft px-2 text-[10px] font-semibold uppercase tracking-[0.04em] text-status-success">
              completed
            </span>
          ) : null}
          {tone === "failed" ? (
            <span className="inline-flex h-[18px] items-center rounded-full bg-status-failed-soft px-2 text-[10px] font-semibold uppercase tracking-[0.04em] text-status-failed">
              failed
            </span>
          ) : null}
          <span className="ml-auto font-mono text-[11px] text-slate-400">{phaseMeta(phase)}</span>
        </div>

        {phase.tasks.map((task) => (
          <TaskCard key={task.id} onOpenAction={onOpenAction} task={task} />
        ))}
      </div>
    </div>
  )
}

/**
 * Flow tab: the agent's execution trace, flow-first. A goal trunk card leads, then
 * a vertical phase timeline whose actions open the output/observation modal. Markup
 * mirrors WorkbenchDetail.dc.html lines 204–262 (the FLOW block in the AFTER template).
 */
export function FlowTab({ detail }: { detail: ExecutionSessionDetail }) {
  const [selected, setSelected] = useState<PhaseAction | null>(null)
  const context = detail.context

  if (!context) {
    return (
      <div className="rounded-lg border border-dashed border-slate-200 px-4 py-8 text-center text-[12.5px] text-slate-500">
        Context trace unavailable for this session.
      </div>
    )
  }

  const trunk = context.trunk
  const phases = context.phases ?? []
  const percent = progressPercent(trunk.progress ?? {})
  const steps = stepsText(trunk.progress ?? {})
  // Drive the trunk header from the real run outcome (was hardcoded amber): a
  // successful run reads green, a failed run red, anything in-between neutral.
  const trunkTone = phaseTone(trunk.state)

  return (
    <div>
      <div className="rounded-xl border border-slate-200 bg-white px-[18px] py-4">
        <div className="flex items-center gap-2">
          <span className="font-sans text-[11px] font-semibold uppercase tracking-[0.06em] text-slate-400">
            Agent goal
          </span>
          <span
            className={cn(
              "inline-flex items-center gap-1.5 text-[11px] font-semibold",
              trunkTone === "success" && "text-status-success",
              trunkTone === "failed" && "text-status-failed",
              trunkTone === "neutral" && "text-status-attention",
            )}
          >
            <span
              className={cn(
                "h-1.5 w-1.5 rounded-full",
                trunkTone === "success" && "bg-status-success",
                trunkTone === "failed" && "bg-status-failed",
                trunkTone === "neutral" && "bg-status-attention",
              )}
            />
            {trunk.state}
          </span>
        </div>
        <div className="mt-1.5 text-[15px] font-semibold leading-snug text-slate-700">
          {trunk.goal}
        </div>
        {trunk.summary ? (
          <div className="mt-1.5 text-[13px] leading-relaxed text-slate-500">{trunk.summary}</div>
        ) : null}
        {percent != null ? (
          <div className="mt-3 flex items-center gap-2.5">
            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-100">
              <div
                className={cn(
                  "h-full rounded-full",
                  trunkTone === "success" && "bg-status-success",
                  trunkTone === "failed" && "bg-status-failed",
                  trunkTone === "neutral" && "bg-status-attention",
                )}
                style={{ width: `${percent}%` }}
              />
            </div>
            {steps ? (
              <span className="shrink-0 font-mono text-[12px] text-slate-500">{steps}</span>
            ) : null}
          </div>
        ) : null}
      </div>

      <div className="mt-[18px]">
        {phases.length > 0 ? (
          phases.map((phase, index) => (
            <PhaseRow
              key={phase.id}
              last={index === phases.length - 1}
              onOpenAction={setSelected}
              phase={phase}
            />
          ))
        ) : (
          <div className="rounded-lg border border-dashed border-slate-200 px-4 py-8 text-center text-[12.5px] text-slate-500">
            No phases were recorded for this run.
          </div>
        )}
      </div>

      {selected ? (
        <ActionDetailModal action={selected} onClose={() => setSelected(null)} />
      ) : null}
    </div>
  )
}
