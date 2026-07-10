import { Menu } from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"

import {
  deleteWorkspace as deleteWorkspaceRequest,
  fetchDashboard,
  fetchLaunchQueue,
  fetchSession,
  submitProjectBatch,
  submitTask,
} from "@/api/client"
import type {
  DashboardResponse,
  ExecutionSessionDetail,
  LaunchBatchResult,
  LaunchQueueState,
  SubmitTaskResponse,
} from "@/api/types"
import { Button } from "@/components/common/Button"
import { Card } from "@/components/common/Card"
import { SummaryStrip } from "@/components/SummaryStrip"
import { Tooltip } from "@/components/ui/tooltip"
import { LaunchSetupsDialog } from "@/components/launch/LaunchSetupsDialog"
import { RailSkeleton } from "@/pages/RailSkeleton"
import { WorkspaceRail } from "@/pages/WorkspaceRail"
import { sortByAttentionFirst } from "@/pages/dashboardAttention"
import { DetailPane } from "@/pages/detail/DetailPane"
import { cn } from "@/lib/utils"

const DASHBOARD_POLL_MS = 5000
const SESSION_DETAIL_POLL_MS = 3000
const LAUNCH_HIGHLIGHT_MS = 8000

export function App() {
  const [dashboard, setDashboard] = useState<DashboardResponse | null>(null)
  const [dashboardError, setDashboardError] = useState<string | null>(null)
  const [routeError, setRouteError] = useState<string | null>(null)
  const [sessionDetails, setSessionDetails] = useState<Record<string, ExecutionSessionDetail>>({})
  const [sessionErrors, setSessionErrors] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(true)
  const [launchQueue, setLaunchQueue] = useState<LaunchQueueState | null>(null)
  const [launchDialogOpen, setLaunchDialogOpen] = useState(false)
  const [launchNotice, setLaunchNotice] = useState<string | null>(null)
  const [highlightedWorkspaces, setHighlightedWorkspaces] = useState<string[]>([])
  const [lastUpdatedAt, setLastUpdatedAt] = useState<number | null>(null)
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState<string | null>(null)
  const [selectedSessionId, setSelectedSessionIdState] = useState<string | undefined>(undefined)
  const [selectedFacet, setSelectedFacet] = useState<string | undefined>(undefined)
  const [railOpen, setRailOpen] = useState(false)
  const [deletingIds, setDeletingIds] = useState<Set<string>>(new Set())
  const highlightTimers = useRef<number[]>([])

  const loadLaunchQueue = useCallback(async () => {
    try {
      setLaunchQueue(await fetchLaunchQueue())
    } catch {
      // Queue state is auxiliary; dashboard errors are reported separately.
    }
  }, [])

  useEffect(() => {
    return () => {
      highlightTimers.current.forEach((timer) => window.clearTimeout(timer))
    }
  }, [])

  const loadDashboard = useCallback(async (options?: { silent?: boolean }) => {
    if (!options?.silent) {
      setLoading(true)
    }
    setDashboardError(null)

    try {
      const nextDashboard = await fetchDashboard()
      setDashboard(nextDashboard)
      setLastUpdatedAt(Date.now())
    } catch (err) {
      setDashboardError(String(err))
    } finally {
      if (!options?.silent) {
        setLoading(false)
      }
    }
  }, [])

  useEffect(() => {
    void loadDashboard()
    void loadLaunchQueue()
  }, [loadDashboard, loadLaunchQueue])

  useEffect(() => {
    const interval = window.setInterval(() => {
      void loadDashboard({ silent: true })
      void loadLaunchQueue()
    }, DASHBOARD_POLL_MS)

    return () => window.clearInterval(interval)
  }, [loadDashboard, loadLaunchQueue])

  const ensureSessionDetail = useCallback(
    async (sessionId: string, options?: { silent?: boolean }) => {
      if (!options?.silent) {
        setRouteError(null)
      }

      try {
        const detail = await fetchSession(sessionId)
        setSessionDetails((current) => ({ ...current, [sessionId]: detail }))
        setSessionErrors((current) => {
          if (!(sessionId in current)) {
            return current
          }
          const next = { ...current }
          delete next[sessionId]
          return next
        })
      } catch (err) {
        setRouteError(String(err))
        setSessionErrors((current) => ({ ...current, [sessionId]: String(err) }))
      }
    },
    [],
  )

  const selectedWorkspace =
    dashboard?.workspaces.find((w) => w.id === selectedWorkspaceId) ?? null
  const sessionId = selectedSessionId ?? selectedWorkspace?.latestSession ?? undefined

  // Auto-select the first attention-first workspace once the dashboard loads.
  // Guard on the resolved workspace (not the raw id) so that if the current
  // selection vanishes out-of-band (deleted via CLI, container pruned, dropped
  // by a background poll), auto-select re-fires instead of leaving a dead id.
  useEffect(() => {
    if (!dashboard || selectedWorkspace) {
      return
    }
    const first = sortByAttentionFirst(dashboard.workspaces)[0]
    setSelectedWorkspaceId(first ? first.id : null)
  }, [dashboard, selectedWorkspace])

  useEffect(() => {
    if (!dashboard || !sessionId) {
      return
    }
    void ensureSessionDetail(sessionId)
  }, [dashboard, ensureSessionDetail, sessionId])

  useEffect(() => {
    if (!sessionId) {
      return
    }
    const detail = sessionDetails[sessionId]
    if (detail && !isLiveSessionStatus(detail.status)) {
      return
    }
    const interval = window.setInterval(() => {
      void ensureSessionDetail(sessionId, { silent: true })
    }, SESSION_DETAIL_POLL_MS)
    return () => window.clearInterval(interval)
  }, [ensureSessionDetail, sessionId, sessionDetails])

  // Esc closes the mobile rail drawer.
  useEffect(() => {
    if (!railOpen) {
      return
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setRailOpen(false)
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [railOpen])

  const selectWorkspace = (id: string) => {
    setSelectedWorkspaceId(id)
    setSelectedSessionIdState(undefined) // fall back to that workspace's latest session
    setSelectedFacet(undefined)
    setRouteError(null)
  }
  const selectSession = (id: string) => setSelectedSessionIdState(id)

  const submitWorkspaceTask = async (
    workspaceId: string,
    task: string,
    sourceSession?: string,
  ): Promise<SubmitTaskResponse> => {
    setRouteError(null)
    const response = await submitTask(workspaceId, task, sourceSession)
    void loadDashboard()
    return response
  }

  const deleteWorkspace = async (workspaceId: string): Promise<void> => {
    // Awaited path, used for the small/fast failed-launch removal. Let errors
    // propagate so the confirm dialog can surface a 409.
    await deleteWorkspaceRequest(workspaceId)
    await loadDashboard()
    await loadLaunchQueue()
  }

  // Non-freezing workspace delete: mark the id "deleting", fire the request in
  // the background, and reconcile via a silent reload. Errors surface as a toast
  // (the dialog has already closed). The row shows an inline "deleting…" state.
  const startDelete = (workspaceId: string) => {
    setDeletingIds((prev) => new Set(prev).add(workspaceId))
    void deleteWorkspaceRequest(workspaceId)
      .then(() => {
        void loadDashboard({ silent: true })
        void loadLaunchQueue()
      })
      .catch((err) => {
        setLaunchNotice(
          `Couldn't delete ${workspaceId}: ${err instanceof Error ? err.message : String(err)}`,
        )
      })
      .finally(() => {
        setDeletingIds((prev) => {
          const next = new Set(prev)
          next.delete(workspaceId)
          return next
        })
      })
  }

  // Delete from the Detail Pane: start the background delete and clear the
  // selection immediately so the pane doesn't dead-end (auto-select picks next).
  const deleteWorkspaceFromDetail = (workspaceId: string): Promise<void> => {
    startDelete(workspaceId)
    setSelectedWorkspaceId(null)
    setSelectedSessionIdState(undefined)
    return Promise.resolve()
  }

  const deleteWorkspaces = (ids: string[]): Promise<void> => {
    ids.forEach(startDelete)
    return Promise.resolve()
  }

  const handleBatchSubmitted = (result: LaunchBatchResult) => {
    setLaunchDialogOpen(false)
    const shownRejections = result.rejected.slice(0, 3).map((row) => row.message)
    const hiddenRejections = result.rejected.length - shownRejections.length
    setLaunchNotice(
      result.rejected.length
        ? `${result.accepted.length} setup${result.accepted.length === 1 ? "" : "s"} launched, ` +
            `${result.rejected.length} rejected: ` +
            shownRejections.join("; ") +
            (hiddenRejections > 0 ? ` and ${hiddenRejections} more` : "")
        : null,
    )

    const ids = result.accepted.map((row) => row.workspace_id)
    if (ids.length) {
      setHighlightedWorkspaces((current) => [...new Set([...current, ...ids])])
      const timer = window.setTimeout(() => {
        setHighlightedWorkspaces((current) => current.filter((id) => !ids.includes(id)))
      }, LAUNCH_HIGHLIGHT_MS)
      highlightTimers.current.push(timer)
    }

    void loadDashboard({ silent: true })
    void loadLaunchQueue()
  }

  return (
    <div className="flex h-screen min-h-0 w-full overflow-hidden bg-[#fbfbfc] text-slate-900">
      {/* Mobile-only backdrop behind the rail drawer. */}
      {railOpen ? (
        <button
          aria-label="Close workspaces menu"
          className="fixed inset-0 z-[var(--z-overlay)] bg-slate-900/30 lg:hidden"
          onClick={() => setRailOpen(false)}
          type="button"
        />
      ) : null}

      {loading && !dashboard ? (
        <RailSkeleton className="hidden lg:flex" />
      ) : dashboard ? (
        <WorkspaceRail
          className={cn(
            // Persistent column at lg+, slide-in drawer below lg.
            "fixed inset-y-0 left-0 z-[var(--z-modal)] transition-transform lg:static lg:z-auto lg:translate-x-0",
            railOpen ? "translate-x-0" : "-translate-x-full lg:translate-x-0",
          )}
          data={dashboard}
          deletingIds={deletingIds}
          highlightedWorkspaces={highlightedWorkspaces}
          lastUpdatedAt={lastUpdatedAt}
          launchQueue={launchQueue}
          onAfterSelect={() => setRailOpen(false)}
          onDeleteMany={deleteWorkspaces}
          onLaunchSetups={() => setLaunchDialogOpen(true)}
          onRemoveLaunch={deleteWorkspace}
          onSelect={selectWorkspace}
          pollFailed={Boolean(dashboardError)}
          selectedId={selectedWorkspaceId}
        />
      ) : null}

      <main className="flex min-h-0 flex-1 flex-col overflow-hidden bg-white">
        {/* Mobile top bar: opens the workspace rail drawer (hidden at lg+). */}
        <div className="flex items-center gap-2 border-b border-slate-200 px-4 py-2 lg:hidden">
          <Tooltip label="Open the workspaces list" side="bottom">
            <button
              aria-controls="workspace-rail"
              aria-expanded={railOpen}
              aria-label="Workspaces menu"
              className="rounded-md border border-slate-200 p-1.5 text-slate-600 hover:bg-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)]"
              onClick={() => setRailOpen((value) => !value)}
              type="button"
            >
              <Menu size={16} />
            </button>
          </Tooltip>
          <span className="truncate font-mono text-[12px] font-semibold text-slate-700">
            {selectedWorkspace ? selectedWorkspace.project : "SAG Workbench"}
          </span>
        </div>

        {dashboard ? <SummaryStrip workspaces={dashboard.workspaces} /> : null}

        <div className="min-h-0 flex-1 overflow-hidden">
        {!dashboard && !loading && dashboardError ? (
          <div className="p-6">
            <Card className="max-w-xl p-5">
              <div className="text-[15px] font-semibold text-slate-900">Dashboard unavailable</div>
              <div className="mt-2 font-mono text-[12px] text-red-600">{dashboardError}</div>
              <Button className="mt-4" onClick={() => void loadDashboard()} type="button" variant="outline">
                Retry
              </Button>
            </Card>
          </div>
        ) : dashboard && selectedWorkspace && sessionId && sessionDetails[sessionId] ? (
          <DetailPane
            key={sessionId}
            detail={sessionDetails[sessionId]}
            initialFacet={selectedFacet}
            onDelete={deleteWorkspaceFromDetail}
            onSession={selectSession}
            onSubmitTask={submitWorkspaceTask}
            sessionId={sessionId}
            workspace={selectedWorkspace}
          />
        ) : dashboard && selectedWorkspace && sessionId && sessionErrors[sessionId] ? (
          <div className="p-6">
            <Card className="max-w-xl p-5">
              <div className="text-[15px] font-semibold text-slate-900">Session {sessionId} unavailable</div>
              <div className="mt-2 font-mono text-[12px] text-red-600">{sessionErrors[sessionId]}</div>
              <Button className="mt-4" onClick={() => void ensureSessionDetail(sessionId)} type="button" variant="outline">
                Retry
              </Button>
            </Card>
          </div>
        ) : dashboard && selectedWorkspace ? (
          <div className="p-6 font-mono text-[13px] text-slate-500">
            {sessionId ? `Loading session ${sessionId}…` : "This workspace has no execution session yet."}
          </div>
        ) : dashboard ? (
          <div className="flex h-full items-center justify-center p-6 text-center">
            <div>
              <div className="text-[15px] font-semibold text-slate-800">Select a workspace</div>
              <p className="mt-1 text-[13px] text-slate-500">Pick a workspace from the rail, or launch a new setup.</p>
            </div>
          </div>
        ) : null}
        </div>
      </main>

      {launchNotice ? (
        <div className="fixed bottom-4 left-1/2 z-[var(--z-modal)] -translate-x-1/2">
          <Card className="flex items-center gap-3 border-blue-100 bg-blue-50/90 px-4 py-3 text-[13px] shadow-lg backdrop-blur">
            <span className="text-blue-700">{launchNotice}</span>
            <Button onClick={() => setLaunchNotice(null)} type="button" variant="outline">Dismiss</Button>
          </Card>
        </div>
      ) : null}

      {launchDialogOpen ? (
        <LaunchSetupsDialog
          defaultConcurrency={launchQueue?.default_concurrency ?? 1}
          onClose={() => setLaunchDialogOpen(false)}
          onSubmit={submitProjectBatch}
          onSubmitted={handleBatchSubmitted}
        />
      ) : null}
    </div>
  )
}

function isLiveSessionStatus(status: string): boolean {
  return ["active", "pending", "queued", "running", "in_progress"].includes(
    status.trim().toLowerCase(),
  )
}
