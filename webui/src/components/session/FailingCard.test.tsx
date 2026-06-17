import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { FailingCard } from "./FailingCard"

afterEach(() => cleanup())

describe("FailingCard", () => {
  it("lists failing test names with a count", () => {
    render(<FailingCard names={["A.testX", "B.testY"]} />)
    expect(screen.getByText(/Failing · 2/)).toBeInTheDocument()
    expect(screen.getByText("A.testX")).toBeInTheDocument()
    expect(screen.getByText("B.testY")).toBeInTheDocument()
  })

  it("folds hiddenCount into the total and notes the remainder", () => {
    render(<FailingCard names={["A.testX"]} hiddenCount={5} evidenceRef="reports/" />)
    expect(screen.getByText(/Failing · 6/)).toBeInTheDocument()
    expect(screen.getByText(/\+5 more/)).toBeInTheDocument()
  })

  it("renders nothing when there are no failing names", () => {
    const { container } = render(<FailingCard names={[]} />)
    expect(container).toBeEmptyDOMElement()
  })
})
