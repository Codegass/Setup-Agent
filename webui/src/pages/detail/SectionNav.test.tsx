import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { Box } from "lucide-react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { FacetMeta } from "./facets"
import { SectionNav } from "./SectionNav"

const facets: FacetMeta[] = [
  { id: "build", label: "Build", icon: Box, count: null, countTone: "neutral" },
  { id: "test", label: "Test", icon: Box, count: 2, countTone: "red" },
]

describe("SectionNav", () => {
  afterEach(() => {
    cleanup()
  })

  it("renders each facet and marks the active one", () => {
    render(<SectionNav facets={facets} active="test" onJump={() => {}} />)
    const test = screen.getByRole("button", { name: /^Test/ })
    expect(test).toHaveAttribute("aria-current", "true")
    expect(screen.getByRole("button", { name: /^Build/ })).toHaveAttribute("aria-current", "false")
  })

  it("calls onJump with the facet id when clicked", () => {
    const onJump = vi.fn()
    render(<SectionNav facets={facets} active="build" onJump={onJump} />)
    fireEvent.click(screen.getByRole("button", { name: /^Test/ }))
    expect(onJump).toHaveBeenCalledWith("test")
  })
})
