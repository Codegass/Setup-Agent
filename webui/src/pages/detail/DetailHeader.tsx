import { MoreHorizontal, Plus, Settings as SettingsIcon, Terminal } from "lucide-react"
import type { LucideIcon } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import type { ExecutionSessionDetail, WorkspaceSummary } from "@/api/types"
import { statusMeta } from "@/components/common/status"
import { Tooltip } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"

function SecondaryButton({
  icon: Icon,
  label,
  hint,
  onClick,
}: {
  icon: LucideIcon
  label: string
  hint: string
  onClick?: () => void
}) {
  return (
    <Tooltip label={hint} side="bottom">
      <button
        className="inline-flex h-[34px] items-center gap-1.5 rounded-lg border border-border bg-card px-3 text-[13px] font-semibold text-foreground hover:bg-accent"
        onClick={onClick}
        type="button"
      >
        <Icon size={14} />
        <span className="hidden sm:inline">{label}</span>
      </button>
    </Tooltip>
  )
}

function stepsClause(steps: number | null | undefined, budget: number | null | undefined): string | null {
  if (steps == null) return null
  if (budget != null) return `${steps} / ${budget} steps`
  return `${steps} steps`
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
  const [menuOpen, setMenuOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!menuOpen) return
    function handle(event: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setMenuOpen(false)
      }
    }
    document.addEventListener("mousedown", handle)
    return () => document.removeEventListener("mousedown", handle)
  }, [menuOpen])

  const sessions = workspace.sessions ?? []
  const entry = detail?.entry?.trim()

  // Single mono metadata line: omit any null/empty piece, join with " · ".
  const meta = [
    workspace.container,
    workspace.stack,
    workspace.commit,
    detail?.model,
    stepsClause(detail?.steps, detail?.stepBudget),
    detail?.duration,
    workspace.updated ? `finished ${workspace.updated}` : null,
  ]
    .filter((piece): piece is string => Boolean(piece && String(piece).trim()))
    .join(" · ")

  return (
    <div className="sticky top-0 z-[var(--z-sticky)] flex-none border-b border-border bg-card/90 px-5 pb-4 pt-4 backdrop-blur-md sm:px-6">
      <div className="flex items-start gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2.5">
            <h2 className="truncate text-[19px] font-bold leading-tight tracking-[-0.02em] text-foreground">
              {workspace.project}
            </h2>
            {entry ? (
              <span className="shrink-0 font-mono text-[11px] text-muted-foreground">{entry}</span>
            ) : null}
          </div>
          {meta ? (
            <div className="mt-[3px] truncate font-mono text-[12px] leading-snug text-muted-foreground">
              {meta}
            </div>
          ) : null}
        </div>

        <div className="flex shrink-0 items-center gap-2">
          <Tooltip label="Run a new task in this workspace" side="bottom">
            <button
              className="inline-flex h-[34px] items-center gap-1.5 rounded-lg bg-primary px-3.5 text-[13px] font-semibold text-primary-foreground hover:opacity-90"
              onClick={onNewTask}
              type="button"
            >
              <Plus size={14} />
              <span className="hidden sm:inline">New task</span>
            </button>
          </Tooltip>
          <SecondaryButton icon={Terminal} label="Terminal" hint="Open a terminal in this container" onClick={onTerminal} />
          <SecondaryButton icon={SettingsIcon} label="Settings" hint="View workspace settings" onClick={onSettings} />

          <div className="relative" ref={menuRef}>
            <Tooltip label="More actions (sessions, delete)" side="bottom">
              <button
                aria-haspopup="menu"
                aria-expanded={menuOpen}
                aria-label="More"
                className="inline-flex h-[34px] w-[34px] items-center justify-center rounded-lg border border-border bg-card text-muted-foreground hover:bg-accent"
                onClick={() => setMenuOpen((open) => !open)}
                type="button"
              >
                <MoreHorizontal size={16} />
              </button>
            </Tooltip>

            {menuOpen ? (
              <div
                className="absolute right-0 top-[40px] z-[var(--z-popover,40)] w-[240px] rounded-xl border border-border bg-card p-1.5 shadow-lg"
                role="menu"
              >
                {sessions.length > 1 ? (
                  <div className="border-b border-border pb-1.5">
                    <div className="px-2 py-1 font-mono text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
                      Sessions
                    </div>
                    {sessions.map((s) => {
                      const active = s.id === sessionId
                      return (
                        <button
                          key={s.id}
                          aria-current={active}
                          className={cn(
                            "flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-[12.5px] transition-colors",
                            active
                              ? "bg-status-running-soft text-status-running"
                              : "text-muted-foreground hover:bg-accent",
                          )}
                          onClick={() => {
                            onSession(s.id)
                            setMenuOpen(false)
                          }}
                          role="menuitemradio"
                          aria-checked={active}
                          title={s.title}
                          type="button"
                        >
                          <span
                            className={cn(
                              "inline-flex h-1.5 w-1.5 shrink-0 rounded-full",
                              `bg-status-${toneToken(statusMeta(s.status).tone)}`,
                            )}
                          />
                          <span className="font-mono">{s.id}</span>
                          <span className="truncate text-muted-foreground">{s.title}</span>
                        </button>
                      )
                    })}
                  </div>
                ) : null}
                <button
                  className="mt-1 w-full rounded-lg px-2 py-2 text-left text-[13px] font-semibold text-status-failed hover:bg-status-failed-soft"
                  onClick={() => {
                    setMenuOpen(false)
                    onDelete()
                  }}
                  role="menuitem"
                  type="button"
                >
                  Delete workspace…
                </button>
              </div>
            ) : null}
          </div>
        </div>
      </div>
    </div>
  )
}

function toneToken(tone: string): string {
  return (
    { neutral: "idle", blue: "running", green: "success", red: "failed", amber: "attention" }[tone] ?? "idle"
  )
}
