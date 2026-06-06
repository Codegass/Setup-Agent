import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { StatusBadge } from "./Badge"
import { TestBar } from "./TestBar"

describe("common components", () => {
  it("renders a status badge with demo label semantics", () => {
    render(<StatusBadge status="passed" />)

    expect(screen.getByText("Passed")).toBeInTheDocument()
  })

  it("renders an empty glyph when a test bar has no total", () => {
    render(<TestBar pass={0} fail={0} total={0} />)

    expect(screen.getByText("—")).toBeInTheDocument()
  })

  it("renders pass, fail, and total counts for a populated test bar", () => {
    render(<TestBar pass={7} fail={2} total={10} />)

    expect(screen.getByText("7")).toBeInTheDocument()
    expect(screen.getByText("/ 2")).toBeInTheDocument()
    expect(screen.getByText("· 10")).toBeInTheDocument()
  })
})
