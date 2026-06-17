import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { BuildDetailPage } from "./BuildDetailPage"

afterEach(() => cleanup())

const multi = {
  build: { state: "success", system: "maven", classCount: 1300, jarCount: 12 },
  moduleSummary: {
    modulesTotal: 24, modulesBuilt: 21, modulesFailed: 1, modulesSkipped: 2,
    modulesWithTestFailures: 2, buildSystems: ["maven"], singleModule: false,
  },
  modules: [{
    name: "connect:runtime", path: "connect/runtime", buildStatus: "failure",
    buildSource: "reactor", buildErrorSamples: ["[ERROR] cannot find symbol"],
  }],
} as any

describe("BuildDetailPage (per-module breakdown)", () => {
  it("renders module stats and the per-module table", () => {
    render(<BuildDetailPage detail={multi} />)
    expect(screen.getByText("24")).toBeInTheDocument() // Modules stat
    expect(screen.getByText(/built/i)).toBeInTheDocument()
    expect(screen.getByText("connect:runtime")).toBeInTheDocument()
  })

  it("shows the module success rate (21/24)", () => {
    render(<BuildDetailPage detail={multi} />)
    expect(screen.getByText(/88%/)).toBeInTheDocument()
  })

  it("notes Gradle best-effort status when the build system is gradle", () => {
    render(
      <BuildDetailPage
        detail={{ ...multi, moduleSummary: { ...multi.moduleSummary, buildSystems: ["gradle"] } }}
      />,
    )
    expect(screen.getByText(/inferred from build outputs/i)).toBeInTheDocument()
  })

  it("renders '—' for absent counts, keeps a real 0", () => {
    render(
      <BuildDetailPage
        detail={{
          ...multi,
          moduleSummary: { ...multi.moduleSummary, modulesTotal: null, modulesBuilt: null, modulesFailed: 0 },
        }}
      />,
    )
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2)
    expect(screen.getAllByText("0").length).toBeGreaterThanOrEqual(1)
  })
})
