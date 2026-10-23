import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { ResultCard, ResultRow } from "@/api/types"

import { ResultBand } from "./ResultBand"

function row(overrides: Partial<ResultRow> & Pick<ResultRow, "key" | "label">): ResultRow {
  return { status: "unknown", tone: "neutral", headline: "—", ...overrides }
}

/** The seven rows in the order and the wording `build_result_card` emits them:
 *  the CI command, the official CI comparison, what SAG could not confirm, how
 *  the run ended, the Summary word, then coverage and the report. The strings
 *  are the card's own, as the terminal block prints them. */
function card(overrides: Partial<ResultCard> = {}): ResultCard {
  return {
    schemaVersion: 1,
    runId: "r",
    verdict: "success",
    verdictSource: "snapshot",
    rows: [
      row({
        key: "ci_command",
        label: "CI command",
        status: "passed",
        tone: "success",
        headline: "1 of 1 steps passed",
        detail: "Java 17 and Maven 3.9.16 as pinned · sources unchanged",
        items: ["ci-step-1: complete — mvn -B -f pom.xml -V clean test → exit 0"],
      }),
      row({
        key: "official_ci",
        label: "Official CI",
        status: "compared",
        tone: "success",
        headline: "built 1 of 1 modules, ran 994 of 994 tests (CI cell 'build (17)')",
        detail: "0 failures, 0 errors in this run · 119 class files",
      }),
      row({
        key: "unconfirmed",
        label: "Not confirmed by SAG",
        status: "none",
        tone: "success",
        headline: "SAG confirmed each of the above from its own records.",
      }),
      row({
        key: "run",
        label: "Run",
        status: "ended normally",
        tone: "success",
        headline: "ended normally",
        detail: "4/4 phases · 12 turns",
      }),
      row({
        key: "summary",
        label: "Summary",
        status: "success",
        tone: "success",
        headline: "derived from the lines above",
      }),
      row({
        key: "coverage",
        label: "Coverage",
        status: "not collected",
        headline: "not collected",
        reason: "coverage pass not run",
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
    // The band remembers whether it was collapsed, so a test that collapses it
    // would otherwise decide what the next one renders. jsdom here exposes a
    // `localStorage` object with no methods on it at all, which is why
    // `safeStorage` exists — hence the optional call rather than a plain one.
    window.localStorage?.clear?.()
  })

  it("shows every row's label, status and headline in the terminal's order", () => {
    const { container } = render(<ResultBand card={card()} onOpenTab={() => {}} />)
    const labels = ["CI command", "Official CI", "Not confirmed by SAG", "Run", "Summary", "Coverage", "Report"]
    for (const label of labels) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }
    expect(
      Array.from(container.querySelectorAll("[data-row]")).map((node) => node.getAttribute("data-row")),
    ).toEqual(["ci_command", "official_ci", "unconfirmed", "run", "summary", "coverage", "report"])
    expect(screen.getByText("1 of 1 steps passed")).toBeInTheDocument()
    expect(
      screen.getByText("built 1 of 1 modules, ran 994 of 994 tests (CI cell 'build (17)')"),
    ).toBeInTheDocument()
    expect(screen.getByText("SAG confirmed each of the above from its own records.")).toBeInTheDocument()
    expect(screen.getByText("derived from the lines above")).toBeInTheDocument()
    expect(screen.getByText("not collected", { selector: "span" })).toBeInTheDocument()
  })

  it("collapses to the first line and the Summary", () => {
    render(<ResultBand card={card()} onOpenTab={() => {}} />)
    fireEvent.click(screen.getByRole("button", { name: "Collapse" }))
    expect(screen.getByText("CI command")).toBeInTheDocument()
    expect(screen.getByText("1 of 1 steps passed")).toBeInTheDocument()
    expect(screen.getByText("Summary")).toBeInTheDocument()
    expect(screen.getByText("derived from the lines above")).toBeInTheDocument()
    for (const label of ["Official CI", "Not confirmed by SAG", "Run", "Coverage", "Report"]) {
      expect(screen.queryByText(label)).toBeNull()
    }
  })

  it("expands again", () => {
    render(<ResultBand card={card()} onOpenTab={() => {}} />)
    fireEvent.click(screen.getByRole("button", { name: "Collapse" }))
    expect(screen.queryByText("Official CI")).toBeNull()
    fireEvent.click(screen.getByRole("button", { name: "Show all 7 rows" }))
    expect(screen.getByText("Official CI")).toBeInTheDocument()
  })

  it("remembers the choice for the next visit", () => {
    // jsdom's `window.localStorage` here is an object with no methods, so
    // `safeStorage` no-ops and the band would come back expanded no matter what
    // it wrote. A real one is installed for this test only, because otherwise
    // the persistence — the whole point of remembering — is never exercised.
    const store = new Map<string, string>()
    const original = Object.getOwnPropertyDescriptor(window, "localStorage")
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: {
        clear: () => store.clear(),
        getItem: (key: string) => store.get(key) ?? null,
        removeItem: (key: string) => void store.delete(key),
        setItem: (key: string, value: string) => void store.set(key, value),
      },
    })
    try {
      const first = render(<ResultBand card={card()} onOpenTab={() => {}} />)
      fireEvent.click(screen.getByRole("button", { name: "Collapse" }))
      first.unmount()

      render(<ResultBand card={card()} onOpenTab={() => {}} />)
      expect(screen.queryByText("Official CI")).toBeNull()
      expect(screen.getByRole("button", { name: "Show all 7 rows" })).toBeInTheDocument()
    } finally {
      if (original) Object.defineProperty(window, "localStorage", original)
    }
  })

  it("hides a kept row's detail and reason rather than trimming them", () => {
    const unfinished = card({
      rows: [
        row({
          key: "ci_command",
          label: "CI command",
          status: "did not finish",
          tone: "attention",
          headline: "1 of 2 steps passed · step 2 of 2 did not finish",
          detail:
            "toolchain not checked: step ci-step-2 did not finish, so its Java version was not recorded · sources unchanged",
        }),
        row({ key: "official_ci", label: "Official CI", status: "not compared" }),
        row({ key: "summary", label: "Summary", status: "partial", tone: "attention", headline: "derived from the lines above" }),
      ],
    })
    render(<ResultBand card={unfinished} onOpenTab={() => {}} />)
    fireEvent.click(screen.getByRole("button", { name: "Collapse" }))
    expect(screen.getByText("1 of 2 steps passed · step 2 of 2 did not finish")).toBeInTheDocument()
    expect(screen.queryByText(/toolchain not checked/)).toBeNull()
  })

  it("shows a row's reason when it has nothing to measure", () => {
    render(<ResultBand card={card()} onOpenTab={() => {}} />)
    expect(screen.getByText("coverage pass not run")).toBeInTheDocument()
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
    fireEvent.click(screen.getByRole("button", { name: "Open the evidence behind Run" }))
    expect(onOpenTab).toHaveBeenCalledWith("flow")
    fireEvent.click(screen.getByRole("button", { name: "Open the evidence behind CI command" }))
    expect(onOpenTab).toHaveBeenCalledWith("evidence")
  })

  // The Summary is derived from the rows above it; those rows are the links.
  it("states the Summary label without a button", () => {
    render(<ResultBand card={card()} onOpenTab={() => {}} />)
    expect(screen.getByText("Summary")).toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: "Open the evidence behind Summary" }),
    ).not.toBeInTheDocument()
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
    expect(screen.getByRole("button", { name: "Open the evidence behind Coverage" })).toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: "Open the evidence behind Official CI" }),
    ).not.toBeInTheDocument()
    expect(screen.getByText("Official CI")).toBeInTheDocument()
  })

  // Real cards say the same word twice on some rows — the Run row's "ended
  // normally"/"ended normally", coverage "not collected"/"not collected". The
  // chip has already said it.
  it("does not repeat a headline that only says the status again", () => {
    const { container } = render(<ResultBand card={card()} onOpenTab={() => {}} />)
    const coverage = container.querySelector('[data-row="coverage"]')!
    expect(coverage.textContent).toContain("not collected")
    expect(coverage.querySelectorAll("p")).toHaveLength(1)
    expect(coverage.querySelector("p")!.textContent).toBe("coverage pass not run")
  })

  it("takes the band's tone from the Summary row", () => {
    const failed = card({
      verdict: "failed",
      rows: card().rows.map((r) =>
        r.key === "summary" ? { ...r, status: "failed", tone: "failed" as const } : r,
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
        r.key === "official_ci"
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
