# Gate truth and receipt scope — the scorekeeper stops lying by omission

**Date:** 2026-08-14
**Status:** design decision, ready to implement (tasks #48/#49/#50)
**Evidence:** D2-r2 slice corpus (commit 2e9ebea), byte-verified. Every claim
below carries its slice locator.

The campaign's three highest-impact harness defects share one disease: the
layer between physical evidence and the sealed verdict speaks with more than
one voice. This spec sets three contracts that reduce it to one.

## 1. Fix A — receipt-scoped counting must not zero real executions (#48, merges #39)

### Convicting evidence

- geode: two `./gradlew test` runs ended BUILD SUCCESSFUL;
  `auxiliary_test_stats` recorded executed 10,448 / passed 10,422; the sealed
  `test_stats.raw/unique` recorded **0**, `rates.test.cases` = 0/9754 band
  `none` (geode.md §0, §2.101).
- freemarker / httpcomponents-client: the decisive test dispatch ran in the
  BUILD phase; its receipt exists and the counts matched baseline, yet the
  test phase closed `test_candidate_resolution_unavailable`
  (freemarker.md / httpcomponents-client.md, final gates).
- polaris: `discovered: 1347` while `test_catalog_summary.by_module` totals
  593 (polaris.md Slice 1) — the denominator side of the same question,
  tracked by #39's denominator rules and out of scope here beyond what §1.3
  states.

### What stays (deliberately)

Receipt scoping exists to stop fabricated or stale reports from counting:
a report XML only enters the headline count when its content hash is claimed
by a terminal invocation receipt. That anti-fabrication property is correct
and stays. P4 still holds: nothing here removes evidence.

### Contract

1. **Run-wide receipts.** The verified-claims input to the report scan
   (`physical_validator._verified_report_claims(records, primary_root)`)
   admits terminal invocation receipts from ANY phase of the current run —
   a `build(action=test)` receipt harvested during the build phase binds its
   reports exactly as a test-phase receipt does. Receipts remain run-scoped
   (never from a previous run; the hash claim already enforces content
   identity).
2. **Gate close consults run-wide receipts.** The engine's close path may
   only emit `test_candidate_resolution_unavailable` after the run-wide
   receipt set is empty of test-bearing receipts. freemarker's shape — counts
   sealed from a build-phase receipt, close code says "unavailable" — becomes
   unconstructible: with such a receipt present the close grades execution
   (`test_execution_observed`).
3. **Unattributed executions become a named conflict, not a silent zero.**
   When `auxiliary_test_stats.executed > 0` and the headline executed count
   is 0, the verdict MUST carry conflict
   `test_executions_unattributed_to_receipts` and the rates block MUST state
   the excluded volume in the cases grain's reason (e.g. `0/9754 — 10,448
   executions visible on disk but bound to no receipt`). Auxiliary counts
   still do not enter the headline number: visibility without authority.
4. **Forensic gate before implementation.** The implementing change must
   first reproduce, from the archived geode session host artifacts
   (`logs/session_20260813_193300_102527_767e4d983969_99841/.setup_agent/`),
   the exact drop point: did the two gradlew receipts exist with report
   claims, and which paths did they claim vs. which report dirs the scan
   found? If the dropper is a claims-production bug (e.g. claims limited to
   the primary coordinate's directory while reports landed across module
   dirs), fix the claims producer as part of this task; if the dropper is
   phase-scoping of `records`, item 1 already covers it. The finding is
   recorded in the implementation commit message.

### Acceptance

- A fixture with a build-phase test receipt and its reports: headline count
  admits them; test close grades `test_execution_observed`.
- A fixture with on-disk reports bound to no receipt: headline 0, conflict
  `test_executions_unattributed_to_receipts` present, reason names the
  volume.
- Existing anti-fabrication fences stay green (stale/claimed-changed reports
  still excluded).

### Amendment 2026-08-14 — counting monotonicity (owner decision, task #48 follow-up)

The first implementation of §1 was faithful to items 1-3 and still carried two
non-monotone edges that the review found. Both are closed here; items 1, 2 and
4 above stand, item 3's trigger is superseded.

1. **Universal claim scoping.** The verified/auxiliary/stale partition applies
   REGARDLESS of receipt presence. The `receipt_scoped = bool(records)` arming
   is removed: a run with zero receipts and reports on disk seals headline 0 +
   auxiliary counts + the unattributed conflict, exactly like geode. Before
   this, arming on receipt PRESENCE while excluding on report CLAIMS meant a
   run with ONE compile receipt sealed 0 while the SAME corpus with the ledger
   removed sealed all of it — deleting attributed evidence improved the number,
   P4 inverted. The gradient is now monotone: adding a receipt can only move
   reports from auxiliary to headline; removing one can never improve any
   sealed number. `receipt_scoped` stays in the sealed schema and is CONSTANT
   for that parser; its absence in a rollup now means only that the counts came
   from the unpartitioned shell fallback (which still cannot close a phase —
   `test_receipt_missing`).
2. **Monotone disclosure.** §1 item 3's trigger ("headline is 0") is replaced
   by "auxiliary executed > 0". A nonzero headline never switches the conflict
   or the sentence off, because one receipted test would otherwise erase both
   the conflict and every trace of the volume standing beside it. The sentence
   names both numbers (`1/9754 — 10,448 executions visible on disk but bound to
   no receipt`; without a denominator, `50 executed, static discovery found no
   count — 4 executions …`). The conflict remains a capping conflict, so a run
   that cannot attribute part of what it sees is capped at partial.
3. **Pre-flight (mandatory, recorded).** Python/pytest test receipts DO carry
   `report_delta` claims for their junitxml: `python_tool` brackets the run with
   `snapshot_reports([working_directory, PYTEST_REPORT_DIR])` and takes the
   after-snapshot AFTER the attempt tagger rewrites the XML, so the claimed
   hash is the byte content the validator later reads. Fenced by
   `tests/test_python_tool.py::test_pytest_receipt_claims_the_junitxml_it_wrote`.
   No claims-producer fix was needed, and universal scoping does not zero
   Python projects.
4. **The unattributed conflict is NON-CAPPING (adjudicated).** Item 2 above
   made the disclosure monotone on the RECEIPT axis and left it non-monotone on
   the REPORT axis: on bigtop's shape a headline of 50 attributed cases beside 4
   unclaimed reports capped the run at `partial`, and deleting those four XML
   files lifted the cap to `success`. Evidence PRESENCE worsened the verdict —
   the same P4 inversion as the arming asymmetry, entering from the other side.
   `test_executions_unattributed_to_receipts` therefore joins
   `verdict.ADJUDICATED_CONFLICTS`: it is named, it is disclosed, and it does not
   cap. Rationale, in the order it was decided:
   - the headline band already derives from ATTRIBUTED counts alone, so the
     unattributed volume has no second claim on the verdict to make;
   - stray unclaimed XML may legitimately pre-date the checkout (a vendored
     sample, a cached report from an image layer), which is not the run's doing
     and not something the run can repair;
   - non-capping is the only treatment monotone on the REPORT axis — adding or
     deleting an unattributed report moves the disclosure and never the word.
     (As first written this bullet claimed monotonicity on BOTH axes. That was
     false and item 7 corrects it: the same change opened a receipt-axis
     gradient through the OTHER excluded door.)
   The conflict and its disclosure sentence are otherwise unchanged: visibility
   without authority, now also without a cap. A genuine evidence conflict
   standing beside it (`test_report_parse_error`) still caps exactly as before;
   `test_reports_stale` is settled by item 7. (That exemplar is superseded by
   item 12 — an unreadable report grades nothing either. The conflict that
   still caps beside this one is `test_receipt_unreadable`.)
5. **The stale destination is disclosed.** Reports leave the headline through
   two doors and they mean different things: AUXILIARY is claimed by nobody,
   STALE was claimed and the bytes were then rewritten. Only the first was ever
   spoken aloud, so a superseded-sha claim silently dropped its volume and a
   rewritten report read exactly like a report that never existed. The claim
   partition now counts stale reports on the same pass it counts auxiliary ones
   (`stale_test_stats`, beside the existing `stale_test_reports` paths) and the
   cases grain names them separately — `50/100 — 4 executions visible on disk
   but bound to no receipt, 6 under rewritten claims`. Stale volume is still
   never counted, and it still rides the existing `test_reports_stale` conflict
   rather than the unattributed one. (How that conflict grades is item 7.)
6. **Fallback parity.** When the compact in-container parser cannot run (no
   `python3` in the image) the shell find/cat rescan produces counts with no
   claim partition behind them. Sealing those as the HEADLINE meant that, for a
   zero-receipt run, the compact parser sealed headline 0 + auxiliary N while
   the fallback sealed N — the two paths disagreed by the entire corpus, and the
   higher number came from the LESS machinery. The finalizer now routes a rollup
   WITHOUT the `receipt_scoped` marker (i.e. fallback-produced) to unattributed
   volume: headline 0, the counts disclosed through the same conflict and the
   same sentence. The counts are moved, never deleted. The phase-close refusal
   is unchanged — an unpartitioned rollup still closes nothing
   (`test_receipt_missing`) — and the deliberate shell-fallback fences in
   `tests/test_pytest_report_aggregation.py` still test that path's AGGREGATION.
7. **Both excluded doors are graded alike (adjudicated).** Item 4 closed the
   report axis and, by leaving `test_reports_stale` capping, opened the receipt
   axis: a report the receipts claim and whose bytes were then rewritten lands
   in the STALE bucket and capped; the SAME file with that receipt absent lands
   in the AUXILIARY bucket and did not. Demonstrated end to end through the real
   parser on real files — one workspace, headline 25 either way, 8 excluded
   executions either way — `partial` with the receipt, `success` without it. So
   DELETING a receipt lifted the word, which is P4 read straight off the page:
   "removing evidence must never improve a verdict … discarding a receipt,
   failing to read one … may make a verdict less certain. It may never make it
   better" (2026-07-29 evidence-lifecycle spec). The gradient is new here: at
   the parent commit both classifications capped.
   The two doors are therefore ONE class — `verdict_rates.EXCLUDED_VOLUME_CONFLICTS`
   = {`test_executions_unattributed_to_receipts`, `test_reports_stale`} — folded
   into `verdict.ADJUDICATED_CONFLICTS`. Rationale, in the order it was decided:
   - item 4's first bullet applies verbatim to stale: the headline band derives
     from ATTRIBUTED counts alone, and stale volume is never counted into it, so
     it has no second claim on the verdict to make;
   - the cap defended nothing it was reached for. Anti-fabrication is EXCLUSION,
     which is untouched. A run that wants the volume uncapped only ever had to
     dispatch through a non-receipting tool from the START (auxiliary, already
     uncapped) — so the cap could only tax the run that used the receipting tool
     FIRST, which is the inverted incentive P4 exists to kill;
   - what stale has that auxiliary lacks — the run itself wrote both statements —
     is a DISCLOSURE fact, and it is fully disclosed: its own conflict name, its
     own paths (`stale_test_reports`), its own counts (`stale_test_stats`), its
     own clause in the cases grain ("6 under rewritten claims"), and its own
     metrics bucket (`receipt_claim_superseded`). It is graded by nobody, which
     is exactly this commit's rule for excluded evidence.
   The cap stays reserved for uncertainty about evidence the harness could not
   READ (`test_report_parse_error`, `test_receipt_unreadable`) — never for bytes
   it read, measured and deliberately left out of the numerator. (Item 12
   narrows this line: `test_report_parse_error` leaves the reservation, because
   bytes nobody could read can be DELETED and a deleted claimed report says
   nothing at all. `test_receipt_unreadable` stays — removing the ledger takes
   the headline's authority with it.) Consequence,
   named: like the unattributed conflict, `test_reports_stale` no longer renders
   as an operator BLOCKER line (`report_tool._sealed_failure_blockers` skips adjudicated
   conflicts); the five disclosures above carry it. Fenced by
   `tests/test_run_wide_receipt_scope.py::test_deleting_the_receipt_that_revealed_a_rewrite_never_changes_the_word`
   (real parser, real files, real rollup, real kernel) and
   `::test_the_stale_conflict_is_adjudicated_not_capping`.

8. **The parse-error CAP follows attribution, not the scan (adjudicated).**
   `excluded_counts` parsed the auxiliary and stale files through the same
   `parse_report` that feeds `parsing_errors`, so any XML exception anywhere in
   the corpus minted `test_report_parse_error` — the CAPPING conflict reserved
   for evidence the harness could not read. An UNPARSEABLE unclaimed report
   therefore capped a run that a PARSEABLE one did not, and `rm` on one corrupt
   stray XML lifted the word: P4 inverted again, through the last door left
   open. Parse failures of EXCLUDED files (auxiliary and stale) now leave
   through their own channel: counted per destination as `unparseable`,
   disclosed inside that destination's clause ("… 4 executions visible on disk
   but bound to no receipt (1 unparseable report)"), never counted into any
   volume and never capping. Parse failures of ATTRIBUTED reports keep today's
   capping semantics unchanged — a receipt claimed those exact bytes, so a
   broken claimed report is a genuine conflict between the run's own
   statements. Both directions are fenced, and the metrics-v2 excluded buckets
   state an explicit UNMEASURED marker (not `executed: 0`) where the volume
   could not be parsed at all: zero is a count, and an unreadable report has
   none. (The ATTRIBUTED half of this item is RETRACTED by item 12: keeping
   that cap left the same P4 inversion on both axes, and the "conflict between
   the run's own statements" predicate is the one item 7 had already ruled a
   DISCLOSURE fact for the strictly stronger stale contradiction.)
9. **The observation fold is unattributed volume too (adjudicated).** When NO
   `test.stats` fact exists, `_fold_test_stats` falls through to the
   tool-observation fold, which sealed `result.test_stats` as the FULL headline
   with no partition, no conflict and no sentence — less machinery, a bigger
   number, and counts that came from a runner's console text, which is exactly
   the render-layer authority this repo's principles refuse. It is the same
   inversion item 6 closed for the shell rescan, entering through the third
   door. The fold now routes its counts to unattributed volume identically:
   headline 0, the same `test_executions_unattributed_to_receipts` disclosure,
   and a sentence that names its provenance
   (`0/9754 — 10,448 executions reported in tool output but bound to no
   receipt`, via `SnapshotTestStats.unattributed_source`). The selection
   machinery still runs and still decides what it can honestly decide — the
   `discovered` denominator and the `test_stats_basis_incomparable` frontier
   conflict. The phase-close refusal is unchanged: no `receipt_scoped` marker is
   invented, so these counts still close nothing.
   Consequence, named: a run whose only test evidence is a tool result can no
   longer seal `success`. Fixtures that depended on an observation-fold headline
   were rebased on the provenance a live dispatch states (a receipt-scoped
   rollup) or onto the destination the volume now lands in, and the three
   affected `tests/fixtures/control_layer` transcripts had their
   `expected_snapshot` refreshed. Their recorded bytes — event rows,
   `source_manifest` hashes and `expected_event_digest` — are untouched and
   byte-verified unchanged; what moved is the harness's declared OUTPUT for
   those bytes, which is what those fixtures exist to pin.
10. **A zeroed count takes its derived facts with it.** Item 6's fallback-parity
    zeroing left `test_failures_detected` / `test_errors_detected` and a
    `flaky_count` standing beside a headline of 0/0/0 — statements about counts
    the snapshot no longer carries. Conflicts DERIVED from the headline counts,
    and the flaky tally computed over the identities they named, are dropped
    with them. Conflicts from any other basis (`metrics_conflict`,
    `test_report_parse_error`, the stale conflict) are untouched. A fact must
    not outlive its basis.
11. **One close, one survey read.** A single engine close asked
    `resolve_survey_test_candidates` up to three times — once for the missing
    attempt requirement, once for the unresolved-coordinate cap, once for the
    forced refusals — each a manifest read plus realpath probes. Three reads of
    one survey can disagree, and then the requirement, the cap and the refusals
    answer different coordinates while the record shows one close.
    `ReActEngine._test_candidate_survey()` returns the one lazily-memoized
    reader a close threads through all three (the same shape
    `attempt_policy.test_closure_survey` gave `phase_tool`), so the read happens
    at most once and still only if the close actually asks (P3).
12. **An unreadable report is disclosed, never graded (adjudicated).** Item 8
    closed the parse-error cap on the REPORT axis for excluded files and left it
    armed for ATTRIBUTED ones, which made the cap follow the receipt. Both
    inversions it left are reproduced end to end through the real parser on real
    files — one corpus, headline 25 in every arm, one corrupt report either way:
    - RECEIPT axis: with a receipt claiming the corrupt bytes the file is
      `verified`, its exception lands in `parsing_errors`, mints
      `test_report_parse_error` and seals `partial`. Delete that receipt and the
      SAME bytes land in `auxiliary` with `unparseable: 1`, no capping conflict,
      `success`. Deleting a receipt improved the sealed word.
    - REPORT axis, inside the attributed channel: `rm` on that same claimed
      corrupt file leaves NO trace at all — `content_sha256` returns None, the
      claim attributes nothing, the scan never sees the file — so the run seals
      `success` with every counted execution unchanged. Deleting a report
      improved the sealed word.
    The second arm settles the owner call, because it rules out the alternative
    resolution (keep the cap, name the gradient): the cap is non-monotone even
    with attribution held fixed. Generally — an unreadable report can ALWAYS be
    deleted, and a deleted claimed report is indistinguishable from one that
    never existed, so NO treatment in which an unreadable report caps is monotone
    on the report axis. Capping on one can only pay a run for `rm`. Item 7's
    second bullet applies verbatim besides: a truncated report produced through
    `bash` sealed `success` while the identical truncated report produced through
    the receipting tool sealed `partial`.
    `test_report_parse_error` therefore joins `verdict.ADJUDICATED_CONFLICTS`,
    through the named set `verdict_rates.UNCOUNTED_REPORT_CONFLICTS` =
    `EXCLUDED_VOLUME_CONFLICTS` + `{test_report_parse_error}` — every report fact
    a run can add or remove without changing what it EXECUTED. Item 8's
    rationale for the attributed cap ("a receipt claimed those exact bytes, so a
    broken claimed report is a genuine conflict between the run's own
    statements") is retracted: it is the same predicate item 7 already ruled a
    DISCLOSURE fact for `test_reports_stale`, where the contradiction is strictly
    stronger (claimed hash H, bytes are now H') and does not cap.
    The price item 7 set for un-capping is paid in full — a THIRD door out of the
    headline, named and disclosed like the other two rather than folded into one
    that would misdescribe it. AUXILIARY is claimed by nobody, STALE was claimed
    and rewritten, UNMEASURED is claimed by a still-matching receipt and could not
    be read: its own paths (`unmeasured_test_reports`), its own count
    (`unmeasured_test_stats`, `unparseable` only — there is no volume to state and
    zeros would be a measured outcome for bytes nobody measured), its own clause
    in the cases grain ("1 unparseable report under receipt claims"), its own
    conflict name, and the `parsing_errors` message naming the file. Consequence,
    named: like the other two, `test_report_parse_error` no longer renders as an
    operator BLOCKER line (`report_tool._sealed_failure_blockers` skips
    adjudicated conflicts); those five disclosures carry it. The cap now stays
    where deletion cannot buy it — an evidence-CLOSURE failure whose removal takes
    the headline's authority with it (`test_receipt_unreadable`: with no ledger
    nothing is attributed and the headline is 0). Every fence that used
    `test_report_parse_error` as its "still caps" exemplar was rebased onto
    `test_receipt_unreadable` with the reason recorded inline. Fenced by
    `tests/test_run_wide_receipt_scope.py::
    test_deleting_the_receipt_that_claimed_a_corrupt_report_never_changes_the_word`,
    `::test_deleting_the_corrupt_report_a_receipt_claims_never_changes_the_word`,
    `::test_the_unreadable_report_conflict_is_adjudicated_not_capping`,
    `::test_an_unreadable_claimed_report_is_counted_and_pathed` (parser → rollup →
    sealed snapshot → sentence), `::test_the_disclosure_names_the_reports_under_
    intact_claims` and `::test_all_three_doors_are_named_in_one_sentence`.

### Deferred items (2026-08-14, named so they are not invisible)

Six known gaps are accepted for now rather than silently carried:

1. **Task #52 — `run_test_receipts` /workspace fallback bound.**
   `attempt_policy.run_test_receipts` falls back to the entire `/workspace` when
   `project_root` is None, which is exactly the state the predicate is consulted
   in (`manifest_unreadable` / `coordinates_missing` are the statuses that most
   often trigger the close). A terminal test dispatch in a sibling checkout or a
   vendored sample under `/workspace` therefore satisfies the run-wide question.
   Bounded and low-likelihood on single-project containers, but the WIDEST
   boundary is applied precisely where the survey is least trustworthy. Tracked
   as task #52; not fixed here because narrowing it needs a boundary the
   unreadable-manifest state does not have.
2. **The panel-lock fence skips when its evidence is absent.**
   `tests/test_category3_panel_registry.py` skips eight data assertions when
   `logs/panel-category3/panel-lock.json` is missing from a checkout. Absent
   evidence skipping with a visible reason is the accepted behaviour for now —
   it is not read as a pass — but the fence does go quiet exactly when its
   evidence disappears. Revisit if the lock regains a committed source of truth;
   the fix is to commit the lock (or gate the skip on an explicit opt-out), not
   to assert over data that is not there.
3. **Three invented cut-offs remain in `_render_issues_recommendations`** —
   found while retiring the five markers, recorded rather than silently left.
   `report_tool.py:5151/5161/5175` still select operator PROSE at `>= 95`
   ("High Pass Rate"), `< 90` ("Low Execution Rate") and `< 80` ("Incomplete
   Coverage"). They pick a sentence rather than grade an outcome, and each
   carries a wording decision the marker rule does not settle (what "low"
   should mean once the boundary is the band table, and whether an existing
   `"Low Execution Rate"` line may be reworded — `tests/test_provision_priority
   .py:296` reads it). Out of scope for the marker decision; the same treatment
   applies when they are addressed. Re-confirmed 2026-08-14 (round 3): these
   three ICON+PROSE boundaries are the named residue carried into task #38, and
   nothing in this round touched them.
4. **`rate_marker`'s strict-100 ✅ is deliberate, not an off-by-one.** A 99.9%
   pass rate renders ⚠️, and that is the intended reading: the missing 0.1% is
   red tests, red tests are exact sealed facts, and a report that decorates
   "3,568 passed, 2 failed, 1 skipped" with a green check is the kafka
   sentence in icon form ("Tests passed above the 80% threshold: 99.9%"). ✅ is
   reserved for complete; ⚠️ says *something did not pass* without grading how
   much; ❌ keeps the one documented boundary (`HALF_FLOOR`, the heavy-red
   rule). Recorded here because "95 and 100 render differently" reads like a
   bug to every next reader, and it is a decision.
5. **The unmeasured door has no metrics-v2 bucket.** Item 12's third
   destination reaches the sealed snapshot, the rates sentence and the report
   snapshot's `test_analysis`, but `report_metrics._project_tests` still
   publishes exactly three observation buckets (`quarantined_observations`,
   `unattributed_observations`, `stale_observations`). A fourth is a metrics-v2
   SCHEMA change — the required-key lists at `report_metrics.py:79/292`, the
   `sag.web.models` read model, the renderer at `:1267` and the golden battery
   all pin those names — and this round changed no published schema. Nothing
   REGRESSED: the claimed bucket never stated how many of its reports were
   unreadable, before or after. Named because item 8's own text promised the
   excluded buckets an UNMEASURED marker, and the door added after it has none.
6. **The shell-fallback path names its unreadable files without counting them.**
   `parsing_errors` in the find/cat rescan (`physical_validator.py:1935/1943`)
   still mints `test_report_parse_error`, which now grades nothing there too —
   so item 12 closes that path's gradient as well. What it does not gain is the
   compact parser's COUNTED disclosure: the fallback partitions nothing (item
   6), so there is no destination to attach an `unparseable` tally to and the
   conflict name plus the message carry it alone. Bounded to images without
   `python3`, where the rollup already closes no phase.

## 2. Fix B — the 80% thresholds leave the gate/claim chain (#49)

### Convicting evidence

- kafka: model claimed `partial`; gate upgraded to `success`, reason
  *"Tests passed above the 80% threshold: 3568/3571 (99.9%)"* (kafka.md S8).
- geode: *"Tests below the 80% pass threshold: 0/0 (0.0%)"* (geode.md §2).
- ignite: reason *"Tests below the 80% pass threshold: 29/37 (78.4%)"*
  sealed alongside `validator_state: "green"`, claim upgraded to `success`
  (ignite.md S12) — the reason text and the outcome point in opposite
  directions.

v4 (rate-banded verdict, approved 2026-08-10) retired the invented 80% from
the verdict chain. These are the surviving sites upstream of it.

### Contract

1. **No pass-rate adjudication.** Remove `DEFAULT_TEST_PASS_THRESHOLD` and
   `DEFAULT_TEST_EXECUTION_THRESHOLD` from every claim-validation and
   outcome-derivation path:
   - `physical_validator.py:5381/5387` reason strings and the branch that
     selects them; the `test_pass_threshold`/`test_execution_threshold`
     parameters at 1024/1100 and their pass-throughs
     (`verdict_finalizer.py:1336-1363`, `agent.py:1574/1695`,
     `report_tool.py:2045/2125`, `settings.py` fields and env vars).
   - `build_tool.py:1733`: the analysis judgment word derives from execution
     (did the invocation run its selected tests to terminal state), never
     from pass rate. Red counts remain exact sealed facts.
2. **Replacement language is execution-based.** Gate reasons state what was
   executed against what was discovered and the red count as fact:
   `executed 3,571 of 20,497 discovered · 3,568 passed, 2 failed, 1 skipped`.
   The only remaining red-test influence is v4's weak signal (demote
   fully→most in the rates block) — which lives in `verdict_rates`, not in
   gates.
3. **Direction fence.** A gate result whose reason text asserts a deficiency
   class (below/insufficient/missing) cannot carry an upgrading
   `expected_outcome`, and vice versa. Implementation: the reason string is
   RENDERED FROM the decision branch (one function produces both the code,
   the outcome and the reason), never assembled independently — plus a fence
   test that reconstructs the ignite shape and asserts it cannot be produced.
4. **Config compatibility.** The `SAG_TEST_PASS_THRESHOLD` /
   `SAG_TEST_EXECUTION_THRESHOLD` env vars and settings fields are removed;
   nothing outside the retired sites reads them (verify by grep, delete
   dead plumbing including `verdict_finalizer.py:662-666`'s deleted param).

### Acceptance

- Grep for `TEST_PASS_THRESHOLD|TEST_EXECUTION_THRESHOLD|pass threshold`
  in `src/sag` returns only historical comments (or nothing).
- kafka shape: a `partial` claim over a 99.9% pass receipt is adjudicated on
  execution evidence, with no percentage in the reason.
- ignite shape: unconstructible (fence test).

## 3. Fix C — one grader, one word (#50)

### Convicting evidence

- camel §0.7: the observation delivered to the model read
  `"validated outcome 'failed'"`; the sealed `gate_decision` and phase record
  read `unknown`/`blocked` — three statements, two words.
- polaris S10: sequence 167's embedded `gate_result` says
  `test_execution_observed` / "All 12 tests passed" / `success`/`green`;
  sequence 169's `gate_decision` says
  `test_candidate_resolution_unavailable` / `unknown`/`unavailable`.

Mechanism (located): `phase_tool.py:472-476` renders the outcome word from
the gate object it computed; the engine's close path
(`react_engine.py:2204/3099`) re-grades and seals its own decision. Two
graders; the model acts on the first word, history records the second.

### Contract

1. **The accepted path seals what it delivered.** When a claim is accepted
   and the observation text carries an outcome word, the engine routes and
   seals THAT gate result object. Any post-acceptance re-derivation that
   would change the word is forbidden on this path — if new evidence must
   change the outcome after delivery, the change MUST produce a fresh
   observation to the model naming both words and the reason
   (`gate_outcome_revised`), and the sealed record carries the revision
   chain. No silent divergence.
2. **Engine-generated decisions render their own observation.** Where the
   engine (not phase_tool) produces the closing decision, the observation
   text delivered to the model is generated from the same event object that
   is sealed — one serialization, two sinks.
3. **Embedded copies are the same object.** `metadata.gate_result` embedded
   in a tool result and the `gate_decision` control event for the same
   grading MUST be serializations of the same object (polaris's
   adjacent-line contradiction becomes unconstructible). Where two gradings
   legitimately exist in sequence (a claim gate followed by a close gate),
   each carries a distinct `decision_id` and the later one names the earlier
   (`supersedes`), so no reader can mistake them for the same statement.
4. **Consistency fence.** A fence test walks a synthetic run and asserts:
   for every delivered observation carrying an outcome word, the sealed
   `gate_decision` for that `decision_id` carries the same word; camel's
   shape reconstructed must fail closed (engine refuses to seal a diverging
   word without the revision observation).

### Inventory — the outcome words derived outside the gate chain

"One grader" scopes the CLAIM chain. Two other producers state an outcome word
by design, and both are named here so the next reader does not rediscover them
as bugs:

1. **`report_tool._determine_actual_status`** (`report_tool.py:2881-2960`, fed to
   the kernel at `:945-963` via `_physical_verdict_from_snapshot`) grades
   success/partial/fail from the report snapshot, including its own heavy-red
   rule. This is the PHYSICAL judge in the documented minimum-across-
   independent-judges design, not a second model-facing grader: its word never
   reaches the model as a phase outcome, and the condensed-log renderer already
   refuses to read `status.overall` directly (`:2260-2265`). It is deliberate.
2. **The four engine closes** state their own word, but through the same
   `validate_phase_claim` funnel and the same `_seal_engine_gate` sink as
   everything else (§3.2), so they are inside the chain, not beside it.

Anything else that derives an outcome word outside `phase_gates` is a defect.

### Acceptance

- camel shape: unconstructible without a `gate_outcome_revised` observation.
- polaris shape: embedded and event serializations byte-equal for the same
  `decision_id`; sequential gradings carry `supersedes` linkage.
- Existing claim-matrix and transition-policy fences stay green.

## 4. Shared constraints

- Integrity-failure families (`gate_decision_persist_failed`,
  `repair_context_projection_invalid`, barrier/dispatch) keep abort
  semantics — untouched, as in #45.
- RunEvidenceState stays engine-written; no tool writes it directly.
- TDD per house style: fence test first, red, minimal change, green, full
  suite green before each commit. No `git stash`, no co-author trailers.
- Implementation order: A → B → C (A changes counting facts B's reasons
  render; C fences the words both produce).
