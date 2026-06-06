import { useCallback, useEffect, useState } from "react"

import { fetchDashboard } from "@/api/client"
import type { DashboardResponse } from "@/api/types"
import { StatusBadge } from "@/components/common/Badge"
import { Button } from "@/components/common/Button"
import { Card } from "@/components/common/Card"
import { Dashboard } from "@/pages/Dashboard"

type Route =
  | { view: "dashboard" }
  | { view: "workspace"; workspaceId: string }
  | { view: "session"; workspaceId: string; sessionId: string; tab?: string }

export function App() {
  const [route, setRoute] = useState<Route>({ view: "dashboard" })
  const [dashboard, setDashboard] = useState<DashboardResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const loadDashboard = useCallback(async () => {
    setLoading(true)
    setError(null)

    try {
      const nextDashboard = await fetchDashboard()
      setDashboard(nextDashboard)
    } catch (err) {
      setError(String(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadDashboard()
  }, [loadDashboard])

  const openDashboard = () => setRoute({ view: "dashboard" })
  const openWorkspace = (workspaceId: string) => setRoute({ view: "workspace", workspaceId })
  const openSession = (workspaceId: string, sessionId: string, tab?: string) =>
    setRoute({ view: "session", workspaceId, sessionId, tab })

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

      {!loading && error ? (
        <main className="mx-auto max-w-[1180px] px-4 py-5 sm:px-6 lg:px-8 lg:py-7">
          <Card className="max-w-xl p-5">
            <div className="text-[15px] font-semibold text-slate-900">Dashboard unavailable</div>
            <div className="mt-2 font-mono text-[12px] text-red-600">{error}</div>
            <Button className="mt-4" onClick={loadDashboard} type="button" variant="outline">
              Retry
            </Button>
          </Card>
        </main>
      ) : null}

      {!error && dashboard && route.view === "dashboard" ? (
        <Dashboard
          data={dashboard}
          onOpenSession={openSession}
          onOpenWorkspace={openWorkspace}
          onRefresh={loadDashboard}
          refreshing={loading}
        />
      ) : null}

      {!error && dashboard && route.view === "workspace" ? (
        <PlaceholderView
          label="workspace"
          title={route.workspaceId}
          detail="Workspace shell"
        />
      ) : null}

      {!error && dashboard && route.view === "session" ? (
        <PlaceholderView
          label={route.tab ?? "session"}
          title={route.sessionId}
          detail={route.workspaceId}
        />
      ) : null}
    </div>
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
