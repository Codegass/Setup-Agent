import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import type { MetricsV2EvidenceSummary, TestEvidenceLayers } from "@/api/types"

import { EvidenceAccounting } from "./EvidenceAccounting"

afterEach(() => cleanup())

// Both fixtures are the bodies `GET /api/sessions/{id}` served for real runs
// under `logs/`, copied field for field — including the reason strings, which
// are the ones the backend actually composes. An invented reason is the reason
// the retired-vocabulary case below would have passed while the screen printed
// two forbidden words.

/** `logs/advisor-high20-terra-high-gson-20260914` — a bounded identity sample,
 *  which is the only shape `partial` takes in the corpus (211 of 211). */
const gson: TestEvidenceLayers = {
  claimed: {
    receiptExecutions: {
      executed: 4866,
      passed: 4846,
      failed: 0,
      errors: 0,
      skipped: 20,
      availability: "available",
      bound: null,
      reason: null,
      basis: "report XML totals over every claimed report",
    },
    latestCases: {
      executed: 2048,
      passed: 2044,
      failed: 0,
      errors: 0,
      skipped: 4,
      availability: "partial",
      bound: "lower",
      reason:
        "one or more receipt identity samples were bounded and truncated; counts cover only retained rows",
      basis: "latest parameter-aware cases (bounded identity sample)",
    },
    latestSubjects: {
      executed: 2034,
      passed: 2030,
      failed: 0,
      errors: 0,
      skipped: 4,
      availability: "partial",
      bound: "lower",
      reason:
        "one or more receipt identity samples were bounded and truncated; counts cover only retained rows",
      basis: "latest module-qualified subjects (bounded identity sample)",
    },
  },
  quarantinedObservations: {
    executed: 0,
    passed: 0,
    failed: 0,
    errors: 0,
    skipped: 0,
    availability: "available",
    bound: null,
    reason: null,
    basis: "receipt-scoped report scan",
    reportFileCount: 0,
    reasonCounts: {},
  },
  unattributedObservations: {
    executed: 0,
    passed: 0,
    failed: 0,
    errors: 0,
    skipped: 0,
    availability: "available",
    bound: null,
    reason: null,
    basis: "all current-target receipt rows were identity-sealed",
    reportFileCount: 0,
    reasonCounts: {},
  },
  staleObservations: {
    executed: 0,
    passed: 0,
    failed: 0,
    errors: 0,
    skipped: 0,
    availability: "available",
    bound: null,
    reason: null,
    basis: "receipt hash validation",
    reportFileCount: 0,
    reasonCounts: {},
  },
  retriedCases: 0,
  flakyCases: 0,
}

const gsonEvidence: MetricsV2EvidenceSummary = {
  integrity: "complete",
  receiptsExpected: 1,
  receiptsPersisted: 1,
  terminalReceiptsUnpersisted: 0,
  conflictCount: 0,
}

/** `logs/recovery-v4-freemarker-mini-3-20260912` — two layers unavailable, and
 *  both reasons carry a word the UI may not print. 434 of the 674 archived
 *  runs state the first one. */
const freemarker: TestEvidenceLayers = {
  claimed: {
    receiptExecutions: {
      executed: 1529,
      passed: 1526,
      failed: 0,
      errors: 0,
      skipped: 3,
      availability: "available",
      bound: null,
      reason: null,
      basis: "gradle suite totals over every claimed report",
    },
    latestCases: {
      executed: null,
      passed: null,
      failed: null,
      errors: null,
      skipped: null,
      availability: "unavailable",
      bound: null,
      reason: "module-qualified subject/case identity was not sealed",
      basis: null,
    },
    latestSubjects: {
      executed: null,
      passed: null,
      failed: null,
      errors: null,
      skipped: null,
      availability: "unavailable",
      bound: null,
      reason: "module-qualified subject/case identity was not sealed",
      basis: null,
    },
  },
  quarantinedObservations: {
    executed: 0,
    passed: 0,
    failed: 0,
    errors: 0,
    skipped: 0,
    availability: "available",
    bound: null,
    reason: null,
    basis: "receipt-scoped report scan",
    reportFileCount: 0,
    reasonCounts: {},
  },
  unattributedObservations: {
    executed: null,
    passed: null,
    failed: null,
    errors: null,
    skipped: null,
    availability: "unavailable",
    bound: null,
    reason: "one or more receipt rows lacked complete module-qualified identity",
    basis: null,
    reportFileCount: null,
    reasonCounts: null,
  },
  staleObservations: {
    executed: 0,
    passed: 0,
    failed: 0,
    errors: 0,
    skipped: 0,
    availability: "available",
    bound: null,
    reason: null,
    basis: "receipt hash validation",
    reportFileCount: 0,
    reasonCounts: {},
  },
  retriedCases: null,
  flakyCases: null,
}

const freemarkerEvidence: MetricsV2EvidenceSummary = {
  integrity: "complete",
  receiptsExpected: 5,
  receiptsPersisted: 5,
  terminalReceiptsUnpersisted: 0,
  conflictCount: 0,
}

function open(): void {
  fireEvent.click(screen.getByText("Evidence accounting"))
}

/** One layer's whole row, so an assertion cannot pass because a DIFFERENT row
 *  happened to render the text it was looking for. */
function row(layer: string): string {
  const node = document.querySelector(`[data-layer="${layer}"]`)
  expect(node, `no row for ${layer}`).not.toBeNull()
  return node?.textContent ?? ""
}

describe("EvidenceAccounting", () => {
  it("is collapsed until asked for", () => {
    render(<EvidenceAccounting evidence={gsonEvidence} layers={gson} />)
    expect(screen.getByText("Evidence accounting")).toBeInTheDocument()
    expect(screen.getByText("Results bound to this run's receipts")).not.toBeVisible()
  })

  it("labels every layer in plain English", () => {
    render(<EvidenceAccounting evidence={gsonEvidence} layers={gson} />)
    open()
    for (const label of [
      "Results bound to this run's receipts",
      "Tests identified by module and name",
      "Test classes identified by module and name",
      "Set aside: not from this run's receipts",
      "Set aside: no module or test name recorded",
      "Set aside: from an earlier run",
      "Evidence records",
    ]) {
      expect(screen.getByText(label)).toBeVisible()
    }
  })

  it("states the five counts the way the result band states them", () => {
    render(<EvidenceAccounting evidence={gsonEvidence} layers={gson} />)
    open()
    expect(row("receiptExecutions")).toContain(
      "4,866 executed · 4,846 passed · 0 failed · 0 errors · 20 skipped",
    )
  })

  it("marks a lower bound as a floor, not a total", () => {
    render(<EvidenceAccounting evidence={gsonEvidence} layers={gson} />)
    open()
    expect(row("latestCases")).toContain(
      "≥2,048 executed · 2,044 passed · 0 failed · 0 errors · 4 skipped",
    )
    expect(row("latestCases")).toContain("at least this many, not a total")
    expect(row("receiptExecutions")).not.toContain("at least this many")
  })

  it("says why a layer is unavailable instead of showing zero", () => {
    render(<EvidenceAccounting evidence={freemarkerEvidence} layers={freemarker} />)
    open()
    expect(row("latestCases")).toContain("not available")
    expect(row("latestCases")).toContain("the run did not record module and test names")
    expect(row("latestSubjects")).toContain("the run did not record module and test names")
    expect(row("unattributedObservations")).toContain(
      "some recorded rows carried no module or test name",
    )
  })

  it("never shows a count for a layer the run did not count", () => {
    render(<EvidenceAccounting evidence={freemarkerEvidence} layers={freemarker} />)
    open()
    // The three unavailable layers have `executed: null`. Rendering them as
    // counts would put "0 executed" on screen beside a real 1,529 and beside
    // two layers whose zeroes are real.
    expect(row("receiptExecutions")).toContain("1,529 executed")
    expect(row("staleObservations")).toContain("0 executed")
    for (const layer of ["latestCases", "latestSubjects", "unattributedObservations"]) {
      expect(row(layer)).toContain("not available")
      expect(row(layer)).not.toMatch(/executed/)
    }
  })

  it("says how many of the run's commands left a receipt", () => {
    render(<EvidenceAccounting evidence={freemarkerEvidence} layers={freemarker} />)
    open()
    expect(row("evidenceRecords")).toContain("complete")
    expect(row("evidenceRecords")).toContain("5 of 5 commands recorded")
  })

  it("never prints a reason the payload wrote for a machine", () => {
    const { container } = render(
      <EvidenceAccounting evidence={freemarkerEvidence} layers={freemarker} />,
    )
    open()
    expect(row("latestCases")).toContain("not available")
    const text = (container.textContent ?? "").toLowerCase()
    for (const word of [
      "sealed",
      "canonical",
      "claimed",
      "quarantined",
      "subject",
      "snapshot",
      "metrics-v2",
      "verdict-bearing",
      "promoting",
    ]) {
      expect(text).not.toContain(word)
    }
  })

  it("says nothing rather than repeating a reason it cannot translate", () => {
    const unknown: TestEvidenceLayers = {
      ...freemarker,
      claimed: {
        ...freemarker.claimed,
        latestCases: {
          ...freemarker.claimed.latestCases,
          reason: "a reason this build has never seen, composed at run time",
        },
      },
    }
    const { container } = render(
      <EvidenceAccounting evidence={freemarkerEvidence} layers={unknown} />,
    )
    open()
    expect(container.textContent).not.toContain("composed at run time")
    expect(row("latestCases")).toBe("Tests identified by module and namenot available")
  })
})
