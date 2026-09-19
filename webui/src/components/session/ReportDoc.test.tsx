import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { ReportDoc } from "./ReportDoc"

afterEach(() => cleanup())

describe("ReportDoc", () => {
  it("shows the report without warning that it disagrees with the band", () => {
    // The banner was there because the report was written during the run and
    // the band after it, so the two counted different runs. One derivation,
    // read once at the run's end, leaves nothing to warn about.
    render(<ReportDoc doc={{
      title: "Setup report",
      generated: "now",
      blocks: [{ type: "p", text: "19 turns across 5 phases" }],
    }} />)

    expect(screen.getByText("19 turns across 5 phases")).toBeInTheDocument()
    expect(
      screen.queryByText(/where its numbers differ.*the band is what the run is judged on/i),
    ).not.toBeInTheDocument()
    expect(screen.queryByText(/preserved for context/i)).not.toBeInTheDocument()
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
