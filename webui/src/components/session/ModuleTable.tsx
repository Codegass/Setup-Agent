import { ChevronDown } from "lucide-react"
import { Fragment, useState } from "react"

import type { ModuleSummary } from "@/api/types"
import { cn } from "@/lib/utils"

function statusClass(s: string): string {
  if (s === "success") return "bg-status-success-soft text-status-success"
  if (s === "failure") return "bg-status-failed-soft text-status-failed"
  if (s === "skipped") return "bg-slate-100 text-slate-500"
  return "bg-status-attention-soft text-status-attention"
}

function num(n?: number | null): string {
  return typeof n === "number" && Number.isFinite(n) ? n.toLocaleString() : "—"
}

function passRate(p?: number | null, f?: number | null): string {
  const pass = p ?? 0, fail = f ?? 0, denom = pass + fail
  return denom > 0 ? `${((pass / denom) * 100).toFixed(1).replace(/\.0$/, "")}%` : "—"
}

function covColor(rate: number): string {
  return rate >= 80
    ? "var(--status-success)"
    : rate >= 50
      ? "var(--status-attention)"
      : "var(--status-failed)"
}

function covTextClass(rate: number): string {
  return rate >= 80 ? "text-status-success" : rate >= 50 ? "text-status-attention" : "text-status-failed"
}

function buildLabel(s: string): string {
  if (s === "success") return "Built"
  if (s === "failure") return "Failed"
  if (s === "skipped") return "Skipped"
  return "Unknown"
}

function buildDotClass(s: string): string {
  if (s === "success") return "bg-status-success"
  if (s === "failure") return "bg-status-failed"
  if (s === "skipped") return "bg-slate-400"
  return "bg-status-attention"
}

function buildTextClass(s: string): string {
  if (s === "success") return "text-status-success"
  if (s === "failure") return "text-status-failed"
  if (s === "skipped") return "text-slate-500"
  return "text-status-attention"
}

function pct1(n: number): string {
  return `${n.toFixed(1).replace(/\.0$/, "")}%`
}

function ProgressBar({ rate, color }: { rate: number; color: string }) {
  return (
    <span className="block h-[5px] overflow-hidden rounded-full bg-slate-100">
      <span
        className="block h-full rounded-full"
        style={{ width: `${Math.max(0, Math.min(100, rate))}%`, background: color }}
      />
    </span>
  )
}

function OverviewRow({ m }: { m: ModuleSummary }) {
  const status = m.buildStatus ?? "unknown"
  const pass = m.testsPassed ?? 0
  const total = m.testsTotal ?? pass + (m.testsFailed ?? 0)
  const fc = m.failingCount ?? 0
  const failing = fc > 0
  const testRate = total > 0 ? (pass / total) * 100 : 0
  const testColor = failing ? "var(--status-failed)" : "var(--status-success)"
  return (
    <div
      className="grid items-center gap-3 border-t border-slate-100 px-4 py-3"
      style={{ gridTemplateColumns: "1.5fr 0.8fr 1.3fr 1fr 1fr" }}
    >
      <div className="min-w-0">
        <div className="text-[13px] font-semibold text-slate-800">{m.name}</div>
        <div className="truncate font-mono text-[11px] text-slate-400">{m.path}</div>
      </div>
      <div>
        <span className={cn("inline-flex items-center gap-1.5 text-[12px] font-semibold", buildTextClass(status))}>
          <span className={cn("h-1.5 w-1.5 rounded-full", buildDotClass(status))} />
          {buildLabel(status)}
        </span>
      </div>
      <div>
        {total > 0 ? (
          <>
            <div className="mb-1.5 flex items-center justify-between font-mono text-[12px] text-slate-600">
              <span>{`${pass.toLocaleString()} / ${total.toLocaleString()}`}</span>
              {failing ? <span className="font-mono text-[11px] text-status-failed">{fc} failing</span> : null}
            </div>
            <ProgressBar rate={testRate} color={testColor} />
          </>
        ) : (
          <span className="font-mono text-[12px] text-slate-400">—</span>
        )}
      </div>
      <div>
        {m.lineRate != null ? (
          <>
            <div className="mb-1.5 font-mono text-[12px] text-slate-700">{pct1(m.lineRate)}</div>
            <ProgressBar rate={m.lineRate} color={covColor(m.lineRate)} />
          </>
        ) : (
          <span className="font-mono text-[12px] text-slate-400">—</span>
        )}
      </div>
      <div>
        {m.branchRate != null ? (
          <>
            <div className="mb-1.5 font-mono text-[12px] text-slate-700">{pct1(m.branchRate)}</div>
            <ProgressBar rate={m.branchRate} color={covColor(m.branchRate)} />
          </>
        ) : (
          <span className="font-mono text-[12px] text-slate-400">—</span>
        )}
      </div>
    </div>
  )
}

function CoverageBar({ label, rate }: { label: string; rate: number }) {
  return (
    <div className="flex items-center gap-2 font-mono text-[11px]">
      <span className="w-2 text-slate-500">{label}</span>
      <span className="inline-block h-[7px] w-24 overflow-hidden rounded-full bg-slate-200">
        <span className="block h-full" style={{ width: `${Math.max(0, Math.min(100, rate))}%`, background: covColor(rate) }} />
      </span>
      <span className={cn("w-9 font-semibold", covTextClass(rate))}>{Math.round(rate)}%</span>
    </div>
  )
}

function failureRank(m: ModuleSummary): number {
  if (m.buildStatus === "failure") return 0
  if ((m.failingCount ?? 0) > 0) return 1
  if (m.buildStatus === "skipped") return 2
  return 3
}

export function ModuleTable({
  modules,
  variant,
}: {
  modules: ModuleSummary[]
  variant: "build" | "test" | "overview"
}) {
  const [open, setOpen] = useState<string | null>(null)
  const ordered = [...modules].sort((a, b) => failureRank(a) - failureRank(b))

  if (variant === "overview") {
    return (
      <div>
        <div
          className="grid gap-3 bg-slate-50 px-4 py-2 font-mono text-[10px] uppercase tracking-[0.06em] text-slate-400"
          style={{ gridTemplateColumns: "1.5fr 0.8fr 1.3fr 1fr 1fr" }}
        >
          <div>Module</div>
          <div>Build</div>
          <div>Tests</div>
          <div>Line cov</div>
          <div>Branch cov</div>
        </div>
        {ordered.map((m) => (
          <OverviewRow key={m.path} m={m} />
        ))}
      </div>
    )
  }

  return (
    <table className="w-full border-collapse">
      <thead>
        <tr className="border-b border-slate-200 font-mono text-[10px] uppercase tracking-[0.1em] text-slate-500">
          <th className="px-2 py-2 text-left">Module</th>
          {variant === "build" ? <th className="px-2 py-2 text-left">Build</th> : null}
          {variant === "build" ? (
            <>
              <th className="px-2 py-2 text-right">Classes</th>
              <th className="px-2 py-2 text-right">JARs</th>
              <th className="px-2 py-2 text-left">Detail</th>
            </>
          ) : (
            <>
              <th className="px-2 py-2 text-right">Pass</th>
              <th className="px-2 py-2 text-right">Fail</th>
              <th className="px-2 py-2 text-right">Skip</th>
              <th className="px-2 py-2 text-left">Rate</th>
              <th className="px-2 py-2 text-left">Coverage</th>
              <th className="px-2 py-2 text-left">Failing methods</th>
            </>
          )}
        </tr>
      </thead>
      <tbody>
        {ordered.map((m) => {
          const isOpen = open === m.path
          const depth = m.path === "." ? 0 : m.path.split("/").length - 1
          const failing = m.failingNames ?? []
          const fc = m.failingCount ?? 0
          const hidden = Math.max(fc - failing.length, 0)
          const errs = m.buildErrorSamples ?? []
          const canExpandTest = variant === "test" && fc > 0
          const canExpandBuild = variant === "build" && m.buildStatus === "failure" && errs.length > 0
          return (
            <Fragment key={m.path}>
              <tr className="border-b border-slate-100 font-mono text-[12px] tabular-nums">
                <td className="px-2 py-2" style={{ paddingLeft: 8 + depth * 14 }}>{m.name}</td>
                {variant === "build" ? (
                  <td className="px-2 py-2">
                    <span className={cn("rounded px-2 py-0.5 text-[10px]", statusClass(m.buildStatus ?? "unknown"))}>
                      {(m.buildStatus ?? "unknown").toUpperCase()}
                    </span>
                    {m.buildSource === "partial" ? (
                      <span className="ml-1.5 rounded bg-status-attention-soft px-1 py-0.5 text-[9px] text-status-attention">
                        partial
                      </span>
                    ) : null}
                  </td>
                ) : null}
                {variant === "build" ? (
                  <>
                    <td className="px-2 py-2 text-right">{num(m.classCount)}</td>
                    <td className="px-2 py-2 text-right">{num(m.jarCount)}</td>
                    <td className="px-2 py-2">
                      {canExpandBuild ? (
                        <button className="text-status-failed underline decoration-dotted" type="button"
                          onClick={() => setOpen(isOpen ? null : m.path)}>
                          {errs.length} error{errs.length > 1 ? "s" : ""}
                          <ChevronDown className={cn("ml-1 inline", isOpen && "rotate-180")} size={12} />
                        </button>
                      ) : m.buildStatus === "skipped" ? (
                        <span className="text-slate-500">upstream failed</span>
                      ) : <span className="text-slate-300">—</span>}
                    </td>
                  </>
                ) : (
                  <>
                    <td className="px-2 py-2 text-right text-status-success">{num(m.testsPassed)}</td>
                    <td className={cn("px-2 py-2 text-right", (m.testsFailed ?? 0) > 0 && "text-status-failed")}>{num(m.testsFailed)}</td>
                    <td className="px-2 py-2 text-right">{num(m.testsSkipped)}</td>
                    <td className="px-2 py-2">{passRate(m.testsPassed, m.testsFailed)}</td>
                    <td className="px-2 py-2" style={{ minWidth: 150 }}>
                      {m.lineRate == null && m.branchRate == null ? (
                        <span className="text-slate-500">— not measured</span>
                      ) : (
                        <div className="space-y-0.5">
                          {m.lineRate != null ? <CoverageBar label="L" rate={m.lineRate} /> : null}
                          {m.branchRate != null ? <CoverageBar label="B" rate={m.branchRate} /> : null}
                        </div>
                      )}
                    </td>
                    <td className="px-2 py-2">
                      {canExpandTest ? (
                        <button className="text-status-failed underline decoration-dotted" type="button"
                          onClick={() => setOpen(isOpen ? null : m.path)}>
                          View {fc} failure{fc > 1 ? "s" : ""}
                          <ChevronDown className={cn("ml-1 inline", isOpen && "rotate-180")} size={12} />
                        </button>
                      ) : <span className="text-slate-300">—</span>}
                    </td>
                  </>
                )}
              </tr>
              {isOpen ? (
                <tr className="bg-status-failed-soft/60">
                  <td colSpan={variant === "build" ? 5 : 7} className="px-3 py-2">
                    <div className="mb-1.5 flex flex-wrap items-center gap-3 font-mono text-[10px] text-slate-500">
                      {variant === "test" && failing.length ? (
                        <button
                          className="rounded border border-slate-300 px-1.5 py-0.5 hover:bg-white"
                          onClick={() => navigator.clipboard?.writeText(failing.join("\n"))}
                          type="button"
                        >
                          Copy all
                        </button>
                      ) : null}
                      {(m.evidenceRefs ?? [])[0] ? (
                        <span>report: <span className="text-slate-600">{(m.evidenceRefs ?? [])[0]}</span></span>
                      ) : null}
                    </div>
                    <div className="max-h-48 overflow-auto font-mono text-[11px] text-status-failed">
                      {(variant === "test" ? failing : errs).map((line) => (
                        <div key={line} className="py-0.5">{line}</div>
                      ))}
                      {variant === "test" && hidden > 0 ? (
                        <div className="text-slate-500">+{hidden} more — full list at {(m.evidenceRefs ?? [])[0] ?? "report dir"}</div>
                      ) : null}
                    </div>
                  </td>
                </tr>
              ) : null}
            </Fragment>
          )
        })}
      </tbody>
    </table>
  )
}
