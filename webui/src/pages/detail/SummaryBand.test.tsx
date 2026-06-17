import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import type { ExecutionSessionDetail } from "@/api/types"

import { SummaryBand } from "./SummaryBand"

function detail(overrides: Partial<ExecutionSessionDetail> = {}): ExecutionSessionDetail {
  return {
    id: "CC-1",
    workspace: "sag-x",
    title: "t",
    status: "completed",
    entry: "SAG",
    start: "now",
    duration: "1m",
    outcome: "Setup completed and the report is ready.",
    build: { state: "success", tool: "Maven", time: "47s", note: "" },
    test: { state: "pass", pass: 10, fail: 0, skip: 0, total: 10 },
    report: "ready",
    evidence: [],
    logs: [],
    ...overrides,
  }
}

describe("SummaryBand", () => {
  it("renders the outcome and the four signal tiles", () => {
    render(<SummaryBand detail={detail()} />)
    expect(screen.getByText("Setup completed and the report is ready.")).toBeInTheDocument()
    expect(screen.getByText("Build")).toBeInTheDocument()
    expect(screen.getByText("Tests")).toBeInTheDocument()
    expect(screen.getByText("Evidence")).toBeInTheDocument()
    expect(screen.getByText("Report")).toBeInTheDocument()
  })

  it("surfaces a partial-discovery callout only when detail.partial is set", () => {
    const { rerender } = render(<SummaryBand detail={detail()} />)
    expect(screen.queryByText("Partially discovered session")).not.toBeInTheDocument()

    rerender(<SummaryBand detail={detail({ partial: true })} />)
    expect(screen.getByText("Partially discovered session")).toBeInTheDocument()
    expect(
      screen.getByText(/evidence, context, or file digests may be\s+incomplete/i),
    ).toBeInTheDocument()
  })

  it("renders the Why callout only when a blocker is present", () => {
    const { rerender } = render(<SummaryBand detail={detail()} />)
    expect(screen.queryByText(/^Why ·/)).not.toBeInTheDocument()

    rerender(
      <SummaryBand
        detail={detail({
          blocker: { code: "E_BUILD", title: "Build failed", detail: "javac error", hint: "fix imports" },
        })}
      />,
    )
    expect(screen.getByText(/Why · Build failed/)).toBeInTheDocument()
    expect(screen.getByText(/fix imports/)).toBeInTheDocument()
  })
})
