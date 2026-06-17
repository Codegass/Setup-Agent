import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { ExecutionSessionDetail } from "@/api/types"

import { App } from "../App"

vi.mock("@/components/terminal/TerminalPanel", () => ({
  TerminalPanel: ({ workspaceId }: { workspaceId: string }) => (
    <div aria-label="Workspace terminal">Terminal for {workspaceId}</div>
  ),
}))

const dashboard = {
  docker: { status: "connected", version: "27.1.1" },
  workspaces: [
    {
      id: "sag-commons-cli",
      project: "apache/commons-cli",
      container: "sag-commons-cli",
      stack: "Java · Maven",
      docker: { status: "running" },
      task: "Build project and run full test suite",
      build: { state: "success", tool: "Maven", time: "47.2s", note: "" },
      test: { state: "partial", pass: 312, fail: 8, skip: 0, total: 320 },
      report: "ready",
      changed: 7,
      activeSession: "CC-3",
      latestSession: "CC-3",
      updated: "just now",
    },
  ],
}

const jsonResponse = (payload: unknown) =>
  new Response(JSON.stringify(payload), {
    headers: { "Content-Type": "application/json" },
    status: 200,
  })

const sessionDetail: ExecutionSessionDetail = {
  id: "CC-3",
  workspace: "sag-commons-cli",
  title: "Build project and execute full test suite",
  status: "running",
  entry: "CLI",
  start: "02:14:08",
  duration: "running · 2m 11s",
  outcome: "Build succeeds and tests are partial.",
  build: { state: "success", tool: "Maven", time: "47.2s", note: "clean package" },
  test: { state: "partial", pass: 312, fail: 8, skip: 0, total: 320 },
  report: "ready",
  reportDoc: {
    title: "setup-report.md",
    generated: "now",
    blocks: [{ type: "p", text: "Project builds." }],
  },
  evidence: [
    {
      source: "Test validator",
      status: "partial",
      counts: "312 / 320",
      time: "02:16",
      summary: "8 failed",
      records: [],
    },
  ],
  files: null,
  context: null,
  logs: ["BUILD SUCCESS"],
}

const emptyLaunchQueue = {
  default_concurrency: 4,
  summary: { queued: 0, launching: 0, running: 0, completed: 0, failed: 0 },
  batches: [],
}

describe("App", () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it("fetches and renders dashboard data after loading", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) =>
      Promise.resolve(
        String(input) === "/api/project-launches"
          ? jsonResponse(emptyLaunchQueue)
          : jsonResponse(dashboard),
      ),
    )

    render(<App />)

    expect(screen.getByRole("status", { name: /loading workspaces/i })).toBeInTheDocument()
    expect(
      (await screen.findAllByRole("button", { name: /apache\/commons-cli/i })).length,
    ).toBeGreaterThan(0)
    // The docker label lives in the rail header as "docker {version}".
    expect(screen.getByText(/docker 27\.1\.1/i)).toBeInTheDocument()
  })

  it("exposes a workspace-rail toggle that opens and closes the drawer", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input)
      if (url === "/api/project-launches") return Promise.resolve(jsonResponse(emptyLaunchQueue))
      if (url.startsWith("/api/sessions/")) return Promise.resolve(jsonResponse(sessionDetail))
      return Promise.resolve(jsonResponse(dashboard))
    })

    render(<App />)

    const toggle = await screen.findByRole("button", { name: /workspaces menu/i })
    expect(toggle).toHaveAttribute("aria-expanded", "false")
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute("aria-expanded", "true")
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute("aria-expanded", "false")
  })

  it("shows an unavailable state when dashboard fetch fails", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("network down"))

    render(<App />)

    expect(await screen.findByText("Dashboard unavailable")).toBeInTheDocument()
    expect(screen.getByText("Error: network down")).toBeInTheDocument()
  })

  it("keeps stale dashboard data visible when refresh fails", async () => {
    let dashboardCalls = 0
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input)
      if (url === "/api/workspaces") {
        dashboardCalls++
        if (dashboardCalls === 1) return Promise.resolve(jsonResponse(dashboard))
        return Promise.reject(new Error("refresh down"))
      }
      // Auxiliary launch-queue polls are swallowed by the app.
      return Promise.reject(new Error(`unexpected fetch: ${url}`))
    })

    render(<App />)

    expect(
      (await screen.findAllByRole("button", { name: /apache\/commons-cli/i })).length,
    ).toBeGreaterThan(0)

    // The next (silent) poll fails; the rail keeps its stale rows and the footer
    // surfaces a quiet "couldn't refresh" stamp instead of a loud banner. The
    // dashboard poll runs on a 5s interval, so allow more than one cycle.
    expect(await screen.findByText(/couldn't refresh/i, undefined, { timeout: 7000 })).toBeInTheDocument()
    expect(
      screen.getAllByRole("button", { name: /apache\/commons-cli/i }).length,
    ).toBeGreaterThan(0)
    expect(screen.queryByText("Dashboard unavailable")).not.toBeInTheDocument()
  }, 10000)

  it("opens the detail pane, submits a workspace task, and renders facet sections", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input)

      if (url === "/api/workspaces") {
        return Promise.resolve(jsonResponse(dashboard))
      }

      if (url === "/api/sessions/CC-3") {
        return Promise.resolve(jsonResponse(sessionDetail))
      }

      if (url === "/api/workspaces/sag-commons-cli/tasks") {
        return Promise.resolve(
          jsonResponse({
            workspace_id: "sag-commons-cli",
            session_id: "CC-4",
            source_session: null,
            status: "queued",
          }),
        )
      }

      return Promise.reject(new Error(`unexpected fetch: ${url}`))
    })

    render(<App />)

    fireEvent.click(
      (await screen.findAllByRole("button", {
        name: /apache\/commons-cli/i,
      }))[0],
    )

    // Master-detail: header heading + summary band + top pill nav.
    expect(await screen.findByRole("heading", { name: "apache/commons-cli" })).toBeInTheDocument()
    expect(screen.getByRole("navigation", { name: /detail sections/i })).toBeInTheDocument()
    expect(screen.getByText("Build succeeds and tests are partial.")).toBeInTheDocument()
    expect(screen.getByText("Project builds.")).toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: "New task" }))
    expect(screen.getByRole("dialog", { name: /new task/i })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText("Task description"), {
      target: { value: "Fix HelpFormatter line wrapping" },
    })
    fireEvent.click(screen.getByRole("button", { name: /submit task/i }))

    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/workspaces/sag-commons-cli/tasks",
        expect.objectContaining({
          body: JSON.stringify({
            task: "Fix HelpFormatter line wrapping",
            source_session: "CC-3",
          }),
          method: "POST",
        }),
      )
    })
  })

  it("refreshes dashboard after submitting a workspace task", async () => {
    const initialDashboard = {
      ...dashboard,
      workspaces: [
        {
          ...dashboard.workspaces[0],
          task: "Build project and run full test suite",
          activeSession: "CC-3",
          latestSession: "CC-3",
        },
      ],
    }
    const refreshedDashboard = {
      ...dashboard,
      workspaces: [
        {
          ...dashboard.workspaces[0],
          task: "Give me a report of all tests",
          activeSession: "UI-12345678",
          latestSession: "UI-12345678",
          updated: "2026-06-06T21:13:38",
        },
      ],
    }
    const uiSessionDetail: ExecutionSessionDetail = {
      ...sessionDetail,
      id: "UI-12345678",
      title: "Give me a report of all tests",
      status: "running",
      entry: "Web UI",
      outcome: "Task is running.",
    }
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input)

      if (url === "/api/workspaces") {
        const workspaceCalls = fetchSpy.mock.calls.filter(
          ([calledInput]) => String(calledInput) === "/api/workspaces",
        )
        return Promise.resolve(
          jsonResponse(workspaceCalls.length === 1 ? initialDashboard : refreshedDashboard),
        )
      }

      if (url === "/api/workspaces/sag-commons-cli/tasks") {
        return Promise.resolve(
          jsonResponse({
            workspace_id: "sag-commons-cli",
            session_id: "UI-12345678",
            source_session: null,
            status: "queued",
          }),
        )
      }

      if (url === "/api/sessions/CC-3") {
        return Promise.resolve(jsonResponse(sessionDetail))
      }

      if (url === "/api/sessions/UI-12345678") {
        return Promise.resolve(jsonResponse(uiSessionDetail))
      }

      return Promise.reject(new Error(`unexpected fetch: ${url}`))
    })

    render(<App />)

    fireEvent.click(
      (await screen.findAllByRole("button", {
        name: /open workspace apache\/commons-cli/i,
      }))[0],
    )
    fireEvent.click(await screen.findByRole("button", { name: "New task" }))
    fireEvent.change(screen.getByLabelText("Task description"), {
      target: { value: "Give me a report of all tests" },
    })
    fireEvent.click(screen.getByRole("button", { name: /submit task/i }))

    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/workspaces/sag-commons-cli/tasks",
        expect.objectContaining({ method: "POST" }),
      )
    })
    await waitFor(() => {
      const dashboardFetches = fetchSpy.mock.calls.filter(
        ([input]) => String(input) === "/api/workspaces",
      )
      expect(dashboardFetches).toHaveLength(2)
    })

    // The refreshed dashboard moves the latest session to UI-12345678; the
    // detail pane follows the workspace's latest session and renders its outcome.
    expect(await screen.findByText("Task is running.")).toBeInTheDocument()
  })

  it("lists completed setup sessions alongside active workspace tasks", async () => {
    const mixedDashboard = {
      ...dashboard,
      workspaces: [
        {
          ...dashboard.workspaces[0],
          task: "Run formatter tests",
          activeSession: "UI-12345678",
          latestSession: "UI-12345678",
          sessions: [
            {
              id: "SETUP-20260606-213241",
              workspace: "sag-commons-cli",
              title: "Setup and configure the commons-cli project to be runnable",
              status: "completed",
              entry: "CLI",
              start: "2026-06-06T21:32:41",
              finish: "2026-06-06T21:35:09",
              duration: "2m 28s",
              build: "success",
              test: { state: "success", pass: 420, fail: 0, skip: 10, total: 430 },
              report: "ready",
              files: 6,
              evidence: 7,
            },
            {
              id: "UI-12345678",
              workspace: "sag-commons-cli",
              title: "Run formatter tests",
              status: "running",
              entry: "Web UI",
              start: "2026-06-06T21:48:30",
              finish: null,
              duration: "running",
              build: "none",
              test: { state: "none", pass: 0, fail: 0, skip: 0, total: 0 },
              report: "none",
              files: 0,
              evidence: 1,
            },
          ],
        },
      ],
    }
    const uiSessionDetail: ExecutionSessionDetail = {
      ...sessionDetail,
      id: "UI-12345678",
      title: "Run formatter tests",
      status: "running",
      entry: "Web UI",
      outcome: "Task is running.",
    }

    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input)

      if (url === "/api/workspaces") {
        return Promise.resolve(jsonResponse(mixedDashboard))
      }

      if (url === "/api/sessions/UI-12345678") {
        return Promise.resolve(jsonResponse(uiSessionDetail))
      }

      return Promise.reject(new Error(`unexpected fetch: ${url}`))
    })

    render(<App />)

    fireEvent.click(
      (await screen.findAllByRole("button", {
        name: /open workspace apache\/commons-cli/i,
      }))[0],
    )

    // The detail header's session switcher lists every session as a chip.
    expect(await screen.findByRole("button", { name: /SETUP-20260606-213241/ })).toBeInTheDocument()
    expect(screen.getByText("Setup and configure the commons-cli project to be runnable")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /UI-12345678/ })).toBeInTheDocument()
    expect(screen.getByText("Run formatter tests")).toBeInTheDocument()
  })

  it("polls open running session details and renders fresh status", async () => {
    const updatedSessionDetail: ExecutionSessionDetail = {
      ...sessionDetail,
      status: "completed",
      duration: "2m 45s",
      outcome: "Setup completed after polling.",
      test: { state: "success", pass: 430, fail: 0, skip: 0, total: 430 },
    }
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input)

      if (url === "/api/workspaces") {
        return Promise.resolve(jsonResponse(dashboard))
      }

      if (url === "/api/sessions/CC-3") {
        const sessionFetches = fetchSpy.mock.calls.filter(
          ([calledInput]) => String(calledInput) === "/api/sessions/CC-3",
        )
        // First fetch is the mount load (running); the first poll returns the
        // completed detail. The merged detail pane fetches once on open, so the
        // running snapshot is served only to the initial request.
        return Promise.resolve(
          jsonResponse(sessionFetches.length <= 1 ? sessionDetail : updatedSessionDetail),
        )
      }

      return Promise.reject(new Error(`unexpected fetch: ${url}`))
    })

    render(<App />)

    fireEvent.click(
      (await screen.findAllByRole("button", {
        name: /open workspace apache\/commons-cli/i,
      }))[0],
    )

    expect(await screen.findByText("Build succeeds and tests are partial.")).toBeInTheDocument()

    await new Promise((resolve) => setTimeout(resolve, 3200))

    expect(await screen.findByText("Setup completed after polling.")).toBeInTheDocument()
    // The Test facet's tiles reflect the freshly polled 430/430 totals.
    expect(screen.getAllByText("430").length).toBeGreaterThanOrEqual(2)
  }, 8000)

  it("does not mount the terminal panel when the workspace container is not running", async () => {
    const stoppedDashboard = {
      ...dashboard,
      workspaces: [
        {
          ...dashboard.workspaces[0],
          docker: { status: "exited" },
        },
      ],
    }
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input)

      if (url === "/api/project-launches") {
        return Promise.resolve(jsonResponse(emptyLaunchQueue))
      }

      if (url === "/api/sessions/CC-3") {
        return Promise.resolve(jsonResponse(sessionDetail))
      }

      return Promise.resolve(jsonResponse(stoppedDashboard))
    })

    render(<App />)

    fireEvent.click(
      (await screen.findAllByRole("button", {
        name: /open workspace apache\/commons-cli/i,
      }))[0],
    )
    fireEvent.click(await screen.findByRole("button", { name: "Terminal" }))

    expect(
      screen.getByText("Start the workspace container before opening an interactive shell."),
    ).toBeInTheDocument()
    expect(screen.queryByLabelText("Workspace terminal")).not.toBeInTheDocument()
  })

  it("deletes a workspace from the detail pane and refetches", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input)
      const method = (init?.method ?? "GET").toUpperCase()

      if (url === "/api/workspaces" && method === "GET") {
        return Promise.resolve(jsonResponse(dashboard))
      }

      if (url === "/api/sessions/CC-3") {
        return Promise.resolve(jsonResponse(sessionDetail))
      }

      if (url === "/api/workspaces/sag-commons-cli" && method === "DELETE") {
        return Promise.resolve(
          jsonResponse({
            workspace_id: "sag-commons-cli",
            container_removed: true,
            queue_items_removed: 1,
            status: "deleted",
          }),
        )
      }

      if (url === "/api/project-launches") {
        return Promise.resolve(jsonResponse(emptyLaunchQueue))
      }

      return Promise.reject(new Error(`unexpected fetch: ${method} ${url}`))
    })

    render(<App />)

    // The rail auto-selects the only workspace; delete from the detail header.
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }))

    fireEvent.click(screen.getByRole("button", { name: "Delete workspace" }))

    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith("/api/workspaces/sag-commons-cli", {
        method: "DELETE",
      })
    })

    await waitFor(() => {
      const dashboardGets = fetchSpy.mock.calls.filter(
        ([calledInput, calledInit]) =>
          String(calledInput) === "/api/workspaces" &&
          (calledInit?.method ?? "GET").toUpperCase() === "GET",
      )
      expect(dashboardGets.length).toBeGreaterThanOrEqual(2)
    })
  })
})
