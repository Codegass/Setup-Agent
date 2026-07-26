# Plan 4 Machine-Verified Acceptance — Fact Projection Closed

> **⚠️ SECOND CORRECTION (2026-07-26, post ground-truth review).** The
> ground-truth review (`2026-07-26-three-project-harness-ground-truth-review.md`)
> falsifies two gradings below; both were verified against live evidence:
>
> 1. **"TVM harness-gate PASS" was too strong.** Full-sweep prevention PASS
>    stands (the bounded smoke demonstrably ran). But the all-skipped smoke
>    (`executed=3, skipped=3`) **minted a capability receipt**
>    (`native_smoke_receipt.json`, verified byte-identical to the review's
>    quote) — `python_tool.py`'s `executed >= 1` counts skipped nodes, so a
>    second bare call would have escaped the bounded gate on zero positive
>    evidence. The 7/7 verifier result is real but the assertion set is
>    incomplete: it never asserts all-skipped ⇒ no receipt. Re-grade:
>    full-sweep prevention **PASS**, capability receipt **FAIL**.
> 2. **Bigtop "54/54 primary-coordinate" was mislabeled.** The validator's
>    recursive XML scan (`physical_validator.py` `rglob("*.xml")`) aggregates
>    without invocation scope; manual container ground truth shows the
>    primary coordinate is exactly **50/50**, with 4 auxiliary
>    test-framework passes leaked in. The verifier's `>=50` anchor held by
>    luck, not by scoped semantics.
>
> Additionally verified from this review: the test-phase steer
> `NATIVE_NOT_BUILT_TEST_GUIDANCE` (react_engine.py) asserts "the NATIVE
> core was not built" from *build-phase outcome alone* — five native `.so`
> files existed in the container. The claim below that TVM's verdict was
> "honest" is therefore wrong at the root-cause layer: the skip facts were
> honest, the projected native-state diagnosis was false.

**Date:** 2026-07-26
**Code under test:** main @ `4b5ad6f` (Plan 4 complete: Tasks 1–5 + sealed-chain reviewer fix + verifier)
**Verifier:** `scripts/verify_native_test_policy.py` — every claim below is a
machine assertion over recorded transcripts, not a human log reading. The
verifier was negative-controlled first: run against the Plan-3 TVM session
it correctly FAILs on the audit's exact finding (`scope='full'` without a
receipt).
**Supersedes:** the "TVM PENDING" state in the CORRECTION block of
`2026-07-26-sagv2-final-acceptance.md`.

## Battery (all `--record`, same pins as every baseline)

| Run | Session | Verdict | Machine assertions |
|---|---|---|---|
| commons-cli | `session_20260726_153129_67815` | SUCCESS — 982/921/0/61 (4th digit-identical) | **5/5 PASS** |
| bigtop | `session_20260726_153131_67860` | PARTIAL — 54/54 primary-coordinate (3rd reproduction) | **4/4 PASS** |
| tvm r1 | `session_20260726_153134_67903` | FAILED (honest) — 3 unique / 3 skipped / 0 errors | **7/7 PASS** |
| tvm r2 | `session_20260726_153136_67939` | FAILED (honest) — identical | **7/7 PASS** |

**23/23 assertions passed**, including for each TVM run: pytest ran with
`collection_scope == "filtered"`, the command contains the exact surveyed
smoke path (`tests/python/all-platform-minimal-test`), selected count within
1..50, zero unfiltered collects without a capability receipt, pairing exact,
every envelope hash recomputed byte-identically, zero scheduler-era events.

## What changed on TVM, in one line each

| | Before Plan 4 (falsified report) | After Plan 4 (machine-verified) |
|---|---|---|
| Scope | bare `pytest`, `collected=11702` full sweep | surveyed smoke, filtered, ≤50 selected |
| Counts | 28 collection-error nodes reported as "56 tests executed" | 3 real smoke tests, honestly all skipped, `collection_errors` a first-class sealed fact |
| Root cause | misread as missing `libtvm_ffi.so` | structured summary sealed and projected (LLVM capability absent) |
| Guard | readiness probe disabled the smoke defense | capability receipt required before any full sweep |

## Acceptance state (final)

- commons-cli **PASS** · bigtop **PASS** · native protocol **PASS** ·
  **TVM harness-gate PASS** — the run's verdict is honestly FAILED because
  the environment lacks the LLVM toolchain the smoke tests require (all 3
  skip); producing that *trustworthy* failed verdict, bounded and correctly
  attributed, is exactly SAG's product. Remaining work (LLVM provisioning
  for TVM-class projects) is a capability roadmap item, not a harness
  defect. · advisor **mechanism PASS / efficacy unproven** (unchanged;
  consult-at-entry removed the known batch-cancellation cost).

## Plan 4 closure vs the audit's six recommendations

1. Acceptance re-graded — done in the correction block, finalized here.
2. Two-layer native status + smoke-first + capability receipt — Task 1,
   verified live by both TVM runs.
3. Collection failures ≠ executed tests — Task 2 + sealed-chain fix,
   visible in the honest 3/3-skipped verdicts.
4. Collection facts projected to model / advisor / report / sealed verdict —
   Tasks 1/3/5 + reviewer fix (`43ee328`).
5. Advisor batch-cancellation fixed via consult-at-entry — Task 5 (spec
   §3.2 amended); bigtop 54/54 reproduced post-change.
6. Machine-asserted re-verification — this battery, via the
   negative-controlled verifier.

Full suite at `4b5ad6f`: **2,602 passed / 1 skipped / 0 failed**.
