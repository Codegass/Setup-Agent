import { useState } from "react"

import type { CardTone, ExecutionSessionDetail, ResultRow } from "@/api/types"
import { Badge } from "@/components/common/Badge"
import { Card } from "@/components/common/Card"
import { TestBar } from "@/components/common/TestBar"
import { formatRate, presentTestRun } from "@/evidencePresentation"

import { EvidenceAccounting } from "./EvidenceAccounting"
import { FailingCard } from "./FailingCard"
import { ModuleBreakdownDialog } from "./ModuleBreakdownDialog"
import { TestDetailPage } from "./TestDetailPage"

const TONE_CLASS: Record<CardTone, string> = {
  success: "text-status-success",
  attention: "text-status-attention",
  failed: "text-status-failed",
  neutral: "text-foreground",
}

/**
 * How the tests came out, said once.
 *
 * When the run recorded a result, the words are the result card's own — the
 * same string the terminal printed and the report wrote. A second reading of
 * the same counts, worked out here, is how the band and the tab came to
 * disagree about one run.
 *
 * A record with no card (a run finalized before the card existed) still states
 * counts, so the tab falls back to reading them; it says so by having nothing
 * else to quote, not by inventing a status the run never recorded.
 */
function TestConclusionCard({ detail }: { detail: ExecutionSessionDetail }) {
  const test = detail.test
  const row: ResultRow | undefined = detail.resultCard?.rows.find((r) => r.key === "tests")
  const run = presentTestRun(test)

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
              Test execution
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
          {row ? row.headline : run.summary}
        </p>
        {row?.detail ? (
          <p className="mt-1 text-[12px] leading-relaxed text-muted-foreground">{row.detail}</p>
        ) : null}
        {row?.reason ? (
          <p className="mt-1 text-[12px] leading-relaxed text-muted-foreground">{row.reason}</p>
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
