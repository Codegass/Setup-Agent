import { useEffect, useMemo, useRef, useState } from "react"

import type { ExecutionSessionDetail, SubmitTaskResponse, WorkspaceSummary } from "@/api/types"
import { NewTaskModal } from "@/components/workspace/NewTaskModal"
import {
  WorkspacePanel,
  type WorkspacePanelKind,
} from "@/components/workspace/WorkspacePanels"
import {
  DeleteWorkspaceDialog,
  type DeleteWorkspaceTarget,
} from "@/components/workspace/DeleteWorkspaceDialog"
import { cn } from "@/lib/utils"

import { DetailHeader } from "./DetailHeader"
import { ResultBand } from "./ResultBand"
import { buildDetailTabs, TabBody, type TabId } from "./facets"

/** The tab bar: one nav model where a tab swaps the panel below it. A badge = items
 *  needing attention. Markup/styling mirrors WorkbenchDetail.dc.html lines 76–92 (the
 *  bottom-border tab nav at the top of the AFTER block). */
function TabBar({
  tabs,
  active,
  onSelect,
}: {
  tabs: ReturnType<typeof buildDetailTabs>
  active: TabId
  onSelect: (id: TabId) => void
}) {
  return (
    <nav
      aria-label="Detail tabs"
      className="flex shrink-0 items-center gap-0.5 overflow-x-auto border-b border-border bg-card px-5 sm:px-7"
    >
      {tabs.map((tab) => {
        const on = active === tab.id
        return (
          <button
            key={tab.id}
            data-tab={tab.id}
            aria-current={on}
            className={cn(
              "inline-flex shrink-0 items-center gap-2 border-b-2 px-3 py-2.5 text-[13px] font-semibold transition-colors",
              on
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
            onClick={() => onSelect(tab.id)}
            type="button"
          >
            <span>{tab.label}</span>
            {tab.count != null ? (
              <span
                className={cn(
                  "inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-full px-1.5 text-[11px] font-bold tabular-nums",
                  tab.tone === "red"
                    ? "bg-status-failed-soft text-status-failed"
                    : "bg-accent text-muted-foreground",
                )}
              >
                {tab.count}
              </span>
            ) : null}
          </button>
        )
      })}
    </nav>
  )
}

export function DetailPane({
  workspace,
  detail,
  sessionId,
  initialFacet,
  onSession,
  onSubmitTask,
  onDelete,
}: {
  workspace: WorkspaceSummary
  detail: ExecutionSessionDetail
  sessionId: string
  initialFacet?: string
  onSession: (sessionId: string) => void
  onSubmitTask: (workspaceId: string, task: string, sourceSession?: string) => Promise<SubmitTaskResponse>
  onDelete: (workspaceId: string) => Promise<void>
}) {
  const tabs = useMemo(() => buildDetailTabs(detail), [detail])
  const tabIds = useMemo(() => tabs.map((tab) => tab.id), [tabs])
  const initial: TabId =
    initialFacet === "turns" ? "flow" : initialFacet && tabs.some((t) => t.id === initialFacet)
      ? (initialFacet as TabId)
      : "overview"
  const [active, setActive] = useState<TabId>(initial)
  const tabsRef = useRef<HTMLDivElement>(null)
  const openTab = (id: TabId) => {
    setActive(id)
    tabsRef.current?.scrollIntoView({ block: "start" })
    tabsRef.current?.querySelector<HTMLButtonElement>(`button[data-tab="${id}"]`)?.focus({ preventScroll: true })
  }

  // A new session (or one whose tab set no longer contains the active tab) resets
  // the panel back to the requested/default tab.
  useEffect(() => {
    setActive(initial)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId])

  const [panel, setPanel] = useState<WorkspacePanelKind | null>(null)
  const [taskOpen, setTaskOpen] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<DeleteWorkspaceTarget | null>(null)

  return (
    <div className="flex h-full min-h-0 flex-col">
      <DetailHeader
        detail={detail}
        onDelete={() =>
          setDeleteTarget({ workspaceId: workspace.id, label: workspace.project, kind: "workspace" })
        }
        onNewTask={() => setTaskOpen(true)}
        onSession={onSession}
        onSettings={() => setPanel("settings")}
        onTerminal={() => setPanel("terminal")}
        sessionId={sessionId}
        workspace={workspace}
      />

      <div className="min-h-0 flex-1 overflow-y-auto" key={sessionId}>
        <div className="bg-card px-5 pb-5 pt-5 sm:px-7">
          <div className="mx-auto max-w-[1000px]">
            <ResultBand
              availableTabs={tabIds}
              card={detail.resultCard}
              onOpenTab={openTab}
              snapshotStatus={detail.snapshotStatus}
            />
          </div>
        </div>

        <div ref={tabsRef} className="sticky top-0 z-10">
          <TabBar active={active} onSelect={setActive} tabs={tabs} />
        </div>

        <main className="min-h-[calc(100%-3rem)] px-5 py-6 sm:px-7">
          <div className="mx-auto max-w-[1000px]">
            <TabBody detail={detail} onOpenFlow={() => openTab("flow")} onSubmitTask={onSubmitTask} tabId={active} />
          </div>
        </main>
      </div>

      {panel ? (
        <WorkspacePanel kind={panel} latest={detail} onClose={() => setPanel(null)} workspace={workspace} />
      ) : null}

      {taskOpen ? (
        <NewTaskModal
          onClose={() => setTaskOpen(false)}
          onSubmit={async (task, sourceSession) => {
            await onSubmitTask(workspace.id, task, sourceSession)
            setTaskOpen(false)
          }}
          sourceSession={sessionId}
          workspace={workspace}
        />
      ) : null}

      {deleteTarget ? (
        <DeleteWorkspaceDialog
          onCancel={() => setDeleteTarget(null)}
          onConfirm={async (id) => {
            await onDelete(id)
            setDeleteTarget(null)
          }}
          target={deleteTarget}
        />
      ) : null}
    </div>
  )
}
