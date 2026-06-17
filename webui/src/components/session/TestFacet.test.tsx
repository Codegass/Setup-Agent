import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { TestFacet } from "./TestFacet"

afterEach(() => cleanup())

const single = {
  test: {
    state: "partial", pass: 312, fail: 8, skip: 0, total: 320, passRate: 97.5, uniqueTotal: 318,
    failingNames: ["HelpFormatterTest.testWrappedWidth", "BugCLI162Test.testInfiniteLoop"],
  },
  moduleSummary: { singleModule: true },
  modules: [],
} as any

const multi = {
  test: { state: "partial", pass: 3838, fail: 3, skip: 0, total: 3841, passRate: 99.9, uniqueTotal: 2907, failingNames: [] },
  moduleSummary: { modulesTotal: 24, modulesWithTestFailures: 2, singleModule: false },
  modules: [{ name: "streams", path: "streams", buildStatus: "success", testSource: "runner_xml", testsPassed: 1, testsFailed: 0 }],
} as any

describe("TestFacet", () => {
  it("shows conclusion + FAILING for single-module, with no breakdown button", () => {
    render(<TestFacet detail={single} />)
    expect(screen.getByText(/97\.5% pass/i)).toBeInTheDocument()
    expect(screen.getByText(/Failing · 2/)).toBeInTheDocument()
    expect(screen.getByText("HelpFormatterTest.testWrappedWidth")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /per-module breakdown/i })).not.toBeInTheDocument()
  })

  it("opens the per-module breakdown modal for a multi-module project", () => {
    render(<TestFacet detail={multi} />)
    fireEvent.click(screen.getByRole("button", { name: /per-module breakdown/i }))
    expect(screen.getByRole("dialog", { name: /per-module test breakdown/i })).toBeInTheDocument()
    expect(screen.getByText("streams")).toBeInTheDocument()
  })
})
