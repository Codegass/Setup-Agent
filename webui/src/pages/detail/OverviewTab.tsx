import type { ReactNode } from "react"

import type { AttentionItem, ExecutionSessionDetail, ResultStats } from "@/api/types"
import { ModuleTable } from "@/components/session/ModuleTable"
import { durationText } from "@/lib/durationText"

/** A count the way a bill prints it: grouped, never shortened, and a dash for
 *  a number the run did not record. `129.6k` hides the difference between two
 *  runs that spent a thousand tokens apart. */
function count(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "—" : value.toLocaleString("en-US")
}

/** The model's name, or a dash where the card names none. */
function named(value: string | null | undefined): string {
  const text = value?.trim() ?? ""
  return text ? text : "—"
}

function spent(inTokens: number | null | undefined, outTokens: number | null | undefined): boolean {
  return inTokens != null || outTokens != null
}

/**
 * The run's token bill, in the words the card and the report already use.
 *
 * The model and the advisor are billed on their own lines because they are two
 * models: one number for both would say the first spent it all. The total is
 * allowed only underneath them, and only when both lines are there to be
 * summed — a "total" of one row is that row said twice, and a total whose
 * parts are off screen is a number nobody can check.
 */
function tokenLines(stats: ResultStats): string[] {
  const model = spent(stats.tokensIn, stats.tokensOut)
  const advisor = spent(stats.advisorTokensIn, stats.advisorTokensOut)
  const lines: string[] = []
  if (model) {
    lines.push(`Model ${named(stats.model)} · ${count(stats.tokensIn)} in · ${count(stats.tokensOut)} out`)
  }
  if (advisor) {
    lines.push(
      `Advisor ${named(stats.advisorModel)} · ${count(stats.advisorTokensIn)} in · ${count(stats.advisorTokensOut)} out`,
    )
  }
  const parts = [stats.tokensIn, stats.tokensOut, stats.advisorTokensIn, stats.advisorTokensOut]
  if (model && advisor && parts.every((part) => typeof part === "number")) {
    lines.push(
      `Total (model + advisor) · ${count(stats.tokensIn! + stats.advisorTokensIn!)} in · ` +
        `${count(stats.tokensOut! + stats.advisorTokensOut!)} out`,
    )
  }
  return lines
}

function Tile({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="rounded-[10px] border border-border bg-card px-4 py-3">
      <div className="font-mono text-[11px] uppercase tracking-[0.06em] text-muted-foreground">
        {label}
      </div>
      {children}
    </div>
  )
}

function usefulTime(value: string | null | undefined): string | null {
  const text = value?.trim() ?? ""
  return text && !["unknown", "none", "—", "-", "now"].includes(text.toLowerCase()) ? text : null
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
  // What the run cost, in time and in tokens. Every value below is a field of
  // `card.stats` — the same numbers the terminal block and the written report
  // state — so nothing here is recomputed from turns or rows.
  const stats = card?.stats
  const ranFor = durationText(stats?.wallClockSeconds)
  const started = usefulTime(detail.start)
  const finished = usefulTime(detail.finish)
  const bill = stats ? tokenLines(stats) : []

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

      {/* What the run cost. Two tiles side by side on a wide pane, one column
          on a narrow one. Drawn only for a card that measured something: a
          tile of dashes is a shape where a number was expected. */}
      {ranFor || bill.length > 0 ? (
        <section aria-label="Run" className="mt-3 grid gap-3 first:mt-0 sm:grid-cols-2">
          {ranFor ? (
            <Tile label="Time">
              <div className="mt-1 text-[22px] font-bold leading-none tracking-[-0.01em] text-foreground">
                {ranFor}
              </div>
              {started && finished ? (
                <div className="mt-1.5 font-mono text-[11px] leading-relaxed text-muted-foreground">
                  {`started ${started} · finished ${finished}`}
                </div>
              ) : null}
            </Tile>
          ) : null}
          {bill.length > 0 ? (
            <Tile label="Tokens">
              <ul className="mt-1.5 space-y-1">
                {bill.map((line) => (
                  <li className="font-mono text-[12px] leading-relaxed text-foreground" key={line}>
                    {line}
                  </li>
                ))}
              </ul>
            </Tile>
          ) : null}
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
