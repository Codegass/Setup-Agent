import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { BuildFacet } from "./BuildFacet"

afterEach(() => cleanup())

const single = {
  build: {
    state: "success", system: "Maven", tool: "Maven 3.9.6", time: "47.2s", note: "clean package",
    classCount: 115, jarCount: 1, warnings: ["2 deprecation warnings in HelpFormatter.java"],
  },
  moduleSummary: { singleModule: true },
  modules: [],
} as any

const multi = {
  build: { state: "success", system: "maven", classCount: 1300, jarCount: 12 },
  moduleSummary: { modulesTotal: 24, modulesBuilt: 21, modulesFailed: 1, modulesSkipped: 2, buildSystems: ["maven"], singleModule: false },
  modules: [{ name: "connect:runtime", path: "connect/runtime", buildStatus: "failure", buildSource: "reactor" }],
} as any

describe("BuildFacet", () => {
  it("shows the two-card summary + a 'View build details' detail for single-module", () => {
    render(<BuildFacet detail={single} />)
    expect(screen.getByText("Success")).toBeInTheDocument()
    expect(screen.getByText("Outputs")).toBeInTheDocument()
    expect(screen.getByText("115")).toBeInTheDocument()
    expect(screen.getByText(/clean package/)).toBeInTheDocument()
    expect(screen.getByText(/HelpFormatter\.java/)).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /per-module breakdown/i })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: /view build details/i }))
    expect(screen.getByRole("dialog", { name: /build details/i })).toBeInTheDocument()
  })

  it("opens the per-module breakdown modal for a multi-module project", () => {
    render(<BuildFacet detail={multi} />)
    fireEvent.click(screen.getByRole("button", { name: /per-module breakdown/i }))
    expect(screen.getByRole("dialog", { name: /per-module build breakdown/i })).toBeInTheDocument()
    expect(screen.getByText("connect:runtime")).toBeInTheDocument()
  })
})
