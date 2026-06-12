import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { PhaseTimeline } from "./PhaseTimeline"

const PHASES = [
  { name: "provision", status: "completed", notes: "", key_results: "JDK 17 ready" },
  { name: "build", status: "failed", notes: "blocked: enforcer violations", key_results: "" },
]

const JOURNAL = [
  {
    iteration: 1,
    total_chars: 4000,
    delta: { added: 1, compacted: 0 },
    intro_text: "=== PHASE: BUILD ===",
    step_span: 1,
  },
  {
    iteration: 2,
    total_chars: 5200,
    delta: { added: 2, compacted: 9 },
    ledger_text: "ATTEMPT LEDGER:\n✗ build: enforcer",
    step_span: 3,
  },
]

const renderTimeline = (overrides: Partial<Parameters<typeof PhaseTimeline>[0]> = {}) =>
  render(
    <PhaseTimeline
      workspaceId="sag-commons-cli"
      fetchPhases={vi.fn().mockResolvedValue(PHASES)}
      fetchPhaseJournal={vi.fn().mockResolvedValue(JOURNAL)}
      {...overrides}
    />,
  )

describe("PhaseTimeline", () => {
  afterEach(() => {
    cleanup()
  })

  it("renders the phase list with status icons and key results", async () => {
    renderTimeline()

    expect(await screen.findByText("provision")).toBeInTheDocument()
    expect(screen.getByText("build")).toBeInTheDocument()
    // Status icons carry accessible names: done for completed, blocked for failed.
    expect(screen.getByLabelText("done")).toBeInTheDocument()
    expect(screen.getByLabelText("blocked")).toBeInTheDocument()
    expect(screen.getByText("JDK 17 ready")).toBeInTheDocument()
  })

  it("shows the blocked reason for a failed phase", async () => {
    renderTimeline()

    expect(await screen.findByText("blocked: enforcer violations")).toBeInTheDocument()
  })

  it("loads the journal on phase click and shows iteration rows with markers", async () => {
    const fetchPhaseJournal = vi.fn().mockResolvedValue(JOURNAL)
    renderTimeline({ fetchPhaseJournal })

    fireEvent.click(await screen.findByRole("button", { name: /build/ }))

    expect(fetchPhaseJournal).toHaveBeenCalledWith("sag-commons-cli", "build")
    // One row per iteration: total_chars plus INTRO/LEDGER/compaction markers.
    expect(await screen.findByText(/iter 1 .*4000 chars.*INTRO/)).toBeInTheDocument()
    expect(screen.getByText(/iter 2 .*5200 chars.*compacted=9.*LEDGER/)).toBeInTheDocument()
  })

  it("surfaces a journal fetch error instead of rows", async () => {
    const fetchPhaseJournal = vi.fn().mockRejectedValue(new Error("404 Not Found"))
    renderTimeline({ fetchPhaseJournal })

    fireEvent.click(await screen.findByRole("button", { name: /provision/ }))

    expect(await screen.findByText("404 Not Found")).toBeInTheDocument()
  })

  it("surfaces a phase list fetch error", async () => {
    renderTimeline({ fetchPhases: vi.fn().mockRejectedValue(new Error("404 Not Found")) })

    expect(await screen.findByText("404 Not Found")).toBeInTheDocument()
  })
})
