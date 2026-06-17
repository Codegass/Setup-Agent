import { describe, expect, it } from "vitest"

import type { ExecutionSessionDetail } from "@/api/types"

import { buildDetailFacets } from "./facets"

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
