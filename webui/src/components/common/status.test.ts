import { describe, expect, it } from "vitest"

import { isUsefulEvidenceStatus, statusMeta } from "./status"

describe("statusMeta", () => {
  it("matches the SAG demo status tones", () => {
    expect(statusMeta("success").tone).toBe("green")
    expect(statusMeta("partial").tone).toBe("amber")
    expect(statusMeta("running").tone).toBe("blue")
    expect(statusMeta("failed").tone).toBe("red")
    expect(statusMeta("unknown").tone).toBe("neutral")
  })

  it("keeps demo label semantics for common execution states", () => {
    expect(statusMeta("passed")).toEqual({ label: "Passed", tone: "green" })
    expect(statusMeta("pending")).toEqual({ label: "Pending", tone: "neutral" })
    expect(statusMeta("none")).toEqual({ label: "—", tone: "neutral" })
    expect(statusMeta("fail")).toEqual({ label: "Failed", tone: "red" })
    expect(statusMeta("conflict")).toEqual({ label: "Conflict", tone: "red" })
    expect(statusMeta("unknown")).toEqual({ label: "Unknown", tone: "neutral" })
  })

  it("normalizes unknown status labels without changing their tone", () => {
    expect(statusMeta("waiting-room")).toEqual({
      label: "Waiting-room",
      tone: "neutral",
    })
  })

  it("suppresses default evidence statuses from noisy surfaces", () => {
    expect(isUsefulEvidenceStatus("unknown")).toBe(false)
    expect(isUsefulEvidenceStatus(null)).toBe(false)
    expect(isUsefulEvidenceStatus("partial")).toBe(true)
    expect(isUsefulEvidenceStatus("conflict")).toBe(true)
  })
})
