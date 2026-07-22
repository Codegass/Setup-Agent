# Superseded orphan: sag-ab-tvm-F-r1

Archived `/workspace/.setup_agent` from the mid-run container `sag-ab-tvm-F-r1`
before `docker rm -f`. This run **never completed** and its metrics were never
distilled or appended to any campaign record. It is preserved only as forensic
evidence of the crash described below.

## Why this run was orphaned

The confirm campaign (`logs/panel-confirm-tvm-http/`) was launched with
`--only-probes tvm,httpcomponents-client`. Its ledger recorded:

1. `baseline-red` (six accepted suite reds)
2. `tvm-P-r1` (run_order_index 8 — the tvm-P-r1 slot in the FULL 27-slot plan)

This container was the **tvm-F-r1** run. Its run pin carries
`run_order_index: 10` (the tvm-F-r1 slot in the FULL 27-slot plan).

That is the P2 defect: `campaign_run_order()` numbered every run over the whole
27-slot plan (calibration + full 4-probe panel) even under `--only-probes`, so
successive runs within one probe/stage campaign carry DIFFERENT
`run_order_index` values.

Combined with the P1 defect — `CampaignStore.append` compared the WHOLE pin
against the first recorded run — the runner would crash `pin mismatch within
probe/stage campaign` as soon as a second run for the same probe/stage landed
(the third confirm run, per the reviewer). The runner was killed while this
container was mid-run.

## Facts of the orphaned run (from the archived run-pin.json)

- run_order_index: 10 (full-plan slot; not filtered)
- sag_git_sha: f2c03d45713e0a9303a09c200a11330dad293bce
- target_repo_sha: 3a5b4d4e64707a1528146e28c0fb75f45da99dd7
- random_seed_or_null: 42
- dependency_cache_state: warm

## Contents

- `.setup_agent/` — the partial sealed state copied out of the container
  (24 files; the run had progressed through report/verdict but was not sealed
  into a campaign record).
- `checksums.sha256` — sha256 of every archived file, verify with
  `shasum -a 256 -c checksums.sha256`.

The container was removed with `docker rm -f sag-ab-tvm-F-r1` immediately after
this archive was checksummed and verified.
