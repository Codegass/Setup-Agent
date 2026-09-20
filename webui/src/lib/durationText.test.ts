import { describe, expect, it } from "vitest"

import { durationText } from "./durationText"

describe("durationText", () => {
  it("says a run's length the way the Setup row says it", () => {
    // The archived commons-cli run: 368.4298 seconds of wall clock, which the
    // card's Setup row prints as 6m 08s. The header line above it has to reach
    // the same eight characters from the same number.
    expect(durationText(368.4298)).toBe("6m 08s")
  })

  it("keeps a decimal under a minute and drops the seconds past an hour", () => {
    expect(durationText(41.04)).toBe("41.0s")
    expect(durationText(3859)).toBe("1h 04m")
  })

  it("pads the seconds so two runs of the same page line up", () => {
    expect(durationText(368)).toBe("6m 08s")
    expect(durationText(600)).toBe("10m 00s")
  })

  it("answers nothing for a length the run never measured", () => {
    expect(durationText(null)).toBeNull()
    expect(durationText(undefined)).toBeNull()
    expect(durationText(-1)).toBeNull()
    expect(durationText(Number.NaN)).toBeNull()
  })
})
