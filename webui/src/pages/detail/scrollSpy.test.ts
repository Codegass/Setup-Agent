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

  it("marks a jumped section active under the real default offset", () => {
    // Regression guard: the default offset must cover a section's scroll-mt so a
    // section that lands ~150px below the container top after a jump is still
    // considered active by the next recompute (not the previous section).
    const SCROLL_MT = 150

    // Build three sections inside a scroll container so recompute can measure them.
    const container = document.createElement("div")
    const sections: HTMLElement[] = ids.map((id) => {
      const section = document.createElement("section")
      section.id = `facet-${id}`
      container.appendChild(section)
      return section
    })
    document.body.appendChild(container)

    // Container top sits at viewport 0; the scroll position is 0 (we jumped to a
    // section that scrolled to the very top, leaving its top at scroll-mt below).
    container.getBoundingClientRect = () => ({ top: 0 }) as DOMRect
    Object.defineProperty(container, "scrollTop", { value: 0, configurable: true })
    // Section absolute tops: build=0 is "above" by scroll-mt after a jump-to-top.
    // We simulate scrolling so the "test" section is the one jumped to: its
    // rect.top sits at exactly scroll-mt (150) from the container top.
    const rectTops = [-300, SCROLL_MT, 700] // build above, test jumped-to, flow below
    sections.forEach((section, i) => {
      section.getBoundingClientRect = () => ({ top: rectTops[i] }) as DOMRect
    })

    // Default offset (no offset option passed).
    const { result } = renderHook(() => useScrollSpy(ids, "CC-1"))

    // Attach the hook's container ref to our measurable container so recompute
    // can read scrollTop and measure section rects.
    result.current.containerRef.current = container

    act(() => result.current.onScroll())

    // With offset >= 150, "test" (top 150) satisfies 150 - offset <= 0 and is
    // marked active. A too-small offset (e.g. 24) would leave "build" active.
    expect(result.current.active).toBe("test")

    document.body.removeChild(container)
  })
})
