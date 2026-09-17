import { describe, expect, it } from "vitest"

import type { CIComparison, ContextTrace, ExecutionSessionDetail } from "@/api/types"

import { buildDetailTabs } from "./facets"

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

const comparison: CIComparison = {
  schema_version: 1,
  status: "evaluated",
  run_id: "CC-1",
  attainment: null,
  receipt_ids: [],
  commands: [],
  reasons: [],
}

/** A run that produced every panel this pane can offer. */
function detailWithEverything(
  overrides: Partial<ExecutionSessionDetail> = {},
): ExecutionSessionDetail {
  return detail({
    ciComparison: comparison,
    evidence: [
      { source: "maven", summary: "", counts: "", time: "", status: "pass", records: [] },
    ],
    logs: ["BUILD SUCCESS"],
    reportDoc: { title: "r", generated: "now", blocks: [] },
    ...overrides,
  })
}

const ctx: ContextTrace = {
  trunk: { goal: "Set up acme", state: "completed", progress: {}, summary: "" },
  phases: [],
  debug: {},
}

describe("buildDetailTabs", () => {
  it("offers the tabs in reading order", () => {
    expect(buildDetailTabs(detailWithEverything()).map((tab) => tab.id)).toEqual([
      "overview",
      "trajectory",
      "tests",
      "build",
      "ci",
      "evidence",
      "logs",
      "report",
    ])
  })

  it("names each tab in words a first-time reader can follow", () => {
    expect(buildDetailTabs(detailWithEverything()).map((tab) => tab.label)).toEqual([
      "Overview",
      "Trajectory",
      "Tests",
      "Build",
      "Official CI",
      "Evidence",
      "Logs",
      "Report",
    ])
  })

  it("hides the official CI tab only when the run served no comparison at all", () => {
    expect(
      buildDetailTabs(detailWithEverything({ ciComparison: null })).map((tab) => tab.id),
    ).not.toContain("ci")
  })

  it("keeps the official CI tab for a comparison that matched no cell", () => {
    // 47 of the recorded runs served exactly this: a comparison whose status
    // says nothing was measured. The tab is where that sentence is said, so
    // hiding it would leave the result band's CI row pointing nowhere.
    const tabs = buildDetailTabs(
      detailWithEverything({
        ciComparison: { ...comparison, status: "no_matched_cell", attainment: null },
      }),
    ).map((tab) => tab.id)
    expect(tabs).toContain("ci")
  })

  it("counts test errors as well as failures on the tests badge", () => {
    const tabs = buildDetailTabs(
      detail({ test: { state: "partial", pass: 10, fail: 2, errors: 3, skip: 0, total: 15 } }),
    )
    expect(tabs.find((tab) => tab.id === "tests")?.count).toBe(5)
  })

  it("counts the receipts on the evidence tab when no evidence group was built", () => {
    const tabs = buildDetailTabs(
      detailWithEverything({
        evidence: [],
        receipts: [
          { receiptId: "r1", tool: "maven", argv: "mvn -q verify", outcome: "ok", reportsNew: 0, reportsChanged: 0 },
          { receiptId: "r2", tool: "maven", argv: "mvn -q test", outcome: "ok", reportsNew: 0, reportsChanged: 0 },
        ],
      }),
    )
    const evidence = tabs.find((tab) => tab.id === "evidence")
    expect(evidence?.count).toBe(2)
    expect(evidence?.tone).toBe("neutral")
  })

  it("puts overview first, then the run itself", () => {
    const tabs = buildDetailTabs(detail())
    expect(tabs[0].id).toBe("overview")
    expect(tabs.map((t) => t.id).slice(0, 2)).toEqual(["overview", "trajectory"])
  })

  it("keeps the trajectory tab for every session that ran", () => {
    // It does not wait on a context trace: the trajectory is derived from the
    // control ledger, which every run writes.
    expect(buildDetailTabs(detail({ context: ctx })).map((t) => t.id)).toContain("trajectory")
  })

  it("omits the trajectory for a demo session, which never ran and wrote no ledger", () => {
    // `sag ui --demo` fabricates every read model; there is no session
    // directory behind one, which is why the builder refuses to name one. The
    // tab offered a reader a panel that could only ever say "unavailable".
    const tabs = buildDetailTabs(detail({ demo: true })).map((t) => t.id)
    expect(tabs).not.toContain("trajectory")
    expect(tabs[0]).toBe("overview")
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
    expect(tabs).not.toContain("ci")
    expect(tabs).not.toContain("evidence")
    expect(tabs).not.toContain("logs")
    expect(tabs).not.toContain("report")
  })

  it("includes evidence / logs / report tabs when their data is present", () => {
    const tabs = buildDetailTabs(
      detail({
        evidence: [{ source: "maven", summary: "", counts: "", time: "", status: "pass", records: [] }],
        logs: ["line"],
        reportDoc: { title: "r", generated: "now", blocks: [] },
      }),
    ).map((t) => t.id)
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
