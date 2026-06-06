import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { ExecutionSessionDetail } from "@/api/types"

import { App } from "../App"

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

describe("App", () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it("fetches and renders dashboard data after loading", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse(dashboard))

    render(<App />)

    expect(screen.getByText("Loading workspaces...")).toBeInTheDocument()
    expect(await screen.findAllByText("apache/commons-cli")).not.toHaveLength(0)
    expect(screen.getByText("docker · connected")).toBeInTheDocument()
  })

  it("shows an unavailable state when dashboard fetch fails", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("network down"))

    render(<App />)

    expect(await screen.findByText("Dashboard unavailable")).toBeInTheDocument()
    expect(screen.getByText("Error: network down")).toBeInTheDocument()
  })

  it("keeps stale dashboard data visible when refresh fails", async () => {
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse(dashboard))
      .mockRejectedValueOnce(new Error("refresh down"))

    render(<App />)

    expect(await screen.findAllByText("apache/commons-cli")).not.toHaveLength(0)

    fireEvent.click(screen.getByRole("button", { name: "Refresh dashboard" }))

    expect(await screen.findByText("Refresh failed")).toBeInTheDocument()
    expect(screen.getByText("Error: refresh down")).toBeInTheDocument()
    expect(screen.getAllByText("apache/commons-cli")).not.toHaveLength(0)
    expect(screen.queryByText("Dashboard unavailable")).not.toBeInTheDocument()
  })

  it("opens workspace overview, submits a workspace task, and fetches session detail", async () => {
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
        name: /open workspace apache\/commons-cli/i,
      }))[0],
    )

    expect(await screen.findByRole("heading", { name: "apache/commons-cli" })).toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: "Shell" }))
    expect(screen.getByText("Independent workspace shell")).toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: /^new task$/i }))
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
            source_session: null,
          }),
          method: "POST",
        }),
      )
    })

    fireEvent.click(screen.getByRole("button", { name: "Overview" }))
    fireEvent.click(screen.getByRole("button", { name: /open session detail/i }))

    expect(await screen.findByText("Build project and execute full test suite")).toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: "Report" }))

    expect(screen.getByText("Project builds.")).toBeInTheDocument()
  })
})
