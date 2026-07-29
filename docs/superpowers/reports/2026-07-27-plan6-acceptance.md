# Plan 6 (Build Contract Loop) — Acceptance Report

**Date:** 2026-07-27
**Design:** `specs/2026-07-26-build-contract-loop-design.md` (Chenhao's
design, second review passed). **Plan:**
`plans/2026-07-26-sagv2-plan6-build-contract-loop.md`, stages 0/A/B/C/D/E/F
all merged. **Final code:** main @ `a0a655a`. Full suite: **3,488 passed /
1 skipped / 0 failed**.

## What was built

The complete loop from the design: bounded document map with typed claims
(provenance can propose, only receipts prove); model intents frozen into
immutable invocation contracts before every runner dispatch; immutable
receipts with append-only assessments; a causal claim graph with grouped
transitions; typed targeted retrieval and reactive repair contracts;
material-progress retry authority; the provenance-gated native affordance.
About 470 new tests across the seven stages.

## Live battery — final graded runs

| Run | Session | Verdict | Assertions |
|---|---|---|---|
| cli r3 | `session_20260727_035638_85557` | SUCCESS — 982/921/0/61, **7th digit-identical** across six protocol generations | **9/9** |
| bigtop r4 | `session_20260727_041818_87528` | PARTIAL — primary exactly 50/50 | **13/13** |
| tvm r3 | `session_20260727_035643_85621` | FAILED (honest) — reached S2 | **18/18** |
| tvm r4 | `session_20260727_043115_89402` | FAILED (honest) — reached S2 | **15/15** |
| tvm r5 | `session_20260727_045159_91023` | FAILED (honest) — model declined the repair | **17/17** |
| tvm r6 | `session_20260727_050438_91699` | FAILED (honest) — reached S2, R2 first created live | **15/15** |
| tvm r7 | `session_20260727_052810_93090` | FAILED (honest) — R2 surfaced complete with the pin | **15/15** |

All armed Plan 6 assertions held live: every runner receipt chains to a
frozen contract whose hash recomputes; every failure receipt carries a
typed assessment; the survey's document map is persisted; receipts are
immutable; pairing is exact.

## The TVM state machine, live

The design's hardest requirement (spec §5) was the two-stage
evidence-triggered repair. What the recorded runs show:

- **S0→S1 (deterministic, 5/5 runs):** the bounded smoke runs, all three
  tests skip, no capability receipt is minted, the skip reasons are
  projected to the model, and a `capability_absent_llvm` assessment is
  written from the per-testcase facts.
- **R1 (deterministic, 5/5 runs):** a repair contract is created and
  surfaced, proposing `build(action='native', features=['llvm'],
  definitions={USE_LLVM: ON, BUILD_TESTING: OFF})` — with provenance
  pointing at claims extracted from the project's own CI/CMake files.
- **I1→S2 (2 of 4 post-R1 runs):** in r3 and r4 the model accepted the
  proposal; the native rebuild ran (allow-listed apt packages,
  `llvm-config` probe recorded 18.1.3, CMAKE_ARGS rebuild), and the smoke
  re-ran with the LLVM test now genuinely executing and failing on
  `ValueError: Could not convert T.float32 to a NumPy dtype` — the exact
  state the ground-truth review reached manually. In r5 the model saw the
  surfaced proposal twice and declined it; the run stayed honestly FAILED.
  The harness proposes with provenance; the model disposes.

  > ⚠️ **"Declined" is wrong (corrected 2026-07-29, while walking the polaris
  > transcripts for the same defect).** The proposal was
  > `build(action='native', definitions={"BUILD_TESTING":"OFF","USE_LLVM":"ON"},
  > features=["llvm"], ...)`. Immediately after seeing it the model called
  > `build(action='compile', args='-DUSE_LLVM=ON', ...)` (`envelope-000062`)
  > and then `build(action='compile', args='-DUSE_LLVM=ON -DBUILD_TESTING=ON',
  > ...)` (`envelope-000075`). It turned on exactly the switch the proposal
  > named, twice, within forty seconds. What it did not do was use the typed
  > `action='native'` verb, so exact-equality acceptance matched nothing and
  > the run recorded no acceptance at all.
  >
  > Exact equality stays — borrowing a proposal's provenance for a different
  > call is the self-attestation §C6 forbids, and r5's second attempt also
  > inverted `BUILD_TESTING`, so the calls are genuinely not the same intent.
  > But "the model disposes" describes a refusal that did not happen. The
  > model agreed and spelled it differently, and nothing in the run could tell
  > those two apart. Same family as p7/p7b polaris; see
  > `2026-07-29-plan7-round3-acceptance.md`.
- **S2→R2 (live, r6/r7):** the NumPy failure emits its distinct
  `dependency_incompatible_numpy` code from the per-testcase failure
  message; the project's own `numpy==1.26.*` pin — a backslash-continued
  line in a docker install script — is extracted as a real dependency
  claim; and in r7 the live repair proposal reads
  `build(action='deps', args="numpy==1.26.*")` with that claim as
  provenance, surfaced to the model after the failed smoke.
- **R2 acceptance → S3 (open):** the model has not yet accepted R2 in a
  live run (it closed the phase honestly instead). Every mechanical link
  is now live-proven; the full S0→S3 traversal is a weak-model choice
  with an observed R1 acceptance rate of 4 in 6 surfaced opportunities.

## What the battery caught (all fixed, all pinned by tests)

Twelve integration defects across seven graded rounds — every one found by
a machine assertion, none by reading logs:

1. Toolchain registration did not change the retry identity (the Maven
   version recovery was refused; the canary broke).
2. ControlAssessments were judged corrupt ReceiptAssessments and the
   fail-closed path blocked a green test phase.
3. Large document maps exceeded the kernel's per-argument bound and the
   write failed silently (now streamed as bounded base64 chunks).
4. Facade-external dispatches (tool-recovery delegate) ran without a
   frozen contract (now they freeze their own).
5. A completed `gradle dependencies` probe settled a blocked consumer
   domain green (inspection is not production).
6. The fallback contract froze the executable into `expected_argv` and
   graded its own compliant dispatch `deviated`.
7. Recovery-path receipts had contracts but no assessments (engine-seam
   backstop added).
8. Failed testcase nodes carried no message, so no distinct dependency
   failure could ever be typed.
9. The shell claim extractor read a pip pin continuation line as a
   mangled environment assignment.
10. Dependency retrieval ignored shell entries — where docker-era pins
    actually live.
11. The repair builder saw only newly-retrieved claims and was blind to
    the pin the survey had already persisted.
12. A multi-package docker install line defeated the single-pin rule and
    the R2 proposal carried no pin (the typed code's own subject now
    filters the set — a citation, not a choice).

## Acceptance state

- commons-cli **PASS** · bigtop **PASS** (truthful partial, scoped
  primary, blocked consumers never green, full contract chain) · TVM
  **harness-gate PASS** with the repair loop proven through S2 live and
  through R2 by machine-verified unit evidence on recorded data · native
  protocol **PASS**.
- Honest limits: the full S0→S3 live traversal depends on the weak model
  accepting two surfaced proposals in one run — observed acceptance rate
  so far is 4 of 6 opportunities for R1; R2 has been surfaced complete
  and not yet accepted. This is the designed boundary between harness authority
  (mechanical gates, receipts, retraction) and model authority (choosing
  among offered actions). The 23-project campaign will provide more
  acceptance-rate data.

## Category-3 compatibility

The first analyze output, phase intro and handoff carry coordinates,
constraints and open conflicts only (structural: domain facts never enter
the fact-sheet allowlist); repairs are reactive and evidence-triggered;
the name-policy guard verifies no project-name-conditioned code exists.
