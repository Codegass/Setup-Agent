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

const HISTORY = [
  { type: "thought", content: "Need to inspect Maven output." },
  {
    type: "action",
    tool_name: "bash",
    success: false,
    parameters: { command: "mvn test" },
    output: "BUILD FAILED: enforcer",
  },
]

const JOURNAL_PAYLOAD = {
  records: JOURNAL,
  total: JOURNAL.length,
  truncated: false,
  limit: 100,
  max_text: 4000,
}

const HISTORY_PAYLOAD = {
  entries: HISTORY,
  total: HISTORY.length,
  truncated: false,
  limit: 100,
  max_text: 4000,
}

const renderTimeline = (overrides: Partial<Parameters<typeof PhaseTimeline>[0]> = {}) =>
  render(
    <PhaseTimeline
      workspaceId="sag-commons-cli"
      fetchPhases={vi.fn().mockResolvedValue(PHASES)}
      fetchPhaseJournal={vi.fn().mockResolvedValue(JOURNAL_PAYLOAD)}
      fetchPhaseHistory={vi.fn().mockResolvedValue(HISTORY_PAYLOAD)}
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
    const fetchPhaseJournal = vi.fn().mockResolvedValue(JOURNAL_PAYLOAD)
    renderTimeline({ fetchPhaseJournal })

    fireEvent.click(await screen.findByRole("button", { name: /build/ }))

    expect(fetchPhaseJournal).toHaveBeenCalledWith("sag-commons-cli", "build")
    // One row per iteration: total_chars plus INTRO/LEDGER/compaction markers.
    expect(await screen.findByText(/iter 1 .*4000 chars.*INTRO/)).toBeInTheDocument()
    expect(screen.getByText(/iter 2 .*5200 chars.*compacted=9.*LEDGER/)).toBeInTheDocument()
    expect(screen.getByText("loaded 2 of 2 records")).toBeInTheDocument()
  })

  it("expands selected phase details with journal text and branch history", async () => {
    const fetchPhaseHistory = vi.fn().mockResolvedValue(HISTORY_PAYLOAD)
    renderTimeline({ fetchPhaseHistory })

    fireEvent.click(await screen.findByRole("button", { name: /build/ }))

    expect(fetchPhaseHistory).toHaveBeenCalledWith("sag-commons-cli", "build")
    expect(await screen.findByText("=== PHASE: BUILD ===")).toBeInTheDocument()
    expect(screen.getByText(/ATTEMPT LEDGER/)).toBeInTheDocument()
    expect(screen.getByText("Need to inspect Maven output.")).toBeInTheDocument()
    expect(screen.getByText(/"command": "mvn test"/)).toBeInTheDocument()
    expect(screen.getByText("BUILD FAILED: enforcer")).toBeInTheDocument()
    expect(screen.getByText("loaded 2 of 2 entries")).toBeInTheDocument()
  })

  it("labels bounded phase details when the API truncates the window", async () => {
    renderTimeline({
      fetchPhaseJournal: vi.fn().mockResolvedValue({
        records: JOURNAL.slice(1),
        total: 2,
        truncated: true,
        limit: 1,
        max_text: 4000,
      }),
      fetchPhaseHistory: vi.fn().mockResolvedValue({
        entries: HISTORY.slice(1),
        total: 2,
        truncated: true,
        limit: 1,
        max_text: 4000,
      }),
    })

    fireEvent.click(await screen.findByRole("button", { name: /build/ }))

    expect(await screen.findByText("showing 1 of 2 records")).toBeInTheDocument()
    expect(screen.getByText("showing 1 of 2 entries")).toBeInTheDocument()
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
