import type {
  AttentionItem,
  CardTone,
  ExecutionSessionDetail,
  ResultCard,
  ResultRow,
  RowKey,
} from "@/api/types"
import { ModuleTable } from "@/components/session/ModuleTable"
import { cn } from "@/lib/utils"

const CHIP: Record<CardTone, string> = {
  success: "bg-status-success-soft text-status-success",
  attention: "bg-status-attention-soft text-status-attention",
  failed: "bg-status-failed-soft text-status-failed",
  neutral: "bg-accent text-muted-foreground",
}

function rowFor(card: ResultCard, key: RowKey): ResultRow | undefined {
  return card.rows.find((row) => row.key === key)
}

/**
 * One measurement, restated exactly as the card wrote it.
 *
 * Nothing here is recomputed: the headline, the detail and the reason are the
 * run's own words, and a row the run did not measure carries its own reason
 * instead of a blank the reader has to interpret.
 */
function Tile({ row }: { row: ResultRow | undefined }) {
  if (!row) return null
  return (
    <div className="min-w-0 rounded-[10px] border border-border bg-card px-4 py-3.5">
      <div className="flex items-center gap-2">
        <span className="font-mono text-[11px] uppercase tracking-[0.06em] text-muted-foreground">
          {row.label}
        </span>
        <span className={cn("rounded-full px-2 py-0.5 text-[11px] font-semibold", CHIP[row.tone])}>
          {row.status}
        </span>
      </div>
      {/* A headline that only repeats the chip beside it is dropped rather
          than said twice — "success", "not collected" and the like. */}
      {row.headline === row.status ? null : (
        <div className="mt-1.5 text-[17px] font-semibold leading-snug tracking-[-0.01em] text-foreground">
          {row.headline}
        </div>
      )}
      {row.detail ? (
        <div className="mt-1 text-[12px] leading-relaxed text-muted-foreground">{row.detail}</div>
      ) : null}
      {row.reason ? (
        <div className="mt-1 text-[12px] italic leading-relaxed text-muted-foreground">
          {row.reason}
        </div>
      ) : null}
    </div>
  )
}

function AttentionRow({ item }: { item: AttentionItem }) {
  const refs = item.refs ?? []
  return (
    <li className="border-t border-border px-4 py-3 first:border-t-0">
      <div className="text-[13px] font-semibold text-foreground">{item.title}</div>
      {item.detail ? (
        <div className="mt-0.5 text-[12px] leading-relaxed text-muted-foreground">{item.detail}</div>
      ) : null}
      {refs.length > 0 ? (
        <div className="mt-1.5 flex flex-wrap gap-1">
          {refs.map((ref) => (
            <span
              className="rounded bg-accent px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground"
              key={ref}
            >
              {ref}
            </span>
          ))}
        </div>
      ) : null}
    </li>
  )
}

/**
 * The Overview: what needs a person first, then the two measurements they came
 * to read, then the modules behind them, then anything the run set aside.
 *
 * Every word below the chrome is the result card's own. The tab does not
 * recompute a count, and it does not fill in a number the run declined to
 * measure — a run with no card at all says that, rather than rendering an
 * empty shape.
 */
export function OverviewTab({ detail }: { detail: ExecutionSessionDetail }) {
  const card = detail.resultCard
  const modules = detail.modules ?? []
  const ms = detail.moduleSummary
  // The card carries the run's goal; older sessions that predate the card
  // carry it on the context trace and nowhere else.
  const goal = card?.goal ?? detail.context?.trunk.goal ?? null
  const attention = card?.attention ?? []
  const notes = card?.notes ?? []

  return (
    <div>
      {card ? (
        <>
          <section
            aria-labelledby="overview-attention"
            className={cn(
              "overflow-hidden rounded-xl border bg-card",
              attention.length > 0 ? "border-status-attention-border" : "border-border",
            )}
          >
            <div className="flex items-center gap-2 border-b border-border px-4 py-3">
              {attention.length > 0 ? (
                <span className="h-[7px] w-[7px] rounded-full bg-status-failed" />
              ) : null}
              <h2 className="text-[14px] font-bold text-foreground" id="overview-attention">
                Needs attention
              </h2>
            </div>
            {attention.length > 0 ? (
              <ul>
                {attention.map((item, index) => (
                  <AttentionRow item={item} key={`${item.kind}-${index}-${item.title}`} />
                ))}
              </ul>
            ) : (
              <p className="px-4 py-3 text-[13px] text-muted-foreground">Nothing needs attention.</p>
            )}
          </section>

          <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Tile row={rowFor(card, "build")} />
            <Tile row={rowFor(card, "tests")} />
          </div>
        </>
      ) : (
        <div className="rounded-xl border border-border bg-card px-4 py-3">
          <p className="text-[13px] text-muted-foreground">
            No result was recorded for this run yet.
          </p>
        </div>
      )}

      {goal ? (
        // Below the two measurements, not above them: a real goal is a
        // paragraph the operator wrote, and putting it first pushed what needs
        // attention off the screen on every run checked against a live API.
        // Stated in full — it is the instruction this run was given.
        <section className="mt-3 rounded-[10px] border border-border bg-card px-4 py-3">
          <div className="font-mono text-[11px] uppercase tracking-[0.06em] text-muted-foreground">
            Goal
          </div>
          <p className="mt-1 text-[13px] leading-relaxed text-foreground">{goal}</p>
        </section>
      ) : null}

      {modules.length > 0 ? (
        <section
          aria-label="Per-module breakdown"
          className="mt-5 overflow-hidden rounded-xl border border-border bg-card"
        >
          <div className="flex items-center justify-between border-b border-border px-4 py-3">
            <h2 className="text-[14px] font-bold text-foreground">Per-module breakdown</h2>
            <span className="font-mono text-[12px] text-muted-foreground">
              {ms?.modulesTotal ?? modules.length} modules
              {ms?.coverageSource ? ` · ${ms.coverageSource}` : ""}
            </span>
          </div>
          <ModuleTable modules={modules} variant="overview" />
        </section>
      ) : (
        <section className="mt-5 rounded-xl border border-dashed border-border bg-muted/30 px-4 py-3">
          <p className="text-[13px] text-muted-foreground">
            Module details are not available for this run.
          </p>
        </section>
      )}

      {notes.length > 0 ? (
        <details className="mt-3 rounded-[10px] border border-border bg-card px-4 py-3">
          <summary className="cursor-pointer select-none text-[12px] font-semibold text-muted-foreground hover:text-foreground">
            Data notes
          </summary>
          <ul className="mt-2 space-y-1.5 pl-4 text-[12px] leading-relaxed text-muted-foreground">
            {notes.map((note) => (
              <li className="list-disc" key={note}>
                {note}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  )
}
