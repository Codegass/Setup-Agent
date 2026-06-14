import { Activity, Check, ChevronDown, Clock, FileText, Layers, X } from "lucide-react"
import { useState } from "react"

import type { ContextReference, ContextTrace as ContextTraceModel } from "@/api/types"
import { Badge, StatusBadge } from "@/components/common/Badge"
import { Card } from "@/components/common/Card"
import { isUsefulEvidenceStatus } from "@/components/common/status"
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

const statusIcons: Record<string, React.ReactNode> = {
  completed: <Check size={13} className="text-emerald-600" />,
  active: <Activity size={13} className="text-blue-600" />,
  in_progress: <Activity size={13} className="text-blue-600" />,
  running: <Activity size={13} className="text-blue-600" />,
  pending: <Clock size={13} className="text-slate-500" />,
  failed: <X size={13} className="text-red-600" />,
  blocked: <X size={13} className="text-red-600" />,
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

function phaseStats(phase: Phase): string {
  const iterations = phase.progress.iterations ?? 0
  const thoughts = phase.progress.thoughts ?? 0
  const actions = phase.progress.actions ?? 0
  return `${iterations} iter / ${thoughts} thought / ${actions} action`
}

function refsButtons(refs: ContextRef[], onOpen: (ref: ContextRef) => void) {
  const items = dedupeRefs(refs)
  if (!items.length) return null
  return (
    <div className="mt-2 flex flex-wrap gap-1.5">
      {items.map((ref) =>
        refContent(ref) ? (
          <button
            key={refKey(ref)}
            className="rounded bg-blue-50 px-1.5 py-0.5 font-mono text-[10px] text-blue-600 hover:bg-blue-100"
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
            key={refKey(ref)}
            className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-500"
          >
            {refLabel(ref)}
          </span>
        ),
      )}
    </div>
  )
}

function IterationCard({
  iteration,
  onOpenRef,
}: {
  iteration: PhaseIteration
  onOpenRef: (ref: ContextRef) => void
}) {
  const label = iteration.iteration == null ? `entry ${iteration.sequence}` : `iter ${iteration.iteration}`
  return (
    <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2">
      <div className="flex items-center justify-between gap-3">
        <div className="font-mono text-[11px] font-medium text-slate-600">{label}</div>
        {iteration.window ? (
          <div className="font-mono text-[10px] text-slate-400">
            {iteration.window.totalChars} chars
            {iteration.window.stepSpan ? ` / span ${iteration.window.stepSpan}` : ""}
          </div>
        ) : null}
      </div>

      {iteration.thoughts.map((thought, index) => (
        <pre
          className="mt-2 whitespace-pre-wrap rounded border border-slate-200 bg-white p-2 text-[12px] leading-relaxed text-slate-600"
          key={`thought-${index}`}
        >
          {thought}
        </pre>
      ))}

      {iteration.actions.map((action, index) => {
        const parameters = formatJson(action.parameters)
        return (
          <div className="mt-2 rounded-md border border-slate-200 bg-white p-2" key={`${action.toolName}-${index}`}>
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-slate-400">
                action
              </span>
              <span className="font-mono text-[11px] font-semibold text-slate-700">{action.toolName}</span>
              {action.success === true ? <Badge tone="green">success</Badge> : null}
              {action.success === false ? <Badge tone="red">failed</Badge> : null}
              {action.dispatchStatus ? <Badge>{action.dispatchStatus}</Badge> : null}
            </div>
            {parameters ? (
              <pre className="mt-2 whitespace-pre-wrap rounded border border-slate-200 bg-slate-50 p-2 font-mono text-[11px] leading-relaxed text-slate-600">
                {parameters}
              </pre>
            ) : null}
            {action.observation ? (
              <div className="mt-2 rounded border border-emerald-100 bg-emerald-50/40 px-2 py-1.5 text-[12px] leading-relaxed text-slate-600">
                {action.observation}
              </div>
            ) : null}
            {action.output ? (
              <pre className="mt-2 max-h-52 overflow-auto whitespace-pre-wrap rounded border border-slate-200 bg-slate-50 p-2 font-mono text-[11px] leading-relaxed text-slate-600">
                {action.output}
              </pre>
            ) : null}
            {refsButtons(action.refs, onOpenRef)}
          </div>
        )
      })}

      {iteration.window?.introText ? (
        <pre className="mt-2 max-h-44 overflow-auto whitespace-pre-wrap rounded border border-slate-200 bg-white p-2 font-mono text-[11px] leading-relaxed text-slate-600">
          {iteration.window.introText}
        </pre>
      ) : null}
      {iteration.window?.ledgerText ? (
        <pre className="mt-2 max-h-44 overflow-auto whitespace-pre-wrap rounded border border-slate-200 bg-white p-2 font-mono text-[11px] leading-relaxed text-slate-600">
          {iteration.window.ledgerText}
        </pre>
      ) : null}
    </div>
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
  const [expandedTasks, setExpandedTasks] = useState<Record<string, boolean>>({})
  const [debugOpen, setDebugOpen] = useState(false)
  const [selectedRef, setSelectedRef] = useState<ContextRef | null>(null)
  const progress = trunkProgress(ctx.trunk.progress)

  return (
    <div className="space-y-4">
      <Card className="overflow-hidden">
        <div className="border-b border-slate-100 bg-slate-50/70 px-4 py-3.5">
          <div className="flex items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-2">
              <Layers size={15} className="text-blue-600" />
              <span className="truncate text-[13px] font-semibold text-slate-800">
                Trunk - Command Center
              </span>
            </div>
            <StatusBadge status={ctx.trunk.state} />
          </div>
          <p className="mt-2 text-[13px] leading-relaxed text-slate-600">{ctx.trunk.goal}</p>
          {progress ? (
            <div className="mt-3 flex items-center gap-3">
              <span className="w-24 shrink-0 truncate font-mono text-[10px] uppercase tracking-[0.12em] text-slate-500">
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
                <div className="h-full bg-blue-500" style={{ width: progressWidth(progress.percent) }} />
              </div>
              <span className="w-16 shrink-0 text-right font-mono text-[11px] text-slate-500">
                {progress.done} / {progress.total}
              </span>
            </div>
          ) : null}
          {!preview && ctx.trunk.summary.trim() ? (
            <p className="mt-3 rounded-md bg-white p-2.5 text-[12px] leading-relaxed text-slate-500">
              {ctx.trunk.summary}
            </p>
          ) : null}
        </div>

        <div className="p-2">
          {ctx.phases.map((phase) => {
            const phaseOpen = expandedPhases[phase.id] ?? false
            const status = phase.status.trim().toLowerCase()
            const conflicts = phase.conflicts ?? []
            const evidenceRefs = phase.evidenceRefs ?? []
            const evidenceStatus = phase.evidenceStatus?.trim() || "unknown"
            const showEvidenceStatus = isUsefulEvidenceStatus(evidenceStatus) || conflicts.length > 0
            return (
              <div className={cn("rounded-md", status === "in_progress" && "bg-blue-50/50 ring-1 ring-blue-100")} key={phase.id}>
                <button
                  className="flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-left hover:bg-slate-50"
                  onClick={() => setExpandedPhases((current) => ({ ...current, [phase.id]: !phaseOpen }))}
                  type="button"
                >
                  <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border border-slate-200 bg-white">
                    {statusIcons[status] ?? <Clock size={13} className="text-slate-500" />}
                  </span>
                  <span className="font-mono text-[10px] text-slate-500">{phase.id}</span>
                  <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-slate-700">
                    {phase.title}
                  </span>
                  <span className="hidden font-mono text-[10px] text-slate-400 md:inline">{phaseStats(phase)}</span>
                  {showEvidenceStatus ? <StatusBadge dot={false} status={evidenceStatus} /> : null}
                  <ChevronDown
                    className={cn("shrink-0 text-slate-300 transition-transform", phaseOpen && "rotate-180")}
                    size={13}
                  />
                </button>

                {phaseOpen ? (
                  <div className="space-y-2 px-2.5 pb-2.5 pl-9">
                    {phase.keyResults ? (
                      <div className="rounded-md border border-slate-200 bg-white px-2.5 py-2 text-[12px] leading-relaxed text-slate-600">
                        {phase.keyResults}
                      </div>
                    ) : null}
                    {phase.notes ? (
                      <div className="rounded-md border border-amber-100 bg-amber-50/40 px-2.5 py-2 text-[12px] leading-relaxed text-slate-600">
                        {phase.notes}
                      </div>
                    ) : null}
                    {conflicts.length ? (
                      <div className="rounded-md border border-red-100 bg-red-50/50 px-2.5 py-2">
                        <div className="font-mono text-[10px] uppercase tracking-[0.12em] text-red-500">
                          Conflicts
                        </div>
                        <ul className="mt-1 space-y-1">
                          {conflicts.map((conflict, index) => (
                            <li className="text-[12px] leading-relaxed text-slate-600" key={`${conflict}-${index}`}>
                              {conflict}
                            </li>
                          ))}
                        </ul>
                      </div>
                    ) : null}
                    {refsButtons([...phase.refs, ...evidenceRefs], setSelectedRef)}

                    {phase.tasks.map((task) => {
                      const taskOpen = expandedTasks[task.id] ?? false
                      return (
                        <div className="rounded-md border border-slate-200 bg-white" key={task.id}>
                          <button
                            className="flex w-full items-center gap-2 px-2.5 py-2 text-left hover:bg-slate-50"
                            onClick={() => setExpandedTasks((current) => ({ ...current, [task.id]: !taskOpen }))}
                            type="button"
                          >
                            <span className="font-mono text-[10px] text-slate-400">{task.id}</span>
                            <span className="min-w-0 flex-1 truncate text-[12.5px] font-medium text-slate-700">
                              {task.title}
                            </span>
                            <span className="font-mono text-[10px] text-slate-400">
                              {task.iterations.length} iter
                            </span>
                            <ChevronDown
                              className={cn("shrink-0 text-slate-300 transition-transform", taskOpen && "rotate-180")}
                              size={13}
                            />
                          </button>
                          {taskOpen ? (
                            <div className="space-y-2 border-t border-slate-100 p-2.5">
                              {task.iterations.length ? (
                                task.iterations.map((iteration) => (
                                  <IterationCard
                                    iteration={iteration}
                                    key={`${task.id}-${iteration.sequence}`}
                                    onOpenRef={setSelectedRef}
                                  />
                                ))
                              ) : (
                                <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-[12px] text-slate-500">
                                  No iteration records.
                                </div>
                              )}
                            </div>
                          ) : null}
                        </div>
                      )
                    })}
                  </div>
                ) : null}
              </div>
            )
          })}
        </div>
      </Card>

      {!preview ? (
        <>
          <Dialog open={Boolean(selectedRef)} onOpenChange={(open) => !open && setSelectedRef(null)}>
            <DialogContent className="max-h-[82vh] w-[calc(100vw-2rem)] max-w-[920px] gap-0 border-slate-200 bg-white p-0 shadow-xl">
              <DialogHeader className="border-b border-slate-100 px-4 py-3">
                <DialogTitle className="text-[13px] font-semibold text-slate-800">
                  Output preview
                </DialogTitle>
                <DialogDescription className="font-mono text-[11px] text-slate-500">
                  {selectedRef ? refLabel(selectedRef) : ""}
                  {selectedRef && refTool(selectedRef) ? ` - ${refTool(selectedRef)}` : ""}
                  {selectedRef && refLength(selectedRef) ? ` - ${refLength(selectedRef)} chars` : ""}
                </DialogDescription>
              </DialogHeader>
              <pre className="max-h-[68vh] overflow-auto whitespace-pre-wrap p-4 font-mono text-[12px] leading-relaxed text-slate-700">
                {selectedRef ? refContent(selectedRef) : ""}
              </pre>
            </DialogContent>
          </Dialog>

          <Card className="overflow-hidden">
            <button
              className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left hover:bg-slate-50"
              onClick={() => setDebugOpen((value) => !value)}
              type="button"
            >
              <span className="flex items-center gap-2 text-[13px] font-medium text-slate-600">
                <FileText size={14} className="text-slate-400" />
                Debug drawer - raw trace files
              </span>
              <ChevronDown
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
