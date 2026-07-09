# Python Project Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** SAG sets up mixed-era Python projects (legacy setup.py → modern pyproject) end-to-end with a verifier as honest as the Java one: evidence ladder (resolve + import + compileall), tri-state verdict, no false greens.

**Architecture:** Mirrors the Java architecture as it exists after PR #12 + the pre-flight/verifier port: `PythonPreflight` joins `JdkPreflight` in `build_preflight.py`; a new internal `python_tool` is wrapped by a `PythonBackend` of the consolidated `BuildTool` (`tools/build/`, the model-facing steered path); the validator gains a Python evidence branch reusing the tri-state verdict and the existing JUnit-XML parser (pytest `--junitxml`). Shared env helpers live in one new `python_env.py` so the setup tool and python_tool never duplicate the installer ladder.

**Tech Stack:** Python 3.12, pytest (`uv run pytest`), Docker orchestrator (`execute_command(cmd, workdir=...) -> {"success", "exit_code", "output"}`), uv (inside the target container) for interpreter provisioning.

**Spec:** `docs/superpowers/specs/2026-07-07-python-project-support-design.md` (approved).
**Base branch:** `feat/preflight-verifier-layer` (the ported pre-flight/verifier layer — this plan CONSUMES its API and MUST be stacked on it, not on `main`).

## Global Constraints

- Never block execution; degrade to verdict conflicts (spec design principle). Retries bounded to **exactly once**. No skip flags.
- Deps installed by the project's OWN declared tool; pip fallback is allowed but must be narrated as a deviation (spec Component 3).
- tox/nox are metadata only — NEVER executed (spec settled decision).
- Wheel build is extra evidence, never required for green (spec settled decision).
- Interpreter policy: newest stable CPython satisfying the declared constraint (spec Component 1).
- Consume the ported API verbatim: `REQUIREMENTS_PATH = "/workspace/.setup_agent/build_requirements.json"`, `read_build_requirements(orch)`, `write_build_requirements(orch, data)`, `PreflightOutcome(matched, active_version, required_version, provisioned, mismatch, narration)` from `src/sag/tools/internal/build_preflight.py`.
- Net-new tests in new dedicated test files; run with `uv run pytest`. TDD per step. No `Co-Authored-By` (or any) trailer. Nothing under `src/sag/web/static/`.
- Line-number anchors below were read on post-#12 `main`; trust names/structure over exact lines.

## File Structure

| File | Role |
|---|---|
| `src/sag/tools/internal/python_env.py` (create) | Shared helpers: requires-python parsing, version-constraint resolution, installer-ladder detection/commands, package discovery |
| `src/sag/tools/internal/build_preflight.py` (modify) | `PythonPreflight` + `classify_python_version_error` |
| `src/sag/tools/internal/python_tool.py` (create) | Internal tool: `setup_env` / `test` / `build` operations |
| `src/sag/tools/build/backends.py` (modify) | `PythonBackend` + `python` markers in `BUILD_MARKERS` |
| `src/sag/tools/build/build_tool.py` (modify) | Register the backend; route the pre-flight by system (python → `PythonPreflight`) |
| `src/sag/agent/agent.py` (modify, `:195-240`) | Construct `PythonTool`, pass to `BuildTool` |
| `src/sag/tools/internal/project_analyzer.py` (modify) | Python analysis depth → manifest keys |
| `src/sag/tools/internal/project_setup_tool.py` (modify, python branches `:456`, `:1602`) | Real python setup via `python_env` helpers |
| `src/sag/agent/physical_validator.py` (modify) | `_verify_python_build` evidence ladder; pytest report discovery; `python_version_mismatch` conflict; static-count fallback |
| Tests (create) | `tests/test_python_requirements.py`, `tests/test_python_preflight.py`, `tests/test_python_tool.py`, `tests/test_build_tool_python_backend.py`, `tests/test_python_verifier.py` |

**Key existing anchors:** `BUILD_MARKERS` (`backends.py:12-15`); `MavenBackend.VERBS` shape (`backends.py:20-54`); `BuildTool.execute`/`_detect_system`/`_envelope` (`build_tool.py:40-146`); legacy-tool construction (`agent.py:201-206`) and `BuildTool(...)` (`agent.py:232`); Maven/Gradle fingerprint branch to extend (`physical_validator.py:1920-1941`); report-dir discovery (`physical_validator.py:789-822`); `static_test_count` resolution from env summary (`physical_validator.py:2361-2364`); analyzer Python label (`project_analyzer.py:281`); env overlay reference (`env_overlay.py`, register signature).

---

### Task 1: `python_env.py` — requires-python parsing + version resolution

**Files:**
- Create: `src/sag/tools/internal/python_env.py`
- Test: `tests/test_python_requirements.py`

**Interfaces (later tasks rely on these exact names):**
- `parse_requires_python(text: str) -> Optional[str]` — extracts the raw constraint from pyproject/setup.py/setup.cfg content (caller passes file content + which file it is via `source` kwarg; simplest: three small extractors `requires_python_from_pyproject/setup_py/setup_cfg`).
- `resolve_python_version(constraint: Optional[str], candidates: list[str] = SUPPORTED_PYTHONS) -> Optional[str]` — newest candidate satisfying every specifier; `None` constraint → `None` (caller keeps container default). `SUPPORTED_PYTHONS = ["3.8","3.9","3.10","3.11","3.12","3.13"]`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_python_requirements.py
"""requires-python parsing + newest-satisfying resolution (spec Component 1)."""

from sag.tools.internal.python_env import (
    SUPPORTED_PYTHONS,
    requires_python_from_pyproject,
    requires_python_from_setup_cfg,
    requires_python_from_setup_py,
    resolve_python_version,
)


def test_pyproject_requires_python():
    content = '[project]\nname = "x"\nrequires-python = ">=3.9,<3.13"\n'
    assert requires_python_from_pyproject(content) == ">=3.9,<3.13"


def test_setup_py_python_requires():
    content = 'setup(name="x", python_requires=">=3.8", packages=[])'
    assert requires_python_from_setup_py(content) == ">=3.8"


def test_setup_cfg_python_requires():
    content = "[options]\npython_requires = >=3.7,!=3.9.*\n"
    assert requires_python_from_setup_cfg(content) == ">=3.7,!=3.9.*"


def test_resolution_policy_is_newest_satisfying():
    assert resolve_python_version(">=3.9,<3.13") == "3.12"
    assert resolve_python_version(">=3.8") == SUPPORTED_PYTHONS[-1]
    assert resolve_python_version("<3.10") == "3.9"
    assert resolve_python_version("~=3.10.0") == "3.10"
    assert resolve_python_version(">=3.7,!=3.9.*") != "3.9"


def test_unresolvable_returns_none():
    assert resolve_python_version(None) is None
    assert resolve_python_version("") is None
    assert resolve_python_version(">=4.0") is None      # nothing satisfies
    assert resolve_python_version("${py.version}") is None  # templated garbage
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_python_requirements.py -v` → ImportError.

- [ ] **Step 3: Implement**

```python
# src/sag/tools/internal/python_env.py
"""Shared Python-environment helpers: requirement parsing, version resolution,
installer-ladder detection. Used by the analyzer, python_tool, and the setup
tool so the ladder exists exactly once (spec Components 1-3)."""

import re
from typing import List, Optional

SUPPORTED_PYTHONS = ["3.8", "3.9", "3.10", "3.11", "3.12", "3.13"]

_PYPROJECT_RP = re.compile(r'requires-python\s*=\s*["\']([^"\']+)["\']')
_SETUP_PY_RP = re.compile(r'python_requires\s*=\s*["\']([^"\']+)["\']')
_SETUP_CFG_RP = re.compile(r'^\s*python_requires\s*=\s*(.+)$', re.MULTILINE)


def requires_python_from_pyproject(content: str) -> Optional[str]:
    m = _PYPROJECT_RP.search(content or "")
    return m.group(1).strip() if m else None


def requires_python_from_setup_py(content: str) -> Optional[str]:
    m = _SETUP_PY_RP.search(content or "")
    return m.group(1).strip() if m else None


def requires_python_from_setup_cfg(content: str) -> Optional[str]:
    m = _SETUP_CFG_RP.search(content or "")
    return m.group(1).strip() if m else None


def _ver(v: str) -> tuple:
    return tuple(int(p) for p in v.split(".")[:2])


def _satisfies(candidate: str, spec: str) -> bool:
    """Minimal PEP-440 subset for major.minor candidates: >=, <=, ==, !=, ~=, <, >.
    Wildcards (3.9.*) compare on the major.minor prefix. Unknown syntax -> False
    (unresolvable is honest; the caller keeps the container default)."""
    spec = spec.strip()
    m = re.match(r"^(>=|<=|==|!=|~=|<|>)\s*(\d+(?:\.\d+)?)(?:\.\d+|\.\*)?$", spec)
    if not m:
        return False
    op, rhs = m.group(1), _ver(m.group(2))
    c = _ver(candidate)
    if op == ">=":
        return c >= rhs
    if op == "<=":
        return c <= rhs
    if op == "<":
        return c < rhs
    if op == ">":
        return c > rhs
    if op == "==":
        return c == rhs
    if op == "!=":
        return c != rhs
    if op == "~=":  # compatible release on major.minor: same as == at this granularity
        return c == rhs
    return False


def resolve_python_version(
    constraint: Optional[str], candidates: List[str] = SUPPORTED_PYTHONS
) -> Optional[str]:
    """Newest candidate satisfying EVERY comma-separated specifier, or None."""
    if not constraint or "${" in constraint:
        return None
    specs = [s for s in (p.strip() for p in constraint.split(",")) if s]
    if not specs:
        return None
    for candidate in reversed(candidates):
        if all(_satisfies(candidate, s) for s in specs):
            return candidate
    return None
```

- [ ] **Step 4: Verify pass** — `uv run pytest tests/test_python_requirements.py -v` → all pass.
- [ ] **Step 5: Commit** — `git add tests/test_python_requirements.py src/sag/tools/internal/python_env.py && git commit -m "feat(python): requires-python parsing + newest-satisfying version resolution"`

---

### Task 2: Installer ladder + package discovery + analyzer wiring → manifest

**Files:**
- Modify: `src/sag/tools/internal/python_env.py`
- Modify: `src/sag/tools/internal/project_analyzer.py` (Python analysis + manifest persistence; the manifest hook from the ported branch is the model)
- Test: `tests/test_python_requirements.py` (append)

**Interfaces:**
- `detect_installer(files_present: set[str]) -> dict` → `{"installer": "poetry"|"pipenv"|"pip", "commands": [<shell strings using {venv} and {dir} placeholders>], "source": <marker file>}` implementing the faithfulness ladder: `poetry.lock`→poetry (`poetry install`), `Pipfile.lock`→pipenv (`pipenv install --dev`), `pyproject.toml`→`{venv}/bin/pip install -e '.[test]' || {venv}/bin/pip install -e .` , `requirements*.txt`→`{venv}/bin/pip install -r <each>` (requirements.txt first), `setup.py`→`{venv}/bin/pip install -e .`
- `discover_packages(orchestrator, project_dir) -> list[str]` — top-level import packages via src-layout (`src/<pkg>/__init__.py`) then flat-layout (`<pkg>/__init__.py`, excluding `tests`, `docs`, `examples`).
- Manifest gains: `python_version` (resolved), `python_constraint` (raw), `python_installer`, `python_install_commands`, `python_packages`, `python_venv` (`<project_dir>/.venv`), `has_c_extensions` (bool: `ext_modules` in setup.py or `[tool.setuptools]`/cython markers), `test_hints` (`{"pytest_args": str|None, "test_deps": [..]}` scraped from tox.ini `[testenv] deps/commands` and setup.cfg `[options.extras_require]` — READ ONLY, never executed).

- [ ] **Step 1: failing tests** — table tests for `detect_installer` (each ladder rung + precedence: poetry.lock beats pyproject; requirements.txt ordering), one for `discover_packages` with a scripted orchestrator (src-layout and flat), one asserting analyzer persistence: after `_analyze_python_project` (new analyzer method) runs against a scripted orchestrator whose files are `pyproject.toml` (`requires-python = ">=3.9"`) + `src/foo/__init__.py`, the dict passed to `write_build_requirements` (monkeypatch it) contains `python_version == "3.13"`, `python_installer == "pip"`, `python_packages == ["foo"]`, `python_venv` endswith `/.venv`.
- [ ] **Step 2: verify failure.**
- [ ] **Step 3: Implement** — helpers in `python_env.py`; in the analyzer, extend the Python branch (label at `project_analyzer.py:281`) with `_analyze_python_project(project_path, analysis)` that reads the marker files (`cat`, untruncated like PR #12's pom reads), fills `analysis["python_config"]`, and extends the SAME manifest write the ported branch added (java keys stay; python keys added).
- [ ] **Step 4: verify pass + regression** — `uv run pytest tests/test_python_requirements.py tests/test_root_shape_policy.py -q` (the manifest test file from the port must stay green).
- [ ] **Step 5: Commit** — `git commit -m "feat(analyzer): python installer ladder, package discovery, and manifest keys"`

---

### Task 3: `PythonPreflight` + python version-error classifier

**Files:**
- Modify: `src/sag/tools/internal/build_preflight.py`
- Test: `tests/test_python_preflight.py`

**Interfaces:**
- `class PythonPreflight: __init__(self, orchestrator); run(self, required_version: Optional[str], constraint: Optional[str] = None, source: str = "unknown") -> PreflightOutcome` (reuses the ported `PreflightOutcome` verbatim; `mismatch=True` maps to the `python_version_mismatch` conflict in Task 6).
- `active_python_version(orchestrator) -> Optional[str]` (major.minor from `python3 --version`).
- `classify_python_version_error(output: str) -> Optional[str]` — extracts the needed major.minor from pip's `Requires-Python` rejections (e.g. `requires a different Python: 3.8.10 not in '>=3.10'` → resolve `>=3.10` via `resolve_python_version`); returns `None` otherwise.

**Behavior (spec Component 2):** requirement satisfied (or none) → no-op. Mismatch → provision ladder: uv present? else install (`curl -LsSf https://astral.sh/uv/install.sh | sh` + PATH) → `uv python install {X.Y}` → `uv venv --python {X.Y} {venv}` → register overlay (`EnvOverlayStore.register("python", f"{venv}/bin/python", version=..., source="python_preflight", env={"VIRTUAL_ENV": venv}, path_prepend=[f"{venv}/bin"], activate=True)`) → narrate `[pre-flight] Required: Python 3.11 (source: requires-python). Active: 3.8. → uv-provisioned 3.11, venv at ...`. Ladder exhausted → `mismatch=True`, narrated, NEVER raises (wrap like `JdkPreflight.run`). apt fallback (`apt-get install -y python3.{minor}-venv python3.{minor}`) between uv and degradation.

- [ ] **Step 1: failing tests** — mirror `tests/test_build_preflight.py`'s scripted-orchestrator style (read it first; it's on this branch): match→no-op (no uv commands issued); mismatch→provision narrated; ladder-exhausted→`mismatch=True` + "could not provision" narration, never raises; `active_python_version` parses `Python 3.11.7` → `"3.11"`; classifier table (pip Requires-Python message → `"3.10"`; unrelated pip error → None; `SyntaxError: invalid syntax` alone → None — too ambiguous to act on, document why in the test).
- [ ] **Step 2: verify failure.** — `uv run pytest tests/test_python_preflight.py -v`
- [ ] **Step 3: Implement** in `build_preflight.py` below `JdkPreflight`, reusing `_register_overlay`-style helper (add `tool="python"` support or a parallel `_register_python_overlay`).
- [ ] **Step 4: verify pass** + `uv run pytest tests/test_build_preflight.py -q` (ported tests stay green).
- [ ] **Step 5: Commit** — `git commit -m "feat(preflight): PythonPreflight with uv->apt ladder + pip Requires-Python classifier"`

---

### Task 4: `python_tool.py` — setup_env / test / build

**Files:**
- Create: `src/sag/tools/internal/python_tool.py`
- Test: `tests/test_python_tool.py`

**Interfaces:**
- `class PythonTool(BaseTool)` with `execute(self, operation: str, working_directory: str = "/workspace", args: str = None, timeout: int = 600) -> ToolResult`; operations `setup_env` | `test` | `build` | `compile`. Task 5 wraps these as backend verbs (`deps`→`setup_env`, `test`→`test`, `package`/`install`→`build`, `compile`→ runs `compileall` over the package and reports the ratio — the evidence generator).
- Constant `PYTEST_REPORT_DIR = "/workspace/.setup_agent/pytest-reports"` and `COLLECTED_JSON = "/workspace/.setup_agent/pytest_collected.json"` (Task 6 reads both).

**Behavior:**
- `setup_env`: `PythonPreflight.run(...)` from the manifest first (narration prepended, same pattern as the ported build tools); create venv if missing (`python -m venv .venv` on the preflight interpreter / `uv venv`); run the manifest's `python_install_commands` in order; a failed poetry/pipenv command falls back to the pip rung, narrated: `[deviation] poetry install failed; fell back to pip install -e . — setup docs must list the fallback`. Bounded retry: a failure classified by `classify_python_version_error` re-provisions and reruns **once**.
- `test`: `.venv/bin/python -m pytest --collect-only -q` → parse the trailing `N tests collected` line → write `{"collected": N}` to `COLLECTED_JSON`; then `.venv/bin/python -m pytest {pytest_args} --junitxml={PYTEST_REPORT_DIR}/pytest-{epoch}.xml`; output = pytest tail; never re-runs on test failures (exit 1 with failures is an HONEST result, not an error).
- `build`: `.venv/bin/python -m build --wheel` (installing `build` into the venv first); failure returns success=False but metadata `{"evidence_only": true}` — callers must not redden a verdict on it (spec: wheel never required).
- `compile`: `.venv/bin/python -m compileall -q <pkg dirs>` and report written/failed counts.

- [ ] **Step 1: failing tests** — scripted orchestrator (record commands): `setup_env` runs preflight then install commands in ladder order and narrates a pip fallback when the poetry command fails; `test` writes COLLECTED_JSON with the parsed count and passes `--junitxml` under PYTEST_REPORT_DIR; test failures do NOT trigger a rerun (exactly one pytest execution command); `build` failure carries `evidence_only` metadata.
- [ ] **Step 2: verify failure.** `uv run pytest tests/test_python_tool.py -v`
- [ ] **Step 3: Implement** (~200 lines; mirror the internal maven_tool's narration/preamble pattern from this branch).
- [ ] **Step 4: verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(tools): python_tool with faithful installer ladder, collect-only denominator, junitxml runs"`

---

### Task 5: `PythonBackend` + BuildTool/agent wiring

**Files:**
- Modify: `src/sag/tools/build/backends.py`, `src/sag/tools/build/build_tool.py`, `src/sag/agent/agent.py:201-240`
- Test: `tests/test_build_tool_python_backend.py`

**Interfaces:**
- `BUILD_MARKERS` gains `"python": ("pyproject.toml", "setup.py", "requirements.txt", "Pipfile")` AFTER maven/gradle (a JVM repo with a stray requirements.txt must stay JVM).
- `class PythonBackend: VERBS = {"deps": "setup_env", "compile": "compile", "test": "test", "package": "build", "install": "build"}; __init__(self, python_tool); run(verb, args, working_directory, timeout) -> ToolResult` (delegates like `MavenBackend`, `backends.py:20-54`).
- `BuildTool.__init__` accepts `python_tool=None` → registers the backend; `BuildTool` pre-flight routing (added by the port): system `python` → skip `JdkPreflight` (PythonPreflight already runs inside `python_tool.setup_env`; do NOT run it twice — document this in a comment at the routing site).

- [ ] **Step 1: failing tests** — marker priority (dir with pom.xml + requirements.txt → maven; only pyproject → python); verb delegation table (each backend verb reaches `python_tool.execute` with the mapped operation); `build(action="test")` on a python dir produces the envelope with `facts["system"] == "python"`; agent wiring smoke: constructing the tool set registers a python backend (import-level test of `agent.py` wiring may be heavy — acceptable alternative: direct `BuildTool(orchestrator, python_tool=FakePythonTool())` construction test, plus a grep-level assertion is NOT a test — skip agent.py test, verify by reading).
- [ ] **Step 2: verify failure.**
- [ ] **Step 3: Implement** — backend class (mirror MavenBackend), `BUILD_MARKERS` entry, facade registration + schema description note ("python: deps installs into ./.venv via the project's own tool"), `agent.py` construction (`python_tool = PythonTool(self.orchestrator)` beside `maven_tool`, passed to `BuildTool(...)`).
- [ ] **Step 4: verify pass** + `uv run pytest tests/ -q -k "build_tool"` (all BuildTool suites green).
- [ ] **Step 5: Commit** — `git commit -m "feat(build): python backend on the consolidated build tool"`

---

### Task 6: Validator — evidence ladder, report discovery, conflicts

**Files:**
- Modify: `src/sag/agent/physical_validator.py`
- Test: `tests/test_python_verifier.py`

**Interfaces:**
- `_verify_python_build(project_dir: str) -> dict` returning `{"venv_exists": bool, "pip_check_clean": bool, "imports_ok": bool|None, "import_failures": [str], "compileall_coverage": float|None, "ext_modules_ok": bool|None, "success": bool, "complete": bool, "reason": str}`.
- Conflict string: `"python_version_mismatch"` (mirror of `jdk_mismatch`, emitted from the same `_collect_jdk_conflicts` site — rename that helper to `_collect_env_conflicts` and cover both, preserving the `jdk_mismatch` string).

**Behavior (spec Component 4):**
- Tri-state mapping in `validate_build_status`: extend the fingerprint branch (`physical_validator.py:1920-1941`) with `elif build_system == "python":` filling evidence from `_verify_python_build`; decision mapping: `imports_ok` False or no venv → BLOCKED (`success=False`); imports ok but (`pip_check_clean` False or `compileall_coverage < self.build_coverage_threshold` or `ext_modules_ok` False) → PARTIAL (`success=True, complete=False`, reason names the failed rung); all green → SUCCESS.
- Evidence commands (per rung): `test -d {dir}/.venv`; `{dir}/.venv/bin/pip check`; `{dir}/.venv/bin/python -c "import <pkg>"` per manifest `python_packages`; `{dir}/.venv/bin/python -m compileall -q <pkg src>` then coverage = `.pyc` count under `__pycache__` / `.py` count (tests/docs/examples excluded — reuse the exclusion globs from `scan_modules`' style); `.so` presence when manifest `has_c_extensions`.
- Report discovery: `parse_test_reports` report-dir candidates (`:789-822`) gain `/workspace/.setup_agent/pytest-reports` — the XML inside is standard JUnit XML; the existing parser must need NO changes (that's the test).
- Static count: where `static_test_count` resolves from env summary (`:2361-2364`), add a fallback read of `COLLECTED_JSON` (`/workspace/.setup_agent/pytest_collected.json`) when the env summary has none and the build system is python — this feeds the existing `tests_not_fully_executed` gate unchanged.

- [ ] **Step 1: failing tests** — scripted orchestrator: each rung's failure produces the right tri-state + reason (5 cases: no venv → blocked; import fails → blocked; pip check dirty → partial; coverage 0.5 < threshold → partial; all green → success/complete); a real minimal JUnit XML written by pytest (`<testsuite tests="3" failures="1" ...>` fixture string) round-trips through `parse_test_reports` when placed in the pytest-reports dir (fake `find`/`cat` responses); `python_version_mismatch` emitted when manifest `python_version` ≠ active (mirror the ported `test_jdk_reactor_conflicts.py` — read it first); collected-json fallback feeds `static_test_count`.
- [ ] **Step 2: verify failure.** `uv run pytest tests/test_python_verifier.py -v`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: verify pass + regression** — `uv run pytest tests/test_python_verifier.py tests/test_jdk_reactor_conflicts.py tests/test_physical_validator.py tests/test_build_test_verdict.py -q`.
- [ ] **Step 5: Commit** — `git commit -m "feat(verifier): python evidence ladder (venv/pip-check/import/compileall), junitxml reuse, python_version_mismatch"`

---

### Task 7: Setup-tool python branch via shared helpers

**Files:**
- Modify: `src/sag/tools/internal/project_setup_tool.py` (python branches at `:456` and `:1602`; read both first)
- Test: `tests/test_python_tool.py` (append)

- [ ] **Step 1: failing test** — the setup tool's python path issues the SAME ladder commands as `detect_installer` (call `_install_dependencies_for_project_type` — or the real method name at `:456` — with a scripted orchestrator over a poetry-locked project; assert `poetry install` is attempted, venv created first, and NO maven/JDK commands run).
- [ ] **Step 2: verify failure.**
- [ ] **Step 3: Implement** — replace the "not implemented" python path with: `PythonPreflight` (manifest) → venv → `python_env.detect_installer` commands → register overlay. No duplicated ladder strings: import from `python_env`.
- [ ] **Step 4: verify pass + regression** — `uv run pytest tests/test_python_tool.py tests/test_project_setup_tool.py -q`.
- [ ] **Step 5: Commit** — `git commit -m "feat(setup): real python environment setup via the shared installer ladder"`

---

### Task 8: Full suite + live probes

- [ ] **Step 1:** `uv run pytest -q -p no:cacheprovider` — everything green except the known `ensurepip` env flake (verify it's the only failure).
- [ ] **Step 2 (live, needs Docker + `.env`):** modern-pyproject probe — `uv run sag --project https://github.com/psf/requests.git` → expect: venv + pip -e install, pytest reactor-scale counts, build verdict from the evidence ladder (SUCCESS or honest PARTIAL with a named rung), no `python_version_mismatch`.
- [ ] **Step 3 (live):** legacy probe — a setup.py-era repo from Billy's dataset shortlist (fallback: `https://github.com/paramiko/paramiko.git`) → expect the pip -e rung, honest verdict.
- [ ] **Step 4 (live):** C-extension probe — `https://github.com/yaml/pyyaml.git` → expect `.so` evidence checked; wheel failure (if any) must NOT redden the verdict.
- [ ] **Step 5:** Update `docs/superpowers/plans/2026-07-09-python-project-support.md` checkboxes, commit test-only fixes, and stop — merge/PR only on user approval. Acceptance milestone beyond this plan: Billy's Python benchmark dataset.
