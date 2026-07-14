import { X } from "lucide-react"

export function FailingCard({
  names,
  hiddenCount = 0,
  evidenceRef,
}: {
  names: string[]
  hiddenCount?: number
  evidenceRef?: string | null
}) {
  if (!names.length) {
    return null
  }
  return (
    <div className="overflow-hidden rounded-lg border border-status-failed-border">
      <div className="border-b border-status-failed-border bg-status-failed-soft/60 px-4 py-2">
        <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-status-failed">
          Failing · {names.length + hiddenCount}
        </span>
      </div>
      <div className="divide-y divide-status-failed-border/40">
        {names.map((n) => (
          <div key={n} className="flex items-center gap-2 px-4 py-2">
            <X className="shrink-0 text-status-failed" size={13} />
            <span className="truncate font-mono text-[12px] text-foreground">{n}</span>
          </div>
        ))}
        {hiddenCount > 0 ? (
          <div className="px-4 py-2 font-mono text-[10px] text-muted-foreground">
            +{hiddenCount} more{evidenceRef ? ` — full list at ${evidenceRef}` : ""}
          </div>
        ) : null}
      </div>
    </div>
  )
}
