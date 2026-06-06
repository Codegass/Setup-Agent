import { afterEach, describe, expect, it, vi } from "vitest"

import { fetchDashboard, fetchSession, submitTask } from "./client"

const jsonResponse = (payload: unknown, init?: ResponseInit) =>
  new Response(JSON.stringify(payload), {
    headers: { "Content-Type": "application/json" },
    status: 200,
    ...init,
  })

describe("api client", () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("fetches the dashboard from workspaces", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ docker: {}, workspaces: [] }))

    await fetchDashboard()

    expect(fetchMock).toHaveBeenCalledWith("/api/workspaces")
  })

  it("fetches a session by id", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ id: "S1" }))

    await fetchSession("S1")

    expect(fetchMock).toHaveBeenCalledWith("/api/sessions/S1")
  })

  it("submits a task with the backend source_session field", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        jsonResponse({ workspace_id: "W1", session_id: "S1", status: "created" }),
      )

    await submitTask("W1", "do it", "S0")

    expect(fetchMock).toHaveBeenCalledWith("/api/workspaces/W1/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task: "do it", source_session: "S0" }),
    })
  })

  it("throws status and statusText for non-OK responses", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ error: "nope" }, { status: 503, statusText: "Service Unavailable" }),
    )

    await expect(fetchDashboard()).rejects.toThrow("503 Service Unavailable")
  })
})
