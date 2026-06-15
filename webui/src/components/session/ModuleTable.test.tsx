import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import type { ModuleSummary } from "@/api/types"

import { ModuleTable } from "./ModuleTable"

const modules: ModuleSummary[] = [
  { name: "clients", path: "clients", buildStatus: "success", buildSource: "reactor",
    classCount: 3140, jarCount: 22, testsTotal: 1420, testsPassed: 1420, testsFailed: 0,
    testSource: "runner_xml", failingNames: [], failingCount: 0 },
  { name: "streams", path: "streams", buildStatus: "success", buildSource: "reactor",
    classCount: 2610, jarCount: 18, testsTotal: 1240, testsPassed: 1238, testsFailed: 2,
    testSource: "runner_xml",
    failingNames: ["a.StreamTest.shouldX", "b.StateTest.shouldY"], failingCount: 2 },
]

afterEach(() => cleanup())

describe("ModuleTable (test variant)", () => {
  it("orders failures first and expands the full failing list", () => {
    render(<ModuleTable modules={modules} variant="test" />)
    const rows = screen.getAllByRole("row")
    // streams (has failures) precedes clients (none) — failures first
    const text = rows.map((r) => r.textContent).join("|")
    expect(text.indexOf("streams")).toBeLessThan(text.indexOf("clients"))
    // failing names hidden until expand
    expect(screen.queryByText("a.StreamTest.shouldX")).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: /view 2 failures/i }))
    expect(screen.getByText("a.StreamTest.shouldX")).toBeInTheDocument()
    expect(screen.getByText("b.StateTest.shouldY")).toBeInTheDocument()
  })

  it("shows truncation pointer when failingCount exceeds names", () => {
    render(<ModuleTable variant="test" modules={[{
      name: "m", path: "m", buildStatus: "success", buildSource: "reactor",
      testSource: "runner_xml", testsFailed: 600,
      failingNames: ["x.T.a"], failingCount: 600, evidenceRefs: ["/w/m"] }]} />)
    fireEvent.click(screen.getByRole("button", { name: /view 600 failures/i }))
    expect(screen.getByText(/\+599 more/)).toBeInTheDocument()
    expect(screen.getByText(/\/w\/m/)).toBeInTheDocument()
  })
})
