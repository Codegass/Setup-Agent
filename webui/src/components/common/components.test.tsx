import { fireEvent, render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { Badge, StatusBadge } from "./Badge"
import { Tabs } from "./Tabs"
import { TestBar } from "./TestBar"

describe("common components", () => {
  it("renders a status badge with demo label semantics", () => {
    render(<StatusBadge status="passed" />)

    expect(screen.getByText("Passed")).toBeInTheDocument()
  })

  it("renders status tones from the semantic token utilities", () => {
    render(<Badge tone="green">ok</Badge>)
    expect(screen.getByText("ok")).toHaveClass("text-status-success")
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

  it("renders tabs as plain buttons without tab ARIA semantics", () => {
    const onChange = vi.fn()

    render(
      <Tabs
        tabs={[
          { id: "active", label: "Active" },
          { id: "blocked", label: "Blocked", count: 1, disabled: true },
        ]}
        value="active"
        onChange={onChange}
      />,
    )

    expect(screen.queryByRole("tablist")).not.toBeInTheDocument()
    expect(screen.queryByRole("tab", { name: /active/i })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: /blocked/i }))

    expect(onChange).not.toHaveBeenCalled()
  })
})
