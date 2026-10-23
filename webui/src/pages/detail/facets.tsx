import type { ExecutionSessionDetail, SubmitTaskResponse } from "@/api/types"
import { BuildFacet } from "@/components/session/BuildFacet"
import { EvidenceTimeline } from "@/components/session/EvidenceTimeline"
import { ReceiptTable } from "@/components/session/ReceiptTable"
import { LogsView } from "@/components/session/LogsView"
import { ReportDoc } from "@/components/session/ReportDoc"
import { TestFacet } from "@/components/session/TestFacet"

import { OfficialCITab } from "./OfficialCITab"
import { FlowTab, RecordedFlowTab } from "./FlowTab"
import { OverviewTab } from "./OverviewTab"

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
  | "flow"
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
 * own evidence. Overview, Flow, Tests and Build are always present;
 * the rest are offered only when the run produced something for them.
 */
export function buildDetailTabs(d: ExecutionSessionDetail): TabMeta[] {
  // One process view: ledger order, enriched with the recorded phase context.
  const tabs: TabMeta[] = [
    { id: "overview", label: "Overview" },
    { id: "flow", label: "Flow" },
  ]

  const failing = testIssues(d)
  tabs.push({
    id: "tests",
    label: "Tests",
    ...(failing != null ? { count: failing, tone: "red" as const } : {}),
  })
  tabs.push({ id: "build", label: "Build" })

  // A normal run can carry a no_matched_cell placeholder without a CI input.
  // Hide that optional surface when the card explicitly says none was supplied.
  // Keep older records and unusable supplied references inspectable.
  if (d.ciComparison && d.resultCard?.dimensions?.hasCiReference !== false) {
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
  onOpenFlow?: () => void
  onSubmitTask?: (
    workspaceId: string,
    task: string,
    sourceSession?: string,
  ) => Promise<SubmitTaskResponse>
}

/**
 * Renders the panel body for a tab. Overview and Flow have dedicated
 * panels; the rest delegate to the existing session
 * renderers. Navigation between tabs belongs to the result band above them,
 * whose rows each link to the tab that answers them.
 */
export function TabBody({ tabId, detail, onOpenFlow }: TabBodyProps) {
  switch (tabId) {
    case "overview":
      return <OverviewTab detail={detail} onOpenFlow={onOpenFlow} />
    case "flow":
      return detail.demo
        ? <FlowTab key={detail.id} detail={detail} />
        : <RecordedFlowTab key={detail.id} detail={detail} />
    case "tests":
      return <TestFacet detail={detail} />
    case "build":
      return <BuildFacet detail={detail} />
    case "ci":
      return detail.ciComparison ? <OfficialCITab comparison={detail.ciComparison} /> : null
    case "evidence":
      // Commands first, grouped artifacts under them — and the timeline's own
      // empty state only where there are no groups AND the tab exists because
      // of them. Three of six real sessions reach this tab on receipts alone,
      // and used to be told "Evidence is not available for this session"
      // beside a payload holding 78 recorded commands.
      return (
        <div className="space-y-4">
          <ReceiptTable
            caption="Every command this run dispatched."
            receipts={detail.receipts ?? []}
          />
          {detail.evidence.length > 0 ? <EvidenceTimeline groups={detail.evidence} /> : null}
        </div>
      )
    case "logs":
      return <LogsView logs={detail.logs} />
    case "report":
      return <ReportDoc doc={detail.reportDoc} />
  }
}
