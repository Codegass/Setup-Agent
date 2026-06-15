# Submodule Build & Test Metrics Design (Feature A)

## Purpose

Software engineers and researchers evaluating a multi-module project want the traditional, structural questions answered at a glance: how many submodules does the build have, how many built successfully, which modules failed, how many tests each module ran, and which modules concentrate the test failures. SAG already produces most of this signal but discards it before the report — the report shows only project-level totals. This feature surfaces per-submodule build and test metrics across the backend verifier, the report generation, and the web UI.

This is **Feature A** of a two-feature effort. **Feature B** (per-submodule code coverage via an isolated, pollution-safe coverage pass) is a separate spec that layers onto the module model and detail pages defined here. Feature A is shippable on its own.

## Scope

In scope:

- A per-module data model keyed by reactor path, build-system-agnostic.
- Backend assembly: a physical per-module scan reconciled with the build tool's reactor/task status (the "hybrid" approach), emitted as a persisted `module_metrics.json` artifact.
- Web read model + Pydantic models exposing the per-module list and a run-level rollup on `ExecutionSessionDetail`.
- Web UI: the existing Overview Build/Test cards become clickable, opening dedicated **Build Details** and **Test Details** drill-down pages with a SonarQube-inspired metric strip and a failures-first per-module table.

Out of scope (explicitly):

- Code coverage (Feature B). The model and UI leave a labeled placeholder for it.
- Bugs, vulnerabilities, code smells, technical debt, duplications. SAG does not run those analyzers; we never fabricate metrics we cannot measure.
- Changing the phase machine, the build/test execution tools' behavior, or the verdict kernel. We consume evidence those layers already produce and add per-module assembly + display.
- URL deep-linking to a specific module (the web app uses in-app state routing with no URL sync).

## Module Model

**Identity.** Each module is keyed by its **reactor path** relative to the repo root: `"."` for the root module, `"connect/api"` for a nested one. A display **name** is derived from the path (`connect:api` Maven-style for the table). The path is the join key Feature B's coverage reports will reuse, so the model is coverage-ready.

**Structure.** The core data is a **flat list** of module records, exactly as Maven's Reactor Summary / Gradle's subprojects report them. The UI groups/indents by path prefix on display and offers a failures-first ordering; the backend does not build a parent→child aggregation tree (YAGNI for "which module failed").

**Per-module record** (snake_case on disk, camelCase in web models):

```
name                 str        # display, e.g. "connect:api"
path                 str        # reactor path, e.g. "connect/api" ("." = root)
build_status         str        # success | failure | skipped | unknown
build_source         str        # reactor | artifacts | partial | none  (how build_status was determined)
class_count          int|null
jar_count            int|null
build_warnings       int|null   # count of warnings attributed to the module (null if not captured)
build_error_samples  [str]      # representative compiler/build error lines for a failed module (truncated, see Truncation)
tests_total          int|null
tests_passed         int|null
tests_failed         int|null
tests_errors         int|null
tests_skipped        int|null
test_source          str        # runner_xml | partial | none
failing_names        [str]      # FULL list of failing test FQNs (see Truncation)
failing_count        int|null   # exact total of failing tests, always accurate even if failing_names is capped
evidence_refs        [str]      # paths to the module's report dir / build log (where the full truth lives)
# (Feature B adds: lines_covered, lines_total, line_rate, branch_rate, coverage_source)
```

**Run-level rollup** (`module_summary`):

```
modules_total                int
modules_built                int   # build_status == success
modules_failed               int   # build_status == failure
modules_skipped              int   # build_status == skipped
modules_with_test_failures   int   # failing_count > 0
build_systems                [str] # e.g. ["maven"] or ["gradle"]
single_module                bool  # true when the project is not multi-module
```

## Truncation Rule (cross-cutting acceptance criterion)

Every truncated view must expand to the complete list, and the full data must travel in the payload so expansion never silently loses anything.

- `failing_names` carries the **full** per-module list (not a slice), capped only by a generous safety bound of **500 names per module** to prevent pathological bloat. `failing_count` always holds the exact total. When the cap is hit, the record still reports the true `failing_count` and the `evidence_refs` point to the complete source (the module's test-report dir), so the full truth is always reachable.
- `build_error_samples` is a small representative slice (≤ 20 lines) with `evidence_refs` to the full build log.
- The module list payload carries **every** module (module counts are bounded even for huge repos), so a failures-first default view can always expand to the full roster.
- The web UI shows truncated-by-default with an inline expand (see UI) that reveals the rest from data already in hand. The Markdown report truncates with the exact count and the evidence path.

## Backend: Hybrid Assembly

Three pieces, mirroring SAG's existing dual-source design (physical_validator + `test_summary.jsonl`) and the pure-assembler pattern of `report_metrics.py`.

### 1. Physical scan (backbone) — `physical_validator.scan_modules(project_dir)`

Enumerates modules and scans each module's output:

- **Maven:** recursively resolve `<modules>` starting from the root `pom.xml` (handles nesting; the existing `_check_modules_without_tests` does a single level — this generalizes it). For each module dir: count `target/classes/**/*.class` and `target/*.jar`; locate `target/surefire-reports` / `target/failsafe-reports`.
- **Gradle:** read `settings.gradle[.kts]` `include` entries (subproject paths). For each: count `build/classes/**/*.class` and `build/libs/*.jar`; locate `build/test-results/**/*.xml`.

Returns a flat list of `{path, name, class_count, jar_count, report_dirs}`. This is authoritative for **which modules exist** and **artifact presence**. It works even when the model ran a raw `mvn`/`gradle` via bash (no tool capture).

Per-module test counts come from parsing the module's report XML with the existing parser (`parse_test_reports` is refactored to accept a directory subset so it can be invoked per module; the global parse remains for the project total).

### 2. Reactor/task status capture (enrichment) — persisted by the build tools

`maven_tool` already parses the Reactor Summary into `reactor_summary` (`[{module, status, raw}]`), `failed_modules`, and `failed_tests` (`maven_tool.py:1304-1346`). Persist this to `/workspace/.setup_agent/module_status.jsonl` (append per build invocation, like `test_summary.jsonl`). `gradle_tool` persists what it can derive from `--continue` output (`> Task :sub:test FAILED`, per-task outcomes) — best-effort.

This is authoritative for **failed vs skipped vs success** per module, which the filesystem alone cannot distinguish.

### 3. Pure assembler — `assemble_module_metrics(...)`

A new pure function (mirrors `assemble_report_metrics`):

```
assemble_module_metrics(*, modules_scan, status_records, test_reports_by_module,
                        build_system, generated_at) -> dict
```

Reconciliation rules:

- Physical scan determines the module set and `class_count`/`jar_count`/test counts.
- `build_status` = reactor/task status when present for the path; else inferred from artifacts (`artifacts present → success`, `declared but no artifacts and an upstream failure exists → skipped`, else `unknown`). `build_source` records which path was taken (`reactor` / `artifacts` / `partial` / `none`).
- `failing_names`/`failing_count` come from the per-module test XML (full list, capped per Truncation Rule).
- Conflicts (e.g., reactor says SUCCESS but no artifacts found) resolve to the more cautious state and set `build_source = "partial"`.

### Persistence & flow

The report tool calls the assembler at report time and writes `/workspace/.setup_agent/module_metrics.json` (`{version, generated_at, modules:[...], module_summary:{...}}`, snake_case). The write is **best-effort**: any failure is caught and logged; report generation is never blocked (same contract as `_persist_report_metrics`). `module_metrics.json` is absent for non-Java / unsupported projects, and the read model degrades gracefully.

**Single-module projects** produce a one-row result with `module_summary.single_module = true`; the UI shows a "single module" note rather than a confusing one-row table.

## Web Read Model & Models

- New `ModuleSummary` Pydantic model in `src/sag/web/models.py` (the per-module record) and `ModuleRollup` (the run-level rollup), both `WebModel` subclasses with camelCase `serialization_alias` + `AliasChoices`, serialized `by_alias`.
- `ExecutionSessionDetail` gains `modules: list[ModuleSummary]` and `module_summary: ModuleRollup | None`.
- `session_registry` gains `_read_module_metrics(orchestrator)` (reads `module_metrics.json`, tolerant of missing/malformed JSON → returns None) and `_modules_payload_from_metrics(...)`, wired into `_session_detail` exactly as `_read_report_metrics` / `_test_payload_from_metrics` are. No new HTTP endpoint — the data rides on the existing `/api/sessions/{id}` detail response, which `Workspace` already receives as `latest`.
- TS interfaces `ModuleSummary` and `ModuleRollup` added to `webui/src/api/types.ts`; `ExecutionSessionDetail` extended with `modules` and `moduleSummary`.

## Web UI

### Page-layer relationship

The Build/Test cards live in `Workspace.tsx`'s `OverviewTab`. Integration (no App.tsx Route changes; `latest` is already a full `ExecutionSessionDetail` carrying `modules`):

- `Workspace` gains local state `detail: "build" | "test" | null`.
- `BuildCard`/`TestCard` become clickable (whole-card button affordance with a `→`); their click sets `detail`. The previous inline "Details" expand on the cards is **removed** — its content graduates to the detail page.
- When `detail` is set, `Workspace` renders `<BuildDetailPage detail={latest} onBack={() => setDetail(null)} />` or `<TestDetailPage .../>` instead of the tab strip, with a **Back** button returning to Overview — mirroring `SessionDetail`'s `onBack` pattern.
- The Overview keeps the project-level Build/Test summary cards unchanged.

### Build Details page

- Header: build verdict (`StatusBadge`), build system, build time when known.
- Metric-tile strip (SonarQube-inspired, real data only): Modules, Built, Failed, Skipped, Classes, JARs, Warnings.
- Toolbar: "Failures first" (default) / "All modules" / sort.
- Failures-first per-module table: module name (grouped/indented by path), build status chip, class/JAR counts, warnings. Failed modules expand inline (dropdown sub-row) to show `build_error_samples` + evidence path. Skipped modules labeled with reason ("upstream failed").

### Test Details page

- Header: pass-rate verdict (`StatusBadge`), runner-executions vs unique-methods context.
- Metric-tile strip: Runner executions, Passed, Failed, Skipped, Unique methods, Modules-with-failures, plus a **dashed, labeled "Coverage" tile** marking where Feature B lands.
- Overall pass-rate bar (`TestBar`, runner rate).
- Failures-first per-module table: module, pass/fail/skip, per-module pass-rate bar. Modules with failures show "View N failures ▾"; clicking expands an **inline dropdown** sub-row (chosen interaction) listing the full failing-test FQNs with copy-all and an "open report" link to the evidence path. The list honors the Truncation Rule (full within cap; "+N more, full list at `<path>`" beyond it).

### Styling

Built entirely from existing primitives — `Card`/`CardHead`, `StatusBadge`, `TestBar`, `Tabs`, `cn`, and the slate/emerald/red Tailwind tokens with mono uppercase labels (`font-mono text-[10px] uppercase tracking-[0.14em]`) and `tabular-nums`. No new design language or raw CSS.

## Report (Markdown) Section

The report tool renders a new "Submodule Breakdown" section when `module_metrics.json` has more than one module:

- A one-line rollup: `24 modules · 21 built · 1 failed · 2 skipped · test failures in 2 modules`.
- A compact table of failed/test-failing modules first (build status, test pass/fail/skip), truncated to the top N with the exact remaining count and a pointer to `module_metrics.json` / per-module evidence paths for the rest.
- Single-module projects omit the section (the existing project-level build/test sections already cover them).

## Build-System Handling

- **Maven:** full fidelity. Per-module build status from the Reactor Summary; per-module tests from each module's surefire/failsafe reports.
- **Gradle:** best-effort, clearly labeled. Per-subproject tests from `build/test-results`; build status from task outcomes where captured, else inferred from artifacts with `build_source = "partial"`. The UI surfaces a "partial evidence" affordance so users know Gradle per-module build status is lower-fidelity than Maven's, and never presents an inferred status as authoritative.

## Error Handling

- `module_metrics.json` write is best-effort; failure never blocks report generation.
- Missing/malformed `module_metrics.json` → read model returns no modules; the cards remain non-clickable (no detail page) and the report omits the section. No crashes, no fake zeroes.
- Uncomputable per-module fields are `null`, rendered as an unavailable state, never `0`.
- Conflicting/under-determined build status resolves to the cautious value with `build_source = "partial"` and a visible label.

## Testing

Backend:

- `assemble_module_metrics` pure-function tests: reconciliation (reactor vs artifacts vs conflict), failures-first rollup counts, truncation cap + exact `failing_count`, single-module degenerate case.
- `scan_modules` tests: recursive Maven `<modules>`, Gradle `settings.gradle` includes, per-module artifact + report-dir discovery (fixture trees).
- Read-model tests: `module_metrics.json` → `ExecutionSessionDetail.modules`/`moduleSummary` with camelCase aliases; graceful degradation when absent/malformed.
- An end-to-end-shaped test mirroring the real producer→assembler→read-model chain (the lesson from the report-metrics work: test the seam, not just each layer).

Frontend:

- Card click navigates to the detail sub-view; Back returns to Overview.
- Build Details renders the tile strip and failures-first table; failed module expands to error samples.
- Test Details renders tiles, pass-rate bar, per-module table; "View N failures" expands the inline dropdown to the full list; truncation shows "+N more" with the evidence path.
- Unavailable/single-module states render without fake zeroes.

Manual verification: a Maven multi-module project (e.g. Kafka-scale via the eval set) and a Gradle multi-project, confirming per-module build/test status, failures-first ordering, the inline expand showing the full failing list, and the Gradle "partial evidence" labeling. Plus a single-module project (commons-cli) confirming the graceful single-module presentation.

## Feature B Hooks

The module record and Test Details tile strip reserve space for coverage (`line_rate`/`branch_rate`/`coverage_source` fields; the dashed Coverage tile). Feature B will populate them from its isolated coverage pass without changing this feature's model or page structure.
