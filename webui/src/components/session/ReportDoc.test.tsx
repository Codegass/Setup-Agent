import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { ReportDoc } from "./ReportDoc"

afterEach(() => cleanup())

describe("ReportDoc", () => {
  it("explains the report's relationship to the recorded result", () => {
    render(<ReportDoc doc={{
      title: "Setup report",
      generated: "now",
      blocks: [{ type: "p", text: "Legacy diagnostic totals" }],
    }} />)

    expect(
      screen.getByText(/where its numbers differ.*the band is what the run is judged on/i),
    ).toBeInTheDocument()
  })

  it("renders partial and unknown status blocks as attention rather than failure", () => {
    render(<ReportDoc doc={{
      title: "Setup report",
      generated: "now",
      blocks: [
        { type: "status", text: "PARTIAL" },
        { type: "status", text: "UNKNOWN" },
      ],
    }} />)

    expect(screen.getByText("PARTIAL")).toHaveClass("text-status-attention")
    expect(screen.getByText("UNKNOWN")).toHaveClass("text-status-attention")
  })
})
