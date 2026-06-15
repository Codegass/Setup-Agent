import type * as React from "react"

import type { BuildSummary } from "@/api/types"

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1">
      <span className="text-[12px] text-slate-600">{label}</span>
      <span className="font-mono text-[12px] text-slate-800">{value}</span>
    </div>
  )
}

export function BuildDetails({ build }: { build: BuildSummary }) {
  const samples = build.artifactSamples ?? []
  const warnings = build.warnings ?? []
  const hasSummary =
    build.classCount != null || build.jarCount != null || build.system != null

  if (!hasSummary && !samples.length && !warnings.length) {
    return (
      <p className="text-[12px] text-slate-500">
        No structured build evidence available
        {build.system ? ` (${build.system})` : ""}.
      </p>
    )
  }

  return (
    <div className="space-y-3">
      <section>
        <h4 className="mb-1 font-mono text-[10px] uppercase tracking-[0.1em] text-slate-500">
          Artifact Summary
        </h4>
        {build.system ? <Row label="Build system" value={build.system} /> : null}
        {build.classCount != null ? <Row label="Class files" value={build.classCount.toLocaleString()} /> : null}
        {build.jarCount != null ? <Row label="JAR files" value={build.jarCount.toLocaleString()} /> : null}
        {build.moduleOutputCount != null ? (
          <Row label="Modules with output" value={build.moduleOutputCount} />
        ) : null}
      </section>

      {samples.length ? (
        <section>
          <h4 className="mb-1 font-mono text-[10px] uppercase tracking-[0.1em] text-slate-500">
            Evidence Samples · Physical artifact scan
          </h4>
          <ul className="space-y-0.5">
            {samples.map((s) => (
              <li key={s} className="truncate font-mono text-[11px] text-slate-600">{s}</li>
            ))}
          </ul>
        </section>
      ) : null}

      {warnings.length ? (
        <section>
          <h4 className="mb-1 font-mono text-[10px] uppercase tracking-[0.1em] text-amber-600">
            Warnings
          </h4>
          <ul className="space-y-0.5">
            {warnings.map((w) => (
              <li key={w} className="text-[12px] leading-relaxed text-slate-700">{w}</li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  )
}
