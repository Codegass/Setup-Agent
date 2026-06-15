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

  it("does not show a >100% method coverage when the static catalog undercounts", () => {
    // Real commons-cli Maven run: 584 unique methods actually ran but only 460
    // were detected by static analysis, so unique/declared = 126.96%. A
    // "127% method coverage" reads as a bug; present the discrepancy instead.
    const undercounted: TestSummary = {
      state: "success", pass: 916, fail: 0, skip: 61, total: 977, errors: 0,
      passRate: 100, reportFileCount: 47,
      uniqueTotal: 584, uniquePassed: 523, uniqueFailed: 0, uniqueErrors: 0, uniqueSkipped: 61,
      declaredTotal: 460, methodExecutionRate: 126.96,
      failingNames: [], conflicts: [], evidenceRefs: [],
    }
    render(<TestCard test={undercounted} />)
    expect(screen.getByText(/584 unique methods/)).toBeInTheDocument()
    expect(screen.queryByText(/method coverage/)).not.toBeInTheDocument()
    expect(screen.queryByText(/12[67]/)).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: /details/i }))
    expect(screen.getByText("Method execution")).toBeInTheDocument()
    expect(screen.getByText(/catalog incomplete/i)).toBeInTheDocument()
    expect(screen.queryByText(/126\.9/)).not.toBeInTheDocument()
  })

  it("counts errors as failures so the body agrees with a non-success badge", () => {
    render(
      <TestCard
        test={{ state: "partial", pass: 97, fail: 0, skip: 0, total: 100, errors: 3 }}
      />,
    )
    // failed line folds errors in (0 failures + 3 errors -> 3 failed)
    expect(screen.getByText("3 failed")).toBeInTheDocument()
    // errors are surfaced explicitly, not hidden
    expect(screen.getByText(/3 errors/)).toBeInTheDocument()
    // the red bar must not be empty when only errors are present
    const bar = screen.getByLabelText("runner pass rate")
    const red = bar.querySelector(".bg-red-500") as HTMLElement
    expect(red.style.width).not.toBe("0%")
  })
})
