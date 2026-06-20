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

  it("renders the PASS label for a success-tone verdict", () => {
    render(
      <VerdictBand
        detail={
          {
            verdict: { tone: "success", headline: "Build passed. 1,205 tests passing" },
            outcome: "✅ PASS",
          } as any
        }
      />,
    )
    expect(screen.getByText("PASS")).toBeInTheDocument()
    expect(screen.getByText(/1,205 tests passing/)).toBeInTheDocument()
  })

  it("renders the FAILED label and the Why detail line for a failed-tone verdict", () => {
    render(
      <VerdictBand
        detail={
          {
            verdict: {
              tone: "failed",
              headline: "Build failed — review before promoting",
              detail: "fix the missing dependency",
            },
            outcome: "❌ FAILED",
          } as any
        }
      />,
    )
    expect(screen.getByText("FAILED")).toBeInTheDocument()
    expect(screen.getByText(/Why/)).toBeInTheDocument()
    expect(screen.getByText(/fix the missing dependency/)).toBeInTheDocument()
  })

  it("falls back to outcome when verdict is null", () => {
    render(<VerdictBand detail={{ verdict: null, outcome: "⚠️ PARTIAL" } as any} />)
    expect(screen.getByText(/PARTIAL/)).toBeInTheDocument()
  })
})
