import type { WorkspaceSummary } from "@/api/types"

function normalize(value: string | null | undefined): string {
  return value?.trim().toLowerCase() ?? ""
}

function buildState(build: WorkspaceSummary["build"]): string {
  return normalize(typeof build === "string" ? build : build.state)
}

/** The Official CI words that are a finding about this run.
 *
 *  Only one. `invalid` — a comparison that reached a CI job and produced no
 *  score — now spells itself "not scored" rather than sharing "not compared"
 *  with the runs that had no CI job at all, so the rail cell CAN tell the two
 *  apart. Whether "not scored" belongs in this set is a separate decision and
 *  has not been made: 135 of the 342 evaluated comparisons under `logs/` are
 *  invalid, so adding it would flag a third of the fleet, and the result
 *  band's Official CI row already carries the row's own tone for it.
 *
 *  `partial` is deliberately absent: it is a weaker match, not a failure, and
 *  the row already prints the word "partial" where a reader can see it.
 */
const CI_ATTENTION = new Set(["not met"])

/** A workspace needs attention if its run failed, a required task step did not
 *  finish, its Official CI comparison was not met, the run recorded a failing
 *  or erroring test, its build failed, its legacy test facet failed, or its
 *  container stopped unexpectedly. */
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
  const ciFinding = CI_ATTENTION.has(normalize(result?.ci?.status))
  // A count, not a word. The legacy `test.state` above says "success" on a run
  // that recorded a failing test — `sag-rocketmq` serves `state: "success",
  // fail: 1` — so 29 archived runs carried red tests while being neither
  // failed nor task-incomplete, and nothing told a reader to look at them.
  // The rail already prints the red `+N`; this is what flags the row beside it.
  const redTests = (result?.tests?.failed ?? 0) + (result?.tests?.errors ?? 0) > 0

  return (
    runFailed ||
    taskUnfinished ||
    ciFinding ||
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
