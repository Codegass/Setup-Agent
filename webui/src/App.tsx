import { useCallback, useEffect, useState } from "react"

import { fetchDashboard, fetchSession, submitTask } from "@/api/client"
import type {
  BuildSummary,
  DashboardResponse,
  ExecutionSessionDetail,
  ExecutionSessionSummary,
  SubmitTaskResponse,
  WorkspaceSummary,
} from "@/api/types"
import { StatusBadge } from "@/components/common/Badge"
import { Button } from "@/components/common/Button"
import { Card } from "@/components/common/Card"
import { Dashboard } from "@/pages/Dashboard"
import { SessionDetail } from "@/pages/SessionDetail"
import { Workspace, type WorkspaceSessionRow } from "@/pages/Workspace"

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

  const loadDashboard = useCallback(async () => {
    setLoading(true)
    setDashboardError(null)

    try {
      const nextDashboard = await fetchDashboard()
      setDashboard(nextDashboard)
    } catch (err) {
      setDashboardError(String(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadDashboard()
  }, [loadDashboard])

  const ensureSessionDetail = useCallback(async (sessionId: string) => {
    setSessionLoading(sessionId)
    setRouteError(null)

    try {
      const detail = await fetchSession(sessionId)
      setSessionDetails((current) => ({ ...current, [sessionId]: detail }))
    } catch (err) {
      setRouteError(String(err))
    } finally {
      setSessionLoading((current) => (current === sessionId ? null : current))
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
            <Button className="mt-4" onClick={loadDashboard} type="button" variant="outline">
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
            <Button onClick={loadDashboard} type="button" variant="outline">
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

      {dashboard && route.view === "dashboard" ? (
        <Dashboard
          data={dashboard}
          onOpenSession={openSession}
          onOpenWorkspace={openWorkspace}
          onRefresh={loadDashboard}
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
