import { describe, expect, it } from "vitest"

import { REASON_GLOSS, gloss, statusWord } from "./ciGlosses"

/** The Python leg is `tests/test_gloss_parity.py`, which holds this table's
 *  keys and sentences to `src/sag/result_card/glosses.py`. What it cannot
 *  reach is the behaviour of `gloss` itself, which is a second copy of
 *  `_sentence` in that module — so that is what is fenced here. */
describe("gloss", () => {
  it("reads a conflict by its family when the id carries a handle", () => {
    // `job_live_at_close:58db946542a8` is one fact plus one job handle. The
    // handle is what a bug report quotes; a reader of the Notes list can do
    // nothing with it, and 31 archived occurrences printed it raw.
    expect(gloss("job_live_at_close:58db946542a8")).toBe(gloss("job_live_at_close"))
    expect(gloss("job_terminal_unpersisted:083f87b9600d")).toBe(
      gloss("job_terminal_unpersisted"),
    )
    expect(
      gloss(
        "forced_test_attempt_nonreceipt:test-1:/workspace/ignite:maven:" +
          "candidate_mismatch:FORCED_TEST_CANDIDATE_MISMATCH",
      ),
    ).toBe(gloss("forced_test_attempt_nonreceipt"))
  })

  it("keeps the whole id of a family it does not know", () => {
    expect(gloss("never_seen_family:abc123")).toBe("never_seen_family:abc123")
    expect(gloss("SOME_CODE_WE_HAVE_NEVER_SEEN")).toBe("SOME_CODE_WE_HAVE_NEVER_SEEN")
  })

  it("reads the conflicts the archive records most often", () => {
    // Measured over the 681 archived verdicts: these five account for 221 of
    // the 303 occurrences that used to reach every surface as machine words.
    for (const code of [
      "maven_reactor_unverified",
      "metrics_conflict",
      "test_primary_coordinate_unresolved",
      "build_validation_failed",
      "acceptance_task_incomplete",
    ]) {
      expect(gloss(code)).not.toBe(code)
      expect(REASON_GLOSS[code]).toBeTruthy()
    }
  })
})

describe("statusWord", () => {
  it("says what happened rather than printing the record's word", () => {
    expect(statusWord("not_met")).toBe("not met")
    // A comparison that reached a CI job and produced no score. Not the same
    // fact as having no CI job to compare against, which reads "not compared".
    expect(statusWord("invalid")).toBe("not scored")
    expect(statusWord("invalid")).not.toBe("not compared")
  })
})
