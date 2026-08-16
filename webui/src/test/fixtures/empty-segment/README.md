# A run whose banding states a segment no turn carries

`trajectory.json` is not written by hand. It is what the real reducer answers
for the ledger beside it:

```
.venv/bin/sag trajectory webui/src/test/fixtures/empty-segment | python3 -m json.tool
```

`control_events.jsonl` is a SYNTHETIC ledger, on the same terms as the synthetic
fixtures in `tests/test_trajectory_reducer.py`: every line is built from the
payload class in `sag.agent.control_events` that the engine would seal and is
validated through `ControlEvent`, so no line here is a shape the engine could not
write. It is synthetic because no recorded run carries this banding — every one
of the 27 archived ledgers under `logs/` bands each phase exactly as often as its
turns do — and the shape is one the reducer's own docstring names: *"a gate can
band a phase no turn has entered"*. Sequence 26 is an orphan
`gate_outcome_revised` for `build`, revising a word nothing in this run
delivered. It owns no turn, so it only BANDS its phase, and the reducer appends a
`build` segment while the run is still in `provision`:

| # | segment     | termination      | gates     | turns    |
|---|-------------|------------------|-----------|----------|
| 0 | `provision` | `advance`        | `success` | 1, 2     |
| 1 | `build`     | —                | `failed`  | **none** |
| 2 | `analyze`   | `advance`        | —         | 3        |
| 3 | `build`     | `evidence_close` | `success` | 4, 5     |

Segment 1 is the empty one. The run's only `build` band is turns 4 and 5, and
what ended it is segment 3 — so a view that takes "the Nth segment named
`build`" hands that band segment 1: no termination at all, and a word decided
for a visit that never happened. `bandTurns` walks both lists in document order
instead, which passes over a segment no band claims.

The ledger is a session directory holding nothing but the ledger, which is why
`session.run_id` is empty: no `verdict.json` beside it names one. The warnings
are the reducer's, and honest — a trimmed ledger really does leave those holes.
