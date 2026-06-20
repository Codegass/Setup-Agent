import { Activity, Box, FileText, Layers, Sparkles, Terminal } from "lucide-react"
import type { LucideIcon } from "lucide-react"

import type { ExecutionSessionDetail, SubmitTaskResponse, Tone } from "@/api/types"
import { BuildFacet } from "@/components/session/BuildFacet"
import { ContextTrace } from "@/components/session/ContextTrace"
import { EvidenceTimeline } from "@/components/session/EvidenceTimeline"
import { FilesDigest } from "@/components/session/FilesDigest"
import { LogsView } from "@/components/session/LogsView"
import { ReportDoc } from "@/components/session/ReportDoc"
import { TestFacet } from "@/components/session/TestFacet"

import { FlowTab } from "./FlowTab"
import { OverviewTab } from "./OverviewTab"

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

// ── Tab model (replaces the facet/scroll-spy nav; wired in DetailPane in Task 12) ──

export type TabId =
  | "overview"
  | "flow"
  | "tests"
  | "build"
  | "files"
  | "evidence"
  | "logs"
  | "report"

export interface TabMeta {
  id: TabId
  label: string
  /** Items needing attention (rendered as a badge); omitted when there's nothing to flag. */
  count?: number
  tone?: "red" | "neutral"
}

/**
 * Tab metadata for the redesigned detail pane. `overview` always leads; `tests`/`build`
 * are core and always present; `flow` and the supplementary panels appear only when their
 * data exists (mirroring `buildDetailFacets` gating). Order matches the design template.
 */
export function buildDetailTabs(d: ExecutionSessionDetail): TabMeta[] {
  const tabs: TabMeta[] = [{ id: "overview", label: "Overview" }]

  if (d.context) {
    tabs.push({ id: "flow", label: "Flow" })
  }

  const failing = nonZero(d.test.fail)
  tabs.push({
    id: "tests",
    label: "Tests",
    ...(failing != null ? { count: failing, tone: "red" as const } : {}),
  })
  tabs.push({ id: "build", label: "Build" })

  if (nonZero(d.files?.items.length)) {
    tabs.push({ id: "files", label: "Files" })
  }
  if (nonZero(d.evidence.length)) {
    tabs.push({ id: "evidence", label: "Evidence" })
  }
  if (nonZero(d.logs.length)) {
    tabs.push({ id: "logs", label: "Logs" })
  }
  if (d.reportDoc) {
    tabs.push({ id: "report", label: "Report" })
  }

  return tabs
}

export interface TabBodyProps {
  tabId: TabId
  detail: ExecutionSessionDetail
  /** Used by the Overview goal button to jump into the Flow tab. */
  onOpenFlow?: () => void
  onSubmitTask?: (
    workspaceId: string,
    task: string,
    sourceSession?: string,
  ) => Promise<SubmitTaskResponse>
}

/**
 * Renders the panel body for a tab. `overview`/`flow` use the dedicated OverviewTab/FlowTab
 * panels; the rest delegate to the existing session renderers. `onOpenFlow` lets the Overview
 * goal button jump into the Flow tab.
 */
export function TabBody({ tabId, detail, onOpenFlow }: TabBodyProps) {
  switch (tabId) {
    case "overview":
      return <OverviewTab detail={detail} onOpenFlow={onOpenFlow ?? (() => {})} />
    case "flow":
      return <FlowTab detail={detail} />
    case "tests":
      return <TestFacet detail={detail} />
    case "build":
      return <BuildFacet detail={detail} />
    case "files":
      return <FilesDigest digest={detail.files} />
    case "evidence":
      return <EvidenceTimeline groups={detail.evidence} />
    case "logs":
      return <LogsView logs={detail.logs} />
    case "report":
      return <ReportDoc doc={detail.reportDoc} />
  }
}
