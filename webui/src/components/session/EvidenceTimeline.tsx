import { ChevronDown, ExternalLink } from "lucide-react"
import { useState } from "react"

import type { EvidenceGroup } from "@/api/types"
import { Card } from "@/components/common/Card"
import { statusMeta } from "@/components/common/status"
import { cn } from "@/lib/utils"

const dotClasses = {
  neutral: "bg-muted-foreground",
  blue: "bg-status-running",
  green: "bg-status-success",
  red: "bg-status-failed",
  amber: "bg-status-attention",
}

const recordDotClasses: Record<string, string> = {
  success: "bg-status-success",
  pass: "bg-status-success",
  passed: "bg-status-success",
  fail: "bg-status-failed",
  failed: "bg-status-failed",
  failure: "bg-status-failed",
  info: "bg-muted-foreground",
  partial: "bg-status-attention",
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
    <div className="divide-y divide-border">
      {shown.map((group, index) => {
        const key = `${group.source}-${index}`
        const meta = statusMeta(group.status)
        const isOpen = preview ? false : (open[key] ?? true)

        return (
          <div key={key}>
            <button
              className={cn(
                "flex w-full items-center gap-3 px-4 py-2.5 text-left",
                !preview && "hover:bg-accent",
              )}
              onClick={() => {
                if (!preview) {
                  setOpen((current) => ({ ...current, [key]: !isOpen }))
                }
              }}
              type="button"
            >
              <span className={cn("h-2 w-2 shrink-0 rounded-full", dotClasses[meta.tone])} />
              <span className="w-40 shrink-0 truncate text-[13px] font-medium text-foreground">
                {group.source}
              </span>
              <span className="hidden flex-1 truncate text-[12px] text-muted-foreground sm:block">
                {group.summary}
              </span>
              <span className="ml-auto shrink-0 font-mono text-[11px] text-muted-foreground">
                {group.counts}
              </span>
              <span className="shrink-0 font-mono text-[10px] text-muted-foreground">
                {group.time}
              </span>
              {!preview ? (
                <ChevronDown
                  className={cn("shrink-0 text-muted-foreground transition-transform", isOpen && "rotate-180")}
                  size={14}
                />
              ) : null}
            </button>
            {isOpen && !preview ? (
              <div className="space-y-2 border-t border-border bg-muted px-4 py-3 pl-9">
                {group.records.length ? (
                  group.records.map((record, recordIndex) => (
                    <Card key={`${record.ref}-${recordIndex}`} className="p-3">
                      <div className="flex items-center gap-2">
                        <span
                          className={cn(
                            "h-1.5 w-1.5 rounded-full",
                            recordDotClasses[record.status.trim().toLowerCase()] ?? "bg-muted-foreground",
                          )}
                        />
                        <span className="text-[12.5px] font-medium text-foreground">
                          {record.title}
                        </span>
                        <span className="ml-auto font-mono text-[10px] text-muted-foreground">
                          {record.time}
                        </span>
                      </div>
                      <div className="mt-1.5 text-[12px] leading-relaxed text-muted-foreground">
                        {record.detail}
                      </div>
                      <div className="mt-2 flex min-w-0 items-center gap-1.5 font-mono text-[10.5px] text-status-running">
                        <ExternalLink size={11} />
                        <span className="truncate">{record.ref}</span>
                      </div>
                    </Card>
                  ))
                ) : (
                  <div className="rounded-md border border-border bg-card px-3 py-2 text-[12px] text-muted-foreground">
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
    <div className="px-4 py-8 text-center text-[13px] text-muted-foreground">
      Evidence is not available for this session.
    </div>
  )
}
