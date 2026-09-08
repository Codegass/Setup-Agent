# CI-defined scope: two real small-project probes, 2026-09-07

## Result

The corrected r3 measurements used real production survey, BuildTool/MavenTool, controller contracts, durable invocation receipts, physical readers, and VerdictFinalizer. They made no LLM calls and did not fabricate successful phase records.

Commons CLI finished with a published local `success`. Gson built its eight-module reactor, then its test invocation failed to start the JPMS test VM; its published local verdict remained `partial`. With the same r3 evidence, removing receipts never produced success, and removing actual build/report outputs produced `failed` for both projects.

The external comparison remained conservative. Gson's real same-commit CI job log proves eight build modules: the build axis is `8/8`. The harvested CI record has no authoritative test count, so overall alpha is unavailable and attainment stays `partial`. Removing CI module scope makes the build fraction unavailable and leaves attainment `partial`. Wrong repository or SHA produces `invalid`, with no fractions. Commons CLI has no admissible matched target under the current conservative matrix-laundering policy, so no external attainment was fabricated.

## Pins and execution environment

| Project | Repository revision | Real upstream CI run |
|---|---|---|
| Commons CLI | `apache/commons-cli@e17111798da51037659b3594d9c0b3b525040081` | [34126280004](https://github.com/apache/commons-cli/actions/runs/34126280004) |
| Gson | `google/gson@b3f4ca20087f9066de4c340522ff84e0558e1ad1` | [33029405097](https://github.com/google/gson/actions/runs/33029405097) |

Both local checkouts stayed at their pinned commit with no tracked source changes. Both used OpenJDK 17.0.20, Maven 3.9.9, Linux aarch64, and image `sag-gradle-receipt-smoke-base:20260901`, digest `sha256:8ab4d231167375147238de6edb1bea66a6ea3f03dc0796edb66cc27b368841f9`. An existing Maven installation under `/opt/apache-maven-3.9.9` was exposed through `/usr/local/bin/mvn` only in the new containers. CI uses its own hosted runner/JDK distribution; this experiment does not assert environment identity.

Only the newly created `sag-ci-scope-commons-cli-20260907-r1` and `sag-ci-scope-gson-20260907-r1` containers were used. They were stopped after evidence archival and preserved for inspection. The pre-existing `sag-ci-commons-cli-r1` container was untouched.

The final r3 epochs are:

- `d0-ci-scope-20260907-r3-commons-cli-real-1-0b197806ac47c863`
- `d0-ci-scope-20260907-r3-gson-real-1-cdc89cfe6f9d15df`

## Actual commands, receipts, and local finalization

| Project / public action | Actual persisted argv | Exit |
|---|---|---:|
| Commons CLI `build(action="test")` | `/usr/local/bin/mvn --fail-at-end -Dmaven.test.failure.ignore=true test` | 0 |
| Gson `build(action="package")` | `/usr/local/bin/mvn --fail-at-end package -DskipTests` | 0 |
| Gson `build(action="test")` | `/usr/local/bin/mvn --fail-at-end -Dmaven.test.failure.ignore=true test` | 1 |

These flags were supplied by the production BuildTool backend. A zero process exit never stood in for test evidence. Counts below come from receipt-scoped physical XML rollups, including skipped rows in the existing `executed`/reported-count vocabulary.

| Actual r3 evidence | Commons CLI | Gson |
|---|---:|---:|
| Persisted dispatch receipts | 1 | 2 |
| Compiled `.class` files observed | 119 | 1,319 |
| Receipt-scoped reported tests | 994 | 4,840 |
| Passed / failed / errors / skipped | 933 / 0 / 0 / 61 | 4,821 / 0 / 0 / 19 |
| Published local verdict | `success` | `partial` |
| Finalizer conflicts | `build_modules_incomplete` | `metrics_conflict`, `test_primary_coordinate_unresolved` |

Commons CLI's remaining module-scan conflict is disclosed and no longer caps JVM execution success. It ran `test`, so the root JAR expected by the survey was not packaged; the physical build evidence still proved compilation and the test run completed.

Gson's real test command reported `java.lang.module.FindException: Module com.google.gson not found, required by com.google.gson.jpms_test`; the forked VM did not start successfully. Its retained passing reports do not turn this run into a full success. The test gate saw the retained receipt-scoped counts as green, while the production finalizer retained the stated conflicts and published `partial`. This is a bounded outcome, not an all-CI-checks-passed claim.

Receipt IDs are `inv-maven-1-0559c280d57e-0001` for Commons CLI and `inv-maven-1-6758b30d663e-0001` / `inv-maven-1-6758b30d663e-0002` for Gson.

## Evidence-removal controls

Each row below reuses one r3 run's source revision, execution results, and host publication epoch. The probe temporarily moves evidence in its own container, constructs a fresh production physical validator, and projects the production finalizer. Every move and restoration must return exit zero. Original evidence is restored before the published finalization.

| r3 control | Commons CLI | Gson |
|---|---|---|
| Full real evidence | `success` | `partial` |
| Invocation-receipt directory removed | `partial` | `partial` |
| Actual `target` output/report trees removed | `failed` | `failed` |
| Evidence restored; actual finalizer publication | `success` | `partial` |

Removal rows are production finalizer projections, not repeated writes over an immutable verdict. The restored final row calls `VerdictFinalizer.finalize`; the exact container `verdict.json` was copied back and checked against the returned snapshot for both projects. No successful phase lifecycle was injected. This deterministic harness does not exercise the entire model-driven phase machine.

## External scope and provenance controls

All upstream inputs were downloaded using the production `gh api` courier at the exact source revisions. No JUnit pool was available in either harvested snapshot. Official log console test totals were retained as source material but were not inserted as authoritative target test counts.

| Same-commit CI evidence remaining | Commons CLI JDK17 module evidence | Gson JDK17 module evidence |
|---|---|---|
| Full harvested evidence | `log`: 1 root module; target remains unmatched | `log`: 8 modules |
| Job log removed, source declarations retained | Unavailable | Unavailable |
| Job log and declarations removed | Unavailable | Unavailable |

The missing lower rungs are a result of actual available evidence, not a simulated staircase. Commons CLI's workflow uses `continue-on-error: ${{ matrix.experimental }}`; the existing vet conservatively treats its conclusion-grade matrix cells as laundered. Gson's selected workflow command contains an unresolved matrix expression, so removing the log does not admit an exact declared denominator. Neither snapshot contains JUnit pools from which to infer `test_bearing`. Kafka's archived acceptance, owned by the integration task, covers the requested test-bearing evidence case separately.

For Gson, the snapshot/receipt adapter binds repository and exact SHA to the host-published RunPin and verifies every receipt's run and revision. It uses the production receipt-scoped rollup and successful module outcomes from the package receipt. It is explicitly a receipt/snapshot view, not a fabricated Java certificate.

| Gson comparison of the same r3 evidence | Build axis | Overall alpha | Attainment |
|---|---|---|---|
| Full real CI target | `8/8`, basis `log` | Unavailable: no CI test count | `partial` |
| Same target with module scope removed | Unavailable; conclusion form | Unavailable | `partial` |
| Target absent | No external comparison | Unavailable | No attainment claim |
| Repository deliberately mismatched | Unavailable | Unavailable | `invalid` |
| SHA deliberately mismatched | Unavailable | Unavailable | `invalid` |

The absence of target scope changes what can be measured; it does not improve the attainment verdict. Local finalizer results remain unchanged because the external comparison is a separate axis.

The upstream lifecycle also exceeds these local dispatches: Commons CLI's bare `mvn` resolves to its multi-goal default lifecycle, while Gson's JDK17 workflow runs `verify javadoc:jar`. This probe does not claim lifecycle equivalence; the automated comparison reports unknown for the unresolved/flagged command forms.

## Harness failures retained and excluded

The experiment retained all attempted setup states rather than silently restarting until green:

1. The image contained inherited `.setup_agent` mutable records; a fresh host epoch correctly refused them. Only the two new containers' inherited records were quarantined. No project build had dispatched.
2. An initial Gson package action had a mismatched `next_action_kind` and was refused before dispatch.
3. r1 used a project directory where PhysicalValidator expected the workspace root, so its receipt reader could not find the host-authorized ledger. r1 does not support product conclusions.
4. r2 corrected the root but omitted production RunPin publication. Its partial verdicts are harness failures. Its physical-removal case also reused an old temporary directory; that mutation failed and its reported removal row is invalid.

Before any r3 Maven dispatch, `bootstrap-check.json` records: canonical RunPin mirror and host publication succeeded; production survey succeeded; `resolve_current_build_receipt_scope` is available at the exact revision; the production receipt reader sees a host-authorized empty ledger; validator workspace and project arguments are correct. Failed mutation exits now abort the probe.

## Reproduction and archived evidence

Manual entry point: `scripts/ci_scope_small_project_probe.py`. It attaches only to the explicitly named experiment containers and refuses an already-existing output directory. It assumes a clean, prepared source checkout and fresh `.setup_agent` epoch, as checked before dispatch. Unit tests do not start Docker or fetch network data.

Final real runs:

```sh
UV_CACHE_DIR=/private/tmp/sag-ci-scope-uv-cache LITELLM_LOCAL_MODEL_COST_MAP=True \
  uv run --offline --no-sync python scripts/ci_scope_small_project_probe.py \
  --project commons-cli --out logs/ci-scope-small-projects-20260907/r3
# Same command with --project gson for the second project.
```

Offline comparison replay:

```sh
UV_CACHE_DIR=/private/tmp/sag-ci-scope-uv-cache LITELLM_LOCAL_MODEL_COST_MAP=True \
  uv run --offline --no-sync python logs/ci-scope-small-projects-20260907/replay_scope.py
```

Evidence root: `logs/ci-scope-small-projects-20260907/`. Important artifacts are:

- `r3/<project>/bootstrap-check.json`, `production-run-pin.json`, `receipts.json`, and `tool-result-*.json`.
- `r3/<project>/full-evidence.json`, `without-receipts.json`, `without-physical-outputs.json`, `published-finalizer.json`, and exact `published-verdict.json`.
- Per-run source tar/hash manifest, project/evidence tar, command audit with content-addressed blobs, and host control events.
- `ci-targets/`, `rung-snapshots/`, `scope-comparisons.json`, and `comparison-engine.sha256.json`.

The live r3 source manifests record two files changed concurrently: `src/sag/metrics/attainment.py` and `src/sag/metrics/ci_vetting.py`. The executed BuildTool/receipt/physical/finalizer implementation files did not change. External comparisons were replayed afterward with the current conservative attainment implementation, and its source hashes are archived separately. No claim is made that the entire dirty tree stayed fixed.

## Validation of the probe deliverable

- `uv run --offline --no-sync python -m pytest -q tests/test_ci_scope_small_project_probe.py`: **2 passed** (offline comparison controls and rejection of failed mutations).
- `MYPYPATH=src uv run --offline --no-sync python -m mypy scripts/ci_scope_small_project_probe.py --follow-imports=silent`: **no issues in 1 source file**. `MYPYPATH=src` is necessary when checking this standalone script against the local untyped installed package.
- Black/isort completed for the two new Python files; `git diff --check` was clean.
- Offline replay completed with current attainment semantics: Gson `8/8` build/overall unavailable/partial; scope removal stays partial; both subject mismatches invalid.
- Exact published container verdict JSON equals the returned finalizer snapshot for each r3 project.

`artifact-manifest.json` hashes the archived evidence files. The final probe source additionally uses a run-specific temporary output-hold directory and explicit typed BuildTool arguments; the exact source used for each real r3 run remains in its source archive. No source changes were committed or staged.
