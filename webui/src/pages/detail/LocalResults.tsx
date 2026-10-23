import { ArrowUpRight, ChevronDown } from "lucide-react"
import type { ReactNode } from "react"

import type { CardTone, ResultDimensions, LocalResultRow } from "@/api/types"
import { cn } from "@/lib/utils"
import type { TabId } from "./facets"

const TONE: Record<CardTone, string> = {
  success: "text-status-success",
  attention: "text-status-attention",
  failed: "text-status-failed",
  neutral: "text-muted-foreground",
}
const fmt = (n: number | null) => (n == null ? "—" : n.toLocaleString("en-US"))

/** Local counts are from the run, never borrowed from its CI comparison. */
export function TestMeasurements({
  tests,
}: {
  tests: NonNullable<ResultDimensions["tests"]>
}) {
  const counts = tests.counts
  if (!counts)
    return (
      <p className="text-[13px] text-muted-foreground">{tests.fact.headline}</p>
    )
  const parts = [
    { key: "passed", label: "passed", color: "bg-status-success" },
    { key: "failed", label: "failed", color: "bg-status-failed" },
    { key: "errors", label: "errors", color: "bg-status-attention" },
    { key: "skipped", label: "skipped", color: "bg-muted-foreground/40" },
  ] as const
  const total = Math.max(
    counts.executed,
    parts.reduce((sum, p) => sum + Math.max(counts[p.key], 0), 0),
    1,
  )
  return (
    <div>
      <div className="font-mono text-[24px] font-semibold tabular-nums">
        {tests.lowerBound ? "≥ " : ""}
        {fmt(counts.executed)}
        <span className="ml-2 font-sans text-[12px] font-normal text-muted-foreground">
          recorded
        </span>
      </div>
      <div
        role="img"
        aria-label={parts
          .map((p) => `${fmt(counts[p.key])} ${p.label}`)
          .join(", ")}
        className="my-3 flex h-2 overflow-hidden rounded-sm bg-accent"
      >
        {parts.map((p) => (
          <span
            key={p.key}
            className={p.color}
            style={{ width: `${(Math.max(counts[p.key], 0) / total) * 100}%` }}
          />
        ))}
      </div>
      <dl className="flex flex-wrap gap-x-4 gap-y-1 text-[12px]">
        {parts.map((p) => (
          <div
            key={p.key}
            className={cn(
              "flex items-baseline gap-1",
              (p.key === "failed" || p.key === "errors") &&
                counts[p.key] > 0 &&
                "font-semibold text-status-failed",
            )}
          >
            <dd className="font-mono tabular-nums">{fmt(counts[p.key])}</dd>
            <dt>{p.label}</dt>
          </div>
        ))}
      </dl>
    </div>
  )
}

function LocalSection({
  title,
  fact,
  children,
  details,
  onOpen,
}: {
  title: string
  fact: LocalResultRow
  children: ReactNode
  details: ReactNode
  onOpen?: () => void
}) {
  return (
    <section aria-label={`${title} result`} className="min-w-0">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-[16px] font-semibold">{title}</h2>
        <span className={cn("text-[12px] font-medium", TONE[fact.tone])}>
          {fact.status}
        </span>
      </div>
      {children}
      <details className="group/local mt-3 text-[12px] text-muted-foreground">
        <summary className="flex w-fit cursor-pointer list-none items-center gap-1 rounded-sm focus-visible:outline-2 focus-visible:outline-ring [&::-webkit-details-marker]:hidden">
          Details{" "}
          <ChevronDown
            aria-hidden="true"
            size={12}
            className="transition-transform group-open/local:rotate-180"
          />
        </summary>
        <div className="mt-2 space-y-2 leading-relaxed [overflow-wrap:anywhere]">
          {details}
          {fact.reason ? <p>{fact.reason}</p> : null}
          {onOpen ? (
            <button
              type="button"
              className="inline-flex items-center gap-1 py-1 font-medium text-primary hover:underline focus-visible:outline-2 focus-visible:outline-ring"
              onClick={onOpen}
            >
              Open {title.toLowerCase()} details{" "}
              <ArrowUpRight aria-hidden="true" size={13} />
            </button>
          ) : null}
        </div>
      </details>
    </section>
  )
}

export function LocalResults({
  dimensions,
  onOpenTab,
  availableTabs,
}: {
  dimensions: ResultDimensions
  onOpenTab: (id: TabId) => void
  availableTabs?: TabId[]
}) {
  const { build, tests } = dimensions
  if (!build && !tests) return null
  const action = (tab: TabId) =>
    availableTabs == null || availableTabs.includes(tab)
      ? () => onOpenTab(tab)
      : undefined
  const measuredModules =
    build?.modulesBuilt != null &&
    build.modulesTotal != null &&
    build.modulesTotal > 0 &&
    build.modulesBuilt >= 0 &&
    build.modulesBuilt <= build.modulesTotal
  return (
    <div className="grid gap-5 pb-4 sm:grid-cols-2 sm:gap-7">
      {build ? (
        <LocalSection
          title="Build"
          fact={build.fact}
          onOpen={action("build")}
          details={
            <>
              <p>{build.fact.headline}</p>
              <p>Module counts cover the recorded build.</p>
            </>
          }
        >
          <div className="font-mono text-[24px] font-semibold tabular-nums">
            {fmt(build.modulesBuilt)}
            <span className="text-[16px] font-normal text-muted-foreground">
              {" "}
              / {fmt(build.modulesTotal)}
            </span>
            <span className="ml-2 font-sans text-[12px] font-normal text-muted-foreground">
              modules built
            </span>
          </div>
          <div
            role="img"
            aria-label={`Recorded modules built: ${fmt(build.modulesBuilt)} of ${fmt(build.modulesTotal)}`}
            className={cn(
              "my-3 h-2 overflow-hidden rounded-sm",
              measuredModules
                ? "bg-accent"
                : "border-t border-dashed border-muted-foreground/40",
            )}
          >
            {measuredModules ? (
              <span
                className="block h-full bg-primary"
                style={{
                  width: `${(build.modulesBuilt! / build.modulesTotal!) * 100}%`,
                }}
              />
            ) : null}
          </div>
          <dl className="flex flex-wrap gap-x-4 gap-y-1 text-[12px]">
            <div className="flex items-baseline gap-1">
              <dd className="font-mono tabular-nums">
                {fmt(build.classFiles)}
              </dd>
              <dt>class files</dt>
            </div>
            <div className="flex items-baseline gap-1">
              <dd className="font-mono tabular-nums">{fmt(build.jars)}</dd>
              <dt>JARs</dt>
            </div>
          </dl>
        </LocalSection>
      ) : null}
      {tests ? (
        <LocalSection
          title="Tests"
          fact={tests.fact}
          onOpen={action("tests")}
          details={
            <>
              <p>{tests.fact.headline}</p>
              {tests.fact.detail ? <p>{tests.fact.detail}</p> : null}
              {tests.flakyCount > 0 ? (
                <p>{fmt(tests.flakyCount)} flaky tests</p>
              ) : null}
            </>
          }
        >
          <TestMeasurements tests={tests} />
        </LocalSection>
      ) : null}
    </div>
  )
}
