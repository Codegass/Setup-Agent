import type { ReceiptSummary } from "@/api/types"

/**
 * What each command in a run actually did.
 *
 * One row per receipt: the command line as dispatched, the directory it ran
 * in, the toolchain the run observed while running it, its exit code, the test
 * reports it wrote and the tests those reports stated.
 *
 * Nothing here is defaulted. Three of the six columns are absent on most real
 * receipts — 843 of the 1,195 under `logs/` state a Java major with no runtime
 * reading, 312 state no tool version, 955 state no test count — so each column
 * has a rendering for what it has, and an em dash only where the payload
 * carries nothing at all.
 */

const ABSENT = "—"

const DEFAULT_EMPTY = "No commands were recorded for this run."

/** `{tool} {version}` when the run read a version off the tool, else the tool's
 *  own name. A receipt with no toolchain record at all is the second case. */
function toolText(receipt: ReceiptSummary): string {
  const version = receipt.toolchain?.version?.trim()
  return version ? `${receipt.tool} ${version}` : receipt.tool
}

/**
 * The Java the command ran under.
 *
 * Headed "Java", not "Runtime": `jdkMajor` is a runtime reading on 639 of the
 * receipts that state one and a requirement read out of build config on the
 * rest, and `ReceiptSummary` flattens both to the same string. Calling the
 * column "Runtime" would report a requirement as an observation. The second
 * half, when there is one, is always a reading.
 */
function javaText(receipt: ReceiptSummary): string {
  const major = receipt.jdkMajor?.trim()
  if (!major) {
    return ABSENT
  }
  const version = receipt.jdkVersion?.trim()
  return version ? `JDK ${major} · ${version}` : `JDK ${major}`
}

/** New files, rewritten files, or neither. A `0 new` on a command that wrote
 *  nothing reads as a count; there is nothing to count. */
function reportsText(receipt: ReceiptSummary): string {
  const parts = [
    receipt.reportsNew > 0 ? `${receipt.reportsNew.toLocaleString()} new` : null,
    receipt.reportsChanged > 0 ? `${receipt.reportsChanged.toLocaleString()} changed` : null,
  ].filter((part): part is string => part !== null)
  return parts.length > 0 ? parts.join(" · ") : ABSENT
}

const HEAD = "px-3 py-1.5 text-left font-mono text-[10px] uppercase tracking-[0.08em] text-muted-foreground"
const CELL = "px-3 py-2 align-top text-[12px] text-foreground"

export function ReceiptTable({
  receipts,
  caption,
  empty = DEFAULT_EMPTY,
}: {
  receipts: ReceiptSummary[]
  /** What this list is, in this placement. Also names the table for a screen
   *  reader, so the three tabs that show one are told apart by ear. */
  caption?: string
  /** What to say when the list is empty. The default states the general case;
   *  a placement that filters says what its own filter found nothing of. */
  empty?: string
}) {
  if (receipts.length === 0) {
    return (
      <div className="rounded-[10px] border border-border bg-card px-4 py-3">
        {caption ? (
          <p className="mb-1 text-[12px] font-semibold text-foreground">{caption}</p>
        ) : null}
        <p className="text-[12px] text-muted-foreground">{empty}</p>
      </div>
    )
  }

  return (
    <div className="overflow-x-auto rounded-[10px] border border-border bg-card">
      <table className="w-full border-collapse">
        {caption ? (
          <caption className="px-3 pt-3 text-left text-[12px] font-semibold text-foreground">
            {caption}
          </caption>
        ) : null}
        <thead>
          <tr className="border-b border-border">
            <th className={HEAD} scope="col">Command</th>
            <th className={HEAD} scope="col">Tool</th>
            <th className={HEAD} scope="col">Java</th>
            <th className={HEAD} scope="col">Exit</th>
            <th className={HEAD} scope="col">Reports</th>
            <th className={HEAD} scope="col">Tests</th>
          </tr>
        </thead>
        <tbody>
          {receipts.map((receipt) => (
            <tr
              className="border-t border-border first:border-t-0"
              data-receipt={receipt.receiptId}
              key={receipt.receiptId}
            >
              <td className={CELL} data-column="command">
                <div className="max-w-[46ch] break-all font-mono text-[12px]">{receipt.argv}</div>
                {receipt.actualCwd ? (
                  <div className="mt-0.5 max-w-[46ch] break-all text-[11px] text-muted-foreground">
                    {receipt.actualCwd}
                  </div>
                ) : null}
              </td>
              <td className={CELL} data-column="tool" title={receipt.toolchain?.version ?? undefined}>
                <span className="line-clamp-2 max-w-[24ch] break-words">{toolText(receipt)}</span>
              </td>
              <td className={`${CELL} whitespace-nowrap font-mono`} data-column="java">
                {javaText(receipt)}
              </td>
              <td
                className={`${CELL} font-mono tabular-nums ${
                  typeof receipt.exitCode === "number" && receipt.exitCode !== 0
                    ? "text-status-failed"
                    : ""
                }`}
                data-column="exit"
              >
                {typeof receipt.exitCode === "number" ? receipt.exitCode : ABSENT}
              </td>
              <td className={`${CELL} whitespace-nowrap font-mono tabular-nums`} data-column="reports">
                {reportsText(receipt)}
              </td>
              <td className={`${CELL} font-mono tabular-nums`} data-column="tests">
                {typeof receipt.testsReported === "number"
                  ? receipt.testsReported.toLocaleString()
                  : ABSENT}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
