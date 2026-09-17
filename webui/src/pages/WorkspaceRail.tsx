import { Activity, AlertTriangle, GitBranch, Loader2, Rocket, Search, Trash2 } from "lucide-react"
import { type MouseEvent as ReactMouseEvent, useState } from "react"

import type { DashboardResponse, LaunchQueueItem, LaunchQueueState, WorkspaceResult, WorkspaceSummary } from "@/api/types"
import { StatusBadge } from "@/components/common/Badge"
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

function visibleMetadata(value: string | null | undefined): string | null {
  const normalized = normalize(value)
  return normalized && !["unknown", "none", "unavailable", "n/a", "-", "—"].includes(normalized)
    ? value!.trim()
    : null
}

const VERDICT_TONE: Record<string, string> = {
  success: "text-status-success",
  partial: "text-status-attention",
  failed: "text-status-failed",
}

/** How many tests came back red, when the run counted any. */
function redCount(tests: NonNullable<WorkspaceResult["tests"]>): number {
  return tests.failed + tests.errors
}

/**
 * One row's whole result: the card's word for the run, how far the required
 * task got, how the tests came out, and the Official CI word.
 *
 * Every cell is copied from `workspace.result`, which the API reads off the
 * run's own card and record. A cell the run did not measure is a dash — there
 * is no zero to fall back on — and a run with no result at all says that once
 * rather than four times.
 */
function ResultStrip({ result }: { result: WorkspaceResult | null | undefined }) {
  if (!result) {
    return (
      <span className="shrink-0 font-mono text-[11px] text-muted-foreground">no result</span>
    )
  }
  const tests = result.tests
  const red = tests ? redCount(tests) : 0
  return (
    <span className="flex shrink-0 items-center gap-2 font-mono text-[11px] tabular-nums">
      <span className={cn(VERDICT_TONE[result.verdict] ?? "text-muted-foreground")} title="How the run came out">
        {result.verdict}
      </span>
      <span className="text-muted-foreground" title="Required task steps completed">
        {result.task ? `${result.task.completed}/${result.task.required}` : "—"}
      </span>
      <span className="text-muted-foreground" title="Tests passed of tests executed">
        {tests ? `${tests.passed.toLocaleString()}/${tests.executed.toLocaleString()}` : "—"}
      </span>
      {red > 0 ? (
        <span className="text-status-failed" title="Tests that failed or errored">
          {`+${red.toLocaleString()}`}
        </span>
      ) : null}
      <span className="text-muted-foreground" title="How this run measures against the project's own CI">
        {result.ci ? result.ci.status : "—"}
      </span>
    </span>
  )
}

/** The same four results, in words, for the row's accessible name — the cells
 *  themselves are inside a button whose aria-label would otherwise hide them. */
function resultClause(result: WorkspaceResult | null | undefined): string {
  if (!result) return " — no result recorded"
  const tests = result.tests
  const red = tests ? redCount(tests) : 0
  const parts = [
    `setup ${result.verdict}`,
    result.task
      ? `required task ${result.task.completed}/${result.task.required}`
      : "required task not recorded",
    tests
      ? `tests ${tests.passed.toLocaleString()}/${tests.executed.toLocaleString()}${
          red > 0 ? `, ${red.toLocaleString()} failed or errored` : ""
        }`
      : "tests not recorded",
    result.ci ? `official CI ${result.ci.status}` : "official CI not recorded",
  ]
  return ` — ${parts.join(", ")}`
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
  const attention = needsAttention(workspace)
  const stack = visibleMetadata(workspace.stack)
  const commit = visibleMetadata(workspace.commit)

  const body = (
    <>
      <span className={cn("relative inline-flex h-1.5 w-1.5 shrink-0 rounded-full", dot)}>
        {!deleting && (dockerNorm === "running" || dockerNorm === "launching") ? (
          <span className={cn("absolute inline-flex h-full w-full animate-ping rounded-full opacity-75", dot)} />
        ) : null}
      </span>
      {/* Two lines, not two columns: the four results are as wide as a sentence
          and sharing one line with the name truncated every project in the rail
          down to a letter or two. The name keeps the full width; the results sit
          under it, beside whatever stack and commit the workspace states. */}
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5">
          <span className={cn("truncate text-[13px] font-medium", selected && !selectMode ? "text-status-running" : "text-foreground")}>
            {workspace.project}
          </span>
          {workspace.release ? <span className="shrink-0 font-mono text-[9.5px] text-muted-foreground">{workspace.release}</span> : null}
          {workspace.activeSession ? <Activity className="shrink-0 text-status-running" size={11} /> : null}
        </span>
        <span className="mt-0.5 flex items-baseline gap-2">
          <span className="min-w-0 flex-1 truncate font-mono text-[10px] text-muted-foreground">
            {[stack, commit].filter(Boolean).join(" · ")}
          </span>
          {deleting ? (
            <span className="inline-flex shrink-0 items-center gap-1 font-mono text-[11px] text-muted-foreground">
              <Loader2 className="animate-spin" size={11} /> deleting…
            </span>
          ) : (
            <ResultStrip result={workspace.result} />
          )}
        </span>
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
      aria-label={`Open workspace ${workspace.project}${resultClause(workspace.result)}`}
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
  const [attentionOnly, setAttentionOnly] = useState(false)
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
  const total = data.workspaces.length
  const running = data.workspaces.filter((w) => normalize(w.docker.status) === "running").length
  const pending = pendingLaunchItems(launchQueue, data.workspaces)
  const failedLaunches = pending.filter((item) => normalize(item.status) === "failed").length
  const attention = data.workspaces.filter(needsAttention).length + failedLaunches
  // The toggle narrows both lists, because the count it shows spans both: a
  // failed launch is one of the things it counts, so leaving pending rows in
  // would make the button claim a number it does not show.
  const filterActive = attentionOnly && attention > 0
  const visible = filterActive ? ordered.filter(needsAttention) : ordered
  const visiblePending = filterActive
    ? pending.filter((item) => normalize(item.status) === "failed")
    : pending
  const rows = q
    ? visible.filter((w) => w.project.toLowerCase().includes(q) || (w.stack ?? "").toLowerCase().includes(q))
    : visible
  // Pending rows respect the workspace filter so the search box also narrows them.
  const pendingRows = q
    ? visiblePending.filter((item) => launchProjectName(item).toLowerCase().includes(q))
    : visiblePending
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
            <div className="mt-3 flex flex-wrap items-center gap-2 text-[12px] text-muted-foreground">
              <span>{`${total} workspace${total === 1 ? "" : "s"}`}</span>
              {running > 0 ? <span>{`· ${running} running`}</span> : null}
              {attention > 0 ? (
                <>
                  <span aria-hidden>·</span>
                  <button
                    aria-pressed={attentionOnly}
                    className={cn(
                      "rounded-full px-2 py-0.5 text-[11px] font-semibold",
                      attentionOnly
                        ? "bg-status-attention text-background"
                        : "bg-status-attention-soft text-status-attention",
                    )}
                    onClick={() => setAttentionOnly((value) => !value)}
                    type="button"
                  >
                    {`${attention} needs attention`}
                  </button>
                </>
              ) : null}
            </div>
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
