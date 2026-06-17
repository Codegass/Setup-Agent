import { Activity, AlertTriangle, Check, Clock, GitBranch, Rocket, Search, X } from "lucide-react"
import { useState } from "react"

import type { DashboardResponse, LaunchQueueState, WorkspaceSummary } from "@/api/types"
import { TestBar } from "@/components/common/TestBar"
import { statusMeta } from "@/components/common/status"
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
  onSelect,
}: {
  workspace: WorkspaceSummary
  selected: boolean
  highlighted: boolean
  onSelect: (id: string) => void
}) {
  const dockerNorm = normalize(workspace.docker.status)
  const dot = DOT_TONE[statusMeta(workspace.docker.status).tone] ?? DOT_TONE.neutral
  const build = buildState(workspace.build)
  const attention = needsAttention(workspace)
  const total = Math.max(workspace.test.total, workspace.test.pass + workspace.test.fail)
  return (
    <button
      aria-current={selected}
      aria-label={`Open workspace ${workspace.project}`}
      className={cn(
        "group flex w-full items-center gap-3 border-b border-slate-100 px-3.5 py-2.5 text-left transition-colors last:border-b-0",
        selected ? "bg-status-running-soft" : attention ? "bg-status-failed-soft/40 hover:bg-status-failed-soft/60" : "hover:bg-slate-50/80",
        highlighted && !selected ? "bg-blue-50/60" : "",
      )}
      onClick={() => onSelect(workspace.id)}
      type="button"
    >
      <span className={cn("relative inline-flex h-1.5 w-1.5 shrink-0 rounded-full", dot)}>
        {dockerNorm === "running" || dockerNorm === "launching" ? (
          <span className={cn("absolute inline-flex h-full w-full animate-ping rounded-full opacity-75", dot)} />
        ) : null}
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5">
          <span className={cn("truncate text-[13px] font-medium", selected ? "text-status-running" : "text-slate-800")}>
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
        {build === "success" ? <Check className="text-status-success" size={13} /> : build === "failure" || build === "failed" ? <X className="text-status-failed" size={13} /> : <Clock className="text-slate-400" size={12} />}
        {normalize(workspace.test.state) !== "none" && total > 0 ? (
          <TestBar fail={workspace.test.fail} pass={workspace.test.pass} total={total} />
        ) : (
          <span className="w-10 text-right font-mono text-[10px] text-slate-400">—</span>
        )}
      </span>
    </button>
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
  highlightedWorkspaces = [],
  lastUpdatedAt = null,
  pollFailed = false,
}: {
  data: DashboardResponse
  selectedId: string | null
  onSelect: (id: string) => void
  onLaunchSetups: () => void
  highlightedWorkspaces?: string[]
  lastUpdatedAt?: number | null
  pollFailed?: boolean
}) {
  const [query, setQuery] = useState("")
  const ordered = sortByAttentionFirst(data.workspaces)
  const q = query.trim().toLowerCase()
  const rows = q
    ? ordered.filter((w) => w.project.toLowerCase().includes(q) || (w.stack ?? "").toLowerCase().includes(q))
    : ordered
  const running = data.workspaces.filter((w) => normalize(w.docker.status) === "running").length
  const attention = data.workspaces.filter(needsAttention).length

  return (
    <aside className="flex h-full min-h-0 w-[320px] shrink-0 flex-col border-r border-slate-200 bg-white">
      <div className="border-b border-slate-200 px-4 pb-3 pt-4">
        <div className="flex items-center gap-2">
          <span className="flex h-6 w-6 items-center justify-center rounded bg-slate-900 font-mono text-[11px] font-bold text-white">S</span>
          <div className="min-w-0">
            <div className="text-[13px] font-semibold tracking-tight text-slate-900">SAG Workbench</div>
            <div className="flex items-center gap-1 font-mono text-[9px] uppercase tracking-[0.14em] text-slate-500">
              <span className="inline-flex h-1 w-1 rounded-full bg-status-success" /> docker {data.docker.version ?? data.docker.status}
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
          <Search className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400" size={13} />
          <input
            className="w-full rounded-md border border-slate-200 bg-slate-50/60 py-1.5 pl-8 pr-2 text-[12.5px] text-slate-700 placeholder:text-slate-400 focus:border-blue-300 focus:bg-white focus:outline-none focus:ring-2 focus:ring-blue-500/20"
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter workspaces…"
            value={query}
          />
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {rows.length ? (
          rows.map((w) => (
            <RailRow
              key={w.id}
              highlighted={highlightedWorkspaces.includes(w.id)}
              onSelect={onSelect}
              selected={w.id === selectedId}
              workspace={w}
            />
          ))
        ) : data.workspaces.length === 0 ? (
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
    </aside>
  )
}
