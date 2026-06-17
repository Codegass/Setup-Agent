import { describe, expect, it } from "vitest"

import { pickActiveSection } from "./scrollSpy"

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
