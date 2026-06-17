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
import { StatusBadge } from "@/components/common/Badge"
import { Button } from "@/components/common/Button"
import { Card } from "@/components/common/Card"
import { LaunchSetupsDialog } from "@/components/launch/LaunchSetupsDialog"
import { Dashboard } from "@/pages/Dashboard"
import { DashboardSkeleton } from "@/pages/DashboardSkeleton"
import { DetailPane } from "@/pages/detail/DetailPane"

const DASHBOARD_POLL_MS = 5000
const SESSION_DETAIL_POLL_MS = 3000
const LAUNCH_HIGHLIGHT_MS = 8000

type Route =
  | { view: "dashboard" }
  | { view: "detail"; workspaceId: string; sessionId?: string; facet?: string }

export function App() {
  const [route, setRoute] = useState<Route>({ view: "dashboard" })
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
    route.view === "detail" ? dashboard?.workspaces.find((w) => w.id === route.workspaceId) : undefined
  const selectedSessionId =
    route.view === "detail" ? route.sessionId ?? selectedWorkspace?.latestSession ?? undefined : undefined

  useEffect(() => {
    if (!dashboard || route.view !== "detail" || !selectedSessionId) {
      return
    }
    void ensureSessionDetail(selectedSessionId)
  }, [dashboard, ensureSessionDetail, route.view, selectedSessionId])

  useEffect(() => {
    if (route.view !== "detail" || !selectedSessionId) {
      return
    }
    const detail = sessionDetails[selectedSessionId]
    if (detail && !isLiveSessionStatus(detail.status)) {
      return
    }
    const interval = window.setInterval(() => {
      void ensureSessionDetail(selectedSessionId, { silent: true })
    }, SESSION_DETAIL_POLL_MS)
    return () => window.clearInterval(interval)
  }, [ensureSessionDetail, route.view, selectedSessionId, sessionDetails])

  const openDashboard = () => {
    setRouteError(null)
    setRoute({ view: "dashboard" })
  }
  const openDetail = (workspaceId: string, sessionId?: string, facet?: string) => {
    setRouteError(null)
    setRoute({ view: "detail", workspaceId, sessionId, facet })
  }
  // Dashboard row → detail (latest session). Dashboard report action → detail at that session.
  const openWorkspace = (workspaceId: string) => openDetail(workspaceId)
  const openSession = (workspaceId: string, sessionId: string, tab?: string) =>
    openDetail(workspaceId, sessionId, tab)

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
    // Let errors propagate so the confirm dialog can surface a 409.
    await deleteWorkspaceRequest(workspaceId)
    await loadDashboard()
    await loadLaunchQueue()
  }

  // Delete initiated from the Detail Pane: after success the workspace is gone,
  // so return to the dashboard instead of the "Workspace not found" dead-end.
  const deleteWorkspaceFromDetail = async (workspaceId: string): Promise<void> => {
    await deleteWorkspace(workspaceId)
    openDashboard()
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
    <div className="min-h-screen bg-[#fbfbfc] text-slate-900">
      <header className="sticky top-0 z-[var(--z-sticky)] border-b border-slate-200 bg-white/85 backdrop-blur">
        <div className="mx-auto flex min-h-12 max-w-[1180px] flex-wrap items-center gap-3 px-4 py-2 sm:px-6 lg:h-12 lg:px-8 lg:py-0">
          <button
            className="flex items-center gap-2"
            onClick={openDashboard}
            type="button"
          >
            <span className="flex h-6 w-6 items-center justify-center rounded bg-blue-600 font-mono text-[11px] font-bold text-white">
              S
            </span>
            <span className="font-mono text-[12px] font-semibold tracking-tight text-slate-800">
              sag
            </span>
          </button>
          <span className="text-slate-200">/</span>
          <Breadcrumb route={route} onDashboard={openDashboard} />
          <div className="ml-auto flex items-center gap-2">
            {dashboard ? (
              <div className="flex items-center gap-1.5 rounded-md border border-slate-200 px-2 py-1">
                <StatusBadge
                  className="border-0 bg-transparent px-0 py-0"
                  status={dashboard.docker.status}
                  label={
                    <span className="font-mono text-[10.5px] text-slate-500">
                      docker · {dashboard.docker.status}
                    </span>
                  }
                />
                {dashboard.docker.version ? (
                  <span className="font-mono text-[10.5px] text-slate-500">
                    v{dashboard.docker.version}
                  </span>
                ) : null}
              </div>
            ) : (
              <div className="rounded-md border border-slate-200 px-2 py-1 font-mono text-[10.5px] text-slate-500">
                docker · checking
              </div>
            )}
          </div>
        </div>
      </header>

      {loading && !dashboard ? <DashboardSkeleton /> : null}

      {!dashboard && !loading && dashboardError ? (
        <main className="mx-auto max-w-[1180px] px-4 py-5 sm:px-6 lg:px-8 lg:py-7">
          <Card className="max-w-xl p-5">
            <div className="text-[15px] font-semibold text-slate-900">Dashboard unavailable</div>
            <div className="mt-2 font-mono text-[12px] text-red-600">{dashboardError}</div>
            <Button
              className="mt-4"
              onClick={() => void loadDashboard()}
              type="button"
              variant="outline"
            >
              Retry
            </Button>
          </Card>
        </main>
      ) : null}

      {dashboard && routeError ? (
        <div className="mx-auto max-w-[1180px] px-4 pt-5 sm:px-6 lg:px-8">
          <Card className="border-amber-100 bg-amber-50/50 px-4 py-3 text-[13px]">
            <div className="font-semibold text-amber-700">Workspace data unavailable</div>
            <div className="mt-0.5 font-mono text-[12px] text-amber-700">{routeError}</div>
          </Card>
        </div>
      ) : null}

      {launchNotice ? (
        <div className="mx-auto max-w-[1180px] px-4 pt-5 sm:px-6 lg:px-8">
          <Card className="flex flex-col gap-3 border-blue-100 bg-blue-50/50 px-4 py-3 text-[13px] sm:flex-row sm:items-center sm:justify-between">
            <div className="text-blue-700">{launchNotice}</div>
            <Button onClick={() => setLaunchNotice(null)} type="button" variant="outline">
              Dismiss
            </Button>
          </Card>
        </div>
      ) : null}

      {dashboard && route.view === "dashboard" ? (
        <Dashboard
          data={dashboard}
          highlightedWorkspaces={highlightedWorkspaces}
          lastUpdatedAt={lastUpdatedAt}
          launchQueue={launchQueue}
          onDeleteWorkspace={deleteWorkspace}
          onLaunchSetups={() => setLaunchDialogOpen(true)}
          onOpenSession={openSession}
          onOpenWorkspace={openWorkspace}
          onRefresh={() => void loadDashboard()}
          pollError={dashboardError}
          pollFailed={Boolean(dashboardError)}
          refreshing={loading}
        />
      ) : null}

      {dashboard && route.view === "detail" ? (
        selectedWorkspace ? (
          selectedSessionId && sessionDetails[selectedSessionId] ? (
            <DetailPane
              key={selectedSessionId}
              detail={sessionDetails[selectedSessionId]}
              initialFacet={route.facet}
              onDelete={deleteWorkspaceFromDetail}
              onSession={(sid) => openDetail(route.workspaceId, sid)}
              onSubmitTask={submitWorkspaceTask}
              sessionId={selectedSessionId}
              workspace={selectedWorkspace}
            />
          ) : selectedSessionId && sessionErrors[selectedSessionId] ? (
            <main className="mx-auto max-w-[1180px] px-4 py-5 sm:px-6 lg:px-8 lg:py-7">
              <Card className="max-w-xl p-5">
                <div className="text-[15px] font-semibold text-slate-900">
                  Session {selectedSessionId} unavailable
                </div>
                <div className="mt-2 font-mono text-[12px] text-red-600">
                  {sessionErrors[selectedSessionId]}
                </div>
                <div className="mt-4 flex gap-2">
                  <Button
                    onClick={() => void ensureSessionDetail(selectedSessionId)}
                    type="button"
                    variant="outline"
                  >
                    Retry
                  </Button>
                  <Button onClick={openDashboard} type="button" variant="ghost">
                    Back to dashboard
                  </Button>
                </div>
              </Card>
            </main>
          ) : (
            <main className="mx-auto max-w-[1180px] px-4 py-5 sm:px-6 lg:px-8 lg:py-7">
              <Card className="inline-flex px-3 py-2 text-[13px] text-slate-500">
                {selectedSessionId
                  ? `Loading session ${selectedSessionId}...`
                  : "This workspace has no execution session yet."}
              </Card>
            </main>
          )
        ) : (
          <main className="mx-auto max-w-[1180px] px-4 py-5 sm:px-6 lg:px-8 lg:py-7">
            <Card className="p-5">
              <div className="text-[15px] font-semibold text-slate-900">Workspace not found</div>
              <div className="mt-1 font-mono text-[12px] text-slate-500">{route.workspaceId}</div>
            </Card>
          </main>
        )
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

function Breadcrumb({ route, onDashboard }: { route: Route; onDashboard: () => void }) {
  return (
    <nav className="flex min-w-0 items-center gap-2">
      <button
        className={`whitespace-nowrap text-[12.5px] ${
          route.view === "dashboard" ? "font-medium text-slate-700" : "text-slate-500 hover:text-slate-700"
        }`}
        disabled={route.view === "dashboard"}
        onClick={onDashboard}
        type="button"
      >
        dashboard
      </button>
      {route.view === "detail" ? (
        <>
          <span className="text-slate-200">/</span>
          <span className="truncate text-[12.5px] font-medium text-slate-700">{route.workspaceId}</span>
        </>
      ) : null}
    </nav>
  )
}
