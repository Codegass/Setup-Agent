import { Activity, AlertTriangle, Check, CircleCheck, Clock, Gauge, GitBranch, Hammer, Loader2, Rocket, Search, Trash2, X } from "lucide-react"
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
import { readStored, writeStored } from "@/lib/safeStorage"
import { cn } from "@/lib/utils"

import { needsAttention, sortByAttentionFirst } from "./dashboardAttention"

function normalize(value: string | null | undefined): string {
  return value?.trim().toLowerCase() ?? ""
}

function buildState(build: WorkspaceSummary["build"]): string {
  return normalize(typeof build === "string" ? build : build.state)
}

function evidenceObservationLabel(test: WorkspaceSummary["test"]): string | null {
  const layers = test.evidenceLayers?.tests
  if (!layers) return null
  const labels: string[] = []
  for (const [name, observations] of [
    ["quarantined", layers.quarantinedObservations],
    ["unattributed", layers.unattributedObservations],
    ["stale", layers.staleObservations],
  ] as const) {
    if (typeof observations.executed === "number" && observations.executed > 0) {
      labels.push(`${observations.executed.toLocaleString()} ${name} observations`)
    } else if (typeof observations.reportFileCount === "number" && observations.reportFileCount > 0) {
      labels.push(`${observations.reportFileCount.toLocaleString()} ${name} report files`)
    }
  }
  return labels.length ? `${labels.join("; ")} (not verdict-bearing)` : null
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
  const build = buildState(workspace.build)
  const attention = needsAttention(workspace)
  const subjectCounts = workspace.test.evidenceLayers?.tests.claimed.latestSubjects
  const subjectValues = subjectCounts
    ? [subjectCounts.executed, subjectCounts.passed, subjectCounts.failed, subjectCounts.errors, subjectCounts.skipped]
    : []
  const subjectsAvailable = !!subjectCounts
    && subjectCounts.availability !== "unavailable"
    && subjectValues.every((value) => typeof value === "number" && Number.isFinite(value))
  const claimedTotal = subjectsAvailable ? subjectCounts.executed as number : 0
  const claimedPassed = subjectsAvailable ? subjectCounts.passed as number : 0
  const claimedFailed = subjectsAvailable ? (subjectCounts.failed as number) + (subjectCounts.errors as number) : 0
  const legacyTotal = Math.max(workspace.test.total, workspace.test.pass + workspace.test.fail)
  const observationLabel = evidenceObservationLabel(workspace.test)

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
        <span className="mt-0.5 block truncate font-mono text-[10px] text-muted-foreground">
          {[workspace.stack, workspace.commit].filter(Boolean).join(" · ")}
        </span>
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
                  : build === "failure" || build === "failed"
                    ? "Build failed"
                    : "No build result yet"
              }
            >
              {build === "success" ? <Check className="text-status-success" size={13} /> : build === "failure" || build === "failed" ? <X className="text-status-failed" size={13} /> : <Clock className="text-muted-foreground" size={12} />}
            </Tooltip>
            {subjectCounts ? (
              subjectsAvailable ? (
                claimedTotal > 0 ? (
                  <Tooltip
                    label={[
                      `Claimed latest subjects: ${claimedPassed} passed, ${claimedFailed} failed of ${claimedTotal}`,
                      observationLabel,
                    ].filter(Boolean).join(". ")}
                  >
                    <TestBar fail={claimedFailed} pass={claimedPassed} total={claimedTotal} />
                  </Tooltip>
                ) : (
                  <Tooltip label={["Claimed latest subjects: 0 executed", observationLabel].filter(Boolean).join("; ")}>
                    <span
                      aria-label={["Claimed latest subjects: 0 executed", observationLabel].filter(Boolean).join("; ")}
                      className="w-10 text-right font-mono text-[10px] text-muted-foreground"
                    >
                      0
                    </span>
                  </Tooltip>
                )
              ) : (
                <Tooltip
                  label={[
                    "Claimed latest subjects unavailable",
                    observationLabel,
                  ].filter(Boolean).join("; ")}
                >
                  <span
                    aria-label={[
                      "Claimed latest subjects unavailable",
                      observationLabel,
                    ].filter(Boolean).join("; ")}
                    className="w-10 text-right font-mono text-[10px] text-muted-foreground"
                  >
                    —
                  </span>
                </Tooltip>
              )
            ) : normalize(workspace.test.state) !== "none" && legacyTotal > 0 ? (
              <Tooltip label={`Tests: ${workspace.test.pass} passed, ${workspace.test.fail} failed of ${legacyTotal}`}>
                <TestBar fail={workspace.test.fail} pass={workspace.test.pass} total={legacyTotal} />
              </Tooltip>
            ) : (
              <Tooltip label="No tests run yet">
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
  return den > 0 ? (100 * num) / den : null
}

function compact(n: number): string {
  return n >= 10000 ? `${(n / 1000).toFixed(1)}k` : n.toLocaleString()
}

function StatCard({
  icon: Icon,
  label,
  rate,
  detail,
  hint,
  value,
}: {
  icon: typeof Hammer
  label: string
  rate: number | null
  detail: string
  hint: string
  value?: string
}) {
  const tone = rate == null
    ? "text-muted-foreground"
    : rate >= 80 ? "text-status-success" : "text-status-attention"
  return (
    <Tooltip className="flex-1" label={hint} side="bottom">
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
  const build = pct(r.buildSuccess, r.buildKnown)
  const hasEvidenceLayers = r.claimedSubjectWorkspaces > 0
  const subjects = r.claimedSubjectUnavailable === 0
    ? pct(r.claimedSubjectPassed, r.claimedSubjectExecutedNonSkip)
    : null
  const legacyPass = pct(r.passed, r.executedNonSkip)
  const legacyExec = pct(r.executed, r.declared)
  const subjectDetail = r.claimedSubjectUnavailable > 0
    ? "Unavailable"
    : subjects === null ? "No executions" : `${compact(r.claimedSubjectPassed)}/${compact(r.claimedSubjectExecutedNonSkip)}`
  const subjectHint = r.claimedSubjectUnavailable > 0
    ? `${r.claimedSubjectUnavailable} workspaces lack module-qualified latest-subject counts`
    : subjects === null
      ? "No non-skipped module-qualified latest subjects were executed"
      : `${r.claimedSubjectPassed.toLocaleString()} passed of ${r.claimedSubjectExecutedNonSkip.toLocaleString()} claimed latest subjects`
  if (build === null && !hasEvidenceLayers && legacyPass === null && legacyExec === null) return null

  return (
    <div className="mt-2 flex gap-2">
      {build !== null ? (
        <StatCard
          detail={`${r.buildSuccess}/${r.buildKnown}`}
          hint={`${r.buildSuccess} of ${r.buildKnown} workspaces built successfully`}
          icon={Hammer}
          label="Build"
          rate={build}
        />
      ) : null}
      {hasEvidenceLayers ? (
        <StatCard
          detail={subjectDetail}
          hint={subjectHint}
          icon={CircleCheck}
          label="Subjects"
          rate={subjects}
        />
      ) : legacyPass !== null ? (
        <StatCard
          detail={`${compact(r.passed)}/${compact(r.executedNonSkip)}`}
          hint={`${r.passed.toLocaleString()} passed of ${r.executedNonSkip.toLocaleString()} executed (skips excluded)`}
          icon={CircleCheck}
          label="Pass"
          rate={legacyPass}
        />
      ) : null}
      {hasEvidenceLayers && (r.nonVerdictObservations > 0 || r.nonVerdictReportFiles > 0) ? (
        <StatCard
          detail="not verdict-bearing"
          hint={`${r.nonVerdictObservations.toLocaleString()} report observations across ${r.nonVerdictReportFiles.toLocaleString()} files; not verdict-bearing`}
          icon={Gauge}
          label="Diagnostics"
          rate={null}
          value={r.nonVerdictObservations > 0 ? compact(r.nonVerdictObservations) : `${r.nonVerdictReportFiles} files`}
        />
      ) : null}
      {!hasEvidenceLayers && legacyExec !== null ? (
        <StatCard
          detail={`${compact(r.executed)}/${compact(r.declared)}`}
          hint={`${r.executed.toLocaleString()} executed of ${r.declared.toLocaleString()} declared test methods`}
          icon={Gauge}
          label="Exec"
          rate={legacyExec}
        />
      ) : null}
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
        <div className="mt-3 flex gap-2">
          <Chip label="Workspaces" value={data.workspaces.length} />
          <Chip label="Running" value={running} tone="blue" />
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
        {onDeleteMany && data.workspaces.length ? (
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
        {pendingRows.length || rows.length ? (
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
