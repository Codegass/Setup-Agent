import { Moon, Sun } from "lucide-react"

import type { SystemSummary } from "@/api/types"
import { Tooltip } from "@/components/ui/tooltip"

/** Top nav bar: launch entry point + live host/docker resource readouts. */

function bytes(value?: number | null): string | null {
  if (value == null) return null
  if (value >= 1 << 30) return `${(value / (1 << 30)).toFixed(1)} GB`
  return `${Math.round(value / (1 << 20))} MB`
}

function Readout({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <Tooltip label={hint} side="bottom">
      <div className="flex items-baseline gap-1.5">
        <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-muted-foreground">
          {label}
        </span>
        <span className="font-mono text-[12px] font-semibold text-foreground">{value}</span>
      </div>
    </Tooltip>
  )
}

export function NavBar({
  dark,
  onToggleTheme,
  system,
}: {
  dark: boolean
  onToggleTheme: () => void
  system: SystemSummary | null
}) {
  const disk = bytes(system?.dockerDiskUsed)
  const mem =
    system?.memTotal != null && system?.memUsed != null
      ? `${bytes(system.memUsed)} / ${bytes(system.memTotal)}`
      : null
  const cpu = system?.cpuLoad != null ? system.cpuLoad.toFixed(2) : null
  const reclaimable = bytes(system?.dockerReclaimable)

  return (
    <div className="flex items-center justify-between gap-3 border-b border-border bg-background px-5 py-2 sm:px-6">
      <span className="font-mono text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
        SAG Workbench
      </span>

      <div className="flex items-center gap-4">
        <div className="hidden items-center gap-4 sm:flex">
          {disk ? (
            <Readout
              label="Docker"
              value={disk}
              hint={`Docker disk in use${reclaimable ? `, ${reclaimable} reclaimable` : ""}`}
            />
          ) : null}
          {mem ? <Readout label="RAM" value={mem} hint="Host memory used / total" /> : null}
          {cpu ? <Readout label="Load" value={cpu} hint="Host 1-minute load average" /> : null}
        </div>
        <Tooltip label={dark ? "Switch to light mode" : "Switch to dark mode"} side="bottom">
          <button
            aria-label={dark ? "Switch to light mode" : "Switch to dark mode"}
            className="rounded-md border border-border p-1.5 text-muted-foreground hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)]"
            onClick={onToggleTheme}
            type="button"
          >
            {dark ? <Sun size={15} /> : <Moon size={15} />}
          </button>
        </Tooltip>
      </div>
    </div>
  )
}
