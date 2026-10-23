import { ChevronDown, Clock3 } from "lucide-react"

import type { TrajectoryTurn } from "@/api/types"
import { iterationUsage } from "@/lib/flow-usage"
import { formatDuration, formatTokens } from "@/lib/trajectory"

/** One model response can dispatch several tools; its bill lives on the
 * iteration, not on every tool. Exact input/output counts are one click away. */
export function IterationUsage({ turns }: { turns: TrajectoryTurn[] }) {
  const usage = iterationUsage(turns)
  const total = (v: typeof usage.actor) => v ? formatTokens(v.input + v.output) : "—"
  const rows = [
    { name: "Actor", value: usage.actor, show: usage.hasActor, className: "text-foreground" },
    { name: "Advisor", value: usage.advisor, show: usage.hasAdvisor || usage.advisor != null, className: "text-advisor" },
  ].filter(r => r.show)
  const time = usage.durationMs == null ? "—" : formatDuration(Math.round(usage.durationMs))
  return (
    <details className="group min-w-0 text-[11px]">
      <summary aria-label="Iteration usage" className="flex cursor-pointer list-none flex-wrap items-center justify-end gap-x-3 gap-y-1 rounded-sm text-muted-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring [&::-webkit-details-marker]:hidden">
        {rows.map(r => <span key={r.name} className={r.className}>{r.name} <span className="font-mono tabular-nums">{total(r.value)}</span></span>)}
        {!rows.length ? <span>{turns.length ? "Engine" : "Tokens —"}</span> : null}
        <span className="inline-flex items-center gap-1" title="Recorded action time; excludes model response time">
          <Clock3 size={12} aria-hidden="true" />
          <span className="font-mono tabular-nums">{time}{usage.partialTime && usage.durationMs != null ? "+" : ""}</span>
          <span className="sr-only"> action time</span>
        </span>
        <ChevronDown size={12} aria-hidden="true" className="transition-transform group-open:rotate-180" />
      </summary>
      <div className="mt-2 space-y-2 rounded-md bg-muted px-3 py-2 text-muted-foreground">
        {rows.length ? <table className="w-full text-right tabular-nums">
          <thead><tr><th className="text-left font-medium">Tokens</th><th className="pl-3 font-medium">Input</th><th className="pl-3 font-medium">Output</th></tr></thead>
          <tbody>{rows.map(r => <tr key={r.name} className={r.className}>
            <th className="text-left font-medium">{r.name}</th>
            <td className="pl-3 font-mono">{r.value?.input.toLocaleString("en-US") ?? "—"}</td>
            <td className="pl-3 font-mono">{r.value?.output.toLocaleString("en-US") ?? "—"}</td>
          </tr>)}</tbody>
        </table> : <p>{turns.length ? "No model response was attributed to this entry." : "Usage is not recorded for this entry."}</p>}
        <p>Action time: {time}{usage.partialTime && usage.durationMs != null ? " (partial)" : ""}. Overlapping calls count once; model response time is excluded.</p>
        {rows.length ? <p>Tokens are counted once per recorded response. — means not recorded.</p> : null}
      </div>
    </details>
  )
}
