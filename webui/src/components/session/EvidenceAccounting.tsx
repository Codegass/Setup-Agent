import type {
  EvidenceCountSummary,
  MetricsV2EvidenceSummary,
  TestEvidenceLayers,
} from "@/api/types"

/**
 * Where a run's test results came from, and what was left out of the total.
 *
 * Six layers plus the record of the commands behind them. Collapsed, because a
 * reader who only wants the number has it in the band above; opened, because a
 * reader who asks "why is that number smaller than the one I expected?" has to
 * be able to find the answer without reading a JSON file.
 *
 * Every count is copied from the payload. A layer the run did not count says so
 * and gives the reason; it never renders as a zero, which a reader would read
 * as "nothing was found" when the truth is "nobody counted".
 */

/** The label each layer carries on screen, in the order a reader reads them:
 *  what counted, then what did not, then the records underneath. */
const LAYERS: {
  key: string
  label: string
  of: (layers: TestEvidenceLayers) => EvidenceCountSummary
}[] = [
  {
    key: "receiptExecutions",
    label: "Results bound to this run's receipts",
    of: (l) => l.claimed.receiptExecutions,
  },
  {
    key: "latestCases",
    label: "Tests identified by module and name",
    of: (l) => l.claimed.latestCases,
  },
  {
    key: "latestSubjects",
    label: "Test classes identified by module and name",
    of: (l) => l.claimed.latestSubjects,
  },
  {
    key: "quarantinedObservations",
    label: "Set aside: not from this run's receipts",
    of: (l) => l.quarantinedObservations,
  },
  {
    key: "unattributedObservations",
    label: "Set aside: no module or test name recorded",
    of: (l) => l.unattributedObservations,
  },
  {
    key: "staleObservations",
    label: "Set aside: from an earlier run",
    of: (l) => l.staleObservations,
  },
]

/**
 * The backend's own reason strings, said in words a reader can act on.
 *
 * These nine are every distinct reason the 674 archived `report_metrics.json`
 * files under `logs/` state. Three of them contain a word this UI may not
 * print, so rendering `reason` as it arrives is a copy violation that no test
 * over an invented fixture can see.
 *
 * The set is NOT closed — `report_metrics.py` composes some reasons at run
 * time — so an unmapped reason is dropped rather than shown. A row with no
 * sentence still says the layer is not available; a row that leaked a raw
 * reason would say it in the wrong words, and we cannot tell which words those
 * will be.
 */
const REASON_IN_PLAIN_ENGLISH: Record<string, string> = {
  "tests were not run": "the tests were not run",
  "module-qualified subject/case identity was not sealed":
    "the run did not record module and test names",
  "one or more receipt rows lacked complete module-qualified identity":
    "some recorded rows carried no module or test name",
  "one or more receipt identity samples were bounded and truncated; counts cover only retained rows":
    "the list of test names was cut short, so these counts cover only the rows kept",
  "current receipt testcase rows were unavailable": "this run's per-test rows could not be read",
  "receipt scope unavailable": "the run did not record which commands these results came from",
  "a current receipt stated no suite totals and its identity rows were bounded":
    "a command reported no totals, and its list of test names was cut short",
  "qualifying receipt execution identity was not sealed":
    "the run did not record identities for the commands that count",
  "a current receipt stated neither suite totals nor sealed rows":
    "a command reported neither totals nor per-test rows",
}

/** What the evidence record says about the commands behind these counts. */
const INTEGRITY: Record<MetricsV2EvidenceSummary["integrity"], string> = {
  complete: "complete",
  degraded: "incomplete",
  failed: "failed",
  unavailable: "not available",
}

const NOT_AVAILABLE = "not available"

function plain(reason: string | null | undefined): string | null {
  const text = (reason ?? "").trim()
  return text ? (REASON_IN_PLAIN_ENGLISH[text] ?? null) : null
}

/** The five counts, in the order and separator the result band states them. */
function counts(bucket: EvidenceCountSummary, floor: boolean): string | null {
  const { executed, passed, failed, errors, skipped } = bucket
  if (
    typeof executed !== "number"
    || typeof passed !== "number"
    || typeof failed !== "number"
    || typeof errors !== "number"
    || typeof skipped !== "number"
  ) {
    return null
  }
  const mark = floor ? "≥" : ""
  return (
    `${mark}${executed.toLocaleString()} executed · ${passed.toLocaleString()} passed`
    + ` · ${failed.toLocaleString()} failed · ${errors.toLocaleString()} errors`
    + ` · ${skipped.toLocaleString()} skipped`
  )
}

function Row({
  label,
  layer,
  value,
  notes,
}: {
  label: string
  layer: string
  value: string
  notes: string[]
}) {
  return (
    <div className="border-t border-border py-2 first:border-t-0" data-layer={layer}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
        <span className="text-[12px] text-muted-foreground">{label}</span>
        <span className="font-mono text-[12px] tabular-nums text-foreground">{value}</span>
      </div>
      {notes.map((note) => (
        <div className="mt-0.5 text-right text-[11px] leading-relaxed text-muted-foreground" key={note}>
          {note}
        </div>
      ))}
    </div>
  )
}

export function EvidenceAccounting({
  layers,
  evidence,
}: {
  layers: TestEvidenceLayers
  evidence: MetricsV2EvidenceSummary
}) {
  const recorded =
    typeof evidence.receiptsPersisted === "number" && typeof evidence.receiptsExpected === "number"
      ? `${evidence.receiptsPersisted.toLocaleString()} of `
        + `${evidence.receiptsExpected.toLocaleString()} commands recorded`
      : null

  return (
    <details className="rounded-[10px] border border-border bg-card px-4 py-3">
      <summary className="cursor-pointer select-none text-[12px] font-semibold text-muted-foreground hover:text-foreground">
        Evidence accounting
      </summary>
      <div className="mt-2">
        {LAYERS.map(({ key, label, of }) => {
          const bucket = of(layers)
          const floor = bucket.availability === "partial" && bucket.bound === "lower"
          const stated = bucket.availability === "unavailable" ? null : counts(bucket, floor)
          const notes = [
            floor ? "at least this many, not a total" : null,
            plain(bucket.reason),
          ].filter((note): note is string => note !== null)
          return (
            <Row
              key={key}
              label={label}
              layer={key}
              notes={notes}
              value={stated ?? NOT_AVAILABLE}
            />
          )
        })}
        <Row
          label="Evidence records"
          layer="evidenceRecords"
          notes={recorded ? [recorded] : []}
          value={INTEGRITY[evidence.integrity]}
        />
      </div>
    </details>
  )
}
