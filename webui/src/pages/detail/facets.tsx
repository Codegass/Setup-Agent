import type { ExecutionSessionDetail, SubmitTaskResponse } from "@/api/types"
import { isLiveSessionStatus } from "@/components/common/status"
import { BuildFacet } from "@/components/session/BuildFacet"
import { EvidenceTimeline } from "@/components/session/EvidenceTimeline"
import { LogsView } from "@/components/session/LogsView"
import { ReportDoc } from "@/components/session/ReportDoc"
import { TestFacet } from "@/components/session/TestFacet"

import { OfficialCITab } from "./OfficialCITab"
import { OverviewTab } from "./OverviewTab"
import { TrajectoryTab } from "./TrajectoryTab"

function nonZero(n: number | null | undefined): number | null {
  return typeof n === "number" && n > 0 ? n : null
}

function testIssues(d: ExecutionSessionDetail): number | null {
  const failed = Number.isFinite(d.test.fail) ? d.test.fail : 0
  const errors = Number.isFinite(d.test.errors) ? d.test.errors ?? 0 : 0
  return nonZero(failed + errors)
}

// ── The pane's one nav model: a tab swaps the panel below it ──

export type TabId =
  | "overview"
  | "trajectory"
  | "tests"
  | "build"
  | "ci"
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
 * One reading order: what happened, how it happened, then each measurement's
 * own evidence. `overview` always leads and `tests`/`build` are always present;
 * the rest are offered only when the run produced something for them.
 */
export function buildDetailTabs(d: ExecutionSessionDetail): TabMeta[] {
  // Trajectory leads the panels because it is the run itself, turn by turn, and
  // for a real session it is never gated on data being present: it derives from
  // the control ledger every run writes, and a run with no ledger YET says so
  // in its own words. A demo session is the one case where there is no ledger
  // to wait for — the read models are fabricated and stand for no run, which is
  // why the builder refuses to name a session directory for one — so the tab is
  // not offered rather than left to answer "unavailable" forever.
  const tabs: TabMeta[] = [{ id: "overview", label: "Overview" }]
  if (!d.demo) {
    tabs.push({ id: "trajectory", label: "Trajectory" })
  }

  const failing = testIssues(d)
  tabs.push({
    id: "tests",
    label: "Tests",
    ...(failing != null ? { count: failing, tone: "red" as const } : {}),
  })
  tabs.push({ id: "build", label: "Build" })

  // Presence, not status: a comparison the run served says inside the tab what
  // it measured, or that it measured nothing. Only a run that served none at
  // all has nothing to open — and the result band's CI row links here, so a
  // status gate would strand that row on a tab this run does not have.
  if (d.ciComparison) {
    tabs.push({ id: "ci", label: "Official CI" })
  }

  // Grouped evidence when the run built it, and otherwise the receipts it
  // recorded. Neither is defaulted: a run with neither gets no tab.
  const evidenceCount = nonZero(d.evidence.length) ?? nonZero(d.receipts?.length)
  if (evidenceCount) {
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
  /** Used by the Overview goal button to jump into the Trajectory tab. Omitted
   *  when this run has no such tab, so the button is not offered at all. */
  onOpenFlow?: () => void
  onSubmitTask?: (
    workspaceId: string,
    task: string,
    sourceSession?: string,
  ) => Promise<SubmitTaskResponse>
}

/**
 * Renders the panel body for a tab. `overview` and `trajectory` use the dedicated
 * OverviewTab/TrajectoryTab panels; the rest delegate to the existing session
 * renderers. `onOpenFlow` lets the Overview goal button jump into Trajectory.
 */
export function TabBody({ tabId, detail, onOpenFlow }: TabBodyProps) {
  switch (tabId) {
    case "overview":
      return <OverviewTab detail={detail} onOpenFlow={onOpenFlow} />
    case "trajectory":
      return <TrajectoryTab live={isLiveSessionStatus(detail.status)} sessionId={detail.id} />
    case "tests":
      return <TestFacet detail={detail} />
    case "build":
      return <BuildFacet detail={detail} />
    case "ci":
      return detail.ciComparison ? <OfficialCITab comparison={detail.ciComparison} /> : null
    case "evidence":
      return <EvidenceTimeline groups={detail.evidence} />
    case "logs":
      return <LogsView logs={detail.logs} />
    case "report":
      return <ReportDoc doc={detail.reportDoc} />
  }
}
