import { Activity, Check, ChevronDown, Clock, Cpu, FileText, Layers, X } from "lucide-react"
import { useState } from "react"

import type { ContextMap as ContextMapModel } from "@/api/types"
import { Badge, StatusBadge } from "@/components/common/Badge"
import { Card } from "@/components/common/Card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { cn } from "@/lib/utils"

type BranchDetail =
  | { type: "pair"; label: string; value: string }
  | { type: "kv"; label: string; value: string }
  | { type: "text"; value: string }

interface TrunkProgress {
  done: number
  total: number
  percent: number
}

type ContextRef = ContextMapModel["tasks"][number]["refs"][number]

const taskIcons: Record<string, React.ReactNode> = {
  completed: <Check size={13} className="text-emerald-600" />,
  active: <Activity size={13} className="text-blue-600" />,
  pending: <Clock size={13} className="text-slate-500" />,
  failed: <X size={13} className="text-red-600" />,
}

function progressWidth(value: number): string {
  const bounded = value <= 1 ? value * 100 : value
  return `${Math.max(0, Math.min(100, bounded))}%`
}

function trunkProgress(progress: Record<string, number>): TrunkProgress | null {
  const done = numericProgressValue(progress, ["done", "completed", "complete"])
  const explicitTotal = numericProgressValue(progress, ["total"])
  const total =
    explicitTotal ??
    Object.entries(progress).reduce((sum, [key, value]) => {
      if (["done", "completed", "complete"].includes(key.toLowerCase())) {
        return sum
      }
      return sum + safeNumber(value)
    }, done ?? 0)

  if (done === null || total <= 0) {
    return null
  }

  return {
    done,
    total,
    percent: (done / total) * 100,
  }
}

function numericProgressValue(progress: Record<string, number>, keys: string[]): number | null {
  for (const key of keys) {
    const match = Object.entries(progress).find(
      ([candidate]) => candidate.toLowerCase() === key,
    )
    if (match) {
      return safeNumber(match[1])
    }
  }
  return null
}

function safeNumber(value: number): number {
  return Number.isFinite(value) ? value : 0
}

function refLabel(ref: Record<string, string>): string {
  return ref.label ?? ref.ref ?? ref.path ?? Object.entries(ref).map(([key, value]) => `${key}:${value}`).join(" ")
}

function contextRefLabel(ref: ContextRef): string {
  return typeof ref === "string" ? ref : ref.label || ref.ref
}

function contextRefKey(ref: ContextRef): string {
  return typeof ref === "string" ? ref : ref.ref
}

function contextRefContent(ref: ContextRef): string | null {
  return typeof ref === "string" ? null : ref.content ?? null
}

function contextRefLength(ref: ContextRef): number | null {
  if (typeof ref === "string") {
    return null
  }
  return ref.contentLength ?? ref.content?.length ?? null
}

function contextRefTool(ref: ContextRef): string | null {
  return typeof ref === "string" ? null : ref.tool ?? null
}

function usefulEvidenceStatus(status?: string | null): boolean {
  const normalized = status?.trim().toLowerCase()
  return Boolean(normalized && !["unknown", "none"].includes(normalized))
}

function findContextRef(refs: ContextRef[], label: string): ContextRef | null {
  return refs.find((ref) => contextRefKey(ref) === label || contextRefLabel(ref) === label) ?? null
}

function branchDetails(summary: string): BranchDetail[] {
  const details: BranchDetail[] = []
  for (const line of summary.split("\n")) {
    for (const segment of line.split(";")) {
      const text = segment.trim()
      if (!text) {
        continue
      }

      const kv = text.match(/^([A-Za-z][\w.-]*)=(.+)$/)
      if (kv) {
        details.push({ type: "kv", label: kv[1], value: kv[2].trim() })
        continue
      }

      const previousTask = text.match(/^Previous task\s+(\([^)]+\)):\s*(.+)$/i)
      if (previousTask) {
        details.push({
          type: "pair",
          label: "Previous task",
          value: `${previousTask[1]}: ${previousTask[2].trim()}`,
        })
        continue
      }

      const pair = text.match(/^([^:]{3,56}):\s+(.+)$/)
      if (pair) {
        details.push({ type: "pair", label: pair[1].trim(), value: pair[2].trim() })
        continue
      }

      details.push({ type: "text", value: text })
    }
  }
  return details
}

function InlineOutputRefs({
  refs,
  text,
  onOpenRef,
}: {
  refs: ContextRef[]
  text: string
  onOpenRef: (ref: ContextRef) => void
}) {
  const parts = text.split(/\b(output_[A-Za-z0-9_-]+)\b/g)

  return (
    <>
      {parts.map((part, index) => {
        const ref = part.startsWith("output_") ? findContextRef(refs, part) : null
        if (!ref || !contextRefContent(ref)) {
          return <span key={`${part}-${index}`}>{part}</span>
        }

        return (
          <button
            key={`${part}-${index}`}
            className="rounded bg-blue-50 px-1 py-0.5 font-mono text-[11px] text-blue-600 hover:bg-blue-100"
            onClick={(event) => {
              event.stopPropagation()
              onOpenRef(ref)
            }}
            type="button"
          >
            {part}
          </button>
        )
      })}
    </>
  )
}

function BranchDetailList({
  refs,
  summary,
  onOpenRef,
}: {
  refs: ContextRef[]
  summary: string
  onOpenRef: (ref: ContextRef) => void
}) {
  const details = branchDetails(summary)
  if (!details.length) {
    return null
  }

  return (
    <div className="space-y-2">
      {details.map((detail, index) => {
        if (detail.type === "text") {
          return (
            <p key={`${detail.value}-${index}`} className="leading-relaxed text-slate-600">
              <InlineOutputRefs onOpenRef={onOpenRef} refs={refs} text={detail.value} />
            </p>
          )
        }

        return (
          <div
            key={`${detail.label}-${detail.value}-${index}`}
            className="grid gap-1 rounded-md bg-slate-50 px-2.5 py-2 sm:grid-cols-[150px_1fr]"
          >
            <span className="font-mono text-[10.5px] uppercase tracking-[0.08em] text-slate-500">
              {detail.label}
            </span>
            <span
              className={cn(
                "min-w-0 break-words text-[12px] leading-relaxed text-slate-600",
                detail.type === "kv" && "font-mono",
              )}
            >
              <InlineOutputRefs onOpenRef={onOpenRef} refs={refs} text={detail.value} />
            </span>
          </div>
        )
      })}
    </div>
  )
}

export function ContextMap({
  ctx,
  preview = false,
}: {
  ctx: ContextMapModel
  preview?: boolean
}) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [debugOpen, setDebugOpen] = useState(false)
  const [selectedRef, setSelectedRef] = useState<ContextRef | null>(null)
  const progress = trunkProgress(ctx.trunk.progress)
  const hasActiveBranch =
    Boolean(ctx.activeBranch.task.trim()) ||
    Boolean(ctx.activeBranch.why.trim()) ||
    ctx.activeBranch.memory.length > 0 ||
    ctx.activeBranch.lastRefs.length > 0 ||
    ctx.activeBranch.pressure > 0

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
            <div className="mt-3">
              <div className="flex items-center gap-3">
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
                  <div
                    className="h-full bg-blue-500"
                    style={{ width: progressWidth(progress.percent) }}
                  />
                </div>
                <span className="w-16 shrink-0 text-right font-mono text-[11px] text-slate-500">
                  {progress.done} / {progress.total}
                </span>
              </div>
            </div>
          ) : null}
          {!preview && ctx.trunk.summary.trim() ? (
            <p className="mt-3 rounded-md bg-white p-2.5 text-[12px] leading-relaxed text-slate-500">
              {ctx.trunk.summary}
            </p>
          ) : null}
        </div>

        <div className="p-2">
          {ctx.tasks.map((task) => {
            const isOpen = expanded[task.id] ?? false
            const active = task.status.trim().toLowerCase() === "active"
            const conflicts = task.conflicts ?? []
            const evidenceRefs = task.evidenceRefs ?? []
            const allRefs = [...task.refs, ...evidenceRefs]
            const showEvidenceStatus =
              usefulEvidenceStatus(task.evidenceStatus) ||
              conflicts.length > 0 ||
              evidenceRefs.length > 0
            const evidenceStatus = task.evidenceStatus?.trim() || "unknown"
            const hasDetails =
              Boolean(task.summary.trim()) ||
              task.refs.length > 0 ||
              conflicts.length > 0 ||
              evidenceRefs.length > 0

            return (
              <div key={task.id} className={cn("rounded-md", active && "bg-blue-50/50 ring-1 ring-blue-100")}>
                <button
                  className={cn(
                    "flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-left",
                    hasDetails && "hover:bg-slate-50",
                  )}
                  onClick={() => {
                    if (hasDetails) {
                      setExpanded((current) => ({ ...current, [task.id]: !isOpen }))
                    }
                  }}
                  type="button"
                >
                  <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border border-slate-200 bg-white">
                    {taskIcons[task.status.trim().toLowerCase()] ?? <Clock size={13} className="text-slate-500" />}
                  </span>
                  <span className="font-mono text-[10px] text-slate-500">{task.id}</span>
                  <span
                    className={cn(
                      "min-w-0 truncate text-[13px]",
                      active ? "font-semibold text-slate-800" : "font-medium text-slate-600",
                    )}
                  >
                    {task.title}
                  </span>
                  {task.recovered ? <Badge tone="amber">recovered</Badge> : null}
                  {active ? <Badge tone="blue">active branch</Badge> : null}
                  {showEvidenceStatus ? <StatusBadge dot={false} status={evidenceStatus} /> : null}
                  {hasDetails ? (
                    <ChevronDown
                      className={cn("ml-auto shrink-0 text-slate-300 transition-transform", isOpen && "rotate-180")}
                      size={13}
                    />
                  ) : null}
                </button>
                {isOpen && hasDetails ? (
                  <div className="px-2.5 pb-2.5 pl-9">
                    <div className="max-h-[360px] overflow-auto rounded-md border border-slate-200 bg-white p-2.5 text-[12px] text-slate-500">
                      <BranchDetailList
                        onOpenRef={setSelectedRef}
                        refs={allRefs}
                        summary={task.summary}
                      />
                      {conflicts.length ? (
                        <div className="mt-2 rounded-md border border-red-100 bg-red-50/50 px-2.5 py-2">
                          <div className="font-mono text-[10px] uppercase tracking-[0.12em] text-red-500">
                            Conflicts
                          </div>
                          <ul className="mt-1 space-y-1">
                            {conflicts.map((conflict, index) => (
                              <li
                                key={`${conflict}-${index}`}
                                className="break-words text-[12px] leading-relaxed text-slate-600"
                              >
                                {conflict}
                              </li>
                            ))}
                          </ul>
                        </div>
                      ) : null}
                      {task.refs.length ? (
                        <div className="mt-2 flex flex-wrap gap-1.5">
                          {task.refs.map((ref) => (
                            contextRefContent(ref) ? (
                              <button
                                key={contextRefKey(ref)}
                                className="rounded bg-blue-50 px-1.5 py-0.5 font-mono text-[10px] text-blue-600 hover:bg-blue-100"
                                onClick={(event) => {
                                  event.stopPropagation()
                                  setSelectedRef(ref)
                                }}
                                type="button"
                              >
                                {contextRefLabel(ref)}
                              </button>
                            ) : (
                              <span
                                key={contextRefKey(ref)}
                                className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-500"
                              >
                                {contextRefLabel(ref)}
                              </span>
                            )
                          ))}
                        </div>
                      ) : null}
                      {evidenceRefs.length ? (
                        <div className="mt-2">
                          <div className="mb-1 font-mono text-[10px] uppercase tracking-[0.12em] text-slate-400">
                            Evidence refs
                          </div>
                          <div className="flex flex-wrap gap-1.5">
                            {evidenceRefs.map((ref) => (
                              contextRefContent(ref) ? (
                                <button
                                  key={contextRefKey(ref)}
                                  className="rounded bg-blue-50 px-1.5 py-0.5 font-mono text-[10px] text-blue-600 hover:bg-blue-100"
                                  onClick={(event) => {
                                    event.stopPropagation()
                                    setSelectedRef(ref)
                                  }}
                                  type="button"
                                >
                                  {contextRefLabel(ref)}
                                </button>
                              ) : (
                                <span
                                  key={contextRefKey(ref)}
                                  className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-500"
                                >
                                  {contextRefLabel(ref)}
                                </span>
                              )
                            ))}
                          </div>
                        </div>
                      ) : null}
                    </div>
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
                  {selectedRef ? contextRefLabel(selectedRef) : ""}
                  {selectedRef && contextRefTool(selectedRef) ? ` · ${contextRefTool(selectedRef)}` : ""}
                  {selectedRef && contextRefLength(selectedRef)
                    ? ` · ${contextRefLength(selectedRef)} chars`
                    : ""}
                </DialogDescription>
              </DialogHeader>
              <pre className="max-h-[68vh] overflow-auto whitespace-pre-wrap break-words bg-slate-950 p-4 font-mono text-[11.5px] leading-relaxed text-slate-100">
                {selectedRef ? contextRefContent(selectedRef) : ""}
              </pre>
            </DialogContent>
          </Dialog>

          {hasActiveBranch ? (
            <Card className="p-4">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="flex min-w-0 items-center gap-2">
                <Activity size={14} className="text-blue-600" />
                <span className="truncate text-[13px] font-semibold text-slate-800">
                  Active Branch Focus
                </span>
              </div>
              <div className="flex items-center gap-2">
                <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-slate-500">
                  context pressure
                </span>
                <div className="h-1.5 w-16 overflow-hidden rounded-full bg-slate-100">
                  <div
                    className="h-full bg-amber-500"
                    style={{ width: progressWidth(ctx.activeBranch.pressure) }}
                  />
                </div>
                <span className="font-mono text-[11px] text-slate-500">
                  {Math.round((ctx.activeBranch.pressure <= 1 ? ctx.activeBranch.pressure * 100 : ctx.activeBranch.pressure))}%
                </span>
              </div>
            </div>
            <div className="mt-2.5 text-[14px] font-medium text-slate-800">
              {ctx.activeBranch.task}
            </div>
            <div className="mt-1 text-[12.5px] text-slate-500">{ctx.activeBranch.why}</div>
            {ctx.activeBranch.memory.length ? (
              <>
                <div className="mt-3.5 font-mono text-[10px] uppercase tracking-[0.12em] text-slate-500">
                  recent branch memory
                </div>
                <ul className="mt-1.5 space-y-1">
                  {ctx.activeBranch.memory.map((memory, index) => (
                    <li key={`${memory}-${index}`} className="flex gap-2 text-[12px] text-slate-600">
                      <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-slate-300" />
                      {memory}
                    </li>
                  ))}
                </ul>
              </>
            ) : null}
            {ctx.activeBranch.lastRefs.length ? (
              <>
                <div className="mt-3.5 font-mono text-[10px] uppercase tracking-[0.12em] text-slate-500">
                  last tool / evidence references
                </div>
                <div className="mt-1.5 flex flex-wrap gap-1.5">
                  {ctx.activeBranch.lastRefs.map((ref, index) => (
                    <span
                      key={`${refLabel(ref)}-${index}`}
                      className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2 py-1 font-mono text-[10.5px] text-slate-500"
                    >
                      <FileText size={11} className="text-blue-500" />
                      {refLabel(ref)}
                    </span>
                  ))}
                </div>
              </>
            ) : null}
            </Card>
          ) : null}

          <Card className="overflow-hidden">
            <button
              className="flex w-full items-center gap-2 px-4 py-2.5 text-left hover:bg-slate-50"
              onClick={() => setDebugOpen((current) => !current)}
              type="button"
            >
              <Cpu size={14} className="text-slate-500" />
              <span className="text-[12.5px] font-medium text-slate-600">
                Debug drawer - raw trunk/branch context
              </span>
              <ChevronDown
                className={cn("ml-auto text-slate-300 transition-transform", debugOpen && "rotate-180")}
                size={14}
              />
            </button>
            {debugOpen ? (
              <pre className="max-h-80 overflow-auto border-t border-slate-100 bg-slate-50/70 px-4 py-3 font-mono text-[11px] leading-relaxed text-slate-500">
                {JSON.stringify(ctx.debug, null, 2)}
              </pre>
            ) : null}
          </Card>
        </>
      ) : null}
    </div>
  )
}
