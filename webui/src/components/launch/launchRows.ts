import type { LaunchQueueItem, LaunchQueueState, WorkspaceSummary } from "@/api/types"

export interface LaunchRowDraft {
  repoUrl: string
  name: string
  ref: string
  goal: string
  record: boolean
  coverage: boolean
}

export function emptyLaunchRow(): LaunchRowDraft {
  return { repoUrl: "", name: "", ref: "", goal: "", record: false, coverage: false }
}

/**
 * Parse multi-line paste input. Supported quick format per line:
 *   repo_url
 *   repo_url ref
 */
export function parsePastedRepoLines(
  text: string,
): Array<{ repoUrl: string; ref: string }> {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const [repoUrl, ref = ""] = line.split(/\s+/)
      return { repoUrl, ref }
    })
}

function normalizeStatus(status: string | null | undefined): string {
  return status?.trim().toLowerCase() ?? ""
}

/** Human-readable name for a launch-queue entry (drops the `sag-` prefix). */
export function launchProjectName(item: LaunchQueueItem): string {
  return item.workspace_id.replace(/^sag-/, "")
}

/** Short status line shown next to a pending/queued/launching/failed launch. */
export function launchStatusLine(item: LaunchQueueItem): string {
  switch (normalizeStatus(item.status)) {
    case "queued":
      return "Waiting for a free setup slot"
    case "launching":
    case "running":
      return "Setting up…"
    case "failed":
      return item.error || "Setup failed"
    default:
      return item.error || "Setup pending"
  }
}

/**
 * Derive the launch-queue entries that are NOT yet discovered as workspaces:
 * queued / launching / running items that are still materializing, plus failed
 * launches that never produced a workspace. Completed items and items whose
 * workspace_id is already a discovered workspace are dropped (deduped by id).
 *
 * Sorted attention-first: failed above active above queued.
 */
export function pendingLaunchItems(
  launchQueue: LaunchQueueState | null | undefined,
  workspaces: WorkspaceSummary[],
): LaunchQueueItem[] {
  if (!launchQueue) {
    return []
  }
  const discovered = new Set(workspaces.map((workspace) => workspace.id))
  const seen = new Set<string>()
  const pending: LaunchQueueItem[] = []
  for (const batch of launchQueue.batches) {
    for (const item of batch.items) {
      const state = normalizeStatus(item.status)
      if (state === "completed" || discovered.has(item.workspace_id)) {
        continue
      }
      if (seen.has(item.workspace_id)) {
        continue
      }
      seen.add(item.workspace_id)
      pending.push(item)
    }
  }
  // Attention-first: failed launches sort above active, active above queued.
  const rank: Record<string, number> = { failed: 0, running: 1, launching: 2, queued: 3 }
  return pending.sort(
    (a, b) => (rank[normalizeStatus(a.status)] ?? 4) - (rank[normalizeStatus(b.status)] ?? 4),
  )
}
