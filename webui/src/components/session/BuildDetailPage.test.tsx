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

it("notes Gradle best-effort status when the build system is gradle", () => {
  render(<BuildDetailPage onBack={() => {}} detail={{
    build: { state: "success", system: "gradle", classCount: 1063, jarCount: 1 },
    moduleSummary: { modulesTotal: 11, modulesBuilt: 5, modulesFailed: 0, modulesSkipped: 0,
                     modulesWithTestFailures: 0, buildSystems: ["gradle"], singleModule: false },
    modules: [{ name: "guava", path: "guava", buildStatus: "success", buildSource: "artifacts" }],
  } as any} />)
  expect(screen.getByText(/inferred from build outputs/i)).toBeInTheDocument()
})

it("renders '—' for absent counts instead of a fake zero", () => {
  render(<BuildDetailPage onBack={() => {}} detail={{
    build: { state: "unknown", system: "maven", classCount: null, jarCount: null },
    moduleSummary: { modulesTotal: 3, modulesBuilt: 2, modulesFailed: 0, modulesSkipped: 0,
                     modulesWithTestFailures: 0, buildSystems: ["maven"], singleModule: false },
    modules: [{ name: "a", path: "a", buildStatus: "success", buildSource: "artifacts" }],
  } as any} />)
  // Classes/JARs are absent -> "—", not "0"
  const dashes = screen.getAllByText("—")
  expect(dashes.length).toBeGreaterThanOrEqual(2)
  // A real zero is still shown as 0 (modulesFailed = 0)
  expect(screen.getAllByText("0").length).toBeGreaterThanOrEqual(1)
})

it("shows a conclusion card with the build system and command", () => {
  render(<BuildDetailPage detail={{
    build: { state: "success", tool: "Maven", time: "47.2s", note: "mvn -q install",
             system: "Maven", classCount: 120, jarCount: 3 },
    moduleSummary: { singleModule: true },
    modules: [],
  } as any} />)
  expect(screen.getByText("Success")).toBeInTheDocument()
  expect(screen.getByText("mvn -q install")).toBeInTheDocument()
  expect(screen.getByText("120")).toBeInTheDocument()
})

it("uses a single-module note that does not mention an Overview tab", () => {
  render(<BuildDetailPage detail={{
    build: { state: "success", system: "maven", classCount: 1, jarCount: 1 },
    moduleSummary: { singleModule: true },
    modules: [],
  } as any} />)
  expect(screen.queryByText(/Overview/i)).not.toBeInTheDocument()
  expect(screen.getByText(/single-module project/i)).toBeInTheDocument()
})

it("omits the back button when onBack is not provided (embedded mode)", () => {
  render(<BuildDetailPage detail={{
    build: { state: "success", system: "maven", classCount: 13104, jarCount: 279 },
    moduleSummary: { modulesTotal: 24, modulesBuilt: 21, modulesFailed: 1, modulesSkipped: 2,
                     modulesWithTestFailures: 2, buildSystems: ["maven"], singleModule: false },
    modules: [{ name: "connect:runtime", path: "connect/runtime", buildStatus: "failure",
                buildSource: "reactor", buildErrorSamples: ["[ERROR] cannot find symbol"] }],
  } as any} />)
  expect(screen.queryByRole("button", { name: /back/i })).not.toBeInTheDocument()
})
