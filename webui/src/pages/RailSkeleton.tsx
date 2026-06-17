import { cn } from "@/lib/utils"

const ROW_KEYS = ["r1", "r2", "r3", "r4", "r5", "r6"]

/** First-load placeholder shaped like the workspace rail. */
export function RailSkeleton({ className }: { className?: string }) {
  return (
    <aside
      aria-label="Loading workspaces"
      className={cn("flex h-full w-[320px] shrink-0 flex-col border-r border-slate-200 bg-white", className)}
      role="status"
    >
      <div className="space-y-3 border-b border-slate-200 px-4 pb-3 pt-4">
        <div className="h-6 w-40 animate-pulse rounded bg-slate-100" />
        <div className="h-9 w-full animate-pulse rounded-md bg-slate-100" />
        <div className="flex gap-2">
          <div className="h-12 flex-1 animate-pulse rounded-lg bg-slate-100" />
          <div className="h-12 flex-1 animate-pulse rounded-lg bg-slate-100" />
          <div className="h-12 flex-1 animate-pulse rounded-lg bg-slate-100" />
        </div>
      </div>
      <div className="flex-1 space-y-px overflow-hidden px-3.5 py-2">
        {ROW_KEYS.map((key) => (
          <div key={key} className="flex items-center gap-3 py-2">
            <div className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-slate-200" />
            <div className="flex-1 space-y-1.5">
              <div className="h-3 w-2/3 animate-pulse rounded bg-slate-100" />
              <div className="h-2.5 w-1/2 animate-pulse rounded bg-slate-100" />
            </div>
          </div>
        ))}
      </div>
      <span className="sr-only">Loading workspaces…</span>
    </aside>
  )
}
