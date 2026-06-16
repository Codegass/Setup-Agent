import { describe, expect, it } from "vitest"

import { formatAgo } from "./relativeTime"

describe("formatAgo", () => {
  it("treats the last few seconds as 'just now'", () => {
    expect(formatAgo(0)).toBe("just now")
    expect(formatAgo(4_000)).toBe("just now")
  })

  it("formats seconds, minutes, and hours", () => {
    expect(formatAgo(5_000)).toBe("5s ago")
    expect(formatAgo(59_000)).toBe("59s ago")
    expect(formatAgo(60_000)).toBe("1m ago")
    expect(formatAgo(59 * 60_000)).toBe("59m ago")
    expect(formatAgo(60 * 60_000)).toBe("1h ago")
    expect(formatAgo(3 * 60 * 60_000)).toBe("3h ago")
  })

  it("never returns a negative value", () => {
    expect(formatAgo(-10_000)).toBe("just now")
  })
})
