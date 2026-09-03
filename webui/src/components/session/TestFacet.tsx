import { useState } from "react"

import type { EvidenceCountSummary, ExecutionSessionDetail, ObservationCountSummary } from "@/api/types"
import { Badge } from "@/components/common/Badge"
import { Card } from "@/components/common/Card"
import { TestBar } from "@/components/common/TestBar"
import {
  completeEvidenceCounts,
  formatCount,
  formatRate,
  lowerBoundEvidenceCounts,
  presentDiagnostics,
  presentTestRun,
  presentVerifiedIdentities,
} from "@/evidencePresentation"

import { FailingCard } from "./FailingCard"
import { ModuleBreakdownDialog } from "./ModuleBreakdownDialog"
import { TestDetailPage } from "./TestDetailPage"

function LayerRow({
  counts,
  label,
  note,
}: {
  counts: EvidenceCountSummary | ObservationCountSummary
  label: string
  note?: string
}) {
  const available = completeEvidenceCounts(counts)
  const lowerBound = lowerBoundEvidenceCounts(counts)
  const fileNote = "reportFileCount" in counts && typeof counts.reportFileCount === "number"
    ? `${counts.reportFileCount.toLocaleString()} report files`
    : null

  return (
    <div className="border-t border-border py-2 first:border-t-0">
      <div className="flex items-center justify-between gap-3">
        <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-muted-foreground">
          {label}
        </span>
        <span
          className={`font-mono text-[12px] ${lowerBound ? "text-status-attention" : "text-foreground"}`}
        >
          {available
            ? `${formatCount(available.passed)} / ${formatCount(available.executed)} passed`
            : lowerBound
              ? `≥${formatCount(lowerBound.executed)} retained`
              : "Unavailable"}
        </span>
      </div>
      {(available || lowerBound) && (
        (available?.failed ?? lowerBound?.failed ?? 0) > 0
        || (available?.errors ?? lowerBound?.errors ?? 0) > 0
        || (available?.skipped ?? lowerBound?.skipped ?? 0) > 0
      ) ? (
        <div className="mt-0.5 text-right font-mono text-[11px] text-muted-foreground">
          {`${formatCount((available ?? lowerBound)!.failed)} failed · ${formatCount((available ?? lowerBound)!.errors)} errors · ${formatCount((available ?? lowerBound)!.skipped)} skipped`}
        </div>
      ) : null}
      {!available || note ? (
        <div className="mt-0.5 text-right font-mono text-[11px] text-muted-foreground">
          {[
            note,
            fileNote,
            lowerBound ? "Lower bound; complete total unavailable" : null,
            !available ? counts.reason || "Count details were incomplete" : null,
          ].filter(Boolean).join(" · ")}
        </div>
      ) : null}
    </div>
  )
}

// Conclusion-first test summary (prototype workbench/sections.jsx TestBody).
export function TestConclusionCard({ test }: { test: ExecutionSessionDetail["test"] }) {
  const run = presentTestRun(test)
  const identities = presentVerifiedIdentities(test)
  const evidenceLayers = test.evidenceLayers
  const layers = evidenceLayers?.tests
  const diagnostics = presentDiagnostics(layers)
  const integrityLabel = evidenceLayers
    ? ({
        complete: "Complete",
        degraded: "Incomplete",
        failed: "Failed",
        unavailable: "Unavailable",
      } as const)[evidenceLayers.evidence.integrity]
    : null

  return (
    <Card className="overflow-hidden">
      <div className="p-4">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <div className={`text-[26px] font-semibold tabular-nums ${run.valueClass ?? "text-foreground"}`}>
              {run.stateLabel}
            </div>
            <div className="mt-0.5 font-mono text-[10px] uppercase tracking-[0.1em] text-muted-foreground">
              Test run result
            </div>
          </div>
          {run.passRate != null ? (
            <Badge tone={run.tone}>{formatRate(run.passRate)} of non-skipped results passed</Badge>
          ) : null}
        </div>
        <p className="mt-2 text-[12px] leading-relaxed text-muted-foreground">{run.summary}</p>
        {run.nonSkipped > 0 ? (
          <div className="mt-3">
            <TestBar fail={run.negative} pass={run.passed} total={run.nonSkipped} />
          </div>
        ) : null}
      </div>

      <div className="border-t border-border p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <div className="text-[13px] font-semibold text-foreground">Verified per-test results</div>
            <p className="mt-0.5 max-w-[70ch] text-[12px] leading-relaxed text-muted-foreground">
              {identities.summary}
            </p>
          </div>
          <div className={`shrink-0 font-mono text-[16px] font-semibold tabular-nums ${identities.valueClass ?? "text-foreground"}`}>
            {identities.value}
          </div>
        </div>
        {identities.counts && identities.nonSkipped && identities.nonSkipped > 0 ? (
          <div className="mt-3">
            <TestBar
              fail={identities.negative ?? 0}
              pass={identities.counts.passed}
              total={identities.nonSkipped}
            />
          </div>
        ) : null}
      </div>

      {layers && diagnostics.hasData ? (
        <div className="border-t border-border bg-muted/35 px-4 py-3">
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="text-[12px] font-semibold text-foreground">Diagnostic observations</div>
              <p className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">{diagnostics.summary}</p>
              {diagnostics.breakdown ? (
                <p className="mt-1 font-mono text-[11px] text-muted-foreground">{diagnostics.breakdown}</p>
              ) : null}
            </div>
            <span className="shrink-0 font-mono text-[15px] font-semibold tabular-nums text-foreground">
              {diagnostics.value}
            </span>
          </div>
        </div>
      ) : null}

      {layers && evidenceLayers ? (
        <details className="group border-t border-border px-4 py-3">
          <summary className="cursor-pointer select-none text-[11px] font-semibold text-muted-foreground hover:text-foreground">
            Evidence details
          </summary>
          <div className="mt-2">
            <LayerRow counts={layers.claimed.latestCases} label="Verified test cases" />
            <LayerRow counts={layers.claimed.receiptExecutions} label="Recorded test executions" />
            <LayerRow
              counts={layers.quarantinedObservations}
              label="Excluded observations"
              note="Not used in the verified result"
            />
            <LayerRow
              counts={layers.unattributedObservations}
              label="Observations without test identity"
              note="Not used in the verified result"
            />
            <LayerRow
              counts={layers.staleObservations}
              label="Earlier-run observations"
              note="Not used in the verified result"
            />
            <div className="flex items-center justify-between gap-3 border-t border-border pt-2">
              <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-muted-foreground">
                Evidence records
              </span>
              <span className="font-mono text-[12px] text-foreground">{integrityLabel}</span>
            </div>
            {identities.rawReason ? (
              <p className="mt-2 font-mono text-[10px] leading-relaxed text-muted-foreground">
                Source note: {identities.rawReason}
              </p>
            ) : null}
          </div>
        </details>
      ) : null}
    </Card>
  )
}

export function TestFacet({ detail }: { detail: ExecutionSessionDetail }) {
  const [open, setOpen] = useState(false)
  const s = detail.moduleSummary
  const moduleRows = detail.modules?.length ?? 0
  const hasModuleMetrics = s != null || moduleRows > 0
  const single = s?.singleModule ?? moduleRows === 1
  const moduleCount = s?.modulesTotal ?? detail.modules?.length ?? 0
  const failing = detail.test.failingNames ?? []
  const evidenceLayers = detail.test.evidenceLayers
  const detailLabel = evidenceLayers
    ? single ? "View module test details →" : `View per-module test details (${moduleCount} modules) →`
    : single ? "View test details →" : `View per-module breakdown (${moduleCount} modules) →`

  return (
    <div className="space-y-4">
      <TestConclusionCard test={detail.test} />
      {!evidenceLayers ? <FailingCard names={failing} /> : null}
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
      {open ? (
        <ModuleBreakdownDialog
          onClose={() => setOpen(false)}
          title={
            single ? "Test details" : "Per-module test details"
          }
        >
          <TestDetailPage detail={detail} />
        </ModuleBreakdownDialog>
      ) : null}
    </div>
  )
}
