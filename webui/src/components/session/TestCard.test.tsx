import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { TestCard } from "./TestCard"

describe("TestCard", () => {
  afterEach(() => {
    cleanup()
  })

  it("prefers backend pass and execution rates when present", () => {
    render(
      <TestCard
        test={{
          state: "partial",
          pass: 39,
          fail: 1,
          skip: 0,
          total: 40,
          passRate: 97.5,
          executionRate: 88,
        }}
      />,
    )

    expect(screen.getByText("97.5% pass rate")).toBeInTheDocument()
    expect(screen.getByText("88% executed")).toBeInTheDocument()
  })

  it("falls back to a computed pass rate when the backend omits it", () => {
    render(
      <TestCard
        test={{
          state: "success",
          pass: 3,
          fail: 1,
          skip: 0,
          total: 4,
        }}
      />,
    )

    expect(screen.getByText("75% pass rate")).toBeInTheDocument()
  })
})
