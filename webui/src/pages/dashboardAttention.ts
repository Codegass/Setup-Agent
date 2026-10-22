import type { WorkspaceSummary } from "@/api/types"

function normalize(value: string | null | undefined): string {
  return value?.trim().toLowerCase() ?? ""
}

function buildState(build: WorkspaceSummary["build"]): string {
  return normalize(typeof build === "string" ? build : build.state)
}

/** The CI command words that are a finding about this run: the project's own
 *  pinned command failed, or never finished. The Official CI cell is not read
 *  for attention: since the 2026-10-22 card it says only whether a comparison
 *  could be made ("compared" / "not compared"), and how this run fell short of
 *  the official CI run is stated on the card's Official CI row. */
const COMMAND_ATTENTION = new Set(["failed", "did not finish"])

/** A workspace needs attention if its run failed, its pinned CI command failed
 *  or did not finish, a required task step did not finish, the run recorded a
 *  failing or erroring test, its build failed, its legacy test facet failed,
 *  or its container stopped unexpectedly. */
export function needsAttention(workspace: WorkspaceSummary): boolean {
  const build = buildState(workspace.build)
  const test = normalize(workspace.test.state)
  const docker = normalize(workspace.docker.status)
  const result = workspace.result

  const buildFailed = build === "failure" || build === "failed"
  const testFailed =
    test === "fail" ||
    test === "failed" ||
    (test === "partial" && workspace.test.fail > 0)
  // Any container that isn't running or freshly created has stopped unexpectedly.
  const containerDown = docker !== "" && docker !== "running" && docker !== "created"

  // Read off the run's own card. A cell the run did not measure is absent, and
  // absence never flags: only a word the record actually wrote does.
  const runFailed = result?.verdict === "failed"
  const taskUnfinished = normalize(result?.task?.status) === "incomplete"
  const commandUnpassed = COMMAND_ATTENTION.has(normalize(result?.ciCommand?.status))
  // A count, not a word. The legacy `test.state` above says "success" on a run
  // that recorded a failing test — `sag-rocketmq` serves `state: "success",
  // fail: 1` — so 29 archived runs carried red tests while being neither
  // failed nor task-incomplete, and nothing told a reader to look at them.
  // The rail already prints the red `+N`; this is what flags the row beside it.
  const redTests = (result?.tests?.failed ?? 0) + (result?.tests?.errors ?? 0) > 0

  return (
    runFailed ||
    taskUnfinished ||
    commandUnpassed ||
    redTests ||
    buildFailed ||
    testFailed ||
    containerDown
  )
}

/** Stable sort: attention-needing workspaces first, original order preserved within each group. */
export function sortByAttentionFirst(workspaces: WorkspaceSummary[]): WorkspaceSummary[] {
  return [...workspaces].sort(
    (a, b) => Number(needsAttention(b)) - Number(needsAttention(a)),
  )
}
