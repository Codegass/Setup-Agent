import { Badge } from "@/components/common/Badge"
import { cn } from "@/lib/utils"

import type { FacetId, FacetMeta } from "./facets"

export function SectionNav({
  facets,
  active,
  onJump,
}: {
  facets: FacetMeta[]
  active: string | null
  onJump: (id: FacetId) => void
}) {
  return (
    <nav className="flex flex-col gap-0.5" aria-label="Detail sections">
      {facets.map((f) => {
        const on = active === f.id
        return (
          <button
            key={f.id}
            aria-current={on}
            className={cn(
              "group flex items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-[12.5px] transition-colors",
              on ? "bg-status-running-soft font-medium text-status-running" : "text-slate-500 hover:bg-slate-100 hover:text-slate-700",
            )}
            onClick={() => onJump(f.id)}
            type="button"
          >
            <f.icon className={on ? "text-status-running" : "text-slate-400"} size={14} />
            <span className="flex-1">{f.label}</span>
            {f.count != null ? <Badge tone={f.countTone}>{f.count}</Badge> : null}
          </button>
        )
      })}
    </nav>
  )
}
