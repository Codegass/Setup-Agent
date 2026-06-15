import { ChevronDown } from "lucide-react"
import { Fragment, useState } from "react"

import type { ModuleSummary } from "@/api/types"
import { cn } from "@/lib/utils"

function statusClass(s: string): string {
  if (s === "success") return "bg-emerald-50 text-emerald-700"
  if (s === "failure") return "bg-red-50 text-red-600"
  if (s === "skipped") return "bg-slate-100 text-slate-500"
  return "bg-amber-50 text-amber-700"
}

function num(n?: number | null): string {
  return typeof n === "number" && Number.isFinite(n) ? n.toLocaleString() : "—"
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
  variant: "build" | "test"
}) {
  const [open, setOpen] = useState<string | null>(null)
  const ordered = [...modules].sort((a, b) => failureRank(a) - failureRank(b))

  return (
    <table className="w-full border-collapse">
      <thead>
        <tr className="border-b border-slate-200 font-mono text-[10px] uppercase tracking-[0.1em] text-slate-500">
          <th className="px-2 py-2 text-left">Module</th>
          <th className="px-2 py-2 text-left">Build</th>
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
                <td className="px-2 py-2">
                  <span className={cn("rounded px-2 py-0.5 text-[10px]", statusClass(m.buildStatus ?? "unknown"))}>
                    {(m.buildStatus ?? "unknown").toUpperCase()}
                  </span>
                  {m.buildSource === "partial" ? (
                    <span className="ml-1.5 rounded bg-amber-50 px-1 py-0.5 text-[9px] text-amber-700">
                      partial
                    </span>
                  ) : null}
                </td>
                {variant === "build" ? (
                  <>
                    <td className="px-2 py-2 text-right">{num(m.classCount)}</td>
                    <td className="px-2 py-2 text-right">{num(m.jarCount)}</td>
                    <td className="px-2 py-2">
                      {canExpandBuild ? (
                        <button className="text-red-600 underline decoration-dotted" type="button"
                          onClick={() => setOpen(isOpen ? null : m.path)}>
                          {errs.length} error{errs.length > 1 ? "s" : ""}
                          <ChevronDown className={cn("ml-1 inline", isOpen && "rotate-180")} size={12} />
                        </button>
                      ) : m.buildStatus === "skipped" ? (
                        <span className="text-slate-400">upstream failed</span>
                      ) : <span className="text-slate-300">—</span>}
                    </td>
                  </>
                ) : (
                  <>
                    <td className="px-2 py-2 text-right text-emerald-700">{num(m.testsPassed)}</td>
                    <td className={cn("px-2 py-2 text-right", (m.testsFailed ?? 0) > 0 && "text-red-600")}>{num(m.testsFailed)}</td>
                    <td className="px-2 py-2 text-right">{num(m.testsSkipped)}</td>
                    <td className="px-2 py-2">
                      {canExpandTest ? (
                        <button className="text-red-600 underline decoration-dotted" type="button"
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
                <tr className="bg-red-50/60">
                  <td colSpan={variant === "build" ? 5 : 6} className="px-3 py-2">
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
                    <div className="max-h-48 overflow-auto font-mono text-[11px] text-red-700">
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
