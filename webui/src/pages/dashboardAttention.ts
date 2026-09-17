import type { WorkspaceSummary } from "@/api/types"

function normalize(value: string | null | undefined): string {
  return value?.trim().toLowerCase() ?? ""
}

function buildState(build: WorkspaceSummary["build"]): string {
  return normalize(typeof build === "string" ? build : build.state)
}

/** The Official CI words that are a finding about this run.
 *
 *  Only one. The card spells `not_met` as "not met" and spells BOTH `invalid`
 *  and "there was nothing to compare" as "not compared" — 269 of the 373
 *  archived runs under `logs/` land on that one word, and only 68 of them are
 *  the failed kind. The rail cell carries the spelled word and nothing else, so
 *  it cannot tell those apart; flagging it would flag most of the fleet. The
 *  result band's Official CI row can tell them apart — it carries the row's
 *  tone — and that is where a reader sees the difference today.
 *
 *  `partial` is deliberately absent: it is a weaker match, not a failure, and
 *  the row already prints the word "partial" where a reader can see it.
 */
const CI_ATTENTION = new Set(["not met"])

/** A workspace needs attention if its run failed, a required task step did not
 *  finish, its Official CI comparison was not met, its build failed, its tests
 *  failed, or its container stopped unexpectedly. */
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

  return (
    runFailed ||
    taskUnfinished ||
    ciFinding ||
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
