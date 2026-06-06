import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

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
})
