import type { ModuleSummary } from "@/api/types"

const MAX_NAMES = 5

interface FailGroup {
  name: string
  count: number
  names: string[]
  hidden: number
}

function toGroups(modules: ModuleSummary[]): FailGroup[] {
  return modules
    .filter((m) => (m.failingNames?.length ?? 0) > 0)
    .map((m) => {
      const names = m.failingNames ?? []
      const count = m.failingCount ?? names.length
      const shown = names.slice(0, MAX_NAMES)
      return {
        name: m.name,
        count,
        names: shown,
        hidden: Math.max(count - shown.length, 0),
      }
    })
}

/**
 * The "Needs attention" card on the Overview tab: per-module failing-test groups
 * followed by a row per build warning. Renders nothing when there's nothing to flag.
 * Markup/styling mirrors WorkbenchDetail.dc.html lines 182–200 (amber-bordered card).
 */
export function NeedsAttention({
  modules,
  warnings,
}: {
  modules: ModuleSummary[]
  warnings: string[]
}) {
  const groups = toGroups(modules)
  if (groups.length === 0 && warnings.length === 0) {
    return null
  }

  return (
    <section className="overflow-hidden rounded-xl border border-status-attention-border bg-card">
      <div className="flex items-center gap-2 border-b border-border px-4 py-3">
        <span className="h-[7px] w-[7px] rounded-full bg-status-failed" />
        <h2 className="text-[14px] font-bold text-foreground">Needs attention</h2>
      </div>
      {groups.map((g) => (
        <div key={g.name} className="border-t border-border px-4 py-3">
          <div className="mb-2 flex items-center gap-2">
            <span className="text-[13px] font-semibold text-foreground">{g.name}</span>
            <span className="font-mono text-[11px] text-status-failed">{g.count} failing</span>
          </div>
          {g.names.map((n) => (
            <div key={n} className="pl-3.5 font-mono text-[12px] leading-[1.7] text-muted-foreground">
              {n}
            </div>
          ))}
          {g.hidden > 0 ? (
            <div className="pl-3.5 font-mono text-[12px] leading-[1.7] text-muted-foreground">
              +{g.hidden} more
            </div>
          ) : null}
        </div>
      ))}
      {warnings.map((w) => (
        <div key={w} className="flex items-center gap-2 border-t border-border px-4 py-3">
          <span className="h-[7px] w-[7px] rounded-full bg-status-attention" />
          <span className="text-[12px] text-muted-foreground">{w}</span>
        </div>
      ))}
    </section>
  )
}
