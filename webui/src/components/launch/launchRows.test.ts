import { describe, expect, it } from "vitest"

import type { LaunchQueueItem, LaunchQueueState, WorkspaceSummary } from "@/api/types"

import {
  emptyLaunchRow,
  launchProjectName,
  launchStatusLine,
  parsePastedRepoLines,
  pendingLaunchItems,
} from "./launchRows"

function queueItem(overrides: Partial<LaunchQueueItem>): LaunchQueueItem {
  return {
    id: "li-1",
    row_index: 0,
    repo_url: "https://github.com/a/b.git",
    workspace_id: "sag-b",
    ref: null,
    status: "queued",
    pid: null,
    exit_code: null,
    error: null,
    process_log: "",
    ...overrides,
  }
}

function queue(items: LaunchQueueItem[]): LaunchQueueState {
  return {
    default_concurrency: 2,
    summary: { queued: 0, launching: 0, running: 0, completed: 0, failed: 0 },
    batches: [{ id: "batch-1", status: "running", concurrency: 2, created: "now", items }],
  }
}

function ws(overrides: Partial<WorkspaceSummary>): WorkspaceSummary {
  return {
    id: "sag-x",
    project: "owner/x",
    container: "sag-x",
    stack: "Java",
    docker: { status: "running", image: "sag/base" },
    task: "t",
    build: { state: "success", tool: "Maven", time: "1s", note: "" },
    test: { state: "pass", pass: 1, fail: 0, skip: 0, total: 1 },
    report: "ready",
    changed: 0,
    updated: "now",
    ...overrides,
  }
}

describe("emptyLaunchRow", () => {
  it("creates a blank row with record and coverage off", () => {
    expect(emptyLaunchRow()).toEqual({
      repoUrl: "",
      name: "",
      ref: "",
      goal: "",
      record: false,
      coverage: false,
    })
  })
})

describe("parsePastedRepoLines", () => {
  it("parses one repo url per line", () => {
    const parsed = parsePastedRepoLines(
      "https://github.com/apache/commons-cli.git\nhttps://github.com/apache/dubbo.git",
    )

    expect(parsed).toEqual([
      { repoUrl: "https://github.com/apache/commons-cli.git", ref: "" },
      { repoUrl: "https://github.com/apache/dubbo.git", ref: "" },
    ])
  })

  it("parses the quick repo_url ref format", () => {
    const parsed = parsePastedRepoLines(
      "https://github.com/apache/commons-cli.git rel/commons-cli-1.11.0\n" +
        "https://github.com/apache/dubbo.git dubbo-3.2.19",
    )

    expect(parsed).toEqual([
      {
        repoUrl: "https://github.com/apache/commons-cli.git",
        ref: "rel/commons-cli-1.11.0",
      },
      { repoUrl: "https://github.com/apache/dubbo.git", ref: "dubbo-3.2.19" },
    ])
  })

  it("ignores blank lines and trims whitespace", () => {
    const parsed = parsePastedRepoLines(
      "\n  https://github.com/a/b.git   v1.0  \r\n\n",
    )

    expect(parsed).toEqual([{ repoUrl: "https://github.com/a/b.git", ref: "v1.0" }])
  })
})

describe("launchProjectName", () => {
  it("strips the sag- prefix from the workspace id", () => {
    expect(launchProjectName(queueItem({ workspace_id: "sag-commons-cli" }))).toBe("commons-cli")
  })
})

describe("launchStatusLine", () => {
  it("maps queued/launching/failed to human status lines", () => {
    expect(launchStatusLine(queueItem({ status: "queued" }))).toBe(
      "Waiting for a free setup slot",
    )
    expect(launchStatusLine(queueItem({ status: "launching" }))).toBe("Setting up…")
    expect(launchStatusLine(queueItem({ status: "running" }))).toBe("Setting up…")
    expect(launchStatusLine(queueItem({ status: "failed", error: "boom" }))).toBe("boom")
    expect(launchStatusLine(queueItem({ status: "failed", error: null }))).toBe("Setup failed")
  })
})

describe("pendingLaunchItems", () => {
  it("returns an empty list when there is no queue", () => {
    expect(pendingLaunchItems(null, [])).toEqual([])
    expect(pendingLaunchItems(undefined, [])).toEqual([])
  })

  it("drops completed items and items already discovered as workspaces", () => {
    const result = pendingLaunchItems(
      queue([
        queueItem({ id: "a", workspace_id: "sag-done", status: "completed" }),
        queueItem({ id: "b", workspace_id: "sag-live", status: "running" }),
        queueItem({ id: "c", workspace_id: "sag-queued", status: "queued" }),
      ]),
      [ws({ id: "sag-live", project: "owner/live" })],
    )
    expect(result.map((item) => item.id)).toEqual(["c"])
  })

  it("dedupes by workspace id and sorts failed above active above queued", () => {
    const result = pendingLaunchItems(
      queue([
        queueItem({ id: "q", workspace_id: "sag-q", status: "queued" }),
        queueItem({ id: "f", workspace_id: "sag-f", status: "failed" }),
        queueItem({ id: "r", workspace_id: "sag-r", status: "launching" }),
        queueItem({ id: "dup", workspace_id: "sag-q", status: "queued" }),
      ]),
      [],
    )
    expect(result.map((item) => item.id)).toEqual(["f", "r", "q"])
  })
})
