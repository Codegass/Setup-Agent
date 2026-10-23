import { useState } from "react"

import type { CardTone, ExecutionSessionDetail, ReceiptSummary, ResultRow } from "@/api/types"
import { Badge } from "@/components/common/Badge"
import { Card } from "@/components/common/Card"
import { TestBar } from "@/components/common/TestBar"
import { RECORD_UNREADABLE, formatRate, presentTestRun, recordWasRead } from "@/evidencePresentation"

import { EvidenceAccounting } from "./EvidenceAccounting"
import { FailingCard } from "./FailingCard"
import { ModuleBreakdownDialog } from "./ModuleBreakdownDialog"
import { ReceiptTable } from "./ReceiptTable"
import { TestDetailPage } from "./TestDetailPage"

/**
 * The commands that wrote test reports.
 *
 * `testsReported` alone — the plan's filter — is null on 955 of the 1,195
 * archived receipts and on five of the six real sessions checked against a
 * running API, including runs whose commands plainly wrote reports, so a tab
 * filtered on it says "nothing was recorded" about a run that recorded 78
 * commands. A command that wrote or rewrote a test report is the division a
 * reader means.
 */
function wroteTestReports(receipt: ReceiptSummary): boolean {
  return (
    typeof receipt.testsReported === "number"
    || receipt.reportsNew > 0
    || receipt.reportsChanged > 0
  )
}

const TONE_CLASS: Record<CardTone, string> = {
  success: "text-status-success",
  attention: "text-status-attention",
  failed: "text-status-failed",
  neutral: "text-foreground",
}

/** How the card's "Not confirmed by SAG" row opens its line about the tests
 *  (`_CHECK_QUESTION` in `src/sag/result_card/rows.py`). */
const UNGRADED_TESTS = "whether the tests passed: "

/**
 * How the tests came out, said once.
 *
 * Prefer the card's local test measurements, independent of CI inputs.
 * Older cards retain their recorded comparison facts as a fallback. Never
 * turn a comparison state such as `compared` into a test-pass verdict.
 *
 * A record with no card (a run finalized before the card existed) still states
 * counts, so the tab falls back to reading them; it says so by having nothing
 * else to quote, not by inventing a status the run never recorded.
 */
function TestConclusionCard({ detail }: { detail: ExecutionSessionDetail }) {
  const test = detail.test
  // `presentTestRun` reports the counts it was handed. When this run's record
  // could not be read it was handed defaults, so its "not recorded" sentence
  // would state a finding nobody made.
  const recordRead = recordWasRead(detail.snapshotStatus)
  const rows = detail.resultCard?.rows ?? []
  const row: ResultRow | undefined = rows.find((r) => r.key === "official_ci")
  const ungraded = rows
    .find((r) => r.key === "unconfirmed")
    ?.items?.find((item) => item.startsWith(UNGRADED_TESTS))
  const run = presentTestRun(test)
  const summary = recordRead
    ? run.summary
    : `${RECORD_UNREADABLE} Its test counts are not known here.`

  const measured = detail.resultCard?.dimensions
  if (measured) {
    const tests = measured.officialCi
    const local = measured.tests
    const facts = local?.fact ?? row
    const check = measured.sagChecks.find((item) => item.check === "test outcome graded")
    const counts = [
      { label: local ? "Recorded" : "Executed", value: local ? local.counts?.executed : tests.agentTestsExecuted },
      { label: "Failures", value: local ? local.counts?.failed : tests.agentFailures },
      { label: "Errors", value: local ? local.counts?.errors : tests.agentErrors },
    ]
    const hasCounts = counts.some(({ value }) => value != null)
    return (
      <section aria-label="Test results" className="border-b border-border pb-4">
        {hasCounts ? <dl className="grid grid-cols-3 gap-5">
          {counts.map(({ label, value }) => (
            <div key={label}>
              <dt className="text-[12px] text-muted-foreground">{label}</dt>
              <dd className={`mt-1 font-mono text-[20px] font-semibold tabular-nums ${(label === "Failures" || label === "Errors") && value != null && value > 0 ? "text-status-failed" : "text-foreground"}`}>
                {value == null ? "—" : `${label === "Recorded" && local?.lowerBound ? "≥ " : ""}${value.toLocaleString("en-US")}`}
              </dd>
            </div>
          ))}
        </dl> : <p className="text-[13px] leading-relaxed">{local?.fact.headline ?? row?.detail ?? summary}</p>}
        {check && !check.confirmed ? <p className="mt-3 text-[12px] text-status-attention">Test outcome not confirmed: {check.reason}</p> : null}
        {facts && hasCounts ? (
          <details className="mt-3 text-[12px] text-muted-foreground">
            <summary className="cursor-pointer rounded-sm focus-visible:outline-2 focus-visible:outline-ring">Recorded facts</summary>
            <p className="mt-2 leading-relaxed">{local ? facts.headline : facts.detail ?? facts.headline}</p>
            {local && facts.detail ? <p className="mt-1">{facts.detail}</p> : null}
            {facts.reason ? <p className="mt-1">{facts.reason}</p> : null}
          </details>
        ) : null}
      </section>
    )
  }

  return (
    <Card className="overflow-hidden">
      <div className="p-4">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <div
              className={`text-[26px] font-semibold tabular-nums ${
                row ? TONE_CLASS[row.tone] : (run.valueClass ?? "text-foreground")
              }`}
            >
              {row ? row.status : run.stateLabel}
            </div>
            <div className="mt-0.5 font-mono text-[10px] uppercase tracking-[0.1em] text-muted-foreground">
              {row ? row.label : "Test execution"}
            </div>
          </div>
          {/* Only where there is no card to quote: a carded run states its rate
              in the card's own detail line, and two readings of one rate on one
              screen is the disagreement this tab was rebuilt to end. */}
          {!row && run.passRate != null ? (
            <Badge tone={run.tone}>{formatRate(run.passRate)} of non-skipped results passed</Badge>
          ) : null}
        </div>
        <p className="mt-2 text-[13px] leading-relaxed text-foreground">
          {row ? row.headline : summary}
        </p>
        {row?.detail ? (
          <p className="mt-1 text-[12px] leading-relaxed text-muted-foreground">{row.detail}</p>
        ) : null}
        {row?.reason ? (
          <p className="mt-1 text-[12px] leading-relaxed text-muted-foreground">{row.reason}</p>
        ) : null}
        {ungraded ? (
          <p className="mt-1 text-[12px] leading-relaxed text-muted-foreground">
            {`Not confirmed by SAG: ${ungraded}`}
          </p>
        ) : null}
        {run.nonSkipped > 0 ? (
          <div className="mt-3">
            <TestBar fail={run.negative} pass={run.passed} total={run.nonSkipped} />
          </div>
        ) : null}
      </div>
    </Card>
  )
}

/**
 * The Tests tab, in reading order: what happened, what failed, the per-module
 * breakdown, and — for a reader who asks where the number came from — the
 * accounting behind it.
 */
export function TestFacet({ detail }: { detail: ExecutionSessionDetail }) {
  const [open, setOpen] = useState(false)
  const s = detail.moduleSummary
  const moduleRows = detail.modules?.length ?? 0
  const hasModuleMetrics = s != null || moduleRows > 0
  const single = s?.singleModule ?? moduleRows === 1
  const moduleCount = s?.modulesTotal ?? detail.modules?.length ?? 0
  const failing = detail.test.failingNames ?? []
  const layers = detail.test.evidenceLayers
  const testReceipts = (detail.receipts ?? []).filter(wroteTestReports)
  const detailLabel = layers
    ? single ? "View module test details →" : `View per-module test details (${moduleCount} modules) →`
    : single ? "View test details →" : `View per-module breakdown (${moduleCount} modules) →`

  return (
    <div className="space-y-4">
      <TestConclusionCard detail={detail} />
      <FailingCard names={failing} />
      {hasModuleMetrics ? (
        <button
          className="font-mono text-[11px] text-status-running hover:underline"
          onClick={() => setOpen(true)}
          type="button"
        >
          {detailLabel}
        </button>
      ) : (
        <p className="font-mono text-[11px] text-muted-foreground">
          Detailed module test metrics were not produced for this run.
        </p>
      )}
      <ReceiptTable
        caption="Commands that wrote test reports."
        empty="No command in this run wrote a test report."
        receipts={testReceipts}
      />
      {layers ? (
        <EvidenceAccounting evidence={layers.evidence} layers={layers.tests} />
      ) : null}
      {open ? (
        <ModuleBreakdownDialog
          onClose={() => setOpen(false)}
          title={single ? "Test details" : "Per-module test details"}
        >
          <TestDetailPage detail={detail} />
        </ModuleBreakdownDialog>
      ) : null}
    </div>
  )
}
