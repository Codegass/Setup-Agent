import { GitBranch, Plus, Settings as SettingsIcon, Terminal, Trash2 } from "lucide-react"
import type { LucideIcon } from "lucide-react"

import type { ExecutionSessionDetail, WorkspaceSummary } from "@/api/types"
import { Badge, LabeledStatus, StatusBadge } from "@/components/common/Badge"
import { statusMeta } from "@/components/common/status"
import { cn } from "@/lib/utils"

function HeaderButton({
  icon: Icon,
  label,
  onClick,
  primary,
  danger,
  title,
}: {
  icon: LucideIcon
  label?: string
  onClick?: () => void
  primary?: boolean
  danger?: boolean
  title?: string
}) {
  if (primary) {
    return (
      <button
        className="inline-flex items-center gap-1.5 rounded-md bg-slate-900 px-2.5 py-1.5 text-[12px] font-medium text-white hover:bg-slate-800"
        onClick={onClick}
        type="button"
      >
        <Icon size={14} />
        {label}
      </button>
    )
  }
  return (
    <button
      aria-label={title}
      className={cn(
        "rounded-md p-1.5 text-slate-400",
        danger ? "hover:bg-red-50 hover:text-red-600" : "hover:bg-slate-100 hover:text-slate-700",
      )}
      onClick={onClick}
      title={title}
      type="button"
    >
      <Icon size={16} />
    </button>
  )
}

export function DetailHeader({
  workspace,
  detail,
  sessionId,
  onSession,
  onNewTask,
  onTerminal,
  onSettings,
  onDelete,
}: {
  workspace: WorkspaceSummary
  detail?: ExecutionSessionDetail
  sessionId: string
  onSession: (sessionId: string) => void
  onNewTask: () => void
  onTerminal: () => void
  onSettings: () => void
  onDelete: () => void
}) {
  const sessions = workspace.sessions ?? []
  const meta = [workspace.stack, workspace.commit, workspace.updated ? `updated ${workspace.updated}` : null]
    .filter(Boolean)
    .join(" · ")

  return (
    <div className="sticky top-0 z-[var(--z-sticky)] border-b border-slate-200 bg-white/85 px-5 py-3.5 backdrop-blur-md sm:px-7">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-slate-200 bg-slate-50 text-slate-500">
              <GitBranch size={16} />
            </div>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <h2 className="truncate text-[18px] font-semibold tracking-tight text-slate-900">
                  {workspace.project}
                </h2>
                {workspace.release ? (
                  <Badge className="border-slate-200 bg-slate-50 text-slate-500" mono>
                    {workspace.release}
                  </Badge>
                ) : null}
                {detail?.partial ? (
                  <Badge tone="amber">partial discovery</Badge>
                ) : null}
              </div>
              <div className="mt-0.5 truncate font-mono text-[10.5px] text-slate-500">
                <span className="text-slate-600">{workspace.container}</span>
                {meta ? ` · ${meta}` : ""}
              </div>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          {detail ? <LabeledStatus label="Flow" status={detail.status} /> : null}
          <StatusBadge status={workspace.docker.status} />
          <div className="mx-1 h-5 w-px bg-slate-200" />
          <HeaderButton icon={Plus} label="New task" onClick={onNewTask} primary />
          <HeaderButton icon={Terminal} onClick={onTerminal} title="Terminal" />
          <HeaderButton icon={SettingsIcon} onClick={onSettings} title="Settings" />
          <HeaderButton danger icon={Trash2} onClick={onDelete} title="Delete" />
        </div>
      </div>

      {sessions.length > 1 ? (
        <div className="mt-3 flex items-center gap-1.5 overflow-x-auto pb-0.5">
          <span className="shrink-0 font-mono text-[10px] uppercase tracking-[0.12em] text-slate-500">
            Sessions
          </span>
          {sessions.map((s) => {
            const active = s.id === sessionId
            return (
              <button
                key={s.id}
                aria-current={active}
                className={cn(
                  "group flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11.5px] transition-colors",
                  active
                    ? "border-status-running-border bg-status-running-soft text-status-running"
                    : "border-slate-200 bg-white text-slate-500 hover:bg-slate-50",
                )}
                onClick={() => onSession(s.id)}
                title={s.title}
                type="button"
              >
                <span
                  className={cn(
                    "inline-flex h-1.5 w-1.5 rounded-full",
                    `bg-status-${toneToken(statusMeta(s.status).tone)}`,
                  )}
                />
                <span className="font-mono">{s.id}</span>
                <span className="hidden max-w-[150px] truncate sm:inline">{s.title}</span>
              </button>
            )
          })}
        </div>
      ) : null}
    </div>
  )
}

function toneToken(tone: string): string {
  return (
    { neutral: "idle", blue: "running", green: "success", red: "failed", amber: "attention" }[tone] ?? "idle"
  )
}
