import type {
  DashboardResponse,
  DeleteWorkspaceResult,
  ExecutionSessionDetail,
  LaunchBatchRequestBody,
  LaunchBatchResponse,
  LaunchBatchResult,
  LaunchQueueState,
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

// --- phase history + context journal (spec §8.3) ---------------------------

export interface PhaseSummary {
  name: string
  status: string
  notes: string
  key_results: string
}

export interface PhaseJournalRecord {
  iteration: number
  total_chars: number
  delta?: { added?: number; compacted?: number }
  intro_text?: string | null
  ledger_text?: string | null
  step_span?: number | null
}

export async function fetchPhases(workspaceId: string): Promise<PhaseSummary[]> {
  const body = await getJson<{ phases: PhaseSummary[] }>(
    `/api/workspaces/${encodeURIComponent(workspaceId)}/phases`,
  )
  return body.phases
}

export async function fetchPhaseJournal(
  workspaceId: string,
  phase: string,
): Promise<PhaseJournalRecord[]> {
  const body = await getJson<{ records: PhaseJournalRecord[] }>(
    `/api/workspaces/${encodeURIComponent(workspaceId)}/phases/${encodeURIComponent(phase)}/journal`,
  )
  return body.records
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
