import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { NavBar } from "./NavBar"

describe("NavBar", () => {
  afterEach(() => cleanup())

  it("renders formatted system stats", () => {
    render(
      <NavBar
        dark={false}
        onToggleTheme={() => {}}
        system={{
          dockerDiskUsed: 2 * (1 << 30),
          memUsed: 4 * (1 << 30),
          memTotal: 16 * (1 << 30),
          cpuLoad: 1.234,
        }}
      />,
    )
    expect(screen.getByText("2.0 GB")).toBeInTheDocument()
    expect(screen.getByText("4.0 GB / 16.0 GB")).toBeInTheDocument()
    expect(screen.getByText("1.23")).toBeInTheDocument()
  })

  it("omits stats that are unavailable", () => {
    render(<NavBar dark={false} onToggleTheme={() => {}} system={null} />)
    expect(screen.queryByText(/GB/)).not.toBeInTheDocument()
  })
})
