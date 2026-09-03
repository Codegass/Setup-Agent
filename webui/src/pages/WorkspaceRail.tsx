import { Activity, AlertTriangle, Check, CircleCheck, Clock, GitBranch, Hammer, Loader2, Rocket, Search, Trash2, X } from "lucide-react"
import { type MouseEvent as ReactMouseEvent, useState } from "react"

import type { DashboardResponse, LaunchQueueItem, LaunchQueueState, WorkspaceSummary } from "@/api/types"
import { rollup } from "@/components/SummaryStrip"
import { StatusBadge } from "@/components/common/Badge"
import { TestBar } from "@/components/common/TestBar"
import { statusMeta } from "@/components/common/status"
import {
  launchProjectName,
  launchStatusLine,
  pendingLaunchItems,
} from "@/components/launch/launchRows"
import {
  DeleteWorkspaceDialog,
  type DeleteWorkspaceTarget,
} from "@/components/workspace/DeleteWorkspaceDialog"
import { Tooltip } from "@/components/ui/tooltip"
import { formatAgo } from "@/lib/relativeTime"
import { lowerBoundEvidenceCounts } from "@/evidencePresentation"
import { readStored, writeStored } from "@/lib/safeStorage"
import { cn } from "@/lib/utils"

import { needsAttention, sortByAttentionFirst } from "./dashboardAttention"

function normalize(value: string | null | undefined): string {
  return value?.trim().toLowerCase() ?? ""
}

function buildState(build: WorkspaceSummary["build"]): string {
  return normalize(typeof build === "string" ? build : build.state)
}

function isFiniteCount(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
}

function visibleMetadata(value: string | null | undefined): string | null {
  const normalized = normalize(value)
  return normalized && !["unknown", "none", "unavailable", "n/a", "-", "—"].includes(normalized)
    ? value!.trim()
    : null
}

function evidenceObservationLabel(test: WorkspaceSummary["test"]): string | null {
  const layers = test.evidenceLayers?.tests
  if (!layers) return null
  let known = 0
  let files = 0
  let incomplete = false
  for (const observations of [
    layers.quarantinedObservations,
    layers.unattributedObservations,
    layers.staleObservations,
  ]) {
    if (isFiniteCount(observations.executed)) {
      known += observations.executed
      if (observations.availability === "partial" || observations.bound === "lower") {
        incomplete = true
      }
    } else if (observations.availability === "unavailable" || observations.executed == null) {
      incomplete = true
    }
    if (isFiniteCount(observations.reportFileCount)) files += observations.reportFileCount
  }
  if (known > 0) {
    return `${incomplete ? "At least " : ""}${known.toLocaleString()} additional diagnostics were excluded from the sealed run result`
  }
  if (incomplete) {
    return "Additional diagnostic observations exist, but their total is unavailable; they are excluded from the sealed run result"
  }
  return files > 0
    ? `Additional diagnostics exist in ${files.toLocaleString()} report files and are excluded from the sealed run result`
    : null
}

function buildBucket(state: string): "success" | "partial" | "failed" | "unavailable" {
  if (["success", "green", "passed", "pass"].includes(state)) return "success"
  if (["partial", "incomplete"].includes(state)) return "partial"
  if (["failure", "failed", "red"].includes(state)) return "failed"
  return "unavailable"
}

const DOT_TONE: Record<string, string> = {
  neutral: "bg-status-idle", blue: "bg-status-running", green: "bg-status-success",
  red: "bg-status-failed", amber: "bg-status-attention",
}

function RailRow({
  workspace,
  selected,
  highlighted,
  deleting = false,
  selectMode = false,
  checked = false,
  onToggleCheck,
  onSelect,
}: {
  workspace: WorkspaceSummary
  selected: boolean
  highlighted: boolean
  deleting?: boolean
  selectMode?: boolean
  checked?: boolean
  onToggleCheck?: (id: string) => void
  onSelect: (id: string) => void
}) {
  const dockerNorm = normalize(workspace.docker.status)
  const dot = DOT_TONE[statusMeta(workspace.docker.status).tone] ?? DOT_TONE.neutral
  const build = buildBucket(buildState(workspace.build))
  const attention = needsAttention(workspace)
  const subjectCounts = workspace.test.evidenceLayers?.tests.claimed.latestSubjects
  const subjectValues = subjectCounts
    ? [subjectCounts.executed, subjectCounts.passed, subjectCounts.failed, subjectCounts.errors, subjectCounts.skipped]
    : []
  const subjectsAvailable = !!subjectCounts
    && subjectCounts.availability !== "unavailable"
    && subjectCounts.availability !== "partial"
    && subjectCounts.bound !== "lower"
    && subjectValues.every(isFiniteCount)
    && (subjectCounts.executed as number) === (
      (subjectCounts.passed as number)
      + (subjectCounts.failed as number)
      + (subjectCounts.errors as number)
      + (subjectCounts.skipped as number)
    )
  const claimedTotal = subjectsAvailable ? subjectCounts.executed as number : 0
  const claimedPassed = subjectsAvailable ? subjectCounts.passed as number : 0
  const claimedFailed = subjectsAvailable ? (subjectCounts.failed as number) + (subjectCounts.errors as number) : 0
  const subjectLowerBound = lowerBoundEvidenceCounts(subjectCounts)
  const runErrors = workspace.test.errors ?? 0
  const runValues = [workspace.test.pass, workspace.test.fail, runErrors, workspace.test.skip, workspace.test.total]
  const runComponents = workspace.test.pass + workspace.test.fail + runErrors + workspace.test.skip
  const runState = normalize(workspace.test.state)
  const runMeasured = !!runState
    && !["none", "unknown", "unavailable", "pending", "not-run", "not_run"].includes(runState)
    && runValues.every(isFiniteCount)
  const runAvailable = runMeasured && runComponents > 0
  const runFailed = workspace.test.fail + runErrors
  const observationLabel = evidenceObservationLabel(workspace.test)
  const identityLabel = subjectCounts
    ? subjectsAvailable
      ? `Per-test results: ${claimedPassed.toLocaleString()} passed and ${claimedFailed.toLocaleString()} failed or errored of ${claimedTotal.toLocaleString()}`
      : subjectLowerBound
        ? `Per-test results: at least ${subjectLowerBound.executed.toLocaleString()} kept; list truncated`
        : "Per-test results unavailable"
    : null
  const stack = visibleMetadata(workspace.stack)
  const commit = visibleMetadata(workspace.commit)
  const runBreakdown = [
    `${workspace.test.pass.toLocaleString()} passed`,
    workspace.test.fail ? `${workspace.test.fail.toLocaleString()} failed` : null,
    runErrors ? `${runErrors.toLocaleString()} errors` : null,
    workspace.test.skip ? `${workspace.test.skip.toLocaleString()} skipped` : null,
  ].filter(Boolean).join(", ")

  const body = (
    <>
      <span className={cn("relative inline-flex h-1.5 w-1.5 shrink-0 rounded-full", dot)}>
        {!deleting && (dockerNorm === "running" || dockerNorm === "launching") ? (
          <span className={cn("absolute inline-flex h-full w-full animate-ping rounded-full opacity-75", dot)} />
        ) : null}
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5">
          <span className={cn("truncate text-[13px] font-medium", selected && !selectMode ? "text-status-running" : "text-foreground")}>
            {workspace.project}
          </span>
          {workspace.release ? <span className="shrink-0 font-mono text-[9.5px] text-muted-foreground">{workspace.release}</span> : null}
          {workspace.activeSession ? <Activity className="shrink-0 text-status-running" size={11} /> : null}
        </span>
        {stack || commit ? (
          <span className="mt-0.5 block truncate font-mono text-[10px] text-muted-foreground">
            {[stack, commit].filter(Boolean).join(" · ")}
          </span>
        ) : null}
      </span>
      <span className="flex shrink-0 items-center gap-2">
        {deleting ? (
          <span className="inline-flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
            <Loader2 className="animate-spin" size={11} /> deleting…
          </span>
        ) : (
          <>
            <Tooltip
              label={
                build === "success"
                  ? "Build succeeded"
                  : build === "partial"
                    ? "Build partially completed"
                    : build === "failed"
                    ? "Build failed"
                    : "Build result unavailable"
              }
            >
              {build === "success" ? (
                <Check className="text-status-success" size={13} />
              ) : build === "partial" ? (
                <Clock className="text-status-attention" size={12} />
              ) : build === "failed" ? (
                <X className="text-status-failed" size={13} />
              ) : (
                <Clock className="text-muted-foreground" size={12} />
              )}
            </Tooltip>
            {runAvailable ? (
              <Tooltip
                label={[
                  `Sealed run: ${runBreakdown}`,
                  identityLabel,
                  observationLabel,
                ].filter(Boolean).join(". ")}
              >
                <TestBar fail={runFailed} pass={workspace.test.pass} total={runComponents} />
              </Tooltip>
            ) : runMeasured ? (
              <Tooltip label={["Sealed run: 0 results", identityLabel, observationLabel].filter(Boolean).join(". ")}>
                <span className="w-10 text-right font-mono text-[10px] text-muted-foreground">0</span>
              </Tooltip>
            ) : subjectsAvailable && claimedTotal > 0 ? (
              <Tooltip label={[identityLabel, observationLabel].filter(Boolean).join(". ")}>
                <TestBar fail={claimedFailed} pass={claimedPassed} total={claimedTotal} />
              </Tooltip>
            ) : subjectLowerBound ? (
              <Tooltip label={[identityLabel, subjectCounts?.reason ? `Source note: ${subjectCounts.reason}` : null, observationLabel].filter(Boolean).join(". ")}>
                <span className="w-10 text-right font-mono text-[10px] text-status-attention">
                  ≥{subjectLowerBound.executed.toLocaleString()}
                </span>
              </Tooltip>
            ) : (
              <Tooltip label={["Sealed run results unavailable", identityLabel, observationLabel].filter(Boolean).join(". ")}>
                <span className="w-10 text-right font-mono text-[10px] text-muted-foreground">—</span>
              </Tooltip>
            )}
          </>
        )}
      </span>
    </>
  )

  if (selectMode) {
    return (
      <label
        className={cn(
          "flex w-full cursor-pointer items-center gap-3 border-b border-border px-3.5 py-2.5 last:border-b-0",
          checked ? "bg-status-running-soft" : "hover:bg-accent",
        )}
      >
        <input
          aria-label={`Select ${workspace.project}`}
          checked={checked}
          className="h-3.5 w-3.5 shrink-0 accent-[var(--primary)]"
          onChange={() => onToggleCheck?.(workspace.id)}
          type="checkbox"
        />
        {body}
      </label>
    )
  }

  return (
    <button
      aria-current={selected}
      aria-label={`Open workspace ${workspace.project}`}
      className={cn(
        "group flex w-full items-center gap-3 border-b border-border px-3.5 py-2.5 text-left transition-colors last:border-b-0",
        deleting
          ? "cursor-default opacity-60"
          : selected
            ? "bg-status-running-soft"
            : attention
              ? "bg-status-failed-soft/40 hover:bg-status-failed-soft/60"
              : "hover:bg-accent",
        highlighted && !selected && !deleting ? "bg-status-running-soft" : "",
      )}
      disabled={deleting}
      onClick={() => onSelect(workspace.id)}
      type="button"
    >
      {body}
    </button>
  )
}

function PendingRailRow({
  item,
  onRemove,
}: {
  item: LaunchQueueItem
  onRemove: (target: DeleteWorkspaceTarget) => void
}) {
  const project = launchProjectName(item)
  const failed = normalize(item.status) === "failed"
  const dot = DOT_TONE[statusMeta(item.status).tone] ?? DOT_TONE.neutral
  return (
    <div
      aria-label={`Pending launch ${project}`}
      className={cn(
        "flex w-full items-center gap-3 border-b border-border px-3.5 py-2.5 text-left last:border-b-0",
        failed ? "bg-status-failed-soft/40" : "bg-muted",
      )}
    >
      <span className={cn("inline-flex h-1.5 w-1.5 shrink-0 rounded-full", dot)} />
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5">
          <span className="truncate text-[13px] font-medium text-muted-foreground">{project}</span>
          {item.ref ? <span className="shrink-0 font-mono text-[9.5px] text-muted-foreground">{item.ref}</span> : null}
        </span>
        <span
          className={cn("mt-0.5 block truncate text-[10px]", failed ? "text-status-failed" : "text-muted-foreground")}
          title={failed ? item.error ?? undefined : undefined}
        >
          {launchStatusLine(item)}
        </span>
      </span>
      <span className="flex shrink-0 items-center gap-1.5">
        <StatusBadge status={item.status} />
        {failed ? (
          <Tooltip label="Remove this failed launch from the list">
            <button
              aria-label={`Remove failed launch ${project}`}
              className="rounded-md p-1 text-muted-foreground hover:bg-status-failed-soft hover:text-status-failed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-status-failed/40"
              onClick={() =>
                onRemove({ workspaceId: item.workspace_id, label: project, kind: "launch" })
              }
              type="button"
            >
              <Trash2 size={14} />
            </button>
          </Tooltip>
        ) : null}
      </span>
    </div>
  )
}

function Chip({ label, value, tone }: { label: string; value: number; tone?: "blue" | "red" }) {
  return (
    <div className="flex-1 rounded-lg border border-border bg-card px-3 py-2">
      <div className={cn("text-[18px] font-semibold tabular-nums", tone === "red" ? "text-status-failed" : tone === "blue" ? "text-status-running" : "text-foreground")}>
        {value}
      </div>
      <div className="font-mono text-[9px] uppercase tracking-[0.12em] text-muted-foreground">{label}</div>
    </div>
  )
}

function pct(num: number, den: number): number | null {
  if (!Number.isFinite(num) || !Number.isFinite(den)) return null
  return den > 0 && num >= 0 && num <= den ? (100 * num) / den : null
}

function StatCard({
  icon: Icon,
  label,
  rate,
  detail,
  hint,
  value,
  tone: toneOverride,
}: {
  icon: typeof Hammer
  label: string
  rate: number | null
  detail: string
  hint: string
  value?: string
  tone?: "good" | "warn" | "neutral"
}) {
  const tone = toneOverride === "good"
    ? "text-status-success"
    : toneOverride === "warn"
      ? "text-status-attention"
      : toneOverride === "neutral"
        ? "text-muted-foreground"
        : rate == null
          ? "text-muted-foreground"
          : rate >= 80 ? "text-status-success" : "text-status-attention"
  return (
    <Tooltip className="min-w-0" label={hint} side="bottom">
      <div className="w-full rounded-lg border border-border bg-card px-2.5 py-2">
        <div className="flex items-center gap-1.5">
          <Icon className={cn("shrink-0", tone)} size={14} />
          <span className={cn("text-[17px] font-bold leading-none tabular-nums", tone)}>
            {value ?? (rate == null ? "—" : `${rate.toFixed(0)}%`)}
          </span>
        </div>
        <div className="mt-1.5 font-mono text-[8.5px] uppercase tracking-[0.1em] text-muted-foreground">
          {label}
        </div>
        <div className="font-mono text-[10px] font-medium tabular-nums text-foreground">{detail}</div>
      </div>
    </Tooltip>
  )
}

/** Fleet rollup shown in the sidebar under the workspace chips. */
function RailSummary({ workspaces }: { workspaces: WorkspaceSummary[] }) {
  if (!workspaces.length) return null
  const r = rollup(workspaces)
  const runPass = pct(r.passed, r.executedNonSkip)
  const measuredRunLabel = `${r.runResultMeasured} measured workspace${r.runResultMeasured === 1 ? "" : "s"}`
  const buildDetail = [
    r.buildSuccess ? `${r.buildSuccess} ok` : null,
    r.buildPartial ? `${r.buildPartial} partial` : null,
    r.buildFailed ? `${r.buildFailed} failed` : null,
    r.buildUnavailable ? `${r.buildUnavailable} n/a` : null,
  ].filter(Boolean).join(" · ")
  return (
    <div className="mt-2 grid grid-cols-2 gap-2">
      <StatCard
        detail={buildDetail}
        hint={`${r.buildSuccess} passed, ${r.buildPartial} partial, ${r.buildFailed} failed, and ${r.buildUnavailable} unavailable`}
        icon={Hammer}
        label="Build coverage"
        rate={null}
        tone={r.buildFailed || r.buildPartial ? "warn" : r.buildSuccess ? "good" : "neutral"}
        value={`${r.buildKnown}/${r.total}`}
      />
      <StatCard
        detail={runPass === null
          ? `${r.runResultMeasured}/${r.total} measured`
          : `${r.runResultMeasured}/${r.total} measured workspaces`}
        hint={runPass === null
          ? `${r.runResultMeasured} of ${r.total} workspaces have a sealed run result; there are no non-skipped results to rate`
          : `${r.passed.toLocaleString()} passed, ${r.failed.toLocaleString()} failed, and ${r.errors.toLocaleString()} errors across ${measuredRunLabel} only`}
        icon={CircleCheck}
        label="Run results"
        rate={runPass}
        tone={runPass === null && r.runResultMeasured < r.total ? "warn" : undefined}
        value={runPass === null ? `${r.runResultMeasured}/${r.total}` : undefined}
      />
    </div>
  )
}

export function WorkspaceRail({
  data,
  selectedId,
  onSelect,
  onLaunchSetups,
  launchQueue = null,
  onRemoveLaunch,
  onAfterSelect,
  onDeleteMany,
  deletingIds,
  highlightedWorkspaces = [],
  lastUpdatedAt = null,
  pollFailed = false,
  className,
}: {
  data: DashboardResponse
  selectedId: string | null
  onSelect: (id: string) => void
  onLaunchSetups: () => void
  launchQueue?: LaunchQueueState | null
  onRemoveLaunch?: (workspaceId: string) => Promise<void>
  onAfterSelect?: () => void
  onDeleteMany?: (ids: string[]) => Promise<void>
  deletingIds?: Set<string>
  highlightedWorkspaces?: string[]
  lastUpdatedAt?: number | null
  pollFailed?: boolean
  className?: string
}) {
  const [query, setQuery] = useState("")
  const [selectMode, setSelectMode] = useState(false)
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [batchConfirm, setBatchConfirm] = useState(false)
  const deleting = deletingIds ?? new Set<string>()
  // Selecting a workspace also runs onAfterSelect (used to close the mobile drawer).
  const handleSelect = (id: string) => {
    onSelect(id)
    onAfterSelect?.()
  }
  const togglePicked = (id: string) => {
    setPicked((prev) => {
      const next = new Set(prev)
      if (next.has(id)) {
        next.delete(id)
      } else {
        next.add(id)
      }
      return next
    })
  }
  const exitSelectMode = () => {
    setSelectMode(false)
    setPicked(new Set())
    setBatchConfirm(false)
  }
  const [removeTarget, setRemoveTarget] = useState<DeleteWorkspaceTarget | null>(null)
  const [railWidth, setRailWidth] = useState(() => {
    const saved = Number(readStored("sag.railWidth"))
    return saved >= 240 && saved <= 560 ? saved : 320
  })
  const startResize = (e: ReactMouseEvent) => {
    e.preventDefault()
    let latest = railWidth
    const onMove = (ev: globalThis.MouseEvent) => {
      latest = Math.min(560, Math.max(240, ev.clientX))
      setRailWidth(latest)
    }
    const onUp = () => {
      window.removeEventListener("mousemove", onMove)
      window.removeEventListener("mouseup", onUp)
      writeStored("sag.railWidth", String(latest))
    }
    window.addEventListener("mousemove", onMove)
    window.addEventListener("mouseup", onUp)
  }
  const ordered = sortByAttentionFirst(data.workspaces)
  const q = query.trim().toLowerCase()
  const rows = q
    ? ordered.filter((w) => w.project.toLowerCase().includes(q) || (w.stack ?? "").toLowerCase().includes(q))
    : ordered
  const running = data.workspaces.filter((w) => normalize(w.docker.status) === "running").length
  const pending = pendingLaunchItems(launchQueue, data.workspaces)
  // Pending rows respect the workspace filter so the search box also narrows them.
  const pendingRows = q
    ? pending.filter((item) => launchProjectName(item).toLowerCase().includes(q))
    : pending
  const failedLaunches = pending.filter((item) => normalize(item.status) === "failed").length
  const attention = data.workspaces.filter(needsAttention).length + failedLaunches
  const dockerDot = DOT_TONE[statusMeta(data.docker.status).tone] ?? DOT_TONE.neutral
  const workspaceDataUnavailable = data.readStatus === "unavailable"

  return (
    <aside
      aria-label="Workspaces"
      className={cn("relative flex h-full min-h-0 shrink-0 flex-col border-r border-border bg-card", className)}
      id="workspace-rail"
      style={{ width: railWidth }}
    >
      {/* Drag the right edge to resize (desktop only; the rail is a drawer on mobile). */}
      <div
        aria-orientation="vertical"
        className="absolute right-0 top-0 z-10 hidden h-full w-1 cursor-col-resize hover:bg-accent lg:block"
        onMouseDown={startResize}
        role="separator"
      />
      <div className="border-b border-border px-4 pb-3 pt-4">
        <div className="flex items-center gap-2">
          <span className="flex h-6 w-6 items-center justify-center rounded bg-primary font-mono text-[11px] font-bold text-primary-foreground">S</span>
          <div className="min-w-0">
            <div className="text-[13px] font-semibold tracking-tight text-foreground">SAG Workbench</div>
            <div className="flex items-center gap-1 font-mono text-[9px] uppercase tracking-[0.14em] text-muted-foreground">
              <span className={cn("inline-flex h-1 w-1 rounded-full", dockerDot)} /> docker {data.docker.version ?? data.docker.status}
            </div>
          </div>
        </div>
        <Tooltip className="mt-3 w-full" label="Queue project setups from a list of repo URLs" side="bottom">
          <button
            className="inline-flex w-full items-center justify-center gap-1.5 rounded-md bg-primary px-3 py-2 text-[12.5px] font-medium text-primary-foreground hover:bg-primary/90"
            onClick={onLaunchSetups}
            type="button"
          >
            <Rocket size={14} /> Launch setups
          </button>
        </Tooltip>
        {!workspaceDataUnavailable ? (
          <>
            <div className="mt-3 flex gap-2">
              <Chip label="Workspaces" value={data.workspaces.length} />
              <Chip label="Containers" value={running} tone="blue" />
              <Chip label="Attention" value={attention} tone={attention ? "red" : undefined} />
            </div>
            <RailSummary workspaces={data.workspaces} />
            <div className="relative mt-3">
              <Search aria-hidden className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground" size={13} />
              <input
                aria-label="Filter workspaces"
                className="w-full rounded-md border border-border bg-muted py-1.5 pl-8 pr-2 text-[12.5px] text-foreground placeholder:text-muted-foreground focus:border-ring focus:bg-card focus:outline-none focus:ring-2 focus:ring-ring/30"
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Filter workspaces…"
                value={query}
              />
            </div>
          </>
        ) : null}
        {!workspaceDataUnavailable && onDeleteMany && data.workspaces.length ? (
          <div className="mt-2 flex justify-end">
            <Tooltip label={selectMode ? "Exit multi-select mode" : "Select multiple workspaces to delete"}>
              <button
                className="font-mono text-[10px] uppercase tracking-[0.1em] text-muted-foreground hover:text-foreground"
                onClick={() => (selectMode ? exitSelectMode() : setSelectMode(true))}
                type="button"
              >
                {selectMode ? "Cancel" : "Select"}
              </button>
            </Tooltip>
          </div>
        ) : null}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {workspaceDataUnavailable ? (
          <div className="m-3 rounded-lg border border-status-attention/40 bg-status-attention-soft px-3 py-4" role="alert">
            <div className="flex items-center gap-2 text-[13px] font-semibold text-foreground">
              <AlertTriangle className="text-status-attention" size={15} />
              Workspace data unavailable
            </div>
            <p className="mt-1.5 text-[12px] leading-relaxed text-muted-foreground">
              The workspace index could not be read. No workspace totals or rows are shown because they may be incomplete.
            </p>
            <button
              className="mt-3 rounded-md border border-border bg-card px-2.5 py-1.5 text-[12px] font-medium text-foreground hover:bg-accent"
              onClick={() => window.location.reload()}
              type="button"
            >
              Retry dashboard
            </button>
          </div>
        ) : pendingRows.length || rows.length ? (
          <>
            {pendingRows.map((item) => (
              <PendingRailRow
                key={`pending-${item.id}`}
                item={item}
                onRemove={onRemoveLaunch ? setRemoveTarget : () => {}}
              />
            ))}
            {rows.map((w) => (
              <RailRow
                key={w.id}
                checked={picked.has(w.id)}
                deleting={deleting.has(w.id)}
                highlighted={highlightedWorkspaces.includes(w.id)}
                onSelect={handleSelect}
                onToggleCheck={togglePicked}
                selectMode={selectMode}
                selected={w.id === selectedId}
                workspace={w}
              />
            ))}
          </>
        ) : data.workspaces.length === 0 && pending.length === 0 ? (
          <div className="flex flex-col items-center px-4 py-12 text-center">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg border border-border bg-muted text-muted-foreground">
              <GitBranch size={18} />
            </div>
            <div className="mt-3 text-[13px] font-medium text-foreground">No workspaces yet</div>
            <p className="mt-1 text-[12px] leading-relaxed text-muted-foreground">
              Launch a setup to add one. Paste a list of repo URLs to queue many at once.
            </p>
          </div>
        ) : (
          <div className="px-4 py-10 text-center text-[12px] text-muted-foreground">No matches</div>
        )}
      </div>

      {selectMode ? (
        <div className="flex items-center gap-2 border-t border-border bg-card px-4 py-2">
          <Tooltip className="flex-1" label="Delete the checked workspaces and their containers">
            <button
              className="w-full rounded-md bg-status-failed px-3 py-1.5 text-[12px] font-medium text-primary-foreground hover:opacity-90 disabled:opacity-40"
              disabled={picked.size === 0}
              onClick={() => setBatchConfirm(true)}
              type="button"
            >
              Delete {picked.size} selected
            </button>
          </Tooltip>
          <button
            className="rounded-md border border-border px-3 py-1.5 text-[12px] text-muted-foreground hover:bg-accent"
            onClick={exitSelectMode}
            type="button"
          >
            Cancel
          </button>
        </div>
      ) : null}

      <div className="flex items-center gap-2 border-t border-border px-4 py-2 font-mono text-[9px] text-muted-foreground">
        <span>{lastUpdatedAt != null ? `Updated ${formatAgo(Date.now() - lastUpdatedAt)}` : "Updating…"}</span>
        {pollFailed ? (
          <span className="inline-flex items-center gap-1 text-status-attention">
            <AlertTriangle size={10} /> couldn't refresh
          </span>
        ) : (
          <span>· refreshes automatically</span>
        )}
      </div>

      {removeTarget && onRemoveLaunch ? (
        <DeleteWorkspaceDialog
          onCancel={() => setRemoveTarget(null)}
          onConfirm={async (id) => {
            await onRemoveLaunch(id)
            setRemoveTarget(null)
          }}
          target={removeTarget}
        />
      ) : null}

      {batchConfirm ? (
        <DeleteWorkspaceDialog
          count={picked.size}
          onCancel={() => setBatchConfirm(false)}
          onConfirm={async () => {
            await onDeleteMany?.([...picked])
            exitSelectMode()
          }}
          target={{ workspaceId: "", label: `${picked.size} workspaces`, kind: "workspace" }}
        />
      ) : null}
    </aside>
  )
}
