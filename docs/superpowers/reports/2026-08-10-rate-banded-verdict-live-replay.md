# Rate-banded verdict: live-proof replay appendix

Date: 2026-08-10

This appendix replays two existing live-proof runs through the current verdict-v4
finalizer and its shared `render_rate_lines` facade. The container snapshots are
historical schema-v3 evidence inputs only; they are not admitted as current live
authority and they are not rewritten.

The replay adapter supplies only facts already present in each historical run:

- compiled class and test execution counts come from the sealed v3 verdict;
- build-module and test-module fractions come from its sealed phase records;
- statically discovered test counts come from the persisted trunk context;
- source-file counts are a read-only reconstruction of the physical census that
  v4 now carries directly in `build_evidence.source_files`.

The production v4 path does not repeat that source scan at evidence close: it
reuses the physical validator's existing census and the existing module summary.

## Apache Commons DBCP

- Container: `sag-lp-commons-dbcp`
- Historical run: `20260809_195014_672809_337421a327be_80866-7-13d6af4436c6`
- Historical input: `/workspace/.setup_agent/verdict.json` (schema v3)
- Static test denominator: 1,163 (`catalog_based_discovery`)

```text
Build:    modules 1/1 (fully) · classes 201/68 (fully)
Tests:    cases 1605/1163 (fully) · modules 1/1 (fully)
Coverage: unavailable — coverage pass not run
Verdict (derived): success
```

All four build/test grains are independently `fully`. Executed test cases may
exceed statically declared methods because parameterized methods can expand at
runtime; the grain remains the honest `1605/1163` fraction rather than being
rewritten to `1163/1163`.

## Apache Cayenne

- Container: `sag-lp-cayenne`
- Historical run: `20260809_193517_569202_ea5a057fa0e5_79523-7-5b11aad97211`
- Historical input: `/workspace/.setup_agent/verdict.json` (schema v3)
- Static test denominator: 4,471 (`catalog_based_discovery`)

```text
Build:    modules 38/52 (half) · classes 8655/2489 (fully)
Tests:    cases 4550/4471 (fully) · modules 29/32 (most)
Coverage: unavailable — coverage pass not run
Verdict (derived): partial
Conflicts: build_modules_incomplete; reactor_scope_narrowed
```

The facade preserves both Cayenne build grains instead of collapsing them: the
module rate is `half` while the class rate is `fully`. The test-case and
test-module grains likewise remain distinct. The derived word is a footnote and
does not replace those fractions.

## Apache Commons DBCP live `--coverage` acceptance

- Container: `sag-lp-dbcp-rates-final`
- Run: `20260810_233111_132666_3312751c219b_7287-7-d1cc5aa1f8f4`
- Ref: `rel/commons-dbcp-2.14.0`
- Console log: `logs/liveproof-20260810/console-dbcp-rates-final.log`
- Session artifacts: `logs/session_20260810_233111_132666_3312751c219b_7287`

```text
Build:    modules 1/1 (fully) · classes 201/68 (fully)
Tests:    cases 1605/1163 (fully) · modules 1/1 (fully)
Coverage: 68.3% line (jacoco-existing)
Verdict (derived): success
```

The persisted terminal snapshot is schema v4 and carries the same serialized
`rates` block. Its test receipt is complete with 1,605 testcase execution rows,
all bound to the surveyed domain `/workspace/commons-dbcp` and the canonical
single-module coordinate `.`. The module metrics preserve JaCoCo's physical
counts (`4,629/6,781` lines and `894/1,297` branches), so coverage is collected
evidence rather than a report-only reconstruction.

## Acceptance result

Both snapshots were constructed by `VerdictFinalizer._snapshot_for_state`,
validated as schema v4, and rendered from their serialized `rates` blocks. The
scratch replay asserted the expected derived word for each run and exited 0.
Neither historical run collected line coverage, so their typed `unavailable`
result remains unchanged. The dedicated live DBCP rerun independently proves
the collected-coverage path and all three public rate lines end to end.
