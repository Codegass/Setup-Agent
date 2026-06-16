# Submodule Code Coverage Design (Feature B)

## Purpose

Software engineers and researchers evaluating a project want real code-coverage numbers, not just pass/fail. Feature A surfaces per-submodule build and test results; Feature B adds per-submodule **line and branch coverage** from JaCoCo. Coverage is measured in a separate, isolated pass that runs after the setup verdict is locked, so it can never affect or pollute the setup, and is best-effort: when it cannot be produced, the UI shows "not measured", never a fabricated zero.

This is **Feature B** of a two-feature effort. It layers onto Feature A's coverage-ready per-module model (keyed by reactor path), merging coverage into the same `module_metrics.json`, and fills the dashed "Coverage" placeholder already present in the web UI.

## Scope

In scope:

- A `--coverage` handle on `sag project`/`sag run` (mirroring `--record`) that triggers an isolated coverage pass after the setup verdict is locked.
- A webui batch-setup "Coverage" checkbox column with a check-all header control (and check-all added to the existing Record column).
- A deterministic coverage runner: reuse existing JaCoCo reports when present, else inject JaCoCo (Maven via CLI plugin goals, Gradle via `--init-script`) and re-run tests — without editing any project files.
- Per-module JaCoCo XML parsing (line + branch counters) merged into `module_metrics.json` by reactor path, plus a lines-weighted project rollup.
- Web models + read model + webui: a per-module Coverage column (stacked line/branch mini-bars with thresholds) and the populated Coverage tile on Test Details.

Out of scope (explicitly):

- Changing Feature A's build/test metrics, the verdict kernel, the phase machine, or the setup flow itself. The coverage pass is strictly post-verdict and additive.
- Instruction/method/complexity counters (only line + branch). Mutation/quality scores.
- LLM-driven coverage. The runner is deterministic; weird projects that the deterministic runner cannot cover show "not measured".
- Coverage on the Build Details page (coverage is a test concern; it lives on Test Details).
- Coverage trend/history across runs.

## Relationship to Feature A

Feature A wrote `module_metrics.json` (per-module records keyed by reactor path + a rollup). Feature B's pass runs **after** that artifact exists, reads it, joins coverage onto each module by path, and rewrites it. The per-module record gains coverage fields; the rollup gains project coverage. The web read model already loads `module_metrics.json` onto `ExecutionSessionDetail`; Feature B only extends the fields. If `module_metrics.json` is absent (non-Java/unsupported), the coverage pass is skipped.

## Trigger & Isolation

**CLI.** A `--coverage` flag (`is_flag`) on `sag project` and `sag run`, parallel to `--record` (`main.py`). When set, after the agent finishes and the verdict/report are produced, SAG invokes the coverage runner against the just-set-up container.

**WebUI batch setup.** `launchRows.ts` gains `coverage: boolean`; `LaunchSetupsDialog` gains a "Coverage" column with a per-row checkbox and a **check-all toggle in the column header** (clicking it sets/clears coverage for all rows). The same check-all control is added to the existing "Record" column. `coverage` is threaded through the launch payload to the backend setup invocation, exactly as `record` is.

**Isolation guarantees:**

- Runs only after the setup verdict is locked; it cannot change the verdict, the report's outcome, or the process exit code.
- **Best-effort:** any failure (injection error, missing JDK, build break, timeout) leaves coverage absent; the setup result is unchanged and the failure is logged.
- **Pollution-safe:** no project files are edited (JaCoCo enabled via CLI args / a generated init script only); all outputs land in `target/`/`build/` (already build dirs). After the pass, a best-effort `git status --porcelain` check on the project source warns if the working tree changed.

## Coverage Runner (deterministic)

New module `src/sag/coverage/runner.py` exposing `run_coverage(orchestrator, project_dir) -> dict` (the per-module coverage map). No LLM.

1. **Detect build system** via `physical_validator._detect_build_system(project_dir)`.
2. **Reuse-then-inject.** Scan module output dirs for existing `jacoco.xml` (Maven `target/site/jacoco/jacoco.xml`, Gradle `build/reports/jacoco/**/*.xml`). If present, parse directly (`coverage_source = "jacoco-existing"`) and skip re-running tests. Otherwise inject and re-run (`coverage_source = "jacoco-injected"`):
   - **Maven:** run JaCoCo from the CLI without editing `pom.xml`:
     `mvn org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent <test-goal> org.jacoco:jacoco-maven-plugin:0.8.12:report` (a pinned recent JaCoCo version), reusing the setup's known-good test command/goal (from `command_tracker`/`test_summary.jsonl`) where available, else `test`.
   - **Gradle:** generate an init script that applies the `jacoco` plugin to all projects and enables the XML report, then run `gradle test jacocoTestReport --init-script <generated.gradle> --continue`. No `build.gradle` edits.
3. **Locate + parse** each module's `jacoco.xml` (see Parsing), producing a `{reactor_path: coverage}` map.
4. Return the map; the report/merge step writes it into `module_metrics.json`.

The runner reuses the setup's already-provisioned JDK/build tool in the container. A coverage run that produces no parseable reports yields an empty map (→ "not measured" everywhere), never an error that affects setup.

## Parsing

`physical_validator.parse_module_coverage(module_dir) -> dict` (or a dedicated coverage parser) locates the module's `jacoco.xml` and reads the top-level counters:

- `<counter type="LINE" missed="m" covered="c"/>` → `line_covered = c`, `line_total = c + m`, `line_rate = round(100 * c / (c+m), 1)` (null when `c+m == 0`).
- `<counter type="BRANCH" .../>` → `branch_covered/total/rate` likewise.

Coverage is keyed by reactor path (the same key Feature A uses), so it joins onto the existing module records. A module with no report contributes nothing (its coverage fields stay null → "not measured").

## Data Contract Additions

The per-module record in `module_metrics.json` gains:

```
line_covered      int|null
line_total        int|null
line_rate         float|null   # 0..100, one decimal
branch_covered    int|null
branch_total      int|null
branch_rate       float|null
coverage_source   str|null     # "jacoco-existing" | "jacoco-injected" | null
```

The `module_summary` rollup gains a **lines-weighted** project coverage:

```
line_covered/line_total/line_rate          # sum covered / sum total across modules
branch_covered/branch_total/branch_rate
coverage_source                            # aggregate provenance, or null
```

Uncomputable values are `null` (never `0`). The coverage pass **merges** these into the existing artifact: read `module_metrics.json`, set coverage fields on each module by `path`, recompute the rollup coverage, rewrite. The write is best-effort (a failure never affects the setup).

## Web Models & Read Model

- `ModuleSummary` (`src/sag/web/models.py`) gains `lineCovered/lineTotal/lineRate`, `branchCovered/branchTotal/branchRate`, `coverageSource` (camelCase `serialization_alias` + `validation_alias=AliasChoices(...)`, defaults null).
- `ModuleRollup` gains the project-level `lineRate/branchRate` (+ covered/total) and `coverageSource`.
- The read model (`session_registry._modules_payload_from_metrics` / `_module_rollup_from_metrics`) already passes the artifact dicts through; it gains the new keys. No new endpoint — coverage rides on the existing `ExecutionSessionDetail`.
- TS interfaces `ModuleSummary`/`ModuleRollup` in `webui/src/api/types.ts` extended with the same camelCase fields.

## WebUI

Test Details only (Build Details unchanged):

- **Coverage column** in the per-module table (test variant): one column with **stacked line (L) and branch (B) mini-bars**, each a colored bar (~96px) + percentage, color thresholds **≥80% green · 50–79% amber · <50% red**. Pass/Fail/Skip/Rate columns are retained; the Failing Methods column shrinks to its content. A module with no coverage renders **"— not measured"** (no fake 0%). Column titles are left-aligned, sitting directly over their data.
- **Coverage tile** on Test Details: the dashed Feature-B placeholder becomes real — a line% headline with branch% + source ("jacoco") as subtext, both as mini-bars; shows an unavailable state when no coverage exists.
- Coverage is built from the existing `detail.modules` / `detail.moduleSummary`; no new data fetch.
- Batch-setup dialog: the Coverage checkbox column + check-all header described under Trigger.

## Error Handling

- Coverage pass is post-verdict and best-effort: injection failure, missing tooling, build break, or timeout → coverage absent, setup result unchanged, logged.
- Merge into `module_metrics.json` is best-effort; a write failure never breaks anything (setup already finished).
- Partial coverage (some modules reported, others not) is fine: reported modules show bars, the rest "not measured".
- A configurable overall timeout bounds the coverage re-run so a hanging build cannot stall indefinitely (on timeout: best-effort partial/none, logged).
- Source-tree pollution is guarded by the post-pass `git status` check (warn-only).

## Testing

Backend:

- Runner: build-system detection, reuse-vs-inject decision (existing report present vs absent), Maven CLI-goal and Gradle init-script command construction, with a fake orchestrator. Assert no project-file writes are issued.
- Parser: `jacoco.xml` line/branch counters → rates (+ covered/total); zero-total → null; missing report → null; realistic JaCoCo XML fixtures.
- Merge: coverage map merged into an existing `module_metrics.json` by path; rollup recomputed lines-weighted; modules without coverage stay null; absent artifact → pass skipped.
- Web/read model: coverage fields round-trip to camelCase on `ModuleSummary`/`ModuleRollup`.

Frontend:

- Coverage column renders stacked L/B bars with correct threshold colors; "— not measured" when null; Pass/Fail/Skip/Rate retained; titles aligned.
- Coverage tile populated vs unavailable state.

Manual: live Maven multi-module and Gradle multi-project with `--coverage`; confirm per-module line/branch render, the project rollup tile, the reuse-vs-inject paths, and that the setup verdict, exit code, and source tree are all unaffected. Verify the batch-dialog Coverage checkbox + check-all.
