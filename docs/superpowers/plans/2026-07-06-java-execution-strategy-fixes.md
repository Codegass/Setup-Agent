# Java Execution-Strategy Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Guarantee that SAG provisions the detected JDK before JVM builds, installs multi-module reactors at the root, and tests at reactor scope by default — with two new verdict conflicts (`jdk_mismatch`, `reactor_scope_narrowed`) keeping the verdict honest when execution falls short.

**Architecture:** A new shared pre-flight module (`build_preflight.py`) is called by both `MavenTool` and `GradleTool` at the top of their execute paths; it consumes the phase-1 analysis via a container-persisted manifest (`/workspace/.setup_agent/build_requirements.json`, next to the existing env-overlay files) and registers provisioned JDKs in the existing `EnvOverlayStore`. The analyzer gains a root-shape classifier that switches the build/test recommendation to root-first fail-at-end for healthy reactors. The validator/report layer gains two report-only conflicts. No hard gates anywhere; retry is bounded to exactly once.

**Tech Stack:** Python 3.12, pytest (`uv run pytest`), Docker orchestrator abstraction (`orchestrator.execute_command(cmd, workdir=...) -> {"success": bool, "exit_code": int, "output": str}`).

**Spec:** `docs/superpowers/specs/2026-07-06-java-execution-strategy-fixes-design.md` (approved).

## Global Constraints

- Never block execution: every check either fixes or degrades to a verdict conflict (spec "Design principle").
- Retry on version-shaped build errors: **exactly once** (spec §1c).
- No `skip_preflight` flag on any tool (spec §1b, settled).
- Verdict semantics unchanged: new conflicts cap at PARTIAL via the existing conflict kernel; do not touch `evaluate_run_verdict`.
- Net-new tests go in **new dedicated test files** (PR #9 convention); do not add tests to pre-existing upstream test files.
- Run tests with `uv run pytest` from the repo root.
- Do NOT modify anything under `src/sag/web/static/` (built artifacts).
- Commit messages: conventional-commit style, **no Co-Authored-By trailer**.
- `docs/` is gitignored in this repo: `git add -f` for any docs file.

## File Structure

| File | Role |
|---|---|
| `src/sag/tools/internal/build_preflight.py` (create) | `JdkPreflight` (check → provision → narrate → overlay), version-error classifier, build-requirements manifest read/write |
| `src/sag/tools/internal/project_analyzer.py` (modify) | Detection hardening (`_normalize_java_version`), root-shape classifier, root-first recommendation, manifest persistence |
| `src/sag/tools/internal/maven_tool.py` (modify) | Pre-flight call, narration prepend, retry-once, scope-narrowing warning |
| `src/sag/tools/internal/gradle_tool.py` (modify) | Same integration as maven_tool |
| `src/sag/agent/physical_validator.py` (modify) | `jdk_mismatch` conflict in `validate_build_status`; `has_test_sources` in `scan_modules` |
| `src/sag/tools/module_metrics.py` (modify) | `modules_test_bearing` in module summary |
| `src/sag/tools/report_tool.py` (modify) | `reactor_scope_narrowed` emission (mirrors existing conflict caps at `report_tool.py:1592-1626`) |
| `tests/test_build_preflight.py` (create) | Tasks 3–5 tests |
| `tests/test_java_version_detection.py` (create) | Task 1 tests |
| `tests/test_root_shape_policy.py` (create) | Task 2 tests |
| `tests/test_build_tool_preflight_integration.py` (create) | Tasks 6–7 tests |
| `tests/test_jdk_reactor_conflicts.py` (create) | Tasks 8–9 tests |

**Key existing anchors (verified against current `main`):**
- Java-version detection regexes: `project_analyzer.py:511-545` (inside `_analyze_maven_config`).
- Recommendation entry points: `_recommend_build_approach` (`project_analyzer.py:1099`), aggregator branch at `:1180-1215`; `_recommend_test_approach` (`:1226`); analysis wiring at `:217-222`; trunk-context handoff at `:1750-1756`.
- `MavenTool.execute` signature: `maven_tool.py:68-83`; workdir auto-resolution ends ~`:180`; command built by `_build_maven_command(command, goals, profiles, properties, pom_file, fail_at_end, use_wrapper, extra_args, maven_executable)` (`:572`).
- `GradleTool.execute`: `gradle_tool.py:38-46`; workdir resolution ends ~`:100`.
- `EnvOverlayStore`: `src/sag/runtime/env_overlay.py`; `register(tool, executable, *, version, source, env, path_prepend, activate)`; JSON at `/workspace/.setup_agent/env_overlay.json`. Reference registration: `project_setup_tool.py:985` (`_register_java_runtime_overlay`).
- Build conflicts list: `physical_validator.py:2088` (inside `validate_build_status`).
- `scan_modules`: `physical_validator.py:2704` (module records `{path, name, class_count, jar_count, report_dirs}`).
- Module summary: `module_metrics.py:98` (`assemble_module_metrics`, summary dict ~`:220`).
- Conflict-cap mirror site: `report_tool.py:1592-1626` (`build_modules_incomplete` / `tests_not_fully_executed` emission into `snapshot["evidence_result"]["conflicts"]`).
- Tool construction: `agent.py:201-202`.

---

### Task 1: Harden Java-version detection

**Files:**
- Modify: `src/sag/tools/internal/project_analyzer.py:511-545`
- Test: `tests/test_java_version_detection.py`

**Interfaces:**
- Produces: module-level `def _normalize_java_version(raw: Optional[str]) -> Optional[str]` in `project_analyzer.py` — returns a plain major-version string (`"8"`, `"17"`) or `None` for unusable input. Tasks 2–3 rely on `analysis["build_config"]["java_version"]` holding only normalized values.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_java_version_detection.py
"""Detection hardening for the JDK pre-flight (execution-strategy fixes).

Covers spec §1a: reject ${...} property indirection, normalize legacy 1.x
versions, take the lower bound of enforcer ranges.
"""

from sag.tools.internal.project_analyzer import _normalize_java_version


def test_plain_major_version_passes_through():
    assert _normalize_java_version("17") == "17"
    assert _normalize_java_version(" 11 ") == "11"


def test_legacy_one_dot_versions_normalize_to_major():
    assert _normalize_java_version("1.8") == "8"
    assert _normalize_java_version("1.7") == "7"


def test_property_indirection_is_rejected():
    assert _normalize_java_version("${jdk.version}") is None
    assert _normalize_java_version("${maven.compiler.release}") is None


def test_garbage_is_rejected():
    assert _normalize_java_version("") is None
    assert _normalize_java_version(None) is None
    assert _normalize_java_version("banana") is None


def test_enforcer_range_lower_bound_via_pattern():
    # The enforcer regex must capture a usable version from range syntax,
    # including legacy 1.x lower bounds ([1.8,) captured "1" before this fix).
    import re
    from sag.tools.internal.project_analyzer import ENFORCER_JAVA_PATTERN

    m = re.search(ENFORCER_JAVA_PATTERN, "<requireJavaVersion><version>[1.8,)</version></requireJavaVersion>", re.DOTALL | re.IGNORECASE)
    assert m and _normalize_java_version(m.group(1)) == "8"
    m = re.search(ENFORCER_JAVA_PATTERN, "<requireJavaVersion><version>[11,17)</version></requireJavaVersion>", re.DOTALL | re.IGNORECASE)
    assert m and _normalize_java_version(m.group(1)) == "11"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_java_version_detection.py -v`
Expected: FAIL — `ImportError: cannot import name '_normalize_java_version'`

- [ ] **Step 3: Implement**

In `project_analyzer.py`, add near the top (module level, after the imports):

```python
# Enforcer version accepts range syntax ([1.8,), [11,17)); capture the lower
# bound including a legacy "1.x" form (the old \d+ captured "1" from "1.8").
ENFORCER_JAVA_PATTERN = (
    r"<requireJavaVersion>.*?<version>\s*\[?\s*(\d+(?:\.\d+)?)"
)


def _normalize_java_version(raw) -> "Optional[str]":
    """Normalize a detected Java version to a plain major string, or None.

    Rejects unresolved property indirection (``${...}``) and non-numeric
    junk; maps legacy ``1.x`` to ``x`` (1.8 -> 8).
    """
    if not raw:
        return None
    value = str(raw).strip()
    if not value or "${" in value:
        return None
    if value.startswith("1.") and value[2:].isdigit():
        return value[2:]
    if value.isdigit():
        return value
    return None
```

Then inside `_analyze_maven_config` (lines 511-545):
1. Replace the inline enforcer pattern string with `ENFORCER_JAVA_PATTERN`.
2. Wrap **every** capture assignment: `java_version = _normalize_java_version(match.group(1))` — and only accept the source (`break`) when the normalized value is not `None` (a rejected capture must fall through to the next pattern, not clear the search).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_java_version_detection.py -v`
Expected: 5 passed

- [ ] **Step 5: Regression-check the analyzer suite**

Run: `uv run pytest tests/test_project_analyzer_build_recommendation.py tests/test_java_version_detection.py -q`
Expected: all pass (the recommendation tests exercise `_analyze_maven_config` indirectly)

- [ ] **Step 6: Commit**

```bash
git add tests/test_java_version_detection.py src/sag/tools/internal/project_analyzer.py
git commit -m "fix(analyzer): normalize detected Java versions; reject \${...}, map 1.x, fix enforcer ranges"
```

---

### Task 2: Root-shape classifier + root-first recommendation

**Files:**
- Modify: `src/sag/tools/internal/project_analyzer.py` (`_recommend_build_approach` `:1099-1224`, `_recommend_test_approach` `:1226+`)
- Test: `tests/test_root_shape_policy.py`

**Interfaces:**
- Consumes: existing recommendation dict (`rec`) with keys `build_system, build_root, goal, rationale, has_gradle`, and the existing `source_modules` scan inside `_recommend_build_approach`.
- Produces: recommendation dict gains `root_shape: "healthy_reactor"|"pathological_aggregator"|"single_module"`, `fail_at_end: bool`, and (healthy reactor only) `goal="install"`, `build_root=<project root>`, `test_root=<project root>`, `test_fail_at_end=True`. Task 3 serializes exactly these keys; Tasks 6–7 read them from the manifest.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_root_shape_policy.py
"""Root-shape classification + root-first targeting (spec §2).

healthy_reactor  -> install -fae at root, test -fae at root
pathological     -> PR #9 leaf path unchanged
single_module    -> current behavior unchanged
"""

from sag.tools.internal.project_analyzer import ProjectAnalyzerTool


class FakeOrch:
    """Answers the analyzer's shell probes from a canned filesystem set."""

    def __init__(self, existing_paths, find_output=""):
        self.existing = set(existing_paths)
        self.find_output = find_output

    def execute_command(self, cmd, workdir=None):
        if cmd.startswith("test -e"):
            path = cmd.split("test -e ", 1)[1].split(" ", 1)[0]
            return {"success": True, "exit_code": 0,
                    "output": "yes" if path in self.existing else "no"}
        if cmd.startswith("find"):
            return {"success": True, "exit_code": 0, "output": self.find_output}
        if "cat" in cmd and "pom.xml" in cmd:
            return {"success": True, "exit_code": 0, "output": self.pom}
        return {"success": True, "exit_code": 0, "output": ""}


def _rec(analysis, orch, project_path="/workspace/proj"):
    tool = ProjectAnalyzerTool.__new__(ProjectAnalyzerTool)  # skip __init__ wiring
    tool.orchestrator = orch
    return tool._recommend_build_approach(project_path, analysis)


def test_healthy_reactor_targets_root_install_fail_at_end():
    orch = FakeOrch(
        existing_paths={"/workspace/proj/pom.xml"},
        # one source-bearing module reachable from root
        find_output="/workspace/proj/core/src/main/java\n",
    )
    orch.pom = "<project><packaging>pom</packaging><modules><module>core</module></modules></project>"
    analysis = {"maven_modules": ["core"], "build_config": {"packaging": "pom"}}
    rec = _rec(analysis, orch)
    assert rec["root_shape"] == "healthy_reactor"
    assert rec["build_root"] == "/workspace/proj"
    assert rec["goal"] == "install"
    assert rec["fail_at_end"] is True


def test_pathological_aggregator_keeps_leaf_targeting():
    orch = FakeOrch(
        existing_paths={"/workspace/proj/pom.xml"},
        find_output="/workspace/proj/vendor-tools/src/main/groovy\n",
    )
    orch.pom = "<project><packaging>pom</packaging></project>"  # no <modules> (profile-gated)
    analysis = {"maven_modules": [], "build_config": {"packaging": "pom"}}
    rec = _rec(analysis, orch)
    assert rec["root_shape"] == "pathological_aggregator"
    assert rec["build_root"] == "/workspace/proj/vendor-tools"  # PR #9 leaf path preserved


def test_single_module_shape_recorded():
    orch = FakeOrch(
        existing_paths={"/workspace/proj/pom.xml", "/workspace/proj/src/main/java"},
    )
    orch.pom = "<project><packaging>jar</packaging></project>"
    analysis = {"maven_modules": [], "build_config": {"packaging": "jar"}}
    rec = _rec(analysis, orch)
    assert rec["root_shape"] == "single_module"


def test_healthy_reactor_test_recommendation_is_root_fail_at_end():
    orch = FakeOrch(
        existing_paths={"/workspace/proj/pom.xml"},
        find_output="/workspace/proj/core/src/main/java\n",
    )
    orch.pom = "<project><packaging>pom</packaging><modules><module>core</module></modules></project>"
    analysis = {"maven_modules": ["core"], "build_config": {"packaging": "pom"}}
    tool = ProjectAnalyzerTool.__new__(ProjectAnalyzerTool)
    tool.orchestrator = orch
    rec = tool._recommend_build_approach("/workspace/proj", analysis)
    tool._recommend_test_approach("/workspace/proj", rec)
    assert rec["test_root"] == "/workspace/proj"
    assert rec["test_fail_at_end"] is True
```

Note for the implementer: `_recommend_build_approach`'s real signature and the
exact probes it makes may differ slightly from the fakes above — adapt the
fakes to the real call pattern (read the function first), but the four
assertions are the contract and must not be weakened.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_root_shape_policy.py -v`
Expected: FAIL — `KeyError: 'root_shape'` (or assertion on `goal`)

- [ ] **Step 3: Implement in `_recommend_build_approach`**

In the aggregator branch (`project_analyzer.py:1180-1215`), the split already
exists (`analysis.get("maven_modules")` → root vs leaf). Change it to:

```python
        if has_pom and packaging == "pom":
            groovy_modules = [m for m in source_modules if m["lang"] == "groovy"]
            if source_modules:
                if analysis.get("maven_modules"):
                    # Healthy reactor: install at root with fail-at-end so every
                    # module lands in ~/.m2 and sibling SNAPSHOT deps resolve
                    # (spec §2: root-first won the benchmark).
                    rec.update(
                        build_system="maven",
                        build_root=project_path,
                        goal="install",
                        root_shape="healthy_reactor",
                        fail_at_end=True,
                    )
                    rec["rationale"] = (
                        f"Reactor root declares {len(analysis['maven_modules'])} module(s); "
                        "install -fae at root builds all of them and populates the local repo."
                    )
                    return rec
                # Pathological aggregator (Bigtop): no reactor modules at root —
                # keep the PR #9 leaf targeting unchanged.
                goal = "install" if groovy_modules else "compile"
                preferred = (groovy_modules or source_modules)[0]
                rec.update(
                    build_system="maven",
                    build_root=preferred["dir"],
                    goal=goal,
                    root_shape="pathological_aggregator",
                    fail_at_end=False,
                )
                rec["rationale"] = (
                    f"Aggregator root with no reactor modules over {len(source_modules)} "
                    f"source module(s); build module {preferred['module']} directly with '{goal}'."
                )
                return rec
```

Everywhere else `rec` is returned (single-module path, gradle-only path,
aggregator-only path), add `rec.setdefault("root_shape", "single_module")`
and `rec.setdefault("fail_at_end", False)` just before the return — simplest
done once at each `return rec` site, or centralized by wrapping the returns.

- [ ] **Step 4: Implement in `_recommend_test_approach`**

At the top of `_recommend_test_approach` (before the test-cluster discovery),
short-circuit for healthy reactors:

```python
        if build_rec.get("root_shape") == "healthy_reactor":
            build_rec["test_root"] = project_path
            build_rec["test_system"] = "maven"
            build_rec["test_fail_at_end"] = True
            build_rec["test_rationale"] = (
                "Healthy reactor: run 'mvn test -fae' at the root so every "
                "module's suite executes."
            )
            return
```

The existing cluster logic remains the fallback for the other two shapes.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_root_shape_policy.py tests/test_project_analyzer_build_recommendation.py tests/test_react_engine_build_recommendation.py -v`
Expected: new tests pass; if an existing recommendation test asserted `goal == "compile"`/leaf targeting for a root **with** declared modules, update that assertion to the new root-first contract (`install` at root, `fail_at_end=True`) — that contract change is this task's point. Existing pathological/Bigtop tests must pass unchanged.

- [ ] **Step 6: Commit**

```bash
git add tests/test_root_shape_policy.py src/sag/tools/internal/project_analyzer.py
git commit -m "feat(analyzer): root-shape classifier; healthy reactors build+test at root with -fae"
```

---

### Task 3: Build-requirements manifest (analyzer → tools handoff)

**Files:**
- Create: `src/sag/tools/internal/build_preflight.py`
- Modify: `src/sag/tools/internal/project_analyzer.py:217-222` (persist after recommendations)
- Test: `tests/test_build_preflight.py`

**Interfaces:**
- Consumes: Task 2's recommendation keys; `analysis["build_config"]["java_version"]`, `analysis["java_version_source"]`, `analysis["java_version_enforced"]`.
- Produces (Tasks 4–8 rely on these exact names):
  - `REQUIREMENTS_PATH = "/workspace/.setup_agent/build_requirements.json"`
  - `def write_build_requirements(orchestrator, data: dict) -> bool`
  - `def read_build_requirements(orchestrator) -> dict` (empty dict when absent/corrupt)
  - Manifest keys: `java_version, java_version_source, java_version_enforced, root_shape, build_root, build_goal, fail_at_end, test_root, test_system, test_fail_at_end`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_build_preflight.py
"""JDK pre-flight + build-requirements manifest (spec §1).

The manifest is the phase-1 -> build-tool handoff: tools only hold an
orchestrator, so requirements persist in the container next to the env
overlay (/workspace/.setup_agent/).
"""

import json

from sag.tools.internal.build_preflight import (
    REQUIREMENTS_PATH,
    read_build_requirements,
    write_build_requirements,
)


class FakeOrch:
    """In-memory container FS: supports the cat/mkdir/heredoc commands used."""

    def __init__(self):
        self.files = {}

    def execute_command(self, cmd, workdir=None):
        if cmd.startswith("mkdir -p"):
            return {"success": True, "exit_code": 0, "output": ""}
        if cmd.startswith("cat "):
            path = cmd.split("cat ", 1)[1].strip()
            if path in self.files:
                return {"success": True, "exit_code": 0, "output": self.files[path]}
            return {"success": False, "exit_code": 1, "output": "No such file"}
        if "<<" in cmd and REQUIREMENTS_PATH in cmd:  # heredoc write
            body = cmd.split("<<'SAGEOF'\n", 1)[1].rsplit("\nSAGEOF", 1)[0]
            self.files[REQUIREMENTS_PATH] = body
            return {"success": True, "exit_code": 0, "output": ""}
        return {"success": True, "exit_code": 0, "output": ""}


def test_write_then_read_round_trips():
    orch = FakeOrch()
    data = {"java_version": "17", "root_shape": "healthy_reactor", "build_root": "/workspace/p"}
    assert write_build_requirements(orch, data) is True
    assert read_build_requirements(orch) == data


def test_read_missing_manifest_returns_empty_dict():
    assert read_build_requirements(FakeOrch()) == {}


def test_read_corrupt_manifest_returns_empty_dict():
    orch = FakeOrch()
    orch.files[REQUIREMENTS_PATH] = "{not json"
    assert read_build_requirements(orch) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_build_preflight.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sag.tools.internal.build_preflight'`

- [ ] **Step 3: Create the module**

```python
# src/sag/tools/internal/build_preflight.py
"""JDK pre-flight + build-requirements manifest.

The pre-flight CONSUMES the phase-1 analysis (it is a guarantee layer, not a
second analyzer): the analyzer persists requirements into the container at
REQUIREMENTS_PATH; MavenTool/GradleTool call JdkPreflight at the top of every
build/test execution. When the environment already matches, the pre-flight is
a single `java -version` no-op. See
docs/superpowers/specs/2026-07-06-java-execution-strategy-fixes-design.md.
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from loguru import logger

REQUIREMENTS_PATH = "/workspace/.setup_agent/build_requirements.json"


def write_build_requirements(orchestrator, data: Dict[str, Any]) -> bool:
    """Persist the analyzer's build requirements into the container."""
    try:
        body = json.dumps(data, indent=2, sort_keys=True)
        orchestrator.execute_command("mkdir -p /workspace/.setup_agent")
        result = orchestrator.execute_command(
            f"cat > {REQUIREMENTS_PATH} <<'SAGEOF'\n{body}\nSAGEOF"
        )
        return bool(result.get("success"))
    except Exception as exc:
        logger.warning(f"Failed to write build requirements: {exc}")
        return False


def read_build_requirements(orchestrator) -> Dict[str, Any]:
    """Read the manifest; {} when absent or corrupt (callers degrade gracefully)."""
    try:
        result = orchestrator.execute_command(f"cat {REQUIREMENTS_PATH}")
        if not result.get("success"):
            return {}
        return json.loads(result.get("output") or "")
    except Exception:
        return {}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_build_preflight.py -v`
Expected: 3 passed

- [ ] **Step 5: Persist from the analyzer**

In `project_analyzer.py`, right after `:222` (`self._recommend_test_approach(...)`):

```python
            from .build_preflight import write_build_requirements

            rec = analysis.get("build_recommendation") or {}
            write_build_requirements(
                self.orchestrator,
                {
                    "java_version": (analysis.get("build_config") or {}).get("java_version"),
                    "java_version_source": analysis.get("java_version_source"),
                    "java_version_enforced": bool(analysis.get("java_version_enforced")),
                    "root_shape": rec.get("root_shape"),
                    "build_root": rec.get("build_root"),
                    "build_goal": rec.get("goal"),
                    "fail_at_end": bool(rec.get("fail_at_end")),
                    "test_root": rec.get("test_root"),
                    "test_system": rec.get("test_system"),
                    "test_fail_at_end": bool(rec.get("test_fail_at_end")),
                },
            )
```

(Adapt the exact analysis-dict keys to where `_analyze_maven_config` stores
them — verify with `grep -n "java_version_source" src/sag/tools/internal/project_analyzer.py` —
but the manifest key names above are the contract and must not change.)

- [ ] **Step 6: Full-suite sanity + commit**

Run: `uv run pytest tests/test_build_preflight.py tests/test_project_analyzer_build_recommendation.py -q`
Expected: all pass

```bash
git add src/sag/tools/internal/build_preflight.py tests/test_build_preflight.py src/sag/tools/internal/project_analyzer.py
git commit -m "feat(preflight): container-persisted build-requirements manifest from phase-1 analysis"
```

---

### Task 4: JdkPreflight — check, provision, narrate, overlay

**Files:**
- Modify: `src/sag/tools/internal/build_preflight.py`
- Test: `tests/test_build_preflight.py` (append)

**Interfaces:**
- Consumes: `EnvOverlayStore` (`sag.runtime.env_overlay`), manifest from Task 3.
- Produces (Tasks 6–7 rely on these):
  - `@dataclass PreflightOutcome: matched: bool; active_version: Optional[str]; required_version: Optional[str]; provisioned: bool; mismatch: bool; narration: str`
  - `class JdkPreflight: __init__(self, orchestrator); run(self, required_version: Optional[str], source: str = "unknown") -> PreflightOutcome`
  - `def active_java_major(orchestrator) -> Optional[str]`

- [ ] **Step 1: Write the failing tests (append to `tests/test_build_preflight.py`)**

```python
from sag.tools.internal.build_preflight import JdkPreflight, active_java_major


class ProvisionOrch(FakeOrch):
    """Scriptable orchestrator: maps command substrings to canned results."""

    def __init__(self, java_version_output, apt_ok=True, temurin_ok=True):
        super().__init__()
        self.java_output = java_version_output
        self.apt_ok = apt_ok
        self.temurin_ok = temurin_ok
        self.commands = []

    def execute_command(self, cmd, workdir=None):
        self.commands.append(cmd)
        if "java -version" in cmd:
            return {"success": True, "exit_code": 0, "output": self.java_output}
        if "apt-get install -y openjdk" in cmd:
            return {"success": self.apt_ok, "exit_code": 0 if self.apt_ok else 100,
                    "output": "" if self.apt_ok else "E: Unable to locate package"}
        if "temurin" in cmd:
            return {"success": self.temurin_ok, "exit_code": 0 if self.temurin_ok else 1,
                    "output": ""}
        if cmd.startswith("ls -d /usr/lib/jvm"):
            return {"success": True, "exit_code": 0,
                    "output": "/usr/lib/jvm/java-17-openjdk-arm64"}
        return super().execute_command(cmd, workdir)


def test_matching_jdk_is_a_noop():
    orch = ProvisionOrch('openjdk version "17.0.9" 2023-10-17')
    outcome = JdkPreflight(orch).run("17", source="maven-enforcer")
    assert outcome.matched is True
    assert outcome.provisioned is False
    assert outcome.narration == ""
    assert not any("apt-get" in c for c in orch.commands)


def test_active_java_major_parses_legacy_and_modern():
    assert active_java_major(ProvisionOrch('openjdk version "17.0.9"')) == "17"
    assert active_java_major(ProvisionOrch('java version "1.8.0_392"')) == "8"


def test_mismatch_provisions_and_narrates(monkeypatch):
    orch = ProvisionOrch('openjdk version "11.0.2"')
    # Overlay registration talks to the container too; stub it out.
    import sag.tools.internal.build_preflight as bp
    monkeypatch.setattr(bp, "_register_overlay", lambda *a, **k: True)
    outcome = JdkPreflight(orch).run("17", source="maven-enforcer")
    assert outcome.provisioned is True
    assert outcome.mismatch is False
    assert "[pre-flight] Required: Java 17" in outcome.narration
    assert "Active: Java 11" in outcome.narration


def test_unprovisionable_degrades_to_mismatch_note_never_raises(monkeypatch):
    orch = ProvisionOrch('openjdk version "11.0.2"', apt_ok=False, temurin_ok=False)
    import sag.tools.internal.build_preflight as bp
    monkeypatch.setattr(bp, "_register_overlay", lambda *a, **k: True)
    outcome = JdkPreflight(orch).run("8", source="maven-compiler")
    assert outcome.provisioned is False
    assert outcome.mismatch is True          # verifier picks this up (Task 8)
    assert "could not provision" in outcome.narration


def test_no_requirement_is_a_noop():
    orch = ProvisionOrch('openjdk version "21.0.1"')
    outcome = JdkPreflight(orch).run(None)
    assert outcome.matched is True and outcome.narration == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_build_preflight.py -v`
Expected: new tests FAIL — `ImportError: cannot import name 'JdkPreflight'`

- [ ] **Step 3: Implement (append to `build_preflight.py`)**

```python
_JAVA_VERSION_RE = re.compile(r'version "(?:1\.)?(\d+)')

# Adoptium/Temurin apt repo for JDKs missing from the base image's Debian
# release (e.g. JDK 8 on bookworm). One-shot, idempotent.
_TEMURIN_SETUP = (
    "apt-get install -y wget apt-transport-https gnupg >/dev/null 2>&1; "
    "wget -qO- https://packages.adoptium.net/artifactory/api/gpg/key/public "
    "| gpg --dearmor -o /usr/share/keyrings/adoptium.gpg 2>/dev/null; "
    'echo "deb [signed-by=/usr/share/keyrings/adoptium.gpg] '
    'https://packages.adoptium.net/artifactory/deb '
    '$(. /etc/os-release && echo $VERSION_CODENAME) main" '
    "> /etc/apt/sources.list.d/adoptium.list && apt-get update"
)


def active_java_major(orchestrator) -> Optional[str]:
    """Major version of the currently active `java`, or None."""
    result = orchestrator.execute_command("java -version 2>&1")
    match = _JAVA_VERSION_RE.search(result.get("output") or "")
    return match.group(1) if match else None


def _register_overlay(orchestrator, java_home: str, version: str) -> bool:
    """Register the provisioned JDK in the shared env overlay (report-visible)."""
    try:
        from sag.runtime.env_overlay import EnvOverlayStore

        EnvOverlayStore(orchestrator).register(
            "java",
            f"{java_home}/bin/java",
            version=version,
            source="build_preflight",
            env={"JAVA_HOME": java_home},
            path_prepend=[f"{java_home}/bin"],
            activate=True,
        )
        return True
    except Exception as exc:
        logger.warning(f"Pre-flight overlay registration failed: {exc}")
        return False


@dataclass
class PreflightOutcome:
    matched: bool
    active_version: Optional[str]
    required_version: Optional[str]
    provisioned: bool = False
    mismatch: bool = False
    narration: str = ""


class JdkPreflight:
    """Check-and-fix JDK guarantee. Never raises; never blocks (spec §1b)."""

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator

    def run(self, required_version: Optional[str], source: str = "unknown") -> PreflightOutcome:
        try:
            return self._run(required_version, source)
        except Exception as exc:  # never let the pre-flight kill a build
            logger.warning(f"JDK pre-flight error (continuing): {exc}")
            return PreflightOutcome(True, None, required_version)

    def _run(self, required: Optional[str], source: str) -> PreflightOutcome:
        if not required:
            return PreflightOutcome(True, None, None)
        active = active_java_major(self.orchestrator)
        if active == required:
            logger.debug(f"JDK pre-flight: active Java {active} matches requirement")
            return PreflightOutcome(True, active, required)

        header = (
            f"[pre-flight] Required: Java {required} (source: {source}). "
            f"Active: Java {active or 'unknown'}."
        )
        java_home = self._provision(required)
        if java_home:
            _register_overlay(self.orchestrator, java_home, required)
            return PreflightOutcome(
                matched=False, active_version=active, required_version=required,
                provisioned=True,
                narration=(
                    f"{header}\n→ installed JDK {required}, "
                    f"JAVA_HOME={java_home} (overlay registered)"
                ),
            )
        return PreflightOutcome(
            matched=False, active_version=active, required_version=required,
            provisioned=False, mismatch=True,
            narration=(
                f"{header}\n→ could not provision JDK {required} "
                f"(apt + Temurin exhausted); continuing on Java {active or 'unknown'} — "
                "the verdict will record jdk_mismatch"
            ),
        )

    def _provision(self, version: str) -> Optional[str]:
        """apt -> Temurin ladder; returns JAVA_HOME on success, None on failure."""
        apt = self.orchestrator.execute_command(
            f"DEBIAN_FRONTEND=noninteractive apt-get update >/dev/null 2>&1; "
            f"DEBIAN_FRONTEND=noninteractive apt-get install -y openjdk-{version}-jdk"
        )
        if not apt.get("success"):
            self.orchestrator.execute_command(_TEMURIN_SETUP)
            temurin = self.orchestrator.execute_command(
                f"DEBIAN_FRONTEND=noninteractive apt-get install -y temurin-{version}-jdk"
            )
            if not temurin.get("success"):
                return None
        home = self.orchestrator.execute_command(
            f"ls -d /usr/lib/jvm/java-{version}-openjdk-* "
            f"/usr/lib/jvm/temurin-{version}-jdk* 2>/dev/null | head -1"
        )
        java_home = (home.get("output") or "").strip().splitlines()
        java_home = java_home[0].strip() if java_home else ""
        if not java_home:
            return None
        self.orchestrator.execute_command(
            f"update-alternatives --install /usr/bin/java java {java_home}/bin/java 100 "
            f"&& update-alternatives --set java {java_home}/bin/java; "
            f"test -x {java_home}/bin/javac && "
            f"update-alternatives --install /usr/bin/javac javac {java_home}/bin/javac 100 "
            f"&& update-alternatives --set javac {java_home}/bin/javac"
        )
        return java_home
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_build_preflight.py -v`
Expected: all pass (8 total)

- [ ] **Step 5: Commit**

```bash
git add src/sag/tools/internal/build_preflight.py tests/test_build_preflight.py
git commit -m "feat(preflight): JdkPreflight check-and-fix with apt->Temurin ladder, overlay registration, narration"
```

---

### Task 5: Version-shaped error classifier (retry-once trigger)

**Files:**
- Modify: `src/sag/tools/internal/build_preflight.py`
- Test: `tests/test_build_preflight.py` (append)

**Interfaces:**
- Produces: `def classify_version_error(output: str) -> Optional[str]` — the JDK major the build actually needs, extracted from the error text, or `None` when the failure is not version-shaped. Tasks 6–7 call it on failed build output.

- [ ] **Step 1: Write the failing tests (append)**

```python
from sag.tools.internal.build_preflight import classify_version_error


def test_enforcer_message_yields_version():
    out = ("[ERROR] Rule 0: org.apache.maven.plugins.enforcer.RequireJavaVersion failed "
           "with message:\nDetected JDK Version: 11.0.2 is not in the allowed range [17,).")
    assert classify_version_error(out) == "17"


def test_unsupported_class_version_maps_bytecode_to_jdk():
    # class file version 61.0 = JDK 17 (44 + major)
    out = ("java.lang.UnsupportedClassVersionError: com/foo/Bar has been compiled by a "
           "more recent version of the Java Runtime (class file version 61.0)")
    assert classify_version_error(out) == "17"


def test_invalid_target_release():
    assert classify_version_error("[ERROR] Fatal error compiling: error: invalid target release: 21") == "21"
    assert classify_version_error("error: release version 17 not supported") == "17"


def test_non_version_failures_return_none():
    assert classify_version_error("[ERROR] Failed to execute goal ... test failures") is None
    assert classify_version_error("") is None
    assert classify_version_error("BUILD SUCCESS") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_build_preflight.py -v -k classify`
Expected: FAIL — `ImportError: cannot import name 'classify_version_error'`

- [ ] **Step 3: Implement (append to `build_preflight.py`)**

```python
# Version-shaped build failures, in match priority. Each pattern captures the
# JDK major the build ACTUALLY needs (the honest, authoritative signal that
# static pom analysis cannot always see — spec §1c).
_VERSION_ERROR_PATTERNS = [
    # enforcer: "... allowed range [17,)" / "allowed version range [11,17)"
    re.compile(r"RequireJavaVersion.*?allowed(?:\s+version)?\s+range\s*\[?(\d+)", re.DOTALL | re.IGNORECASE),
    # javac: "invalid target release: 21" / "release version 17 not supported"
    re.compile(r"invalid (?:target|source) release:?\s*(?:1\.)?(\d+)", re.IGNORECASE),
    re.compile(r"release version (\d+) not supported", re.IGNORECASE),
]
_CLASS_FILE_VERSION = re.compile(r"class file version (\d+)\.")


def classify_version_error(output: str) -> Optional[str]:
    """Extract the JDK major a failed build says it needs, else None."""
    if not output:
        return None
    for pattern in _VERSION_ERROR_PATTERNS:
        match = pattern.search(output)
        if match:
            return match.group(1)
    match = _CLASS_FILE_VERSION.search(output)
    if match:
        # Class-file major 52 = JDK 8, 61 = JDK 17: major - 44.
        return str(int(match.group(1)) - 44)
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_build_preflight.py -v`
Expected: all pass (12 total)

- [ ] **Step 5: Commit**

```bash
git add src/sag/tools/internal/build_preflight.py tests/test_build_preflight.py
git commit -m "feat(preflight): version-shaped error classifier for the bounded retry"
```

---

### Task 6: MavenTool integration (pre-flight + narration + retry-once + scope warning)

**Files:**
- Modify: `src/sag/tools/internal/maven_tool.py` (inside `execute`, after workdir resolution ~`:180`, and around the main command execution)
- Test: `tests/test_build_tool_preflight_integration.py`

**Interfaces:**
- Consumes: `JdkPreflight`, `classify_version_error`, `read_build_requirements`, `active_java_major` from Task 3–5.
- Produces: behavior only — narration/warnings prepended to `ToolResult.output`; retry metadata key `metadata["jdk_retry"] = {"from": str, "to": str}` when the retry fired.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_build_tool_preflight_integration.py
"""MavenTool/GradleTool pre-flight integration (spec §§1b-1c, 3).

Uses a scriptable orchestrator; asserts on the OBSERVATION text because the
narration IS the feature (transparency-by-construction)."""

import json

from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.maven_tool import MavenTool


class ScriptedOrch:
    """Returns canned results; records every command."""

    def __init__(self, java="17", manifest=None, build_output="BUILD SUCCESS", build_ok=True):
        self.java = java
        self.manifest = manifest or {}
        self.build_output = build_output
        self.build_ok = build_ok
        self.commands = []
        self.project_name = "proj"

    def execute_command(self, cmd, workdir=None):
        self.commands.append(cmd)
        if "java -version" in cmd:
            return {"success": True, "exit_code": 0,
                    "output": f'openjdk version "{self.java}.0.1"'}
        if cmd == f"cat {REQUIREMENTS_PATH}":
            if self.manifest:
                return {"success": True, "exit_code": 0, "output": json.dumps(self.manifest)}
            return {"success": False, "exit_code": 1, "output": ""}
        if "mvn" in cmd and ("test" in cmd or "install" in cmd or "compile" in cmd):
            return {"success": self.build_ok, "exit_code": 0 if self.build_ok else 1,
                    "output": self.build_output}
        if "test -f" in cmd or "test -e" in cmd:  # pom probes
            return {"success": True, "exit_code": 0, "output": "yes"}
        if "command -v mvn" in cmd or "which mvn" in cmd:
            return {"success": True, "exit_code": 0, "output": "/usr/bin/mvn"}
        return {"success": True, "exit_code": 0, "output": ""}


def _tool(orch):
    tool = MavenTool.__new__(MavenTool)  # skip full __init__; wire minimum
    tool.orchestrator = orch
    tool.toolchain_manager = None
    tool.command_tracker = None
    return tool


def test_matching_jdk_no_narration():
    orch = ScriptedOrch(java="17", manifest={"java_version": "17"})
    result = _tool(orch).execute(command="compile", working_directory="/workspace/proj")
    assert "[pre-flight]" not in (result.output or "")


def test_mismatch_narrated_in_observation(monkeypatch):
    import sag.tools.internal.build_preflight as bp
    monkeypatch.setattr(bp.JdkPreflight, "_provision", lambda self, v: "/usr/lib/jvm/java-17-openjdk-arm64")
    orch = ScriptedOrch(java="11", manifest={"java_version": "17", "java_version_source": "maven-enforcer"})
    result = _tool(orch).execute(command="compile", working_directory="/workspace/proj")
    assert "[pre-flight] Required: Java 17" in (result.output or "")


def test_version_error_triggers_single_retry(monkeypatch):
    import sag.tools.internal.build_preflight as bp
    monkeypatch.setattr(bp.JdkPreflight, "_provision", lambda self, v: f"/usr/lib/jvm/java-{v}-openjdk-arm64")
    enforcer_fail = ("[ERROR] RequireJavaVersion failed ... Detected JDK Version: "
                     "11.0.2 is not in the allowed range [17,). BUILD FAILURE")
    orch = ScriptedOrch(java="11", manifest={}, build_output=enforcer_fail, build_ok=False)
    result = _tool(orch).execute(command="compile", working_directory="/workspace/proj")
    mvn_runs = [c for c in orch.commands if "mvn" in c and "compile" in c]
    assert len(mvn_runs) == 2          # original + exactly one retry
    assert "retry 1/1" in (result.output or "")


def test_non_version_failure_does_not_retry():
    orch = ScriptedOrch(java="17", manifest={"java_version": "17"},
                        build_output="BUILD FAILURE: test failures", build_ok=False)
    _tool(orch).execute(command="test", working_directory="/workspace/proj")
    mvn_runs = [c for c in orch.commands if "mvn" in c and "test" in c]
    assert len(mvn_runs) == 1


def test_scope_narrowing_warning_when_leaf_targeted():
    orch = ScriptedOrch(java="17", manifest={
        "java_version": "17", "root_shape": "healthy_reactor",
        "build_root": "/workspace/proj",
    })
    result = _tool(orch).execute(command="test", working_directory="/workspace/proj/core")
    assert "[scope]" in (result.output or "")


def test_no_scope_warning_at_recommended_root():
    orch = ScriptedOrch(java="17", manifest={
        "java_version": "17", "root_shape": "healthy_reactor",
        "build_root": "/workspace/proj",
    })
    result = _tool(orch).execute(command="test", working_directory="/workspace/proj")
    assert "[scope]" not in (result.output or "")


def test_unscoped_invocation_defaults_to_reactor_root_with_fail_at_end():
    # Spec §3: no explicit working_directory -> the recommendation wins.
    orch = ScriptedOrch(java="17", manifest={
        "java_version": "17", "root_shape": "healthy_reactor",
        "build_root": "/workspace/proj",
    })
    result = _tool(orch).execute(command="test")  # default "/workspace"
    assert "defaulting to the recommended reactor root" in (result.output or "")
    mvn_runs = [c for c in orch.commands if "mvn" in c and "test" in c]
    assert mvn_runs and "-fae" in mvn_runs[0]  # reactor test runs fail-at-end
```

Note for the implementer: `MavenTool.execute` has probes and toolchain paths
these fakes must satisfy — read `maven_tool.py:68-250` first and extend
`ScriptedOrch` for whatever additional probe commands the real path issues
(pattern above: match on command substring, return canned success). The
assertions are the contract.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_build_tool_preflight_integration.py -v`
Expected: FAIL — no `[pre-flight]`/`[scope]` text, single mvn run assertions fail on retry test

- [ ] **Step 3: Implement in `maven_tool.py`**

At the top of the file: `from .build_preflight import JdkPreflight, classify_version_error, read_build_requirements`.

Inside `execute`, immediately after the working-directory resolution block (~`:180`), insert:

```python
        # --- JDK pre-flight + scope defaults (spec §§1b, 3) ----------------
        preamble_lines: list[str] = []
        requirements = read_build_requirements(self.orchestrator)
        outcome = JdkPreflight(self.orchestrator).run(
            requirements.get("java_version"),
            source=requirements.get("java_version_source") or "unknown",
        )
        if outcome.narration:
            preamble_lines.append(outcome.narration)

        recommended_root = (requirements.get("build_root") or "").rstrip("/")
        if requirements.get("root_shape") == "healthy_reactor" and recommended_root:
            # Spec §3: unscoped invocations default to the recommendation.
            if working_directory in (None, "/workspace"):
                working_directory = recommended_root
                preamble_lines.append(
                    f"[scope] defaulting to the recommended reactor root {recommended_root}"
                )
            # Reactor runs default to fail-at-end so one broken module can't
            # hide the rest (the agent's explicit True is left untouched).
            if not fail_at_end and any(
                phase in command for phase in ("test", "install", "package", "verify")
            ):
                fail_at_end = True

        if (
            requirements.get("root_shape") == "healthy_reactor"
            and recommended_root
            and working_directory.rstrip("/") != recommended_root
        ) or ("-pl" in (extra_args or "")):
            preamble_lines.append(
                "[scope] narrower than the recommended reactor root "
                f"({recommended_root or 'root'}) — sibling deps may be unresolved; "
                "tests outside this module will not run"
            )
        preamble = ("\n".join(preamble_lines) + "\n") if preamble_lines else ""
```

Around the **main** command execution (the site that runs `maven_cmd` via
`self.orchestrator.execute_command(maven_cmd, workdir=working_directory)` for
the primary build path — not the `version_command` early-return), wrap with
the bounded retry:

```python
        result = self.orchestrator.execute_command(maven_cmd, workdir=working_directory)

        # Bounded retry: a version-shaped failure means the requirement in the
        # error text is authoritative; re-provision from IT and rerun ONCE.
        if result.get("exit_code") != 0:
            needed = classify_version_error(result.get("output") or "")
            if needed and needed != outcome.active_version:
                retry_outcome = JdkPreflight(self.orchestrator).run(
                    needed, source="build-error"
                )
                if retry_outcome.provisioned:
                    preamble += (
                        f"[pre-flight] build error requires Java {needed}, "
                        f"re-provisioned, retry 1/1\n"
                    )
                    result = self.orchestrator.execute_command(
                        maven_cmd, workdir=working_directory
                    )
```

Finally, prepend `preamble` to the `output` (and `raw_output`) of the
`ToolResult` built from this main path, e.g. `output=preamble + output_text`.
Add `metadata["jdk_retry"] = {"from": outcome.active_version, "to": needed}`
when the retry fired.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_build_tool_preflight_integration.py -v`
Expected: 6 passed (gradle tests come in Task 7)

- [ ] **Step 5: Regression: maven tool suite**

Run: `uv run pytest tests/ -q -k "maven"`
Expected: all pass — existing maven tests use orchestrator fakes; where a fake
doesn't answer `cat /workspace/.setup_agent/build_requirements.json`,
`read_build_requirements` returns `{}` and the pre-flight no-ops (verify this
holds; if a fake asserts on exact command sequences, update it to tolerate the
two new probe commands).

- [ ] **Step 6: Commit**

```bash
git add tests/test_build_tool_preflight_integration.py src/sag/tools/internal/maven_tool.py
git commit -m "feat(maven): JDK pre-flight, narrated scope check, bounded version-error retry"
```

---

### Task 7: GradleTool integration

**Files:**
- Modify: `src/sag/tools/internal/gradle_tool.py` (inside `execute`, after workdir resolution ~`:100`)
- Test: `tests/test_build_tool_preflight_integration.py` (append)

**Interfaces:**
- Consumes: identical helpers as Task 6.
- Produces: identical behavior on `GradleTool` (narration prepend, retry-once, `[scope]` warning when `working_directory` differs from a healthy reactor's `build_root`).

- [ ] **Step 1: Write the failing tests (append)**

```python
from sag.tools.internal.gradle_tool import GradleTool


class GradleScriptedOrch(ScriptedOrch):
    def execute_command(self, cmd, workdir=None):
        self.commands.append(cmd)
        if "java -version" in cmd:
            return {"success": True, "exit_code": 0,
                    "output": f'openjdk version "{self.java}.0.1"'}
        if cmd == f"cat {REQUIREMENTS_PATH}":
            if self.manifest:
                return {"success": True, "exit_code": 0, "output": json.dumps(self.manifest)}
            return {"success": False, "exit_code": 1, "output": ""}
        if "gradle" in cmd and ("build" in cmd or "test" in cmd):
            return {"success": self.build_ok, "exit_code": 0 if self.build_ok else 1,
                    "output": self.build_output}
        return {"success": True, "exit_code": 0, "output": "yes"}


def _gradle_tool(orch):
    tool = GradleTool.__new__(GradleTool)
    tool.orchestrator = orch
    tool.toolchain_manager = None
    return tool


def test_gradle_mismatch_narrated(monkeypatch):
    import sag.tools.internal.build_preflight as bp
    monkeypatch.setattr(bp.JdkPreflight, "_provision", lambda self, v: "/usr/lib/jvm/java-17-openjdk-arm64")
    orch = GradleScriptedOrch(java="11", manifest={"java_version": "17"})
    result = _gradle_tool(orch).execute(command="build", working_directory="/workspace/proj")
    assert "[pre-flight] Required: Java 17" in (result.output or "")


def test_gradle_version_error_single_retry(monkeypatch):
    import sag.tools.internal.build_preflight as bp
    monkeypatch.setattr(bp.JdkPreflight, "_provision", lambda self, v: f"/usr/lib/jvm/java-{v}-openjdk-arm64")
    fail = "Unsupported class file major version 61 ... class file version 61.0"
    orch = GradleScriptedOrch(java="11", manifest={}, build_output=fail, build_ok=False)
    _gradle_tool(orch).execute(command="build", working_directory="/workspace/proj")
    runs = [c for c in orch.commands if "gradle" in c and "build" in c]
    assert len(runs) == 2
```

(As with Task 6, extend the fake for whatever probes the real
`GradleTool.execute` issues — wrapper detection, settings probes — returning
canned successes.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_build_tool_preflight_integration.py -v -k gradle`
Expected: FAIL

- [ ] **Step 3: Implement**

Mirror Task 6 exactly in `gradle_tool.py`: same import, same preamble block
after workdir resolution (~`:100`), same bounded-retry wrapper around the main
gradle command execution site, same `preamble +` prepend on the main
`ToolResult` output. (The logic is deliberately identical and small at each
call site; the shared behavior already lives in `build_preflight.py` — do NOT
copy `JdkPreflight` internals into the tools.)

Gradle's fail-at-end analog (spec §2): when the manifest says
`root_shape == "healthy_reactor"` and the command runs `build`/`test`/`check`,
append `--continue` to the gradle argument string if not already present, so
one failing subproject doesn't hide the rest. Add this where the gradle
command string is assembled, and assert it in the
`test_gradle_mismatch_narrated` fixture setup (extend that test or add
`test_gradle_healthy_reactor_appends_continue` with
`manifest={"java_version": "17", "root_shape": "healthy_reactor", "build_root": "/workspace/proj"}`,
asserting `"--continue" in` the recorded gradle command).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_build_tool_preflight_integration.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add tests/test_build_tool_preflight_integration.py src/sag/tools/internal/gradle_tool.py
git commit -m "feat(gradle): JDK pre-flight, narrated scope check, bounded version-error retry"
```

---

### Task 8: `jdk_mismatch` verdict conflict

**Files:**
- Modify: `src/sag/agent/physical_validator.py` (inside `validate_build_status`, conflicts assembly at ~`:2088`)
- Test: `tests/test_jdk_reactor_conflicts.py`

**Interfaces:**
- Consumes: `read_build_requirements`, `active_java_major` (Task 3–4).
- Produces: `"jdk_mismatch"` appended to the build-status `conflicts` list when required ≠ active at validation time. The existing conflict kernel (report_tool `_snapshot_kernel_verdict`) already caps any non-adjudicated conflict at PARTIAL — no kernel change.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jdk_reactor_conflicts.py
"""Verifier honesty layer for the execution-strategy fixes (spec §4).

jdk_mismatch: required JDK != active at validation time -> PARTIAL cap.
reactor_scope_narrowed: tests ran in a strict subset of test-bearing modules.
Both are report-only; they NEVER block execution."""

import json

from sag.agent.physical_validator import PhysicalValidator
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH


class ConflictOrch:
    """Minimal fake: manifest + java -version + benign answers elsewhere."""

    def __init__(self, java="11", manifest=None):
        self.java = java
        self.manifest = manifest or {}

    def execute_command(self, cmd, workdir=None, **kwargs):
        if "java -version" in cmd:
            return {"success": True, "exit_code": 0,
                    "output": f'openjdk version "{self.java}.0.1"'}
        if cmd == f"cat {REQUIREMENTS_PATH}":
            if self.manifest:
                return {"success": True, "exit_code": 0, "output": json.dumps(self.manifest)}
            return {"success": False, "exit_code": 1, "output": ""}
        return {"success": True, "exit_code": 0, "output": ""}


def test_collect_jdk_conflict_on_mismatch():
    validator = PhysicalValidator.__new__(PhysicalValidator)
    validator.docker_orchestrator = ConflictOrch(java="11", manifest={"java_version": "17"})
    assert validator._collect_jdk_conflicts() == ["jdk_mismatch"]


def test_no_conflict_when_matching_or_unknown():
    validator = PhysicalValidator.__new__(PhysicalValidator)
    validator.docker_orchestrator = ConflictOrch(java="17", manifest={"java_version": "17"})
    assert validator._collect_jdk_conflicts() == []
    validator.docker_orchestrator = ConflictOrch(java="11", manifest={})  # no requirement
    assert validator._collect_jdk_conflicts() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_jdk_reactor_conflicts.py -v`
Expected: FAIL — `AttributeError: ... has no attribute '_collect_jdk_conflicts'`

- [ ] **Step 3: Implement**

In `physical_validator.py`, add the helper method to `PhysicalValidator`:

```python
    def _collect_jdk_conflicts(self) -> List[str]:
        """jdk_mismatch when the manifest's required JDK != the active one.

        Report-only honesty signal (spec §4): provisioning failures degrade
        here instead of blocking the run. Empty when no requirement is known.
        """
        try:
            from sag.tools.internal.build_preflight import (
                active_java_major,
                read_build_requirements,
            )

            required = (read_build_requirements(self.docker_orchestrator) or {}).get(
                "java_version"
            )
            if not required:
                return []
            active = active_java_major(self.docker_orchestrator)
            if active and active != str(required):
                return ["jdk_mismatch"]
        except Exception as exc:
            logger.debug(f"jdk conflict check skipped: {exc}")
        return []
```

Then in `validate_build_status`, where the conflicts list is assembled
(`:2088`, `conflicts = ["build_modules_incomplete"]` branch and the default
empty list), extend both paths: `conflicts.extend(self._collect_jdk_conflicts())`
just before the result dict is built (the one place both branches converge,
next to the `"conflicts": conflicts` key at `:2100`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_jdk_reactor_conflicts.py tests/test_physical_validator.py tests/test_build_test_verdict.py -q`
Expected: all pass (existing validator fakes return canned empty output for
the two new probes → no requirement → no conflict → no behavior change)

- [ ] **Step 5: Commit**

```bash
git add tests/test_jdk_reactor_conflicts.py src/sag/agent/physical_validator.py
git commit -m "feat(verifier): jdk_mismatch conflict caps verdict at PARTIAL when requirement unmet"
```

---

### Task 9: `reactor_scope_narrowed` verdict conflict

**Files:**
- Modify: `src/sag/agent/physical_validator.py:2704+` (`scan_modules`: add `has_test_sources`)
- Modify: `src/sag/tools/module_metrics.py` (~`:220` summary: add `modules_test_bearing`)
- Modify: `src/sag/tools/report_tool.py` (conflict emission next to `:1592-1626`; status passthrough next to the existing `modules_tested` passthrough at ~`:962`)
- Test: `tests/test_jdk_reactor_conflicts.py` (append)

**Interfaces:**
- Consumes: module records from `scan_modules`; `module_summary` from `assemble_module_metrics` (PR #9's `modules_tested` already present).
- Produces: module record key `has_test_sources: bool`; summary key `modules_test_bearing: int`; snapshot conflict `"reactor_scope_narrowed"` when `0 < modules_tested < modules_test_bearing`.

- [ ] **Step 1: Write the failing tests (append to `tests/test_jdk_reactor_conflicts.py`)**

```python
from sag.tools.module_metrics import assemble_module_metrics


def _metrics(tested_pairs):
    """tested_pairs: list of (path, has_test_sources, tests_total)."""
    return assemble_module_metrics(
        modules=[
            {"path": p, "name": p, "class_count": 5, "jar_count": 1,
             "report_dirs": [], "has_test_sources": bearing}
            for p, bearing, _ in tested_pairs
        ],
        reactor_status={p: "success" for p, _, _ in tested_pairs},
        tests={
            p: {"tests_total": total, "tests_passed": total, "failing_count": 0}
            for p, _, total in tested_pairs if total
        },
        build_systems=["maven"],
        build_error_samples={},
        generated_at="t",
    )


def test_summary_counts_test_bearing_modules():
    metrics = _metrics([("api", True, 10), ("core", True, 0), ("docs", False, 0)])
    s = metrics["module_summary"]
    assert s["modules_test_bearing"] == 2
    assert s["modules_tested"] == 1


def test_scope_narrowed_condition():
    # The report emits reactor_scope_narrowed when 0 < tested < test_bearing.
    s = _metrics([("api", True, 10), ("core", True, 0)])["module_summary"]
    assert 0 < s["modules_tested"] < s["modules_test_bearing"]  # narrow -> conflict fires
    s_full = _metrics([("api", True, 10), ("core", True, 3)])["module_summary"]
    assert s_full["modules_tested"] == s_full["modules_test_bearing"]  # full -> no conflict
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_jdk_reactor_conflicts.py -v -k "bearing or narrowed"`
Expected: FAIL — `KeyError: 'modules_test_bearing'`

- [ ] **Step 3: Implement**

1. `physical_validator.py` `scan_modules` (in the per-module loop, next to the
   `report_dirs` probes):

```python
            tst = self._execute_command_with_logging(
                f"test -d {module_dir}/src/test && echo EXISTS",
                f"checking test sources {rel}",
            )
            has_test_sources = "EXISTS" in (tst.get("output") or "")
```

   and add `"has_test_sources": has_test_sources,` to the appended record.

2. `module_metrics.py`: pass the flag through to `out_modules` rows
   (`"has_test_sources": bool(scan.get("has_test_sources"))` where the row is
   assembled from `scan`), and in the summary dict (~`:220`):

```python
        "modules_test_bearing": sum(
            1 for m in out_modules if m.get("has_test_sources")
        ),
```

3. `report_tool.py`:
   - Status passthrough (next to the existing `modules_tested` passthrough
     added in PR #9, ~`:962-966`):
     `report_snapshot["status"]["modules_test_bearing"] = msum.get("modules_test_bearing")`
   - Conflict emission — directly below the `tests_not_fully_executed` block
     (`:1606-1626`), mirroring its shape:

```python
        # Scope shortfall caps the run at PARTIAL: tests ran in a strict subset
        # of the test-bearing modules (leaf-scoped run in a reactor). Same
        # non-adjudicated conflict mechanism as the two gates above (spec §4).
        if (
            status.get("modules_test_bearing")
            and status.get("modules_tested") is not None
            and 0 < status["modules_tested"] < status["modules_test_bearing"]
        ):
            ev_conflicts = snapshot["evidence_result"].setdefault("conflicts", [])
            if "reactor_scope_narrowed" not in ev_conflicts:
                ev_conflicts.append("reactor_scope_narrowed")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_jdk_reactor_conflicts.py tests/test_build_test_verdict.py tests/test_report_module_metrics.py tests/test_module_metrics.py -q`
Expected: all pass (records without `has_test_sources` default to `False` →
`modules_test_bearing == 0` → condition never fires → zero behavior change
for existing fixtures)

- [ ] **Step 5: Commit**

```bash
git add tests/test_jdk_reactor_conflicts.py src/sag/agent/physical_validator.py src/sag/tools/module_metrics.py src/sag/tools/report_tool.py
git commit -m "feat(verifier): reactor_scope_narrowed conflict when tests cover a strict subset of test-bearing modules"
```

---

### Task 10: Full-suite regression + live integration

**Files:**
- No new code — verification only.

- [ ] **Step 1: Full suite**

Run: `uv run pytest -q`
Expected: everything passes except the known environmental flake
`tests/test_packaging_smoke.py::test_wheel_installs_and_cli_loads` (ensurepip
SIGABRT in a throwaway venv on this host — pre-existing, unrelated; verify it
also fails on a clean `main` checkout before dismissing it).

- [ ] **Step 2: Live integration — regression project**

Run a real setup of commons-vfs with the branch (from the repo root; requires
Docker + `.env` with model credentials):

```bash
uv run sag --project https://github.com/apache/commons-vfs.git
```

Expected: build reads SUCCESS; report shows no `jdk_mismatch` and no
`reactor_scope_narrowed`; modules tested == test-bearing modules; the
condensed line shows reactor-scale counts.

- [ ] **Step 3: Live integration — big-swing project**

```bash
uv run sag --project https://github.com/apache/httpcomponents-client.git
```

Expected: reactor-scale test counts (~2,255 per Billy's benchmark, vs 16 on
old main); `mvn install -fae -DskipTests` then `mvn test -fae` visible at
the reactor root in the trajectory; JDK pre-flight either silent (match) or
narrated once.

- [ ] **Step 4: Commit any test-only adjustments and hand off**

```bash
git add -f docs/superpowers/plans/2026-07-06-java-execution-strategy-fixes.md
git commit -m "docs(plan): java execution-strategy fixes implementation plan"
```

The acceptance gate beyond this plan (spec "Testing & acceptance") is the
23-project benchmark rerun — coordinate with Billy: big-swing projects
(cassandra-java-driver, tapestry-5, jackrabbit, cayenne,
httpcomponents-client) should reach reactor-scale test counts and average
iterations should drop from ~51 toward single digits, with no new false
greens.
