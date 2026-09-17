import { useState } from "react"

import type { CardTone, ResultCard, ResultRow, RowKey, SnapshotStatus } from "@/api/types"
import { RECORD_UNREADABLE, recordWasRead } from "@/evidencePresentation"
import { cn } from "@/lib/utils"

import type { TabId } from "./facets"

/** Which tab answers each row. Clicking a row is the two-click path from a
 *  number to the evidence behind it. */
const ROW_TAB: Record<RowKey, TabId> = {
  setup: "turns",
  task: "evidence",
  build: "build",
  tests: "tests",
  coverage: "tests",
  ci: "ci",
  report: "report",
}

/** What a row's extra lines are, so the disclosure can name them. Only the
 *  required-task and Official CI rows carry items today; anything else that
 *  grows them reads as "details" until it is named here. */
const ITEM_NOUN: Partial<Record<RowKey, string>> = {
  task: "step",
  ci: "finding",
}

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`
}

const CHIP: Record<CardTone, string> = {
  success: "bg-status-success-soft text-status-success",
  attention: "bg-status-attention-soft text-status-attention",
  failed: "bg-status-failed-soft text-status-failed",
  neutral: "bg-accent text-muted-foreground",
}

const BAND: Record<CardTone, string> = {
  success: "border-status-success-border bg-status-success-soft/30",
  attention: "border-status-attention-border bg-status-attention-soft/30",
  failed: "border-status-failed-border bg-status-failed-soft/30",
  neutral: "border-border bg-card",
}

function Items({ row }: { row: ResultRow }) {
  const [open, setOpen] = useState(false)
  const items = row.items ?? []
  if (items.length === 0) return null
  const counted = plural(items.length, ITEM_NOUN[row.key] ?? "detail")
  return (
    <div className="mt-1">
      <button
        aria-expanded={open}
        className="text-[11px] text-muted-foreground underline-offset-2 hover:underline"
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        {open ? `Hide the ${counted}` : `Show ${counted}`}
      </button>
      {open ? (
        <ul className="mt-1 space-y-0.5">
          {items.map((item) => (
            <li className="font-mono text-[11px] text-muted-foreground" key={item}>
              {item}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}

function Row({
  row,
  linkedTab,
  onOpenTab,
}: {
  row: ResultRow
  /** The tab that answers this row, when this run has it. `null` when it does
   *  not — the label is then stated rather than offered as a button that would
   *  go nowhere. */
  linkedTab: TabId | null
  onOpenTab: (id: TabId) => void
}) {
  const status = row.status.replace(/_/g, " ")
  return (
    <div className="flex items-start gap-3 py-1.5" data-row={row.key}>
      {linkedTab ? (
        // Named for what it does, not just for the row: the tab bar above
        // already has a button called "Tests", and two buttons on one screen
        // reading the same to a screen reader while going different places is
        // the kind of thing only a screen reader user finds out about.
        <button
          aria-label={`Open the evidence behind ${row.label}`}
          className="w-28 shrink-0 text-left text-[13px] font-semibold text-foreground underline-offset-2 hover:underline"
          onClick={() => onOpenTab(linkedTab)}
          title={`Open the evidence behind ${row.label}`}
          type="button"
        >
          {row.label}
        </button>
      ) : (
        <span className="w-28 shrink-0 text-[13px] font-semibold text-foreground">
          {row.label}
        </span>
      )}
      <span
        className={cn(
          "mt-0.5 shrink-0 rounded-full px-2 py-0.5 text-[11px] font-semibold",
          CHIP[row.tone],
        )}
      >
        {status}
      </span>
      <div className="min-w-0 flex-1">
        {/* Real cards often set the headline to the status word itself —
            "success", "not collected", "not compared". The chip beside it has
            already said that, so the line is dropped rather than doubled. */}
        {row.headline === status ? null : (
          <p className="text-[13px] text-foreground">{row.headline}</p>
        )}
        {row.detail ? <p className="text-[11px] text-muted-foreground">{row.detail}</p> : null}
        {row.reason ? (
          <p className="text-[11px] italic text-muted-foreground">{row.reason}</p>
        ) : null}
        <Items row={row} />
      </div>
    </div>
  )
}

/**
 * The run's whole result, seven rows, in the order the terminal prints them.
 *
 * Every string below the chrome is the run's own — the band restates a row's
 * headline, detail, reason and items exactly as the record wrote them, and
 * fills nothing in. A row the run did not measure says so in its own words.
 */
export function ResultBand({
  card,
  onOpenTab,
  availableTabs,
  snapshotStatus,
}: {
  card: ResultCard | null | undefined
  onOpenTab: (id: TabId) => void
  /** The tabs this run actually has. Omit to link every row. */
  availableTabs?: TabId[]
  /** How the run's record read. Without it the band cannot tell a run that
   *  recorded nothing from a record this page could not read, and it used to
   *  state the first when it only knew the second. */
  snapshotStatus?: SnapshotStatus | null
}) {
  if (!card) {
    return (
      <div className="rounded-lg border border-border bg-card p-4" data-tone="neutral">
        <p className="text-[13px] text-muted-foreground">
          {recordWasRead(snapshotStatus)
            ? "No result was recorded for this run yet."
            : `${RECORD_UNREADABLE} Whether the run recorded one is not known here.`}
        </p>
      </div>
    )
  }
  const tone = card.rows[0]?.tone ?? "neutral"
  const linkable = (id: TabId): TabId | null =>
    availableTabs === undefined || availableTabs.includes(id) ? id : null
  return (
    <div className={cn("rounded-lg border px-4 py-2", BAND[tone])} data-tone={tone}>
      {/* A result rebuilt from an older run record is worth less than one read
          from the run that wrote it, and looks identical to one unless the page
          says so — which the terminal and the report both do, in these words.
          `tests/test_gloss_parity.py` holds the three copies together. */}
      {card.verdictSource === "legacy" ? (
        <p className="py-1 text-[11px] text-muted-foreground">
          <span className="font-mono uppercase tracking-[0.1em]">Record</span>
          {" — "}
          {"reconstructed from an older run record"}
        </p>
      ) : null}
      {card.rows.map((row) => (
        <Row
          key={row.key}
          linkedTab={linkable(ROW_TAB[row.key])}
          onOpenTab={onOpenTab}
          row={row}
        />
      ))}
    </div>
  )
}
