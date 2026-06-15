# Submodule Build & Test Metrics Implementation Plan (Feature A)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface per-submodule build (success/failure/skipped, classes, JARs) and test (pass/fail/skip, failing names) metrics for multi-module Maven/Gradle projects across the verifier, report, and webui — clickable Build/Test cards open SonarQube-inspired drill-down pages with failures-first per-module tables and inline-expandable full failing-test lists.

**Architecture:** A hybrid backend assembles a per-module list: `physical_validator.scan_modules` enumerates modules and scans each for artifacts + test XML (the backbone), and the already-persisted Maven reactor status in `test_summary.jsonl` enriches build pass/fail. A pure `assemble_module_metrics` reconciles them into `/workspace/.setup_agent/module_metrics.json` (snake_case), mirroring the existing `report_metrics.py` → `report_metrics.json` pattern. The web read model loads it into `ModuleSummary`/`ModuleRollup` on `ExecutionSessionDetail`; the webui renders detail pages built from existing primitives.

**Tech Stack:** Python 3 (pytest), Pydantic v2 web models, FastAPI read model, React + TypeScript + Tailwind (vitest), Docker-in-container artifacts.

**Spec:** `docs/superpowers/specs/2026-06-15-submodule-build-test-metrics-design.md`

**Branch:** `feature/submodule-build-test-metrics` (already exists, spec committed).

**Conventions (read before starting):**
- Backend tests: `.venv/bin/python -m pytest` (never bare `pytest`). Full backend gate: `.venv/bin/python -m pytest tests/ -q --ignore=tests/test_web_task_runner.py` (known docker-daemon flake).
- Frontend: `cd webui && npx vitest run`; typecheck `cd webui && npx tsc -p tsconfig.app.json --noEmit`.
- Stage only each task's files by exact path. NEVER `git add -A`. Leave `src/sag/web/static/` and unrelated dirty files unstaged. `docs/` is gitignored; the spec/plan were force-added — that is expected.
- MAINTAINER-PROTECTION: never modify `webui/src/pages/Dashboard.tsx`; never modify anything under `src/sag/web/static/`; never run `npm run build` (that is the operator's final task).
- No `Co-Authored-By` trailer in commits.
- The disk contract is snake_case; web models expose camelCase via `serialization_alias` + `validation_alias=AliasChoices(...)` and serialize with `model_dump(mode="json", by_alias=True)`.

---

## Data Contract (single source of truth — every task conforms to this)

On-disk `/workspace/.setup_agent/module_metrics.json`:

```json
{
  "version": 1,
  "generated_at": "2026-06-15 12:00:00",
  "module_summary": {
    "modules_total": 24, "modules_built": 21, "modules_failed": 1,
    "modules_skipped": 2, "modules_with_test_failures": 2,
    "build_systems": ["maven"], "single_module": false
  },
  "modules": [
    {
      "name": "connect:api", "path": "connect/api",
      "build_status": "success", "build_source": "reactor",
      "class_count": 180, "jar_count": 3, "build_warnings": null,
      "build_error_samples": [],
      "tests_total": 198, "tests_passed": 198, "tests_failed": 0,
      "tests_errors": 0, "tests_skipped": 0, "test_source": "runner_xml",
      "failing_names": [], "failing_count": 0,
      "evidence_refs": ["/workspace/connect/api/target/surefire-reports"]
    }
  ]
}
```

Field domains: `build_status` ∈ {`success`,`failure`,`skipped`,`unknown`}; `build_source` ∈ {`reactor`,`artifacts`,`partial`,`none`}; `test_source` ∈ {`runner_xml`,`partial`,`none`}. Uncomputable numbers are `null` (never `0`). `failing_names` is the full list capped at 500; `failing_count` is always the exact total.

Web model camelCase keys: `name, path, buildStatus, buildSource, classCount, jarCount, buildWarnings, buildErrorSamples, testsTotal, testsPassed, testsFailed, testsErrors, testsSkipped, testSource, failingNames, failingCount, evidenceRefs`. Rollup: `modulesTotal, modulesBuilt, modulesFailed, modulesSkipped, modulesWithTestFailures, buildSystems, singleModule`.

---

## File Structure

- Create `src/sag/tools/module_metrics.py` — pure `assemble_module_metrics(...)` + constants/helpers (mirrors `report_metrics.py`). One responsibility: turn raw scan + reactor + test inputs into the contract dict.
- Modify `src/sag/agent/physical_validator.py` — add `scan_modules(project_dir, build_system)` (enumerate + per-module artifact counts + report dirs) and `parse_module_test_reports(project_dir, report_dirs)` (per-module test counts + failing names from a dir subset).
- Modify `src/sag/tools/report_tool.py` — `_persist_module_metrics(...)` + call site in the generate flow (reads reactor records from the already-loaded test history, calls scan + per-module parse + assembler).
- Modify `src/sag/web/models.py` — `ModuleSummary`, `ModuleRollup`; extend `ExecutionSessionDetail`.
- Modify `src/sag/web/session_registry.py` — `_read_module_metrics`, `_modules_payload_from_metrics`, `_module_rollup_from_metrics`; wire into `_session_detail`.
- Modify `src/sag/web/demo_data.py` — per-module demo data on the demo session detail.
- Modify `webui/src/api/types.ts` — `ModuleSummary`, `ModuleRollup`; extend `ExecutionSessionDetail`.
- Create `webui/src/components/session/ModuleTable.tsx` — shared failures-first per-module table with inline-expand rows.
- Create `webui/src/components/session/BuildDetailPage.tsx`, `webui/src/components/session/TestDetailPage.tsx`.
- Modify `webui/src/components/session/BuildCard.tsx`, `TestCard.tsx` — clickable, `onOpenDetail` prop; remove inline expand.
- Modify `webui/src/pages/Workspace.tsx` — `detail` state, render detail pages with Back.
- Modify `src/sag/tools/report_tool.py` (markdown) — "Submodule Breakdown" section.

---

## Task 1: Pure assembler `assemble_module_metrics`

**Files:**
- Create: `src/sag/tools/module_metrics.py`
- Test: `tests/test_module_metrics.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_module_metrics.py
from sag.tools.module_metrics import assemble_module_metrics, MODULE_METRICS_VERSION


def _scan():
    return [
        {"path": "connect/api", "name": "connect:api", "class_count": 180,
         "jar_count": 3, "report_dirs": ["/w/connect/api/target/surefire-reports"]},
        {"path": "connect/runtime", "name": "connect:runtime", "class_count": 0,
         "jar_count": 0, "report_dirs": []},
        {"path": "raft", "name": "raft", "class_count": 0, "jar_count": 0, "report_dirs": []},
    ]


def test_reconciles_reactor_status_and_tests():
    metrics = assemble_module_metrics(
        modules=_scan(),
        reactor_status={"connect:api": "success", "connect:runtime": "failure",
                        "raft": "skipped"},
        tests={"connect/api": {"tests_total": 198, "tests_passed": 198, "tests_failed": 0,
                               "tests_errors": 0, "tests_skipped": 0,
                               "failing_names": [], "failing_count": 0,
                               "evidence_refs": ["/w/connect/api/target/surefire-reports"]}},
        build_systems=["maven"],
        build_error_samples={"connect/runtime": ["[ERROR] cannot find symbol"]},
        generated_at="2026-06-15 00:00:00",
    )
    assert metrics["version"] == MODULE_METRICS_VERSION
    by_path = {m["path"]: m for m in metrics["modules"]}
    assert by_path["connect/api"]["build_status"] == "success"
    assert by_path["connect/api"]["build_source"] == "reactor"
    assert by_path["connect/api"]["tests_passed"] == 198
    assert by_path["connect/runtime"]["build_status"] == "failure"
    assert by_path["connect/runtime"]["build_error_samples"] == ["[ERROR] cannot find symbol"]
    assert by_path["raft"]["build_status"] == "skipped"
    s = metrics["module_summary"]
    assert s["modules_total"] == 3 and s["modules_failed"] == 1 and s["modules_skipped"] == 1
    assert s["modules_built"] == 1 and s["single_module"] is False


def test_falls_back_to_artifacts_when_no_reactor():
    metrics = assemble_module_metrics(
        modules=[{"path": "core", "name": "core", "class_count": 50, "jar_count": 1,
                  "report_dirs": []}],
        reactor_status={},
        tests={},
        build_systems=["gradle"],
        build_error_samples={},
        generated_at="t",
    )
    m = metrics["modules"][0]
    assert m["build_status"] == "success"   # artifacts present
    assert m["build_source"] == "artifacts"
    assert metrics["module_summary"]["single_module"] is True


def test_failing_names_capped_but_count_exact():
    names = [f"com.x.T{i}.m" for i in range(600)]
    metrics = assemble_module_metrics(
        modules=[{"path": "m", "name": "m", "class_count": 1, "jar_count": 0,
                  "report_dirs": ["/w/m"]}],
        reactor_status={"m": "success"},
        tests={"m": {"tests_total": 600, "tests_passed": 0, "tests_failed": 600,
                     "tests_errors": 0, "tests_skipped": 0,
                     "failing_names": names, "failing_count": 600,
                     "evidence_refs": ["/w/m"]}},
        build_systems=["maven"], build_error_samples={}, generated_at="t",
    )
    m = metrics["modules"][0]
    assert len(m["failing_names"]) == 500
    assert m["failing_count"] == 600
    assert metrics["module_summary"]["modules_with_test_failures"] == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_module_metrics.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'sag.tools.module_metrics'`.

- [ ] **Step 3: Implement `src/sag/tools/module_metrics.py`**

```python
"""Assemble the per-submodule build/test metrics artifact (module_metrics.json).

Pure reconciliation of three inputs the report tool gathers:
- physical per-module scan (the backbone: which modules exist + artifacts + report dirs),
- the build tool's reactor status (already persisted in test_summary.jsonl for Maven),
- per-module test counts parsed from each module's report XML.

Mirrors report_metrics.py: a single pure function, missing values -> null/[].
"""

from typing import Any, Dict, List, Optional

MODULE_METRICS_PATH = "/workspace/.setup_agent/module_metrics.json"
MODULE_METRICS_VERSION = 1
_MAX_FAILING = 500
_MAX_ERROR_SAMPLES = 20

_BUILD_STATES = {"success", "failure", "skipped", "unknown"}


def _int_or_none(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str_list(value: Any, limit: int) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value[:limit]]


def _norm_status(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    low = value.strip().lower()
    return low if low in _BUILD_STATES else None


def assemble_module_metrics(
    *,
    modules: List[Dict[str, Any]],
    reactor_status: Dict[str, str],
    tests: Dict[str, Dict[str, Any]],
    build_systems: List[str],
    build_error_samples: Dict[str, List[str]],
    generated_at: str,
) -> Dict[str, Any]:
    reactor_status = reactor_status or {}
    tests = tests or {}
    build_error_samples = build_error_samples or {}
    out_modules: List[Dict[str, Any]] = []

    any_failure = any(_norm_status(v) == "failure" for v in reactor_status.values())

    for scan in modules or []:
        path = str(scan.get("path") or "")
        name = str(scan.get("name") or path or ".")
        class_count = _int_or_none(scan.get("class_count"))
        jar_count = _int_or_none(scan.get("jar_count"))

        # Build status: reactor (by name) wins; else infer from artifacts.
        reactor = _norm_status(reactor_status.get(name)) or _norm_status(reactor_status.get(path))
        if reactor is not None:
            build_status, build_source = reactor, "reactor"
            # Conflict guard: reactor says success but nothing was produced.
            if reactor == "success" and not (class_count or jar_count) and path != ".":
                build_source = "partial"
        elif class_count or jar_count:
            build_status, build_source = "success", "artifacts"
        elif any_failure:
            build_status, build_source = "skipped", "partial"
        else:
            build_status, build_source = "unknown", "none"

        t = tests.get(path) or {}
        has_tests = bool(t)
        failing_names = _str_list(t.get("failing_names"), _MAX_FAILING)
        failing_count = _int_or_none(t.get("failing_count"))
        if failing_count is None and has_tests:
            failing_count = len(t.get("failing_names") or [])

        out_modules.append({
            "name": name,
            "path": path,
            "build_status": build_status,
            "build_source": build_source,
            "class_count": class_count,
            "jar_count": jar_count,
            "build_warnings": _int_or_none(scan.get("build_warnings")),
            "build_error_samples": _str_list(build_error_samples.get(path), _MAX_ERROR_SAMPLES),
            "tests_total": _int_or_none(t.get("tests_total")),
            "tests_passed": _int_or_none(t.get("tests_passed")),
            "tests_failed": _int_or_none(t.get("tests_failed")),
            "tests_errors": _int_or_none(t.get("tests_errors")),
            "tests_skipped": _int_or_none(t.get("tests_skipped")),
            "test_source": "runner_xml" if has_tests else "none",
            "failing_names": failing_names,
            "failing_count": failing_count,
            "evidence_refs": _str_list(t.get("evidence_refs") or scan.get("report_dirs"), 25),
        })

    total = len(out_modules)
    summary = {
        "modules_total": total,
        "modules_built": sum(1 for m in out_modules if m["build_status"] == "success"),
        "modules_failed": sum(1 for m in out_modules if m["build_status"] == "failure"),
        "modules_skipped": sum(1 for m in out_modules if m["build_status"] == "skipped"),
        "modules_with_test_failures": sum(
            1 for m in out_modules if (m["failing_count"] or 0) > 0
        ),
        "build_systems": _str_list(build_systems, 5),
        "single_module": total <= 1,
    }
    return {
        "version": MODULE_METRICS_VERSION,
        "generated_at": generated_at,
        "module_summary": summary,
        "modules": out_modules,
    }
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_module_metrics.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/sag/tools/module_metrics.py tests/test_module_metrics.py
git commit -m "Add pure assemble_module_metrics for per-submodule contract"
```

---

## Task 2: `physical_validator.scan_modules` (enumerate + per-module artifacts)

**Files:**
- Modify: `src/sag/agent/physical_validator.py` (add method near `_validate_maven_fingerprints`, ~line 2563)
- Test: `tests/test_physical_validator_modules.py`

Reuse the existing find-command style (`_execute_command_with_logging`). Maven modules = dirs (depth ≥ 2) containing a `pom.xml`; Gradle subprojects = dirs containing `build.gradle[.kts]`. Per module count `*.class` and `*.jar`, and record report dirs.

- [ ] **Step 1: Write the failing test** (uses a fake orchestrator returning canned `find`/`test` output)

```python
# tests/test_physical_validator_modules.py
from sag.agent.physical_validator import PhysicalValidator


class FakeOrch:
    def __init__(self, responses):
        self.responses = responses  # dict: substring -> {"success","output","exit_code"}

    def execute_command(self, command, **kwargs):
        for needle, resp in self.responses.items():
            if needle in command:
                return {"success": True, "exit_code": 0, **resp}
        return {"success": True, "exit_code": 0, "output": ""}


def test_scan_modules_maven_counts_artifacts_and_report_dirs():
    responses = {
        "-name 'pom.xml'": {"output": "/w/p/connect/api/pom.xml\n/w/p/core/pom.xml"},
        "/connect/api/target/classes": {"output": "180"},
        "/connect/api/target' -name '*.jar": {"output": "3"},
        "/core/target/classes": {"output": "50"},
        "/core/target' -name '*.jar": {"output": "1"},
        "/connect/api/target/surefire-reports": {"output": "EXISTS"},
        "/core/target/surefire-reports": {"output": "EXISTS"},
    }
    v = PhysicalValidator(docker_orchestrator=FakeOrch(responses))
    modules = v.scan_modules("/w/p", "maven")
    by_path = {m["path"]: m for m in modules}
    assert by_path["connect/api"]["name"] == "connect:api"
    assert by_path["connect/api"]["class_count"] == 180
    assert by_path["connect/api"]["jar_count"] == 3
    assert any("surefire" in d for d in by_path["connect/api"]["report_dirs"])
    assert by_path["core"]["class_count"] == 50


def test_scan_modules_single_module_returns_root():
    v = PhysicalValidator(docker_orchestrator=FakeOrch({"-name 'pom.xml'": {"output": ""}}))
    modules = v.scan_modules("/w/solo", "maven")
    assert len(modules) == 1 and modules[0]["path"] == "."
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_physical_validator_modules.py -q`
Expected: FAIL with `AttributeError: 'PhysicalValidator' object has no attribute 'scan_modules'`.

- [ ] **Step 3: Implement `scan_modules`** (add as a method on `PhysicalValidator`)

```python
    def scan_modules(self, project_dir: str, build_system: str) -> List[Dict[str, any]]:
        """Enumerate submodules and scan each for artifacts + test report dirs.

        Backbone of the per-module metrics: physical evidence of which modules
        exist and what each produced. Returns a flat list of module records.
        Single-module projects return one record with path '.'.
        """
        if not self.docker_orchestrator:
            return []

        if build_system == "gradle":
            find_cmd = (
                f"find {project_dir} -mindepth 2 -maxdepth 3 "
                f"\\( -name 'build.gradle' -o -name 'build.gradle.kts' \\) 2>/dev/null"
            )
            classes_glob = "build/classes"
            jars_glob = "build/libs"
            report_subdirs = ["build/test-results/test", "build/test-results"]
            sep = ":"
        else:
            find_cmd = (
                f"find {project_dir} -mindepth 2 -maxdepth 3 -name 'pom.xml' -type f 2>/dev/null"
            )
            classes_glob = "target/classes"
            jars_glob = "target"
            report_subdirs = ["target/surefire-reports", "target/failsafe-reports"]
            sep = ":"

        found = self._execute_command_with_logging(find_cmd, "enumerating submodules")
        lines = [l for l in (found.get("output") or "").splitlines() if l.strip()]
        module_dirs = sorted({l.rsplit("/", 1)[0] for l in lines})

        # Single-module project: scan the root itself.
        if not module_dirs:
            module_dirs = [project_dir]

        modules: List[Dict[str, any]] = []
        for module_dir in module_dirs:
            rel = module_dir[len(project_dir):].strip("/") or "."
            name = "." if rel == "." else rel.replace("/", sep)

            class_cmd = (
                f"find {module_dir}/{classes_glob} -name '*.class' -type f 2>/dev/null | wc -l"
            )
            cc = self._execute_command_with_logging(class_cmd, f"counting classes in {rel}")
            class_count = int((cc.get("output") or "0").strip() or 0) if cc.get("success") else 0

            jar_cmd = (
                f"find {module_dir}/{jars_glob} -maxdepth 2 -name '*.jar' -type f "
                f"-not -path '*/gradle/wrapper/*' 2>/dev/null | wc -l"
            )
            jc = self._execute_command_with_logging(jar_cmd, f"counting jars in {rel}")
            jar_count = int((jc.get("output") or "0").strip() or 0) if jc.get("success") else 0

            report_dirs: List[str] = []
            for sub in report_subdirs:
                rd = f"{module_dir}/{sub}"
                chk = self._execute_command_with_logging(
                    f"test -d {rd} && echo EXISTS", f"checking reports {rel}"
                )
                if "EXISTS" in (chk.get("output") or ""):
                    report_dirs.append(rd)

            modules.append({
                "path": rel,
                "name": name,
                "class_count": class_count,
                "jar_count": jar_count,
                "report_dirs": report_dirs,
            })
        return modules
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_physical_validator_modules.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/sag/agent/physical_validator.py tests/test_physical_validator_modules.py
git commit -m "Add scan_modules: enumerate submodules with per-module artifacts"
```

---

## Task 3: `physical_validator.parse_module_test_reports` (per-module test counts)

**Files:**
- Modify: `src/sag/agent/physical_validator.py` (add method after `scan_modules`)
- Test: `tests/test_physical_validator_modules.py` (extend)

Parse each module's report dir's `TEST-*.xml` for `tests/failures/errors/skipped` and failing test names. Use a single in-container awk/grep over the module's report dir; one orchestrator call per module keeps it cheap.

- [ ] **Step 1: Write the failing test** (append to `tests/test_physical_validator_modules.py`)

```python
def test_parse_module_test_reports_counts_per_module():
    surefire_xml = (
        '<testsuite tests="3" failures="1" errors="0" skipped="1">'
        '<testcase classname="com.x.FooTest" name="ok"/>'
        '<testcase classname="com.x.FooTest" name="bad"><failure/></testcase>'
        '<testcase classname="com.x.FooTest" name="ign"><skipped/></testcase>'
        '</testsuite>'
    )

    class Orch:
        def execute_command(self, command, **kwargs):
            if "cat" in command and "surefire" in command:
                return {"success": True, "exit_code": 0, "output": surefire_xml}
            if "find" in command and "surefire" in command:
                return {"success": True, "exit_code": 0,
                        "output": "/w/m/target/surefire-reports/TEST-com.x.FooTest.xml"}
            return {"success": True, "exit_code": 0, "output": ""}

    v = PhysicalValidator(docker_orchestrator=Orch())
    res = v.parse_module_test_reports("/w/m", ["/w/m/target/surefire-reports"])
    assert res["tests_total"] == 3
    assert res["tests_failed"] == 1
    assert res["tests_skipped"] == 1
    assert res["failing_count"] == 1
    assert any("FooTest" in n for n in res["failing_names"])
    assert res["evidence_refs"] == ["/w/m/target/surefire-reports"]


def test_parse_module_test_reports_empty_when_no_dirs():
    v = PhysicalValidator(docker_orchestrator=object())
    assert v.parse_module_test_reports("/w/m", []) == {}
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_physical_validator_modules.py::test_parse_module_test_reports_counts_per_module -q`
Expected: FAIL with `AttributeError: ... parse_module_test_reports`.

- [ ] **Step 3: Implement `parse_module_test_reports`**

```python
    def parse_module_test_reports(
        self, module_dir: str, report_dirs: List[str]
    ) -> Dict[str, any]:
        """Parse one module's JUnit XML report dirs into counts + failing names.

        Returns {} when the module has no report dirs (test_source -> none).
        Sums testsuite attributes across the module's XML files; failing names
        are testcases containing a <failure> or <error> child.
        """
        if not self.docker_orchestrator or not report_dirs:
            return {}

        import re as _re

        totals = {"tests_total": 0, "tests_failed": 0, "tests_errors": 0, "tests_skipped": 0}
        failing: List[str] = []
        for rd in report_dirs:
            find_cmd = f"find {rd} -name 'TEST-*.xml' -o -name '*.xml' -path '*{rd}*' 2>/dev/null"
            listing = self._execute_command_with_logging(find_cmd, f"listing reports {rd}")
            files = [f for f in (listing.get("output") or "").splitlines() if f.strip().endswith(".xml")]
            for xml_file in files:
                cat = self._execute_command_with_logging(
                    f"cat '{xml_file}'", f"reading {xml_file}"
                )
                content = cat.get("output") or ""
                suite = _re.search(
                    r'<testsuite[^>]*tests="(\d+)"[^>]*failures="(\d+)"'
                    r'[^>]*errors="(\d+)"[^>]*skipped="(\d+)"',
                    content,
                )
                if suite:
                    totals["tests_total"] += int(suite.group(1))
                    totals["tests_failed"] += int(suite.group(2))
                    totals["tests_errors"] += int(suite.group(3))
                    totals["tests_skipped"] += int(suite.group(4))
                for case in _re.finditer(
                    r'<testcase[^>]*classname="([^"]*)"[^>]*name="([^"]*)"[^>]*>(.*?)</testcase>',
                    content,
                    _re.DOTALL,
                ):
                    body = case.group(3)
                    if "<failure" in body or "<error" in body:
                        failing.append(f"{case.group(1)}.{case.group(2)}")

        passed = max(
            totals["tests_total"] - totals["tests_failed"]
            - totals["tests_errors"] - totals["tests_skipped"],
            0,
        )
        return {
            "tests_total": totals["tests_total"],
            "tests_passed": passed,
            "tests_failed": totals["tests_failed"],
            "tests_errors": totals["tests_errors"],
            "tests_skipped": totals["tests_skipped"],
            "failing_names": failing,
            "failing_count": len(failing),
            "evidence_refs": list(report_dirs),
        }
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_physical_validator_modules.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/sag/agent/physical_validator.py tests/test_physical_validator_modules.py
git commit -m "Add parse_module_test_reports for per-module test counts"
```

---

## Task 4: Persist `module_metrics.json` from the report tool

**Files:**
- Modify: `src/sag/tools/report_tool.py` (add `_persist_module_metrics` near `_persist_report_metrics` ~line 2982; add a `_build_module_metrics` helper; call it in the generate flow near where `_persist_report_metrics` is called ~line 929)
- Test: `tests/test_report_module_metrics.py`

The reactor status comes from the already-loaded test history (`_load_test_history` reads `test_summary.jsonl`, whose entries carry `reactor_summary`/`failed_modules`/`skipped_modules`). Map reactor entries to `{module_label: status}` and `build_error_samples` from `failed_modules`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_report_module_metrics.py
from sag.tools.report_tool import ReportTool


def test_build_module_metrics_reconciles_scan_and_reactor(monkeypatch):
    tool = ReportTool()

    monkeypatch.setattr(tool, "_get_project_info", lambda: {
        "directory": "/workspace/p", "build_system": "Maven"})

    class V:
        def scan_modules(self, project_dir, build_system):
            return [
                {"path": "core", "name": "core", "class_count": 50, "jar_count": 1,
                 "report_dirs": ["/workspace/p/core/target/surefire-reports"]},
                {"path": "api", "name": "api", "class_count": 0, "jar_count": 0,
                 "report_dirs": []},
            ]

        def parse_module_test_reports(self, module_dir, report_dirs):
            if report_dirs:
                return {"tests_total": 10, "tests_passed": 9, "tests_failed": 1,
                        "tests_errors": 0, "tests_skipped": 0,
                        "failing_names": ["core.FooTest.bad"], "failing_count": 1,
                        "evidence_refs": report_dirs}
            return {}

    tool.physical_validator = V()
    test_history = {
        "reactor_records": [{"module": "core", "status": "success"},
                            {"module": "api", "status": "failure"}],
        "failed_modules": ["api"],
    }
    metrics = tool._build_module_metrics(test_history, generated_at="t")
    by_path = {m["path"]: m for m in metrics["modules"]}
    assert by_path["core"]["build_status"] == "success"
    assert by_path["core"]["tests_failed"] == 1
    assert by_path["api"]["build_status"] == "failure"
    assert metrics["module_summary"]["modules_with_test_failures"] == 1


def test_build_module_metrics_returns_none_without_validator():
    tool = ReportTool()
    tool.physical_validator = None
    assert tool._build_module_metrics({}, generated_at="t") is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_report_module_metrics.py -q`
Expected: FAIL with `AttributeError: ... _build_module_metrics`.

- [ ] **Step 3: Implement the helpers in `report_tool.py`**

First, near the top imports add: `from sag.tools.module_metrics import assemble_module_metrics, MODULE_METRICS_PATH`.

Add these methods to `ReportTool`:

```python
    def _reactor_status_from_history(self, test_history: dict) -> dict:
        """Flatten reactor_summary records from test history into {label: status}."""
        status: dict = {}
        records = (test_history or {}).get("reactor_records") or []
        for rec in records:
            label = str(rec.get("module") or "").strip()
            state = str(rec.get("status") or "").strip().lower()
            if label and state:
                # Match by last path/label segment so reactor labels line up
                # with module names (e.g. "connect:api" or "api").
                status[label] = state
                status[label.split(":")[-1]] = state
        return status

    def _build_module_metrics(self, test_history: dict, *, generated_at: str):
        """Assemble the per-module metrics dict, or None when unavailable."""
        validator = getattr(self, "physical_validator", None)
        if validator is None:
            return None
        project_info = self._get_project_info() or {}
        project_dir = project_info.get("directory") or "/workspace"
        build_system = str(project_info.get("build_system") or "").strip().lower()
        if build_system not in ("maven", "gradle"):
            build_system = "maven"
        try:
            modules = validator.scan_modules(project_dir, build_system)
        except Exception as exc:
            logger.debug(f"scan_modules failed: {exc}")
            return None
        if not modules:
            return None

        tests: dict = {}
        for m in modules:
            parsed = validator.parse_module_test_reports(
                f"{project_dir}/{m['path']}" if m["path"] != "." else project_dir,
                m.get("report_dirs") or [],
            )
            if parsed:
                tests[m["path"]] = parsed

        reactor_status = self._reactor_status_from_history(test_history)
        build_error_samples = {}  # populated by Maven error parsing when available
        return assemble_module_metrics(
            modules=modules,
            reactor_status=reactor_status,
            tests=tests,
            build_systems=[build_system],
            build_error_samples=build_error_samples,
            generated_at=generated_at,
        )

    def _persist_module_metrics(self, metrics: dict) -> None:
        """Best-effort write of module_metrics.json (never blocks report gen)."""
        if not metrics or not self.docker_orchestrator:
            return
        try:
            import json as _json
            payload = _json.dumps(metrics, indent=2)
            delimiter = "SAG_MODULE_METRICS_EOF"
            cmd = (
                f"mkdir -p /workspace/.setup_agent && "
                f"cat > {MODULE_METRICS_PATH} <<'{delimiter}'\n{payload}\n{delimiter}"
            )
            self.docker_orchestrator.execute_command(cmd)
        except Exception as exc:
            logger.debug(f"Failed to persist module metrics: {exc}")
```

- [ ] **Step 4: Wire the call into the generate flow.** Find where `_persist_report_metrics(...)` is invoked (~line 929, inside the `try` block after `report_snapshot` is built). Immediately after that call, add:

```python
            try:
                module_metrics = self._build_module_metrics(
                    physical_validation.get("test_history") or self._load_test_history() or {},
                    generated_at=generated_at,
                )
                if module_metrics:
                    self._persist_module_metrics(module_metrics)
            except Exception as exc:
                logger.debug(f"module metrics step skipped: {exc}")
```

(Use the same `generated_at` value already in scope for `_persist_report_metrics`; if it is named differently there, reuse that exact variable.)

- [ ] **Step 5: Make reactor records available in test history.** In `_load_test_history` (~line 1008), ensure the returned dict includes `reactor_records` and `failed_modules` aggregated from the jsonl entries. Add, where the per-line entries are aggregated, a collection step:

```python
            reactor_records: list = []
            failed_modules: list = []
            for entry in entries:   # entries = parsed jsonl lines already in this method
                reactor_records.extend(entry.get("reactor_summary") or [])
                failed_modules.extend(entry.get("failed_modules") or [])
            history["reactor_records"] = reactor_records
            history["failed_modules"] = failed_modules
```

(Adapt variable names to the actual locals in `_load_test_history`; the goal is the returned history dict carries `reactor_records` and `failed_modules`.)

- [ ] **Step 6: Run tests**

Run: `.venv/bin/python -m pytest tests/test_report_module_metrics.py tests/test_report_contract.py -q`
Expected: PASS (new tests + no regression in the report contract suite).

- [ ] **Step 7: Commit**

```bash
git add src/sag/tools/report_tool.py tests/test_report_module_metrics.py
git commit -m "Assemble and persist module_metrics.json from the report tool"
```

---

## Task 5: Web models `ModuleSummary` + `ModuleRollup`

**Files:**
- Modify: `src/sag/web/models.py` (add classes before `ExecutionSessionDetail`; extend it)
- Test: `tests/test_web_module_models.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_module_models.py
from sag.web.models import ModuleSummary, ModuleRollup


def test_module_summary_round_trips_camelcase():
    m = ModuleSummary.model_validate({
        "name": "connect:api", "path": "connect/api",
        "build_status": "success", "build_source": "reactor",
        "class_count": 180, "jar_count": 3,
        "tests_total": 198, "tests_passed": 198, "tests_failed": 0,
        "failing_names": [], "failing_count": 0,
        "evidence_refs": ["/w/connect/api/target/surefire-reports"],
    })
    d = m.model_dump(mode="json", by_alias=True)
    assert d["buildStatus"] == "success"
    assert d["classCount"] == 180
    assert d["testsTotal"] == 198
    assert d["failingCount"] == 0
    assert d["evidenceRefs"] == ["/w/connect/api/target/surefire-reports"]


def test_module_rollup_camelcase():
    r = ModuleRollup.model_validate({
        "modules_total": 24, "modules_built": 21, "modules_failed": 1,
        "modules_skipped": 2, "modules_with_test_failures": 2,
        "build_systems": ["maven"], "single_module": False,
    })
    d = r.model_dump(mode="json", by_alias=True)
    assert d["modulesTotal"] == 24 and d["modulesWithTestFailures"] == 2
    assert d["buildSystems"] == ["maven"] and d["singleModule"] is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_web_module_models.py -q`
Expected: FAIL with `ImportError: cannot import name 'ModuleSummary'`.

- [ ] **Step 3: Implement the models** (add to `src/sag/web/models.py`, before `class ExecutionSessionDetail`)

```python
class ModuleSummary(WebModel):
    name: str = ""
    path: str = ""
    build_status: str = Field(
        default="unknown",
        validation_alias=AliasChoices("build_status", "buildStatus"),
        serialization_alias="buildStatus",
    )
    build_source: str = Field(
        default="none",
        validation_alias=AliasChoices("build_source", "buildSource"),
        serialization_alias="buildSource",
    )
    class_count: int | None = Field(
        default=None,
        validation_alias=AliasChoices("class_count", "classCount"),
        serialization_alias="classCount",
    )
    jar_count: int | None = Field(
        default=None,
        validation_alias=AliasChoices("jar_count", "jarCount"),
        serialization_alias="jarCount",
    )
    build_warnings: int | None = Field(
        default=None,
        validation_alias=AliasChoices("build_warnings", "buildWarnings"),
        serialization_alias="buildWarnings",
    )
    build_error_samples: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("build_error_samples", "buildErrorSamples"),
        serialization_alias="buildErrorSamples",
    )
    tests_total: int | None = Field(
        default=None,
        validation_alias=AliasChoices("tests_total", "testsTotal"),
        serialization_alias="testsTotal",
    )
    tests_passed: int | None = Field(
        default=None,
        validation_alias=AliasChoices("tests_passed", "testsPassed"),
        serialization_alias="testsPassed",
    )
    tests_failed: int | None = Field(
        default=None,
        validation_alias=AliasChoices("tests_failed", "testsFailed"),
        serialization_alias="testsFailed",
    )
    tests_errors: int | None = Field(
        default=None,
        validation_alias=AliasChoices("tests_errors", "testsErrors"),
        serialization_alias="testsErrors",
    )
    tests_skipped: int | None = Field(
        default=None,
        validation_alias=AliasChoices("tests_skipped", "testsSkipped"),
        serialization_alias="testsSkipped",
    )
    test_source: str = Field(
        default="none",
        validation_alias=AliasChoices("test_source", "testSource"),
        serialization_alias="testSource",
    )
    failing_names: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("failing_names", "failingNames"),
        serialization_alias="failingNames",
    )
    failing_count: int | None = Field(
        default=None,
        validation_alias=AliasChoices("failing_count", "failingCount"),
        serialization_alias="failingCount",
    )
    evidence_refs: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("evidence_refs", "evidenceRefs"),
        serialization_alias="evidenceRefs",
    )


class ModuleRollup(WebModel):
    modules_total: int = Field(
        default=0,
        validation_alias=AliasChoices("modules_total", "modulesTotal"),
        serialization_alias="modulesTotal",
    )
    modules_built: int = Field(
        default=0,
        validation_alias=AliasChoices("modules_built", "modulesBuilt"),
        serialization_alias="modulesBuilt",
    )
    modules_failed: int = Field(
        default=0,
        validation_alias=AliasChoices("modules_failed", "modulesFailed"),
        serialization_alias="modulesFailed",
    )
    modules_skipped: int = Field(
        default=0,
        validation_alias=AliasChoices("modules_skipped", "modulesSkipped"),
        serialization_alias="modulesSkipped",
    )
    modules_with_test_failures: int = Field(
        default=0,
        validation_alias=AliasChoices(
            "modules_with_test_failures", "modulesWithTestFailures"
        ),
        serialization_alias="modulesWithTestFailures",
    )
    build_systems: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("build_systems", "buildSystems"),
        serialization_alias="buildSystems",
    )
    single_module: bool = Field(
        default=False,
        validation_alias=AliasChoices("single_module", "singleModule"),
        serialization_alias="singleModule",
    )
```

Then extend `ExecutionSessionDetail` with two fields (after `test: TestSummary`):

```python
    modules: list[ModuleSummary] = Field(default_factory=list)
    module_summary: ModuleRollup | None = Field(
        default=None,
        validation_alias=AliasChoices("module_summary", "moduleSummary"),
        serialization_alias="moduleSummary",
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_web_module_models.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/sag/web/models.py tests/test_web_module_models.py
git commit -m "Add ModuleSummary/ModuleRollup web models on ExecutionSessionDetail"
```

---

## Task 6: Read model — load `module_metrics.json` into the session detail

**Files:**
- Modify: `src/sag/web/session_registry.py` (add readers near `_read_report_metrics` ~line 762; wire into `_session_detail` ~line 308; add `modules` + `module_summary` to the item in `_setup_artifact_item` ~line 470)
- Test: `tests/test_web_module_read_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_module_read_model.py
import json
from sag.web.session_registry import _modules_payload_from_metrics, _module_rollup_from_metrics


def _metrics():
    return {
        "version": 1,
        "module_summary": {"modules_total": 2, "modules_built": 1, "modules_failed": 1,
                           "modules_skipped": 0, "modules_with_test_failures": 1,
                           "build_systems": ["maven"], "single_module": False},
        "modules": [
            {"name": "core", "path": "core", "build_status": "success",
             "build_source": "reactor", "class_count": 50, "jar_count": 1,
             "tests_total": 10, "tests_passed": 9, "tests_failed": 1,
             "failing_names": ["core.FooTest.bad"], "failing_count": 1,
             "evidence_refs": ["/w/core/target/surefire-reports"]},
            {"name": "api", "path": "api", "build_status": "failure",
             "build_source": "reactor", "class_count": 0, "jar_count": 0,
             "tests_total": None, "failing_names": [], "failing_count": None,
             "evidence_refs": []},
        ],
    }


def test_modules_payload_maps_records():
    payload = _modules_payload_from_metrics(_metrics())
    assert len(payload) == 2
    assert payload[0]["build_status"] == "success"
    assert payload[0]["failing_count"] == 1


def test_module_rollup_maps_summary():
    rollup = _module_rollup_from_metrics(_metrics())
    assert rollup["modules_total"] == 2 and rollup["modules_failed"] == 1


def test_payload_none_when_absent():
    assert _modules_payload_from_metrics(None) == []
    assert _module_rollup_from_metrics(None) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_web_module_read_model.py -q`
Expected: FAIL with `ImportError: cannot import name '_modules_payload_from_metrics'`.

- [ ] **Step 3: Implement the readers** (add near `_read_report_metrics` in `session_registry.py`)

```python
def _read_module_metrics(orchestrator: Any) -> dict[str, Any] | None:
    """Read module_metrics.json from the container, or None when absent/invalid."""
    from sag.tools.module_metrics import MODULE_METRICS_PATH

    raw = _read_container_file(orchestrator, MODULE_METRICS_PATH)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _modules_payload_from_metrics(metrics: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(metrics, dict):
        return []
    modules = metrics.get("modules")
    return [m for m in modules if isinstance(m, dict)] if isinstance(modules, list) else []


def _module_rollup_from_metrics(metrics: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(metrics, dict):
        return None
    summary = metrics.get("module_summary")
    return summary if isinstance(summary, dict) else None
```

- [ ] **Step 4: Populate the item in `_setup_artifact_item`.** After the `metrics = _read_report_metrics(orchestrator)` line (~460), add:

```python
    module_metrics = _read_module_metrics(orchestrator)
```

and add two keys to the returned dict (alongside `"build"`/`"test"`):

```python
        "modules": _modules_payload_from_metrics(module_metrics),
        "module_summary": _module_rollup_from_metrics(module_metrics),
```

- [ ] **Step 5: Wire into `_session_detail`.** In the `ExecutionSessionDetail(...)` constructor call (~line 318), add:

```python
        modules=[ModuleSummary.model_validate(m) for m in (item.get("modules") or [])],
        module_summary=(
            ModuleRollup.model_validate(item["module_summary"])
            if item.get("module_summary") else None
        ),
```

Add `ModuleSummary, ModuleRollup` to the existing `from sag.web.models import (...)` block at the top of the file.

- [ ] **Step 6: Run tests**

Run: `.venv/bin/python -m pytest tests/test_web_module_read_model.py tests/test_web_metrics_read_model.py -q`
Expected: PASS (new tests + no regression).

- [ ] **Step 7: Commit**

```bash
git add src/sag/web/session_registry.py tests/test_web_module_read_model.py
git commit -m "Load module_metrics.json into ExecutionSessionDetail (read model)"
```

---

## Task 7: Demo data + frontend types

**Files:**
- Modify: `src/sag/web/demo_data.py` (add `modules`/`module_summary` to the demo session detail)
- Modify: `webui/src/api/types.ts` (add `ModuleSummary`, `ModuleRollup`; extend `ExecutionSessionDetail`)
- Test: `tests/test_web_demo_data.py` (extend or add) for demo modules; frontend types are checked by `tsc`.

- [ ] **Step 1: Write the failing test** (backend demo)

```python
# tests/test_web_demo_data.py  (add this test; create the file if missing)
from sag.web.demo_data import build_demo_dashboard  # adapt to the actual demo entrypoint


def test_demo_session_detail_has_modules():
    # Find the demo session detail and assert it carries multi-module data.
    from sag.web import demo_data
    detail = demo_data.demo_session_detail()  # adapt to actual function name
    assert detail.modules, "demo detail should include modules"
    assert detail.module_summary is not None
    assert detail.module_summary.modules_total >= 2
```

If the demo module exposes differently-named entrypoints, first run `grep -n "def " src/sag/web/demo_data.py` and adapt the import/call to the real function that returns an `ExecutionSessionDetail`.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_web_demo_data.py -q`
Expected: FAIL (no modules on the demo detail).

- [ ] **Step 3: Implement demo modules.** In `demo_data.py`, where the demo `ExecutionSessionDetail` is built, add:

```python
    modules=[
        ModuleSummary(name="connect:runtime", path="connect/runtime",
                      build_status="failure", build_source="reactor",
                      class_count=None, jar_count=None,
                      build_error_samples=["[ERROR] WorkerSinkTask.java:[412,7] cannot find symbol"],
                      test_source="none", failing_names=[], failing_count=None,
                      evidence_refs=["/workspace/connect/runtime/target"]),
        ModuleSummary(name="streams", path="streams", build_status="success",
                      build_source="reactor", class_count=2610, jar_count=18,
                      tests_total=1240, tests_passed=1238, tests_failed=2, tests_errors=0,
                      tests_skipped=0, test_source="runner_xml",
                      failing_names=["org.apache.kafka.streams.StreamThreadTest.shouldShutdown",
                                     "org.apache.kafka.streams.state.RocksDBStoreTest.shouldFlush"],
                      failing_count=2,
                      evidence_refs=["/workspace/streams/build/test-results"]),
        ModuleSummary(name="clients", path="clients", build_status="success",
                      build_source="reactor", class_count=3140, jar_count=22,
                      tests_total=1420, tests_passed=1420, tests_failed=0, tests_errors=0,
                      tests_skipped=0, test_source="runner_xml",
                      failing_names=[], failing_count=0,
                      evidence_refs=["/workspace/clients/build/test-results"]),
    ],
    module_summary=ModuleRollup(modules_total=3, modules_built=2, modules_failed=1,
                                modules_skipped=0, modules_with_test_failures=1,
                                build_systems=["maven"], single_module=False),
```

Add `ModuleSummary, ModuleRollup` to the demo module's imports from `sag.web.models`.

- [ ] **Step 4: Add the frontend types** to `webui/src/api/types.ts`:

```typescript
export interface ModuleSummary {
  name: string
  path: string
  buildStatus: "success" | "failure" | "skipped" | "unknown"
  buildSource: "reactor" | "artifacts" | "partial" | "none"
  classCount?: number | null
  jarCount?: number | null
  buildWarnings?: number | null
  buildErrorSamples?: string[]
  testsTotal?: number | null
  testsPassed?: number | null
  testsFailed?: number | null
  testsErrors?: number | null
  testsSkipped?: number | null
  testSource: "runner_xml" | "partial" | "none"
  failingNames?: string[]
  failingCount?: number | null
  evidenceRefs?: string[]
}

export interface ModuleRollup {
  modulesTotal: number
  modulesBuilt: number
  modulesFailed: number
  modulesSkipped: number
  modulesWithTestFailures: number
  buildSystems: string[]
  singleModule: boolean
}
```

And add to the existing `ExecutionSessionDetail` interface:

```typescript
  modules?: ModuleSummary[]
  moduleSummary?: ModuleRollup | null
```

- [ ] **Step 5: Verify**

Run: `.venv/bin/python -m pytest tests/test_web_demo_data.py -q` (PASS) and `cd webui && npx tsc -p tsconfig.app.json --noEmit` (no output).

- [ ] **Step 6: Commit**

```bash
git add src/sag/web/demo_data.py webui/src/api/types.ts tests/test_web_demo_data.py
git commit -m "Add module demo data and frontend module types"
```

---

## Task 8: `ModuleTable` component (failures-first, inline-expand)

**Files:**
- Create: `webui/src/components/session/ModuleTable.tsx`
- Test: `webui/src/components/session/ModuleTable.test.tsx`

A reusable table used by both detail pages. Props pick which columns to show (`variant: "build" | "test"`). Failures-first ordering; rows with failures/errors get an inline expand revealing the full failing list (test variant) or `buildErrorSamples` (build variant). Uses `StatusBadge`, `cn`, Tailwind mono tokens.

- [ ] **Step 1: Write the failing test**

```tsx
// webui/src/components/session/ModuleTable.test.tsx
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import type { ModuleSummary } from "@/api/types"

import { ModuleTable } from "./ModuleTable"

const modules: ModuleSummary[] = [
  { name: "clients", path: "clients", buildStatus: "success", buildSource: "reactor",
    classCount: 3140, jarCount: 22, testsTotal: 1420, testsPassed: 1420, testsFailed: 0,
    testSource: "runner_xml", failingNames: [], failingCount: 0 },
  { name: "streams", path: "streams", buildStatus: "success", buildSource: "reactor",
    classCount: 2610, jarCount: 18, testsTotal: 1240, testsPassed: 1238, testsFailed: 2,
    testSource: "runner_xml",
    failingNames: ["a.StreamTest.shouldX", "b.StateTest.shouldY"], failingCount: 2 },
]

afterEach(() => cleanup())

describe("ModuleTable (test variant)", () => {
  it("orders failures first and expands the full failing list", () => {
    render(<ModuleTable modules={modules} variant="test" />)
    const rows = screen.getAllByRole("row")
    // streams (has failures) precedes clients (none) — failures first
    const text = rows.map((r) => r.textContent).join("|")
    expect(text.indexOf("streams")).toBeLessThan(text.indexOf("clients"))
    // failing names hidden until expand
    expect(screen.queryByText("a.StreamTest.shouldX")).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: /view 2 failures/i }))
    expect(screen.getByText("a.StreamTest.shouldX")).toBeInTheDocument()
    expect(screen.getByText("b.StateTest.shouldY")).toBeInTheDocument()
  })

  it("shows truncation pointer when failingCount exceeds names", () => {
    render(<ModuleTable variant="test" modules={[{
      name: "m", path: "m", buildStatus: "success", buildSource: "reactor",
      testSource: "runner_xml", testsFailed: 600,
      failingNames: ["x.T.a"], failingCount: 600, evidenceRefs: ["/w/m"] }]} />)
    fireEvent.click(screen.getByRole("button", { name: /view 600 failures/i }))
    expect(screen.getByText(/\+599 more/)).toBeInTheDocument()
    expect(screen.getByText(/\/w\/m/)).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd webui && npx vitest run src/components/session/ModuleTable.test.tsx`
Expected: FAIL — module `./ModuleTable` not found.

- [ ] **Step 3: Implement `ModuleTable.tsx`**

```tsx
import { ChevronDown } from "lucide-react"
import { useState } from "react"

import type { ModuleSummary } from "@/api/types"
import { cn } from "@/lib/utils"

function statusClass(s: string): string {
  if (s === "success") return "bg-emerald-50 text-emerald-700"
  if (s === "failure") return "bg-red-50 text-red-600"
  if (s === "skipped") return "bg-slate-100 text-slate-500"
  return "bg-amber-50 text-amber-700"
}

function num(n?: number | null): string {
  return typeof n === "number" && Number.isFinite(n) ? n.toLocaleString() : "—"
}

function failureRank(m: ModuleSummary): number {
  if (m.buildStatus === "failure") return 0
  if ((m.failingCount ?? 0) > 0) return 1
  if (m.buildStatus === "skipped") return 2
  return 3
}

export function ModuleTable({
  modules,
  variant,
}: {
  modules: ModuleSummary[]
  variant: "build" | "test"
}) {
  const [open, setOpen] = useState<string | null>(null)
  const ordered = [...modules].sort((a, b) => failureRank(a) - failureRank(b))

  return (
    <table className="w-full border-collapse">
      <thead>
        <tr className="border-b border-slate-200 font-mono text-[10px] uppercase tracking-[0.1em] text-slate-500">
          <th className="px-2 py-2 text-left">Module</th>
          <th className="px-2 py-2 text-left">Build</th>
          {variant === "build" ? (
            <>
              <th className="px-2 py-2 text-right">Classes</th>
              <th className="px-2 py-2 text-right">JARs</th>
              <th className="px-2 py-2 text-left">Detail</th>
            </>
          ) : (
            <>
              <th className="px-2 py-2 text-right">Pass</th>
              <th className="px-2 py-2 text-right">Fail</th>
              <th className="px-2 py-2 text-right">Skip</th>
              <th className="px-2 py-2 text-left">Failing methods</th>
            </>
          )}
        </tr>
      </thead>
      <tbody>
        {ordered.map((m) => {
          const isOpen = open === m.path
          const depth = m.path === "." ? 0 : m.path.split("/").length - 1
          const failing = m.failingNames ?? []
          const fc = m.failingCount ?? 0
          const hidden = Math.max(fc - failing.length, 0)
          const errs = m.buildErrorSamples ?? []
          const canExpandTest = variant === "test" && fc > 0
          const canExpandBuild = variant === "build" && m.buildStatus === "failure" && errs.length > 0
          return (
            <>
              <tr key={m.path} className="border-b border-slate-100 font-mono text-[12px] tabular-nums">
                <td className="px-2 py-2" style={{ paddingLeft: 8 + depth * 14 }}>{m.name}</td>
                <td className="px-2 py-2">
                  <span className={cn("rounded px-2 py-0.5 text-[10px]", statusClass(m.buildStatus))}>
                    {m.buildStatus.toUpperCase()}
                  </span>
                </td>
                {variant === "build" ? (
                  <>
                    <td className="px-2 py-2 text-right">{num(m.classCount)}</td>
                    <td className="px-2 py-2 text-right">{num(m.jarCount)}</td>
                    <td className="px-2 py-2">
                      {canExpandBuild ? (
                        <button className="text-red-600 underline decoration-dotted" type="button"
                          onClick={() => setOpen(isOpen ? null : m.path)}>
                          {errs.length} error{errs.length > 1 ? "s" : ""}
                          <ChevronDown className={cn("ml-1 inline", isOpen && "rotate-180")} size={12} />
                        </button>
                      ) : m.buildStatus === "skipped" ? (
                        <span className="text-slate-400">upstream failed</span>
                      ) : <span className="text-slate-300">—</span>}
                    </td>
                  </>
                ) : (
                  <>
                    <td className="px-2 py-2 text-right text-emerald-700">{num(m.testsPassed)}</td>
                    <td className={cn("px-2 py-2 text-right", (m.testsFailed ?? 0) > 0 && "text-red-600")}>{num(m.testsFailed)}</td>
                    <td className="px-2 py-2 text-right">{num(m.testsSkipped)}</td>
                    <td className="px-2 py-2">
                      {canExpandTest ? (
                        <button className="text-red-600 underline decoration-dotted" type="button"
                          onClick={() => setOpen(isOpen ? null : m.path)}>
                          View {fc} failure{fc > 1 ? "s" : ""}
                          <ChevronDown className={cn("ml-1 inline", isOpen && "rotate-180")} size={12} />
                        </button>
                      ) : <span className="text-slate-300">—</span>}
                    </td>
                  </>
                )}
              </tr>
              {isOpen ? (
                <tr key={`${m.path}-detail`} className="bg-red-50/60">
                  <td colSpan={variant === "build" ? 5 : 6} className="px-3 py-2">
                    <div className="max-h-48 overflow-auto font-mono text-[11px] text-red-700">
                      {(variant === "test" ? failing : errs).map((line) => (
                        <div key={line} className="py-0.5">{line}</div>
                      ))}
                      {variant === "test" && hidden > 0 ? (
                        <div className="text-slate-500">+{hidden} more — full list at {(m.evidenceRefs ?? [])[0] ?? "report dir"}</div>
                      ) : null}
                    </div>
                  </td>
                </tr>
              ) : null}
            </>
          )
        })}
      </tbody>
    </table>
  )
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd webui && npx vitest run src/components/session/ModuleTable.test.tsx`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add webui/src/components/session/ModuleTable.tsx webui/src/components/session/ModuleTable.test.tsx
git commit -m "Add failures-first ModuleTable with inline-expand"
```

---

## Task 9: Build/Test detail pages + clickable cards + Workspace wiring

**Files:**
- Create: `webui/src/components/session/BuildDetailPage.tsx`, `webui/src/components/session/TestDetailPage.tsx`
- Modify: `webui/src/components/session/BuildCard.tsx`, `TestCard.tsx` (add `onOpenDetail?: () => void`, whole-card button, remove inline expand)
- Modify: `webui/src/pages/Workspace.tsx` (`detail` state + render)
- Test: `webui/src/components/session/BuildDetailPage.test.tsx`, `TestDetailPage.test.tsx`

- [ ] **Step 1: Write the failing tests**

```tsx
// webui/src/components/session/TestDetailPage.test.tsx
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { TestDetailPage } from "./TestDetailPage"

const detail: any = {
  test: { state: "partial", pass: 3838, fail: 3, skip: 0, total: 3841, passRate: 99.9,
          uniqueTotal: 2907 },
  moduleSummary: { modulesTotal: 24, modulesBuilt: 21, modulesFailed: 1, modulesSkipped: 2,
                   modulesWithTestFailures: 2, buildSystems: ["maven"], singleModule: false },
  modules: [
    { name: "streams", path: "streams", buildStatus: "success", buildSource: "reactor",
      testSource: "runner_xml", testsPassed: 1238, testsFailed: 2, testsSkipped: 0,
      failingNames: ["a.StreamTest.shouldX", "b.StateTest.shouldY"], failingCount: 2 },
  ],
}

afterEach(() => cleanup())

describe("TestDetailPage", () => {
  it("renders tiles, a back button, and the per-module table", () => {
    const onBack = vi.fn()
    render(<TestDetailPage detail={detail} onBack={onBack} />)
    expect(screen.getByText("3,841")).toBeInTheDocument()      // runner exec tile
    expect(screen.getByText(/modules w\/ fails/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: /back/i }))
    expect(onBack).toHaveBeenCalled()
    fireEvent.click(screen.getByRole("button", { name: /view 2 failures/i }))
    expect(screen.getByText("a.StreamTest.shouldX")).toBeInTheDocument()
  })

  it("shows a single-module note when not multi-module", () => {
    render(<TestDetailPage onBack={() => {}} detail={{
      test: detail.test,
      moduleSummary: { ...detail.moduleSummary, modulesTotal: 1, singleModule: true },
      modules: [],
    }} />)
    expect(screen.getByText(/single module/i)).toBeInTheDocument()
  })
})
```

```tsx
// webui/src/components/session/BuildDetailPage.test.tsx
import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { BuildDetailPage } from "./BuildDetailPage"

afterEach(() => cleanup())

it("renders build tiles and per-module table", () => {
  render(<BuildDetailPage onBack={() => {}} detail={{
    build: { state: "success", system: "maven", classCount: 13104, jarCount: 279 },
    moduleSummary: { modulesTotal: 24, modulesBuilt: 21, modulesFailed: 1, modulesSkipped: 2,
                     modulesWithTestFailures: 2, buildSystems: ["maven"], singleModule: false },
    modules: [{ name: "connect:runtime", path: "connect/runtime", buildStatus: "failure",
                buildSource: "reactor", buildErrorSamples: ["[ERROR] cannot find symbol"] }],
  } as any} />)
  expect(screen.getByText("24")).toBeInTheDocument()
  expect(screen.getByText(/built/i)).toBeInTheDocument()
  expect(screen.getByText("connect:runtime")).toBeInTheDocument()
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd webui && npx vitest run src/components/session/TestDetailPage.test.tsx src/components/session/BuildDetailPage.test.tsx`
Expected: FAIL — modules not found.

- [ ] **Step 3: Implement the detail pages.** Create `TestDetailPage.tsx`:

```tsx
import { ArrowLeft } from "lucide-react"

import type { ExecutionSessionDetail } from "@/api/types"
import { Button } from "@/components/common/Button"
import { Card } from "@/components/common/Card"
import { StatusBadge } from "@/components/common/Badge"

import { ModuleTable } from "./ModuleTable"

function Tile({ label, value, tone, dashed }: {
  label: string; value: React.ReactNode; tone?: string; dashed?: boolean
}) {
  return (
    <div className={`rounded-lg border px-3 py-2 ${dashed ? "border-dashed bg-slate-50" : "border-slate-200"}`}>
      <div className={`text-[22px] font-semibold tabular-nums ${tone ?? "text-slate-900"}`}>{value}</div>
      <div className="font-mono text-[10px] uppercase tracking-[0.1em] text-slate-500">{label}</div>
    </div>
  )
}

export function TestDetailPage({
  detail, onBack,
}: { detail: ExecutionSessionDetail; onBack: () => void }) {
  const t = detail.test
  const s = detail.moduleSummary
  const single = s?.singleModule ?? (detail.modules?.length ?? 0) <= 1
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <Button onClick={onBack} size="sm" type="button" variant="ghost">
          <ArrowLeft size={14} /> Back
        </Button>
        <StatusBadge status={t.state} />
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-7">
        <Tile label="Runner exec" value={(t.total ?? 0).toLocaleString()} />
        <Tile label="Passed" value={(t.pass ?? 0).toLocaleString()} tone="text-emerald-700" />
        <Tile label="Failed" value={(t.fail ?? 0).toLocaleString()} tone="text-red-600" />
        <Tile label="Skipped" value={(t.skip ?? 0).toLocaleString()} />
        <Tile label="Unique methods" value={(t.uniqueTotal ?? "—").toLocaleString?.() ?? "—"} />
        <Tile label="Modules w/ fails" value={s?.modulesWithTestFailures ?? "—"} tone="text-red-600" />
        <Tile label="Coverage" value={<>— <span className="rounded bg-amber-50 px-1 font-mono text-[9px] text-amber-700">Feature B</span></>} dashed />
      </div>
      <Card className="p-4">
        {single ? (
          <div className="font-mono text-[12px] text-slate-500">Single module project — see the project-level test summary on Overview.</div>
        ) : (
          <ModuleTable modules={detail.modules ?? []} variant="test" />
        )}
      </Card>
    </div>
  )
}
```

Create `BuildDetailPage.tsx` (same shape; build tiles + `variant="build"`):

```tsx
import { ArrowLeft } from "lucide-react"

import type { ExecutionSessionDetail } from "@/api/types"
import { Button } from "@/components/common/Button"
import { Card } from "@/components/common/Card"
import { StatusBadge } from "@/components/common/Badge"

import { ModuleTable } from "./ModuleTable"

function Tile({ label, value, tone }: { label: string; value: React.ReactNode; tone?: string }) {
  return (
    <div className="rounded-lg border border-slate-200 px-3 py-2">
      <div className={`text-[22px] font-semibold tabular-nums ${tone ?? "text-slate-900"}`}>{value}</div>
      <div className="font-mono text-[10px] uppercase tracking-[0.1em] text-slate-500">{label}</div>
    </div>
  )
}

export function BuildDetailPage({
  detail, onBack,
}: { detail: ExecutionSessionDetail; onBack: () => void }) {
  const b = detail.build
  const s = detail.moduleSummary
  const single = s?.singleModule ?? (detail.modules?.length ?? 0) <= 1
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <Button onClick={onBack} size="sm" type="button" variant="ghost">
          <ArrowLeft size={14} /> Back
        </Button>
        <StatusBadge status={b.state} />
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-6">
        <Tile label="Modules" value={s?.modulesTotal ?? "—"} />
        <Tile label="Built" value={s?.modulesBuilt ?? "—"} tone="text-emerald-700" />
        <Tile label="Failed" value={s?.modulesFailed ?? "—"} tone="text-red-600" />
        <Tile label="Skipped" value={s?.modulesSkipped ?? "—"} />
        <Tile label="Classes" value={(b.classCount ?? 0).toLocaleString()} />
        <Tile label="JARs" value={(b.jarCount ?? 0).toLocaleString()} />
      </div>
      <Card className="p-4">
        {single ? (
          <div className="font-mono text-[12px] text-slate-500">Single module project — see the project-level build summary on Overview.</div>
        ) : (
          <ModuleTable modules={detail.modules ?? []} variant="build" />
        )}
      </Card>
    </div>
  )
}
```

- [ ] **Step 4: Make the cards clickable.** In `BuildCard.tsx` and `TestCard.tsx`, add an optional prop `onOpenDetail?: () => void`. Wrap the card's body in a `<button>`/clickable region that calls `onOpenDetail` and shows a `→` affordance; **remove** the existing inline `Details` expand toggle and its `TestDetails`/`BuildDetails` render (that content now lives on the detail page). Keep the conclusion-first headline/stats. Example for `TestCard.tsx` (replace the `Details` button + expand block):

```tsx
      {onOpenDetail ? (
        <button
          aria-label="Open test details"
          className="mt-3 inline-flex items-center gap-1 font-mono text-[10.5px] text-slate-500 hover:text-slate-700"
          onClick={onOpenDetail}
          type="button"
        >
          Details <span aria-hidden>→</span>
        </button>
      ) : null}
```

Update each card's prop type to include `onOpenDetail?: () => void`. Remove the now-unused `TestDetails`/`BuildDetails` imports and the `useState(open)` expand state. (Leave `TestDetails.tsx`/`BuildDetails.tsx` files in place; they are no longer imported — note this in the task result. A later cleanup may delete them.)

Update the existing `BuildCard.test.tsx` / `TestCard.test.tsx`: remove assertions that click "Details" to reveal the inline calculation table; add an assertion that clicking Details calls `onOpenDetail`.

- [ ] **Step 5: Wire Workspace.** In `webui/src/pages/Workspace.tsx`:

Add state and handlers in `Workspace`:

```tsx
  const [detail, setDetail] = useState<"build" | "test" | null>(null)
```

Reset it on workspace change (extend the existing `useEffect` that resets `tab`): add `setDetail(null)`.

In the render, when `detail` is set and `latest` exists, render the detail page instead of the tabs:

```tsx
  if (detail && latest) {
    return detail === "build"
      ? <BuildDetailPage detail={latest} onBack={() => setDetail(null)} />
      : <TestDetailPage detail={latest} onBack={() => setDetail(null)} />
  }
```

Pass `onOpenDetail` into the cards inside `OverviewTab` (thread a prop `onOpenDetail: (k: "build" | "test") => void` from `Workspace` into `OverviewTab`):

```tsx
        <BuildCard build={build} onOpenDetail={() => onOpenDetail("build")} />
        <TestCard test={test} onOpenDetail={() => onOpenDetail("test")} />
```

Import `BuildDetailPage`, `TestDetailPage`.

- [ ] **Step 6: Run the frontend suite + typecheck**

Run: `cd webui && npx vitest run && npx tsc -p tsconfig.app.json --noEmit`
Expected: all tests pass, no type errors.

- [ ] **Step 7: Commit**

```bash
git add webui/src/components/session/BuildDetailPage.tsx webui/src/components/session/TestDetailPage.tsx \
  webui/src/components/session/BuildDetailPage.test.tsx webui/src/components/session/TestDetailPage.test.tsx \
  webui/src/components/session/BuildCard.tsx webui/src/components/session/TestCard.tsx \
  webui/src/components/session/BuildCard.test.tsx webui/src/components/session/TestCard.test.tsx \
  webui/src/pages/Workspace.tsx
git commit -m "Add Build/Test detail pages reached by clicking Overview cards"
```

---

## Task 10: Report Markdown "Submodule Breakdown" section

**Files:**
- Modify: `src/sag/tools/report_tool.py` (add `_render_submodule_breakdown(self, module_metrics)` and call it from the markdown assembly where other sections are appended, e.g. near `_render_detailed_test_analysis`)
- Test: `tests/test_report_module_metrics.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
def test_submodule_breakdown_section_renders_failures_first():
    from sag.tools.report_tool import ReportTool
    tool = ReportTool()
    metrics = {
        "module_summary": {"modules_total": 3, "modules_built": 1, "modules_failed": 1,
                           "modules_skipped": 1, "modules_with_test_failures": 1,
                           "build_systems": ["maven"], "single_module": False},
        "modules": [
            {"name": "api", "path": "api", "build_status": "success",
             "tests_total": 10, "tests_passed": 10, "tests_failed": 0, "failing_count": 0},
            {"name": "runtime", "path": "runtime", "build_status": "failure",
             "tests_total": None, "failing_count": None},
            {"name": "core", "path": "core", "build_status": "success",
             "tests_total": 20, "tests_passed": 18, "tests_failed": 2, "failing_count": 2},
        ],
    }
    lines = tool._render_submodule_breakdown(metrics)
    body = "\n".join(lines)
    assert "Submodule Breakdown" in body
    assert "3 modules" in body and "1 failed" in body
    # failed/test-failing modules listed before all-green ones
    assert body.index("runtime") < body.index("api")


def test_submodule_breakdown_empty_for_single_module():
    from sag.tools.report_tool import ReportTool
    tool = ReportTool()
    assert tool._render_submodule_breakdown(
        {"module_summary": {"single_module": True}, "modules": [{"name": ".", "path": "."}]}
    ) == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_report_module_metrics.py -k submodule_breakdown -q`
Expected: FAIL — `_render_submodule_breakdown` missing.

- [ ] **Step 3: Implement `_render_submodule_breakdown`**

```python
    def _render_submodule_breakdown(self, module_metrics: dict) -> List[str]:
        """Markdown 'Submodule Breakdown' section; [] for single-module projects."""
        if not module_metrics:
            return []
        summary = module_metrics.get("module_summary") or {}
        modules = module_metrics.get("modules") or []
        if summary.get("single_module") or len(modules) <= 1:
            return []

        def rank(m):
            if m.get("build_status") == "failure":
                return 0
            if (m.get("failing_count") or 0) > 0:
                return 1
            if m.get("build_status") == "skipped":
                return 2
            return 3

        lines = ["", "## 🧩 Submodule Breakdown", ""]
        lines.append(
            f"{summary.get('modules_total', 0)} modules · "
            f"{summary.get('modules_built', 0)} built · "
            f"{summary.get('modules_failed', 0)} failed · "
            f"{summary.get('modules_skipped', 0)} skipped · "
            f"test failures in {summary.get('modules_with_test_failures', 0)}"
        )
        lines.append("")
        lines.append("| Module | Build | Tests (pass/fail/skip) |")
        lines.append("|---|---|---|")
        ordered = sorted(modules, key=rank)
        shown = ordered[:20]
        for m in shown:
            tp = m.get("tests_passed")
            tf = m.get("tests_failed")
            ts = m.get("tests_skipped")
            tests = (
                f"{tp if tp is not None else '—'}/"
                f"{tf if tf is not None else '—'}/"
                f"{ts if ts is not None else '—'}"
            )
            lines.append(f"| `{m.get('name')}` | {str(m.get('build_status', 'unknown')).upper()} | {tests} |")
        if len(ordered) > len(shown):
            lines.append("")
            lines.append(
                f"_+{len(ordered) - len(shown)} more modules — full per-module data in "
                f"`/workspace/.setup_agent/module_metrics.json`_"
            )
        return lines
```

- [ ] **Step 4: Call it from the markdown assembly.** In `_generate_markdown_report` (or the method that appends sections like `_render_detailed_test_analysis`), after the test analysis section, add:

```python
        try:
            mm = self._build_module_metrics(
                physical_validation.get("test_history") or self._load_test_history() or {},
                generated_at=timestamp,
            )
            report_lines.extend(self._render_submodule_breakdown(mm or {}))
        except Exception as exc:
            logger.debug(f"submodule breakdown skipped: {exc}")
```

Use the section's actual local names (`report_lines`, `timestamp`) — match the surrounding code.

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_report_module_metrics.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/sag/tools/report_tool.py tests/test_report_module_metrics.py
git commit -m "Render Submodule Breakdown section in the markdown report"
```

---

## Task 11: Full-suite gate, webui build, live verification (operator)

**Files:** none (verification only).

- [ ] **Step 1: Full backend suite** — `.venv/bin/python -m pytest tests/ -q --ignore=tests/test_web_task_runner.py`. Expected: all pass (no new failures vs. the branch point).

- [ ] **Step 2: Frontend suite + typecheck** — `cd webui && npx vitest run && npx tsc -p tsconfig.app.json --noEmit`. Expected: all pass, no type errors.

- [ ] **Step 3: Build the webui** — `cd webui && npm run build` (emits to `src/sag/web/static/`; leave the build output uncommitted per project policy).

- [ ] **Step 4: Live Maven multi-module verification** — run a multi-module Maven setup with `--record` (e.g. a reactor project from the eval set), then in-container confirm `module_metrics.json` exists with `module_summary.modules_total > 1`, per-module `build_status` values, and at least one module's `tests_*`. Confirm the read model surfaces it: start `sag ui`, open the workspace, click the Build card → Build Details (per-module table, failures-first), click the Test card → Test Details (per-module table, inline-expand reveals full failing list). Verify `sag ui --demo` shows the demo modules.

- [ ] **Step 5: Live Gradle verification** — run a Gradle multi-project setup; confirm per-subproject test results appear and build status is labeled best-effort/partial where Gradle gives no reactor table.

- [ ] **Step 6: Single-module sanity** — run a single-module project (commons-cli); confirm the detail pages show the "single module" note and the report omits the Submodule Breakdown section.

- [ ] **Step 7: Clean up** verification containers; report results; merge to main after approval.

---

## Notes for the implementer

- The reactor-label ↔ module-path matching in `_reactor_status_from_history` is best-effort (matches by full label and last `:` segment). When no match is found, the assembler falls back to physical artifacts and sets `build_source` to `artifacts`/`partial` — this is intended Gradle/edge behavior, not a bug.
- Maven `build_error_samples` population from the failed-module compiler errors is left as a follow-up enrichment; the field exists and renders, and `failed_modules` from `test_summary.jsonl` already drives `build_status=failure`. Do not block on parsing every error line.
- Keep all container writes best-effort: a failure to write `module_metrics.json` must never break report generation (mirror `_persist_report_metrics`).
