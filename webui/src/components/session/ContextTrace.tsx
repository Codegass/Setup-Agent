import {
  ChevronDown,
  ChevronRight,
  FileText,
  History,
  PanelTop,
  Sparkles,
  Target,
} from "lucide-react"
import { useState } from "react"

import type {
  ContextReference,
  ContextTrace as ContextTraceModel,
  Tone,
} from "@/api/types"
import { Badge, StatusBadge } from "@/components/common/Badge"
import { Card } from "@/components/common/Card"
import { isUsefulEvidenceStatus, statusMeta } from "@/components/common/status"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { cn } from "@/lib/utils"

type ContextRef = ContextReference | string
type Phase = ContextTraceModel["phases"][number]
type PhaseTask = Phase["tasks"][number]
type PhaseIteration = PhaseTask["iterations"][number]
type PhaseAction = PhaseIteration["actions"][number]

// Status color lives on the rail node only (the "status earns color" rule):
// the dot fill carries hue, the surrounding ring stays neutral.
const nodeFill: Record<Tone, string> = {
  neutral: "bg-slate-300",
  blue: "bg-blue-500",
  green: "bg-emerald-500",
  red: "bg-red-500",
  amber: "bg-amber-500",
}

function progressWidth(value: number): string {
  const bounded = value <= 1 ? value * 100 : value
  return `${Math.max(0, Math.min(100, bounded))}%`
}

function trunkProgress(progress: Record<string, number>) {
  const done = Number.isFinite(progress.done) ? progress.done : null
  const total = Number.isFinite(progress.total) ? progress.total : 0
  if (done === null || total <= 0) return null
  return { done, total, percent: (done / total) * 100 }
}

function refKey(ref: ContextRef): string {
  return typeof ref === "string" ? ref : ref.ref
}

function refLabel(ref: ContextRef): string {
  return typeof ref === "string" ? ref : ref.label || ref.ref
}

function refContent(ref: ContextRef): string | null {
  return typeof ref === "string" ? null : ref.content ?? null
}

function refTool(ref: ContextRef): string | null {
  return typeof ref === "string" ? null : ref.tool ?? null
}

function refLength(ref: ContextRef): number | null {
  return typeof ref === "string" ? null : ref.contentLength ?? ref.content?.length ?? null
}

function dedupeRefs(refs: ContextRef[]): ContextRef[] {
  const seen = new Set<string>()
  return refs.filter((ref) => {
    const key = refKey(ref)
    if (!key || seen.has(key)) return false
    seen.add(key)
    return true
  })
}

function formatJson(value: unknown): string {
  if (!value || typeof value !== "object") return ""
  if (!Object.keys(value).length) return ""
  return JSON.stringify(value, null, 2)
}

function numberFrom(record: Record<string, unknown>, key: string): number | null {
  const value = record[key]
  return typeof value === "number" && Number.isFinite(value) ? value : null
}

/** Compact "4,512 chars · span 6 · +2 · −9" meta line for one window. */
function windowMeta(window: NonNullable<PhaseIteration["window"]>): string {
  const parts = [`${window.totalChars.toLocaleString()} chars`]
  if (window.stepSpan != null) parts.push(`span ${window.stepSpan}`)
  const added = numberFrom(window.delta ?? {}, "added")
  const compacted = numberFrom(window.delta ?? {}, "compacted")
  if (added) parts.push(`+${added}`)
  if (compacted) parts.push(`−${compacted}`)
  return parts.join(" · ")
}

function phaseStat(progress: Record<string, number>): string {
  const iterations = progress.iterations ?? 0
  const actions = progress.actions ?? 0
  return `${iterations} iter · ${actions} action`
}

function RefChips({ refs, onOpen }: { refs: ContextRef[]; onOpen: (ref: ContextRef) => void }) {
  const items = dedupeRefs(refs)
  if (!items.length) return null
  return (
    <div className="mt-2 flex flex-wrap gap-1.5">
      {items.map((ref) =>
        refContent(ref) ? (
          <button
            className="rounded bg-blue-50 px-1.5 py-0.5 font-mono text-[10px] text-blue-700 transition-colors hover:bg-blue-100"
            key={refKey(ref)}
            onClick={(event) => {
              event.stopPropagation()
              onOpen(ref)
            }}
            type="button"
          >
            {refLabel(ref)}
          </button>
        ) : (
          <span
            className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-600"
            key={refKey(ref)}
          >
            {refLabel(ref)}
          </span>
        ),
      )}
    </div>
  )
}

/** A model reasoning step: quiet prose, marked but never loud. */
function ThoughtBlock({ text }: { text: string }) {
  return (
    <div className="flex gap-2">
      <Sparkles aria-hidden className="mt-0.5 shrink-0 text-slate-400" size={12} />
      <p className="min-w-0 whitespace-pre-wrap text-[12.5px] leading-relaxed text-slate-600">
        {text}
      </p>
    </div>
  )
}

/** A tool call: header carries status; bulky output hides behind disclosure. */
function ActionRow({
  action,
  onOpenRef,
}: {
  action: PhaseAction
  onOpenRef: (ref: ContextRef) => void
}) {
  const [showOutput, setShowOutput] = useState(false)
  const parameters = formatJson(action.parameters)
  const tone: Tone =
    action.success === true ? "green" : action.success === false ? "red" : "neutral"

  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="font-mono text-[12px] font-semibold text-slate-800">{action.toolName}</span>
        {action.success === true ? <Badge tone="green">success</Badge> : null}
        {action.success === false ? <Badge tone="red">failed</Badge> : null}
        {action.dispatchStatus ? <Badge tone="blue">{action.dispatchStatus}</Badge> : null}
      </div>

      {parameters ? (
        <pre className="overflow-x-auto whitespace-pre-wrap rounded bg-slate-50 px-2 py-1.5 font-mono text-[11px] leading-relaxed text-slate-600">
          {parameters}
        </pre>
      ) : null}

      {action.observation ? (
        <div
          className={cn(
            "rounded px-2 py-1.5 text-[12px] leading-relaxed",
            tone === "red" ? "bg-red-50/60 text-slate-700" : "bg-emerald-50/50 text-slate-700",
          )}
        >
          {action.observation}
        </div>
      ) : null}

      {action.output ? (
        <div>
          <button
            aria-expanded={showOutput}
            className="inline-flex items-center gap-1 py-0.5 font-mono text-[10.5px] text-slate-500 transition-colors hover:text-slate-700"
            onClick={() => setShowOutput((value) => !value)}
            type="button"
          >
            <ChevronRight
              aria-hidden
              className={cn("transition-transform", showOutput && "rotate-90")}
              size={11}
            />
            {showOutput ? "hide output" : `output · ${action.output.length.toLocaleString()} chars`}
          </button>
          {showOutput ? (
            <pre className="mt-1 max-h-60 overflow-auto whitespace-pre-wrap rounded bg-slate-50 px-2 py-1.5 font-mono text-[11px] leading-relaxed text-slate-600">
              {action.output}
            </pre>
          ) : null}
        </div>
      ) : null}

      <RefChips onOpen={onOpenRef} refs={action.refs} />
    </div>
  )
}

/** Collapsible view of a journal window segment (intro / attempt ledger). */
function WindowPanel({
  icon,
  label,
  text,
}: {
  icon: React.ReactNode
  label: string
  text: string
}) {
  const [open, setOpen] = useState(false)
  return (
    <div className="rounded bg-slate-50">
      <button
        aria-expanded={open}
        className="flex w-full items-center gap-1.5 px-2 py-2 text-left transition-colors hover:bg-slate-100"
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        <ChevronRight aria-hidden className={cn("text-slate-400 transition-transform", open && "rotate-90")} size={11} />
        {icon}
        <span className="font-mono text-[10px] uppercase tracking-[0.1em] text-slate-500">{label}</span>
        <span className="ml-auto font-mono text-[10px] text-slate-500">
          {text.length.toLocaleString()} chars
        </span>
      </button>
      {open ? (
        <pre className="max-h-52 overflow-auto whitespace-pre-wrap border-t border-slate-200 px-2 py-1.5 font-mono text-[11px] leading-relaxed text-slate-600">
          {text}
        </pre>
      ) : null}
    </div>
  )
}

/** One step on the iteration rail: reasoning, then tool actions, then window. */
function IterationStep({
  iteration,
  last,
  onOpenRef,
}: {
  iteration: PhaseIteration
  last: boolean
  onOpenRef: (ref: ContextRef) => void
}) {
  const label =
    iteration.iteration == null ? `entry ${iteration.sequence}` : `iter ${iteration.iteration}`
  const hasTrace =
    iteration.thoughts.length > 0 ||
    iteration.actions.length > 0 ||
    Boolean(iteration.window?.introText) ||
    Boolean(iteration.window?.ledgerText)

  return (
    <li className="relative grid grid-cols-[14px_1fr] gap-x-3">
      <div className="relative flex justify-center">
        {!last ? (
          <span aria-hidden className="absolute left-1/2 top-2.5 bottom-0 w-px -translate-x-1/2 bg-slate-200" />
        ) : null}
        <span className="relative z-10 mt-1 h-2 w-2 rounded-full bg-slate-300 ring-2 ring-white" />
      </div>

      <div className={cn("min-w-0", last ? "pb-0" : "pb-4")}>
        <div className="flex items-baseline justify-between gap-3">
          <span className="font-mono text-[11px] font-medium text-slate-600">{label}</span>
          {iteration.window ? (
            <span className="truncate font-mono text-[10px] text-slate-500">
              {windowMeta(iteration.window)}
            </span>
          ) : null}
        </div>

        <div className="mt-1.5 space-y-2">
          {iteration.thoughts.map((thought, index) => (
            <ThoughtBlock key={`thought-${index}`} text={thought} />
          ))}

          {iteration.actions.map((action, index) => (
            <ActionRow action={action} key={`${action.toolName}-${index}`} onOpenRef={onOpenRef} />
          ))}

          {iteration.window?.introText ? (
            <WindowPanel
              icon={<PanelTop aria-hidden className="text-slate-400" size={11} />}
              label="window intro"
              text={iteration.window.introText}
            />
          ) : null}
          {iteration.window?.ledgerText ? (
            <WindowPanel
              icon={<History aria-hidden className="text-slate-400" size={11} />}
              label="attempt ledger"
              text={iteration.window.ledgerText}
            />
          ) : null}

          {!hasTrace ? (
            <p className="text-[12px] leading-relaxed text-slate-500">
              No branch trace was recorded for this iteration.
            </p>
          ) : null}
        </div>
      </div>
    </li>
  )
}

function IterationTimeline({
  iterations,
  onOpenRef,
}: {
  iterations: PhaseIteration[]
  onOpenRef: (ref: ContextRef) => void
}) {
  if (!iterations.length) {
    return <p className="text-[12px] leading-relaxed text-slate-500">No iteration records.</p>
  }
  return (
    <ol className="mt-1">
      {iterations.map((iteration, index) => (
        <IterationStep
          iteration={iteration}
          key={`${iteration.sequence}-${iteration.iteration ?? index}`}
          last={index === iterations.length - 1}
          onOpenRef={onOpenRef}
        />
      ))}
    </ol>
  )
}

function TaskGroup({
  task,
  onOpenRef,
}: {
  task: PhaseTask
  onOpenRef: (ref: ContextRef) => void
}) {
  const [open, setOpen] = useState(false)
  return (
    <div className="border-t border-slate-100 pt-2">
      <button
        aria-expanded={open}
        className="flex w-full items-center gap-2 text-left"
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        <ChevronDown
          aria-hidden
          className={cn("shrink-0 text-slate-300 transition-transform", !open && "-rotate-90")}
          size={13}
        />
        <span className="font-mono text-[10px] text-slate-500">{task.id}</span>
        <span className="min-w-0 flex-1 truncate text-[12.5px] font-medium text-slate-700">
          {task.title}
        </span>
        <span className="font-mono text-[10px] text-slate-500">{task.iterations.length} iter</span>
      </button>
      {open ? <IterationTimeline iterations={task.iterations} onOpenRef={onOpenRef} /> : null}
    </div>
  )
}

function PhaseRow({
  phase,
  open,
  last,
  onToggle,
  onOpenRef,
}: {
  phase: Phase
  open: boolean
  last: boolean
  onToggle: () => void
  onOpenRef: (ref: ContextRef) => void
}) {
  const status = phase.status.trim().toLowerCase()
  const meta = statusMeta(status)
  const conflicts = phase.conflicts ?? []
  const evidenceRefs = phase.evidenceRefs ?? []
  const evidenceStatus = phase.evidenceStatus?.trim() || "unknown"
  const showEvidence = isUsefulEvidenceStatus(evidenceStatus) || conflicts.length > 0
  const allRefs = [...phase.refs, ...evidenceRefs]
  // One task per phase is the common phase-machine shape; flatten it so the
  // iterations sit one click below the phase. Task subheads appear only when
  // a phase genuinely holds more than one task.
  const flatten = phase.tasks.length === 1

  return (
    <li className="relative grid grid-cols-[20px_1fr] gap-x-2.5">
      <div className="relative flex justify-center">
        {!last ? (
          <span aria-hidden className="absolute left-1/2 top-6 bottom-0 w-px -translate-x-1/2 bg-slate-200" />
        ) : null}
        <span
          className={cn(
            "relative z-10 mt-2 h-2.5 w-2.5 rounded-full ring-4 ring-white",
            nodeFill[meta.tone],
          )}
        />
      </div>

      <div className={cn("min-w-0", last ? "pb-0" : "pb-3")}>
        <button
          aria-expanded={open}
          className="flex w-full items-center gap-2 rounded-md py-1 text-left"
          onClick={onToggle}
          type="button"
        >
          <ChevronDown
            aria-hidden
            className={cn("shrink-0 text-slate-300 transition-transform", !open && "-rotate-90")}
            size={14}
          />
          <span className="font-mono text-[10px] text-slate-500">{phase.id}</span>
          <span className="min-w-0 flex-1 truncate text-[13px] font-semibold text-slate-800">
            {phase.title}
          </span>
          <span className="hidden font-mono text-[10px] text-slate-500 sm:inline">
            {phaseStat(phase.progress)}
          </span>
          {showEvidence ? <StatusBadge dot={false} status={evidenceStatus} /> : null}
          <StatusBadge status={status} />
        </button>

        {open ? (
          <div className="mt-1.5 space-y-2.5 pl-6">
            {phase.keyResults ? (
              <p className="text-[12.5px] leading-relaxed text-slate-600">{phase.keyResults}</p>
            ) : null}
            {phase.notes ? (
              <div className="rounded bg-amber-50/50 px-2.5 py-2 text-[12px] leading-relaxed text-slate-700">
                {phase.notes}
              </div>
            ) : null}
            {conflicts.length ? (
              <div className="rounded bg-red-50/50 px-2.5 py-2">
                <span className="font-mono text-[10px] uppercase tracking-[0.1em] text-red-600">
                  Conflicts
                </span>
                <ul className="mt-1 space-y-0.5">
                  {conflicts.map((conflict, index) => (
                    <li className="text-[12px] leading-relaxed text-slate-700" key={`${conflict}-${index}`}>
                      {conflict}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            <RefChips onOpen={onOpenRef} refs={allRefs} />

            {flatten ? (
              <IterationTimeline iterations={phase.tasks[0].iterations} onOpenRef={onOpenRef} />
            ) : (
              phase.tasks.map((task) => (
                <TaskGroup key={task.id} onOpenRef={onOpenRef} task={task} />
              ))
            )}
          </div>
        ) : null}
      </div>
    </li>
  )
}

export function ContextTrace({
  ctx,
  preview = false,
}: {
  ctx: ContextTraceModel
  preview?: boolean
}) {
  const [expandedPhases, setExpandedPhases] = useState<Record<string, boolean>>({})
  const [debugOpen, setDebugOpen] = useState(false)
  const [selectedRef, setSelectedRef] = useState<ContextRef | null>(null)
  const progress = trunkProgress(ctx.trunk.progress)

  return (
    <div className="space-y-4">
      <Card className="overflow-hidden">
        <div className="border-b border-slate-100 bg-slate-50/70 px-4 py-3.5">
          <div className="flex items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-2">
              <Target aria-hidden className="text-blue-600" size={15} />
              <span className="truncate text-[13px] font-semibold text-slate-800">Trunk goal</span>
            </div>
            <StatusBadge status={ctx.trunk.state} />
          </div>
          <p className="mt-2 text-[13px] leading-relaxed text-slate-700">{ctx.trunk.goal}</p>
          {progress ? (
            <div className="mt-3 flex items-center gap-3">
              <span className="shrink-0 font-mono text-[10px] uppercase tracking-[0.1em] text-slate-500">
                Done / Total
              </span>
              <div
                aria-label="Done / Total"
                aria-valuemax={progress.total}
                aria-valuemin={0}
                aria-valuenow={progress.done}
                className="flex h-1.5 flex-1 overflow-hidden rounded-full bg-slate-200"
                role="progressbar"
              >
                <div className="h-full rounded-full bg-blue-500" style={{ width: progressWidth(progress.percent) }} />
              </div>
              <span className="shrink-0 text-right font-mono text-[11px] text-slate-500">
                {progress.done} / {progress.total}
              </span>
            </div>
          ) : null}
          {!preview && ctx.trunk.summary.trim() ? (
            <p className="mt-3 text-[12px] leading-relaxed text-slate-500">{ctx.trunk.summary}</p>
          ) : null}
        </div>

        <ol className="px-4 py-3">
          {ctx.phases.map((phase, index) => (
            <PhaseRow
              key={phase.id}
              last={index === ctx.phases.length - 1}
              onOpenRef={setSelectedRef}
              onToggle={() =>
                setExpandedPhases((current) => ({
                  ...current,
                  [phase.id]: !(current[phase.id] ?? false),
                }))
              }
              open={expandedPhases[phase.id] ?? false}
              phase={phase}
            />
          ))}
        </ol>
      </Card>

      {!preview ? (
        <>
          <Dialog onOpenChange={(open) => !open && setSelectedRef(null)} open={Boolean(selectedRef)}>
            <DialogContent className="max-h-[82vh] w-[calc(100vw-2rem)] max-w-[920px] gap-0 border-slate-200 bg-white p-0 shadow-xl">
              <DialogHeader className="border-b border-slate-100 px-4 py-3">
                <DialogTitle className="text-[13px] font-semibold text-slate-800">
                  Output preview
                </DialogTitle>
                <DialogDescription className="font-mono text-[11px] text-slate-500">
                  {selectedRef ? refLabel(selectedRef) : ""}
                  {selectedRef && refTool(selectedRef) ? ` · ${refTool(selectedRef)}` : ""}
                  {selectedRef && refLength(selectedRef) ? ` · ${refLength(selectedRef)} chars` : ""}
                </DialogDescription>
              </DialogHeader>
              <pre className="max-h-[68vh] overflow-auto whitespace-pre-wrap p-4 font-mono text-[12px] leading-relaxed text-slate-700">
                {selectedRef ? refContent(selectedRef) : ""}
              </pre>
            </DialogContent>
          </Dialog>

          <Card className="overflow-hidden">
            <button
              aria-expanded={debugOpen}
              className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left transition-colors hover:bg-slate-50"
              onClick={() => setDebugOpen((value) => !value)}
              type="button"
            >
              <span className="flex items-center gap-2 text-[13px] font-medium text-slate-600">
                <FileText aria-hidden className="text-slate-400" size={14} />
                Debug drawer · raw trace files
              </span>
              <ChevronDown
                aria-hidden
                className={cn("text-slate-300 transition-transform", debugOpen && "rotate-180")}
                size={14}
              />
            </button>
            {debugOpen ? (
              <pre className="max-h-72 overflow-auto border-t border-slate-100 bg-slate-50 p-3 font-mono text-[11px] leading-relaxed text-slate-600">
                {JSON.stringify(ctx.debug, null, 2)}
              </pre>
            ) : null}
          </Card>
        </>
      ) : null}
    </div>
  )
}
