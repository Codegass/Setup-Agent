import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
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

/** Two rows from `logs/recovery-v4-freemarker-mini-3-20260912`, as the API
 *  serves them. The plan filtered this list to `tool` in {maven, gradle, bash,
 *  python}, which is every tool the receipt validator accepts — 1,181 of 1,181
 *  archived receipts pass it — so the filter divided nothing. */
const receipts = [
  {
    receiptId: "inv-gradle-1-463aa590bbf7-0001",
    tool: "gradle",
    argv: "/workspace/freemarker/gradlew --continue clean build",
    workingDirectory: "/workspace/freemarker",
    actualCwd: "/workspace/freemarker",
    exitCode: 0,
    outcome: "completed",
    lifecycleState: "finished",
    toolchain: { executable: "/workspace/freemarker/gradlew", version: "Gradle 8.5" },
    jdkMajor: "17",
    jdkVersion: "17.0.20",
    reportsNew: 194,
    reportsChanged: 0,
    testsReported: null,
  },
  {
    receiptId: "inv-bash-native-463aa590bbf7-0005",
    tool: "bash",
    argv: "./freemarker-test-graalvm-native/build/native/nativeCompile/freemarker-test-graalvm-native",
    workingDirectory: "/workspace/freemarker",
    actualCwd: "/workspace/freemarker",
    exitCode: 0,
    outcome: "completed",
    lifecycleState: "finished",
    toolchain: null,
    jdkMajor: null,
    jdkVersion: null,
    reportsNew: 0,
    reportsChanged: 0,
    testsReported: null,
  },
]

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

  it("shows partial build evidence without inventing missing artifact or module totals", () => {
    render(<BuildFacet detail={{
      build: {
        state: "partial", tool: "sealed snapshot", time: "—",
        note: "Canonical build evidence from verdict.json", classCount: 16221,
      },
      modules: [],
    } as any} />)

    expect(screen.getByText("Partial")).toBeInTheDocument()
    expect(screen.getByText("16,221")).toBeInTheDocument()
    expect(screen.queryByText("JARs")).not.toBeInTheDocument()
    expect(screen.getByText(/detailed module metrics were not produced/i)).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /view build details/i })).not.toBeInTheDocument()
    expect(screen.queryByText("sealed snapshot")).not.toBeInTheDocument()
  })

  it("distinguishes measured zero outputs from unmeasured outputs", () => {
    const { rerender } = render(<BuildFacet detail={{
      build: { state: "failed", tool: "maven", time: "1s", note: "", classCount: 0 },
      modules: [],
    } as any} />)
    expect(screen.getByText("0")).toBeInTheDocument()
    expect(screen.queryByText(/artifact totals were not measured/i)).not.toBeInTheDocument()

    rerender(<BuildFacet detail={{
      build: { state: "unknown", tool: "—", time: "—", note: "" },
      modules: [],
      snapshotStatus: "valid",
    } as any} />)
    expect(screen.getByText(/artifact totals were not measured/i)).toBeInTheDocument()
  })

  // "not measured" is a finding. A run whose record this page could not read
  // was not measured and was not looked at, and only the second is known.
  it("does not say totals were unmeasured when the record was never read", () => {
    render(<BuildFacet detail={{
      build: { state: "unknown", tool: "—", time: "—", note: "" },
      modules: [],
      snapshotStatus: "untrusted",
    } as any} />)
    expect(screen.queryByText(/artifact totals were not measured/i)).toBeNull()
    expect(screen.getByText(/result record could not be read here/i)).toBeInTheDocument()
  })

  it("lists every command the run dispatched, with the toolchain each one used", () => {
    render(<BuildFacet detail={{ ...multi, receipts } as any} />)

    const table = screen.getByRole("table", {
      name: "Every command this run dispatched, with the toolchain it actually used.",
    })
    expect(within(table).getByText(/gradlew --continue clean build/)).toBeInTheDocument()
    // The bash row is in the same table. A `tool` filter would have dropped
    // nothing, and a `testsReported` filter would have dropped both.
    expect(within(table).getByText(/freemarker-test-graalvm-native$/)).toBeInTheDocument()
    expect(within(table).getByText("gradle Gradle 8.5")).toBeInTheDocument()
    expect(within(table).getByText("bash")).toBeInTheDocument()
  })

  it("says so when a run recorded no commands, rather than showing nothing", () => {
    render(<BuildFacet detail={multi} />)
    expect(screen.getByText("No commands were recorded for this run.")).toBeInTheDocument()
  })
})
