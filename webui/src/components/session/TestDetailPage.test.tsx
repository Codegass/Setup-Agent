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
})
