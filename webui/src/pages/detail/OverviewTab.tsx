import type { AttentionItem, ExecutionSessionDetail } from "@/api/types"
import { ModuleTable } from "@/components/session/ModuleTable"

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
 * The Overview: what needs a person first, then what the run was asked to do,
 * then the modules behind the numbers, then anything the run set aside.
 *
 * It does not restate the result band. The band sits above the tab bar and is
 * on screen the whole time; an Overview that repeated its Build and Tests rows
 * printed the same sentence twice on one screen. What this tab adds is what
 * the band has no room for.
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
      {/* Only when there is something to attend to. The box used to be drawn
          on every run with a card, and on a clean one its whole content was
          the sentence "Nothing needs attention." — a bordered band across the
          top of the tab holding no item a reader could act on, above the
          things they came for. A run with no card has no attention list at
          all, and the band two inches above already says that run recorded no
          result, so nothing is said about it here either. */}
      {attention.length > 0 ? (
        <section
          aria-labelledby="overview-attention"
          className="overflow-hidden rounded-xl border border-status-attention-border bg-card"
        >
          <div className="flex items-center gap-2 border-b border-border px-4 py-3">
            <span className="h-[7px] w-[7px] rounded-full bg-status-failed" />
            <h2 className="text-[14px] font-bold text-foreground" id="overview-attention">
              Needs attention
            </h2>
          </div>
          <ul>
            {attention.map((item, index) => (
              <AttentionRow item={item} key={`${item.kind}-${index}-${item.title}`} />
            ))}
          </ul>
        </section>
      ) : null}

      {goal ? (
        // Below what needs attention, not above it: a real goal is a paragraph
        // the operator wrote, and putting it first pushed what needs attention
        // off the screen on every run checked against a live API. Stated in
        // full — it is the instruction this run was given.
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
