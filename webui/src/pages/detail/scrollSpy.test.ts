import { act, renderHook } from "@testing-library/react"
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest"

import { pickActiveSection, useScrollSpy } from "./scrollSpy"

const positions = [
  { id: "build", top: 0 },
  { id: "test", top: 400 },
  { id: "flow", top: 900 },
]

describe("pickActiveSection", () => {
  it("returns the first section at the top", () => {
    expect(pickActiveSection(positions, 0, 170)).toBe("build")
  })

  it("advances as scroll passes each section's top (minus offset)", () => {
    expect(pickActiveSection(positions, 250, 170)).toBe("test") // 400-170=230 <= 250
    expect(pickActiveSection(positions, 720, 170)).toBe("test") // 900-170=730 > 720 -> still test
    expect(pickActiveSection(positions, 740, 170)).toBe("flow")
  })

  it("never returns past the last section and tolerates an empty list", () => {
    expect(pickActiveSection(positions, 99999, 170)).toBe("flow")
    expect(pickActiveSection([], 100, 170)).toBeNull()
  })
})

describe("useScrollSpy", () => {
  const ids = ["build", "test", "flow"]

  beforeAll(() => {
    // jsdom has no layout; stub so jump() doesn't throw.
    Element.prototype.scrollIntoView = vi.fn()
  })

  afterEach(() => {
    vi.restoreAllMocks()
    document.body.innerHTML = ""
  })

  it("resets the active section to the first when the session changes", () => {
    const { result, rerender } = renderHook(
      ({ sessionId }) => useScrollSpy(ids, sessionId),
      { initialProps: { sessionId: "CC-1" } },
    )
    expect(result.current.active).toBe("build")

    act(() => result.current.jump("flow"))
    expect(result.current.active).toBe("flow")

    // A re-render that keeps the same session must NOT reset (regression guard).
    rerender({ sessionId: "CC-1" })
    expect(result.current.active).toBe("flow")

    // Switching the session resets to the first section.
    rerender({ sessionId: "CC-2" })
    expect(result.current.active).toBe("build")
  })

  it("jumps to the requested initialFacet on mount / session change", () => {
    const el = document.createElement("section")
    el.id = "facet-report"
    document.body.appendChild(el)

    const { result } = renderHook(() =>
      useScrollSpy(["build", "report"], "CC-1", { initialFacet: "report" }),
    )
    expect(result.current.active).toBe("report")
    expect(el.scrollIntoView).toHaveBeenCalled()
  })
})
