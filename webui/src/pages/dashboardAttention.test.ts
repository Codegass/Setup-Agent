import { describe, expect, it } from "vitest"

import type { WorkspaceSummary } from "@/api/types"

import { needsAttention, sortByAttentionFirst } from "./dashboardAttention"

function ws(overrides: Partial<WorkspaceSummary>): WorkspaceSummary {
  return {
    id: "sag-x",
    project: "owner/x",
    container: "sag-x",
    stack: "Java · Maven",
    docker: { status: "running", image: "sag/base" },
    task: "Build and test",
    build: { state: "success", tool: "Maven", time: "1s", note: "" },
    test: { state: "pass", pass: 10, fail: 0, skip: 0, total: 10 },
    report: "ready",
    changed: 0,
    updated: "just now",
    ...overrides,
  }
}

describe("needsAttention", () => {
  it("flags a failed build", () => {
    expect(needsAttention(ws({ build: { state: "failure", tool: "", time: "", note: "" } }))).toBe(true)
  })

  it("flags failed tests and a partial run with real failures", () => {
    expect(needsAttention(ws({ test: { state: "fail", pass: 0, fail: 3, skip: 0, total: 3 } }))).toBe(true)
    expect(needsAttention(ws({ test: { state: "partial", pass: 8, fail: 2, skip: 0, total: 10 } }))).toBe(true)
  })

  it("flags a stopped/exited container but not a freshly created one", () => {
    expect(needsAttention(ws({ docker: { status: "exited", image: "sag/base" } }))).toBe(true)
    expect(needsAttention(ws({ docker: { status: "created", image: "sag/base" } }))).toBe(false)
  })

  it("treats a string build value the same as the object form", () => {
    expect(needsAttention(ws({ build: "failure" }))).toBe(true)
    expect(needsAttention(ws({ build: "success" }))).toBe(false)
  })

  it("keeps a healthy workspace quiet", () => {
    expect(needsAttention(ws({}))).toBe(false)
  })
})

describe("sortByAttentionFirst", () => {
  it("moves attention-needing workspaces to the top, preserving order within groups", () => {
    const healthyA = ws({ id: "a" })
    const failing = ws({ id: "b", build: { state: "failure", tool: "", time: "", note: "" } })
    const healthyC = ws({ id: "c" })

    const ordered = sortByAttentionFirst([healthyA, failing, healthyC])

    expect(ordered.map((w) => w.id)).toEqual(["b", "a", "c"])
  })

  it("does not mutate the input array", () => {
    const list = [ws({ id: "a" }), ws({ id: "b", docker: { status: "exited", image: "x" } })]
    sortByAttentionFirst(list)
    expect(list.map((w) => w.id)).toEqual(["a", "b"])
  })
})
