import { afterEach, describe, expect, it, vi } from "vitest"

import { readStored, writeStored } from "./safeStorage"

afterEach(() => vi.unstubAllGlobals())

describe("safeStorage", () => {
  it("reads and writes through a working localStorage", () => {
    const store = new Map<string, string>()
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
    })
    writeStored("k", "v")
    expect(readStored("k")).toBe("v")
  })

  it("returns null instead of throwing when localStorage is missing", () => {
    // The jsdom / SSR / private-mode case that crashed WorkspaceRail + App.
    vi.stubGlobal("localStorage", undefined)
    expect(() => readStored("k")).not.toThrow()
    expect(readStored("k")).toBeNull()
  })

  it("swallows write failures (quota / unavailable)", () => {
    vi.stubGlobal("localStorage", {
      getItem: () => null,
      setItem: () => {
        throw new DOMException("QuotaExceededError")
      },
    })
    expect(() => writeStored("k", "v")).not.toThrow()
  })
})
