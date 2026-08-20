import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { EvidenceTimeline } from "./EvidenceTimeline"

afterEach(() => cleanup())

describe("EvidenceTimeline", () => {
  it("presents an evidence ref as a copyable value instead of a fake external link", () => {
    const writeText = vi.fn()
    Object.assign(navigator, { clipboard: { writeText } })
    render(<EvidenceTimeline groups={[{
      source: "Build evidence",
      status: "partial",
      counts: "1 reference",
      time: "03:50",
      summary: "Evidence used for the sealed build result.",
      records: [{
        time: "03:50",
        status: "partial",
        title: "Build evidence reference",
        detail: "Canonical build evidence",
        ref: "output_abc123",
      }],
    }]} />)

    fireEvent.click(screen.getByRole("button", { name: /copy evidence reference output_abc123/i }))
    expect(writeText).toHaveBeenCalledWith("output_abc123")
  })
})
