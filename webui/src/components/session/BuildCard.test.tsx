import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

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

  it("expands to artifact summary and evidence samples", () => {
    render(<BuildCard build={build} />)
    fireEvent.click(screen.getByRole("button", { name: /details/i }))
    expect(screen.getByText("Artifact Summary")).toBeInTheDocument()
    expect(screen.getByText("target/classes/Foo.class")).toBeInTheDocument()
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
