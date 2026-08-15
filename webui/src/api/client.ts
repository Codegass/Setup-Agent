import type {
  DashboardResponse,
  DeleteWorkspaceResult,
  ExecutionSessionDetail,
  LaunchBatchRequestBody,
  LaunchBatchResponse,
  LaunchBatchResult,
  LaunchQueueState,
  SubmitTaskResponse,
  SystemSummary,
  TrajectoryDetail,
  TrajectoryDocument,
} from "./types"

async function readJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`)
  }

  return response.json() as Promise<T>
}

async function getJson<T>(path: string): Promise<T> {
  return readJson<T>(await fetch(path))
}

export function fetchDashboard(): Promise<DashboardResponse> {
  return getJson<DashboardResponse>("/api/workspaces")
}

export function fetchSystem(): Promise<SystemSummary> {
  return getJson<SystemSummary>("/api/system")
}

export function fetchSession(sessionId: string): Promise<ExecutionSessionDetail> {
  return getJson<ExecutionSessionDetail>(
    `/api/sessions/${encodeURIComponent(sessionId)}`,
  )
}

/**
 * One session's trajectory-v1 document.
 *
 * `since` is the poller's cut and it cuts TURNS ONLY: the response still
 * restates `session`, `phases`, `annotations` and `warnings` whole, because a
 * warning can be withdrawn and an annotation can land on a turn far below the
 * cut, and a cut has no channel for a retraction. Consumers upsert turns by id
 * and REPLACE the rest — see `mergeTrajectory`.
 *
 * `detail=full` additionally resolves every ref to its bytes; it is not the
 * tier to poll with, since `outputs` is whole on every response.
 */
export function fetchTrajectory(
  sessionId: string,
  options?: { detail?: TrajectoryDetail; since?: number },
): Promise<TrajectoryDocument> {
  const query = new URLSearchParams({ detail: options?.detail ?? "summary" })
  // `since=0` is a real cut (drop nothing, and say so), not a missing option.
  if (options?.since != null) {
    query.set("since", String(options.since))
  }

  return getJson<TrajectoryDocument>(
    `/api/sessions/${encodeURIComponent(sessionId)}/trajectory?${query.toString()}`,
  )
}

export async function submitTask(
  workspaceId: string,
  task: string,
  sourceSession?: string,
): Promise<SubmitTaskResponse> {
  return readJson(
    await fetch(`/api/workspaces/${encodeURIComponent(workspaceId)}/tasks`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task, source_session: sourceSession ?? null }),
    }),
  )
}

export async function deleteWorkspace(
  workspaceId: string,
): Promise<DeleteWorkspaceResult> {
  const response = await fetch(
    `/api/workspaces/${encodeURIComponent(workspaceId)}`,
    { method: "DELETE" },
  )

  if (response.ok) {
    return (await response.json()) as DeleteWorkspaceResult
  }

  let detail = ""
  try {
    const body = (await response.json()) as { detail?: unknown }
    if (typeof body.detail === "string") {
      detail = body.detail
    }
  } catch {
    // Non-JSON error body; fall back to the status line.
  }
  throw new Error(detail || `${response.status} ${response.statusText}`)
}

export function fetchLaunchQueue(): Promise<LaunchQueueState> {
  return getJson<LaunchQueueState>("/api/project-launches")
}

export async function submitProjectBatch(
  payload: LaunchBatchRequestBody,
): Promise<LaunchBatchResult> {
  const response = await fetch("/api/project-launches/batch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })

  if (response.status === 202 || response.status === 409) {
    const body = (await response.json()) as LaunchBatchResponse
    return { ...body, status: response.status }
  }

  let detail = ""
  try {
    const body = (await response.json()) as { detail?: unknown }
    if (typeof body.detail === "string") {
      detail = body.detail
    }
  } catch {
    // Non-JSON error body; fall back to the status line.
  }
  throw new Error(detail || `${response.status} ${response.statusText}`)
}
