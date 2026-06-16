import { Box, Check, X } from "lucide-react"

import type { BuildSummary } from "@/api/types"
import { StatusBadge } from "@/components/common/Badge"
import { Card } from "@/components/common/Card"
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
        <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-slate-500">Build</div>
        <StatusBadge status={build.state} />
      </div>

      <div className="mt-2.5 flex items-center gap-2.5">
        <div className={cn("flex h-9 w-9 shrink-0 items-center justify-center rounded-md",
          tone === "ok" ? "bg-emerald-50 text-emerald-600"
            : tone === "bad" ? "bg-red-50 text-red-600" : "bg-slate-100 text-slate-500")}>
          {tone === "ok" ? <Check size={18} /> : tone === "bad" ? <X size={18} /> : <Box size={16} />}
        </div>
        <div className="min-w-0">
          <div className="truncate text-[14px] font-semibold text-slate-800">{line}</div>
          {meta ? <div className="font-mono text-[11px] text-slate-500">{meta}</div> : null}
        </div>
      </div>

      {(build.classCount != null || build.jarCount != null) ? (
        <div className="mt-2 flex flex-wrap gap-3 font-mono text-[11px] text-slate-600">
          {build.classCount != null ? <span>{build.classCount.toLocaleString()} classes</span> : null}
          {build.jarCount != null ? <span>{build.jarCount.toLocaleString()} JARs</span> : null}
          <span className="text-slate-500">Physical artifact scan</span>
        </div>
      ) : null}

      {onOpenDetail ? (
        <button
          aria-label="Open build details"
          className="mt-3 inline-flex items-center gap-1 font-mono text-[10.5px] text-slate-500 transition-colors hover:text-slate-700"
          onClick={onOpenDetail}
          type="button"
        >
          Details <span aria-hidden>→</span>
        </button>
      ) : null}
    </Card>
  )
}
