import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { RailSkeleton } from "./RailSkeleton"

describe("RailSkeleton", () => {
  it("exposes an accessible loading status", () => {
    render(<RailSkeleton />)
    expect(screen.getByRole("status", { name: /loading workspaces/i })).toBeInTheDocument()
  })
})
