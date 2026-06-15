import type { TestSummary } from "@/api/types"

function fmt(n?: number | null): string {
  return typeof n === "number" && Number.isFinite(n) ? n.toLocaleString() : "—"
}
function pct(n?: number | null): string {
  return typeof n === "number" && Number.isFinite(n) ? `${n.toFixed(1)}%` : "—"
}

// Method coverage = executed methods / declared methods. It is only a valid
// coverage figure when the static catalog is a complete denominator (rate
// in (0, 100]). When more unique methods ran than were statically declared the
// rate exceeds 100% -- a sign the catalog undercounts, not real >100% coverage.
export function isValidCoverage(rate?: number | null): rate is number {
  return typeof rate === "number" && Number.isFinite(rate) && rate >= 0 && rate <= 100
}

function CalcRow({ label, value, source }: { label: string; value: string; source?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1">
      <span className="text-[12px] text-slate-600">
        {label}
        {source ? <span className="ml-1.5 font-mono text-[10px] text-slate-400">{source}</span> : null}
      </span>
      <span className="font-mono text-[12px] tabular-nums text-slate-800">{value}</span>
    </div>
  )
}

export function TestDetails({ test }: { test: TestSummary }) {
  const failing = test.failingNames ?? []
  const conflicts = test.conflicts ?? []
  const refs = test.evidenceRefs ?? []
  const shownFailing = failing.slice(0, 20)

  return (
    <div className="space-y-3">
      <section>
        <h4 className="mb-1 font-mono text-[10px] uppercase tracking-[0.1em] text-slate-500">Calculation</h4>
        <CalcRow label="Runner executions" value={fmt(test.total)} source="Runner XML" />
        <CalcRow label="Passed executions" value={fmt(test.pass)} />
        <CalcRow label="Failed executions" value={fmt(test.fail)} />
        {test.errors != null ? <CalcRow label="Errored executions" value={fmt(test.errors)} /> : null}
        <CalcRow label="Skipped executions" value={fmt(test.skip)} />
        <CalcRow label="Unique methods" value={fmt(test.uniqueTotal)} source="Normalized runtime methods" />
        <CalcRow label="Declared methods" value={fmt(test.declaredTotal)} source="Static catalog" />
        {isValidCoverage(test.methodExecutionRate) ? (
          <CalcRow label="Method execution" value={pct(test.methodExecutionRate)} />
        ) : test.methodExecutionRate != null ? (
          <CalcRow label="Method execution" value="—" source="static catalog incomplete" />
        ) : (
          <CalcRow label="Method execution" value="—" />
        )}
      </section>

      {(failing.length || conflicts.length) ? (
        <section>
          <h4 className="mb-1 font-mono text-[10px] uppercase tracking-[0.1em] text-red-600">Attention</h4>
          {conflicts.map((c) => (
            <div key={c} className="font-mono text-[11px] text-amber-700">{c}</div>
          ))}
          {shownFailing.length ? (
            <ul className="mt-1 space-y-0.5">
              {shownFailing.map((f) => (
                <li key={f} className="truncate font-mono text-[11px] text-red-600">{f}</li>
              ))}
            </ul>
          ) : null}
          {failing.length > shownFailing.length ? (
            <div className="mt-1 text-[11px] text-slate-500">
              +{failing.length - shownFailing.length} more failing methods
            </div>
          ) : null}
        </section>
      ) : null}

      <section>
        <h4 className="mb-1 font-mono text-[10px] uppercase tracking-[0.1em] text-slate-500">Evidence Sources</h4>
        <CalcRow label="XML reports" value={fmt(test.reportFileCount)} source="Runner XML" />
        {refs.length ? (
          <div className="mt-1 flex flex-wrap gap-1.5">
            {refs.map((r) => (
              <span key={r} className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-600">{r}</span>
            ))}
          </div>
        ) : null}
      </section>
    </div>
  )
}
