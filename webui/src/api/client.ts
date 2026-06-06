import type {
  DashboardResponse,
  ExecutionSessionDetail,
  SubmitTaskResponse,
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

export function fetchSession(sessionId: string): Promise<ExecutionSessionDetail> {
  return getJson<ExecutionSessionDetail>(
    `/api/sessions/${encodeURIComponent(sessionId)}`,
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
