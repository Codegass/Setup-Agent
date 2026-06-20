import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import type { ModuleSummary } from "@/api/types"

import { NeedsAttention } from "./NeedsAttention"

afterEach(() => cleanup())

function mod(overrides: Partial<ModuleSummary>): ModuleSummary {
  return {
    name: "m",
    path: "m",
    buildStatus: "success",
    buildSource: "reactor",
    testSource: "runner_xml",
    failingNames: [],
    failingCount: 0,
    ...overrides,
  }
}

describe("NeedsAttention", () => {
  it("groups failing tests by module and renders the failing names", () => {
    render(
      <NeedsAttention
        modules={[
          mod({
            name: "acme-cli",
            path: "modules/acme-cli",
            failingNames: ["cli.LoginTest.shouldA", "cli.LoginTest.shouldB"],
            failingCount: 2,
          }),
          mod({ name: "acme-core", path: "modules/acme-core" }),
        ]}
        warnings={[]}
      />,
    )
    expect(screen.getByText("acme-cli")).toBeInTheDocument()
    expect(screen.getByText(/2 failing/)).toBeInTheDocument()
    expect(screen.getByText("cli.LoginTest.shouldA")).toBeInTheDocument()
    expect(screen.getByText("cli.LoginTest.shouldB")).toBeInTheDocument()
    // a module with no failures is not listed
    expect(screen.queryByText("acme-core")).not.toBeInTheDocument()
  })

  it("collapses overflow past five names into a +N more pointer", () => {
    render(
      <NeedsAttention
        modules={[
          mod({
            name: "acme-cli",
            path: "modules/acme-cli",
            failingNames: ["a", "b", "c", "d", "e", "f", "g"],
            failingCount: 7,
          }),
        ]}
        warnings={[]}
      />,
    )
    expect(screen.getByText("a")).toBeInTheDocument()
    expect(screen.getByText("e")).toBeInTheDocument()
    expect(screen.queryByText("f")).not.toBeInTheDocument()
    expect(screen.getByText(/\+2 more/)).toBeInTheDocument()
  })

  it("renders a warning row per warning", () => {
    render(
      <NeedsAttention
        modules={[]}
        warnings={["3 deprecation warnings in acme-cli"]}
      />,
    )
    expect(screen.getByText("3 deprecation warnings in acme-cli")).toBeInTheDocument()
  })

  it("renders nothing when there are no failures and no warnings", () => {
    const { container } = render(
      <NeedsAttention modules={[mod({ name: "acme-core", path: "acme-core" })]} warnings={[]} />,
    )
    expect(container).toBeEmptyDOMElement()
  })
})
