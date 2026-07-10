import type { BuildSummary, WorkspaceSummary } from "@/api/types"
import { Tooltip } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"

/** Fleet-wide rollup band shown above the detail pane: cumulative build success,
 * test pass rate, and execution rate across every workspace on the dashboard. */

interface Rollup {
  total: number
  running: number
  buildSuccess: number
  buildKnown: number
  passed: number
  executedNonSkip: number
  executed: number
  declared: number
}

function buildState(build: BuildSummary | string): string {
  return (typeof build === "string" ? build : build.state).trim().toLowerCase()
}

export function rollup(workspaces: WorkspaceSummary[]): Rollup {
  const r: Rollup = {
    total: workspaces.length,
    running: 0,
    buildSuccess: 0,
    buildKnown: 0,
    passed: 0,
    executedNonSkip: 0,
    executed: 0,
    declared: 0,
  }
  for (const w of workspaces) {
    if (w.docker.status === "running") r.running += 1
    const state = buildState(w.build)
    if (state && state !== "none" && state !== "unknown") {
      r.buildKnown += 1
      if (state === "success" || state === "green" || state === "passed") r.buildSuccess += 1
    }
    const t = w.test
    if (t && t.total > 0) {
      r.passed += t.pass
      // Skips are "not run", not failures — matches SAG's own pass-rate rule.
      r.executedNonSkip += t.pass + t.fail + (t.errors ?? 0)
      r.executed += t.total
      if (t.declaredTotal && t.declaredTotal > 0) r.declared += t.declaredTotal
    }
  }
  return r
}

function pct(numerator: number, denominator: number): string | null {
  if (denominator <= 0) return null
  return `${((100 * numerator) / denominator).toFixed(1)}%`
}

function Stat({
  label,
  value,
  hint,
  tone = "neutral",
}: {
  label: string
  value: string
  hint: string
  tone?: "neutral" | "good" | "warn"
}) {
  return (
    <Tooltip label={hint} side="bottom">
      <div className="flex flex-col rounded-lg border border-slate-200 bg-white px-3 py-1.5">
        <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-slate-400">
          {label}
        </span>
        <span
          className={cn(
            "text-[15px] font-bold leading-tight",
            tone === "good" && "text-emerald-600",
            tone === "warn" && "text-amber-600",
            tone === "neutral" && "text-slate-800",
          )}
        >
          {value}
        </span>
      </div>
    </Tooltip>
  )
}

function rateTone(rate: string | null): "good" | "warn" | "neutral" {
  if (rate === null) return "neutral"
  return parseFloat(rate) >= 80 ? "good" : "warn"
}

export function SummaryStrip({ workspaces }: { workspaces: WorkspaceSummary[] }) {
  if (workspaces.length === 0) return null
  const r = rollup(workspaces)

  const buildRate = pct(r.buildSuccess, r.buildKnown)
  const passRate = pct(r.passed, r.executedNonSkip)
  const execRate = pct(r.executed, r.declared)

  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-slate-200 bg-[#fbfbfc] px-5 py-2.5 sm:px-6">
      <span className="mr-1 font-mono text-[10px] uppercase tracking-[0.12em] text-slate-400">
        Fleet
      </span>
      <Stat
        label="Workspaces"
        value={`${r.total}`}
        hint={`${r.total} workspaces on the dashboard, ${r.running} running`}
      />
      {buildRate !== null ? (
        <Stat
          label="Build success"
          value={buildRate}
          tone={rateTone(buildRate)}
          hint={`${r.buildSuccess} of ${r.buildKnown} workspaces with a known build state built successfully`}
        />
      ) : null}
      {passRate !== null ? (
        <Stat
          label="Pass rate"
          value={passRate}
          tone={rateTone(passRate)}
          hint={`${r.passed.toLocaleString()} passed of ${r.executedNonSkip.toLocaleString()} executed (pass+fail+errors; skips excluded)`}
        />
      ) : null}
      {execRate !== null ? (
        <Stat
          label="Execution rate"
          value={execRate}
          tone={rateTone(execRate)}
          hint={`${r.executed.toLocaleString()} tests executed of ${r.declared.toLocaleString()} declared test methods`}
        />
      ) : null}
    </div>
  )
}
