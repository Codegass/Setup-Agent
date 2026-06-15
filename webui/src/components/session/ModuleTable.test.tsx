import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

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
    // path appears in both the toolbar ("report:") and the truncation pointer
    expect(screen.getAllByText(/\/w\/m/).length).toBeGreaterThanOrEqual(1)
  })

  it("flags partial build evidence and tolerates a missing buildStatus", () => {
    render(<ModuleTable variant="build" modules={[
      { name: "a", path: "a", buildStatus: "success", buildSource: "partial",
        testSource: "none" },
      { name: "b", path: "b", buildSource: "artifacts", testSource: "none" } as never,
    ]} />)
    expect(screen.getByText("partial")).toBeInTheDocument()
    // a record missing buildStatus must not crash; it renders UNKNOWN
    expect(screen.getByText("UNKNOWN")).toBeInTheDocument()
  })

  it("offers copy-all and the report path in the expanded failing list", () => {
    const writeText = vi.fn()
    Object.assign(navigator, { clipboard: { writeText } })
    render(<ModuleTable variant="test" modules={[{
      name: "streams", path: "streams", buildStatus: "success", buildSource: "reactor",
      testSource: "runner_xml", testsFailed: 2,
      failingNames: ["a.StreamTest.shouldX", "b.StateTest.shouldY"], failingCount: 2,
      evidenceRefs: ["/workspace/streams/build/test-results"] }]} />)
    fireEvent.click(screen.getByRole("button", { name: /view 2 failures/i }))
    // report path surfaced so the user can find the full source
    expect(screen.getByText(/\/workspace\/streams\/build\/test-results/)).toBeInTheDocument()
    // copy-all copies the full newline-joined list
    fireEvent.click(screen.getByRole("button", { name: /copy all/i }))
    expect(writeText).toHaveBeenCalledWith("a.StreamTest.shouldX\nb.StateTest.shouldY")
  })
})
