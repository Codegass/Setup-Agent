import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { ResultCard, ResultRow } from "@/api/types"

import { ResultBand } from "./ResultBand"

function row(overrides: Partial<ResultRow> & Pick<ResultRow, "key" | "label">): ResultRow {
  return { status: "unknown", tone: "neutral", headline: "—", ...overrides }
}

/** The seven rows in the order and the wording `build_result_card` emits them:
 *  every headline, detail, reason and item below was copied from a card built
 *  from a real `verdict.json` in `logs/`. */
function card(overrides: Partial<ResultCard> = {}): ResultCard {
  return {
    schemaVersion: 1,
    runId: "r",
    verdict: "success",
    verdictSource: "snapshot",
    rows: [
      row({ key: "setup", label: "Setup", status: "success", tone: "success", headline: "4/4 phases · 12 turns" }),
      row({
        key: "task",
        label: "Required task",
        status: "complete",
        tone: "success",
        headline: "complete 1/1 steps",
        detail: "mvn -B -f pom.xml -V clean test → exit 0",
        items: ["ci-step-1: complete — mvn -B -f pom.xml -V clean test → exit 0"],
      }),
      row({ key: "build", label: "Build", status: "success", tone: "success", headline: "1/1 modules built" }),
      row({
        key: "tests",
        label: "Tests",
        status: "executed",
        tone: "success",
        headline: "994 executed · 994 passed · 0 failed · 0 errors · 0 skipped",
      }),
      row({
        key: "coverage",
        label: "Coverage",
        status: "not collected",
        headline: "not collected",
        reason: "coverage pass not run",
      }),
      row({
        key: "ci",
        label: "Official CI",
        status: "not compared",
        headline: "not compared",
        reason: "no CI job on this commit matches the run's JDK and OS (official_ci_cell_not_matched)",
      }),
      row({ key: "report", label: "Report", status: "delivered", headline: "setup-report.md" }),
    ],
    stats: {},
    attention: [],
    notes: [],
    ...overrides,
  }
}

describe("ResultBand", () => {
  afterEach(() => {
    cleanup()
  })

  it("shows every row's label, status and headline", () => {
    render(<ResultBand card={card()} onOpenTab={() => {}} />)
    for (const label of ["Setup", "Required task", "Build", "Tests", "Coverage", "Official CI", "Report"]) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }
    expect(screen.getByText("994 executed · 994 passed · 0 failed · 0 errors · 0 skipped")).toBeInTheDocument()
    expect(screen.getByText("complete 1/1 steps")).toBeInTheDocument()
    expect(screen.getByText("not collected", { selector: "span" })).toBeInTheDocument()
  })

  it("shows a row's reason when it has nothing to measure", () => {
    render(<ResultBand card={card()} onOpenTab={() => {}} />)
    expect(
      screen.getByText(/no CI job on this commit matches the run's JDK and OS/),
    ).toBeInTheDocument()
  })

  it("hides a row's items behind a disclosure", () => {
    render(<ResultBand card={card()} onOpenTab={() => {}} />)
    expect(screen.queryByText(/ci-step-1: complete/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: /show 1 step/i }))
    expect(screen.getByText(/ci-step-1: complete/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: /hide the 1 step/i }))
    expect(screen.queryByText(/ci-step-1: complete/)).not.toBeInTheDocument()
  })

  it("opens the tab a row belongs to", () => {
    const onOpenTab = vi.fn()
    render(<ResultBand card={card()} onOpenTab={onOpenTab} />)
    fireEvent.click(screen.getByRole("button", { name: "Open the evidence behind Tests" }))
    expect(onOpenTab).toHaveBeenCalledWith("tests")
  })

  // A row whose tab this run does not have must not offer a button that goes
  // nowhere. `buildDetailTabs` omits the Official CI tab when the run carries
  // no comparison, which is 184 of the 681 runs recorded under `logs/`.
  it("states a row's label without a button when its tab is not on this run", () => {
    const onOpenTab = vi.fn()
    render(
      <ResultBand
        availableTabs={["overview", "tests", "build", "report"]}
        card={card()}
        onOpenTab={onOpenTab}
      />,
    )
    expect(screen.getByRole("button", { name: "Open the evidence behind Tests" })).toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: "Open the evidence behind Official CI" }),
    ).not.toBeInTheDocument()
    expect(screen.getByText("Official CI")).toBeInTheDocument()
  })

  // Real cards say the same word twice on three of the seven rows — build
  // "success"/"success", coverage "not collected"/"not collected", CI "not
  // compared"/"not compared". The chip has already said it.
  it("does not repeat a headline that only says the status again", () => {
    const { container } = render(<ResultBand card={card()} onOpenTab={() => {}} />)
    const coverage = container.querySelector('[data-row="coverage"]')!
    expect(coverage.textContent).toContain("not collected")
    expect(coverage.querySelectorAll("p")).toHaveLength(1)
    expect(coverage.querySelector("p")!.textContent).toBe("coverage pass not run")
  })

  it("takes the band's tone from the setup row", () => {
    const failed = card({
      verdict: "failed",
      rows: card().rows.map((r) =>
        r.key === "setup" ? { ...r, status: "failed", tone: "failed" as const } : r,
      ),
    })
    const { container } = render(<ResultBand card={failed} onOpenTab={() => {}} />)
    expect(container.firstChild).toHaveAttribute("data-tone", "failed")
  })

  // The terminal and the report both print a Record line for a reconstructed
  // result (`tests/test_snapshot_surface_agreement.py`). The browser printed
  // nothing, so the same run looked like a reading in one place and a
  // reconstruction in another.
  it("says when a result was reconstructed from an older record", () => {
    render(
      <ResultBand card={card({ verdictSource: "legacy" })} onOpenTab={() => {}} />,
    )
    expect(screen.getByText(/reconstructed from an older run record/)).toBeInTheDocument()
  })

  it("says nothing about provenance for a result read from the run that wrote it", () => {
    render(<ResultBand card={card()} onOpenTab={() => {}} />)
    expect(screen.queryByText(/reconstructed from an older run record/)).toBeNull()
    expect(screen.queryByText("Record")).toBeNull()
  })

  it("says so when no result was recorded at all", () => {
    render(<ResultBand card={null} onOpenTab={() => {}} snapshotStatus="missing" />)
    expect(screen.getByText(/No result was recorded for this run/)).toBeInTheDocument()
  })

  // A record this page could not read is not a run that recorded nothing. The
  // band said the second when it only knew the first, which is the one failure
  // this layer exists to prevent.
  it("does not claim a run recorded nothing when its record could not be read", () => {
    render(<ResultBand card={null} onOpenTab={() => {}} snapshotStatus="untrusted" />)
    expect(screen.queryByText(/No result was recorded/)).toBeNull()
    expect(screen.getByText(/result record could not be read here/i)).toBeInTheDocument()
  })

  it("says the same of a record it could read the bytes of but not parse", () => {
    render(<ResultBand card={null} onOpenTab={() => {}} snapshotStatus="corrupt" />)
    expect(screen.queryByText(/No result was recorded/)).toBeNull()
    expect(screen.getByText(/result record could not be read here/i)).toBeInTheDocument()
  })

  // The band states a row's reason in the run's own words, code and all. This
  // pins the passthrough so the vocabulary test below is not mistaken for a
  // guarantee about the payload — it guards only the band's own copy.
  it("states a row's reason in the run's own words", () => {
    const withCode = card({
      rows: card().rows.map((r) =>
        r.key === "ci"
          ? {
              ...r,
              items: [
                "TARGET_TEST_UNIVERSE_EMPTY: the CI job recorded no test identities to compare against",
              ],
            }
          : r,
      ),
    })
    render(<ResultBand card={withCode} onOpenTab={() => {}} />)
    fireEvent.click(screen.getByRole("button", { name: /show 1 finding/i }))
    expect(screen.getByText(/TARGET_TEST_UNIVERSE_EMPTY/)).toBeInTheDocument()
  })

  it("writes none of the retired words in its own copy", () => {
    const { container } = render(<ResultBand card={card()} onOpenTab={() => {}} />)
    fireEvent.click(screen.getByRole("button", { name: /show 1 step/i }))
    const text = container.textContent ?? ""
    // Guard both directions: a band that rendered nothing would pass the
    // absence assertions below for the wrong reason.
    expect(text).toContain("Official CI")
    expect(text).toContain("ci-step-1: complete")
    expect(text.length).toBeGreaterThan(200)
    const retired = [
      "sealed",
      "canonical",
      "claimed",
      "quarantined",
      "subject",
      "snapshot",
      "metrics-v2",
      "verdict-bearing",
      "promoting",
    ]
    expect(retired).toHaveLength(9)
    for (const word of retired) {
      expect(text.toLowerCase()).not.toContain(word)
    }
  })
})
