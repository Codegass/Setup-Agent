# Gradle receipt rows — r2 improvement plan

- **Date:** 2026-09-01
- **Basis:** the owner's hand-review of `worktree-gradle-receipt-rows`
  (5 commits, `36c6165..5b9f9f8`): design direction and coverage sound,
  semantically not mergeable. Three blockers — (1) the old exact-row path
  in `record_invocation` still seals unbounded rows and a kafka-scale
  delta bursts the whole receipt; (2) the new totals have writers but no
  consumers, so live metrics stay empty; (3) tier-1 totals are not
  SHA-bound at read time and no cross-field reconciliation exists
  (`tests=1, failures=2` is accepted) — plus the blind-spot list
  (task/dir allowlists, discovery-cap false absence, same-dir
  concurrency, schema compat, `tests_reported=0` as witness).
- **Execution:** r2 batch on the same branch, same ultracode shape;
  owner re-reviews before merge. No code changes accompany this plan.

## Three principles (the review's findings, elevated to contracts)

- **P-A. Totals are load-bearing; identities are a bounded sample.**
  Nothing unbounded may enter a receipt. Overflow degrades the identity
  sample (greens first, reds last, drops accounted) and NEVER the totals,
  the receipt, or its exit/argv/delta. A receipt that cannot hold its
  evidence discloses what it dropped; it never fails to exist.
- **P-B. Every count is bound to the bytes it counts.** A reader that
  sums a file verifies that file's digest against the delta's claim *in
  the same read*; a mismatch excludes the file and discloses a
  post-snapshot rewrite (this is also the same-workdir concurrency
  defense). The exact-row path already does this; tier-1 must too.
- **P-C. A receipt's numbers reconcile or the receipt is
  unconstructible.** Cross-field conservation is validated at build
  time; impossible data is an engine bug surfacing as a construction
  error, never a persisted contradiction.

## Tasks

### T1 — Bound the exact-row production path (blocker 1)

`read_delta_testcase_rows`' container program
(`receipt_test_rows.py:124`) gains hard bounds: per-file and total row
caps aligned with the harvest caps, red-first retention in the fold, and
exact drop accounting returned alongside the rows. In
`record_invocation` (`invocation_receipts.py:~2900`) the sealed-row and
diagnostic-row paths consume the bounded result; overflow withdraws
identities (disclosed), never totals or the receipt. A structural guard
makes the rows section's worst-case canonical size provably far below
`RECEIPT_MAX_CANONICAL_BYTES`.
**Flips the e2e proof**: the existing test showing 27,219 rows cannot
fit becomes the regression that a kafka-scale delta PERSISTS — full
totals, sampled identities, complete reds, exact drop counts.

### T2 — SHA-in-the-same-read for tier-1; claims come from the delta itself (blocker 3a + false-absence)

`_gradle_suite_head_entries` (`gradle_tool.py:392`) reads, per file in
one batched round trip, `sha256(whole file)` + bounded head bytes; the
engine compares each digest against the delta's claimed digest —
mismatch excludes the file with a `post_snapshot_rewrite` disclosure.
The claim set is enumerated **directly from `report_delta`'s own path
list** (it already names every claimed path and digest); the capped
discovery listing only feeds unclaimed-presence disclosure. Consequence:
`GRADLE_NO_CLAIMED_TEST_REPORTS` can only state what the delta proves
(empty delta), never what a 2,048-entry listing failed to name
(`gradle_tool.py:555` today).

### T3 — Receipt-level reconciliation (blocker 3b)

New build-time validators (`invocation_receipts.py:~1755` family):

- per-suite conservation: `failures + errors + skipped ≤ tests`
  (`tests=1, failures=2` unconstructible);
- `module_outcomes.tests_reported(module)` equals the sum of that
  module's suite totals;
- identity rows ⊆ the claimed report set; red row count ≤ summed
  failures+errors; `red_rows_complete` implied false whenever any bound
  that could hide a red fired;
- disclosure arithmetic exact: kept + dropped == observed, at every cap;
- one transport per receipt: a `gradle_row_disclosure` may only ride
  beside `testcase_outcomes` produced by the same source (hardening the
  r1 repair into a validator);
- `tests_reported ≥ 1` for any execution witness; `tests_reported = 0`
  with files present is its own disclosed state ("ran, found none"),
  never a witness.

### T4 — Remove task/dir allowlists (owner fix #4)

Any `test-results/<taskdir>/` directory counts, task name taken verbatim
from the path segment (hyphens included); the FROM-CACHE cached-roots
logic uses the same generic rule (cached `distributedTest` included).
No name regex, no allowlist anywhere in the harvest. Positive evidence
is decided by the report delta alone; the meaning of absence defers to
the sealed plan/contract test disposition where one exists (E1
`TestDisposition`), else discloses unknown. Regressions: cached
`distributedTest`, `smokeTest`, hyphenated task names.

### T5 — Wire totals into live consumers (blocker 2)

- `report_metrics`/report path: gradle receipts' suite totals feed
  `tests.claimed.receipt_executions` (counts complete even when
  identities are sampled); red identities come from the bounded red
  rows; availability reasons updated so the 2026-08-26 kafka shape
  ("current receipt testcase rows were unavailable") becomes a number.
- Certificate adapter (first live piece of MS-1 §18 step 7): an
  engine-side helper builds `TestCounts` + `test_results_authority`
  from receipt suite totals when the chain checks pass — callers never
  hand-author counts.
- Canonical verdict/WebUI read the counts through the existing
  report-metrics surfaces; the certificate panel itself stays shadow.

### T6 — Schema version bump (owner sealed 2026-09-01)

Owner decision: no compatibility machinery — sole-developer project.
`RECEIPT_SCHEMA_VERSION` bumps to 3, strict single-version equality
exactly as the codebase already validates; no dual-version readers, no
cross-version replay fixtures. Fixtures and tests move to 3 wholesale.

### T7 — Regression battery + hygiene (owner fix #6)

- kafka 27k persistence e2e (from the evidence tar's real counts);
- same-workdir concurrency: a claimed report rewritten between snapshot
  and read → excluded + disclosed, counts unpolluted (P-B probe);
- cached custom task; cap-only claim (delta-direct claims make the
  false-absence path unreachable — prove it);
- isort on the 3 unclean test files; remove the branch's one new mypy
  error.

## Scope fences

Maven path semantics unchanged beyond the shared reader's bounds; abort
semantics of integrity families untouched; receipts stay engine-written
only; no model-visible raw bytes. The d3 freeze, MS-1, and attainment
layers are consumers here, not modification targets (except the T5
adapter explicitly listed).

## Acceptance

The owner's review checklist, verbatim, plus: full suite green (d0
pre-existing excepted), the kafka e2e persists, and the same three
container evidence trees re-parsed through the new path produce the
same aggregate numbers the evidence report pinned (27,219/8/32,
10,435/0/22, 179/0/0).
