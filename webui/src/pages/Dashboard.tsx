import { useEffect, useState } from "react"
import type { KeyboardEvent, ReactNode } from "react"
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Check,
  Clock,
  FileText,
  GitBranch,
  RefreshCw,
  Rocket,
  Trash2,
  X,
} from "lucide-react"

import type {
  DashboardResponse,
  LaunchQueueItem,
  LaunchQueueState,
  WorkspaceSummary,
} from "@/api/types"
import { Badge, StatusBadge } from "@/components/common/Badge"
import { Button } from "@/components/common/Button"
import { Card } from "@/components/common/Card"
import { TestBar } from "@/components/common/TestBar"
import {
  DeleteWorkspaceDialog,
  type DeleteWorkspaceTarget,
} from "@/components/workspace/DeleteWorkspaceDialog"
import { formatAgo } from "@/lib/relativeTime"
import { cn } from "@/lib/utils"

import { needsAttention, sortByAttentionFirst } from "./dashboardAttention"

interface DashboardProps {
  data: DashboardResponse
  onOpenWorkspace: (workspaceId: string) => void
  onOpenSession: (workspaceId: string, sessionId: string, tab?: string) => void
  onRefresh?: () => void
  refreshing?: boolean
  onLaunchSetups?: () => void
  onDeleteWorkspace?: (workspaceId: string) => Promise<void>
  launchQueue?: LaunchQueueState | null
  highlightedWorkspaces?: string[]
  lastUpdatedAt?: number | null
  pollFailed?: boolean
  pollError?: string | null
}

interface BuildDetails {
  state: string
  tool?: string
  time?: string
}

const tableHeaders = [
  "Project",
  "Container",
  "Current task",
  "Build",
  "Test",
  "Report",
  "Changed",
  "",
]

const tableColumns =
  "grid-cols-[2fr_1fr_1.3fr_0.95fr_1fr_0.65fr_0.55fr_76px]"

function normalize(status: string | null | undefined): string {
  return status?.trim().toLowerCase() ?? ""
}

function buildDetails(build: WorkspaceSummary["build"]): BuildDetails {
  if (typeof build === "string") {
    return { state: build }
  }

  return {
    state: build.state,
    tool: build.tool,
    time: build.time,
  }
}

function buildMeta(build: WorkspaceSummary["build"]): string | null {
  const details = buildDetails(build)
  const parts = [details.tool, details.time].filter(Boolean)

  return parts.length ? parts.join(" · ") : null
}

function launchProjectName(item: LaunchQueueItem): string {
  return item.workspace_id.replace(/^sag-/, "")
}

function launchStatusLine(item: LaunchQueueItem): string {
  switch (normalize(item.status)) {
    case "queued":
      return "Waiting for a free setup slot"
    case "launching":
    case "running":
      return "Setting up…"
    case "failed":
      return item.error || "Setup failed"
    default:
      return item.error || "Setup pending"
  }
}

function pendingLaunchItems(
  launchQueue: LaunchQueueState | null,
  workspaces: WorkspaceSummary[],
): LaunchQueueItem[] {
  if (!launchQueue) {
    return []
  }
  const discovered = new Set(workspaces.map((workspace) => workspace.id))
  const seen = new Set<string>()
  const pending: LaunchQueueItem[] = []
  for (const batch of launchQueue.batches) {
    for (const item of batch.items) {
      const state = normalize(item.status)
      if (state === "completed" || discovered.has(item.workspace_id)) {
        continue
      }
      if (seen.has(item.workspace_id)) {
        continue
      }
      seen.add(item.workspace_id)
      pending.push(item)
    }
  }
  // Attention-first: failed launches sort above active, active above queued.
  const rank: Record<string, number> = { failed: 0, running: 1, launching: 2, queued: 3 }
  return pending.sort(
    (a, b) => (rank[normalize(a.status)] ?? 4) - (rank[normalize(b.status)] ?? 4),
  )
}

function workspaceMeta(workspace: WorkspaceSummary): string {
  // Drop "unknown"/"Unknown" placeholders so the line carries only real signal.
  return [workspace.stack, workspace.commit, workspace.updated]
    .filter(
      (value): value is string =>
        typeof value === "string" && value.length > 0 && value.toLowerCase() !== "unknown",
    )
    .join(" · ")
}

function reportIsReady(workspace: WorkspaceSummary): boolean {
  return normalize(workspace.report) === "ready"
}

export function Dashboard({
  data,
  onOpenWorkspace,
  onOpenSession,
  onRefresh,
  refreshing = false,
  onLaunchSetups,
  onDeleteWorkspace,
  launchQueue = null,
  highlightedWorkspaces = [],
  lastUpdatedAt = null,
  pollFailed = false,
  pollError = null,
}: DashboardProps) {
  const [deleteTarget, setDeleteTarget] = useState<DeleteWorkspaceTarget | null>(null)
  const [attentionOnly, setAttentionOnly] = useState(false)
  const workspaces = data.workspaces
  const orderedWorkspaces = sortByAttentionFirst(workspaces)
  const running = workspaces.filter((w) => normalize(w.docker.status) === "running").length
  const pendingLaunches = pendingLaunchItems(launchQueue, workspaces)
  const failedLaunches = pendingLaunches.filter(
    (item) => normalize(item.status) === "failed",
  ).length
  const attention = workspaces.filter(needsAttention).length + failedLaunches
  const filterActive = attentionOnly && attention > 0
  const visibleWorkspaces = filterActive
    ? orderedWorkspaces.filter(needsAttention)
    : orderedWorkspaces
  const visiblePending = filterActive
    ? pendingLaunches.filter((item) => normalize(item.status) === "failed")
    : pendingLaunches

  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  return (
    <div className="mx-auto max-w-[1180px] px-4 py-5 sm:px-6 lg:px-8 lg:py-7">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0">
          <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-slate-500">
            sag ui · local workbench
          </div>
          <h1 className="mt-1.5 text-[22px] font-semibold tracking-tight text-slate-900">
            Workspaces
          </h1>
          <p className="mt-1 text-[13px] text-slate-500">
            SAG-managed containers and their latest setup state.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {onLaunchSetups ? (
            <Button onClick={onLaunchSetups} type="button">
              <Rocket size={14} />
              Launch setups
            </Button>
          ) : null}
          {onRefresh ? (
            <Button
              aria-label="Refresh dashboard"
              disabled={refreshing}
              onClick={onRefresh}
              type="button"
              variant="outline"
            >
              <RefreshCw className={refreshing ? "animate-spin" : undefined} size={14} />
              Refresh
            </Button>
          ) : null}
        </div>
      </div>

      <div className="mt-5 grid gap-3 sm:grid-cols-3">
        <SummaryCard label="Workspaces" value={workspaces.length} sub="managed by SAG" />
        <SummaryCard
          icon={<Activity size={14} className="text-blue-500" />}
          label="Running"
          value={running}
          sub="active containers"
        />
        <SummaryCard
          active={filterActive}
          icon={attention ? <AlertTriangle size={14} className="text-status-failed" /> : null}
          interactive={attention > 0}
          label="Need attention"
          onClick={attention > 0 ? () => setAttentionOnly((value) => !value) : undefined}
          sub="failed, partial, or stopped"
          value={attention}
          valueTone={attention > 0 ? "text-status-failed" : undefined}
        />
      </div>

      {workspaces.length === 0 && pendingLaunches.length === 0 ? (
        <EmptyState onLaunchSetups={onLaunchSetups} />
      ) : (
        <>
          <Card className="mt-5 hidden overflow-hidden lg:block">
            <div
              className={`grid ${tableColumns} items-center gap-3 border-b border-slate-100 bg-slate-50/60 px-4 py-2.5`}
            >
              {tableHeaders.map((header) => (
                <div
                  key={header}
                  className="font-mono text-[10px] uppercase tracking-[0.12em] text-slate-500"
                >
                  {header}
                </div>
              ))}
            </div>
            {visiblePending.map((item) => (
              <PendingLaunchRow
                key={`pending-${item.id}`}
                item={item}
                onDelete={setDeleteTarget}
              />
            ))}
            {visibleWorkspaces.map((workspace) => (
              <WorkspaceRow
                key={workspace.id}
                attention={needsAttention(workspace)}
                highlighted={highlightedWorkspaces.includes(workspace.id)}
                onDelete={setDeleteTarget}
                onOpenSession={onOpenSession}
                onOpenWorkspace={onOpenWorkspace}
                workspace={workspace}
              />
            ))}
          </Card>

          <div className="mt-5 grid gap-3 lg:hidden">
            {visiblePending.map((item) => (
              <PendingLaunchCard
                key={`pending-${item.id}`}
                item={item}
                onDelete={setDeleteTarget}
              />
            ))}
            {visibleWorkspaces.map((workspace) => (
              <WorkspaceCard
                key={workspace.id}
                attention={needsAttention(workspace)}
                highlighted={highlightedWorkspaces.includes(workspace.id)}
                onDelete={setDeleteTarget}
                onOpenSession={onOpenSession}
                onOpenWorkspace={onOpenWorkspace}
                workspace={workspace}
              />
            ))}
          </div>
        </>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-x-2 gap-y-1 px-1 font-mono text-[10px] text-slate-500">
        <span>
          {lastUpdatedAt != null ? `Updated ${formatAgo(now - lastUpdatedAt)}` : "Updating…"}
        </span>
        {pollFailed ? (
          <span
            className="inline-flex items-center gap-1 text-status-attention"
            title={pollError ?? undefined}
          >
            <span aria-hidden="true" className="inline-block h-1.5 w-1.5 rounded-full bg-status-attention" />
            couldn't refresh
          </span>
        ) : null}
        {filterActive ? (
          <button
            className="underline decoration-dotted hover:text-slate-700"
            onClick={() => setAttentionOnly(false)}
            type="button"
          >
            · Showing {visibleWorkspaces.length} needing attention · Show all
          </button>
        ) : (
          <span>· refreshes automatically</span>
        )}
      </div>

      {deleteTarget ? (
        <DeleteWorkspaceDialog
          onCancel={() => setDeleteTarget(null)}
          onConfirm={async (id) => {
            await onDeleteWorkspace?.(id)
            setDeleteTarget(null)
          }}
          target={deleteTarget}
        />
      ) : null}
    </div>
  )
}

function EmptyState({ onLaunchSetups }: { onLaunchSetups?: () => void }) {
  return (
    <Card className="mt-5 flex flex-col items-center px-6 py-14 text-center">
      <div className="flex h-11 w-11 items-center justify-center rounded-lg border border-slate-200 bg-slate-50 text-slate-500">
        <GitBranch size={20} />
      </div>
      <h2 className="mt-4 text-[15px] font-semibold text-slate-800">No workspaces yet</h2>
      <p className="mt-1.5 max-w-[440px] text-[13px] leading-relaxed text-slate-500">
        A workspace is a SAG-managed container set up from a repository — its build,
        tests, evidence, and report in one place. Launch one to get started.
      </p>
      {onLaunchSetups ? (
        <Button className="mt-5" onClick={onLaunchSetups} type="button">
          <Rocket size={14} />
          Launch your first setup
        </Button>
      ) : null}
      <p className="mt-4 font-mono text-[11px] text-slate-500">
        Tip: paste a list of repo URLs to queue many setups at once.
      </p>
    </Card>
  )
}

function SummaryCard({
  label,
  value,
  sub,
  icon,
  onClick,
  active = false,
  interactive = false,
  valueTone,
}: {
  label: string
  value: number
  sub: string
  icon?: ReactNode
  onClick?: () => void
  active?: boolean
  interactive?: boolean
  valueTone?: string
}) {
  const clickable = interactive && Boolean(onClick)
  return (
    <Card
      aria-label={clickable ? `Filter: ${label}` : undefined}
      aria-pressed={clickable ? active : undefined}
      className={cn(
        "px-4 py-3.5",
        clickable &&
          "cursor-pointer transition-colors hover:bg-slate-50/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-status-failed-border",
        active && "bg-status-failed-soft/50 ring-1 ring-status-failed-border",
      )}
      onClick={clickable ? onClick : undefined}
      onKeyDown={
        clickable
          ? (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault()
                onClick?.()
              }
            }
          : undefined
      }
      role={clickable ? "button" : undefined}
      tabIndex={clickable ? 0 : undefined}
    >
      <div className="flex items-center justify-between gap-3">
        <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-slate-500">
          {label}
        </div>
        {icon}
      </div>
      <div className="mt-1.5 flex items-baseline gap-2">
        <span className={cn("text-[26px] font-semibold tabular-nums text-slate-900", valueTone)}>
          {value}
        </span>
        <span className="min-w-0 text-[12px] text-slate-500">{sub}</span>
      </div>
    </Card>
  )
}

function WorkspaceRow({
  workspace,
  onOpenWorkspace,
  onOpenSession,
  onDelete,
  highlighted = false,
  attention = false,
}: {
  workspace: WorkspaceSummary
  onOpenWorkspace: (workspaceId: string) => void
  onOpenSession: (workspaceId: string, sessionId: string, tab?: string) => void
  onDelete: (target: DeleteWorkspaceTarget) => void
  highlighted?: boolean
  attention?: boolean
}) {
  const openWorkspace = () => onOpenWorkspace(workspace.id)
  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget) {
      return
    }

    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault()
      openWorkspace()
    }
  }

  return (
    <div
      aria-label={`Open workspace ${workspace.project}`}
      className={cn(
        "group grid",
        tableColumns,
        "cursor-pointer items-center gap-3 border-b border-slate-100 px-4 py-3 text-left transition-colors duration-700 last:border-b-0 hover:bg-slate-50/70 focus-visible:bg-slate-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/30",
        attention && "bg-status-failed-soft/50",
        highlighted && "bg-blue-50/60",
      )}
      onClick={openWorkspace}
      onKeyDown={handleKeyDown}
      role="button"
      tabIndex={0}
    >
      <ProjectCell workspace={workspace} />
      <ContainerCell workspace={workspace} />
      <TaskCell workspace={workspace} />
      <BuildCell build={workspace.build} />
      <TestCell workspace={workspace} />
      <ReportCell workspace={workspace} />
      <ChangedCell changed={workspace.changed} />
      <RowActions
        onDelete={onDelete}
        onOpenSession={onOpenSession}
        onOpenWorkspace={onOpenWorkspace}
        workspace={workspace}
      />
    </div>
  )
}

function WorkspaceCard({
  workspace,
  onOpenWorkspace,
  onOpenSession,
  onDelete,
  highlighted = false,
  attention = false,
}: {
  workspace: WorkspaceSummary
  onOpenWorkspace: (workspaceId: string) => void
  onOpenSession: (workspaceId: string, sessionId: string, tab?: string) => void
  onDelete: (target: DeleteWorkspaceTarget) => void
  highlighted?: boolean
  attention?: boolean
}) {
  return (
    <Card
      aria-label={`Open workspace ${workspace.project}`}
      className={cn(
        "cursor-pointer p-4 transition-colors duration-700 hover:bg-slate-50/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/30",
        attention && "border-status-failed-border bg-status-failed-soft/50",
        highlighted && "border-blue-200 bg-blue-50/60",
      )}
      onClick={() => onOpenWorkspace(workspace.id)}
      onKeyDown={(event) => {
        if (event.target !== event.currentTarget) {
          return
        }

        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault()
          onOpenWorkspace(workspace.id)
        }
      }}
      role="button"
      tabIndex={0}
    >
      <div className="flex items-start justify-between gap-3">
        <ProjectCell workspace={workspace} />
        <StatusBadge status={workspace.docker.status} />
      </div>
      <div className="mt-3 text-[12.5px] text-slate-600">{workspace.task}</div>
      {workspace.activeSession ? (
        <div className="mt-1 flex items-center gap-1 font-mono text-[10px] text-blue-500">
          <Activity size={11} />
          {workspace.activeSession} · active
        </div>
      ) : null}
      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <MobileField label="Container">
          <span className="font-mono text-[11px] text-slate-600">{workspace.container}</span>
        </MobileField>
        <MobileField label="Build">
          <BuildCell build={workspace.build} />
        </MobileField>
        <MobileField label="Test">
          <TestCell workspace={workspace} />
        </MobileField>
        <MobileField label="Changed">
          <ChangedCell changed={workspace.changed} />
        </MobileField>
      </div>
      <div className="mt-4 flex items-center justify-between border-t border-slate-100 pt-3">
        <ReportCell workspace={workspace} />
        <RowActions
          alwaysVisible
          onDelete={onDelete}
          onOpenSession={onOpenSession}
          onOpenWorkspace={onOpenWorkspace}
          workspace={workspace}
        />
      </div>
    </Card>
  )
}

function launchDeleteTarget(item: LaunchQueueItem, project: string): DeleteWorkspaceTarget {
  return { workspaceId: item.workspace_id, label: project, kind: "launch" }
}

function RemoveLaunchButton({
  item,
  project,
  onDelete,
}: {
  item: LaunchQueueItem
  project: string
  onDelete: (target: DeleteWorkspaceTarget) => void
}) {
  return (
    <button
      aria-label={`Remove failed launch ${project}`}
      className="shrink-0 rounded-md p-1.5 text-slate-400 hover:bg-red-50 hover:text-red-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500/30"
      onClick={(event) => {
        event.stopPropagation()
        onDelete(launchDeleteTarget(item, project))
      }}
      onKeyDown={(event) => event.stopPropagation()}
      type="button"
    >
      <Trash2 size={15} />
    </button>
  )
}

function PendingLaunchRow({
  item,
  onDelete,
}: {
  item: LaunchQueueItem
  onDelete: (target: DeleteWorkspaceTarget) => void
}) {
  const project = launchProjectName(item)
  const failed = normalize(item.status) === "failed"

  return (
    <div
      aria-label={`Pending launch ${project}`}
      className={`grid ${tableColumns} animate-in fade-in-0 items-center gap-3 border-b border-slate-100 px-4 py-3 text-left duration-200 last:border-b-0 ${
        failed ? "bg-red-50/40" : "bg-slate-50/40"
      }`}
    >
      <div className="flex min-w-0 items-center gap-2.5">
        <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-dashed border-slate-300 bg-white text-slate-400">
          <GitBranch size={14} />
        </div>
        <div className="min-w-0">
          <div className="truncate text-[13px] font-medium text-slate-600">{project}</div>
          {item.ref ? (
            <div className="mt-0.5 truncate font-mono text-[10px] text-slate-500">
              {item.ref}
            </div>
          ) : null}
        </div>
      </div>
      <div className="col-span-7 flex min-w-0 items-center gap-2.5">
        <StatusBadge status={item.status} />
        <span
          className={`min-w-0 truncate text-[12.5px] ${failed ? "text-red-600" : "text-slate-500"}`}
          title={failed ? item.error ?? undefined : undefined}
        >
          {launchStatusLine(item)}
        </span>
        {failed ? (
          <div className="ml-auto">
            <RemoveLaunchButton item={item} onDelete={onDelete} project={project} />
          </div>
        ) : null}
      </div>
    </div>
  )
}

function PendingLaunchCard({
  item,
  onDelete,
}: {
  item: LaunchQueueItem
  onDelete: (target: DeleteWorkspaceTarget) => void
}) {
  const project = launchProjectName(item)
  const failed = normalize(item.status) === "failed"

  return (
    <Card
      aria-label={`Pending launch ${project}`}
      className={`animate-in fade-in-0 p-4 duration-200 ${failed ? "border-red-100 bg-red-50/40" : "bg-slate-50/40"}`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-dashed border-slate-300 bg-white text-slate-400">
            <GitBranch size={14} />
          </div>
          <div className="min-w-0">
            <div className="truncate text-[13px] font-medium text-slate-600">{project}</div>
            {item.ref ? (
              <div className="mt-0.5 truncate font-mono text-[10px] text-slate-500">
                {item.ref}
              </div>
            ) : null}
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          <StatusBadge status={item.status} />
          {failed ? (
            <RemoveLaunchButton item={item} onDelete={onDelete} project={project} />
          ) : null}
        </div>
      </div>
      <div
        className={`mt-3 text-[12.5px] ${failed ? "text-red-600" : "text-slate-500"}`}
        title={failed ? item.error ?? undefined : undefined}
      >
        {launchStatusLine(item)}
      </div>
    </Card>
  )
}

function ProjectCell({ workspace }: { workspace: WorkspaceSummary }) {
  return (
    <div className="flex min-w-0 items-center gap-2.5">
      <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-slate-200 bg-slate-50 text-slate-500">
        <GitBranch size={14} />
      </div>
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-1.5">
          <span className="truncate text-[13px] font-medium text-slate-800 group-hover:text-blue-600">
            {workspace.project}
          </span>
          {workspace.release ? (
            <Badge
              className="shrink-0 rounded border-slate-200 bg-slate-50 px-1.5 py-px text-[9.5px] text-slate-500"
              mono
              title={workspace.tag ? `tag ${workspace.tag}` : undefined}
            >
              {workspace.release}
            </Badge>
          ) : null}
        </div>
        <div className="mt-0.5 truncate font-mono text-[10px] text-slate-500">
          {workspaceMeta(workspace)}
        </div>
      </div>
    </div>
  )
}

function ContainerCell({ workspace }: { workspace: WorkspaceSummary }) {
  return (
    <div className="min-w-0">
      <div className="truncate font-mono text-[11px] text-slate-600">{workspace.container}</div>
      <div className="mt-1">
        <StatusBadge status={workspace.docker.status} />
      </div>
    </div>
  )
}

function TaskCell({ workspace }: { workspace: WorkspaceSummary }) {
  return (
    <div className="min-w-0">
      <div className="truncate text-[12.5px] text-slate-600">{workspace.task}</div>
      {workspace.activeSession ? (
        <div
          className="mt-0.5 flex items-center gap-1 font-mono text-[10px] text-blue-500"
          title="Active execution session"
        >
          <Activity size={11} />
          {workspace.activeSession} · active
        </div>
      ) : null}
    </div>
  )
}

function BuildCell({ build }: { build: WorkspaceSummary["build"] }) {
  const details = buildDetails(build)
  const state = normalize(details.state)
  const meta = buildMeta(build)

  if (state === "success") {
    return (
      <div>
        <span className="inline-flex items-center gap-1.5 text-[12px] text-emerald-600">
          <Check size={14} />
          Success
        </span>
        {meta ? <div className="mt-0.5 font-mono text-[10px] text-slate-500">{meta}</div> : null}
      </div>
    )
  }

  if (state === "failure" || state === "failed") {
    return (
      <div>
        <span className="inline-flex items-center gap-1.5 text-[12px] text-red-600">
          <X size={14} />
          Failure
        </span>
        {meta ? <div className="mt-0.5 font-mono text-[10px] text-slate-500">{meta}</div> : null}
      </div>
    )
  }

  if (state === "pending" || state === "queued") {
    return (
      <span className="inline-flex items-center gap-1.5 text-[12px] text-slate-500">
        <Clock size={13} />
        Pending
      </span>
    )
  }

  if (state === "none" || !state) {
    return <span className="text-[12px] text-slate-300">—</span>
  }

  return <StatusBadge status={details.state} />
}

function TestCell({ workspace }: { workspace: WorkspaceSummary }) {
  const state = normalize(workspace.test.state)

  if (state === "pending" || state === "none") {
    return <span className="text-[12px] text-slate-500">Pending</span>
  }

  return (
    <TestBar
      fail={workspace.test.fail}
      pass={workspace.test.pass}
      total={workspace.test.total}
    />
  )
}

function ReportCell({ workspace }: { workspace: WorkspaceSummary }) {
  if (reportIsReady(workspace)) {
    return <Badge tone="green">Ready</Badge>
  }

  return <span className="text-[12px] text-slate-300">—</span>
}

function ChangedCell({ changed }: { changed: number }) {
  if (changed <= 0) {
    return <span className="font-mono text-[12px] text-slate-300">0</span>
  }

  return (
    <span className="inline-flex items-center gap-1 font-mono text-[12px] text-slate-500">
      <FileText size={12} className="text-slate-400" />
      {changed}
    </span>
  )
}

function RowActions({
  workspace,
  onOpenWorkspace,
  onOpenSession,
  onDelete,
  alwaysVisible = false,
}: {
  workspace: WorkspaceSummary
  onOpenWorkspace: (workspaceId: string) => void
  onOpenSession: (workspaceId: string, sessionId: string, tab?: string) => void
  onDelete: (target: DeleteWorkspaceTarget) => void
  alwaysVisible?: boolean
}) {
  const showReportAction = reportIsReady(workspace) && Boolean(workspace.latestSession)

  return (
    <div
      className={`flex items-center justify-end gap-1 ${
        alwaysVisible ? "" : "opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100"
      }`}
    >
      {showReportAction ? (
        <button
          aria-label={`Open latest report for ${workspace.project}`}
          className="rounded-md p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/30"
          onClick={(event) => {
            event.stopPropagation()
            onOpenSession(workspace.id, workspace.latestSession as string, "report")
          }}
          onKeyDown={(event) => event.stopPropagation()}
          type="button"
        >
          <FileText size={15} />
        </button>
      ) : null}
      <button
        aria-label={`Open workspace details for ${workspace.project}`}
        className="rounded-md p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/30"
        onClick={(event) => {
          event.stopPropagation()
          onOpenWorkspace(workspace.id)
        }}
        onKeyDown={(event) => event.stopPropagation()}
        type="button"
      >
        <ArrowRight size={15} />
      </button>
      <button
        aria-label={`Delete workspace ${workspace.project}`}
        className="rounded-md p-1.5 text-slate-400 hover:bg-red-50 hover:text-red-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500/30"
        onClick={(event) => {
          event.stopPropagation()
          onDelete({
            workspaceId: workspace.id,
            label: workspace.project,
            kind: "workspace",
          })
        }}
        onKeyDown={(event) => event.stopPropagation()}
        type="button"
      >
        <Trash2 size={15} />
      </button>
    </div>
  )
}

function MobileField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="mb-1 font-mono text-[10px] uppercase tracking-[0.12em] text-slate-500">
        {label}
      </div>
      {children}
    </div>
  )
}
