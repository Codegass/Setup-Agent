import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import type { TestSummary } from "@/api/types"

import { TestCard } from "./TestCard"

const test: TestSummary = {
  state: "partial", pass: 18805, fail: 5, skip: 29, total: 18839, errors: 0,
  passRate: 99.8, reportFileCount: 760,
  uniqueTotal: 9497, uniquePassed: 9480, uniqueFailed: 5, uniqueErrors: 0, uniqueSkipped: 12,
  declaredTotal: 20500, methodExecutionRate: 46.3,
  failingNames: ["com.x.FooTest.testA", "com.x.BarTest.testB"],
  conflicts: ["test_report_parse_error"], evidenceRefs: ["output_5b9a"],
}

afterEach(() => cleanup())

describe("TestCard", () => {
  it("headlines the runner pass rate and separates unique methods", () => {
    render(<TestCard test={test} />)
    expect(screen.getByText("99.8% passed")).toBeInTheDocument()
    expect(screen.getByText(/18,805 \/ 18,839 runner executions/)).toBeInTheDocument()
    expect(screen.getByText(/9,497 unique methods/)).toBeInTheDocument()
    expect(screen.getByText(/46.3% method coverage/)).toBeInTheDocument()
    expect(screen.getByText(/760 XML reports/)).toBeInTheDocument()
  })

  it("expands to a calculation table and failing list", () => {
    render(<TestCard test={test} />)
    fireEvent.click(screen.getByRole("button", { name: /details/i }))
    expect(screen.getByText("Calculation")).toBeInTheDocument()
    expect(screen.getByText("Runner executions")).toBeInTheDocument()
    expect(screen.getByText("com.x.FooTest.testA")).toBeInTheDocument()
    expect(screen.getByText(/parse_error/i)).toBeInTheDocument()
  })

  it("renders an unavailable state without fake zeroes", () => {
    render(<TestCard test={{ state: "none", pass: 0, fail: 0, skip: 0, total: 0 }} />)
    expect(screen.getByText("No test evidence")).toBeInTheDocument()
    expect(screen.queryByText("0% passed")).not.toBeInTheDocument()
  })
})
