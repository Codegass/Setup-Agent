import { cn } from "@/lib/utils"

import type { FacetId, FacetMeta } from "./facets"

export function FacetTabs({
  facets,
  active,
  onJump,
}: {
  facets: FacetMeta[]
  active: string | null
  onJump: (id: FacetId) => void
}) {
  return (
    <nav
      aria-label="Detail sections"
      className="sticky top-0 z-[var(--z-sticky)] flex items-center gap-1 overflow-x-auto border-b border-slate-200 bg-white/85 px-5 py-2 backdrop-blur-md sm:px-7"
    >
      {facets.map((f) => {
        const on = active === f.id
        return (
          <button
            key={f.id}
            aria-current={on}
            className={cn(
              "inline-flex shrink-0 items-center gap-1.5 rounded-full px-3 py-1 text-[12px] font-medium transition-colors",
              on ? "bg-slate-900 text-white" : "text-slate-500 hover:bg-slate-100",
            )}
            onClick={() => onJump(f.id)}
            type="button"
          >
            <f.icon size={13} />
            {f.label}
            {f.count != null ? (
              <span
                className={cn(
                  "rounded-full px-1.5 text-[10px] tabular-nums",
                  on ? "bg-white/20" : f.countTone === "red" ? "bg-status-failed-soft text-status-failed" : "bg-slate-200 text-slate-600",
                )}
              >
                {f.count}
              </span>
            ) : null}
          </button>
        )
      })}
    </nav>
  )
}
