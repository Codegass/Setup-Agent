import { Activity, Box, FileText, Layers, Sparkles, Terminal } from "lucide-react"
import type { LucideIcon } from "lucide-react"

import type { ExecutionSessionDetail, SubmitTaskResponse, Tone } from "@/api/types"
import { isLiveSessionStatus } from "@/components/common/status"
import { BuildFacet } from "@/components/session/BuildFacet"
import { ContextTrace } from "@/components/session/ContextTrace"
import { EvidenceTimeline } from "@/components/session/EvidenceTimeline"
import { FilesDigest } from "@/components/session/FilesDigest"
import { LogsView } from "@/components/session/LogsView"
import { ReportDoc } from "@/components/session/ReportDoc"
import { TestFacet } from "@/components/session/TestFacet"

import { FlowTab } from "./FlowTab"
import { OverviewTab } from "./OverviewTab"
import { TimelineTab } from "./TimelineTab"

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

function testIssues(d: ExecutionSessionDetail): number | null {
  const failed = Number.isFinite(d.test.fail) ? d.test.fail : 0
  const errors = Number.isFinite(d.test.errors) ? d.test.errors ?? 0 : 0
  return nonZero(failed + errors)
}

/** Nav/section metadata for the detail pane (order matters; bodies render via <FacetBody>). */
export function buildDetailFacets(d: ExecutionSessionDetail): FacetMeta[] {
  return [
    { id: "build", label: "Build", icon: Box, count: null, countTone: "neutral" },
    { id: "test", label: "Test", icon: Activity, count: testIssues(d), countTone: "red" },
    { id: "flow", label: "Flow", icon: Layers, count: null, countTone: "neutral" },
    { id: "evidence", label: "Evidence", icon: Sparkles, count: nonZero(d.evidence.length), countTone: "neutral" },
    { id: "files", label: "Files", icon: FileText, count: nonZero(d.files?.items.length), countTone: "neutral" },
    { id: "report", label: "Report", icon: FileText, count: null, countTone: "neutral" },
    { id: "logs", label: "Logs", icon: Terminal, count: null, countTone: "neutral" },
  ]
}

export function Empty({ label }: { label: string }) {
  return (
    <div className="rounded-lg border border-dashed border-border px-4 py-8 text-center text-[12.5px] text-muted-foreground">
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

// `trajectory` and `ci` are the two names the result band links to that this
// builder does not yet emit; Task 6 adds them here and retires `timeline`,
// `flow` and `files` along with their bodies. Until then a row pointed at one
// of them renders as plain text rather than a button that goes nowhere.
export type TabId =
  | "overview"
  | "timeline"
  | "trajectory"
  | "flow"
  | "tests"
  | "build"
  | "files"
  | "evidence"
  | "ci"
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
  // Timeline leads the panels because it is the run itself, turn by turn, and
  // for a real session it is never gated on data being present: it derives from
  // the control ledger every run writes, and a run with no ledger YET says so
  // in its own words. A demo session is the one case where there is no ledger
  // to wait for — the read models are fabricated and stand for no run, which is
  // why the builder refuses to name a session directory for one — so the tab is
  // not offered rather than left to answer "unavailable" forever.
  const tabs: TabMeta[] = [{ id: "overview", label: "Overview" }]
  if (!d.demo) {
    tabs.push({ id: "timeline", label: "Timeline" })
  }

  if (d.context) {
    tabs.push({ id: "flow", label: "Flow" })
  }

  const failing = testIssues(d)
  tabs.push({
    id: "tests",
    label: "Tests",
    ...(failing != null ? { count: failing, tone: "red" as const } : {}),
  })
  tabs.push({ id: "build", label: "Build" })

  if (nonZero(d.files?.items.length)) {
    tabs.push({ id: "files", label: "Files" })
  }
  const evidenceCount = nonZero(d.evidence.length)
  if (evidenceCount) {
    // Mirror the prior buildDetailFacets behavior + the spec's "Evidence 2"
    // inline count (neutral toned, unlike the red Tests fail count).
    tabs.push({ id: "evidence", label: "Evidence", count: evidenceCount, tone: "neutral" })
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
    case "timeline":
      return <TimelineTab live={isLiveSessionStatus(detail.status)} sessionId={detail.id} />
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
