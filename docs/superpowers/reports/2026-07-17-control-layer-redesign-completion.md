# SAG Control-Layer Redesign — Completion Report

- **Date:** 2026-07-18
- **Spec:** [`2026-07-14-control-layer-redesign.md`](../specs/2026-07-14-control-layer-redesign.md)
- **Branch:** `AA/control-eval-replay`
- **Baseline SAG:** `b4aec8e315486aa44cbd1c596e483b54adf57e10`
- **Live-evaluated SAG:** `b1288e1770214b800be191a09ae7d8a867a09ee7`
- **Post-eval metadata/replay HEAD:** `57373bf`
- **Status:** accepted — the four-probe `ws7-final7` campaign has three structured repeats per probe and `summarize --enforce` exits 0 with `failures: []`.

## 1. Outcome

WS0–WS7 are implemented as one control-layer contract:

> Phase termination describes control flow; phase outcome describes evidence; the finalizer computes the run verdict once at evidence-close; downstream components only render the sealed snapshot.

The final campaign contains 12 accepted runs. All 12 rendered surfaces agree with their immutable `verdict.json`; no contradicted gate claim survives; no diversity-triggered loop break occurs; no evidence-envelope contract counter is non-zero; Paramiko keeps its successful evidence ladder while median thinking calls fall from 26 to 11 (57.7%); and all three Cassandra runs remain successful inside the corrected canonical/raw/class ranges.

The authoritative artifacts are under:

```text
logs/ab-2026-07-17-control-layer-final/
  panel-lock.json
  <probe>-baseline.json
  <probe>-ws7-final7.json
  summary-ws7-final7.json
  ab-<probe>-<stage>-rN-cli.log
```

## 2. Workstream closure

| Workstream | Delivered contract | Verification focus |
|---|---|---|
| WS0 | Orthogonal invocation/outcome/evidence taxonomy; pending detached work is not completion; illegal phase termination/outcome pairs are rejected. | Typed result, lifecycle, recovery, and migration guards. |
| WS1 | Engine-owned mutable `RunEvidenceState`; idempotent `VerdictFinalizer`; immutable snapshot at evidence-close; report/CLI/web render that snapshot. | Evidence sealing, abort/corrupt-read behavior, literal surface agreement. |
| WS3 | Two-axis phase claims and semantic gates; determined failures may terminate honestly; repair routing is bounded. | Gate truth table, prerequisite enforcement, failure closure in at most two gate interactions. |
| WS2 | Cumulative structured handoff with provenance, active blockers, failure tails, omitted-count/ref behavior, and untrusted-input marking. | Cross-phase retention and prompt projection. |
| WS5 | Phase-agnostic loop identity with evidence-version progress; recurrence is detected through interleaved actions; diversity gets advisory treatment only. | No-progress recurrence, second-occurrence guidance, bounded force-break behavior. |
| WS4 | Executable `CurrentPlan`, literal/placeholder parameters, trigger-coalesced reasoning, and action-envelope/result lineage. | Scheduler state machine, actor mismatch handling, replayed plan execution. |
| WS6 | Role-typed `ProjectBrief` composition across policy, requirement, evidence, and default inputs, with fingerprint invalidation and DAG-shaped build guidance. | Deterministic composition and project-shaped projections. |
| WS7 | Canonical test identity/history, separate unique and raw bases, honest flaky/invalid metrics, validator-owned reactor rollups, and pinned structured A/B collection. | Metric reproducibility, retry history, compiled-class/test rollup, four-probe enforcement. |

Primary implementation seams include `src/sag/agent/evidence_state.py`, `verdict_finalizer.py`, `phase_gates.py`, `phase_handoff.py`, `loop_memory.py`, `reasoning_scheduler.py`, `project_brief.py`, `control_events.py`, and `src/sag/tools/report_metrics.py`.

## 3. Reproducibility pins

| Probe | Locked target SHA | Why it is in the panel |
|---|---|---|
| TVM | `3a5b4d4e64707a1528146e28c0fb75f45da99dd7` | Native/cross-language failure, handoff, retry, and loop behavior. |
| Bigtop | `e32423c444a9311b802946d5b695767a9b921e1e` | Mixed build systems, islands, and JDK/reactor behavior. |
| Paramiko | `d60d5c17d78f344b51ed651e796d2931133a9b22` | Straight-line happy path and reasoning-cost guard. |
| Cassandra Java Driver | `ab22858095651f2b1950f81b1e6345ca82d68240` | Pure Maven multi-module non-regression. |

Every final run records the same model/config family (`gpt-5.4-mini` thinking and action models), seed 17, warm dependency cache, arm64 host, container image digest `sha256:1f701c2d4555be2b976cb1846aaf0d73c955ccdc143c0bce5581ffbac7705feb`, and prompt bundle SHA-256 `74287fe3fa11c56857e726a08da1c1a6a4b000e47f5e06839af435a807e74287`.

## 4. Final A/B evidence

Values are medians with `[min–max]`, computed from structured snapshot/event/token inputs rather than rendered report text.

| Probe | Final verdicts | Thinking calls | Action calls | Compiled classes | Unique tests | Raw executions |
|---|---:|---:|---:|---:|---:|---:|
| TVM | `failed, unknown, failed` | 15 `[14–17]` | 16 `[15–18]` | n/a | 357 `[357–357]` | 357 `[357–714]` |
| Bigtop | `failed ×3` | 12 `[11–13]` | 15 `[14–15]` | 121 `[121–121]` | 50 `[50–50]` | 50 `[50–50]` |
| Paramiko | `success ×3` | 11 `[11–12]` | 16 `[14–17]` | n/a | 559 `[559–559]` | 559 `[559–559]` |
| Cassandra | `success ×3` | 18 `[14–22]` | 19 `[15–23]` | 8916 `[8914–8916]` | 4590 `[4589–4590]` | 4810 `[4809–4810]` |

### Reasoning/action deltas

| Probe | Baseline thinking | Final thinking | Change | Baseline actions | Final actions | Change |
|---|---:|---:|---:|---:|---:|---:|
| TVM | 26 `[25–38]` | 15 `[14–17]` | −42.3% | 25 `[24–41]` | 16 `[15–18]` | −36.0% |
| Bigtop | 22 `[14–22]` | 12 `[11–13]` | −45.5% | 23 `[17–24]` | 15 `[14–15]` | −34.8% |
| Paramiko | 26 `[19–29]` | 11 `[11–12]` | **−57.7%** | 24 `[20–30]` | 16 `[14–17]` | −33.3% |

Paramiko is the campaign's controlled efficiency comparison: baseline and final verdicts are both `success ×3`, and both report the identical 559 unique executions (541 passed, 18 skipped). TVM and Bigtop verdicts are reported rather than normalized: their failed/unknown outcomes are valid evidence-bearing results, not collector failures. The older baseline schema had no canonical cross-surface agreement field and is used only for pinned cost/outcome context; the final stage is the strict surface/gate/lineage instrument.

### Final run manifest

| Probe/repeat | Container | Run ID | Result summary |
|---|---|---|---|
| Paramiko r1 | `sag-ab-paramiko-ws7-final7-r1` | `20260718_025547_42748` | success; 559 unique/raw; thoughts/actions 11/17 |
| Paramiko r2 | `sag-ab-paramiko-ws7-final7-r2` | `20260718_030129_44097` | success; 559 unique/raw; thoughts/actions 11/14 |
| Paramiko r3 | `sag-ab-paramiko-ws7-final7-r3` | `20260718_030634_45372` | success; 559 unique/raw; thoughts/actions 12/16 |
| Cassandra r2 | `sag-ab-cassandra-java-driver-ws7-final7-r2` | `20260718_031137_46689` | success; 8916 classes; 4590/4810 unique/raw; thoughts/actions 22/23 |
| Cassandra r3 | `sag-ab-cassandra-java-driver-ws7-final7-r3` | `20260718_032714_50395` | success; 8914 classes; 4589/4809 unique/raw; thoughts/actions 14/15 |
| Cassandra r4 | `sag-ab-cassandra-java-driver-ws7-final7-r4` | `20260718_034135_53728` | success; 8916 classes; 4590/4810 unique/raw; thoughts/actions 18/19 |
| TVM r1 | `sag-ab-tvm-ws7-final7-r1` | `20260718_040139_58337` | failed; 357/357 unique/raw; thoughts/actions 14/15 |
| TVM r2 | `sag-ab-tvm-ws7-final7-r2` | `20260718_040631_59481` | unknown; 357/357 unique/raw; thoughts/actions 17/18 |
| TVM r3 | `sag-ab-tvm-ws7-final7-r3` | `20260718_041203_60702` | failed; 357/714 unique/raw; second-occurrence guide 1; thoughts/actions 15/16 |
| Bigtop r1 | `sag-ab-bigtop-ws7-final7-r1` | `20260718_041659_61644` | failed; 121 classes; 50/50 unique/raw; thoughts/actions 13/15 |
| Bigtop r2 | `sag-ab-bigtop-ws7-final7-r2` | `20260718_042252_62821` | failed; 121 classes; 50/50 unique/raw; thoughts/actions 12/15 |
| Bigtop r3 | `sag-ab-bigtop-ws7-final7-r3` | `20260718_042806_63735` | failed; 121 classes; 50/50 unique/raw; thoughts/actions 11/14 |

## 5. Six campaign bars

The enforced summary contains 116 metric aggregates and no failure entries.

| Bar | Final evidence |
|---|---|
| 1 — verdict/surface agreement | 12/12 runs have `surface_ok=true`; zero mismatches. |
| 2 — semantic gates and bounded failure closure | Contradicted claims, prerequisite violations, invalid repairs, repair-budget violations, and repair count all max at 0. Maximum determined-failure gate interactions is 1 (limit 2). |
| 3 — recurrence, not diversity | Diversity breaks max 0; second-occurrence misses max 0; TVM retry evidence produced one second-occurrence guide. |
| 4 — executable-plan evidence and Paramiko efficiency | Evidence-contract and envelope lineage counters all max 0. Paramiko verdict remains `success ×3`; median thinking calls fall 57.7%. |
| 5 — one honest metrics basis | No run has raw executions below unique executions; all rendered values come from the sealed snapshot; no retry/flaky conflict is hidden. TVM r3 visibly retains 714 raw vs 357 unique. |
| 6 — Cassandra non-regression | All three verdicts are success; classes 8914–8916, canonical unique tests 4589–4590, raw executions 4809–4810. |

Enforcement command:

```bash
PYTHONPATH=src \
VIRTUAL_ENV=/Users/chenhao/Documents/github/Setup-Agent/.venv \
UV_CACHE_DIR=/private/tmp/setup-agent-uv-cache \
uv run --active --no-sync python scripts/collect_control_layer_ab.py summarize \
  --campaign /Users/chenhao/Documents/github/Setup-Agent/logs/ab-2026-07-17-control-layer-final \
  --stage ws7-final7 --min-repeats 3 --enforce
```

Result: exit 0, `failures: []`. LiteLLM could not refresh its remote model-cost table in the sandbox and used its local backup; this warning does not participate in any evidence or acceptance calculation.

## 6. Cassandra basis correction

The original `~4.9k tests` reference mixed two bases. Historical evidence called 4,928 the runner-execution count and separately recorded 2,896 parameterized unique methods. WS7's explicit identity `(module_or_file, class, name, param_id)` now yields 4,589–4,590 canonical unique tests while raw runner executions remain 4,809–4,810. Treating 4,800–5,100 as the unique range would therefore reject the correct canonical metric.

The accepted Bar 6 ranges are now explicit in both the evaluator and `scripts/control_layer_panel.json`:

- compiled classes: 8,914–8,916;
- canonical unique tests: 4,500–4,700;
- raw executions: 4,800–5,100.

The two-class spread is explainable: a non-clean failed build can leave two unshaded Guava substitution class files, while a clean lifecycle produces 8,914. No count is clamped or silently moved between bases.

The first formal Cassandra attempt (`sag-ab-cassandra-java-driver-ws7-final7-r1`, run `20260718_022919_35674`) is diagnostic-only: the action model returned an empty response before the test gate/report, so it was retained but not appended as a completed structured repeat. The rollup smoke (`20260718_020708_24151`) independently produced success, 8,914 classes, 4,590 unique, and 4,810 raw executions before the three formal accepted runs.

## 7. Focused verification

No broad repository suite was used for the final gate. The final affected aggregate covered the core contract for every workstream:

```text
tests/test_tool_result_taxonomy.py
tests/test_run_evidence_state.py
tests/test_verdict_finalizer.py
tests/test_phase_gates.py
tests/test_phase_handoff.py
tests/test_loop_memory.py
tests/test_reasoning_scheduler.py
tests/test_project_brief_projection.py
tests/test_test_result_history.py
tests/test_control_layer_replay.py
tests/test_control_layer_ab_collector.py
tests/test_live_surface_checker.py
tests/test_snapshot_surface_agreement.py
```

Result: **214 passed** in 0.48s.

The aggregate initially exposed seven replay failures: validator-owned `compiled_classes` had been added to actual `BuildEvidenceSnapshot` objects but was absent from the four frozen expected snapshots. An actual-vs-expected comparison showed that this was the only differing top-level field. The legacy transcripts correctly expect `compiled_classes=null`; after refreshing that schema field, replay-only was 21/21 and the identical aggregate was 214/214. Collector/panel basis tests are 21/21; Black, isort, JSON parsing, and `git diff --check` pass for the affected files.

## 8. Docker evidence retention

At `2026-07-18 04:44:13 EDT`, 59 containers match `sag-ab-*`; all were still running. Nothing was stopped, removed, or reused before handoff. This preserves container filesystems for replay/forensics, but it also means resources have not yet been reclaimed. The safe cleanup order after explicit approval is: stop all 59 first (reversible), retain them until branch integration/evidence review, then remove only the approved campaign containers.

Exact retained inventory, grouped by probe/stage:

- Bigtop (10): `baseline-r1` through `baseline-r4`; `ws7-final6-r1` through `r3`; `ws7-final7-r1` through `r3`.
- Cassandra Java Driver (9): `baseline-r1` through `r3`; `ws7-cassandra-rollup-smoke-r1`; `ws7-final6-r1`; `ws7-final7-r1` through `r4`.
- Paramiko (28): `baseline-r1` through `r3`; `ws7-r1` through `r7`; `ws7-final-r1`; `ws7-final2-r1`; `ws7-final3-r1` through `r3`; `ws7-final4-r1` through `r2`; `ws7-final5-r1` through `r3`; `ws7-final6-r1` through `r5`; `ws7-final7-r1` through `r3`.
- TVM (12): `baseline-r1` through `r3`; `ws7-final3-r1`; `ws7-final5-r1`; `ws7-final6-r1` through `r4`; `ws7-final7-r1` through `r3`.

Every inventory suffix above has the full prefix `sag-ab-<probe>-`. CLI logs and structured JSON remain under the campaign directory whether or not containers are later stopped.

## 9. Handoff state

- Implementation, focused verification, live panel, and six-bar enforcement are complete.
- The main workspace was not modified or reset; all implementation work is isolated in `.worktrees/control-eval-replay` on `AA/control-eval-replay`.
- No container or campaign artifact was deleted.
- No merge, push, or pull request was performed.
