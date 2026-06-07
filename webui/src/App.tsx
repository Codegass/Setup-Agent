import { useCallback, useEffect, useRef, useState } from "react"

import {
  fetchDashboard,
  fetchLaunchQueue,
  fetchSession,
  submitProjectBatch,
  submitTask,
} from "@/api/client"
import type {
  BuildSummary,
  DashboardResponse,
  ExecutionSessionDetail,
  ExecutionSessionSummary,
  LaunchBatchResult,
  LaunchQueueState,
  SubmitTaskResponse,
  WorkspaceSummary,
} from "@/api/types"
import { StatusBadge } from "@/components/common/Badge"
import { Button } from "@/components/common/Button"
import { Card } from "@/components/common/Card"
import { LaunchSetupsDialog } from "@/components/launch/LaunchSetupsDialog"
import { Dashboard } from "@/pages/Dashboard"
import { SessionDetail } from "@/pages/SessionDetail"
import { Workspace, type WorkspaceSessionRow } from "@/pages/Workspace"

const DASHBOARD_POLL_MS = 5000
const SESSION_DETAIL_POLL_MS = 3000
const LAUNCH_HIGHLIGHT_MS = 8000

type Route =
  | { view: "dashboard" }
  | { view: "workspace"; workspaceId: string; newTaskSourceSession?: string | null }
  | { view: "session"; workspaceId: string; sessionId: string; tab?: string }

export function App() {
  const [route, setRoute] = useState<Route>({ view: "dashboard" })
  const [dashboard, setDashboard] = useState<DashboardResponse | null>(null)
  const [dashboardError, setDashboardError] = useState<string | null>(null)
  const [routeError, setRouteError] = useState<string | null>(null)
  const [sessionDetails, setSessionDetails] = useState<Record<string, ExecutionSessionDetail>>({})
  const [sessionLoading, setSessionLoading] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [launchQueue, setLaunchQueue] = useState<LaunchQueueState | null>(null)
  const [launchDialogOpen, setLaunchDialogOpen] = useState(false)
  const [launchNotice, setLaunchNotice] = useState<string | null>(null)
  const [highlightedWorkspaces, setHighlightedWorkspaces] = useState<string[]>([])
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

  const ensureSessionDetail = useCallback(async (sessionId: string, options?: { silent?: boolean }) => {
    if (!options?.silent) {
      setSessionLoading(sessionId)
    }
    setRouteError(null)

    try {
      const detail = await fetchSession(sessionId)
      setSessionDetails((current) => ({ ...current, [sessionId]: detail }))
    } catch (err) {
      setRouteError(String(err))
    } finally {
      if (!options?.silent) {
        setSessionLoading((current) => (current === sessionId ? null : current))
      }
    }
  }, [])

  useEffect(() => {
    if (!dashboard) {
      return
    }

    if (route.view === "session") {
      void ensureSessionDetail(route.sessionId)
      return
    }

    if (route.view === "workspace") {
      const workspace = dashboard.workspaces.find((candidate) => candidate.id === route.workspaceId)
      if (workspace?.latestSession) {
        void ensureSessionDetail(workspace.latestSession)
      }
    }
  }, [dashboard, ensureSessionDetail, route])

  useEffect(() => {
    if (route.view !== "session") {
      return
    }

    const detail = sessionDetails[route.sessionId]
    if (detail && !isLiveSessionStatus(detail.status)) {
      return
    }

    const interval = window.setInterval(() => {
      void ensureSessionDetail(route.sessionId, { silent: true })
    }, SESSION_DETAIL_POLL_MS)

    return () => window.clearInterval(interval)
  }, [ensureSessionDetail, route, sessionDetails])

  const openDashboard = () => {
    setRouteError(null)
    setRoute({ view: "dashboard" })
  }
  const openWorkspace = (workspaceId: string) => {
    setRouteError(null)
    setRoute({ view: "workspace", workspaceId })
  }
  const openSession = (workspaceId: string, sessionId: string, tab?: string) =>
    setRoute({ view: "session", workspaceId, sessionId, tab })
  const openTaskFromSession = (workspaceId: string, sourceSession: string) => {
    setRouteError(null)
    setRoute({ view: "workspace", workspaceId, newTaskSourceSession: sourceSession })
  }

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
      <header className="sticky top-0 z-30 border-b border-slate-200 bg-white/85 backdrop-blur">
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
                  <span className="font-mono text-[10.5px] text-slate-400">
                    v{dashboard.docker.version}
                  </span>
                ) : null}
              </div>
            ) : (
              <div className="rounded-md border border-slate-200 px-2 py-1 font-mono text-[10.5px] text-slate-400">
                docker · checking
              </div>
            )}
          </div>
        </div>
      </header>

      {loading && !dashboard ? (
        <main className="mx-auto max-w-[1180px] px-4 py-5 sm:px-6 lg:px-8 lg:py-7">
          <Card className="inline-flex px-3 py-2 text-[13px] text-slate-500">
            Loading workspaces...
          </Card>
        </main>
      ) : null}

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

      {dashboard && dashboardError ? (
        <div className="mx-auto max-w-[1180px] px-4 pt-5 sm:px-6 lg:px-8">
          <Card className="flex flex-col gap-3 border-red-100 bg-red-50/50 px-4 py-3 text-[13px] sm:flex-row sm:items-center sm:justify-between">
            <div>
              <div className="font-semibold text-red-700">Refresh failed</div>
              <div className="mt-0.5 font-mono text-[12px] text-red-600">{dashboardError}</div>
            </div>
            <Button onClick={() => void loadDashboard()} type="button" variant="outline">
              Retry
            </Button>
          </Card>
        </div>
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
          launchQueue={launchQueue}
          onLaunchSetups={() => setLaunchDialogOpen(true)}
          onOpenSession={openSession}
          onOpenWorkspace={openWorkspace}
          onRefresh={() => void loadDashboard()}
          refreshing={loading}
        />
      ) : null}

      {dashboard && route.view === "workspace" ? (
        <WorkspaceRoute
          dashboard={dashboard}
          initialTaskSourceSession={route.newTaskSourceSession}
          onBack={openDashboard}
          onOpenSession={(sessionId, tab) => openSession(route.workspaceId, sessionId, tab)}
          onSubmitTask={submitWorkspaceTask}
          route={route}
          sessionDetails={sessionDetails}
        />
      ) : null}

      {dashboard && route.view === "session" ? (
        <SessionRoute
          detail={sessionDetails[route.sessionId]}
          loading={sessionLoading === route.sessionId}
          onBack={() => openWorkspace(route.workspaceId)}
          onNewTask={(sourceSession) => openTaskFromSession(route.workspaceId, sourceSession)}
          route={route}
        />
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

function WorkspaceRoute({
  dashboard,
  route,
  sessionDetails,
  initialTaskSourceSession,
  onBack,
  onOpenSession,
  onSubmitTask,
}: {
  dashboard: DashboardResponse
  route: Extract<Route, { view: "workspace" }>
  sessionDetails: Record<string, ExecutionSessionDetail>
  initialTaskSourceSession?: string | null
  onBack: () => void
  onOpenSession: (sessionId: string, tab?: string) => void
  onSubmitTask: (
    workspaceId: string,
    task: string,
    sourceSession?: string,
  ) => Promise<SubmitTaskResponse>
}) {
  const workspace = dashboard.workspaces.find((candidate) => candidate.id === route.workspaceId)

  if (!workspace) {
    return (
      <PlaceholderView
        detail="Workspace was not returned by /api/workspaces."
        label="workspace"
        title={route.workspaceId}
      />
    )
  }

  const latest = workspace.latestSession ? sessionDetails[workspace.latestSession] : null
  const sessions = sessionRows(workspace, sessionDetails)

  return (
    <Workspace
      initialTaskSourceSession={initialTaskSourceSession}
      latest={latest}
      onBack={onBack}
      onOpenSession={onOpenSession}
      onSubmitTask={onSubmitTask}
      sessions={sessions}
      workspace={workspace}
    />
  )
}

function SessionRoute({
  route,
  detail,
  loading,
  onBack,
  onNewTask,
}: {
  route: Extract<Route, { view: "session" }>
  detail?: ExecutionSessionDetail
  loading: boolean
  onBack: () => void
  onNewTask: (sourceSession: string) => void
}) {
  if (!detail) {
    return (
      <main className="mx-auto max-w-[1180px] px-4 py-5 sm:px-6 lg:px-8 lg:py-7">
        <Card className="inline-flex px-3 py-2 text-[13px] text-slate-500">
          {loading ? `Loading session ${route.sessionId}...` : `Session ${route.sessionId} unavailable`}
        </Card>
      </main>
    )
  }

  return (
    <SessionDetail
      detail={detail}
      initialTab={route.tab}
      onBack={onBack}
      onNewTask={onNewTask}
    />
  )
}

function sessionRows(
  workspace: WorkspaceSummary,
  sessionDetails: Record<string, ExecutionSessionDetail>,
): WorkspaceSessionRow[] {
  const summaries = workspace.sessions?.length
    ? workspace.sessions
    : fallbackSessionSummaries(workspace)

  return summaries.map((summary) => {
    const detail = sessionDetails[summary.id]

    if (detail) {
      return {
        id: summary.id,
        title: detail.title,
        status: detail.status,
        entry: detail.entry,
        start: detail.start,
        duration: detail.duration,
        build: detail.build,
        test: detail.test,
        evidenceCount: detail.evidence.length,
        filesCount: detail.files?.items.length ?? null,
      }
    }

    return {
      id: summary.id,
      title: summary.title,
      status: summary.status,
      entry: summary.entry,
      start: summary.start,
      duration: summary.duration,
      build: normalizeSummaryBuild(summary.build),
      test: summary.test,
      evidenceCount: summary.evidence,
      filesCount: summary.files,
    }
  })
}

function fallbackSessionSummaries(workspace: WorkspaceSummary): ExecutionSessionSummary[] {
  const ids = [workspace.activeSession, workspace.latestSession].filter(
    (value, index, values): value is string => Boolean(value) && values.indexOf(value) === index,
  )

  return ids.map((id) => ({
    id,
    workspace: workspace.id,
    title: workspace.task,
    status: workspace.activeSession === id ? "active" : "latest",
    entry: "SAG",
    start: workspace.updated,
    finish: null,
    duration: "unknown",
    build: normalizeWorkspaceBuild(workspace.build).state,
    test: workspace.test,
    report: workspace.report,
    files: workspace.changed,
    evidence: 0,
  }))
}

function normalizeSummaryBuild(build: string): BuildSummary {
  return { state: build, tool: "", time: "", note: "" }
}

function normalizeWorkspaceBuild(build: WorkspaceSummary["build"]): BuildSummary {
  if (typeof build === "string") {
    return { state: build, tool: "", time: "", note: "" }
  }

  return build
}

function isLiveSessionStatus(status: string): boolean {
  return ["active", "pending", "queued", "running", "in_progress"].includes(
    status.trim().toLowerCase(),
  )
}

function Breadcrumb({
  route,
  onDashboard,
}: {
  route: Route
  onDashboard: () => void
}) {
  return (
    <nav className="flex min-w-0 items-center gap-2">
      <button
        className={`whitespace-nowrap text-[12.5px] ${
          route.view === "dashboard"
            ? "font-medium text-slate-700"
            : "text-slate-400 hover:text-slate-700"
        }`}
        disabled={route.view === "dashboard"}
        onClick={onDashboard}
        type="button"
      >
        dashboard
      </button>
      {route.view === "workspace" ? (
        <>
          <span className="text-slate-200">/</span>
          <span className="truncate text-[12.5px] font-medium text-slate-700">
            {route.workspaceId}
          </span>
        </>
      ) : null}
      {route.view === "session" ? (
        <>
          <span className="text-slate-200">/</span>
          <span className="max-w-[32vw] truncate text-[12.5px] text-slate-400 sm:max-w-none">
            {route.workspaceId}
          </span>
          <span className="text-slate-200">/</span>
          <span className="truncate text-[12.5px] font-medium text-slate-700">
            {route.sessionId}
          </span>
        </>
      ) : null}
    </nav>
  )
}

function PlaceholderView({
  label,
  title,
  detail,
}: {
  label: string
  title: string
  detail: string
}) {
  return (
    <main className="mx-auto max-w-[1180px] px-4 py-5 sm:px-6 lg:px-8 lg:py-7">
      <Card className="p-5">
        <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-slate-400">
          {label}
        </div>
        <h1 className="mt-1.5 text-[22px] font-semibold tracking-tight text-slate-900">
          {title}
        </h1>
        <p className="mt-1 font-mono text-[12px] text-slate-500">{detail}</p>
      </Card>
    </main>
  )
}
