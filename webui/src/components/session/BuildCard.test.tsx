import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { BuildSummary } from "@/api/types"

import { BuildCard } from "./BuildCard"

const build: BuildSummary = {
  state: "success", tool: "/usr/bin/mvn", time: "—", note: "",
  system: "maven", classCount: 115, jarCount: 0, moduleOutputCount: 3,
  artifactSamples: ["target/classes/Foo.class", "target/app.jar"],
  warnings: [], evidenceRefs: ["output_x"],
}

afterEach(() => cleanup())

describe("BuildCard", () => {
  it("shows the conclusion line and key counts", () => {
    render(<BuildCard build={build} />)
    expect(screen.getByText("Artifacts verified")).toBeInTheDocument()
    expect(screen.getByText(/115/)).toBeInTheDocument() // class count
    expect(screen.getByText(/maven/i)).toBeInTheDocument()
  })

  it("opens the detail page when Details is clicked", () => {
    const onOpenDetail = vi.fn()
    render(<BuildCard build={build} onOpenDetail={onOpenDetail} />)
    fireEvent.click(screen.getByRole("button", { name: /open build details/i }))
    expect(onOpenDetail).toHaveBeenCalled()
  })

  it("omits the Details affordance when no handler is provided", () => {
    render(<BuildCard build={build} />)
    expect(screen.queryByRole("button", { name: /open build details/i })).not.toBeInTheDocument()
  })

  it("does not overclaim when no artifacts", () => {
    render(<BuildCard build={{ ...build, state: "failed", classCount: 0, jarCount: 0, artifactSamples: [] }} />)
    expect(screen.getByText("No build artifacts found")).toBeInTheDocument()
  })

  it("renders an unavailable state without fake zeroes", () => {
    render(<BuildCard build={{ state: "unknown", tool: "—", time: "—", note: "" }} />)
    expect(screen.getByText("Build evidence unavailable")).toBeInTheDocument()
    expect(screen.queryByText(/0 classes/)).not.toBeInTheDocument()
  })
})
