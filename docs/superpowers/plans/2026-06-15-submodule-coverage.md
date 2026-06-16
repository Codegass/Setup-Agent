# Submodule Code Coverage Implementation Plan (Feature B)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-submodule line/branch code coverage via an isolated, best-effort JaCoCo pass triggered by a `--coverage` handle, merged into `module_metrics.json` and surfaced as a per-module Coverage column + tile on the Test Details page.

**Architecture:** A `--coverage` flag (parallel to `--record`) runs a deterministic coverage runner AFTER the setup verdict is locked. The runner detects the build system, reuses existing `jacoco.xml` or injects JaCoCo without editing project files (Maven CLI plugin goals / Gradle `--init-script`), re-runs tests, parses line/branch counters per module, and merges them into the existing `module_metrics.json` (keyed by reactor path) with a lines-weighted rollup. The web read model already loads that artifact; the UI gains a Coverage column and a populated Coverage tile. Strictly additive: no change to Feature A's metrics, the verdict kernel, or the phase machine.

**Tech Stack:** Python 3 (pytest, xml.etree.ElementTree), click CLI, Pydantic v2 web models, FastAPI read model, React + TypeScript + Tailwind (vitest), Docker-in-container, JaCoCo 0.8.12.

**Spec:** `docs/superpowers/specs/2026-06-15-submodule-coverage-design.md`

**Branch:** `feature/submodule-coverage` (exists, spec committed).

**Conventions (read before starting):**
- Backend tests: `.venv/bin/python -m pytest` (never bare `pytest`). Full gate: `.venv/bin/python -m pytest tests/ -q --ignore=tests/test_web_task_runner.py`.
- Frontend: `cd webui && npx vitest run`; typecheck `cd webui && npx tsc -p tsconfig.app.json --noEmit`.
- Stage only each task's files by exact path. NEVER `git add -A`. Leave `src/sag/web/static/` and unrelated dirty files unstaged. `docs/` is gitignored; force-add the plan/spec (expected).
- MAINTAINER-PROTECTION: never modify `webui/src/pages/Dashboard.tsx`; never modify `src/sag/web/static/`; never run `npm run build` (operator's final task).
- No `Co-Authored-By` trailer.
- Coverage is best-effort and post-verdict: nothing in this feature may change the setup verdict, exit code, or edit project source files.

---

## Coverage Data Contract (single source of truth)

The runner produces a **coverage map** `{reactor_path: cov}` where each `cov` is:

```python
{
  "line_covered": int, "line_total": int, "line_rate": float|None,     # 0..100, 1 decimal
  "branch_covered": int, "branch_total": int, "branch_rate": float|None,
  "coverage_source": "jacoco-existing" | "jacoco-injected",
}
```

`merge_coverage_into_metrics` adds these keys to each matching module in `module_metrics.json` (`line_covered/line_total/line_rate/branch_covered/branch_total/branch_rate/coverage_source`; absent → not set / null) and sets a lines-weighted rollup on `module_summary` (`line_covered/line_total/line_rate/branch_covered/branch_total/branch_rate/coverage_source`).

Web camelCase: `lineCovered/lineTotal/lineRate/branchCovered/branchTotal/branchRate/coverageSource`. `line_rate`/`branch_rate` are `null` when total is 0; modules with no coverage keep all fields null → UI shows "— not measured".

---

## File Structure

- Create `src/sag/coverage/__init__.py` — package marker.
- Create `src/sag/coverage/jacoco_parser.py` — pure `parse_jacoco_xml(content) -> dict` (line/branch counters → rates).
- Create `src/sag/coverage/merge.py` — pure `merge_coverage_into_metrics(metrics, coverage_map) -> metrics` (+ lines-weighted rollup).
- Create `src/sag/coverage/runner.py` — `run_coverage(orchestrator, project_dir) -> dict` (detect, reuse-vs-inject, locate+parse) and `apply_coverage(orchestrator) -> bool` (read module_metrics.json, merge, write back). Coverage code lives together here rather than in `physical_validator` (cohesion; the parser/runner are coverage-specific and independently testable).
- Modify `src/sag/main.py` — `--coverage` flag on `project` + `run`; `_run_coverage_pass(orchestrator)` helper called post-setup.
- Modify `src/sag/web/project_cli.py` — `coverage` field + `--coverage` arg.
- Modify `src/sag/web/launch_service.py` — request row `coverage` field + thread into `ProjectCliCommand`.
- Modify `src/sag/web/models.py` — `ModuleSummary`/`ModuleRollup` coverage fields.
- Modify `src/sag/web/demo_data.py` — coverage on demo modules + rollup.
- Modify `webui/src/api/types.ts` — coverage fields on `ModuleSummary`/`ModuleRollup`.
- Modify `webui/src/components/session/ModuleTable.tsx` — Rate column + Coverage column (stacked L/B bars + thresholds).
- Modify `webui/src/components/session/TestDetailPage.tsx` — populate the Coverage tile.
- Modify `webui/src/components/launch/launchRows.ts` + `LaunchSetupsDialog.tsx` — Coverage checkbox column + check-all (and Record check-all).

The web read model needs **no change**: `_modules_payload_from_metrics`/`_module_rollup_from_metrics` pass the artifact dicts through, and `_session_detail` does `ModuleSummary.model_validate(...)`/`ModuleRollup.model_validate(...)`, which pick up the new aliased fields automatically (verified in Task 6's test).

---

## Task 1: Pure JaCoCo XML parser

**Files:**
- Create: `src/sag/coverage/__init__.py` (empty), `src/sag/coverage/jacoco_parser.py`
- Test: `tests/test_jacoco_parser.py`

JaCoCo's per-module `jacoco.xml` ends with report-level `<counter>` elements that are **direct children** of the root `<report>` (nested package/class counters have the same `type` and must be ignored). `ElementTree.findall("counter")` returns only direct children.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jacoco_parser.py
from sag.coverage.jacoco_parser import parse_jacoco_xml

REPORT = """<?xml version="1.0" encoding="UTF-8"?>
<report name="core">
  <package name="org/x">
    <counter type="LINE" missed="1" covered="9"/>
    <counter type="BRANCH" missed="5" covered="5"/>
  </package>
  <counter type="INSTRUCTION" missed="40" covered="160"/>
  <counter type="BRANCH" missed="30" covered="70"/>
  <counter type="LINE" missed="20" covered="80"/>
  <counter type="METHOD" missed="2" covered="18"/>
</report>"""


def test_parses_report_level_line_and_branch():
    cov = parse_jacoco_xml(REPORT)
    # report-level totals, NOT the nested package counters
    assert cov["line_covered"] == 80 and cov["line_total"] == 100
    assert cov["line_rate"] == 80.0
    assert cov["branch_covered"] == 70 and cov["branch_total"] == 100
    assert cov["branch_rate"] == 70.0


def test_zero_total_yields_null_rate():
    xml = '<report name="x"><counter type="LINE" missed="0" covered="0"/></report>'
    cov = parse_jacoco_xml(xml)
    assert cov["line_covered"] == 0 and cov["line_total"] == 0
    assert cov["line_rate"] is None
    assert cov["branch_total"] == 0 and cov["branch_rate"] is None


def test_malformed_xml_returns_empty():
    assert parse_jacoco_xml("not xml <<<") == {}
    assert parse_jacoco_xml("") == {}
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_jacoco_parser.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'sag.coverage'`.

- [ ] **Step 3: Implement**

Create `src/sag/coverage/__init__.py` (empty). Create `src/sag/coverage/jacoco_parser.py`:

```python
"""Pure parser for JaCoCo jacoco.xml -> line/branch coverage totals.

Reads the REPORT-LEVEL counters (direct children of <report>); nested
package/class counters share the same type and must be ignored.
"""

from typing import Any, Dict
from xml.etree import ElementTree as ET


def _rate(covered: int, total: int):
    return round(100.0 * covered / total, 1) if total > 0 else None


def parse_jacoco_xml(content: str) -> Dict[str, Any]:
    """Parse a jacoco.xml string into line/branch totals. {} on failure."""
    if not content or not content.strip():
        return {}
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return {}
    if root.tag != "report":
        # Some writers wrap differently; find the first <report>.
        found = root.find(".//report")
        if found is None:
            return {}
        root = found

    totals = {"LINE": (0, 0), "BRANCH": (0, 0)}
    for counter in root.findall("counter"):  # direct children only
        ctype = counter.get("type")
        if ctype in totals:
            missed = int(counter.get("missed", "0"))
            covered = int(counter.get("covered", "0"))
            totals[ctype] = (covered, covered + missed)

    line_c, line_t = totals["LINE"]
    branch_c, branch_t = totals["BRANCH"]
    return {
        "line_covered": line_c, "line_total": line_t, "line_rate": _rate(line_c, line_t),
        "branch_covered": branch_c, "branch_total": branch_t,
        "branch_rate": _rate(branch_c, branch_t),
    }
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_jacoco_parser.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/sag/coverage/__init__.py src/sag/coverage/jacoco_parser.py tests/test_jacoco_parser.py
git commit -m "Add pure JaCoCo xml parser (report-level line/branch counters)"
```

---

## Task 2: Merge coverage into module_metrics (lines-weighted rollup)

**Files:**
- Create: `src/sag/coverage/merge.py`
- Test: `tests/test_coverage_merge.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_coverage_merge.py
from sag.coverage.merge import merge_coverage_into_metrics


def _metrics():
    return {
        "version": 1,
        "module_summary": {"modules_total": 3, "build_systems": ["gradle"]},
        "modules": [
            {"name": "core", "path": "core", "build_status": "success"},
            {"name": "io", "path": "io", "build_status": "success"},
            {"name": "examples", "path": "examples", "build_status": "unknown"},
        ],
    }


def test_merges_by_path_and_weights_rollup():
    cov_map = {
        "core": {"line_covered": 80, "line_total": 100, "line_rate": 80.0,
                 "branch_covered": 70, "branch_total": 100, "branch_rate": 70.0,
                 "coverage_source": "jacoco-injected"},
        "io": {"line_covered": 30, "line_total": 100, "line_rate": 30.0,
               "branch_covered": 10, "branch_total": 50, "branch_rate": 20.0,
               "coverage_source": "jacoco-injected"},
    }
    out = merge_coverage_into_metrics(_metrics(), cov_map)
    by_path = {m["path"]: m for m in out["modules"]}
    assert by_path["core"]["line_rate"] == 80.0
    assert by_path["io"]["branch_covered"] == 10
    # examples had no coverage -> fields stay absent/None
    assert by_path["examples"].get("line_rate") is None
    s = out["module_summary"]
    # lines-weighted: (80+30)/(100+100) = 55.0 ; branch (70+10)/(100+50)=53.3
    assert s["line_covered"] == 110 and s["line_total"] == 200 and s["line_rate"] == 55.0
    assert s["branch_total"] == 150 and s["branch_rate"] == 53.3
    assert s["coverage_source"] == "jacoco-injected"


def test_no_coverage_leaves_rollup_null():
    out = merge_coverage_into_metrics(_metrics(), {})
    s = out["module_summary"]
    assert s["line_rate"] is None and s["branch_rate"] is None
    assert s["coverage_source"] is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_coverage_merge.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'sag.coverage.merge'`.

- [ ] **Step 3: Implement `src/sag/coverage/merge.py`**

```python
"""Merge a coverage map into an existing module_metrics dict (pure)."""

from typing import Any, Dict

_COV_FIELDS = (
    "line_covered", "line_total", "line_rate",
    "branch_covered", "branch_total", "branch_rate", "coverage_source",
)


def _rate(covered: int, total: int):
    return round(100.0 * covered / total, 1) if total > 0 else None


def merge_coverage_into_metrics(
    metrics: Dict[str, Any], coverage_map: Dict[str, Dict[str, Any]]
) -> Dict[str, Any]:
    """Set per-module coverage fields by reactor path and recompute the
    lines-weighted rollup. Modules absent from coverage_map keep null coverage.
    Returns the same dict (mutated) for convenience."""
    coverage_map = coverage_map or {}
    modules = metrics.get("modules") or []

    line_c = line_t = branch_c = branch_t = 0
    sources: set = set()
    for module in modules:
        cov = coverage_map.get(module.get("path"))
        if not cov:
            continue
        for field in _COV_FIELDS:
            module[field] = cov.get(field)
        line_c += int(cov.get("line_covered") or 0)
        line_t += int(cov.get("line_total") or 0)
        branch_c += int(cov.get("branch_covered") or 0)
        branch_t += int(cov.get("branch_total") or 0)
        if cov.get("coverage_source"):
            sources.add(cov["coverage_source"])

    summary = metrics.setdefault("module_summary", {})
    has_any = bool(sources)
    summary["line_covered"] = line_c if has_any else None
    summary["line_total"] = line_t if has_any else None
    summary["line_rate"] = _rate(line_c, line_t) if has_any else None
    summary["branch_covered"] = branch_c if has_any else None
    summary["branch_total"] = branch_t if has_any else None
    summary["branch_rate"] = _rate(branch_c, branch_t) if has_any else None
    # If any module needed injection, the aggregate provenance is "injected".
    summary["coverage_source"] = (
        "jacoco-injected" if "jacoco-injected" in sources
        else "jacoco-existing" if "jacoco-existing" in sources
        else None
    )
    return metrics
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_coverage_merge.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/sag/coverage/merge.py tests/test_coverage_merge.py
git commit -m "Add coverage merge into module_metrics with lines-weighted rollup"
```

---

## Task 3: Deterministic coverage runner

**Files:**
- Create: `src/sag/coverage/runner.py`
- Test: `tests/test_coverage_runner.py`

The runner reuses existing `jacoco.xml` (parse only) or injects JaCoCo and re-runs, then parses per-module reports into a coverage map. `apply_coverage` reads `module_metrics.json` from the container, merges, and writes it back. All container IO goes through the orchestrator (`execute_command` / a file read). Injection NEVER edits project files.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_coverage_runner.py
import json

from sag.coverage.runner import run_coverage, apply_coverage, JACOCO_VERSION


class FakeOrch:
    def __init__(self, files, listings=None):
        self.files = files            # path -> content (cat)
        self.listings = listings or {}  # substring -> find output
        self.commands = []

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        if command.startswith("cat "):
            path = command[4:].strip().strip("'")
            return {"success": path in self.files, "exit_code": 0 if path in self.files else 1,
                    "output": self.files.get(path, "")}
        for needle, out in self.listings.items():
            if needle in command:
                return {"success": True, "exit_code": 0, "output": out}
        return {"success": True, "exit_code": 0, "output": ""}


REPORT = ('<report name="m"><counter type="LINE" missed="20" covered="80"/>'
          '<counter type="BRANCH" missed="30" covered="70"/></report>')


def test_reuses_existing_reports_without_running_build():
    # An existing jacoco.xml under a module -> parse, no test re-run.
    orch = FakeOrch(
        files={"/w/p/core/build/reports/jacoco/test/jacocoTestReport.xml": REPORT},
        listings={"-name 'jacoco*.xml'": "/w/p/core/build/reports/jacoco/test/jacocoTestReport.xml"},
    )
    cov = run_coverage(orch, "/w/p", build_system="gradle")
    assert cov["core"]["line_rate"] == 80.0
    assert cov["core"]["coverage_source"] == "jacoco-existing"
    # no test/build command was issued (reuse path)
    assert not any("jacocoTestReport" in c and "gradle" in c for c in orch.commands)


def test_injects_and_runs_when_no_existing_report_maven():
    # First listing (existing) empty -> inject+run, then second listing finds the produced report.
    calls = {"n": 0}

    class Orch(FakeOrch):
        def execute_command(self, command, **kwargs):
            if "-name 'jacoco.xml'" in command or "jacoco*.xml" in command:
                calls["n"] += 1
                # empty on the pre-check, populated after the run
                if calls["n"] == 1:
                    return {"success": True, "exit_code": 0, "output": ""}
                return {"success": True, "exit_code": 0,
                        "output": "/w/p/core/target/site/jacoco/jacoco.xml"}
            return super().execute_command(command, **kwargs)

    orch = Orch(files={"/w/p/core/target/site/jacoco/jacoco.xml": REPORT})
    cov = run_coverage(orch, "/w/p", build_system="maven")
    assert cov["core"]["coverage_source"] == "jacoco-injected"
    assert any(f"jacoco-maven-plugin:{JACOCO_VERSION}:prepare-agent" in c for c in orch.commands)
    # never edits project files
    assert not any("pom.xml" in c and (">" in c or "sed" in c) for c in orch.commands)


def test_apply_coverage_merges_into_container_metrics():
    metrics = {"version": 1, "module_summary": {"modules_total": 1},
               "modules": [{"name": "core", "path": "core", "build_status": "success"}]}
    written = {}

    class Orch(FakeOrch):
        def execute_command(self, command, **kwargs):
            if command.startswith("cat ") and "module_metrics.json" in command:
                return {"success": True, "exit_code": 0, "output": json.dumps(metrics)}
            if "module_metrics.json" in command and "cat >" in command:
                written["payload"] = command
                return {"success": True, "exit_code": 0, "output": ""}
            if "jacoco" in command and "xml" in command:
                return {"success": True, "exit_code": 0,
                        "output": "/w/p/core/build/reports/jacoco/test/jacocoTestReport.xml"}
            if command.startswith("cat "):
                return {"success": True, "exit_code": 0, "output": REPORT}
            return {"success": True, "exit_code": 0, "output": ""}

    orch = Orch(files={})
    ok = apply_coverage(orch, "/w/p", build_system="gradle")
    assert ok is True
    assert "line_rate" in written["payload"]  # merged coverage written back
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_coverage_runner.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'sag.coverage.runner'`.

- [ ] **Step 3: Implement `src/sag/coverage/runner.py`**

```python
"""Deterministic, isolated, best-effort coverage runner.

Reuses existing jacoco.xml when present, else injects JaCoCo WITHOUT editing
project files (Maven CLI plugin goals / Gradle --init-script) and re-runs the
test suite, then parses per-module reports into a coverage map and merges it
into module_metrics.json. Any failure leaves coverage absent; it never raises
into the caller (the setup is already finished)."""

import json
import shlex
from typing import Any, Dict, Optional

from loguru import logger

from sag.coverage.jacoco_parser import parse_jacoco_xml
from sag.coverage.merge import merge_coverage_into_metrics
from sag.tools.module_metrics import MODULE_METRICS_PATH

JACOCO_VERSION = "0.8.12"
COVERAGE_TIMEOUT_SEC = 1800

# Gradle init script: apply jacoco to all projects + force an XML report. No
# build.gradle edits; passed via --init-script only.
_GRADLE_INIT = """allprojects { p ->
    p.plugins.withId('java') { p.apply plugin: 'jacoco' }
    p.tasks.withType(JacocoReport).configureEach { reports.xml.required = true }
}
"""


def _find_reports(orchestrator: Any, project_dir: str, build_system: str) -> list:
    if build_system == "gradle":
        cmd = f"find {project_dir} -path '*/build/reports/jacoco/*' -name 'jacoco*.xml' 2>/dev/null"
    else:
        cmd = f"find {project_dir} -path '*/target/site/jacoco/*' -name 'jacoco.xml' 2>/dev/null"
    res = orchestrator.execute_command(cmd)
    return [l for l in (res.get("output") or "").splitlines() if l.strip().endswith(".xml")]


def _module_path(project_dir: str, xml_path: str, build_system: str) -> str:
    # .../<module>/target/site/jacoco/jacoco.xml  or  .../<module>/build/reports/jacoco/.../*.xml
    marker = "/build/" if build_system == "gradle" else "/target/"
    head = xml_path.split(marker)[0]
    rel = head[len(project_dir):].strip("/")
    return rel or "."


def _inject_and_run(orchestrator: Any, project_dir: str, build_system: str) -> None:
    if build_system == "gradle":
        init_path = f"{project_dir}/.setup_agent_jacoco.init.gradle"
        delim = "SAG_JACOCO_INIT"
        orchestrator.execute_command(
            f"cat > {init_path} <<'{delim}'\n{_GRADLE_INIT}\n{delim}"
        )
        cmd = (
            f"cd {project_dir} && (./gradlew --no-daemon --continue "
            f"--init-script {init_path} test jacocoTestReport "
            f"|| gradle --no-daemon --continue --init-script {init_path} test jacocoTestReport)"
        )
    else:
        plugin = f"org.jacoco:jacoco-maven-plugin:{JACOCO_VERSION}"
        cmd = (
            f"cd {project_dir} && mvn -B {plugin}:prepare-agent test {plugin}:report "
            f"-Dmaven.test.failure.ignore=true"
        )
    orchestrator.execute_command(cmd, timeout=COVERAGE_TIMEOUT_SEC)


def run_coverage(
    orchestrator: Any, project_dir: str, build_system: Optional[str] = None
) -> Dict[str, Dict[str, Any]]:
    """Produce a {reactor_path: coverage} map. Best-effort: {} on any failure."""
    try:
        if build_system is None:
            return {}
        existing = _find_reports(orchestrator, project_dir, build_system)
        source = "jacoco-existing"
        if not existing:
            _inject_and_run(orchestrator, project_dir, build_system)
            existing = _find_reports(orchestrator, project_dir, build_system)
            source = "jacoco-injected"

        coverage: Dict[str, Dict[str, Any]] = {}
        for xml_path in existing:
            cat = orchestrator.execute_command(f"cat '{xml_path}'")
            cov = parse_jacoco_xml(cat.get("output") or "")
            if not cov:
                continue
            path = _module_path(project_dir, xml_path, build_system)
            cov["coverage_source"] = source
            # If multiple reports map to one module, keep the larger line_total.
            prev = coverage.get(path)
            if prev is None or (cov.get("line_total") or 0) >= (prev.get("line_total") or 0):
                coverage[path] = cov
        return coverage
    except Exception as exc:  # never propagate; setup already finished
        logger.warning(f"Coverage run failed (best-effort, ignored): {exc}")
        return {}


def apply_coverage(orchestrator: Any, project_dir: str, build_system: Optional[str] = None) -> bool:
    """Run coverage and merge it into module_metrics.json in the container.
    Returns True when coverage was written, False otherwise (best-effort)."""
    coverage = run_coverage(orchestrator, project_dir, build_system)
    if not coverage:
        return False
    try:
        cat = orchestrator.execute_command(f"cat {MODULE_METRICS_PATH}")
        if not cat.get("success") or not (cat.get("output") or "").strip():
            return False
        metrics = json.loads(cat["output"])
        merged = merge_coverage_into_metrics(metrics, coverage)
        payload = json.dumps(merged, indent=2)
        delim = "SAG_MODULE_METRICS_EOF"
        orchestrator.execute_command(
            f"cat > {MODULE_METRICS_PATH} <<'{delim}'\n{payload}\n{delim}"
        )
        return True
    except Exception as exc:
        logger.warning(f"Coverage merge/write failed (best-effort, ignored): {exc}")
        return False
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_coverage_runner.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/sag/coverage/runner.py tests/test_coverage_runner.py
git commit -m "Add deterministic coverage runner (reuse-then-inject, merge)"
```

---

## Task 4: `--coverage` CLI handle on project + run

**Files:**
- Modify: `src/sag/main.py` (add `--coverage` option + param to `project` and `run`; add `_run_coverage_pass`; call it after setup, next to the `if record:` block)
- Test: `tests/test_coverage_cli.py`

The coverage pass needs the build system + project dir. Detect the build system via `PhysicalValidator(orchestrator)._detect_build_system(project_dir)`. The project dir is `/workspace/<project_name>` (the clone dir). Use the existing orchestrator.

- [ ] **Step 1: Write the failing test** (unit-test the helper, not the whole CLI)

```python
# tests/test_coverage_cli.py
from sag.main import _run_coverage_pass


def test_run_coverage_pass_invokes_apply(monkeypatch):
    calls = {}

    class Orch:
        def execute_command(self, command, **kwargs):
            if "project_meta.json" in command:
                return {"success": True, "exit_code": 0,
                        "output": '{"project_name": "caffeine"}'}
            return {"success": True, "exit_code": 0, "output": ""}

    import sag.main as m

    monkeypatch.setattr(m, "_detect_coverage_build_system",
                        lambda orch, project_dir: "gradle")

    def fake_apply(orch, project_dir, build_system=None):
        calls["project_dir"] = project_dir
        calls["build_system"] = build_system
        return True

    monkeypatch.setattr(m, "apply_coverage", fake_apply)
    ok = _run_coverage_pass(Orch(), "caffeine")
    assert ok is True
    assert calls["project_dir"] == "/workspace/caffeine"
    assert calls["build_system"] == "gradle"


def test_run_coverage_pass_best_effort_on_error(monkeypatch):
    import sag.main as m
    monkeypatch.setattr(m, "_detect_coverage_build_system",
                        lambda orch, project_dir: "maven")
    monkeypatch.setattr(m, "apply_coverage",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    class Orch:
        def execute_command(self, command, **kwargs):
            return {"success": True, "exit_code": 0, "output": ""}

    # must not raise
    assert _run_coverage_pass(Orch(), "demo") is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_coverage_cli.py -q`
Expected: FAIL with `ImportError: cannot import name '_run_coverage_pass'`.

- [ ] **Step 3: Implement in `src/sag/main.py`.** Add imports near the top:

```python
from sag.coverage.runner import apply_coverage
```

Add the helpers (place them near `_save_setup_artifacts`):

```python
def _detect_coverage_build_system(orchestrator, project_dir: str):
    """Detect maven/gradle physically for the coverage pass (or None)."""
    try:
        from sag.agent.physical_validator import PhysicalValidator

        bs = PhysicalValidator(docker_orchestrator=orchestrator)._detect_build_system(project_dir)
        return bs if bs in ("maven", "gradle") else None
    except Exception as exc:
        logger.debug(f"coverage build-system detect failed: {exc}")
        return None


def _run_coverage_pass(orchestrator, project_name: str) -> bool:
    """Isolated, best-effort coverage pass AFTER the setup verdict is locked.

    Never raises; never changes the setup result. Warns if the project source
    tree changed (pollution guard)."""
    project_dir = f"/workspace/{project_name}"
    build_system = _detect_coverage_build_system(orchestrator, project_dir)
    if build_system is None:
        logger.info("Coverage: no maven/gradle build detected; skipping.")
        return False
    try:
        wrote = apply_coverage(orchestrator, project_dir, build_system)
    except Exception as exc:  # defensive; apply_coverage is already best-effort
        logger.warning(f"Coverage pass failed (best-effort, ignored): {exc}")
        return False
    # Pollution guard (warn-only): tracked source files must be unchanged.
    try:
        dirty = orchestrator.execute_command(
            f"cd {project_dir} && git status --porcelain 2>/dev/null "
            f"| grep -vE 'target/|build/|\\.setup_agent' | head -5"
        )
        if (dirty.get("output") or "").strip():
            logger.warning(f"Coverage pass left source-tree changes:\n{dirty['output']}")
    except Exception:
        pass
    return wrote
```

Add the option + param to **both** `project` and `run` (mirror `--record`). For `project`, after the existing `--record` option add:

```python
@click.option(
    "--coverage", is_flag=True, help="Run an isolated JaCoCo coverage pass after setup (best-effort)"
)
```

Change the signature `def project(ctx, repo_url, name, goal, record, ui, project_ref):` to include `coverage` (place it after `record`): `def project(ctx, repo_url, name, goal, record, coverage, ui, project_ref):`. After the `if record: _save_setup_artifacts(orchestrator, project_name)` block add:

```python
        if coverage:
            _run_coverage_pass(orchestrator, project_name)
```

Do the same for `run`: add the `--coverage` option, add `coverage` to its signature after `record`, and after its `if record: _save_setup_artifacts(orchestrator, actual_project_name)` add `if coverage: _run_coverage_pass(orchestrator, actual_project_name)`.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_coverage_cli.py -q` (PASS) and `.venv/bin/sag project --help 2>&1 | grep -- --coverage` (shows the flag).

- [ ] **Step 5: Commit**

```bash
git add src/sag/main.py tests/test_coverage_cli.py
git commit -m "Add --coverage handle to sag project/run (post-verdict, best-effort)"
```

---

## Task 5: Thread `coverage` through the web launch flow

**Files:**
- Modify: `src/sag/web/project_cli.py` (`coverage` field + `--coverage` arg)
- Modify: `src/sag/web/launch_service.py` (request row `coverage` field + `ProjectCliCommand(coverage=...)` at the two call sites ~169, ~186)
- Test: `tests/test_project_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_project_cli.py
from sag.web.project_cli import ProjectCliCommand


def test_coverage_flag_appended_when_set():
    args = ProjectCliCommand(repo_url="https://x/y.git", record=True, coverage=True).project_args()
    assert "--coverage" in args and "--record" in args


def test_coverage_flag_absent_when_unset():
    args = ProjectCliCommand(repo_url="https://x/y.git").project_args()
    assert "--coverage" not in args
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_project_cli.py -q`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'coverage'`.

- [ ] **Step 3: Implement.** In `project_cli.py`, add `coverage: bool = False` after `record`, and in `project_args` after the `--record` block:

```python
        if self.coverage:
            args.append("--coverage")
```

In `launch_service.py`: the request row dataclass/model has `record: bool = False` (~line 32) — add `coverage: bool = False` right after it. At both `ProjectCliCommand(...)` constructions (~169 and ~186), add `coverage=row.coverage,` after `record=row.record,`.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_project_cli.py tests/ -q -k "launch or project_cli" 2>&1 | tail -5`
Expected: PASS (new tests + no launch regression).

- [ ] **Step 5: Commit**

```bash
git add src/sag/web/project_cli.py src/sag/web/launch_service.py tests/test_project_cli.py
git commit -m "Thread coverage handle through the web launch flow"
```

---

## Task 6: Coverage fields on the web models

**Files:**
- Modify: `src/sag/web/models.py` (`ModuleSummary` + `ModuleRollup`)
- Test: `tests/test_web_coverage_models.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_coverage_models.py
from sag.web.models import ModuleSummary, ModuleRollup


def test_module_summary_coverage_camelcase():
    m = ModuleSummary.model_validate({
        "name": "core", "path": "core",
        "line_covered": 80, "line_total": 100, "line_rate": 80.0,
        "branch_covered": 70, "branch_total": 100, "branch_rate": 70.0,
        "coverage_source": "jacoco-injected",
    })
    d = m.model_dump(mode="json", by_alias=True)
    assert d["lineRate"] == 80.0 and d["branchCovered"] == 70
    assert d["coverageSource"] == "jacoco-injected"


def test_rollup_coverage_defaults_null():
    r = ModuleRollup.model_validate({"modules_total": 2})
    d = r.model_dump(mode="json", by_alias=True)
    assert d["lineRate"] is None and d["branchRate"] is None and d["coverageSource"] is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_web_coverage_models.py -q`
Expected: FAIL (`lineRate` KeyError).

- [ ] **Step 3: Implement.** Add to `ModuleSummary` (after `evidence_refs`):

```python
    line_covered: int | None = Field(
        default=None, validation_alias=AliasChoices("line_covered", "lineCovered"),
        serialization_alias="lineCovered")
    line_total: int | None = Field(
        default=None, validation_alias=AliasChoices("line_total", "lineTotal"),
        serialization_alias="lineTotal")
    line_rate: float | None = Field(
        default=None, validation_alias=AliasChoices("line_rate", "lineRate"),
        serialization_alias="lineRate")
    branch_covered: int | None = Field(
        default=None, validation_alias=AliasChoices("branch_covered", "branchCovered"),
        serialization_alias="branchCovered")
    branch_total: int | None = Field(
        default=None, validation_alias=AliasChoices("branch_total", "branchTotal"),
        serialization_alias="branchTotal")
    branch_rate: float | None = Field(
        default=None, validation_alias=AliasChoices("branch_rate", "branchRate"),
        serialization_alias="branchRate")
    coverage_source: str | None = Field(
        default=None, validation_alias=AliasChoices("coverage_source", "coverageSource"),
        serialization_alias="coverageSource")
```

Add the same seven fields to `ModuleRollup`.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_web_coverage_models.py tests/test_web_module_models.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sag/web/models.py tests/test_web_coverage_models.py
git commit -m "Add coverage fields to ModuleSummary/ModuleRollup web models"
```

---

## Task 7: Demo coverage data + frontend types

**Files:**
- Modify: `src/sag/web/demo_data.py` (coverage on demo modules + rollup)
- Modify: `webui/src/api/types.ts` (coverage fields)
- Test: `tests/test_web_demo_data.py` (extend)

- [ ] **Step 1: Write the failing test** (extend the existing demo modules test)

```python
# add to tests/test_web_demo_data.py
def test_demo_modules_carry_coverage():
    detail = get_demo_session("CC-3")
    by_path = {m.path: m for m in detail.modules}
    assert by_path["core"].line_rate is not None
    assert by_path["core"].branch_rate is not None
    assert detail.module_summary.line_rate is not None
```

(If the demo accessor differs, mirror the existing `test_demo_session_detail_has_modules` in the same file.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_web_demo_data.py::test_demo_modules_carry_coverage -q`
Expected: FAIL (line_rate is None).

- [ ] **Step 3: Implement.** In `demo_data.py` `_commons_modules()`, add coverage kwargs to the two built modules (`core`, `help`) and leave the failed/unmeasured ones null. For `commons-cli-core`:

```python
            line_covered=2040, line_total=2480, line_rate=82.3,
            branch_covered=520, branch_total=720, branch_rate=72.2,
            coverage_source="jacoco-injected",
```

For `commons-cli-help`:

```python
            line_covered=410, line_total=520, line_rate=78.8,
            branch_covered=90, branch_total=140, branch_rate=64.3,
            coverage_source="jacoco-injected",
```

In `_commons_module_summary()`, add a lines-weighted rollup:

```python
        line_covered=2450, line_total=3000, line_rate=81.7,
        branch_covered=610, branch_total=860, branch_rate=70.9,
        coverage_source="jacoco-injected",
```

- [ ] **Step 4: Add the TS fields** to `webui/src/api/types.ts` — extend `ModuleSummary` and `ModuleRollup`:

```typescript
  lineCovered?: number | null
  lineTotal?: number | null
  lineRate?: number | null
  branchCovered?: number | null
  branchTotal?: number | null
  branchRate?: number | null
  coverageSource?: string | null
```

- [ ] **Step 5: Verify**

Run: `.venv/bin/python -m pytest tests/test_web_demo_data.py -q` (PASS) and `cd webui && npx tsc -p tsconfig.app.json --noEmit` (clean).

- [ ] **Step 6: Commit**

```bash
git add src/sag/web/demo_data.py webui/src/api/types.ts tests/test_web_demo_data.py
git commit -m "Add coverage demo data and frontend coverage types"
```

---

## Task 8: Coverage column + Rate column in ModuleTable

**Files:**
- Modify: `webui/src/components/session/ModuleTable.tsx` (test variant: add Rate + Coverage columns)
- Test: `webui/src/components/session/ModuleTable.test.tsx` (extend)

The approved layout for the Test Details table is **Module | Pass | Fail | Skip | Rate | Coverage | Failing methods** — note there is **no Build column** in the test variant (it stays only in the build variant). Today `ModuleTable` renders a shared Build column for both variants, so this task makes the Build column **build-variant-only**, then adds a **Rate** column (runner pass rate = `pass / (pass + fail)` as a %, "—" when no tests) and a **Coverage** column (stacked line/branch mini-bars, ≥80 green / 50–79 amber / <50 red, "— not measured" when `lineRate == null && branchRate == null`).

- [ ] **Step 1: Write the failing test**

```tsx
// add to webui/src/components/session/ModuleTable.test.tsx
it("shows a rate column and stacked coverage bars (test variant)", () => {
  render(<ModuleTable variant="test" modules={[
    { name: "core", path: "core", buildStatus: "success", buildSource: "reactor",
      testSource: "runner_xml", testsPassed: 998, testsFailed: 2, testsSkipped: 0,
      failingNames: [], failingCount: 0,
      lineRate: 82, branchRate: 71 },
    { name: "examples", path: "examples", buildStatus: "unknown", buildSource: "none",
      testSource: "none", failingNames: [], failingCount: 0 },
  ]} />)
  expect(screen.getByText("99.8%")).toBeInTheDocument()   // 998/(998+2)
  expect(screen.getByText("82%")).toBeInTheDocument()      // line coverage
  expect(screen.getByText("71%")).toBeInTheDocument()      // branch coverage
  expect(screen.getByText(/not measured/i)).toBeInTheDocument()  // examples
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd webui && npx vitest run src/components/session/ModuleTable.test.tsx`
Expected: FAIL (no "99.8%"/"82%").

- [ ] **Step 3: Implement.** In `ModuleTable.tsx` add helpers (near `num`):

```tsx
function passRate(p?: number | null, f?: number | null): string {
  const pass = p ?? 0, fail = f ?? 0, denom = pass + fail
  return denom > 0 ? `${((pass / denom) * 100).toFixed(1).replace(/\.0$/, "")}%` : "—"
}
function covColor(rate: number): string {
  return rate >= 80 ? "#22c55e" : rate >= 50 ? "#f59e0b" : "#ef4444"
}
function covTextClass(rate: number): string {
  return rate >= 80 ? "text-emerald-700" : rate >= 50 ? "text-amber-600" : "text-red-600"
}
function CoverageBar({ label, rate }: { label: string; rate: number }) {
  return (
    <div className="flex items-center gap-2 font-mono text-[11px]">
      <span className="w-2 text-slate-500">{label}</span>
      <span className="inline-block h-[7px] w-24 overflow-hidden rounded-full bg-slate-200">
        <span className="block h-full" style={{ width: `${Math.max(0, Math.min(100, rate))}%`, background: covColor(rate) }} />
      </span>
      <span className={cn("w-9 font-semibold", covTextClass(rate))}>{Math.round(rate)}%</span>
    </div>
  )
}
```

**Make the shared Build column build-variant-only.** The header currently renders `<th>Module</th><th>Build</th>` before the variant conditional, and each row renders a Module cell then a Build status `<td>` before the variant cells. Wrap the Build header and the Build status cell in `{variant === "build" ? (...) : null}` so the test variant has no Build column.

In the test-variant header branch (the `Pass/Fail/Skip/Failing methods` `<th>`s), make the columns: `Pass`, `Fail`, `Skip`, then `Rate`, `Coverage`, `Failing methods`:

```tsx
              <th className="px-2 py-2 text-left">Pass</th>
              <th className="px-2 py-2 text-left">Fail</th>
              <th className="px-2 py-2 text-left">Skip</th>
              <th className="px-2 py-2 text-left">Rate</th>
              <th className="px-2 py-2 text-left">Coverage</th>
              <th className="px-2 py-2 text-left">Failing methods</th>
```

In the test-variant row branch, after the Skip cell add the Rate and Coverage cells (before the Failing-methods cell):

```tsx
                    <td className="px-2 py-2">{passRate(m.testsPassed, m.testsFailed)}</td>
                    <td className="px-2 py-2" style={{ minWidth: 150 }}>
                      {m.lineRate == null && m.branchRate == null ? (
                        <span className="text-slate-400">— not measured</span>
                      ) : (
                        <div className="space-y-0.5">
                          {m.lineRate != null ? <CoverageBar label="L" rate={m.lineRate} /> : null}
                          {m.branchRate != null ? <CoverageBar label="B" rate={m.branchRate} /> : null}
                        </div>
                      )}
                    </td>
```

Update the expanded-row `colSpan`. With Build removed from the test variant, the test columns are Module, Pass, Fail, Skip, Rate, Coverage, Failing = **7**; the build variant is unchanged at 5. Change `<td colSpan={variant === "build" ? 5 : 6} ...>` to `<td colSpan={variant === "build" ? 5 : 7} ...>`.

If any existing test-variant test asserts a Build status chip (e.g. `getByText("SUCCESS")` while rendering `variant="test"`), update it — the test table no longer shows build status (that lives on Build Details).

- [ ] **Step 4: Run to verify it passes**

Run: `cd webui && npx vitest run src/components/session/ModuleTable.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webui/src/components/session/ModuleTable.tsx webui/src/components/session/ModuleTable.test.tsx
git commit -m "Add Rate + stacked-bar Coverage columns to the test module table"
```

---

## Task 9: Populate the Coverage tile on Test Details

**Files:**
- Modify: `webui/src/components/session/TestDetailPage.tsx` (replace the dashed Feature-B tile with real coverage)
- Test: `webui/src/components/session/TestDetailPage.test.tsx` (extend)

- [ ] **Step 1: Write the failing test**

```tsx
// add to webui/src/components/session/TestDetailPage.test.tsx
it("shows real coverage in the tile when present", () => {
  render(<TestDetailPage onBack={() => {}} detail={{
    test: { state: "success", pass: 100, fail: 0, skip: 0, total: 100, passRate: 100 },
    moduleSummary: { modulesTotal: 2, modulesBuilt: 2, modulesFailed: 0, modulesSkipped: 0,
                     modulesWithTestFailures: 0, buildSystems: ["gradle"], singleModule: false,
                     lineRate: 81, branchRate: 68 },
    modules: [{ name: "core", path: "core", buildStatus: "success", buildSource: "reactor",
                testSource: "runner_xml", lineRate: 81, branchRate: 68 }],
  } as any} />)
  expect(screen.getByText("81%")).toBeInTheDocument()
  expect(screen.getByText(/68% branch/i)).toBeInTheDocument()
})

it("shows coverage unavailable when no coverage data", () => {
  render(<TestDetailPage onBack={() => {}} detail={{
    test: { state: "success", pass: 100, fail: 0, skip: 0, total: 100, passRate: 100 },
    moduleSummary: { modulesTotal: 1, modulesBuilt: 1, modulesFailed: 0, modulesSkipped: 0,
                     modulesWithTestFailures: 0, buildSystems: ["maven"], singleModule: false },
    modules: [{ name: "core", path: "core", buildStatus: "success", buildSource: "reactor",
                testSource: "runner_xml" }],
  } as any} />)
  expect(screen.getByText(/not measured/i)).toBeInTheDocument()
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd webui && npx vitest run src/components/session/TestDetailPage.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement.** In `TestDetailPage.tsx`, replace the dashed Coverage tile (the `<Tile label="Coverage" ... dashed />` line) with a real one driven by `s?.lineRate`/`s?.branchRate`:

```tsx
        {s?.lineRate != null || s?.branchRate != null ? (
          <Tile
            label="Coverage · line"
            tone={s!.lineRate != null && s!.lineRate >= 80 ? "text-emerald-700"
              : s!.lineRate != null && s!.lineRate >= 50 ? "text-amber-600" : "text-red-600"}
            value={<>{s?.lineRate != null ? `${Math.round(s.lineRate)}%` : "—"}
              <span className="block font-mono text-[10px] font-normal text-slate-500">
                {s?.branchRate != null ? `${Math.round(s.branchRate)}% branch` : "branch —"}
                {s?.coverageSource ? " · jacoco" : ""}
              </span></>}
          />
        ) : (
          <Tile label="Coverage" value={<span className="text-slate-400">— not measured</span>} dashed />
        )}
```

(Keep the `Tile` component's existing signature; it already accepts `label`, `value`, `tone`, `dashed`.)

- [ ] **Step 4: Run to verify it passes**

Run: `cd webui && npx vitest run src/components/session/TestDetailPage.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webui/src/components/session/TestDetailPage.tsx webui/src/components/session/TestDetailPage.test.tsx
git commit -m "Populate the Coverage tile on Test Details (line% + branch% + source)"
```

---

## Task 10: Coverage checkbox + check-all in the batch dialog

**Files:**
- Modify: `webui/src/components/launch/launchRows.ts` (`coverage` field + default)
- Modify: `webui/src/components/launch/LaunchSetupsDialog.tsx` (Coverage column header with check-all, per-row checkbox, payload `coverage`; add check-all to the Record header too)
- Test: `webui/src/components/launch/LaunchSetupsDialog.test.tsx` (extend)

- [ ] **Step 1: Write the failing test** (extend the existing dialog test)

```tsx
// add to webui/src/components/launch/LaunchSetupsDialog.test.tsx
it("check-all toggles coverage for every row", () => {
  // render the dialog with >=2 rows (mirror the existing test's setup/imports),
  // then:
  fireEvent.click(screen.getByRole("button", { name: /select all coverage/i }))
  const boxes = screen.getAllByRole("checkbox", { name: /coverage row/i })
  expect(boxes.every((b) => (b as HTMLInputElement).checked)).toBe(true)
})
```

(Mirror the existing test file's render harness/imports; if the dialog needs props, copy them from the neighbouring test.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd webui && npx vitest run src/components/launch/LaunchSetupsDialog.test.tsx`
Expected: FAIL (no "select all coverage" control).

- [ ] **Step 3: Implement.** In `launchRows.ts`: add `coverage: boolean` to the row type and `coverage: false` to the factory default (next to `record`).

In `LaunchSetupsDialog.tsx`:
- The headers array currently is `["Repo URL", "Name", "Version", "Goal", "Record", ""]`. Replace the bare `"Record"` / add `"Coverage"` so the header cells can host check-all controls. Simplest: change that `.map(header => <th>{header}</th>)` row so the Record and Coverage headers render a label + a small check-all button. Replace the header `<tr>` content with explicit `<th>`s: keep Repo URL/Name/Version/Goal, then:

```tsx
                <th>
                  <div className="flex items-center gap-1">Record
                    <button type="button" aria-label="Select all record"
                      className="font-mono text-[10px] text-blue-600 underline"
                      onClick={() => setAllFlag("record")}>all</button>
                  </div>
                </th>
                <th>
                  <div className="flex items-center gap-1">Coverage
                    <button type="button" aria-label="Select all coverage"
                      className="font-mono text-[10px] text-blue-600 underline"
                      onClick={() => setAllFlag("coverage")}>all</button>
                  </div>
                </th>
                <th></th>
```

- Add the `setAllFlag` helper inside the component (uses the existing rows state setter; match the real state variable name — likely `setRows`):

```tsx
  const setAllFlag = (key: "record" | "coverage") => {
    const target = !rows.every((r) => r[key])
    setRows((prev) => prev.map((r) => ({ ...r, [key]: target })))
  }
```

- Add a per-row Coverage checkbox cell next to the existing Record checkbox cell (mirror lines ~343-346):

```tsx
        <input
          aria-label={`Coverage row ${rowLabel}`}
          checked={row.coverage}
          type="checkbox"
          onChange={(event) => onChange({ coverage: event.target.checked })}
        />
```

- In the launch payload object (where `record: row.record` is set, ~line 147), add `coverage: row.coverage,`.

(If the real state variable is not `rows`/`setRows`, adapt `setAllFlag` to the actual names; the goal is "set the flag on every row".)

- [ ] **Step 4: Run to verify it passes**

Run: `cd webui && npx vitest run src/components/launch/LaunchSetupsDialog.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webui/src/components/launch/launchRows.ts webui/src/components/launch/LaunchSetupsDialog.tsx webui/src/components/launch/LaunchSetupsDialog.test.tsx
git commit -m "Add Coverage checkbox + check-all (and Record check-all) to batch dialog"
```

---

## Task 11: Full-suite gate, webui build, live verification (operator)

**Files:** none (verification only).

- [ ] **Step 1: Full backend suite** — `.venv/bin/python -m pytest tests/ -q --ignore=tests/test_web_task_runner.py`. Expected: all pass.

- [ ] **Step 2: Frontend suite + typecheck** — `cd webui && npx vitest run && npx tsc -p tsconfig.app.json --noEmit`. Expected: all pass, clean.

- [ ] **Step 3: Build the webui** — `cd webui && npm run build` (emits to `src/sag/web/static/`; leave uncommitted).

- [ ] **Step 4: Live Maven coverage** — `sag project <maven-multimodule> --record --coverage`. In-container, confirm `module_metrics.json` now carries per-module `line_rate`/`branch_rate` and a rollup; confirm the setup verdict/exit code are unchanged vs a no-`--coverage` run; confirm `git status` in the project dir shows only `target/` changes (no source edits). In `sag ui`, open Test Details → confirm the Coverage column (stacked L/B bars) and the populated Coverage tile; confirm "— not measured" on modules JaCoCo didn't cover.

- [ ] **Step 5: Live Gradle coverage** — `sag project <gradle-multiproject> --coverage`; confirm the `--init-script` path produces per-subproject `jacocoTestReport` XML, coverage merges, and no `build.gradle`/source edits remain.

- [ ] **Step 6: Reuse path** — run `--coverage` on a project that already produces JaCoCo; confirm `coverage_source = "jacoco-existing"` and that no second test run was triggered (check the logs).

- [ ] **Step 7: Demo** — `sag ui --demo`; confirm the Coverage column + tile render with the demo numbers.

- [ ] **Step 8: Clean up** verification containers; report results; merge to main after approval.

---

## Notes for the implementer

- Coverage is strictly best-effort and post-verdict. If anything in the runner/CLI raises, catch it and continue — the setup result must never change. The tests in Tasks 3-4 pin the no-raise contract.
- The runner must never edit project source files. JaCoCo is enabled only via Maven CLI plugin goals and a Gradle `--init-script` written to a `.setup_agent_*` file (build dirs / dotfiles), and outputs land under `target/`/`build/`. The Task-3 test asserts no `pom.xml`/`build.gradle` write commands are issued.
- The read model needs no change: `ModuleSummary.model_validate` / `ModuleRollup.model_validate` pick up the new aliased coverage fields from the artifact dicts that `_modules_payload_from_metrics` / `_module_rollup_from_metrics` already pass through. Task 6's test plus a live run confirm the end-to-end surface.
- Gradle's report path varies (`build/reports/jacoco/test/jacocoTestReport.xml` and others); `_find_reports` globs `*/build/reports/jacoco/*` + `jacoco*.xml` to catch them. If a project nests differently, that module shows "not measured" — acceptable best-effort.
