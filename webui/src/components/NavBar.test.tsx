import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { NavBar } from "./NavBar"

describe("NavBar", () => {
  afterEach(() => cleanup())

  it("launches and renders formatted system stats", () => {
    const onLaunch = vi.fn()
    render(
      <NavBar
        onLaunchSetups={onLaunch}
        system={{
          dockerDiskUsed: 2 * (1 << 30),
          memUsed: 4 * (1 << 30),
          memTotal: 16 * (1 << 30),
          cpuLoad: 1.234,
        }}
      />,
    )
    fireEvent.click(screen.getByRole("button", { name: /Launch setups/ }))
    expect(onLaunch).toHaveBeenCalled()
    expect(screen.getByText("2.0 GB")).toBeInTheDocument()
    expect(screen.getByText("4.0 GB / 16.0 GB")).toBeInTheDocument()
    expect(screen.getByText("1.23")).toBeInTheDocument()
  })

  it("omits stats that are unavailable", () => {
    render(<NavBar onLaunchSetups={() => {}} system={null} />)
    expect(screen.getByRole("button", { name: /Launch setups/ })).toBeInTheDocument()
    expect(screen.queryByText(/GB/)).not.toBeInTheDocument()
  })
})
