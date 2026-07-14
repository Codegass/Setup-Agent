import type { ExecutionSessionDetail, VerdictSummary } from "@/api/types"
import { cn } from "@/lib/utils"

type Tone = VerdictSummary["tone"]

const TONE_LABEL: Record<Tone, string> = {
  success: "PASS",
  attention: "PARTIAL",
  failed: "FAILED",
}

const TONE_BAND: Record<Tone, string> = {
  success: "border-status-success-border bg-status-success-soft/60",
  attention: "border-status-attention-border bg-status-attention-soft/60",
  failed: "border-status-failed-border bg-status-failed-soft/60",
}

const TONE_PILL: Record<Tone, string> = {
  success: "bg-status-success-soft text-status-success",
  attention: "bg-status-attention-soft text-status-attention",
  failed: "bg-status-failed-soft text-status-failed",
}

const TONE_DOT: Record<Tone, string> = {
  success: "bg-status-success",
  attention: "bg-status-attention",
  failed: "bg-status-failed",
}

/**
 * The single, server-composed verdict band. Replaces the old multi-tile
 * SummaryBand. Markup/styling mirrors WorkbenchDetail.dc.html lines 70–74
 * (the amber PARTIAL row at the top of the AFTER block): a toned, rounded
 * band carrying a status pill + a one-sentence headline. When no verdict is
 * composed, falls back to the raw `outcome` string.
 */
function toneFromOutcome(outcome: string): Tone {
  const o = outcome.toLowerCase()
  if (o.includes("fail")) return "failed"
  if (o.includes("partial") || o.includes("warn")) return "attention"
  return "success"
}

export function VerdictBand({ detail }: { detail: ExecutionSessionDetail }) {
  const verdict = detail.verdict
  const tone: Tone = verdict?.tone ?? toneFromOutcome(detail.outcome ?? "")

  return (
    <div
      className={cn(
        "flex items-center gap-3.5 rounded-xl border px-4 py-3",
        TONE_BAND[tone],
      )}
    >
      {verdict ? (
        <span
          className={cn(
            "inline-flex h-7 shrink-0 items-center gap-2 rounded-full px-3 text-[12px] font-bold uppercase tracking-[0.04em]",
            TONE_PILL[tone],
          )}
        >
          <span className={cn("h-[7px] w-[7px] shrink-0 rounded-full", TONE_DOT[tone])} />
          {TONE_LABEL[tone]}
        </span>
      ) : (
        <span
          className={cn("h-[7px] w-[7px] shrink-0 rounded-full", TONE_DOT[tone])}
          aria-hidden
        />
      )}
      <div className="min-w-0 text-[13px] leading-snug text-foreground">
        {verdict ? (
          <>
            <span>{verdict.headline}</span>
            {verdict.detail ? (
              <span className="mt-0.5 block text-[12.5px] text-muted-foreground">
                <b className="font-medium text-muted-foreground">Why —</b> {verdict.detail}
              </span>
            ) : null}
          </>
        ) : (
          <span>{detail.outcome}</span>
        )}
      </div>
    </div>
  )
}
