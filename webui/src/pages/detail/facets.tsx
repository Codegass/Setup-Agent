import { Activity, Box, FileText, Layers, Sparkles, Terminal } from "lucide-react"
import type { LucideIcon } from "lucide-react"

import type { ExecutionSessionDetail, Tone } from "@/api/types"
import { BuildFacet } from "@/components/session/BuildFacet"
import { ContextTrace } from "@/components/session/ContextTrace"
import { EvidenceTimeline } from "@/components/session/EvidenceTimeline"
import { FilesDigest } from "@/components/session/FilesDigest"
import { LogsView } from "@/components/session/LogsView"
import { ReportDoc } from "@/components/session/ReportDoc"
import { TestFacet } from "@/components/session/TestFacet"

export type FacetId = "build" | "test" | "flow" | "evidence" | "files" | "report" | "logs"

export interface FacetMeta {
  id: FacetId
  label: string
  icon: LucideIcon
  count: number | null
  countTone: Tone
}

function nonZero(n: number | null | undefined): number | null {
  return typeof n === "number" && n > 0 ? n : null
}

/** Nav/section metadata for the detail pane (order matters; bodies render via <FacetBody>). */
export function buildDetailFacets(d: ExecutionSessionDetail): FacetMeta[] {
  return [
    { id: "build", label: "Build", icon: Box, count: null, countTone: "neutral" },
    { id: "test", label: "Test", icon: Activity, count: nonZero(d.test.fail), countTone: "red" },
    { id: "flow", label: "Flow", icon: Layers, count: null, countTone: "neutral" },
    { id: "evidence", label: "Evidence", icon: Sparkles, count: nonZero(d.evidence.length), countTone: "neutral" },
    { id: "files", label: "Files", icon: FileText, count: nonZero(d.files?.items.length), countTone: "neutral" },
    { id: "report", label: "Report", icon: FileText, count: null, countTone: "neutral" },
    { id: "logs", label: "Logs", icon: Terminal, count: null, countTone: "neutral" },
  ]
}

export function Empty({ label }: { label: string }) {
  return (
    <div className="rounded-lg border border-dashed border-slate-200 px-4 py-8 text-center text-[12.5px] text-slate-500">
      {label}
    </div>
  )
}

/** Renders a facet body by reusing the existing session renderers (no restyle — Phase 4/5). */
export function FacetBody({ id, detail }: { id: FacetId; detail: ExecutionSessionDetail }) {
  switch (id) {
    case "build":
      return <BuildFacet detail={detail} />
    case "test":
      return <TestFacet detail={detail} />
    case "flow":
      return detail.context ? (
        <ContextTrace ctx={detail.context} />
      ) : (
        <Empty label="Context trace unavailable for this session." />
      )
    case "evidence":
      return <EvidenceTimeline groups={detail.evidence} />
    case "files":
      return <FilesDigest digest={detail.files} />
    case "report":
      return <ReportDoc doc={detail.reportDoc} />
    case "logs":
      return <LogsView logs={detail.logs} />
  }
}
