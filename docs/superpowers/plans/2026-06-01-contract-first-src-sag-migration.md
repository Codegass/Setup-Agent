# Contract-First `src/sag` Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move Setup-Agent runtime code into the `sag` package and lock the package/tool/result/state contracts with focused tests.

**Architecture:** Create a single `src/sag/` runtime namespace and update all internal imports to absolute `sag.*` paths. Keep current module responsibilities intact; only fix contract bugs needed for installation, import, tool schema, result data, state guidance, and report return behavior.

**Tech Stack:** Python 3.10+, Hatchling, Click, Pydantic, pytest, uv, standard library packaging smoke tests.

---

## Spec Reference

- Design spec: `docs/superpowers/specs/2026-06-01-contract-first-src-sag-migration-design.md`
- Important constraint: commit messages must not include Co-Authorship or similar authorship trailers.

## File Structure

### Runtime Package

- Create: `src/sag/__init__.py`
- Move: `main.py` -> `src/sag/main.py`
- Move directory: `agent/` -> `src/sag/agent/`
- Move directory: `config/` -> `src/sag/config/`
- Move directory: `docker_orch/` -> `src/sag/docker_orch/`
- Move directory: `reporting/` -> `src/sag/reporting/`
- Move directory: `testcases/` -> `src/sag/testcases/`
- Move directory: `tools/` -> `src/sag/tools/`
- Move directory: `ui/` -> `src/sag/ui/`

### Project Metadata

- Modify: `pyproject.toml`
  - Change script entrypoint to `sag.main:cli`
  - Configure Hatchling to package `src/sag`
  - Add pytest config if needed for `src` layout

### Tests

- Create: `tests/test_import_smoke.py`
- Create: `tests/test_tool_contracts.py`
- Create: `tests/test_result_state_contracts.py`
- Create: `tests/test_report_contract.py`
- Create: `tests/test_static_import_guard.py`

### Documentation

- Modify: `README.md`
  - Update any module-path examples or references if the implementation changes user-visible paths
  - Keep CLI usage examples as `uv run sag ...`

## Task 1: Add Failing Packaging And Import Smoke Tests

**Files:**
- Create: `tests/test_import_smoke.py`
- Modify later: `pyproject.toml`
- Create later: `src/sag/__init__.py`
- Create later: `src/sag/main.py`

- [ ] **Step 1: Write failing import smoke tests**

Create `tests/test_import_smoke.py`:

```python
import importlib


def test_import_sag_package():
    module = importlib.import_module("sag")
    assert module is not None


def test_import_core_runtime_modules():
    module_names = [
        "sag.main",
        "sag.agent.react_engine",
        "sag.tools.base",
        "sag.reporting",
        "sag.testcases.catalog",
        "sag.ui.events",
    ]

    for module_name in module_names:
        assert importlib.import_module(module_name) is not None


def test_cli_object_loads():
    main = importlib.import_module("sag.main")
    assert hasattr(main, "cli")
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/test_import_smoke.py -v
```

Expected: FAIL because `sag` package does not exist yet.

- [ ] **Step 3: Add `src/sag` package skeleton**

Create `src/sag/__init__.py`:

```python
"""Setup-Agent runtime package."""
```

Do not add compatibility imports for old top-level packages.

- [ ] **Step 4: Move `main.py` into `src/sag/main.py`**

Move the file mechanically. Do not edit behavior yet.

- [ ] **Step 5: Move runtime directories under `src/sag/`**

Move these directories mechanically:

```text
agent/
config/
docker_orch/
reporting/
testcases/
tools/
ui/
```

Result:

```text
src/sag/agent/
src/sag/config/
src/sag/docker_orch/
src/sag/reporting/
src/sag/testcases/
src/sag/tools/
src/sag/ui/
```

- [ ] **Step 6: Update `pyproject.toml` package and entrypoint**

Change:

```toml
[project.scripts]
sag = "sag.main:cli"

[tool.hatch.build.targets.wheel]
packages = ["src/sag"]
```

If pytest cannot resolve `src` imports during local tests, add:

```toml
[tool.pytest.ini_options]
pythonpath = ["src"]
```

- [ ] **Step 7: Run import smoke tests again**

Run:

```bash
uv run pytest tests/test_import_smoke.py -v
```

Expected: FAIL with import errors from old internal imports. Those are fixed in Task 2.

- [ ] **Step 8: Commit task 1**

Commit only the package skeleton, mechanical moves, `pyproject.toml`, and import smoke test.

```bash
git add pyproject.toml src/sag tests/test_import_smoke.py
git add -u main.py agent config docker_orch reporting testcases tools ui
git commit -m "Move runtime package under src sag"
```

Do not add Co-Authorship.

## Task 2: Convert Runtime Imports To `sag.*`

**Files:**
- Modify: `src/sag/main.py`
- Modify: `src/sag/agent/**/*.py`
- Modify: `src/sag/tools/**/*.py`
- Modify: `src/sag/config/**/*.py`
- Modify: `src/sag/docker_orch/**/*.py`
- Modify: `src/sag/reporting/**/*.py`
- Modify: `src/sag/testcases/**/*.py`
- Modify: `src/sag/ui/**/*.py`
- Create: `tests/test_static_import_guard.py`

- [ ] **Step 1: Write failing static import guard**

Create `tests/test_static_import_guard.py`:

```python
from pathlib import Path


RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "src" / "sag"

DISALLOWED_IMPORT_PREFIXES = (
    "from agent",
    "import agent",
    "from config",
    "import config",
    "from docker_orch",
    "import docker_orch",
    "from reporting",
    "import reporting",
    "from testcases",
    "import testcases",
    "from tools",
    "import tools",
    "from ui",
    "import ui",
)


def test_runtime_code_does_not_use_old_absolute_imports():
    offenders = []

    for path in RUNTIME_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(DISALLOWED_IMPORT_PREFIXES):
                offenders.append(f"{path.relative_to(RUNTIME_ROOT)}:{line_number}: {stripped}")

    assert offenders == []
```

- [ ] **Step 2: Run guard to verify it fails**

Run:

```bash
uv run pytest tests/test_static_import_guard.py -v
```

Expected: FAIL listing old absolute imports.

- [ ] **Step 3: Update imports in runtime modules**

Use a mechanical but reviewed migration:

```python
from sag.agent.agent import SetupAgent
from sag.config import Config
from sag.docker_orch.orch import DockerOrchestrator
from sag.tools.base import BaseTool, ToolResult
from sag.ui.events import UIEventEmitter
from sag.reporting import render_condensed_summary
from sag.testcases.catalog import TestCaseCatalog
```

Use absolute `sag.*` imports for runtime package-to-package dependencies. Do not keep old absolute imports, and do not introduce relative imports for cross-package dependencies such as agent-to-tools, tools-to-ui, or tools-to-reporting. Existing local sibling relative imports may remain only when they stay within the same package directory and do not conflict with the static import guard.

- [ ] **Step 4: Run import and static tests**

Run:

```bash
uv run pytest tests/test_import_smoke.py tests/test_static_import_guard.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit task 2**

```bash
git add src/sag tests/test_static_import_guard.py
git commit -m "Update runtime imports to sag namespace"
```

Do not add Co-Authorship.

## Task 3: Stabilize Tool Schema Contract

**Files:**
- Modify: `src/sag/tools/base.py`
- Modify: `src/sag/agent/react_engine.py`
- Modify as needed: tool subclasses under `src/sag/tools/`
- Create: `tests/test_tool_contracts.py`

- [ ] **Step 1: Write failing tool schema tests**

Create `tests/test_tool_contracts.py`:

```python
from sag.tools.base import BaseTool, ToolResult
from sag.agent.react_engine import ReActEngine
from sag.tools.report_tool import ReportTool


class ExampleTool(BaseTool):
    def __init__(self):
        super().__init__("example", "Example tool")

    def execute(self, command: str) -> ToolResult:
        return ToolResult(success=True, output=command)


def test_base_tool_exposes_public_parameter_schema():
    tool = ExampleTool()
    schema = tool.get_parameter_schema()

    assert schema["type"] == "object"
    assert "command" in schema["properties"]
    assert schema["required"] == ["command"]


def test_react_engine_uses_public_tool_schema_without_empty_fallback():
    tool = ExampleTool()
    engine = ReActEngine.__new__(ReActEngine)
    engine.tools = {"example": tool}
    engine.is_claude_model = False

    schema = ReActEngine._build_tools_schema(engine)

    assert schema[0]["function"]["name"] == "example"
    assert "command" in schema[0]["function"]["parameters"]["properties"]


def test_real_tool_custom_schema_is_preserved():
    schema = ReportTool().get_parameter_schema()

    assert schema["type"] == "object"
    assert schema["properties"]["action"]["enum"] == ["generate"]
    assert "status" in schema["properties"]
    assert "details" in schema["properties"]
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/test_tool_contracts.py -v
```

Expected: FAIL because `BaseTool` does not expose `get_parameter_schema()`.

- [ ] **Step 3: Implement public schema API in `BaseTool`**

In `src/sag/tools/base.py`, add:

```python
def get_parameter_schema(self) -> Dict[str, Any]:
    """Return this tool's JSON parameter schema for function calling."""
    custom_schema_method = getattr(self, "_get_parameters_schema", None)
    if custom_schema_method is not None:
        return custom_schema_method()
    return self._parameter_schema
```

If this causes recursion because the method is inherited from `BaseTool`, use a private helper instead:

```python
def get_parameter_schema(self) -> Dict[str, Any]:
    """Return this tool's JSON parameter schema for function calling."""
    override = type(self).__dict__.get("_get_parameters_schema")
    if override is not None:
        return override(self)
    return self._parameter_schema
```

- [ ] **Step 4: Update `ReActEngine._build_tools_schema()`**

In `src/sag/agent/react_engine.py`, consume the public method:

```python
schema = tool.get_parameter_schema()
```

Do not fall back to `{}` for tools that inherit `BaseTool`.

- [ ] **Step 5: Run focused tests**

Run:

```bash
uv run pytest tests/test_tool_contracts.py tests/test_import_smoke.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit task 3**

```bash
git add src/sag/tools/base.py src/sag/agent/react_engine.py tests/test_tool_contracts.py
git commit -m "Stabilize tool schema contract"
```

Do not add Co-Authorship.

## Task 4: Stabilize ToolResult And Agent State Contracts

**Files:**
- Modify: `src/sag/tools/base.py`
- Modify: `src/sag/agent/agent_state_evaluator.py`
- Modify as needed: `src/sag/agent/react_engine.py`
- Create: `tests/test_result_state_contracts.py`

- [ ] **Step 1: Write failing result/state contract tests**

Create `tests/test_result_state_contracts.py`:

```python
from types import SimpleNamespace

from sag.agent.agent_state_evaluator import AgentStateEvaluator
from sag.agent.agent_state_evaluator import AgentStateAnalysis, AgentStatus
from sag.tools.base import ToolResult


class FakeContextManager:
    current_task_id = None

    def load_trunk_context(self):
        return {
            "todo_list": [
                {"id": "task_1", "description": "Clone repository", "status": "pending"}
            ]
        }


def test_tool_result_preserves_declared_raw_data():
    result = ToolResult(
        success=True,
        output="ok",
        raw_data={"full_report": "report text", "report_snapshot": {"status": "success"}},
    )

    assert result.raw_data["full_report"] == "report text"
    assert result.model_dump()["raw_data"]["report_snapshot"]["status"] == "success"


def test_agent_status_has_stuck_state():
    assert AgentStatus.STUCK.value == "stuck"


def test_agent_state_analysis_uses_declared_guidance_fields():
    analysis = AgentStateAnalysis(
        status=AgentStatus.STUCK,
        needs_guidance=True,
        guidance_message="Use project_analyzer",
        guidance_priority=10,
    )

    assert analysis.guidance_message == "Use project_analyzer"
    assert analysis.guidance_priority == 10


def test_agent_state_evaluator_guidance_branch_uses_declared_fields():
    evaluator = AgentStateEvaluator(FakeContextManager())

    analysis = evaluator._check_ghost_state([SimpleNamespace(tool_name="maven")])

    assert analysis.status == AgentStatus.STUCK
    assert analysis.needs_guidance is True
    assert "GHOST STATE" in analysis.guidance_message
    assert analysis.guidance_priority == 10
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/test_result_state_contracts.py -v
```

Expected: FAIL because `ToolResult.raw_data` and `AgentStatus.STUCK` are missing.

- [ ] **Step 3: Add `raw_data` to `ToolResult`**

In `src/sag/tools/base.py`, change `ToolResult`:

```python
from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    success: bool
    output: str
    error: Optional[str] = None
    error_code: Optional[str] = None
    suggestions: List[str] = Field(default_factory=list)
    documentation_links: List[str] = Field(default_factory=list)
    raw_output: Optional[str] = None
    raw_data: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
```

Use `Field(default_factory=...)` for mutable defaults while editing this model.

- [ ] **Step 4: Add `AgentStatus.STUCK`**

In `src/sag/agent/agent_state_evaluator.py`:

```python
class AgentStatus(str, Enum):
    PROCEEDING = "proceeding"
    STUCK = "stuck"
    STUCK_REPETITION = "stuck_repetition"
    ...
```

- [ ] **Step 5: Replace undeclared state fields**

In `src/sag/agent/agent_state_evaluator.py`, replace all `AgentStateAnalysis(...)` calls using `guidance=` and `priority=` with:

```python
guidance_message=...
guidance_priority=...
```

Do not change the guidance message content unless needed for syntax correctness.

- [ ] **Step 6: Run focused tests**

Run:

```bash
uv run pytest tests/test_result_state_contracts.py tests/test_import_smoke.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit task 4**

```bash
git add src/sag/tools/base.py src/sag/agent/agent_state_evaluator.py src/sag/agent/react_engine.py tests/test_result_state_contracts.py
git commit -m "Stabilize result and state contracts"
```

Do not add Co-Authorship.

## Task 5: Stabilize ReportTool Structured Return Contract

**Files:**
- Modify: `src/sag/tools/report_tool.py`
- Create: `tests/test_report_contract.py`

- [ ] **Step 1: Write failing report contract test**

Create `tests/test_report_contract.py`:

```python
from sag.tools.report_tool import ReportTool


def test_report_tool_returns_full_report_in_raw_data(monkeypatch):
    tool = ReportTool()

    monkeypatch.setattr(tool, "_validate_context_prerequisites", lambda: {"valid": True})
    monkeypatch.setattr(
        tool,
        "_generate_comprehensive_report",
        lambda summary, status, details: (
            "# Full Report",
            "success",
            "setup-report-test.md",
            {
                "build_success": True,
                "test_success": True,
                "physical_validation": {"test_analysis": {"pass_rate": 100, "total_tests": 1, "passed_tests": 1}},
            },
            {"status": "success"},
        ),
    )
    monkeypatch.setattr(
        tool,
        "_generate_condensed_log_output",
        lambda verified_status, report_filename, actual_accomplishments, report_snapshot: "condensed",
    )

    result = tool.execute(action="generate", summary="done", status="success")

    assert result.success is True
    assert result.output == "condensed"
    assert result.raw_data["full_report"] == "# Full Report"
    assert result.raw_data["report_snapshot"]["status"] == "success"
    assert result.metadata["verified_status"] == "success"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/test_report_contract.py -v
```

Expected: FAIL if `ToolResult` does not preserve `raw_data` or `ReportTool` does not use it consistently.

- [ ] **Step 3: Update `ReportTool` return**

In `src/sag/tools/report_tool.py`, ensure the successful report `ToolResult` uses the declared field:

```python
return ToolResult(
    success=True,
    output=condensed_output,
    metadata=metadata,
    documentation_links=[],
    raw_data={
        "full_report": report,
        "report_snapshot": report_snapshot,
    },
)
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
uv run pytest tests/test_report_contract.py tests/test_result_state_contracts.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit task 5**

```bash
git add src/sag/tools/report_tool.py tests/test_report_contract.py
git commit -m "Stabilize report result contract"
```

Do not add Co-Authorship.

## Task 6: Add Wheel Packaging Smoke Test

**Files:**
- Create or modify: `tests/test_packaging_smoke.py`
- Modify as needed: `pyproject.toml`

- [ ] **Step 1: Write packaging smoke test**

Create `tests/test_packaging_smoke.py`:

```python
import subprocess
import sys
import venv
from pathlib import Path


def test_wheel_installs_and_cli_loads(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    dist_dir = tmp_path / "dist"

    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist_dir)],
        cwd=repo_root,
        check=True,
    )

    wheels = list(dist_dir.glob("*.whl"))
    assert len(wheels) == 1

    venv_dir = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    python = venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    sag_bin = venv_dir / ("Scripts/sag.exe" if sys.platform == "win32" else "bin/sag")

    subprocess.run([str(python), "-m", "pip", "install", str(wheels[0])], check=True)
    subprocess.run([str(python), "-c", "import sag; import sag.main; assert hasattr(sag.main, 'cli')"], check=True)
    subprocess.run([str(sag_bin), "--help"], check=True)
```

- [ ] **Step 2: Ensure build dependency is available**

If `python -m build` is not available, add `build>=1.2.0` to the dev dependency group in `pyproject.toml`.

- [ ] **Step 3: Run packaging smoke test**

Run:

```bash
uv run pytest tests/test_packaging_smoke.py -v
```

Expected: PASS.

- [ ] **Step 4: Run all contract tests**

Run:

```bash
uv run pytest tests/test_import_smoke.py tests/test_static_import_guard.py tests/test_tool_contracts.py tests/test_result_state_contracts.py tests/test_report_contract.py tests/test_packaging_smoke.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit task 6**

```bash
git add pyproject.toml tests/test_packaging_smoke.py
git commit -m "Add packaging smoke test"
```

Do not add Co-Authorship.

## Task 7: Update Documentation And Run Full Verification

**Files:**
- Modify: `README.md`
- Modify as needed: `examples/*.py`
- Modify as needed: `.gitignore`

- [ ] **Step 1: Search for stale user-facing module paths**

Run:

```bash
rg -n "from (agent|tools|config|docker_orch|reporting|testcases|ui)|import (agent|tools|config|docker_orch|reporting|testcases|ui)|main:cli|packages = \\[" README.md examples pyproject.toml src tests
```

Expected: only intentional references remain, such as static guard tests or historical notes.

- [ ] **Step 2: Update docs/examples**

Update user-facing references to the new package namespace where relevant:

```python
from sag.agent.agent import SetupAgent
from sag.tools.base import ToolResult
```

Do not change CLI examples unless they are wrong; `uv run sag ...` remains valid.

- [ ] **Step 3: Ensure generated local folders stay untracked**

If `.superpowers/` is still untracked, add it to `.gitignore` unless the user asks to keep it visible.

Expected `.gitignore` addition:

```gitignore
.superpowers/
```

- [ ] **Step 4: Run formatting and tests**

Run:

```bash
uv run black src tests
uv run isort src tests
uv run pytest -v
```

Expected: PASS.

- [ ] **Step 5: Run status check**

Run:

```bash
git status --short
```

Expected: only intentional task changes are staged/unstaged. Existing unrelated user changes, such as `banner.jpeg` deletion if still present, must not be reverted or committed unless explicitly requested.

- [ ] **Step 6: Commit task 7**

```bash
git add README.md examples .gitignore src tests pyproject.toml
git commit -m "Document sag package migration"
```

Do not add Co-Authorship.

## Task 8: Independent Implementation Correctness Review

**Files:**
- No direct code ownership for the reviewer.
- Review inputs:
  - `docs/superpowers/specs/2026-06-01-contract-first-src-sag-migration-design.md`
  - this implementation plan
  - final diff from all implementation commits

- [ ] **Step 1: Dispatch review agent**

Ask an independent review agent to check correctness only:

```text
Review the implementation for the contract-first src/sag migration.

Do not reopen the design direction. Check whether implementation matches the approved spec:
- runtime code lives under src/sag
- no old runtime absolute imports remain
- pyproject publishes sag and CLI resolves to sag.main:cli
- BaseTool exposes public get_parameter_schema
- ReActEngine uses the public schema API
- ToolResult preserves raw_data
- AgentStatus/AgentStateAnalysis fields are consistent
- ReportTool returns structured report data through raw_data
- tests cover import, packaging, schema, result/state, report, and static import guard
- no unrelated changes were included
- commit messages do not include Co-Authorship

Return blocking findings first with file/line references.
```

- [ ] **Step 2: Address review findings**

If the reviewer finds blocking issues, fix them with tests first, then implementation.

- [ ] **Step 3: Re-run verification**

Run:

```bash
uv run pytest -v
git status --short
```

Expected: tests pass and only intended changes remain.

- [ ] **Step 4: Final implementation commit if needed**

```bash
git add src/sag tests pyproject.toml README.md examples .gitignore
git commit -m "Address src sag migration review findings"
```

Do not add Co-Authorship.

## Final Done Criteria

- `uv run pytest -v` passes.
- Packaging smoke test builds and installs the wheel.
- CLI entrypoint resolves to `sag.main:cli`.
- Runtime code imports through `sag.*`.
- Static import guard passes.
- Contract tests cover the known schema/result/state/report failures.
- Independent review agent reports no blocking correctness findings.
- No unrelated worktree changes are included.
- No commit message includes Co-Authorship.
