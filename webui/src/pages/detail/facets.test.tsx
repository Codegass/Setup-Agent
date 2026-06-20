import { describe, expect, it } from "vitest"

import type { ContextTrace, ExecutionSessionDetail } from "@/api/types"

import { buildDetailFacets, buildDetailTabs } from "./facets"

function detail(overrides: Partial<ExecutionSessionDetail> = {}): ExecutionSessionDetail {
  return {
    id: "CC-1",
    workspace: "sag-x",
    title: "Build and test",
    status: "completed",
    entry: "SAG",
    start: "now",
    duration: "1m",
    outcome: "Done.",
    build: { state: "success", tool: "Maven", time: "1s", note: "" },
    test: { state: "pass", pass: 10, fail: 0, skip: 0, total: 10 },
    report: "ready",
    evidence: [],
    logs: [],
    ...overrides,
  }
}

const ctx: ContextTrace = {
  trunk: { goal: "Set up acme", state: "completed", progress: {}, summary: "" },
  phases: [],
  debug: {},
}

describe("buildDetailFacets", () => {
  it("returns the seven facets in order", () => {
    const ids = buildDetailFacets(detail()).map((f) => f.id)
    expect(ids).toEqual(["build", "test", "flow", "evidence", "files", "report", "logs"])
  })

  it("surfaces a red test-fail count and evidence/files counts, omitting zero counts", () => {
    const facets = buildDetailFacets(
      detail({
        test: { state: "partial", pass: 8, fail: 2, skip: 0, total: 10 },
        evidence: [{ source: "maven", summary: "", counts: "", time: "", status: "pass", records: [] }],
        files: {
          snapshot: { base: "", head: "", mode: "" },
          counts: { modified: 1, added: 0, deleted: 0, renamed: 0 },
          items: [{ path: "a.java", change: "modified", type: "file", size: "", mtime: "", note: "" }],
        },
      }),
    )
    const byId = Object.fromEntries(facets.map((f) => [f.id, f]))
    expect(byId.test.count).toBe(2)
    expect(byId.test.countTone).toBe("red")
    expect(byId.evidence.count).toBe(1)
    expect(byId.files.count).toBe(1)
    expect(byId.build.count).toBeNull()
  })
})

describe("buildDetailTabs", () => {
  it("puts overview first and includes flow when context is present", () => {
    const tabs = buildDetailTabs(detail({ context: ctx }))
    expect(tabs[0].id).toBe("overview")
    expect(tabs.map((t) => t.id)).toContain("flow")
  })

  it("always includes core tabs and surfaces tests count + red tone when failing", () => {
    const tabs = buildDetailTabs(
      detail({ test: { state: "partial", pass: 8, fail: 2, skip: 0, total: 10 } }),
    )
    const byId = Object.fromEntries(tabs.map((t) => [t.id, t]))
    expect(byId.overview).toBeDefined()
    expect(byId.tests).toBeDefined()
    expect(byId.build).toBeDefined()
    expect(byId.tests.count).toBe(2)
    expect(byId.tests.tone).toBe("red")
  })

  it("omits tabs whose data is absent", () => {
    const tabs = buildDetailTabs(detail()).map((t) => t.id)
    expect(tabs).not.toContain("flow")
    expect(tabs).not.toContain("files")
    expect(tabs).not.toContain("evidence")
    expect(tabs).not.toContain("logs")
    expect(tabs).not.toContain("report")
  })

  it("includes files / evidence / logs / report tabs when their data is present", () => {
    const tabs = buildDetailTabs(
      detail({
        evidence: [{ source: "maven", summary: "", counts: "", time: "", status: "pass", records: [] }],
        files: {
          snapshot: { base: "", head: "", mode: "" },
          counts: { modified: 1, added: 0, deleted: 0, renamed: 0 },
          items: [{ path: "a.java", change: "modified", type: "file", size: "", mtime: "", note: "" }],
        },
        logs: ["line"],
        reportDoc: { title: "r", generated: "now", blocks: [] },
      }),
    ).map((t) => t.id)
    expect(tabs).toContain("files")
    expect(tabs).toContain("evidence")
    expect(tabs).toContain("logs")
    expect(tabs).toContain("report")
  })

  it("attaches a neutral inline count to the evidence tab", () => {
    const tabs = buildDetailTabs(
      detail({
        evidence: [
          { source: "maven", summary: "", counts: "", time: "", status: "pass", records: [] },
          { source: "junit", summary: "", counts: "", time: "", status: "pass", records: [] },
        ],
      }),
    )
    const evidence = tabs.find((t) => t.id === "evidence")
    expect(evidence?.count).toBe(2)
    expect(evidence?.tone).toBe("neutral")
  })
})
