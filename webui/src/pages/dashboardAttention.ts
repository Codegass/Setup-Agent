import type { WorkspaceSummary } from "@/api/types"

function normalize(value: string | null | undefined): string {
  return value?.trim().toLowerCase() ?? ""
}

function buildState(build: WorkspaceSummary["build"]): string {
  return normalize(typeof build === "string" ? build : build.state)
}

/** A workspace needs attention if its build failed, tests failed, or its container stopped unexpectedly. */
export function needsAttention(workspace: WorkspaceSummary): boolean {
  const build = buildState(workspace.build)
  const test = normalize(workspace.test.state)
  const docker = normalize(workspace.docker.status)

  const buildFailed = build === "failure" || build === "failed"
  const testFailed =
    test === "fail" ||
    test === "failed" ||
    (test === "partial" && workspace.test.fail > 0)
  // Any container that isn't running or freshly created has stopped unexpectedly.
  const containerDown = docker !== "" && docker !== "running" && docker !== "created"

  return buildFailed || testFailed || containerDown
}

/** Stable sort: attention-needing workspaces first, original order preserved within each group. */
export function sortByAttentionFirst(workspaces: WorkspaceSummary[]): WorkspaceSummary[] {
  return [...workspaces].sort(
    (a, b) => Number(needsAttention(b)) - Number(needsAttention(a)),
  )
}
