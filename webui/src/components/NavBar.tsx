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
        <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-slate-400">
          {label}
        </span>
        <span className="font-mono text-[12px] font-semibold text-slate-700">{value}</span>
      </div>
    </Tooltip>
  )
}

export function NavBar({ system }: { system: SystemSummary | null }) {
  const disk = bytes(system?.dockerDiskUsed)
  const mem =
    system?.memTotal != null && system?.memUsed != null
      ? `${bytes(system.memUsed)} / ${bytes(system.memTotal)}`
      : null
  const cpu = system?.cpuLoad != null ? system.cpuLoad.toFixed(2) : null
  const reclaimable = bytes(system?.dockerReclaimable)

  return (
    <div className="flex items-center justify-between gap-3 border-b border-slate-200 bg-[#fbfbfc] px-5 py-2 sm:px-6">
      <span className="font-mono text-[11px] uppercase tracking-[0.14em] text-slate-400">
        SAG Workbench
      </span>

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
    </div>
  )
}
