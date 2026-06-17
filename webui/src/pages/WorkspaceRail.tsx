import { Activity, AlertTriangle, Check, Clock, GitBranch, Loader2, Rocket, Search, Trash2, X } from "lucide-react"
import { useState } from "react"

import type { DashboardResponse, LaunchQueueItem, LaunchQueueState, WorkspaceSummary } from "@/api/types"
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
import { formatAgo } from "@/lib/relativeTime"
import { cn } from "@/lib/utils"

import { needsAttention, sortByAttentionFirst } from "./dashboardAttention"

function normalize(value: string | null | undefined): string {
  return value?.trim().toLowerCase() ?? ""
}

function buildState(build: WorkspaceSummary["build"]): string {
  return normalize(typeof build === "string" ? build : build.state)
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
  const total = Math.max(workspace.test.total, workspace.test.pass + workspace.test.fail)

  const body = (
    <>
      <span className={cn("relative inline-flex h-1.5 w-1.5 shrink-0 rounded-full", dot)}>
        {!deleting && (dockerNorm === "running" || dockerNorm === "launching") ? (
          <span className={cn("absolute inline-flex h-full w-full animate-ping rounded-full opacity-75", dot)} />
        ) : null}
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5">
          <span className={cn("truncate text-[13px] font-medium", selected && !selectMode ? "text-status-running" : "text-slate-800")}>
            {workspace.project}
          </span>
          {workspace.release ? <span className="shrink-0 font-mono text-[9.5px] text-slate-500">{workspace.release}</span> : null}
          {workspace.activeSession ? <Activity className="shrink-0 text-status-running" size={11} /> : null}
        </span>
        <span className="mt-0.5 block truncate font-mono text-[10px] text-slate-500">
          {[workspace.stack, workspace.commit].filter(Boolean).join(" · ")}
        </span>
      </span>
      <span className="flex shrink-0 items-center gap-2">
        {deleting ? (
          <span className="inline-flex items-center gap-1 font-mono text-[10px] text-slate-500">
            <Loader2 className="animate-spin" size={11} /> deleting…
          </span>
        ) : (
          <>
            {build === "success" ? <Check className="text-status-success" size={13} /> : build === "failure" || build === "failed" ? <X className="text-status-failed" size={13} /> : <Clock className="text-slate-400" size={12} />}
            {normalize(workspace.test.state) !== "none" && total > 0 ? (
              <TestBar fail={workspace.test.fail} pass={workspace.test.pass} total={total} />
            ) : (
              <span className="w-10 text-right font-mono text-[10px] text-slate-400">—</span>
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
          "flex w-full cursor-pointer items-center gap-3 border-b border-slate-100 px-3.5 py-2.5 last:border-b-0",
          checked ? "bg-status-running-soft" : "hover:bg-slate-50/80",
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
        "group flex w-full items-center gap-3 border-b border-slate-100 px-3.5 py-2.5 text-left transition-colors last:border-b-0",
        deleting
          ? "cursor-default opacity-60"
          : selected
            ? "bg-status-running-soft"
            : attention
              ? "bg-status-failed-soft/40 hover:bg-status-failed-soft/60"
              : "hover:bg-slate-50/80",
        highlighted && !selected && !deleting ? "bg-blue-50/60" : "",
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
        "flex w-full items-center gap-3 border-b border-slate-100 px-3.5 py-2.5 text-left last:border-b-0",
        failed ? "bg-status-failed-soft/40" : "bg-slate-50/40",
      )}
    >
      <span className={cn("inline-flex h-1.5 w-1.5 shrink-0 rounded-full", dot)} />
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5">
          <span className="truncate text-[13px] font-medium text-slate-600">{project}</span>
          {item.ref ? <span className="shrink-0 font-mono text-[9.5px] text-slate-500">{item.ref}</span> : null}
        </span>
        <span
          className={cn("mt-0.5 block truncate text-[10px]", failed ? "text-status-failed" : "text-slate-500")}
          title={failed ? item.error ?? undefined : undefined}
        >
          {launchStatusLine(item)}
        </span>
      </span>
      <span className="flex shrink-0 items-center gap-1.5">
        <StatusBadge status={item.status} />
        {failed ? (
          <button
            aria-label={`Remove failed launch ${project}`}
            className="rounded-md p-1 text-slate-400 hover:bg-red-50 hover:text-red-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500/30"
            onClick={() =>
              onRemove({ workspaceId: item.workspace_id, label: project, kind: "launch" })
            }
            type="button"
          >
            <Trash2 size={14} />
          </button>
        ) : null}
      </span>
    </div>
  )
}

function Chip({ label, value, tone }: { label: string; value: number; tone?: "blue" | "red" }) {
  return (
    <div className="flex-1 rounded-lg border border-slate-200 bg-white px-3 py-2">
      <div className={cn("text-[18px] font-semibold tabular-nums", tone === "red" ? "text-status-failed" : tone === "blue" ? "text-status-running" : "text-slate-900")}>
        {value}
      </div>
      <div className="font-mono text-[9px] uppercase tracking-[0.12em] text-slate-500">{label}</div>
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
      className={cn("flex h-full min-h-0 w-[320px] shrink-0 flex-col border-r border-slate-200 bg-white", className)}
      id="workspace-rail"
    >
      <div className="border-b border-slate-200 px-4 pb-3 pt-4">
        <div className="flex items-center gap-2">
          <span className="flex h-6 w-6 items-center justify-center rounded bg-slate-900 font-mono text-[11px] font-bold text-white">S</span>
          <div className="min-w-0">
            <div className="text-[13px] font-semibold tracking-tight text-slate-900">SAG Workbench</div>
            <div className="flex items-center gap-1 font-mono text-[9px] uppercase tracking-[0.14em] text-slate-500">
              <span className={cn("inline-flex h-1 w-1 rounded-full", dockerDot)} /> docker {data.docker.version ?? data.docker.status}
            </div>
          </div>
        </div>
        <button
          className="mt-3 inline-flex w-full items-center justify-center gap-1.5 rounded-md bg-slate-900 px-3 py-2 text-[12.5px] font-medium text-white hover:bg-slate-800"
          onClick={onLaunchSetups}
          type="button"
        >
          <Rocket size={14} /> Launch setups
        </button>
        <div className="mt-3 flex gap-2">
          <Chip label="Workspaces" value={data.workspaces.length} />
          <Chip label="Running" value={running} tone="blue" />
          <Chip label="Attention" value={attention} tone={attention ? "red" : undefined} />
        </div>
        <div className="relative mt-3">
          <Search aria-hidden className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400" size={13} />
          <input
            aria-label="Filter workspaces"
            className="w-full rounded-md border border-slate-200 bg-slate-50/60 py-1.5 pl-8 pr-2 text-[12.5px] text-slate-700 placeholder:text-slate-400 focus:border-blue-300 focus:bg-white focus:outline-none focus:ring-2 focus:ring-blue-500/20"
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter workspaces…"
            value={query}
          />
        </div>
        {onDeleteMany && data.workspaces.length ? (
          <div className="mt-2 flex justify-end">
            <button
              className="font-mono text-[10px] uppercase tracking-[0.1em] text-slate-500 hover:text-slate-700"
              onClick={() => (selectMode ? exitSelectMode() : setSelectMode(true))}
              type="button"
            >
              {selectMode ? "Cancel" : "Select"}
            </button>
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
            <div className="flex h-10 w-10 items-center justify-center rounded-lg border border-slate-200 bg-slate-50 text-slate-500">
              <GitBranch size={18} />
            </div>
            <div className="mt-3 text-[13px] font-medium text-slate-700">No workspaces yet</div>
            <p className="mt-1 text-[12px] leading-relaxed text-slate-500">
              Launch a setup to add one. Paste a list of repo URLs to queue many at once.
            </p>
          </div>
        ) : (
          <div className="px-4 py-10 text-center text-[12px] text-slate-500">No matches</div>
        )}
      </div>

      {selectMode ? (
        <div className="flex items-center gap-2 border-t border-slate-200 bg-white px-4 py-2">
          <button
            className="flex-1 rounded-md bg-status-failed px-3 py-1.5 text-[12px] font-medium text-white hover:opacity-90 disabled:opacity-40"
            disabled={picked.size === 0}
            onClick={() => setBatchConfirm(true)}
            type="button"
          >
            Delete {picked.size} selected
          </button>
          <button
            className="rounded-md border border-slate-200 px-3 py-1.5 text-[12px] text-slate-600 hover:bg-slate-50"
            onClick={exitSelectMode}
            type="button"
          >
            Cancel
          </button>
        </div>
      ) : null}

      <div className="flex items-center gap-2 border-t border-slate-100 px-4 py-2 font-mono text-[9px] text-slate-500">
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
