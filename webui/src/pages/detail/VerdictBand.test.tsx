import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { VerdictBand } from "./VerdictBand"

afterEach(() => cleanup())

describe("VerdictBand", () => {
  it("renders the verdict headline with attention tone", () => {
    render(
      <VerdictBand
        detail={
          {
            verdict: {
              tone: "attention",
              headline: "Build passed on 3 of 4 modules. 7 failing — review before promoting",
            },
            outcome: "⚠️ PARTIAL",
          } as any
        }
      />,
    )
    expect(screen.getByText(/7 failing — review before promoting/)).toBeInTheDocument()
  })

  it("falls back to outcome when verdict is null", () => {
    render(<VerdictBand detail={{ verdict: null, outcome: "⚠️ PARTIAL" } as any} />)
    expect(screen.getByText(/PARTIAL/)).toBeInTheDocument()
  })
})
