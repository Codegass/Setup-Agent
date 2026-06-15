import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { BuildDetailPage } from "./BuildDetailPage"

afterEach(() => cleanup())

it("renders build tiles and per-module table", () => {
  render(<BuildDetailPage onBack={() => {}} detail={{
    build: { state: "success", system: "maven", classCount: 13104, jarCount: 279 },
    moduleSummary: { modulesTotal: 24, modulesBuilt: 21, modulesFailed: 1, modulesSkipped: 2,
                     modulesWithTestFailures: 2, buildSystems: ["maven"], singleModule: false },
    modules: [{ name: "connect:runtime", path: "connect/runtime", buildStatus: "failure",
                buildSource: "reactor", buildErrorSamples: ["[ERROR] cannot find symbol"] }],
  } as any} />)
  expect(screen.getByText("24")).toBeInTheDocument()
  expect(screen.getByText(/built/i)).toBeInTheDocument()
  expect(screen.getByText("connect:runtime")).toBeInTheDocument()
})
