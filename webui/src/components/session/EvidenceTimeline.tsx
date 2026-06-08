import { ChevronDown, ExternalLink } from "lucide-react"
import { useState } from "react"

import type { EvidenceGroup } from "@/api/types"
import { Card } from "@/components/common/Card"
import { statusMeta } from "@/components/common/status"
import { cn } from "@/lib/utils"

const dotClasses = {
  neutral: "bg-slate-400",
  blue: "bg-blue-500",
  green: "bg-emerald-500",
  red: "bg-red-500",
  amber: "bg-amber-500",
}

const recordDotClasses: Record<string, string> = {
  success: "bg-emerald-500",
  pass: "bg-emerald-500",
  passed: "bg-emerald-500",
  fail: "bg-red-500",
  failed: "bg-red-500",
  failure: "bg-red-500",
  info: "bg-slate-400",
  partial: "bg-amber-500",
}

export function EvidenceTimeline({
  groups,
  preview = false,
}: {
  groups: EvidenceGroup[]
  preview?: boolean
}) {
  const [open, setOpen] = useState<Record<string, boolean>>({})
  const shown = preview ? groups.slice(0, 5) : groups

  if (!shown.length) {
    return <EmptyEvidence />
  }

  return (
    <div className="divide-y divide-slate-100">
      {shown.map((group, index) => {
        const key = `${group.source}-${index}`
        const meta = statusMeta(group.status)
        const isOpen = preview ? false : (open[key] ?? true)

        return (
          <div key={key}>
            <button
              className={cn(
                "flex w-full items-center gap-3 px-4 py-2.5 text-left",
                !preview && "hover:bg-slate-50/70",
              )}
              onClick={() => {
                if (!preview) {
                  setOpen((current) => ({ ...current, [key]: !isOpen }))
                }
              }}
              type="button"
            >
              <span className={cn("h-2 w-2 shrink-0 rounded-full", dotClasses[meta.tone])} />
              <span className="w-40 shrink-0 truncate text-[13px] font-medium text-slate-700">
                {group.source}
              </span>
              <span className="hidden flex-1 truncate text-[12px] text-slate-500 sm:block">
                {group.summary}
              </span>
              <span className="ml-auto shrink-0 font-mono text-[11px] text-slate-500">
                {group.counts}
              </span>
              <span className="shrink-0 font-mono text-[10px] text-slate-300">
                {group.time}
              </span>
              {!preview ? (
                <ChevronDown
                  className={cn("shrink-0 text-slate-300 transition-transform", isOpen && "rotate-180")}
                  size={14}
                />
              ) : null}
            </button>
            {isOpen && !preview ? (
              <div className="space-y-2 border-t border-slate-100 bg-slate-50/50 px-4 py-3 pl-9">
                {group.records.length ? (
                  group.records.map((record, recordIndex) => (
                    <Card key={`${record.ref}-${recordIndex}`} className="p-3">
                      <div className="flex items-center gap-2">
                        <span
                          className={cn(
                            "h-1.5 w-1.5 rounded-full",
                            recordDotClasses[record.status.trim().toLowerCase()] ?? "bg-slate-400",
                          )}
                        />
                        <span className="text-[12.5px] font-medium text-slate-700">
                          {record.title}
                        </span>
                        <span className="ml-auto font-mono text-[10px] text-slate-500">
                          {record.time}
                        </span>
                      </div>
                      <div className="mt-1.5 text-[12px] leading-relaxed text-slate-500">
                        {record.detail}
                      </div>
                      <div className="mt-2 flex min-w-0 items-center gap-1.5 font-mono text-[10.5px] text-blue-500">
                        <ExternalLink size={11} />
                        <span className="truncate">{record.ref}</span>
                      </div>
                    </Card>
                  ))
                ) : (
                  <div className="rounded-md border border-slate-200 bg-white px-3 py-2 text-[12px] text-slate-500">
                    No record-level evidence captured for this source.
                  </div>
                )}
              </div>
            ) : null}
          </div>
        )
      })}
    </div>
  )
}

function EmptyEvidence() {
  return (
    <div className="px-4 py-8 text-center text-[13px] text-slate-500">
      Evidence is not available for this session.
    </div>
  )
}
