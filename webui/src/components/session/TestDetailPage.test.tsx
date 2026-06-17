import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { TestDetailPage } from "./TestDetailPage"

const detail: any = {
  test: { state: "partial", pass: 3838, fail: 3, skip: 0, total: 3841, passRate: 99.9,
          uniqueTotal: 2907 },
  moduleSummary: { modulesTotal: 24, modulesBuilt: 21, modulesFailed: 1, modulesSkipped: 2,
                   modulesWithTestFailures: 2, buildSystems: ["maven"], singleModule: false },
  modules: [
    { name: "streams", path: "streams", buildStatus: "success", buildSource: "reactor",
      testSource: "runner_xml", testsPassed: 1238, testsFailed: 2, testsSkipped: 0,
      failingNames: ["a.StreamTest.shouldX", "b.StateTest.shouldY"], failingCount: 2 },
  ],
}

afterEach(() => cleanup())

describe("TestDetailPage", () => {
  it("renders tiles, a back button, and the per-module table", () => {
    const onBack = vi.fn()
    render(<TestDetailPage detail={detail} onBack={onBack} />)
    expect(screen.getByText("3,841")).toBeInTheDocument()      // runner exec tile
    expect(screen.getByText(/modules w\/ fails/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: /back/i }))
    expect(onBack).toHaveBeenCalled()
    fireEvent.click(screen.getByRole("button", { name: /view 2 failures/i }))
    expect(screen.getByText("a.StreamTest.shouldX")).toBeInTheDocument()
  })

  it("shows a single-module note when not multi-module", () => {
    render(<TestDetailPage onBack={() => {}} detail={{
      test: detail.test,
      moduleSummary: { ...detail.moduleSummary, modulesTotal: 1, singleModule: true },
      modules: [],
    } as any} />)
    expect(screen.getByText(/single module/i)).toBeInTheDocument()
  })

  it("shows real coverage in the tile when present", () => {
    render(<TestDetailPage onBack={() => {}} detail={{
      test: { state: "success", pass: 100, fail: 0, skip: 0, total: 100, passRate: 100 },
      moduleSummary: { modulesTotal: 2, modulesBuilt: 2, modulesFailed: 0, modulesSkipped: 0,
                       modulesWithTestFailures: 0, buildSystems: ["gradle"], singleModule: false,
                       lineRate: 81, branchRate: 68 },
      modules: [{ name: "core", path: "core", buildStatus: "success", buildSource: "reactor",
                  testSource: "runner_xml", lineRate: 81, branchRate: 68 }],
    } as any} />)
    // "81%" also appears in the ModuleTable coverage bar; the tile-specific
    // "68% branch" string is unique to the Coverage tile.
    expect(screen.getAllByText("81%").length).toBeGreaterThan(0)
    expect(screen.getByText(/68% branch/i)).toBeInTheDocument()
  })

  it("headlines branch coverage when only branch is present (no red 'line —')", () => {
    render(<TestDetailPage onBack={() => {}} detail={{
      test: { state: "success", pass: 100, fail: 0, skip: 0, total: 100, passRate: 100 },
      moduleSummary: { modulesTotal: 1, modulesBuilt: 1, modulesFailed: 0, modulesSkipped: 0,
                       modulesWithTestFailures: 0, buildSystems: ["maven"], singleModule: false,
                       branchRate: 88, coverageSource: "jacoco-existing" },
      modules: [{ name: "core", path: "core", buildStatus: "success", buildSource: "reactor",
                  testSource: "runner_xml", branchRate: 88 }],
    } as any} />)
    expect(screen.getByText(/coverage · branch/i)).toBeInTheDocument()
    expect(screen.getAllByText("88%").length).toBeGreaterThan(0)
  })

  it("shows coverage unavailable when no coverage data", () => {
    render(<TestDetailPage onBack={() => {}} detail={{
      test: { state: "success", pass: 100, fail: 0, skip: 0, total: 100, passRate: 100 },
      moduleSummary: { modulesTotal: 1, modulesBuilt: 1, modulesFailed: 0, modulesSkipped: 0,
                       modulesWithTestFailures: 0, buildSystems: ["maven"], singleModule: false },
      modules: [{ name: "core", path: "core", buildStatus: "success", buildSource: "reactor",
                  testSource: "runner_xml" }],
    } as any} />)
    // The dashed Coverage tile renders "— not measured" (the ModuleTable also
    // shows it for the uncovered module), so assert at least one is present.
    expect(screen.getAllByText(/not measured/i).length).toBeGreaterThan(0)
  })

  it("omits the back button when onBack is not provided (embedded mode)", () => {
    render(<TestDetailPage detail={detail} />)
    expect(screen.queryByRole("button", { name: /back/i })).not.toBeInTheDocument()
  })
})
