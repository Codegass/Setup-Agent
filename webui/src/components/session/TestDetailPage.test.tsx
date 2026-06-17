import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { TestDetailPage } from "./TestDetailPage"

afterEach(() => cleanup())

const multi = {
  test: { state: "partial", pass: 3838, fail: 3, skip: 0, total: 3841, passRate: 99.9, uniqueTotal: 2907 },
  moduleSummary: {
    modulesTotal: 24, modulesBuilt: 21, modulesFailed: 1, modulesSkipped: 2,
    modulesWithTestFailures: 2, buildSystems: ["maven"], singleModule: false,
  },
  modules: [{
    name: "streams", path: "streams", buildStatus: "success", buildSource: "reactor",
    testSource: "runner_xml", testsPassed: 1238, testsFailed: 2, testsSkipped: 0,
    failingNames: ["a.StreamTest.shouldX", "b.StateTest.shouldY"], failingCount: 2,
  }],
} as any

describe("TestDetailPage (per-module breakdown)", () => {
  it("renders the tiles and the per-module table with failing expand", () => {
    render(<TestDetailPage detail={multi} />)
    expect(screen.getByText(/modules w\/ fails/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: /view 2 failures/i }))
    expect(screen.getByText("a.StreamTest.shouldX")).toBeInTheDocument()
  })

  it("shows real coverage in the tile when present", () => {
    render(<TestDetailPage detail={{
      test: { state: "success", pass: 100, fail: 0, skip: 0, total: 100, passRate: 100 },
      moduleSummary: { modulesTotal: 2, modulesBuilt: 2, modulesFailed: 0, modulesSkipped: 0,
                       modulesWithTestFailures: 0, buildSystems: ["gradle"], singleModule: false,
                       lineRate: 81, branchRate: 68 },
      modules: [{ name: "core", path: "core", buildStatus: "success", buildSource: "reactor",
                  testSource: "runner_xml", lineRate: 81, branchRate: 68 }],
    } as any} />)
    expect(screen.getAllByText("81%").length).toBeGreaterThan(0)
    expect(screen.getByText(/68% branch/i)).toBeInTheDocument()
  })

  it("headlines branch coverage when only branch is present (no red 'line —')", () => {
    render(<TestDetailPage detail={{
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
    render(<TestDetailPage detail={{
      test: { state: "success", pass: 100, fail: 0, skip: 0, total: 100, passRate: 100 },
      moduleSummary: { modulesTotal: 1, modulesBuilt: 1, modulesFailed: 0, modulesSkipped: 0,
                       modulesWithTestFailures: 0, buildSystems: ["maven"], singleModule: false },
      modules: [{ name: "core", path: "core", buildStatus: "success", buildSource: "reactor",
                  testSource: "runner_xml" }],
    } as any} />)
    expect(screen.getAllByText(/not measured/i).length).toBeGreaterThan(0)
  })
})
