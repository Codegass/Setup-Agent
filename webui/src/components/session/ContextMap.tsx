import { Activity, Check, ChevronDown, Clock, Cpu, FileText, Layers, X } from "lucide-react"
import { useState } from "react"

import type { ContextMap as ContextMapModel } from "@/api/types"
import { Badge, StatusBadge } from "@/components/common/Badge"
import { Card } from "@/components/common/Card"
import { cn } from "@/lib/utils"

const taskIcons: Record<string, React.ReactNode> = {
  completed: <Check size={13} className="text-emerald-600" />,
  active: <Activity size={13} className="text-blue-600" />,
  pending: <Clock size={13} className="text-slate-400" />,
  failed: <X size={13} className="text-red-600" />,
}

function progressWidth(value: number): string {
  const bounded = value <= 1 ? value * 100 : value
  return `${Math.max(0, Math.min(100, bounded))}%`
}

function refLabel(ref: Record<string, string>): string {
  return ref.label ?? ref.ref ?? ref.path ?? Object.entries(ref).map(([key, value]) => `${key}:${value}`).join(" ")
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
  const progressEntries = Object.entries(ctx.trunk.progress)
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
          {progressEntries.length ? (
            <div className="mt-3 space-y-2">
              {progressEntries.map(([key, value]) => (
                <div key={key} className="flex items-center gap-3">
                  <span className="w-24 shrink-0 truncate font-mono text-[10px] uppercase tracking-[0.12em] text-slate-400">
                    {key}
                  </span>
                  <div className="flex h-1.5 flex-1 overflow-hidden rounded-full bg-slate-200">
                    <div className="h-full bg-blue-500" style={{ width: progressWidth(value) }} />
                  </div>
                  <span className="w-12 shrink-0 text-right font-mono text-[11px] text-slate-500">
                    {value}
                  </span>
                </div>
              ))}
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
            const hasDetails = Boolean(task.summary.trim()) || task.refs.length > 0

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
                    {taskIcons[task.status.trim().toLowerCase()] ?? <Clock size={13} className="text-slate-400" />}
                  </span>
                  <span className="font-mono text-[10px] text-slate-300">{task.id}</span>
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
                  {hasDetails ? (
                    <ChevronDown
                      className={cn("ml-auto shrink-0 text-slate-300 transition-transform", isOpen && "rotate-180")}
                      size={13}
                    />
                  ) : null}
                </button>
                {isOpen && hasDetails ? (
                  <div className="px-2.5 pb-2.5 pl-9">
                    <div className="rounded-md border border-slate-200 bg-white p-2.5 text-[12px] text-slate-500">
                      {task.summary}
                      {task.refs.length ? (
                        <div className="mt-2 flex flex-wrap gap-1.5">
                          {task.refs.map((ref) => (
                            <span
                              key={ref}
                              className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-500"
                            >
                              {ref}
                            </span>
                          ))}
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
                <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-slate-400">
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
                <div className="mt-3.5 font-mono text-[10px] uppercase tracking-[0.12em] text-slate-400">
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
                <div className="mt-3.5 font-mono text-[10px] uppercase tracking-[0.12em] text-slate-400">
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
              <Cpu size={14} className="text-slate-400" />
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
