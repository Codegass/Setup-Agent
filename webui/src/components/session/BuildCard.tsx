import { Box, Check, X } from "lucide-react"

import type { BuildSummary } from "@/api/types"
import { StatusBadge } from "@/components/common/Badge"
import { Card } from "@/components/common/Card"
import { Tooltip } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"

function normalized(status: string): string {
  return status.trim().toLowerCase()
}

function conclusion(build: BuildSummary): { line: string; tone: "ok" | "bad" | "unknown" } {
  const hasArtifacts = (build.classCount ?? 0) > 0 || (build.jarCount ?? 0) > 0
  const known = build.classCount != null || build.jarCount != null
  const state = normalized(build.state)
  if (hasArtifacts) return { line: "Artifacts verified", tone: "ok" }
  if (known || state === "failed" || state === "failure") {
    return { line: "No build artifacts found", tone: "bad" }
  }
  return { line: "Build evidence unavailable", tone: "unknown" }
}

export function BuildCard({
  build,
  onOpenDetail,
}: {
  build: BuildSummary
  onOpenDetail?: () => void
}) {
  const { line, tone } = conclusion(build)
  const meta = [build.system ?? build.tool, build.moduleOutputCount != null
    ? `${build.moduleOutputCount} modules` : null].filter((v) => v && v !== "—").join(" · ")

  return (
    <Card className="p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">Build</div>
        <StatusBadge status={build.state} />
      </div>

      <div className="mt-2.5 flex items-center gap-2.5">
        <div className={cn("flex h-9 w-9 shrink-0 items-center justify-center rounded-md",
          tone === "ok" ? "bg-status-success-soft text-status-success"
            : tone === "bad" ? "bg-status-failed-soft text-status-failed" : "bg-muted text-muted-foreground")}>
          {tone === "ok" ? <Check size={18} /> : tone === "bad" ? <X size={18} /> : <Box size={16} />}
        </div>
        <div className="min-w-0">
          <div className="truncate text-[14px] font-semibold text-foreground">{line}</div>
          {meta ? <div className="font-mono text-[11px] text-muted-foreground">{meta}</div> : null}
        </div>
      </div>

      {(build.classCount != null || build.jarCount != null) ? (
        <div className="mt-2 flex flex-wrap gap-3 font-mono text-[11px] text-muted-foreground">
          {build.classCount != null ? <span>{build.classCount.toLocaleString()} classes</span> : null}
          {build.jarCount != null ? <span>{build.jarCount.toLocaleString()} JARs</span> : null}
          <span className="text-muted-foreground">Physical artifact scan</span>
        </div>
      ) : null}

      {onOpenDetail ? (
        <Tooltip className="mt-3" label="Open the full build breakdown">
          <button
            aria-label="Open build details"
            className="inline-flex items-center gap-1 font-mono text-[10.5px] text-muted-foreground transition-colors hover:text-foreground"
            onClick={onOpenDetail}
            type="button"
          >
            Details <span aria-hidden>→</span>
          </button>
        </Tooltip>
      ) : null}
    </Card>
  )
}
