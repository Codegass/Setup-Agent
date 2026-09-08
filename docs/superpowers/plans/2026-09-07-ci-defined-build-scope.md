# CI-Defined Build Scope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the project's own CI build on the same commit the only build denominator: the CI target record carries the modules CI built (graded by source), attainment grades SAG's receipt-bound modules against them, and the local surfaces stop presenting scan-based scope as a rate or a verdict input.

**Architecture:** Part A works on the external-target side — a shared module-key grammar (`sag.metrics.module_keys`), a target-record schema bump that lets a cell carry `modules` + `modules_basis` + `command`, three harvester rungs that fill them (JUnit pool paths → `test_bearing`; job logs → `log`; settings.gradle/pom under the CI command → `declared`), a lifecycle-parity axis, and the attainment build axis reading all of it, pinned by a kafka acceptance test on archived evidence. Part B works on the local side — removes `source_scope_coverage` and the N/N sources line, decouples the verdict word from module bands, makes scan-scope conflicts non-capping, and rewrites the Build/Tests headline lines, the web model, and the README. The two parts are independent and may be executed by separate batches in either order.

**Tech Stack:** Python 3.10+, pydantic v2, pytest; `gh api` for the courier (network tasks only); React/TypeScript + vitest for the Web UI.

**Spec:** `docs/superpowers/specs/2026-09-07-ci-defined-build-scope-design.md` (decisions), SAG-MS-1 Part IV §22 (`docs/superpowers/specs/2026-08-27-sag-ms-1-measurement-standard.md`) for the attainment algebra.

## Global Constraints

- Counts come only from authoritative artifacts (receipts, sealed snapshots, harvested CI evidence); never from console text, model claims, or a disk scan used as a denominator.
- `unavailable` is never rendered as `0` or `100%`; a bounded count renders as `≥N`; no percentage is ever derived from an unproven denominator.
- Removing evidence must never improve a verdict (MS-1 P4).
- No compatibility machinery: `TARGET_RECORD_SCHEMA_VERSION` bumps 1 → 2 as a strict single-version equality (owner decision 2026-09-01, same as receipts v3). Frozen d3 v1 records are re-assembled offline from their archived snapshots; pins do not move.
- User-facing copy (CLI headline lines, Web UI, README) is plain English; internal vocabulary (receipt, sealed, identity, laundered) stays in engine strings and docs only.
- Commit style: no `Co-Authored-By` trailers; never `git stash`; never bypass hooks; `docs/` requires `git add -f`.
- Test runner: `.venv/bin/python -m pytest -q` from the main checkout; Web UI: `cd webui && npx vitest run` and `npm run build` (the built assets under `src/sag/web/static/assets/` are committed).
- Network is used only by the courier tasks (A6) and only through `gh api`; every other task is offline and must run without credentials.

---

## File structure

**Part A (external target side)**

- Create `src/sag/metrics/module_keys.py` — the one module-name grammar both sides speak (`module_key`, `module_keys`).
- Modify `src/sag/metrics/target_record.py` — schema v2; `CellTarget.modules_basis`, `CellTarget.command`; canonical-key validation.
- Modify `src/sag/metrics/ci_vetting.py` — `extract_build_commands` (the step walker already exists for laundering; this reads the `run:` text the same way).
- Create `src/sag/metrics/ci_logs.py` — Gradle task lines / Maven Reactor Summary → modules; job-log zip reader.
- Create `src/sag/metrics/build_scope.py` — `parse_ci_command`, `gradle_declared_projects`, `maven_declared_modules`, `maven_default_goal`.
- Create `src/sag/metrics/parity.py` — `command_parity` → `LifecycleParity`.
- Modify `src/sag/metrics/attainment.py` — module-key normalization, `build_form`, `modules_basis`, `unmatched_observed_module_ids`, `lifecycle_parity`, `CertificateView.commands`.
- Modify `scripts/d3_harvest_target.py` — rungs wired into `read_pool` / `cell_from_pool` / `assemble_target_record`; courier `--fetch-logs` / `--fetch-sources`.
- Tests: `tests/test_module_keys.py`, `tests/test_target_record.py`, `tests/test_ci_vetting.py`, `tests/test_ci_logs.py`, `tests/test_build_scope.py`, `tests/test_parity.py`, `tests/test_attainment.py`, `tests/test_d3_harvester.py`, `tests/test_kafka_build_scope_acceptance.py`.

**Part B (local surfaces)**

- Modify `src/sag/verdict.py` — `BUILD_SCOPE_CONFLICTS` join `ADJUDICATED_CONFLICTS`.
- Modify `src/sag/verdict_rates.py` — delete `source_scope_coverage` and `_BUILD_SCOPE_CONFLICTS`; `derived_verdict_word(build_judgment, test_cases)`; new `render_snapshot_metric_lines` Build/Tests lines.
- Modify `src/sag/agent/verdict_finalizer.py` — both `derived_verdict_word` call sites; `_physical_judgment` no longer reads JVM scan completeness as `partial`.
- Modify `src/sag/agent/physical_validator.py` — `evidence["build_system"]` on the physical status.
- Modify `src/sag/web/models.py`, `src/sag/web/session_registry.py`, `src/sag/web/demo_data.py` — delete `SourceScopeSummary` / `source_scope`.
- Modify `webui/src/api/types.ts`, `webui/src/evidencePresentation.ts`, `webui/src/pages/detail/OverviewTab.tsx` (+ tests) — delete source scope; module scan is a diagnostic sentence.
- Modify `README.md` — *Reading a Result* and *Measuring against the project's CI*.

---

# Part A — the CI cell carries its build universe

### Task A1: One module grammar

**Files:**
- Create: `src/sag/metrics/module_keys.py`
- Test: `tests/test_module_keys.py`

**Interfaces:**
- Produces: `module_key(raw: object) -> str` (raises `ValueError` on empty), `module_keys(values: Iterable[object]) -> tuple[str, ...]` (sorted, unique).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_module_keys.py
"""The one grammar for a module name, on both sides of the CI comparison.

CI JUnit pools and Gradle task lines spell a module as a Gradle project path
(`:connect:api`); SAG's evidence trees and receipts spell it as a directory
(`connect/api`); Maven prints a reactor display name (`Apache Camel :: Core`)
on both sides. One key, so a count and its identity cannot disagree by
punctuation alone.
"""

import pytest

from sag.metrics.module_keys import module_key, module_keys


@pytest.mark.parametrize(
    "raw, key",
    [
        (":connect:api", "connect/api"),
        ("connect/api", "connect/api"),
        ("connect/api/", "connect/api"),
        ("./connect/api", "connect/api"),
        (":clients", "clients"),
        (":root", "."),
        (":", "."),
        (".", "."),
        ("./", "."),
        ("ignite-checkstyle", "ignite-checkstyle"),
        ("Apache Camel :: Core", "Apache Camel :: Core"),
        ("  Jackrabbit   JCR Commons ", "Jackrabbit JCR Commons"),
        (":storage:storage-api", "storage/storage-api"),
    ],
)
def test_module_key_canonical_forms(raw, key):
    assert module_key(raw) == key


def test_module_key_is_idempotent():
    for raw in (":connect:api", "connect/api", "Apache Camel :: Core", ":root"):
        assert module_key(module_key(raw)) == module_key(raw)


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_an_empty_identity_is_refused(raw):
    with pytest.raises(ValueError):
        module_key(raw)


def test_module_keys_sorts_and_dedupes_across_spellings():
    assert module_keys([":connect:api", "connect/api", ":clients", "clients/"]) == (
        "clients",
        "connect/api",
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_module_keys.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'sag.metrics.module_keys'`

- [ ] **Step 3: Write the implementation**

```python
# src/sag/metrics/module_keys.py
"""The one grammar for a module name, on both sides of the CI comparison.

A canonical key is a directory-style path relative to the build root
(`connect/api`), with the root project spelled ``"."``.  Gradle project
paths (`:connect:api`, `:root`) translate into it; a Maven reactor display
name (what the Reactor Summary prints, and what SAG's Maven receipts record)
contains whitespace and passes through unchanged — it is the same string on
both sides already, and rewriting its punctuation would only break that.

This module is pure and depends on nothing in the package, so every other
metrics module may import it.
"""

from __future__ import annotations

from typing import Iterable

_ROOT_SPELLINGS = frozenset({":root", ":", ".", "./"})


def module_key(raw: object) -> str:
    """Return the canonical key for one module identity; refuse an empty one."""

    text = " ".join(str(raw if raw is not None else "").split())
    if not text:
        raise ValueError("module identity is empty")
    if " " in text:
        # A reactor display name.  Whitespace never appears in a Gradle project
        # path or a directory key, so this is the one reliable tell.
        return text
    if text in _ROOT_SPELLINGS:
        return "."
    text = text.lstrip(":").replace(":", "/")
    if text.startswith("./"):
        text = text[2:]
    text = text.strip("/")
    return text or "."


def module_keys(values: Iterable[object]) -> tuple[str, ...]:
    """Canonical keys of every identity, sorted and de-duplicated."""

    return tuple(sorted({module_key(value) for value in values}))


__all__ = ["module_key", "module_keys"]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest -q tests/test_module_keys.py`
Expected: PASS (17 tests)

- [ ] **Step 5: Commit**

```bash
git add src/sag/metrics/module_keys.py tests/test_module_keys.py
git commit -m "feat: one module grammar for both sides of the CI comparison"
```

---

### Task A2: The cell carries its build universe (schema v2)

**Files:**
- Modify: `src/sag/metrics/target_record.py:20-40` (constants), `:60-126` (`CellTarget`), `:128-135` (`TargetRecord.schema_version`)
- Test: `tests/test_target_record.py`

**Interfaces:**
- Consumes: `module_key` from A1.
- Produces: `ModulesBasis = Literal["log", "declared", "test_bearing"]`; `CellTarget.modules_basis: ModulesBasis | None`; `CellTarget.command: str | None`; `TARGET_RECORD_SCHEMA_VERSION: Literal[2] = 2`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_target_record.py`)

```python
from sag.metrics.target_record import TARGET_RECORD_SCHEMA_VERSION, CellTarget


def _grade_b(**overrides) -> CellTarget:
    payload = {"cell_id": "build (17)", "build": "ok", "executed_count": 0, "red_count": 0, "grade": "B"}
    payload.update(overrides)
    return CellTarget(**payload)


def test_schema_version_is_two_and_strict():
    assert TARGET_RECORD_SCHEMA_VERSION == 2


def test_modules_need_a_basis_and_a_basis_needs_modules():
    with pytest.raises(ValueError, match="basis"):
        _grade_b(modules=("clients",))
    with pytest.raises(ValueError, match="modules"):
        _grade_b(modules_basis="log")


def test_modules_must_be_canonical_keys():
    with pytest.raises(ValueError, match="canonical"):
        _grade_b(modules=(":connect:api",), modules_basis="log")
    cell = _grade_b(modules=("connect/api", "clients"), modules_basis="test_bearing")
    assert cell.modules == ("clients", "connect/api")
    assert cell.modules_basis == "test_bearing"


def test_command_is_bounded_text_or_absent():
    assert _grade_b().command is None
    assert _grade_b(command="  mvn -V test --file pom.xml ").command == "mvn -V test --file pom.xml"
    with pytest.raises(ValueError):
        _grade_b(command="x" * 2_001)
    with pytest.raises(ValueError, match="command"):
        _grade_b(command="   ")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_target_record.py -k "schema_version or basis or canonical or command"`
Expected: FAIL — `assert 1 == 2`, and `ValidationError: Extra inputs are not permitted` for `modules_basis`/`command`.

- [ ] **Step 3: Implement the schema**

In `src/sag/metrics/target_record.py`:

```python
# constants block: replace the version line and add the basis alias
from sag.metrics.module_keys import module_key

TARGET_RECORD_SCHEMA_VERSION: Literal[2] = 2
# Where a cell's module universe was read from, best first: a job log names
# every module the build ran; the declared reactor names every module the CI
# command selects; JUnit pool paths name only the modules that ran tests.
ModulesBasis = Literal["log", "declared", "test_bearing"]
```

In `CellTarget`, add two fields after `modules`:

```python
    modules_basis: ModulesBasis | None = None
    # The build/test command the CI job ran, as its workflow wrote it.
    command: str | None = Field(default=None, max_length=2_000)
```

In `CellTarget._validate_identity_sets`, after `modules = _canonical_ids(self.modules, label="modules")` add:

```python
        for name in modules:
            if module_key(name) != name:
                raise ValueError(f"module identity {name!r} is not canonical")
        if modules and self.modules_basis is None:
            raise ValueError("modules need a basis stating where they were read from")
        if self.modules_basis is not None and not modules:
            raise ValueError("a modules basis needs modules")
        command = self.command
        if command is not None:
            command = " ".join(command.split())
            if not command:
                raise ValueError("command cannot be blank")
```

and before the final `return self`:

```python
        object.__setattr__(self, "command", command)
```

In `TargetRecord`:

```python
    schema_version: Literal[2] = TARGET_RECORD_SCHEMA_VERSION
```

- [ ] **Step 4: Run the record and attainment tests**

Run: `.venv/bin/python -m pytest -q tests/test_target_record.py tests/test_attainment.py tests/test_d3_harvester.py tests/test_d3_selector.py`
Expected: PASS. If a test builds a record with a literal `"schema_version": 1`, change it to 2 — there is no dual-version reader.

- [ ] **Step 5: Commit**

```bash
git add src/sag/metrics/target_record.py tests/test_target_record.py tests/test_attainment.py tests/test_d3_harvester.py
git commit -m "feat: a CI cell states the modules it built and where that list was read from"
```

---

### Task A3: Rung `test_bearing` — modules from JUnit pool paths

**Files:**
- Modify: `scripts/d3_harvest_target.py:159-168` (`PoolReading`), `:234-262` (`read_pool`), `:293-328` (`cell_from_pool`)
- Test: `tests/test_d3_harvester.py`

**Interfaces:**
- Consumes: `module_key`, `module_keys` (A1); `ModulesBasis` (A2).
- Produces: `PoolReading.modules: tuple[str, ...]`, `PoolReading.layout: str`; `pool_member_module(name: str) -> str`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_d3_harvester.py`; `_suite`, `_pool`, `build_snapshot`, `_record`, `_cell` already exist there)

```python
from scripts.d3_harvest_target import pool_member_module, read_pool


@pytest.mark.parametrize(
    "member, module",
    [
        ("clients/17-noflaky-nonew/TEST-a.xml", "clients"),
        ("connect/runtime/17-noflaky-nonew/TEST-a.xml", "connect/runtime"),
        ("streams/integration-tests/17-noflaky-nonew/TEST-a.xml", "streams/integration-tests"),
        ("core/build/test-results/test/TEST-a.xml", "core"),
        ("build/test-results/test/TEST-a.xml", "."),
        ("cli/target/surefire-reports/TEST-a.xml", "cli"),
        ("target/failsafe-reports/TEST-a.xml", "."),
        ("cli/TEST-a.xml", "cli"),
        ("TEST-a.xml", "."),
    ],
)
def test_pool_member_module_reads_every_upload_layout(member, module):
    assert pool_member_module(member) == module


def test_read_pool_states_the_test_bearing_modules(tmp_path):
    pool = _pool(
        tmp_path,
        "junit-xml-17-noflaky-nonew",
        {
            "clients/17-noflaky-nonew/TEST-a.xml": _suite("a.A", (("t1", False),)),
            "connect/runtime/17-noflaky-nonew/TEST-b.xml": _suite("b.B", (("t2", False),)),
            "clients/17-noflaky-nonew/TEST-c.xml": _suite("c.C", (("t3", True),)),
        },
    )

    reading = read_pool(pool)

    assert reading.modules == ("clients", "connect/runtime")
    assert reading.layout == "upload-prefix"


def test_a_pool_cell_carries_its_modules_as_a_lower_bound(tmp_path):
    snapshot = build_snapshot(
        tmp_path / "snap",
        pools={
            "junit-xml-17-noflaky-nonew": {
                "clients/17-noflaky-nonew/TEST-a.xml": _suite("a.A", (("t1", False),)),
                "core/17-noflaky-nonew/TEST-b.xml": _suite("b.B", (("t2", False),)),
            }
        },
    )

    record = _record(snapshot)
    cell = _cell(record, "junit-xml-17-noflaky-nonew")

    assert cell.modules == ("clients", "core")
    assert cell.modules_basis == "test_bearing"
    assert _has_note(record, "test-bearing lower bound")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_d3_harvester.py -k "pool_member_module or test_bearing or states_the_test_bearing"`
Expected: FAIL with `ImportError: cannot import name 'pool_member_module'`

- [ ] **Step 3: Implement the rung**

In `scripts/d3_harvest_target.py`, add the import `from sag.metrics.module_keys import module_key, module_keys` beside the other `sag.metrics` imports, then:

```python
# Where a report lives inside an uploaded pool.  A pool that uploaded whole
# report directories keeps the build's own marker; kafka's upload flattens to
# `<module>/<pool-id>/<file>`, so the module is everything before the last two
# segments; a single-file upload is the root.
_REPORT_DIR_MARKERS = (
    "build/test-results/",
    "target/surefire-reports/",
    "target/failsafe-reports/",
)


def pool_member_module(name: str) -> str:
    """The canonical module key one pool member's path encodes."""

    path = name.strip("/")
    for marker in _REPORT_DIR_MARKERS:
        head, separator, _ = path.partition(marker)
        if separator:
            return module_key(head or ".")
    parts = path.split("/")
    if len(parts) >= 3:
        return module_key("/".join(parts[:-2]))
    if len(parts) == 2:
        return module_key(parts[0])
    return "."


def _pool_layout(names: list[str]) -> str:
    if any(marker in name for name in names for marker in _REPORT_DIR_MARKERS):
        return "report-directory"
    if any(name.count("/") >= 2 for name in names):
        return "upload-prefix"
    return "flat"
```

Extend `PoolReading`:

```python
class PoolReading(BaseModel):
    """One JUnit pool zip, parsed and deconvolved."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pool_id: str
    artifact: str
    deconvolved: Deconvolved
    unreadable: tuple[str, ...] = ()
    # The test-bearing modules the member paths encode, and which path layout
    # the reading recognised — the record discloses both.
    modules: tuple[str, ...] = ()
    layout: str = "flat"
```

Replace `read_pool`:

```python
def read_pool(pool_path: Path) -> PoolReading:
    """Parse and deconvolve every JUnit XML in one pool zip.

    Sorted-name order is the document order deconvolution reads: a retry lives
    beside its first attempt in the same suite file, and sorting keeps every
    pool's assembly reproducible.  The member paths are read a second way, for
    the modules they name: a pool proves a module ran tests, which is the
    lower bound of what the build built.
    """

    entries: list[TestEntry] = []
    unreadable: list[str] = []
    modules: list[str] = []
    names: list[str] = []
    try:
        with zipfile.ZipFile(pool_path) as archive:
            names = sorted(name for name in archive.namelist() if name.endswith(".xml"))
            for name in names:
                try:
                    entries.extend(parse_junit_entries(archive.read(name)))
                except ValueError:
                    unreadable.append(name)
                    continue
                modules.append(pool_member_module(name))
    except (OSError, zipfile.BadZipFile) as exc:
        raise HarvestError(f"{pool_path.name} is not a readable pool archive: {exc}") from exc

    return PoolReading(
        pool_id=pool_path.stem,
        artifact=pool_path.name,
        deconvolved=deconvolve(tuple(entries)),
        unreadable=tuple(unreadable),
        modules=module_keys(modules) if modules else (),
        layout=_pool_layout(names),
    )
```

In `cell_from_pool`, before `cell = CellTarget(`:

```python
    if reading.modules:
        notes.append(
            f"cell {cell_id}: modules are the test-bearing lower bound read from the "
            f"pool's report paths ({len(reading.modules)} modules, layout {reading.layout}); "
            "modules without tests are not visible here"
        )
```

and pass `modules=reading.modules, modules_basis="test_bearing" if reading.modules else None,` to the `CellTarget(...)` call.

- [ ] **Step 4: Run the harvester tests**

Run: `.venv/bin/python -m pytest -q tests/test_d3_harvester.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/d3_harvest_target.py tests/test_d3_harvester.py
git commit -m "feat: a JUnit pool names the modules that ran tests, as the lower bound it is"
```

---

### Task A4: Rung `log` — modules from the CI job log

**Files:**
- Create: `src/sag/metrics/ci_logs.py`
- Modify: `scripts/d3_harvest_target.py` (`assemble_target_record`, after the check loop)
- Test: `tests/test_ci_logs.py`, `tests/test_d3_harvester.py`

**Interfaces:**
- Consumes: `module_key`, `module_keys` (A1).
- Produces: `LogModules(tool: Literal["gradle","maven"] | None, modules: tuple[str,...], skipped: int, failed: int)`; `modules_from_log(text: str) -> LogModules`; `job_logs(zip_path: Path) -> dict[str, str]` (job name → log text); harvester constant `LOGS_GLOB = "run-*-logs.zip"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ci_logs.py
"""A CI job log names every module the build ran — the exact build universe.

GitHub prefixes every line with a timestamp; the readers search within the
line.  Gradle prints one `> Task :path:compileJava` per compiled project;
Maven prints a Reactor Summary once, with the display name it also prints in
SAG's own Maven receipts.
"""

import zipfile

from sag.metrics.ci_logs import job_logs, modules_from_log

GRADLE_LOG = """
2026-06-17T21:40:01.1Z > Task :clients:compileJava
2026-06-17T21:40:02.1Z > Task :connect:runtime:compileJava FROM-CACHE
2026-06-17T21:40:03.1Z > Task :compileJava NO-SOURCE
2026-06-17T21:40:04.1Z > Task :clients:test
2026-06-17T21:40:05.1Z > Task :storage:storage-api:classes UP-TO-DATE
2026-06-17T21:40:06.1Z > Task :docs:javadoc
"""

MAVEN_REACTOR_LOG = """
2026-06-01T07:25:22.4Z [INFO] Reactor Summary for Apache Commons Parent 1.0:
2026-06-01T07:25:22.4Z [INFO]
2026-06-01T07:25:22.4Z [INFO] Apache Commons Parent ............................ SUCCESS [  1.2 s]
2026-06-01T07:25:22.4Z [INFO] Apache Camel :: Core ............................. SUCCESS [ 12.3 s]
2026-06-01T07:25:22.4Z [INFO] ignite-tools ..................................... FAILURE [  0.1 s]
2026-06-01T07:25:22.4Z [INFO] ignite-checkstyle ................................ SKIPPED
2026-06-01T07:25:22.4Z [INFO] ------------------------------------------------------------------------
2026-06-01T07:25:22.4Z [INFO] BUILD FAILURE
"""

MAVEN_SINGLE_LOG = """
2026-06-01T07:25:24.1Z [INFO] Building Apache Tomcat Migration Tool for Jakarta EE 1.0.12
2026-06-01T07:25:31.8Z [INFO]       [jar] Building jar: D:\\a\\x\\target\\test-classes\\cgi-api.jar
2026-06-01T07:25:37.2Z [INFO] Tests run: 52, Failures: 0, Errors: 0, Skipped: 0
2026-06-01T07:25:40.0Z [INFO] BUILD SUCCESS
"""


def test_gradle_compile_tasks_name_the_built_projects():
    result = modules_from_log(GRADLE_LOG)

    assert result.tool == "gradle"
    # Root `:compileJava` is the root project; `:docs:javadoc` is not a compile task.
    assert result.modules == (".", "clients", "connect/runtime", "storage/storage-api")


def test_maven_reactor_summary_counts_success_rows_only():
    result = modules_from_log(MAVEN_REACTOR_LOG)

    assert result.tool == "maven"
    assert result.modules == ("Apache Camel :: Core", "Apache Commons Parent")
    assert result.failed == 1
    assert result.skipped == 1


def test_a_single_module_maven_build_is_the_root():
    result = modules_from_log(MAVEN_SINGLE_LOG)

    assert result.tool == "maven"
    assert result.modules == (".",)


def test_a_log_that_names_no_build_states_nothing():
    result = modules_from_log("2026-06-01T07:25:24.1Z Run actions/checkout@v4\n")

    assert result.tool is None
    assert result.modules == ()


def test_job_logs_map_the_zip_members_to_job_names(tmp_path):
    path = tmp_path / "run-1-logs.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("0_JDK11 windows-latest.txt", "a")
        archive.writestr("1_JDK17 ubuntu-latest.txt", "b")
        archive.writestr("JDK17 ubuntu-latest/system.txt", "ignored")
        archive.writestr("JDK17 ubuntu-latest/3_Run tests.txt", "ignored step log")

    assert job_logs(path) == {"JDK11 windows-latest": "a", "JDK17 ubuntu-latest": "b"}
```

And in `tests/test_d3_harvester.py`:

```python
def test_a_job_log_outranks_the_pool_lower_bound(tmp_path):
    snapshot = build_snapshot(
        tmp_path / "snap",
        pools={
            "junit-xml-17-noflaky-nonew": {
                "clients/17-noflaky-nonew/TEST-a.xml": _suite("a.A", (("t1", False),)),
            }
        },
        jobs=(("JUnit tests Java 17", "success"),),
    )
    with zipfile.ZipFile(snapshot / "run-1-logs.zip", "w") as archive:
        archive.writestr(
            "0_JUnit tests Java 17.txt",
            "> Task :clients:compileJava\n> Task :core:compileJava\n> Task :generator:compileJava\n",
        )

    record = _record(snapshot)
    cell = _cell(record, "junit-xml-17-noflaky-nonew")

    assert cell.modules == ("clients", "core", "generator")
    assert cell.modules_basis == "log"
    assert _has_note(record, "job log")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_ci_logs.py tests/test_d3_harvester.py -k "log"`
Expected: FAIL with `ModuleNotFoundError: No module named 'sag.metrics.ci_logs'`

- [ ] **Step 3: Implement the reader**

```python
# src/sag/metrics/ci_logs.py
"""A CI job log names every module the build ran.

This is the exact rung of a cell's build universe: Gradle prints one
``> Task :path:compileJava`` per compiled project and Maven prints a Reactor
Summary whose display names are the ones SAG's own Maven receipts record.
Pure: the caller hands in text; nothing here touches the network.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from sag.metrics.module_keys import module_key, module_keys

# Tasks that prove a project's sources were compiled or packaged.  Test tasks
# are deliberately absent: a module that ran tests is the pool rung's fact.
_GRADLE_TASK_RE = re.compile(
    r"> Task (?P<path>(?::[A-Za-z0-9_.\-]+)*):"
    r"(?P<task>compileJava|compileKotlin|compileScala|compileGroovy|classes|jar)\b"
)
_MAVEN_SUMMARY_RE = re.compile(
    r"\[INFO\]\s+(?P<name>\S.*?)\s+\.{2,}\s+(?P<status>SUCCESS|FAILURE|SKIPPED)\b"
)
# `[INFO] Building <name> <version>` opens a project build; the packaging
# plugin's `[jar] Building jar:` sits behind more whitespace and a tag.
_MAVEN_BUILDING_RE = re.compile(r"\[INFO\] Building (?P<name>\S.*?)(?: \[\d+/\d+\])?\s*$")
_MAVEN_RESULT_RE = re.compile(r"\[INFO\] BUILD (?P<result>SUCCESS|FAILURE)\b")
_JOB_FILE_RE = re.compile(r"^\d+_(?P<job>.+)\.txt$")


class LogModules(BaseModel):
    """What one job log proves about the build universe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: Literal["gradle", "maven"] | None = None
    modules: tuple[str, ...] = ()
    skipped: int = 0
    failed: int = 0


def modules_from_log(text: str) -> LogModules:
    """The modules a job log proves were built, by the tool that printed it."""

    gradle = [module_key(match.group("path") or ".") for match in _GRADLE_TASK_RE.finditer(text)]
    if gradle:
        return LogModules(tool="gradle", modules=module_keys(gradle))

    rows = list(_MAVEN_SUMMARY_RE.finditer(text))
    if rows:
        built = [match.group("name") for match in rows if match.group("status") == "SUCCESS"]
        return LogModules(
            tool="maven",
            modules=module_keys(built) if built else (),
            skipped=sum(1 for match in rows if match.group("status") == "SKIPPED"),
            failed=sum(1 for match in rows if match.group("status") == "FAILURE"),
        )

    result = _MAVEN_RESULT_RE.search(text)
    building = [
        match for match in (_MAVEN_BUILDING_RE.search(line) for line in text.splitlines()) if match
    ]
    if result is not None and len(building) == 1:
        # One project, no reactor: the root is the whole universe.
        succeeded = result.group("result") == "SUCCESS"
        return LogModules(tool="maven", modules=(".",) if succeeded else (), failed=0 if succeeded else 1)
    return LogModules()


def job_logs(zip_path: Path) -> dict[str, str]:
    """Job name → the job's full log, from a run's downloaded log archive.

    GitHub names the per-job file ``<index>_<job name>.txt`` at the archive
    root; per-step files live under ``<job name>/`` and are not read.
    """

    logs: dict[str, str] = {}
    with zipfile.ZipFile(zip_path) as archive:
        for name in sorted(archive.namelist()):
            if "/" in name:
                continue
            match = _JOB_FILE_RE.match(name)
            if match is None:
                continue
            logs[match.group("job")] = archive.read(name).decode("utf-8", "replace")
    return logs


__all__ = ["LogModules", "job_logs", "modules_from_log"]
```

In `scripts/d3_harvest_target.py`: add `LOGS_GLOB = "run-*-logs.zip"` beside `POOL_GLOB`, import `from sag.metrics.ci_logs import job_logs, modules_from_log`, and add this helper plus its call in `assemble_target_record` immediately after the `if not cells:` guard:

```python
def _with_modules(cell: CellTarget, modules: tuple[str, ...], basis: str) -> CellTarget:
    """A cell restated with a module universe; re-validated, never patched."""

    payload = cell.model_dump(mode="json")
    payload.update(modules=modules, modules_basis=basis)
    return CellTarget(**payload)


def _cell_for_job(cells: list[CellTarget], pool_ids: list[str], job_name: str) -> int | None:
    """Index of the cell a job log belongs to: its own check, or the pool covering it."""

    for index, cell in enumerate(cells):
        if cell.cell_id == job_name:
            return index
    covering = next((pool_id for pool_id in pool_ids if pool_covers_check(pool_id, job_name)), None)
    if covering is None:
        return None
    wanted = pool_cell_id(covering)
    return next((index for index, cell in enumerate(cells) if cell.cell_id == wanted), None)


def apply_job_logs(
    snapshot_dir: Path, cells: list[CellTarget], pool_ids: list[str]
) -> tuple[str, ...]:
    """Replace pool lower bounds with the exact universe a job log proves."""

    notes: list[str] = []
    for zip_path in sorted(snapshot_dir.glob(LOGS_GLOB)):
        try:
            logs = job_logs(zip_path)
        except (OSError, zipfile.BadZipFile) as exc:
            notes.append(f"{zip_path.name} could not be read as a log archive: {exc}")
            continue
        for job_name, text in logs.items():
            index = _cell_for_job(cells, pool_ids, job_name)
            if index is None:
                continue
            found = modules_from_log(text)
            if not found.modules:
                continue
            cells[index] = _with_modules(cells[index], found.modules, "log")
            detail = f"{len(found.modules)} modules built"
            if found.failed or found.skipped:
                detail += f", {found.failed} failed, {found.skipped} skipped"
            notes.append(
                f"cell {cells[index].cell_id}: modules read from the job log "
                f"{zip_path.name}/{job_name} ({found.tool}; {detail})"
            )
    return tuple(notes)
```

Call site in `assemble_target_record` (after `if not cells: raise ...`):

```python
    notes.extend(apply_job_logs(snapshot_dir, cells, pool_ids))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest -q tests/test_ci_logs.py tests/test_d3_harvester.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/sag/metrics/ci_logs.py scripts/d3_harvest_target.py tests/test_ci_logs.py tests/test_d3_harvester.py
git commit -m "feat: a job log states the exact modules a CI build ran"
```

---

### Task A5: Rung `declared` — the reactor the CI command selects

**Files:**
- Create: `src/sag/metrics/build_scope.py`
- Modify: `src/sag/metrics/ci_vetting.py` (add `extract_build_commands`)
- Modify: `scripts/d3_harvest_target.py` (`assemble_target_record`: sources + commands)
- Test: `tests/test_build_scope.py`, `tests/test_ci_vetting.py`, `tests/test_d3_harvester.py`

**Interfaces:**
- Consumes: `module_key`, `module_keys` (A1); `_with_modules` (A4).
- Produces: `CiCommand(tool, goals, profiles, projects, excluded_tasks, text)`; `parse_ci_command(text) -> CiCommand`; `gradle_declared_projects(settings_text) -> tuple[str, ...]` (canonical dir keys, root included); `maven_declared_modules(root_pom: str, read_pom: Callable[[str], str | None], *, active_profiles=(), projects=()) -> tuple[str, ...]` (reactor display names, the Maven grammar); `maven_default_goal(pom_text) -> str | None`; `extract_build_commands(yaml_text) -> tuple[BuildCommandStep, ...]` with `BuildCommandStep(job_id, job_name_template, text)`; harvester constant `SOURCES_DIR = "sources"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_build_scope.py
"""The reactor a CI command selects on one commit — exact for the declared scope.

`settings.gradle` includes name Gradle project paths and may remap a project
directory (kafka: `:storage:storage-api` lives in `storage/api`); a Maven
reactor is the `<modules>` tree, widened by the profiles the command
activates and narrowed by its `-pl`.  Maven modules are stated by the display
name the Reactor Summary prints, so the log rung and this rung agree.
"""

import pytest

from sag.metrics.build_scope import (
    gradle_declared_projects,
    maven_declared_modules,
    maven_default_goal,
    parse_ci_command,
)

KAFKA_SETTINGS = """
rootProject.name = 'kafka'
include 'clients',
    'connect:api',
    'connect:runtime',
    'core',
    'storage:storage-api'
include("streams", "streams:examples")
project(":storage:storage-api").projectDir = file("storage/api")
"""

ROOT_POM = """<?xml version="1.0"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <artifactId>parent</artifactId>
  <name>Ignite Parent</name>
  <build><defaultGoal>clean verify</defaultGoal></build>
  <modules>
    <module>modules/core</module>
    <module>modules/tools</module>
  </modules>
  <profiles>
    <profile>
      <id>checkstyle</id>
      <modules><module>modules/checkstyle</module></modules>
    </profile>
  </profiles>
</project>
"""
CHILD_POMS = {
    "modules/core": "<project><artifactId>ignite-core</artifactId><name>ignite-core</name></project>",
    "modules/tools": "<project><artifactId>ignite-tools</artifactId></project>",
    "modules/checkstyle": "<project><artifactId>ignite-checkstyle</artifactId><name>Ignite Checkstyle</name></project>",
}


def test_gradle_settings_name_every_included_project_by_directory():
    assert gradle_declared_projects(KAFKA_SETTINGS) == (
        ".",
        "clients",
        "connect/api",
        "connect/runtime",
        "core",
        "storage/api",
        "streams",
        "streams/examples",
    )


def test_maven_reactor_uses_display_names_and_profiles_widen_it():
    read = CHILD_POMS.get

    assert maven_declared_modules(ROOT_POM, read) == ("Ignite Parent", "ignite-core", "ignite-tools")
    assert maven_declared_modules(ROOT_POM, read, active_profiles=("checkstyle",)) == (
        "Ignite Checkstyle",
        "Ignite Parent",
        "ignite-core",
        "ignite-tools",
    )


def test_maven_pl_narrows_by_directory_or_artifact_id():
    read = CHILD_POMS.get

    assert maven_declared_modules(ROOT_POM, read, projects=("modules/core",)) == ("ignite-core",)
    assert maven_declared_modules(ROOT_POM, read, projects=("ignite-tools",)) == ("ignite-tools",)


def test_maven_default_goal_is_read_or_absent():
    assert maven_default_goal(ROOT_POM) == "clean verify"
    assert maven_default_goal("<project><artifactId>x</artifactId></project>") is None


@pytest.mark.parametrize(
    "text, tool, goals, profiles, projects, excluded",
    [
        ("mvn -V test --file pom.xml --no-transfer-progress", "maven", ("test",), (), (), ()),
        ("./mvnw -B -Pfast,ci -pl core,tools -am verify checkstyle:check", "maven", ("verify", "checkstyle:check"), ("fast", "ci"), ("core", "tools"), ()),
        ("./gradlew --info build -x test -x javadoc --scan", "gradle", ("build",), (), (), ("test", "javadoc")),
        ("mvn", "maven", (), (), (), ()),
        ("echo hi && npm test", "unknown", (), (), (), ()),
    ],
)
def test_parse_ci_command(text, tool, goals, profiles, projects, excluded):
    command = parse_ci_command(text)

    assert command.tool == tool
    assert command.goals == goals
    assert command.profiles == profiles
    assert command.projects == projects
    assert command.excluded_tasks == excluded


def test_a_multi_line_run_script_takes_its_build_line():
    command = parse_ci_command("set -e\nexport JAVA_HOME=/x\n./gradlew test\necho done\n")

    assert command.tool == "gradle"
    assert command.goals == ("test",)
    assert command.text == "./gradlew test"
```

In `tests/test_ci_vetting.py` (append):

```python
from sag.metrics.ci_vetting import extract_build_commands

MATRIX_WORKFLOW = """
name: CI
on: [push]
jobs:
  test:
    name: JDK${{ matrix.java }} ${{ matrix.os }}
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - run: mvn -V test --file pom.xml --no-transfer-progress
  lint:
    steps:
      - run: echo lint
"""


def test_extract_build_commands_names_the_job_and_its_build_step():
    steps = extract_build_commands(MATRIX_WORKFLOW)

    assert len(steps) == 1
    assert steps[0].job_id == "test"
    assert steps[0].job_name_template == "JDK${{ matrix.java }} ${{ matrix.os }}"
    assert steps[0].text == "mvn -V test --file pom.xml --no-transfer-progress"


def test_an_unparseable_workflow_yields_no_commands():
    assert extract_build_commands("jobs: [") == ()
```

In `tests/test_d3_harvester.py` (append; `build_snapshot` accepts `workflow=` already):

```python
def test_declared_sources_fill_modules_when_no_log_exists(tmp_path):
    workflow = tmp_path / "ci.yml"
    workflow.write_text(
        "jobs:\n  build:\n    name: build (${{ matrix.java }})\n    steps:\n"
        "      - run: ./gradlew build -x test\n",
        encoding="utf-8",
    )
    snapshot = build_snapshot(
        tmp_path / "snap", jobs=(("build (17)", "success"),), workflow=workflow
    )
    sources = snapshot / "sources"
    sources.mkdir()
    (sources / "settings.gradle").write_text(
        "include 'clients', 'core'\nproject(':core').projectDir = file('kafka-core')\n",
        encoding="utf-8",
    )

    record = _record(snapshot)
    cell = _cell(record, "build (17)")

    assert cell.modules == (".", "clients", "kafka-core")
    assert cell.modules_basis == "declared"
    assert cell.command == "./gradlew build -x test"
    assert _has_note(record, "declared reactor")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_build_scope.py tests/test_ci_vetting.py tests/test_d3_harvester.py -k "declared or parse_ci_command or extract_build_commands or gradle_settings or maven_"`
Expected: FAIL with `ModuleNotFoundError: No module named 'sag.metrics.build_scope'` and `ImportError: cannot import name 'extract_build_commands'`

- [ ] **Step 3: Implement the parsers**

```python
# src/sag/metrics/build_scope.py
"""The reactor a CI command selects on one commit.

Exact for the declared scope and subject to no log cliff: `settings.gradle`
includes (with `projectDir` remaps) or the pom `<modules>` tree under the
command's `-P` and `-pl`.  Pure: the caller supplies file text and a reader
for child poms; nothing here touches a filesystem or the network.
"""

from __future__ import annotations

import re
import shlex
import xml.etree.ElementTree as ET
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict

from sag.metrics.module_keys import module_key, module_keys

_BUILD_TOOLS = {
    "mvn": "maven",
    "mvnw": "maven",
    "./mvnw": "maven",
    "gradle": "gradle",
    "gradlew": "gradle",
    "./gradlew": "gradle",
}
# Flags whose value is the NEXT token.
_MAVEN_VALUED = {"-P", "--activate-profiles", "-pl", "--projects", "-f", "--file", "-s", "--settings", "-T", "--threads", "-rf", "--resume-from", "-l", "--log-file"}
_GRADLE_VALUED = {"-x", "--exclude-task", "--max-workers", "-p", "--project-dir", "-b", "--build-file", "-c", "--settings-file", "-I", "--init-script"}

_GRADLE_INCLUDE_RE = re.compile(r"""\binclude\s*\(?\s*((?:['"][^'"]+['"]\s*,?\s*)+)\)?""")
_QUOTED_RE = re.compile(r"""['"]([^'"]+)['"]""")
_GRADLE_PROJECTDIR_RE = re.compile(
    r"""project\s*\(\s*['"](?P<path>:?[^'"]+)['"]\s*\)\s*\.projectDir\s*=\s*"""
    r"""(?:new\s+File\s*\(|file\s*\()\s*(?:rootDir\s*,\s*|rootProject\.projectDir\s*,\s*)?['"](?P<dir>[^'"]+)['"]"""
)


class CiCommand(BaseModel):
    """One build/test invocation as a workflow wrote it, taken apart."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: Literal["maven", "gradle", "unknown"] = "unknown"
    goals: tuple[str, ...] = ()
    profiles: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    excluded_tasks: tuple[str, ...] = ()
    text: str = ""


def _build_line(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        try:
            tokens = shlex.split(stripped)
        except ValueError:
            tokens = stripped.split()
        if any(token in _BUILD_TOOLS for token in tokens):
            return stripped
    return None


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def parse_ci_command(text: str) -> CiCommand:
    """Take one workflow `run:` text apart; unknown tools stay unknown."""

    line = _build_line(text or "")
    if line is None:
        return CiCommand(text=" ".join((text or "").split())[:2_000])
    try:
        tokens = shlex.split(line)
    except ValueError:
        tokens = line.split()
    start = next(index for index, token in enumerate(tokens) if token in _BUILD_TOOLS)
    tool = _BUILD_TOOLS[tokens[start]]
    args = tokens[start + 1 :]
    # Anything after a shell operator belongs to another command.
    for stop in ("&&", "||", ";", "|"):
        if stop in args:
            args = args[: args.index(stop)]
    goals: list[str] = []
    profiles: list[str] = []
    projects: list[str] = []
    excluded: list[str] = []
    valued = _MAVEN_VALUED if tool == "maven" else _GRADLE_VALUED
    index = 0
    while index < len(args):
        token = args[index]
        if token in valued:
            value = args[index + 1] if index + 1 < len(args) else ""
            if tool == "maven" and token in {"-P", "--activate-profiles"}:
                profiles.extend(_split_csv(value))
            elif tool == "maven" and token in {"-pl", "--projects"}:
                projects.extend(_split_csv(value))
            elif tool == "gradle" and token in {"-x", "--exclude-task"}:
                excluded.append(value)
            index += 2
            continue
        if tool == "maven" and token.startswith("-P") and len(token) > 2:
            profiles.extend(_split_csv(token[2:]))
        elif tool == "maven" and token.startswith("-pl") and len(token) > 3:
            projects.extend(_split_csv(token[3:]))
        elif token.startswith("-"):
            pass
        else:
            goals.append(token)
        index += 1
    return CiCommand(
        tool=tool,
        goals=tuple(goals),
        profiles=tuple(profiles),
        projects=tuple(projects),
        excluded_tasks=tuple(excluded),
        text=" ".join(tokens),
    )


def gradle_declared_projects(settings_text: str) -> tuple[str, ...]:
    """Every included Gradle project as a canonical directory key, root included."""

    remaps = {
        module_key(match.group("path")): module_key(match.group("dir"))
        for match in _GRADLE_PROJECTDIR_RE.finditer(settings_text)
    }
    paths: list[str] = ["."]
    for include in _GRADLE_INCLUDE_RE.finditer(settings_text):
        for quoted in _QUOTED_RE.findall(include.group(1)):
            key = module_key(quoted)
            paths.append(remaps.get(key, key))
    return module_keys(paths)


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child_text(node: ET.Element, name: str) -> str | None:
    for child in node:
        if _strip_ns(child.tag) == name and child.text and child.text.strip():
            return child.text.strip()
    return None


def _children(node: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in node if _strip_ns(child.tag) == name]


def _parse_pom(text: str) -> ET.Element | None:
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        return None


def maven_default_goal(pom_text: str) -> str | None:
    """`<build><defaultGoal>` of a pom, which a bare `mvn` runs."""

    root = _parse_pom(pom_text)
    if root is None:
        return None
    for build in _children(root, "build"):
        goal = _child_text(build, "defaultGoal")
        if goal:
            return " ".join(goal.split())
    return None


def maven_declared_modules(
    root_pom: str,
    read_pom: Callable[[str], str | None],
    *,
    active_profiles: tuple[str, ...] = (),
    projects: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """The reactor under `-P`/`-pl`, each module as the name Maven prints.

    ``read_pom(relative_dir)`` returns a child pom's text or ``None``; a child
    that cannot be read is named by its directory so the gap stays visible.
    ``projects`` narrows to modules named by directory or artifactId; ``-am``
    is not modelled (the narrowed set is stated as the declared scope).
    """

    selected = set(projects)
    names: list[str] = []
    seen: set[str] = set()

    def visit(directory: str, text: str | None) -> None:
        if directory in seen:
            return
        seen.add(directory)
        root = _parse_pom(text) if text else None
        artifact_id = _child_text(root, "artifactId") if root is not None else None
        name = (_child_text(root, "name") if root is not None else None) or artifact_id or directory
        if not selected or directory in selected or artifact_id in selected:
            names.append(name)
        if root is None:
            return
        children: list[str] = []
        for modules in _children(root, "modules"):
            children.extend(text.strip() for text in (m.text or "" for m in _children(modules, "module")) if text.strip())
        for profiles in _children(root, "profiles"):
            for profile in _children(profiles, "profile"):
                if _child_text(profile, "id") in active_profiles:
                    for modules in _children(profile, "modules"):
                        children.extend(text.strip() for text in (m.text or "" for m in _children(modules, "module")) if text.strip())
        for child in children:
            child_dir = module_key(child if directory == "." else f"{directory}/{child}")
            visit(child_dir, read_pom(child_dir))

    visit(".", root_pom)
    return module_keys(names)


__all__ = [
    "CiCommand",
    "gradle_declared_projects",
    "maven_declared_modules",
    "maven_default_goal",
    "parse_ci_command",
]
```

In `src/sag/metrics/ci_vetting.py`, add after `LaunderingVet`:

```python
class BuildCommandStep(BaseModel):
    """One workflow step that builds or tests, with the job that runs it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    job_name_template: str
    text: str


def extract_build_commands(yaml_text: str) -> tuple[BuildCommandStep, ...]:
    """Every `run:` step that mentions a build or test, in document order."""

    try:
        document = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return ()
    jobs = document.get("jobs") if isinstance(document, dict) else None
    if not isinstance(jobs, dict):
        return ()
    found: list[BuildCommandStep] = []
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        template = job.get("name") if isinstance(job.get("name"), str) else str(job_id)
        for step in job.get("steps") or []:
            if not isinstance(step, dict) or not isinstance(step.get("run"), str):
                continue
            if _mentions_build_or_test(step["run"]):
                found.append(
                    BuildCommandStep(job_id=str(job_id), job_name_template=template, text=step["run"].strip())
                )
    return tuple(found)
```

In `scripts/d3_harvest_target.py`: add `SOURCES_DIR = "sources"`; import `from sag.metrics.build_scope import gradle_declared_projects, maven_declared_modules, parse_ci_command` and `from sag.metrics.ci_vetting import extract_build_commands`; add:

```python
_EXPRESSION_RE = re.compile(r"\$\{\{[^}]*\}\}")


def _job_matches_check(template: str, check_name: str) -> bool:
    """A job's `name:` template names a check when its literal tokens all appear."""

    literal = name_signature(_EXPRESSION_RE.sub(" ", template))
    return bool(literal) and literal <= name_signature(check_name)


def apply_declared_scope(
    snapshot_dir: Path, cells: list[CellTarget], workflow_files: tuple[str, ...]
) -> tuple[str, ...]:
    """Attach the CI command to each cell and, absent a log, the declared reactor."""

    notes: list[str] = []
    steps = []
    for file_name in workflow_files:
        text = (snapshot_dir / WORKFLOWS_DIR / file_name).read_text(encoding="utf-8", errors="replace")
        steps.extend(extract_build_commands(text))
    if not steps:
        return ()
    sources = snapshot_dir / SOURCES_DIR
    settings = next(
        (path for name in ("settings.gradle", "settings.gradle.kts") if (path := sources / name).exists()),
        None,
    )
    root_pom = sources / "pom.xml"

    def read_pom(relative_dir: str) -> str | None:
        path = sources / relative_dir / "pom.xml"
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else None

    for index, cell in enumerate(cells):
        matching = [step for step in steps if _job_matches_check(step.job_name_template, cell.cell_id)]
        if len({step.text for step in matching}) != 1:
            continue
        command = parse_ci_command(matching[0].text)
        payload = cell.model_dump(mode="json")
        payload["command"] = command.text or matching[0].text
        cells[index] = CellTarget(**payload)
        if cell.modules_basis == "log":
            continue
        declared: tuple[str, ...] = ()
        if command.tool == "gradle" and settings is not None:
            declared = gradle_declared_projects(settings.read_text(encoding="utf-8", errors="replace"))
        elif command.tool == "maven" and root_pom.exists():
            declared = maven_declared_modules(
                root_pom.read_text(encoding="utf-8", errors="replace"),
                read_pom,
                active_profiles=command.profiles,
                projects=command.projects,
            )
        if declared:
            cells[index] = _with_modules(cells[index], declared, "declared")
            notes.append(
                f"cell {cells[index].cell_id}: modules are the declared reactor under "
                f"`{payload['command']}` ({len(declared)} modules)"
            )
    return tuple(notes)
```

Call it in `assemble_target_record` right after `notes.extend(apply_job_logs(...))`:

```python
    notes.extend(apply_declared_scope(snapshot_dir, cells, workflow_files))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest -q tests/test_build_scope.py tests/test_ci_vetting.py tests/test_d3_harvester.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/sag/metrics/build_scope.py src/sag/metrics/ci_vetting.py scripts/d3_harvest_target.py tests/test_build_scope.py tests/test_ci_vetting.py tests/test_d3_harvester.py
git commit -m "feat: the reactor a CI command selects is a cell's declared build universe"
```

---

### Task A6: The courier fetches logs and sources (network)

**Files:**
- Modify: `scripts/d3_harvest_target.py` (`fetch_snapshot`, `build_parser`, `main`)
- Test: `tests/test_d3_harvester.py` (parser only — the network path is never exercised by tests)

**Interfaces:**
- Consumes: `LOGS_GLOB`, `SOURCES_DIR` (A4/A5); `download`, `fetch`, `HarvestError`.
- Produces: `fetch_logs(repo: str, snapshot_dir: Path) -> tuple[str, ...]`; `fetch_sources(repo: str, sha: str, snapshot_dir: Path) -> tuple[str, ...]`; CLI flags `--fetch-logs`, `--fetch-sources` usable with `--from-dir` on an existing snapshot.

- [ ] **Step 1: Write the failing parser test**

```python
def test_parser_accepts_refetch_flags_on_an_existing_snapshot():
    from scripts.d3_harvest_target import build_parser

    args = build_parser().parse_args(
        ["--from-dir", "x", "--repo", "apache/kafka", "--sha", "a" * 40, "--fetch-logs", "--fetch-sources"]
    )

    assert args.fetch_logs is True
    assert args.fetch_sources is True
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest -q tests/test_d3_harvester.py -k refetch_flags`
Expected: FAIL with `error: unrecognized arguments: --fetch-logs --fetch-sources`

- [ ] **Step 3: Implement the fetchers**

```python
_MAX_POM_FETCHES = 400


def fetch_logs(repo: str, snapshot_dir: Path) -> tuple[str, ...]:
    """Download each signal run's log archive; an expired log is a note, not a failure."""

    index_path = snapshot_dir / "runs-index.json"
    if not index_path.exists():
        return ("no runs-index.json; nothing to fetch logs for",)
    notes: list[str] = []
    for run in _read_json(index_path) or []:
        run_id = run.get("id")
        if not isinstance(run_id, int):
            continue
        destination = snapshot_dir / f"run-{run_id}-logs.zip"
        if destination.exists():
            continue
        try:
            download(f"repos/{repo}/actions/runs/{run_id}/logs", destination)
        except HarvestError as exc:
            # GitHub keeps logs for 90 days; past that the API answers 410.
            (snapshot_dir / f"run-{run_id}-logs.missing").write_text(str(exc), encoding="utf-8")
            notes.append(f"run {run_id}: logs unavailable ({exc})")
    return tuple(notes)


def _fetch_source(repo: str, sha: str, path: str, destination: Path) -> bool:
    try:
        body = fetch(f"repos/{repo}/contents/{path}?ref={sha}")
    except HarvestError:
        return False
    content = body.get("content") if isinstance(body, dict) else None
    if not isinstance(content, str):
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(base64.b64decode(content))
    return True


def fetch_sources(repo: str, sha: str, snapshot_dir: Path) -> tuple[str, ...]:
    """Fetch the build definition files the declared rung reads, at the pinned sha."""

    sources = snapshot_dir / SOURCES_DIR
    notes: list[str] = []
    for name in ("settings.gradle", "settings.gradle.kts"):
        if _fetch_source(repo, sha, name, sources / name):
            notes.append(f"fetched {name}")
    if not _fetch_source(repo, sha, "pom.xml", sources / "pom.xml"):
        return tuple(notes) or ("no build definition file found at the root",)
    notes.append("fetched pom.xml")
    # Walk the reactor: every child pom named by a <module>, bounded.
    pending = ["."]
    fetched = 0
    while pending and fetched < _MAX_POM_FETCHES:
        directory = pending.pop(0)
        pom_path = sources / ("pom.xml" if directory == "." else f"{directory}/pom.xml")
        if not pom_path.exists():
            continue
        try:
            root = ET.fromstring(pom_path.read_text(encoding="utf-8", errors="replace"))
        except ET.ParseError:
            continue
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1] != "module" or not (node.text or "").strip():
                continue
            child = node.text.strip()
            child_dir = child if directory == "." else f"{directory}/{child}"
            if (sources / child_dir / "pom.xml").exists():
                continue
            if _fetch_source(repo, sha, f"{child_dir}/pom.xml", sources / child_dir / "pom.xml"):
                fetched += 1
                pending.append(child_dir)
    if fetched >= _MAX_POM_FETCHES:
        notes.append(f"reactor walk stopped at {_MAX_POM_FETCHES} poms")
    return tuple(notes)
```

(`import xml.etree.ElementTree as ET` joins the imports.) In `build_parser` add:

```python
    parser.add_argument("--fetch-logs", action="store_true", help="download job logs into the snapshot (needs --repo)")
    parser.add_argument("--fetch-sources", action="store_true", help="download settings.gradle/pom.xml at --sha into the snapshot")
```

In `main`, after `snapshot_dir` is known and before `harvest_from_dir`:

```python
        if args.fetch_logs or args.fetch_sources:
            if not args.repo:
                raise HarvestError("--fetch-logs/--fetch-sources need --repo")
            if args.fetch_logs:
                for note in fetch_logs(args.repo, snapshot_dir):
                    print(f"D3 HARVEST: {note}", file=sys.stderr)
            if args.fetch_sources:
                if not args.sha:
                    raise HarvestError("--fetch-sources needs --sha")
                for note in fetch_sources(args.repo, args.sha, snapshot_dir):
                    print(f"D3 HARVEST: {note}", file=sys.stderr)
```

Also make `fetch_snapshot` call both at its end (before `return out_dir`): `fetch_logs(repo, out_dir)` and `fetch_sources(repo, sha, out_dir)`.

- [ ] **Step 4: Run the parser test**

Run: `.venv/bin/python -m pytest -q tests/test_d3_harvester.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/d3_harvest_target.py tests/test_d3_harvester.py
git commit -m "feat: the courier brings home the job logs and the build definition a snapshot needs"
```

---

### Task A7: Lifecycle parity and the attainment build axis

**Files:**
- Create: `src/sag/metrics/parity.py`
- Modify: `src/sag/metrics/attainment.py:81-145` (`CertificateView`, `view_from_certificate`), `:148-190` (`AttainmentResult`), `:201-334` (`evaluate_attainment`)
- Test: `tests/test_parity.py`, `tests/test_attainment.py`

**Interfaces:**
- Consumes: `CiCommand`, `parse_ci_command`, `maven_default_goal` (A5); `module_key` (A1); `ModulesBasis` (A2).
- Produces: `LifecycleParity(status: Literal["equivalent","not_equivalent","unknown"], form: Literal["maven_phases","gradle_tasks","none"], ci_command: str, sag_commands: tuple[str,...], ci_reach: str | None, sag_reach: str | None, missing: tuple[str,...], extra: tuple[str,...])`; `command_parity(ci: CiCommand, sag: Sequence[CiCommand], *, ci_default_goal: str | None = None) -> LifecycleParity`; `CertificateView.commands: tuple[str, ...] = ()`; `AttainmentResult.build_form`, `.modules_basis`, `.unmatched_observed_module_ids`, `.lifecycle_parity`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_parity.py
"""Lifecycle parity: the CI command is the build's specification.

Its own axis — never folded into alpha — stating whether SAG's dispatches
reached what the CI command runs, and naming what they did not.
"""

from sag.metrics.build_scope import parse_ci_command
from sag.metrics.parity import command_parity


def _maven(text):
    return parse_ci_command(text)


def test_reaching_the_ci_phase_is_equivalent():
    parity = command_parity(_maven("mvn -V test --file pom.xml"), [_maven("mvn --fail-at-end compile"), _maven("mvn test")])

    assert parity.status == "equivalent"
    assert parity.form == "maven_phases"
    assert parity.ci_reach == "test"
    assert parity.sag_reach == "test"
    assert parity.missing == ()


def test_stopping_short_names_the_missing_phases_and_plugin_goals():
    parity = command_parity(
        _maven("mvn -B verify japicmp:cmp checkstyle:check"),
        [_maven("mvn compile"), _maven("mvn test")],
    )

    assert parity.status == "not_equivalent"
    assert parity.missing == ("package", "integration-test", "verify", "checkstyle:check", "japicmp:cmp")


def test_a_bare_mvn_resolves_through_the_pom_default_goal_or_stays_unknown():
    assert command_parity(_maven("mvn"), [_maven("mvn test")]).status == "unknown"
    resolved = command_parity(_maven("mvn"), [_maven("mvn test")], ci_default_goal="clean verify")
    assert resolved.status == "not_equivalent"
    assert resolved.ci_reach == "verify"


def test_gradle_tasks_compare_as_sets_after_exclusions():
    ci = parse_ci_command("./gradlew build -x javadoc")
    assert command_parity(ci, [parse_ci_command("./gradlew build")]).status == "equivalent"
    short = command_parity(ci, [parse_ci_command("./gradlew test")])
    assert short.status == "not_equivalent"
    assert short.missing == ("build",)


def test_different_tools_or_no_sag_commands_are_unknown():
    assert command_parity(_maven("mvn test"), [parse_ci_command("./gradlew test")]).status == "unknown"
    assert command_parity(_maven("mvn test"), []).status == "unknown"
```

In `tests/test_attainment.py` (append; `_cell`, `_record`, `_view` exist):

```python
def test_build_axis_grades_receipt_modules_against_the_cells_universe():
    cell = _cell(modules=("clients", "core", "storage/storage-api"), modules_basis="log")
    view = _view(modules=(":clients", "storage/api"))

    result = evaluate_attainment(view, _record(cell))

    assert result.build_form == "modules"
    assert result.modules_basis == "log"
    assert result.alpha_build == Pair(numerator=1, denominator=3)
    assert result.missing_module_ids == ("core", "storage/storage-api")
    assert result.unmatched_observed_module_ids == ("storage/api",)
    assert result.built is False


def test_a_cell_without_modules_falls_back_to_the_conclusion_form():
    result = evaluate_attainment(_view(), _record(_cell()))

    assert result.build_form == "conclusion"
    assert result.modules_basis is None
    assert result.unmatched_observed_module_ids == ()


def test_lifecycle_parity_rides_the_result_and_never_the_score():
    cell = _cell(command="mvn -V test --file pom.xml")
    view = _view(commands=("mvn --fail-at-end compile", "mvn test"))

    result = evaluate_attainment(view, _record(cell))

    assert result.lifecycle_parity is not None
    assert result.lifecycle_parity.status == "equivalent"
    assert result.verdict == evaluate_attainment(_view(), _record(_cell())).verdict


def test_no_command_on_either_side_means_no_parity_claim():
    assert evaluate_attainment(_view(), _record(_cell())).lifecycle_parity is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_parity.py tests/test_attainment.py`
Expected: FAIL — `ModuleNotFoundError: sag.metrics.parity`; `ValidationError: Extra inputs are not permitted` for `commands`.

- [ ] **Step 3: Implement parity and the attainment changes**

```python
# src/sag/metrics/parity.py
"""Lifecycle parity: did SAG run what the CI command runs?

The CI command is the build's specification — the profiles, phases and
plugin goals a project's own CI considers a working checkout.  This axis
states equivalence and names the gap; it never enters the attainment score.
"""

from __future__ import annotations

from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict

from sag.metrics.build_scope import CiCommand, parse_ci_command

MAVEN_PHASES: tuple[str, ...] = (
    "validate", "initialize", "generate-sources", "process-sources", "generate-resources",
    "process-resources", "compile", "process-classes", "generate-test-sources",
    "process-test-sources", "generate-test-resources", "process-test-resources",
    "test-compile", "process-test-classes", "test", "prepare-package", "package",
    "pre-integration-test", "integration-test", "post-integration-test", "verify",
    "install", "deploy",
)
# The phases a reader recognises as milestones; the gap is named in these.
_MILESTONES = ("compile", "test", "package", "integration-test", "verify", "install", "deploy")
_PHASE_INDEX = {phase: index for index, phase in enumerate(MAVEN_PHASES)}


class LifecycleParity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["equivalent", "not_equivalent", "unknown"]
    form: Literal["maven_phases", "gradle_tasks", "none"]
    ci_command: str
    sag_commands: tuple[str, ...] = ()
    ci_reach: str | None = None
    sag_reach: str | None = None
    missing: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()


def _reach(goals: Sequence[str]) -> str | None:
    phases = [goal for goal in goals if goal in _PHASE_INDEX]
    return max(phases, key=_PHASE_INDEX.__getitem__) if phases else None


def command_parity(
    ci: CiCommand,
    sag: Sequence[CiCommand],
    *,
    ci_default_goal: str | None = None,
) -> LifecycleParity:
    """Compare one CI command with everything SAG dispatched."""

    sag_texts = tuple(command.text for command in sag)
    unknown = LifecycleParity(status="unknown", form="none", ci_command=ci.text, sag_commands=sag_texts)
    if ci.tool == "unknown" or not sag or any(command.tool != ci.tool for command in sag):
        return unknown

    if ci.tool == "maven":
        ci_goals = tuple(ci.goals)
        if not ci_goals and ci_default_goal:
            ci_goals = tuple(ci_default_goal.split())
        if not ci_goals:
            return unknown
        ci_reach = _reach(ci_goals)
        sag_goals = [goal for command in sag for goal in command.goals]
        sag_reach = _reach(sag_goals)
        missing: list[str] = []
        if ci_reach is not None:
            reached = _PHASE_INDEX[sag_reach] if sag_reach is not None else -1
            missing.extend(
                phase for phase in _MILESTONES
                if reached < _PHASE_INDEX[phase] <= _PHASE_INDEX[ci_reach]
            )
        ci_plugins = sorted(goal for goal in ci_goals if ":" in goal)
        sag_plugins = {goal for goal in sag_goals if ":" in goal}
        missing.extend(goal for goal in ci_plugins if goal not in sag_plugins)
        extra = tuple(sorted(sag_plugins - set(ci_plugins)))
        return LifecycleParity(
            status="equivalent" if not missing else "not_equivalent",
            form="maven_phases",
            ci_command=ci.text,
            sag_commands=sag_texts,
            ci_reach=ci_reach,
            sag_reach=sag_reach,
            missing=tuple(missing),
            extra=extra,
        )

    ci_tasks = set(ci.goals) - set(ci.excluded_tasks)
    if not ci_tasks:
        return unknown
    sag_tasks = {task for command in sag for task in command.goals}
    missing_tasks = tuple(sorted(ci_tasks - sag_tasks))
    return LifecycleParity(
        status="equivalent" if not missing_tasks else "not_equivalent",
        form="gradle_tasks",
        ci_command=ci.text,
        sag_commands=sag_texts,
        missing=missing_tasks,
        extra=tuple(sorted(sag_tasks - ci_tasks)),
    )


def parity_from_texts(ci_text: str, sag_texts: Sequence[str], *, ci_default_goal: str | None = None) -> LifecycleParity:
    """Convenience for callers holding raw command strings."""

    return command_parity(parse_ci_command(ci_text), [parse_ci_command(t) for t in sag_texts], ci_default_goal=ci_default_goal)


__all__ = ["MAVEN_PHASES", "LifecycleParity", "command_parity", "parity_from_texts"]
```

In `src/sag/metrics/attainment.py`:

```python
from sag.metrics.module_keys import module_key
from sag.metrics.parity import LifecycleParity, parity_from_texts
from sag.metrics.target_record import CellGrade, CellTarget, ModulesBasis, TargetRecord, _canonical_ids

BuildForm = Literal["modules", "conclusion"]
```

`CertificateView` gains `commands: tuple[str, ...] = ()` (no validation beyond stripping blanks in the validator: `object.__setattr__(self, "commands", tuple(c.strip() for c in self.commands if c.strip()))`) and `modules` is canonicalized in `_validate_identity_sets` by `modules = _canonical_ids(tuple(module_key(m) for m in self.modules), label="modules")`.

`AttainmentResult` gains:

```python
    build_form: BuildForm = "conclusion"
    modules_basis: ModulesBasis | None = None
    unmatched_observed_module_ids: tuple[str, ...] = ()
    lifecycle_parity: LifecycleParity | None = None
```

In `evaluate_attainment`, replace the `observed_modules = set(view.modules)` block's head with:

```python
    observed_modules = set(view.modules)
    target_modules = set(cell.modules)
    missing_modules: tuple[str, ...] = ()
    unmatched_observed: tuple[str, ...] = ()
    build_form: BuildForm = "modules" if target_modules else "conclusion"
    if target_modules:
        missing_modules = tuple(sorted(target_modules - observed_modules))
        unmatched_observed = tuple(sorted(observed_modules - target_modules))
```

(the rest of that branch is unchanged), and before constructing the result:

```python
    lifecycle_parity = (
        parity_from_texts(cell.command, view.commands) if cell.command and view.commands else None
    )
```

and pass `build_form=build_form, modules_basis=cell.modules_basis, unmatched_observed_module_ids=unmatched_observed, lifecycle_parity=lifecycle_parity` into `AttainmentResult(...)`. Update the module docstring's third rule to add: *the build denominator is the cell's own module universe when it states one; when it states none, the build axis can only say whether SAG's own build succeeded, and the result says so (`build_form="conclusion"`).*

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest -q tests/test_parity.py tests/test_attainment.py tests/test_certificate_v2_surface.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/sag/metrics/parity.py src/sag/metrics/attainment.py tests/test_parity.py tests/test_attainment.py
git commit -m "feat: the build axis grades receipts against the CI universe, and parity states what the CI command runs"
```

---

### Task A8: Kafka acceptance on archived evidence

**Files:**
- Create: `tests/test_kafka_build_scope_acceptance.py`

**Interfaces:**
- Consumes: `read_pool` (A3), `module_key` (A1), `evaluate_attainment` / `CertificateView` (A7), `CellTarget` / `TargetRecord` (A2).

- [ ] **Step 1: Write the acceptance test** (skips when the archives are absent, exactly like `tests/test_gradle_evidence_reparse.py`)

```python
# tests/test_kafka_build_scope_acceptance.py
"""The build axis measured on kafka's real evidence: CI pool vs SAG's tree.

CI's JUnit pool (run 27721225836 at 26b251a4) encodes 34 test-bearing modules
by Gradle project path; SAG's 2026-08-26 evidence tree encodes 19 by
directory.  18 agree under the canonical grammar; `storage/api` is CI's
`storage/storage-api` (a `projectDir` remap the declared rung resolves once
settings.gradle is fetched) and is disclosed as unmatched, never counted
either way.  Every number here was measured before it was pinned
(docs/superpowers/specs/2026-09-07-ci-defined-build-scope-design.md).
"""

import tarfile
from pathlib import Path

import pytest

from sag.metrics.attainment import CertificateView, Pair, evaluate_attainment
from sag.metrics.module_keys import module_keys
from sag.metrics.target_record import CellTarget, TargetRecord
from scripts.d3_harvest_target import read_pool

POOL = Path("logs/ci-ground-truth-probe-20260827/kafka-run-27721225836/junit-xml-17-noflaky-nonew.zip")
TREE = Path("logs/gradle-evidence-20260830/kafka-d2r6-container/test-results.tar")

pytestmark = pytest.mark.skipif(
    not (POOL.exists() and TREE.exists()),
    reason="kafka CI pool and SAG evidence tar are archived under logs/ only",
)


def _sag_modules() -> tuple[str, ...]:
    prefixes = set()
    with tarfile.open(TREE) as tar:
        for member in tar.getmembers():
            if member.isfile() and "/test-results/" in member.name and member.name.endswith(".xml") and "/binary/" not in member.name:
                prefixes.add(member.name.split("/build/test-results/")[0])
    return module_keys(prefixes)


def test_kafka_build_axis_is_eighteen_of_thirty_four():
    reading = read_pool(POOL)
    assert len(reading.modules) == 34
    assert reading.modules_basis if hasattr(reading, "modules_basis") else True

    cell = CellTarget(
        cell_id="junit-xml-17-noflaky-nonew",
        build="ok",
        executed_count=len(reading.deconvolved.executed_ids),
        red_count=len(reading.deconvolved.final_red_ids),
        flaky_count=len(reading.deconvolved.flaky_ids),
        skipped=len(reading.deconvolved.final_skipped_ids),
        modules=reading.modules,
        modules_basis="test_bearing",
        grade="A",
    )
    target = TargetRecord(
        repo="apache/kafka",
        sha="26b251a451ce941d3d7a55e6487bcb7f16b5ad48",
        harvested_at="2026-08-27T00:00:00Z",
        cells=(cell,),
        matched_cell=cell.cell_id,
    )
    ours = _sag_modules()
    assert len(ours) == 19
    view = CertificateView(
        authority_ok=True,
        counts_receipt_bound=True,
        build_ok=True,
        executed_count=27_219,
        red_count=8,
        modules=ours,
    )

    result = evaluate_attainment(view, target)

    assert result.build_form == "modules"
    assert result.modules_basis == "test_bearing"
    assert result.alpha_build == Pair(numerator=18, denominator=34)
    assert result.modules_matched == 18
    assert len(result.missing_module_ids) == 16
    assert result.unmatched_observed_module_ids == ("storage/api",)
    assert result.built is False
    # The verdict is still decided by the reds CI does not have (E2b, 2026-09-03).
    assert result.verdict == "not_met"
```

Remove the line `assert reading.modules_basis if hasattr(...)` before running — `PoolReading` has no basis field; the cell states it. (It is listed here so the executor does not add one.)

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest -q tests/test_kafka_build_scope_acceptance.py -rs`
Expected: PASS (or SKIPPED with the archive reason on a checkout without `logs/`).

- [ ] **Step 3: Commit**

```bash
git add tests/test_kafka_build_scope_acceptance.py
git commit -m "test: kafka's build axis measures eighteen of thirty-four, and names the one it cannot match"
```

---

### Task A9: Re-assemble the frozen d3 records to v2 (offline) and refetch what the cliff allows

**Files:**
- Create: `scripts/d3_reassemble_v2.py`
- Create: `logs/d3-freeze-20260830/<seat>/target_record.v2.json` (23 files), `logs/d3-freeze-20260830/d3-pin-manifest-v2-addendum.json`
- Create: `docs/superpowers/reports/d3-build-scope-20260907.md`

**Interfaces:**
- Consumes: `harvest_from_dir`, `fetch_logs`, `fetch_sources` (A5/A6).

- [ ] **Step 1: Write the driver**

```python
# scripts/d3_reassemble_v2.py
"""Re-assemble every frozen d3 seat to a v2 target record, offline first.

Pins do not move: the v1 record and its digest stay as frozen.  This writes
`target_record.v2.json` beside it and an addendum manifest with the v2
digest and the matched cell's modules basis.  With --refetch it first asks
the courier for job logs (90-day cliff) and build definition files.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from scripts.d3_harvest_target import HarvestError, fetch_logs, fetch_sources, harvest_from_dir

FREEZE = Path("logs/d3-freeze-20260830")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refetch", action="store_true")
    parser.add_argument("--jdk", type=int, default=17)
    args = parser.parse_args(argv)
    manifest = json.loads((FREEZE / "d3-pin-manifest.json").read_text())
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    addendum = {"schema_version": 1, "reassembled_at": stamp, "record_schema_version": 2, "seats": {}}
    for seat, pin in sorted(manifest["seats"].items()):
        snapshot = FREEZE / seat
        notes: list[str] = []
        if args.refetch:
            notes.extend(fetch_logs(pin["repo"], snapshot))
            notes.extend(fetch_sources(pin["repo"], pin["sha"], snapshot))
        try:
            record, digest, _ = harvest_from_dir(
                snapshot, repo=pin["repo"], sha=pin["sha"], harvested_at=stamp,
                jdk_major=args.jdk, out_path=snapshot / "target_record.v2.json",
            )
        except HarvestError as exc:
            addendum["seats"][seat] = {"error": str(exc), "courier_notes": notes}
            print(f"{seat}: ERROR {exc}", file=sys.stderr)
            continue
        matched = next((c for c in record.cells if c.cell_id == record.matched_cell), None)
        addendum["seats"][seat] = {
            "record_v2_sha256": digest,
            "matched_cell": record.matched_cell,
            "matched_modules_basis": matched.modules_basis if matched else None,
            "matched_modules_count": len(matched.modules) if matched else 0,
            "matched_command": matched.command if matched else None,
            "cells_with_modules": sum(1 for c in record.cells if c.modules),
            "courier_notes": notes,
        }
        print(f"{seat}: v2 sha256={digest[:12]} basis={addendum['seats'][seat]['matched_modules_basis']} modules={addendum['seats'][seat]['matched_modules_count']}")
    (FREEZE / "d3-pin-manifest-v2-addendum.json").write_text(json.dumps(addendum, indent=1, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run offline first, then with `--refetch`**

Run: `.venv/bin/python scripts/d3_reassemble_v2.py` then `.venv/bin/python scripts/d3_reassemble_v2.py --refetch`
Expected: 23 lines, each naming the seat, v2 digest, basis and count; errors only for seats whose v1 harvest also errored (none expected). Refetch adds logs for runs younger than 90 days and `sources/` for every seat.

- [ ] **Step 3: Write the report** `docs/superpowers/reports/d3-build-scope-20260907.md` with: a table seat → matched cell → basis → module count → command → whether logs were available; the kafka row must read basis `log` or `declared` with `storage/api` resolved (or say why not); a paragraph on how many of the 23 now carry a build universe and at which grade; and the v1→v2 digest pairs. Add `SHA256SUMS` entries for every new file under `logs/d3-freeze-20260830/`.

- [ ] **Step 4: Commit** (logs are gitignored; the report and script are not)

```bash
git add -f docs/superpowers/reports/d3-build-scope-20260907.md
git add scripts/d3_reassemble_v2.py
git commit -m "docs: the frozen d3 battery restated with each cell's build universe"
```

---

# Part B — local surfaces state what SAG did; CI grades it

### Task B1: Scan-scope conflicts stop capping the verdict

**Files:**
- Modify: `src/sag/verdict.py:72-78`
- Test: `tests/test_verdict_kernel.py` (create if absent; otherwise append to the existing verdict kernel test file — `grep -l "run_verdict" tests/*.py` names it)

- [ ] **Step 1: Write the failing test**

```python
from sag.verdict import BUILD_SCOPE_CONFLICTS, run_verdict


def test_scan_scope_conflicts_are_recorded_facts_not_caps():
    assert BUILD_SCOPE_CONFLICTS == frozenset(
        {
            "build_modules_incomplete",
            "reactor_scope_narrowed",
            "build_coverage_scope_unverified",
            "module_scan_contradicts_physical_build",
        }
    )
    for conflict in BUILD_SCOPE_CONFLICTS:
        assert run_verdict("success", "success", [conflict]) == "success"
    # Evidence-closure failures still cap: deleting the ledger must not help.
    assert run_verdict("success", "success", ["build_receipts_unreadable"]) == "partial"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest -q tests/test_verdict_kernel.py`
Expected: FAIL with `ImportError: cannot import name 'BUILD_SCOPE_CONFLICTS'`

- [ ] **Step 3: Implement**

In `src/sag/verdict.py`, before `ADJUDICATED_CONFLICTS`:

```python
# Scope measured against SAG's own module scan.  The yardstick for a build's
# scope is the project's CI on the same commit (spec 2026-09-07,
# ci-defined-build-scope): what the scan finds on disk is a disclosure a
# reader can see, never a denominator, so a shortfall against it is recorded
# and does not cap the setup verdict.  The evidence-closure failures around it
# (`build_receipts_unreadable`, `build_receipt_not_terminal`, ...) are not in
# this set and still cap.
BUILD_SCOPE_CONFLICTS = frozenset(
    {
        "build_modules_incomplete",
        "reactor_scope_narrowed",
        "build_coverage_scope_unverified",
        "module_scan_contradicts_physical_build",
    }
)
```

and add `*BUILD_SCOPE_CONFLICTS,` inside `ADJUDICATED_CONFLICTS`.

- [ ] **Step 4: Run the kernel tests**

Run: `.venv/bin/python -m pytest -q tests/test_verdict_kernel.py tests/test_verdict_rates.py tests/test_verdict_finalizer.py`
Expected: the new test passes; finalizer/rates tests that assert a scan-scope cap now fail — that is Task B2/B3's work, leave them.

- [ ] **Step 5: Commit**

```bash
git add src/sag/verdict.py tests/test_verdict_kernel.py
git commit -m "feat: a shortfall against SAG's own module scan is a fact, not a cap"
```

---

### Task B2: The verdict word and the headline lines speak execution, not scan scope

**Files:**
- Modify: `src/sag/verdict_rates.py:238-246` (`derived_verdict_word`), `:248-262` (delete `_BUILD_SCOPE_CONFLICTS`), `source_scope_coverage` (delete), `render_snapshot_metric_lines` (rewrite Build and Tests lines)
- Modify: `src/sag/agent/verdict_finalizer.py:1748`, `:1886` (call sites)
- Test: `tests/test_verdict_rates.py`

- [ ] **Step 1: Rewrite the tests**

Delete `test_source_scope_coverage_requires_independent_full_build_authority` and `test_source_scope_coverage_refuses_incomplete_authority`. Replace `test_derived_verdict_word` and the two metric-line tests with:

```python
@pytest.mark.parametrize(
    "build_judgment, test_band_args, word",
    [
        ("failed", (100, 100), "failed"),
        ("success", (100, 100), "success"),
        ("success", (99, 100), "partial"),
        ("success", (0, None), "partial"),  # unavailable is never success
        ("partial", (100, 100), "partial"),  # the Python ladder's own completeness
        (None, (100, 100), "partial"),
        ("unknown", (100, 100), "partial"),
    ],
)
def test_derived_verdict_word(build_judgment, test_band_args, word):
    test = GrainRate(numerator=test_band_args[0], denominator=test_band_args[1])
    assert derived_verdict_word(build_judgment, test) == word


def test_snapshot_metric_lines_state_what_ran_and_grade_nothing_against_the_scan():
    snapshot = _sealed_commons_cli_snapshot()

    assert render_snapshot_metric_lines(snapshot) == [
        "Build: SUCCESS · modules built 1 of 1 declared on disk (diagnostic) · class files 56 (diagnostic) · production Java sources 36 (diagnostic)",
        (
            "Tests: EXECUTED · outcomes accounted 987/987 · non-skipped passed 926/926 · "
            "skipped 61 · failed 0 · errors 0 · static declarations 468 (diagnostic)"
        ),
        "Coverage: unavailable — coverage pass not run",
    ]


def test_red_tests_are_counted_on_the_tests_line_and_never_named_failed():
    snapshot = deepcopy(_sealed_commons_cli_snapshot())
    snapshot["test_stats"]["unique"].update(passed=925, failed=1)

    _, test_line, _ = render_snapshot_metric_lines(snapshot)

    assert test_line.startswith("Tests: EXECUTED · outcomes accounted 987/987")
    assert "failed 1 · errors 0" in test_line
    assert "FAILED" not in test_line


def test_a_partial_scan_is_a_disclosure_and_the_build_line_still_says_success():
    snapshot = deepcopy(_sealed_commons_cli_snapshot())
    snapshot["rates"]["build"]["modules"] = GrainRate(21, 26).payload()
    snapshot["conflicts"] = ["build_modules_incomplete"]

    build_line, _, _ = render_snapshot_metric_lines(snapshot)

    assert build_line.startswith("Build: SUCCESS · modules built 21 of 26 declared on disk (diagnostic)")
    assert "%" not in build_line


@pytest.mark.parametrize(
    "judgment, label",
    [("success", "EXECUTED"), ("partial", "INTERRUPTED"), ("failed", "FAILED TO RUN"), ("unknown", "UNAVAILABLE")],
)
def test_tests_line_names_the_execution_state(judgment, label):
    snapshot = deepcopy(_sealed_commons_cli_snapshot())
    snapshot["test_stats"]["judgment"] = judgment

    _, test_line, _ = render_snapshot_metric_lines(snapshot)

    assert test_line.startswith(f"Tests: {label} ·")
```

Also change the assertion at the old line ~846 (`derived_verdict_word(grains["modules"], GrainRate(0, 20497)) == "partial"`) to `derived_verdict_word("success", GrainRate(0, 20497)) == "partial"`.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_verdict_rates.py`
Expected: FAIL (signature and line-format mismatches).

- [ ] **Step 3: Implement**

In `src/sag/verdict_rates.py`:

```python
def derived_verdict_word(build_judgment: str | None, test_cases: GrainRate) -> str:
    """The setup verdict: did the build run and did the tests run to completion.

    Scope against SAG's own module scan is not an input (spec 2026-09-07):
    the yardstick for scope is the project's CI on the same commit, read by
    the attainment layer.  ``build_judgment`` is the sealed physical build
    judgment; ``partial`` there is the Python ladder's own completeness.
    """

    if build_judgment == "failed":
        return "failed"
    if build_judgment == "success" and test_cases.band == BAND_FULLY:
        return "success"
    return "partial"
```

Delete `_BUILD_SCOPE_CONFLICTS` and `source_scope_coverage` entirely. In `render_snapshot_metric_lines` replace the Build line assembly with:

```python
    class_files = _strict_count(build.get("compiled_classes"))
    source_files = _strict_count(build.get("source_files"))
    build_state = _word(build.get("judgment")) or "unknown"
    modules_text = (
        f"modules built {module_numerator:,} of {module_denominator:,} declared on disk (diagnostic)"
        if module_numerator is not None and module_denominator is not None and module_denominator > 0
        else "modules built unavailable"
    )
    class_text = f"class files {class_files:,} (diagnostic)" if class_files is not None else "class files unavailable"
    source_text = (
        f"production Java sources {source_files:,} (diagnostic)" if source_files is not None else "production Java sources unavailable"
    )
    build_line = f"Build: {build_state.upper()} · {modules_text} · {class_text} · {source_text}"
```

and the Tests state with:

```python
    test_state = {
        "success": "EXECUTED",
        "partial": "INTERRUPTED",
        "failed": "FAILED TO RUN",
    }.get(_word(tests.get("judgment")), "UNAVAILABLE")
```

(delete the `failed + errors > 0 → "FAILED"` override). In `src/sag/agent/verdict_finalizer.py` change both call sites: line 1748 to `derived_verdict_word(snapshot.build_evidence.judgment, test_cases)` and line 1886 to `derived_verdict_word(build.judgment, test_cases_rate)`. Confirm `BuildEvidenceSnapshot.judgment` is the field name (it is used at line 1391).

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest -q tests/test_verdict_rates.py tests/test_verdict_finalizer.py tests/test_web_session_registry.py`
Expected: `test_verdict_rates.py` passes; `test_web_session_registry.py` fails on the deleted import — that is B4.

- [ ] **Step 5: Commit**

```bash
git add src/sag/verdict_rates.py src/sag/agent/verdict_finalizer.py tests/test_verdict_rates.py
git commit -m "feat: the verdict word answers execution, and the build line states what ran"
```

---

### Task B3: The physical judgment no longer reads a JVM scan shortfall as partial

**Files:**
- Modify: `src/sag/agent/verdict_finalizer.py:479-485` (`_physical_judgment`)
- Modify: `src/sag/agent/physical_validator.py` (`validate_build_status`: `evidence["build_system"]`, beside the existing `evidence["build_command"]` write near line 3922)
- Test: `tests/test_verdict_finalizer.py`

- [ ] **Step 1: Write the failing test**

```python
from sag.agent.verdict_finalizer import _physical_judgment


def test_a_jvm_build_with_real_output_is_success_whatever_the_scan_covered():
    status = {"success": True, "build_complete": False, "evidence": {"build_system": "maven"}}
    assert _physical_judgment(status) == "success"


def test_the_python_ladder_keeps_its_own_completeness():
    status = {"success": True, "build_complete": False, "evidence": {"build_system": "python"}}
    assert _physical_judgment(status) == "partial"


def test_no_output_is_still_failed():
    assert _physical_judgment({"success": False, "evidence": {"build_system": "gradle"}}) == "failed"
```

- [ ] **Step 2: Run to verify the first fails**

Run: `.venv/bin/python -m pytest -q tests/test_verdict_finalizer.py -k "physical_judgment or jvm_build_with_real_output or python_ladder"`
Expected: the JVM test FAILS (`'partial' != 'success'`).

- [ ] **Step 3: Implement**

```python
def _physical_judgment(status: dict[str, Any]) -> str | None:
    success = status.get("success")
    if success is True:
        evidence = status.get("evidence") if isinstance(status.get("evidence"), dict) else {}
        # A JVM build's completeness against the module scan is a disclosure,
        # not a judgment (spec 2026-09-07): the CI universe grades scope.  The
        # Python ladder has no CI layer and keeps its own completeness.
        if str(evidence.get("build_system") or "").lower() == "python":
            return "success" if status.get("build_complete", True) else "partial"
        return "success"
    if success is False:
        return "failed"
    return None
```

In `physical_validator.validate_build_status`, where `evidence["build_command"]` is set (the `command_tracker` block), add one line before it: `evidence["build_system"] = build_system` (the local already in scope there).

- [ ] **Step 4: Run the finalizer, verdict, and report batteries**

Run: `.venv/bin/python -m pytest -q tests/test_verdict_finalizer.py tests/test_build_test_verdict.py tests/test_coverage_basis.py tests/test_report_honesty.py tests/test_cli_project_exit_codes.py tests/test_cli_report_verdict_mirror.py tests/test_mixed_build_module_metrics.py tests/test_jdk_reactor_conflicts.py tests/test_verdict_physical_oracle.py tests/test_module_coverage_shared.py tests/test_snapshot_surface_agreement.py tests/test_python_phase_verdict.py tests/test_python_verifier.py`
Expected: failures only where a test asserts that a scan-scope shortfall (`build_modules_incomplete`, `reactor_scope_narrowed`, `build_coverage_scope_unverified`, `module_scan_contradicts_physical_build`, or `modules N/M` with N<M) yields `partial`. For each: keep the assertion that the conflict is recorded in `conflicts`; change the verdict expectation to `success` when the physical build succeeded and tests executed fully; keep `partial` when the failure is an evidence-closure conflict or interrupted execution. Any other failure is a regression — stop and report it.

- [ ] **Step 5: Commit**

```bash
git add src/sag/agent/verdict_finalizer.py src/sag/agent/physical_validator.py tests/
git commit -m "feat: a real build is a real build; the module scan discloses and the CI universe grades"
```

---

### Task B4: The web model and the Web UI drop the source-scope rate

**Files:**
- Modify: `src/sag/web/models.py:34-46, 62-66`, `src/sag/web/session_registry.py:863-885`, `src/sag/web/demo_data.py` (every `source_scope`)
- Modify: `webui/src/api/types.ts:142, 150-157`, `webui/src/evidencePresentation.ts:78, 498-600`
- Test: `tests/test_web_session_registry.py`, `tests/test_web_build_test_models.py`, `tests/test_web_api.py`, `tests/test_web_demo_data.py`, `webui/src/evidencePresentation.test.ts`, `webui/src/api/types.test.ts`, `webui/src/pages/detail/OverviewTab.test.tsx`

- [ ] **Step 1: Write the failing presentation test** (replace the existing `presentBuild`/`sourceScope` cases in `webui/src/evidencePresentation.test.ts`)

```ts
it("states the module scan as a diagnostic sentence and never as a rate", () => {
  const rates = { build: { modules: { numerator: 21, denominator: 26, rate: 80.8, band: "most" } } }
  const build = presentBuild(
    { state: "success", tool: "sealed snapshot", time: "—", note: "", classCount: 4230 },
    rates,
  )

  expect(build.value).toBe("Success")
  expect(build.summary).toBe("Modules built 21 of 26 declared on disk (diagnostic) · Class files 4,230 (diagnostic)")
  expect(build.summary).not.toMatch(/%/)
  expect(build.summary).not.toMatch(/scope incomplete/i)
  expect(build.scan).toEqual({ built: 21, declared: 26 })
})

it("a partial build is partial because of its state, not its scan", () => {
  const build = presentBuild({ state: "partial", tool: "Maven", time: "", note: "" }, null)

  expect(build.value).toBe("Partial")
  expect(build.summary).toBe("Build stopped before completion.")
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd webui && npx vitest run src/evidencePresentation.test.ts`
Expected: FAIL (`scan` undefined; old summary text).

- [ ] **Step 3: Implement**

Python: delete `SourceScopeSummary` and the `source_scope` field from `BuildSummary`; in `session_registry._snapshot_build_payload` delete the import and both `"source_scope": source_scope,` entries; in `demo_data.py` delete every `source_scope` key. Update the four `tests/test_web_*.py` files by deleting their `source_scope`/`sourceScope` assertions.

TypeScript: in `types.ts` delete `sourceScope` from `BuildSummary` and the `SourceScopeSummary` interface; in `evidencePresentation.ts` delete `SourceScopePresentation`, `sourceScopeFromBuild`, and the `sourceScope` field, rename `buildScopeFromRates` to `moduleScanFromRates` returning `{ built, declared } | null` (no `incomplete`), and rewrite the `presentBuild` facts:

```ts
  const scan = moduleScanFromRates(rates)
  const facts = [
    scan ? `Modules built ${formatCount(scan.built)} of ${formatCount(scan.declared)} declared on disk (diagnostic)` : null,
    Number.isInteger(build.classCount) ? `Class files ${formatCount(build.classCount as number)} (diagnostic)` : null,
  ].filter((fact): fact is string => fact !== null)
  const summary = state.label === "Partial"
    ? "Build stopped before completion."
    : facts.join(" · ") || "No build counts were recorded."
```

with `scan` in `BuildPresentation` in place of `scope`/`sourceScope`. Fix the OverviewTab and types tests accordingly; then `npm run build`.

- [ ] **Step 4: Run everything**

Run: `.venv/bin/python -m pytest -q tests/test_web_session_registry.py tests/test_web_build_test_models.py tests/test_web_api.py tests/test_web_demo_data.py && cd webui && npx vitest run && npm run build`
Expected: PASS; new asset hashes under `src/sag/web/static/assets/`.

- [ ] **Step 5: Commit**

```bash
git add src/sag/web webui/src tests/test_web_*.py
git commit -m "feat: the workbench states the module scan as a diagnostic and drops the source-scope rate"
```

---

### Task B5: README and full-suite verification

**Files:**
- Modify: `README.md` (*📏 Reading a Result*, *🧪 Measuring against the project's CI*)

- [ ] **Step 1: Rewrite the README sections**

In *Reading a Result*, replace the example block and the first three bullets with:

```text
🎯 SETUP COMPLETED: ✅ SUCCESS
Build: SUCCESS · modules built 1 of 1 declared on disk (diagnostic) · class files 56 (diagnostic) · production Java sources 36 (diagnostic)
Tests: EXECUTED · outcomes accounted 987/987 · non-skipped passed 926/926 · skipped 61 · failed 0 · errors 0 · static declarations 468 (diagnostic)
Coverage: unavailable — not collected
```

- **The Build line states what SAG built; it does not grade it.** `modules built 21 of 26 declared on disk` is a disclosure a reader can see, never a percentage and never a verdict input: a build is a mix of profiles, excluded modules, and toolchain settings, and only the project's own CI command encodes that mix. Whether 21 was enough is answered by the CI comparison below.
- **`SUCCESS` on the Build line means the build ran and left real artifacts.** `FAILED` means it did not. The setup verdict word follows the same rule and adds the tests: `success` = build ran, tests ran to completion with every outcome accounted for.
- **`Tests: EXECUTED · failed 12`** reports the project's result without judging it. Whether those 12 are SAG's fault is a question only the CI comparison can answer: a test CI also fails does not count against SAG; a test CI passes and SAG fails does.

In *Measuring against the project's CI*, replace step 1's cell description with: *per CI job: build conclusion, the tests it ran (with retries folded to their final outcome), the modules it built — read from the job log (exact), else from the reactor the CI command selects on that commit (exact for the declared scope), else from the JUnit report paths (test-bearing modules only, disclosed as a lower bound) — the command the job ran, and whether the job's green status was laundered by `continue-on-error`.* Add after step 3: *Beside the score, the comparison states lifecycle parity — `CI: mvn -V test · SAG: mvn compile; mvn test · parity: equivalent` — naming any phase or plugin goal SAG did not reach. It never changes the score.* Add a closing sentence: *A project whose CI cannot be harvested gets no scope judgment at all: the local verdict is execution-only and the disk reactor count stays a disclosure.*

- [ ] **Step 2: Full verification**

Run: `.venv/bin/python -m pytest -q && .venv/bin/python -m mypy src/sag 2>&1 | tail -1 && .venv/bin/python -m isort --check-only src tests scripts && cd webui && npx vitest run && cd .. && git status --short`
Expected: pytest green (0 failed); mypy error count ≤ the pre-plan baseline (980); isort clean except the three pre-existing files (`tests/test_explicit_evidence_architecture.py`, `scripts/run_category3_stage2.py`, `scripts/run_category3_panel.py`); vitest green; a clean tree after the commit below.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: the README reads a build the way the product grades it — against the project's CI"
```

---

## Self-review

**Spec coverage.** Principle consequences 1–3 → B1–B4 (+B5 copy); 4 (three rungs, precedence log > declared > test_bearing) → A3, A4, A5 with A4's `apply_job_logs` running before A5's `apply_declared_scope`, which skips cells whose basis is already `log`; 5 (grammar, kafka remap disclosed) → A1, A7 `unmatched_observed_module_ids`, A8; 6 (parity as its own axis) → A7; 7 (no CI → no scope judgment) → B1–B3 leave the disk count as a disclosure and A7's `build_form="conclusion"` says the build axis had no external universe. Measured facts → A8 pins 18/34 and `storage/api`; A9 restates the battery.

**Placeholder scan.** Every code step carries its code. A8 Step 1 contains one line the executor is told to delete (the `hasattr` guard) — it is there to pre-empt adding a field to `PoolReading`; deleting it is the instruction, not a placeholder. B3 Step 4 and B5 Step 2 describe a triage rule for pre-existing tests rather than listing every assertion; the rule is exact (which conflict codes, which verdict expectation) and the file list is complete.

**Type consistency.** `module_key`/`module_keys` (A1) are used by A2 (validator), A3 (`pool_member_module`, `read_pool`), A4 (`modules_from_log`), A5 (`gradle_declared_projects`, `maven_declared_modules`), A7 (`CertificateView`). `ModulesBasis` (A2) is used by A7's `AttainmentResult.modules_basis`. `CiCommand`/`parse_ci_command` (A5) are consumed by A7's `command_parity`/`parity_from_texts`. `_with_modules` (A4) is reused by A5. `LOGS_GLOB`/`SOURCES_DIR` (A4/A5) are consumed by A6. `derived_verdict_word(build_judgment, test_cases)` (B2) is called from both finalizer sites with `.judgment`. `BuildPresentation.scan` (B4) replaces `scope`/`sourceScope` consistently.
