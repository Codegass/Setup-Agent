import type { ReactNode } from "react"
import {
  ArrowUpRight,
  Check,
  ChevronDown,
  CircleHelp,
  CircleX,
} from "lucide-react"

import type {
  CardTone,
  ResultCard,
  ResultDimensions,
  ResultRow,
} from "@/api/types"
import { cn } from "@/lib/utils"
import type { TabId } from "./facets"
import { LocalResults } from "./LocalResults"

const TONE: Record<CardTone, string> = {
  success: "text-status-success",
  attention: "text-status-attention",
  failed: "text-status-failed",
  neutral: "text-muted-foreground",
}
const FOCUS =
  "focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-ring"
const count = (n: number | null) =>
  n == null ? "—" : n.toLocaleString("en-US")

function Status({ tone, children }: { tone: CardTone; children: string }) {
  const Icon =
    tone === "success" ? Check : tone === "failed" ? CircleX : CircleHelp
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 text-[13px] font-semibold",
        TONE[tone],
      )}
    >
      <Icon aria-hidden="true" size={15} />
      {children}
    </span>
  )
}

/** Keep the original facts available. Never reverse-parse prose for charts. */
function RowFacts({ row }: { row?: ResultRow }) {
  if (!row) return null
  return (
    <div className="space-y-2 text-[12px] leading-relaxed text-muted-foreground [overflow-wrap:anywhere]">
      <p>{row.headline}</p>
      {row.detail ? <p>{row.detail}</p> : null}
      {row.reason ? <p>{row.reason}</p> : null}
      {(row.items?.length ?? 0) > 0 ? (
        <ul className="space-y-1.5 border-l-2 border-border pl-3">
          {row.items!.map((item, index) => (
            <li className="font-mono" key={`${index}-${item}`}>
              {item}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}

/** Shared scale even when this run exceeds CI. Null is not a recorded zero.
 *  These are counts, not a CI-attainment verdict. */
function CountComparison({
  label,
  actual,
  target,
  children,
}: {
  label: string
  actual: number | null
  target: number | null
  children: ReactNode
}) {
  const scale = Math.max(actual ?? 0, target ?? 0, 1)
  const comparable = actual != null && target != null
  return (
    <details className="group/count">
      <summary
        aria-label={`${label}: this run ${actual == null ? "not recorded" : count(actual)}, CI ${target == null ? "not recorded" : count(target)}`}
        className={cn(
          "grid cursor-pointer list-none grid-cols-[5.25rem_minmax(0,1fr)_7.5rem_1rem] items-center gap-3 rounded-sm py-2.5 text-[13px] hover:bg-muted/40 [&::-webkit-details-marker]:hidden",
          FOCUS,
        )}
      >
        <span className="font-medium">{label}</span>
        <span aria-hidden="true" className="relative h-4">
          <span
            className={cn(
              "absolute inset-x-0 top-1.5 h-1 rounded-full",
              comparable
                ? "bg-accent"
                : "border-t border-dashed border-muted-foreground/40",
            )}
          />
          {comparable ? (
            <span
              className="absolute top-0 h-4 border-r-2 border-muted-foreground/60 bg-muted-foreground/15"
              style={{ width: `${(target! / scale) * 100}%` }}
            />
          ) : null}
          {comparable ? (
            <span
              className="absolute top-1 h-2 rounded-r-sm bg-primary"
              style={{ width: `${(actual! / scale) * 100}%` }}
            />
          ) : null}
        </span>
        <span className="text-right font-mono text-[12px] tabular-nums">
          <span className="font-semibold text-foreground">{count(actual)}</span>
          <span className="text-muted-foreground"> / {count(target)}</span>
        </span>
        <ChevronDown
          aria-hidden="true"
          size={14}
          className="text-muted-foreground transition-transform group-open/count:rotate-180"
        />
      </summary>
      <div className="pb-3 pt-1 text-[12px] leading-relaxed text-muted-foreground [overflow-wrap:anywhere]">
        {children}
      </div>
    </details>
  )
}

const CHECK_LABELS: Record<
  ResultDimensions["sagChecks"][number]["check"],
  string
> = {
  "java requirement decided": "Java requirement",
  "built modules confirmed": "Built modules",
  "test outcome graded": "Test outcome",
  "test reports tied to the command": "Report binding",
}

export function MeasuredResultBand({
  card,
  dimensions,
  onOpenTab,
  availableTabs,
}: {
  card: ResultCard
  dimensions: ResultDimensions
  onOpenTab: (id: TabId) => void
  availableTabs?: TabId[]
}) {
  const {
    ciCommand: command,
    officialCi: ci,
    sagChecks: checks,
    runControl: control,
  } = dimensions
  const row = (key: ResultRow["key"]) =>
    card.rows.find((item) => item.key === key)
  const commandRow = row("ci_command"),
    officialRow = row("official_ci"),
    runRow = row("run"),
    summary = row("summary")
  const unconfirmed = checks.filter((check) => !check.confirmed)
  const hasCiReference =
    dimensions.hasCiReference ?? (ci.basis !== "unavailable" || ci.cell != null)
  const ciModules = ci.ciModules == null ? null : [...new Set(ci.ciModules)]
  const builtModules =
    ci.agentModulesBuilt == null ? null : new Set(ci.agentModulesBuilt)
  // Count the intersection, as the backend does. Extra modules cannot replace
  // missing modules in the pinned CI cell.
  const matchedModules =
    ciModules == null || builtModules == null
      ? null
      : ciModules.filter((name) => builtModules.has(name)).length
  const link = (id: TabId, label: string) =>
    availableTabs == null || availableTabs.includes(id) ? (
      <button
        type="button"
        onClick={() => onOpenTab(id)}
        className={cn(
          "inline-flex items-center gap-1 py-1 text-[12px] font-medium text-primary hover:underline",
          FOCUS,
        )}
      >
        {label}
        <ArrowUpRight aria-hidden="true" size={13} />
      </button>
    ) : null
  const commandLabel = command
    ? {
        passed: "Passed",
        failed: "Failed",
        not_finished: "Not finished",
        unavailable: "Not available",
      }[command.result]
    : "No bound task"
  const commandTone = commandRow?.tone ?? "neutral"
  const pins = command
    ? [
        command.pinnedJava.length
          ? `Java ${command.pinnedJava.join(" / ")}`
          : null,
        command.pinnedMaven.length
          ? `Maven ${command.pinnedMaven.join(" / ")}`
          : null,
      ]
        .filter(Boolean)
        .join(" · ")
    : ""

  return (
    <section
      aria-label="Run result"
      className="min-w-0 text-foreground"
      data-tone="neutral"
    >
      {card.verdictSource === "legacy" ? (
        <p className="mb-3 text-[12px] text-muted-foreground">
          reconstructed from an older run record
        </p>
      ) : null}

      <LocalResults
        dimensions={dimensions}
        onOpenTab={onOpenTab}
        availableTabs={availableTabs}
      />
      {!hasCiReference &&
      !dimensions.build &&
      !dimensions.tests &&
      officialRow?.detail ? (
        <p className="mb-3 text-[13px] leading-relaxed">{officialRow.detail}</p>
      ) : null}

      {command ? (
        <details
          className="group/command border-t border-border pt-3"
          data-row="ci_command"
        >
          <summary
            className={cn(
              "flex cursor-pointer list-none flex-wrap items-center gap-x-4 gap-y-2 rounded-sm [&::-webkit-details-marker]:hidden",
              FOCUS,
            )}
          >
            <h3 className="text-[13px] font-semibold">Task verification</h3>
            <Status tone={commandTone}>{commandLabel}</Status>
            <span className="ml-auto font-mono text-[12px] tabular-nums text-muted-foreground">
              {command && command.stepsTotal > 0
                ? `${command.stepsPassed} / ${command.stepsTotal} steps`
                : ""}
            </span>
            <ChevronDown
              aria-hidden="true"
              size={16}
              className="text-muted-foreground transition-transform group-open/command:rotate-180"
            />
            {command && command.stepsTotal > 0 ? (
              <span
                role="progressbar"
                aria-label="Required steps passed"
                aria-valuenow={command.stepsPassed}
                aria-valuemin={0}
                aria-valuemax={command.stepsTotal}
                className="h-1.5 basis-full overflow-hidden rounded-full bg-accent"
              >
                <span
                  className="block h-full bg-status-success"
                  style={{
                    width: `${(command.stepsPassed / command.stepsTotal) * 100}%`,
                  }}
                />
              </span>
            ) : null}
          </summary>
          <div className="mt-3 rounded-md bg-muted/40 p-3">
            <RowFacts row={commandRow} />
            {command?.toolchainReason ? (
              <p className="mt-2 text-[12px] text-muted-foreground">
                {command.toolchainReason}
              </p>
            ) : null}
            {command?.sourceDetail ? (
              <p className="mt-2 text-[12px] text-muted-foreground">
                {command.sourceDetail}
              </p>
            ) : null}
            {link("evidence", "Command receipts")}
          </div>
        </details>
      ) : null}
      {command?.firstUnpassedStep ? (
        <p
          className={cn(
            "mt-2 text-[13px] [overflow-wrap:anywhere]",
            TONE[commandTone],
          )}
        >
          {command.firstUnpassedStep.id} ·{" "}
          {command.firstUnpassedStep.status.replace(/_/g, " ")}
          {command.firstUnpassedStep.exitCode != null
            ? ` · exit ${command.firstUnpassedStep.exitCode}`
            : ""}
        </p>
      ) : null}
      {command ? (
        <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-[12px]">
          <span
            className={
              command.toolchainAsPinned === "yes"
                ? "text-muted-foreground"
                : "text-status-attention"
            }
          >
            {pins || "Toolchain"} ·{" "}
            {command.toolchainAsPinned === "yes"
              ? "as pinned"
              : command.toolchainAsPinned === "no"
                ? "differs from pin"
                : "not confirmed"}
          </span>
          <span
            className={
              command.sourceUnchanged === "yes"
                ? "text-muted-foreground"
                : "text-status-attention"
            }
          >
            {command.sourceUnchanged === "yes"
              ? command.sourceDetail
                ? "Source check passed"
                : "Sources unchanged"
              : command.sourceUnchanged === "no"
                ? "Sources changed"
                : "Source integrity not confirmed"}
          </span>
        </div>
      ) : null}

      {hasCiReference ? (
        <section
          className="mt-4 border-t border-border pt-3"
          data-row="official_ci"
          aria-label="Official CI counts"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-[13px] font-semibold">CI comparison</h3>
            <span className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <span
                aria-hidden="true"
                className="h-2 w-3 rounded-sm bg-primary"
              />
              This run<span className="mx-0.5">/</span>
              <span
                aria-hidden="true"
                className="h-2 w-3 border-r-2 border-muted-foreground/60 bg-muted-foreground/15"
              />
              CI
            </span>
          </div>
          {ci.basis === "unavailable" ? (
            <p className="mt-2 text-[12px] text-muted-foreground">
              {ci.reason}
            </p>
          ) : null}
          <CountComparison
            label="Modules"
            actual={matchedModules}
            target={ciModules?.length ?? null}
          >
            {ciModules == null ? (
              <p>CI did not supply a module list.</p>
            ) : ciModules.length === 0 ? (
              <p>The CI module list is empty.</p>
            ) : (
              <ul className="grid gap-1.5 sm:grid-cols-2">
                {ciModules.map((name) => (
                  <li className="flex items-center gap-2" key={name}>
                    {builtModules?.has(name) ? (
                      <Check
                        aria-hidden="true"
                        size={13}
                        className="shrink-0 text-status-success"
                      />
                    ) : (
                      <CircleHelp
                        aria-hidden="true"
                        size={13}
                        className="shrink-0 text-status-attention"
                      />
                    )}
                    <span className="font-mono">{name}</span>
                    <span className="sr-only">
                      {builtModules?.has(name)
                        ? "built"
                        : "not confirmed built"}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {builtModules == null ? (
              <p>This run did not confirm a built module list.</p>
            ) : ciModules == null ? (
              <p>{builtModules.size} built modules recorded in this run.</p>
            ) : null}
            {link("build", "Build details")}
          </CountComparison>
          <CountComparison
            label="Tests run"
            actual={ci.agentTestsExecuted}
            target={ci.ciTestsExecuted}
          >
            <p>
              {count(ci.agentFailures)} failures · {count(ci.agentErrors)}{" "}
              errors in this run · {count(ci.ciTestsRed)} red tests in CI
            </p>
            <p className="mt-1">
              Counts do not establish matching test identities.
            </p>
            {link("tests", "Test results")}
          </CountComparison>
          {(ci.agentFailures != null && ci.agentFailures > 0) ||
          (ci.agentErrors != null && ci.agentErrors > 0) ? (
            <p className="mb-2 text-[12px] font-medium text-status-failed">
              {count(ci.agentFailures)} failures · {count(ci.agentErrors)}{" "}
              errors
            </p>
          ) : null}
          <details className="group/record">
            <summary
              className={cn(
                "flex cursor-pointer list-none items-center gap-1.5 rounded-sm py-1 text-[12px] text-muted-foreground [&::-webkit-details-marker]:hidden",
                FOCUS,
              )}
            >
              <ChevronDown
                aria-hidden="true"
                size={13}
                className="shrink-0 transition-transform group-open/record:rotate-180"
              />
              <span className="min-w-0 truncate">
                {ci.cell ?? "Recorded build and test facts"}
              </span>
            </summary>
            <div className="mt-2 pb-2">
              <RowFacts row={officialRow} />
              {link("ci", "CI record")}
            </div>
          </details>
        </section>
      ) : null}

      <details
        key={`${card.runId}-${unconfirmed.length}`}
        open={unconfirmed.length > 0}
        className="group/checks mt-3 border-t border-border pt-3"
        data-row="unconfirmed"
      >
        <summary
          className={cn(
            "flex cursor-pointer list-none flex-wrap items-center gap-2 rounded-sm [&::-webkit-details-marker]:hidden",
            FOCUS,
          )}
        >
          <h3 className="text-[13px] font-semibold">SAG checks</h3>
          <span
            className={cn(
              "ml-auto text-[12px]",
              unconfirmed.length
                ? "text-status-attention"
                : "text-muted-foreground",
            )}
          >
            {unconfirmed.length
              ? `${unconfirmed.length} not confirmed`
              : `${checks.length} confirmed`}
          </span>
          <ChevronDown
            aria-hidden="true"
            size={14}
            className="text-muted-foreground transition-transform group-open/checks:rotate-180"
          />
        </summary>
        <div className="mt-3 grid gap-x-6 gap-y-3 sm:grid-cols-2">
          {checks.map((check) => (
            <div
              key={check.check}
              className="flex items-start gap-2 text-[12px]"
            >
              {check.confirmed ? (
                <Check
                  aria-hidden="true"
                  size={15}
                  className="mt-0.5 shrink-0 text-status-success"
                />
              ) : (
                <CircleHelp
                  aria-hidden="true"
                  size={15}
                  className="mt-0.5 shrink-0 text-status-attention"
                />
              )}
              <div className="min-w-0">
                <p
                  className={cn(
                    "font-medium",
                    check.confirmed
                      ? "text-muted-foreground"
                      : "text-status-attention",
                  )}
                >
                  {CHECK_LABELS[check.check]}
                  <span className="sr-only">
                    : {check.confirmed ? "confirmed" : "not confirmed"}
                  </span>
                </p>
                {check.reason ? (
                  <p className="mt-1 leading-relaxed text-muted-foreground [overflow-wrap:anywhere]">
                    {check.reason}
                  </p>
                ) : null}
              </div>
            </div>
          ))}
        </div>
        {unconfirmed.length ? (
          <div className="mt-2">{link("evidence", "Check evidence")}</div>
        ) : null}
      </details>

      <div className="mt-3 flex flex-wrap items-start justify-between gap-x-6 gap-y-2 border-t border-border pt-3">
        <details className="group/run min-w-0" data-row="run">
          <summary
            className={cn(
              "flex cursor-pointer list-none flex-wrap items-center gap-2 rounded-sm text-[12px] [&::-webkit-details-marker]:hidden",
              FOCUS,
            )}
          >
            <span className="font-semibold">Run</span>
            <span className={TONE[runRow?.tone ?? "neutral"]}>
              {runRow?.headline ?? control.ended.replace(/_/g, " ")}
            </span>
            <ChevronDown
              aria-hidden="true"
              size={13}
              className="text-muted-foreground transition-transform group-open/run:rotate-180"
            />
          </summary>
          <div className="mt-2">
            <RowFacts row={runRow} />
            {link("flow", "Open flow")}
          </div>
        </details>
        <div className="flex items-center gap-3" data-row="summary">
          <span className="text-[12px] font-semibold">Summary</span>
          <Status tone={summary?.tone ?? "neutral"}>{card.verdict}</Status>
        </div>
      </div>
      <div className="mt-3 flex flex-wrap items-start gap-x-6 gap-y-2 text-[12px] text-muted-foreground">
        {(["coverage", "report"] as const).map((key) => {
          const fact = row(key)
          if (!fact) return null
          return (
            <details className="group/extra min-w-0" data-row={key} key={key}>
              <summary
                className={cn(
                  "flex cursor-pointer list-none items-center gap-1.5 rounded-sm [&::-webkit-details-marker]:hidden",
                  FOCUS,
                )}
              >
                <span>{fact.label}</span>
                <span>· {fact.status.replace(/_/g, " ")}</span>
                <ChevronDown
                  aria-hidden="true"
                  size={12}
                  className="transition-transform group-open/extra:rotate-180"
                />
              </summary>
              <div className="mt-2">
                <RowFacts row={fact} />
                {link(
                  key === "coverage" ? "tests" : "report",
                  key === "coverage" ? "Coverage details" : "Open report",
                )}
              </div>
            </details>
          )
        })}
      </div>
    </section>
  )
}
