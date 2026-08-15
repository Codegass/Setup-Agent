import { afterEach, describe, expect, it, vi } from "vitest"

import {
  deleteWorkspace,
  fetchDashboard,
  fetchLaunchQueue,
  fetchSession,
  fetchTrajectory,
  submitProjectBatch,
  submitTask,
} from "./client"

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

  it("encodes session ids in request paths", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ id: "S 1/?" }))

    await fetchSession("S 1/?")

    expect(fetchMock).toHaveBeenCalledWith("/api/sessions/S%201%2F%3F")
  })

  it("asks for a trajectory at the summary tier by default", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ schema_version: 1, turns: [] }))

    await fetchTrajectory("S1")

    expect(fetchMock).toHaveBeenCalledWith("/api/sessions/S1/trajectory?detail=summary")
  })

  it("carries the detail tier and the ledger watermark into the query", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ schema_version: 1, turns: [] }))

    await fetchTrajectory("S 1/?", { detail: "full", sinceSeq: 12 })

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/sessions/S%201%2F%3F/trajectory?detail=full&since_seq=12",
    )
  })

  it("sends since_seq=0 rather than dropping it — zero is a cut, not an absence", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ schema_version: 1, turns: [] }))

    await fetchTrajectory("S1", { sinceSeq: 0 })

    expect(fetchMock).toHaveBeenCalledWith("/api/sessions/S1/trajectory?detail=summary&since_seq=0")
  })

  it("submits a task with the backend source_session field", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        jsonResponse({
          workspace_id: "W1",
          session_id: "S1",
          source_session: "S0",
          status: "created",
        }),
      )

    const result = await submitTask("W1", "do it", "S0")

    expect(fetchMock).toHaveBeenCalledWith("/api/workspaces/W1/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task: "do it", source_session: "S0" }),
    })
    expect(result).toEqual({
      workspace_id: "W1",
      session_id: "S1",
      source_session: "S0",
      status: "created",
    })
  })

  it("encodes workspace ids in task request paths", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        jsonResponse({
          workspace_id: "W 1/#",
          session_id: "S1",
          source_session: null,
          status: "created",
        }),
      )

    await submitTask("W 1/#", "do it")

    expect(fetchMock).toHaveBeenCalledWith("/api/workspaces/W%201%2F%23/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task: "do it", source_session: null }),
    })
  })

  it("throws status and statusText for non-OK responses", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ error: "nope" }, { status: 503, statusText: "Service Unavailable" }),
    )

    await expect(fetchDashboard()).rejects.toThrow("503 Service Unavailable")
  })

  it("carries the server's own words when a refusal states why", async () => {
    // A status line is not a reason. The trajectory endpoint refuses to attribute
    // a session to a run when nothing names which run it is, and names the
    // directories it could not decide between — a fact the reader can act on,
    // and only if it survives the fetch.
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(
        { detail: "cannot be attributed to a run: 2 host session directories ran 'kafka'" },
        { status: 409, statusText: "Conflict" },
      ),
    )

    await expect(fetchTrajectory("S1")).rejects.toThrow(
      "409 Conflict: cannot be attributed to a run: 2 host session directories ran 'kafka'",
    )
  })

  it("submits a project batch and returns the body with http status", async () => {
    const body = {
      batch_id: "BATCH-20260607-abcdef",
      concurrency: 2,
      accepted: [
        {
          launch_id: "LAUNCH-12345678",
          row_index: 0,
          workspace_id: "sag-commons-cli",
          status: "queued",
        },
      ],
      rejected: [],
    }
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse(body, { status: 202 }))

    const result = await submitProjectBatch({
      concurrency: 2,
      projects: [{ repo_url: "https://github.com/apache/commons-cli.git" }],
    })

    expect(fetchMock).toHaveBeenCalledWith("/api/project-launches/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        concurrency: 2,
        projects: [{ repo_url: "https://github.com/apache/commons-cli.git" }],
      }),
    })
    expect(result).toEqual({ status: 202, ...body })
  })

  it("returns conflict batch responses instead of throwing on 409", async () => {
    const body = {
      batch_id: null,
      concurrency: 2,
      accepted: [],
      rejected: [
        {
          row_index: 0,
          workspace_id: "sag-existing",
          status: "conflict",
          message: "Workspace already exists: sag-existing",
        },
      ],
    }
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(body, { status: 409 }),
    )

    const result = await submitProjectBatch({
      projects: [{ repo_url: "https://github.com/x/existing.git" }],
    })

    expect(result).toEqual({ status: 409, ...body })
  })

  it("throws on unexpected batch submit failures", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("boom", { status: 500, statusText: "Internal Server Error" }),
    )

    await expect(
      submitProjectBatch({ projects: [{ repo_url: "x" }] }),
    ).rejects.toThrow("500")
  })

  it("fetches the launch queue", async () => {
    const payload = {
      default_concurrency: 4,
      summary: { queued: 0, launching: 0, running: 0, completed: 0, failed: 0 },
      batches: [],
    }
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse(payload))

    const queue = await fetchLaunchQueue()

    expect(fetchMock).toHaveBeenCalledWith("/api/project-launches")
    expect(queue).toEqual(payload)
  })

  it("deletes a workspace via DELETE and returns the result", async () => {
    const body = {
      workspace_id: "sag-commons-cli",
      container_removed: true,
      queue_items_removed: 2,
      status: "deleted",
    }
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse(body))

    const result = await deleteWorkspace("sag-commons-cli")

    expect(fetchMock).toHaveBeenCalledWith("/api/workspaces/sag-commons-cli", {
      method: "DELETE",
    })
    expect(result).toEqual(body)
  })

  it("rejects with the conflict detail when a setup is in progress", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(
        { detail: "Workspace sag-commons-cli has a setup in progress" },
        { status: 409, statusText: "Conflict" },
      ),
    )

    await expect(deleteWorkspace("sag-commons-cli")).rejects.toThrow(
      "Workspace sag-commons-cli has a setup in progress",
    )
  })

  it("surfaces server validation detail on 422", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(
        { detail: "concurrency must be an integer between 1 and 8" },
        { status: 422, statusText: "Unprocessable Entity" },
      ),
    )

    await expect(
      submitProjectBatch({ projects: [{ repo_url: "x" }] }),
    ).rejects.toThrow("concurrency must be an integer between 1 and 8")
  })
})
