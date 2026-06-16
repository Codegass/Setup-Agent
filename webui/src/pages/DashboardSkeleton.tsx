import { Card } from "@/components/common/Card"

const ROW_KEYS = ["r1", "r2", "r3", "r4"]
const TILE_KEYS = ["t1", "t2", "t3"]

/** First-load placeholder that mirrors the dashboard's summary strip + list. */
export function DashboardSkeleton() {
  return (
    <main
      aria-label="Loading workspaces"
      className="mx-auto max-w-[1180px] px-4 py-5 sm:px-6 lg:px-8 lg:py-7"
      role="status"
    >
      <div className="h-7 w-44 animate-pulse rounded bg-slate-100" />
      <div className="mt-5 grid gap-3 sm:grid-cols-3">
        {TILE_KEYS.map((key) => (
          <div key={key} className="h-[72px] animate-pulse rounded-lg bg-slate-100" />
        ))}
      </div>
      <Card className="mt-5 overflow-hidden">
        {ROW_KEYS.map((key) => (
          <div
            key={key}
            className="flex items-center gap-3 border-b border-slate-100 px-4 py-3 last:border-b-0"
          >
            <div className="h-7 w-7 shrink-0 animate-pulse rounded-md bg-slate-100" />
            <div className="flex-1 space-y-2">
              <div className="h-3 w-1/3 animate-pulse rounded bg-slate-100" />
              <div className="h-2.5 w-1/2 animate-pulse rounded bg-slate-100" />
            </div>
          </div>
        ))}
      </Card>
      <span className="sr-only">Loading workspaces…</span>
    </main>
  )
}
