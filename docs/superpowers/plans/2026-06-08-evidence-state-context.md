# Evidence State Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add structured evidence state beside SAG's existing trunk/branch narrative context so tools, validators, reports, and Web UI agree on success, partial, blocked, conflict, and unknown outcomes without breaking agent reasoning continuity.

**Architecture:** Introduce a small evidence model module, keep `ToolResult.success` backward compatible while adding explicit status/evidence fields, then thread those fields through tool orchestration, context manager, physical validation, reporting, web read models, and React UI. Bash remains execution-fact-only; Maven/Gradle and validators carry domain evidence.

**Tech Stack:** Python 3.12, Pydantic, pytest, Docker-backed SAG runtime, FastAPI read models, React + TypeScript + Vitest.

---

## File Structure

Create:

- `src/sag/evidence.py`: shared evidence enums and lightweight records used by tools, context, validators, reports, and web models.
- `tests/test_evidence_models.py`: contract tests for evidence states, aggregation, and percent-preserving test stats.
- `tests/test_context_evidence_state.py`: context manager and context tool tests for narrative plus evidence trunk/branch behavior.
- `tests/test_tool_result_evidence_contract.py`: backward-compatible `ToolResult` behavior and bash execution-fact contract.
- `docs/manual-tests/evidence-state-regression.md`: local manual regression checklist for commons-cli, commons-vfs, Beam, and Iceberg.

Modify:

- `src/sag/tools/base.py`: add status/evidence fields to `ToolResult` while preserving existing `.success` consumers.
- `src/sag/tools/bash.py`: add execution facts metadata; do not add domain status.
- `src/sag/tools/maven_tool.py`: emit Maven evidence status, raw refs, test stats, and conflict findings.
- `src/sag/tools/gradle_tool.py`: emit Gradle evidence status, raw refs, test stats, and conflict findings.
- `src/sag/agent/tool_orchestration.py`: preserve evidence fields in observations and formatted output.
- `src/sag/agent/context_manager.py`: store evidence fields on trunk tasks and branch history.
- `src/sag/tools/context_tool.py`: accept optional evidence refs/status and write narrative plus evidence state.
- `src/sag/agent/physical_validator.py`: expose structured build/test validator findings and percentages.
- `src/sag/tools/report_tool.py`: make report status evidence-driven and preserve counts/percentages.
- `src/sag/agent/react_prompt_builder.py` and prompt YAML/config files if present: teach agent the evidence-state rules.
- `src/sag/web/models.py`: add evidence status and percent fields to read models.
- `src/sag/web/session_registry.py`, `src/sag/web/context_map.py`, `src/sag/web/evidence.py`, `src/sag/web/read_model.py`, `src/sag/web/demo_data.py`: read and expose evidence state to Web UI.
- `webui/src/api/types.ts`: co-evolve TypeScript API types with backend models.
- `webui/src/components/session/BuildCard.tsx`, `TestCard.tsx`, `EvidenceTimeline.tsx`, `ContextMap.tsx`, `ReportDoc.tsx`: render flow status and evidence status separately, preserve test percentages, and keep raw refs expandable.
- `webui/src/pages/Workspace.tsx`, `webui/src/pages/SessionDetail.tsx`: show evidence-aware summaries.
- Existing tests under `tests/` and `webui/src/**/*.test.tsx`: update fixtures and expectations.

## Implementation Tasks

### Task 1: Add Shared Evidence Models

**Files:**
- Create: `src/sag/evidence.py`
- Create: `tests/test_evidence_models.py`

- [ ] **Step 1: Write failing tests for evidence status and aggregation**

Create `tests/test_evidence_models.py`:

```python
from sag.evidence import EvidenceStatus, TestStats, aggregate_evidence_status


def test_evidence_status_values_are_constrained():
    assert [state.value for state in EvidenceStatus] == [
        "success",
        "partial",
        "blocked",
        "conflict",
        "unknown",
    ]


def test_aggregate_evidence_status_uses_blocked_conflict_partial_precedence():
    assert aggregate_evidence_status([EvidenceStatus.SUCCESS]) == EvidenceStatus.SUCCESS
    assert aggregate_evidence_status([EvidenceStatus.SUCCESS, EvidenceStatus.PARTIAL]) == EvidenceStatus.PARTIAL
    assert aggregate_evidence_status([EvidenceStatus.PARTIAL, EvidenceStatus.CONFLICT]) == EvidenceStatus.CONFLICT
    assert aggregate_evidence_status([EvidenceStatus.CONFLICT, EvidenceStatus.BLOCKED]) == EvidenceStatus.BLOCKED
    assert aggregate_evidence_status([]) == EvidenceStatus.UNKNOWN


def test_test_stats_preserve_counts_and_percentages():
    stats = TestStats(executed=214, passed=206, failed=3, skipped=5, discovered=460)

    assert stats.pass_rate == 96.3
    assert stats.execution_rate == 46.5
    assert stats.as_summary() == "206 / 214 passed, 96.3% pass rate, 3 failed, 5 skipped"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_evidence_models.py -q
```

Expected: import failure for `sag.evidence`.

- [ ] **Step 3: Implement evidence models**

Create `src/sag/evidence.py`:

```python
"""Shared evidence state models for SAG tools, context, reports, and Web UI."""

from __future__ import annotations

from enum import Enum
from typing import Iterable

from pydantic import BaseModel, Field


class EvidenceStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class EvidenceRef(BaseModel):
    ref: str
    kind: str = "output"
    source: str = ""
    task_id: str | None = None
    label: str = ""


class EvidenceFinding(BaseModel):
    type: str
    reason: str
    status: EvidenceStatus = EvidenceStatus.UNKNOWN
    refs: list[str] = Field(default_factory=list)
    details: dict[str, object] = Field(default_factory=dict)


class TestStats(BaseModel):
    discovered: int | None = None
    executed: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0

    @property
    def pass_rate(self) -> float:
        if self.executed <= 0:
            return 0.0
        return round((self.passed / self.executed) * 100, 1)

    @property
    def execution_rate(self) -> float | None:
        if not self.discovered:
            return None
        return round((self.executed / self.discovered) * 100, 1)

    def as_summary(self) -> str:
        return (
            f"{self.passed} / {self.executed} passed, "
            f"{self.pass_rate:.1f}% pass rate, "
            f"{self.failed} failed, {self.skipped} skipped"
        )


def coerce_evidence_status(value: EvidenceStatus | str | None) -> EvidenceStatus:
    if isinstance(value, EvidenceStatus):
        return value
    if not value:
        return EvidenceStatus.UNKNOWN
    try:
        return EvidenceStatus(str(value).strip().lower())
    except ValueError:
        return EvidenceStatus.UNKNOWN


def aggregate_evidence_status(statuses: Iterable[EvidenceStatus | str | None]) -> EvidenceStatus:
    normalized = [coerce_evidence_status(status) for status in statuses]
    if not normalized:
        return EvidenceStatus.UNKNOWN
    for candidate in (
        EvidenceStatus.BLOCKED,
        EvidenceStatus.CONFLICT,
        EvidenceStatus.PARTIAL,
        EvidenceStatus.UNKNOWN,
    ):
        if candidate in normalized:
            return candidate
    return EvidenceStatus.SUCCESS
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/test_evidence_models.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/sag/evidence.py tests/test_evidence_models.py
git commit -m "Add evidence state models"
```

### Task 2: Extend ToolResult Without Breaking Existing Tools

**Files:**
- Modify: `src/sag/tools/base.py`
- Create: `tests/test_tool_result_evidence_contract.py`

- [ ] **Step 1: Write failing tests for status compatibility**

Create `tests/test_tool_result_evidence_contract.py`:

```python
from sag.evidence import EvidenceStatus
from sag.tools.base import ToolResult


def test_tool_result_defaults_status_from_success_boolean():
    success = ToolResult(success=True, output="ok")
    failure = ToolResult(success=False, output="", error="bad")

    assert success.status == EvidenceStatus.SUCCESS
    assert failure.status == EvidenceStatus.BLOCKED
    assert success.success is True
    assert failure.success is False


def test_tool_result_status_can_represent_partial_without_losing_legacy_success():
    result = ToolResult(
        success=True,
        status=EvidenceStatus.PARTIAL,
        output="Build command exited 0 but tests failed.",
        evidence_refs=["output_abc"],
        conflicts=["maven_success_vs_surefire_failures"],
        test_stats={"executed": 214, "passed": 206, "failed": 3, "skipped": 5},
    )

    assert result.success is True
    assert result.status == EvidenceStatus.PARTIAL
    assert result.evidence_refs == ["output_abc"]
    assert result.conflicts == ["maven_success_vs_surefire_failures"]
    assert result.test_stats.pass_rate == 96.3
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_tool_result_evidence_contract.py -q
```

Expected: `ToolResult` has no status/evidence fields.

- [ ] **Step 3: Add evidence fields to ToolResult**

Modify `src/sag/tools/base.py` imports:

```python
from sag.evidence import EvidenceFinding, EvidenceStatus, TestStats, coerce_evidence_status
```

Modify `ToolResult`:

```python
class ToolResult(BaseModel):
    """Result of a tool execution."""

    success: bool
    output: str
    status: EvidenceStatus | str | None = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    suggestions: List[str] = Field(default_factory=list)
    documentation_links: List[str] = Field(default_factory=list)
    raw_output: Optional[str] = None
    raw_data: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    evidence_refs: List[str] = Field(default_factory=list)
    conflicts: List[str] = Field(default_factory=list)
    validator_findings: List[EvidenceFinding] = Field(default_factory=list)
    test_stats: Optional[TestStats] = None

    def model_post_init(self, __context: Any) -> None:
        if self.status is None:
            self.status = EvidenceStatus.SUCCESS if self.success else EvidenceStatus.BLOCKED
        else:
            self.status = coerce_evidence_status(self.status)
        if isinstance(self.test_stats, dict):
            self.test_stats = TestStats(**self.test_stats)
```

Keep `success` as a required field in this task to avoid broad breakage. The enum is added beside it; later tasks decide whether individual consumers should use `.status` instead of `.success`.

- [ ] **Step 4: Run focused compatibility tests**

Run:

```bash
uv run pytest tests/test_tool_result_evidence_contract.py tests/test_tool_contracts.py tests/test_react_engine_tool_orchestration.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/sag/tools/base.py tests/test_tool_result_evidence_contract.py
git commit -m "Extend tool results with evidence state"
```

### Task 3: Make Bash Emit Execution Facts Only

**Files:**
- Modify: `src/sag/tools/bash.py`
- Modify: `tests/test_bash_tool_timeout.py`
- Modify: `tests/test_tool_result_evidence_contract.py`

- [ ] **Step 1: Add failing bash execution-fact tests**

Append to `tests/test_tool_result_evidence_contract.py`:

```python
from sag.tools.bash import BashTool


class FakeBashOrchestrator:
    def __init__(self, result):
        self.result = result

    def execute_command(self, command, working_dir=None, timeout=None):
        return self.result


def test_bash_success_reports_execution_facts_without_domain_status():
    tool = BashTool(docker_orchestrator=FakeBashOrchestrator({
        "success": True,
        "exit_code": 0,
        "output": "BUILD SUCCESS",
        "duration": 1.25,
    }))

    result = tool.execute(command="mvn test", working_directory="/workspace/project", timeout=30)

    assert result.success is True
    assert result.metadata["execution"]["executed"] is True
    assert result.metadata["execution"]["exit_code"] == 0
    assert result.metadata["execution"]["timed_out"] is False
    assert "domain_status" not in result.metadata


def test_bash_nonzero_reports_executed_nonzero_without_domain_status():
    tool = BashTool(docker_orchestrator=FakeBashOrchestrator({
        "success": False,
        "exit_code": 2,
        "output": "tests failed",
    }))

    result = tool.execute(command="npm test", working_directory="/workspace/project", timeout=30)

    assert result.success is False
    assert result.metadata["execution"]["executed"] is True
    assert result.metadata["execution"]["exit_code"] == 2
    assert result.status.value == "blocked"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_tool_result_evidence_contract.py::test_bash_success_reports_execution_facts_without_domain_status tests/test_tool_result_evidence_contract.py::test_bash_nonzero_reports_executed_nonzero_without_domain_status -q
```

Expected: execution metadata is missing.

- [ ] **Step 3: Add execution metadata in BashTool**

In `src/sag/tools/bash.py`, update result construction paths so every returned `ToolResult` includes:

```python
execution = {
    "command": command,
    "cwd": working_directory,
    "executed": bool(exit_code is not None),
    "exit_code": exit_code,
    "timed_out": timed_out,
    "duration": duration,
}
metadata = dict(existing_metadata)
metadata["execution"] = execution
```

For validation failures before command execution:

```python
metadata["execution"] = {
    "command": command,
    "cwd": working_directory,
    "executed": False,
    "exit_code": None,
    "timed_out": False,
    "duration": 0,
}
```

Do not add `domain_status` or Maven/Gradle/test interpretation in bash.

- [ ] **Step 4: Run bash tests**

Run:

```bash
uv run pytest tests/test_tool_result_evidence_contract.py tests/test_bash_tool_timeout.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/sag/tools/bash.py tests/test_bash_tool_timeout.py tests/test_tool_result_evidence_contract.py
git commit -m "Record bash execution facts"
```

### Task 4: Add Evidence State to Trunk and Branch Context

**Files:**
- Modify: `src/sag/agent/context_manager.py`
- Modify: `src/sag/tools/context_tool.py`
- Create: `tests/test_context_evidence_state.py`

- [ ] **Step 1: Write failing context tests**

Create `tests/test_context_evidence_state.py`:

```python
from sag.agent.context_manager import ContextManager, TaskStatus
from sag.evidence import EvidenceStatus


def test_trunk_task_records_narrative_and_evidence(tmp_path):
    manager = ContextManager(workspace_path=str(tmp_path))
    trunk = manager.create_trunk_context(
        goal="Set up project",
        project_url="https://example.test/demo",
        project_name="demo",
    )
    task_id = trunk.add_task("Run tests")
    manager._save_trunk_context(trunk)

    updated = manager.update_task_evidence(
        task_id,
        evidence_status="partial",
        evidence_refs=["output_abc", "surefire_report"],
        conflicts=["maven_success_vs_surefire_failures"],
        validator_findings=[{"type": "contradiction", "reason": "surefire failures", "status": "partial"}],
    )

    assert updated is True
    reloaded = manager.load_trunk_context()
    task = reloaded.todo_list[0]
    assert task.key_results == ""
    assert task.evidence_status == EvidenceStatus.PARTIAL
    assert task.evidence_refs == ["output_abc", "surefire_report"]
    assert task.conflicts == ["maven_success_vs_surefire_failures"]


def test_branch_receives_previous_summary_and_evidence_digest(tmp_path):
    manager = ContextManager(workspace_path=str(tmp_path))
    trunk = manager.create_trunk_context(
        goal="Set up project",
        project_url="https://example.test/demo",
        project_name="demo",
    )
    task_1 = trunk.add_task("Run build")
    task_2 = trunk.add_task("Run tests")
    trunk.update_task_status(task_1, TaskStatus.COMPLETED)
    trunk.update_task_key_results(task_1, "Build completed but test reports were not checked.")
    manager._save_trunk_context(trunk)
    manager.update_task_evidence(task_1, evidence_status="partial", evidence_refs=["output_build"], conflicts=[])

    result = manager.start_new_branch(task_2)
    branch = manager.load_branch_history(task_2)

    assert "Previous task (task_1)" in result["previous_summary"]
    assert "task_1 evidence_status: partial" in branch.previous_task_evidence_digest
    assert "output_build" in branch.previous_task_evidence_digest
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_context_evidence_state.py -q
```

Expected: missing evidence fields or helper.

- [ ] **Step 3: Add task and branch evidence fields**

Modify `src/sag/agent/context_manager.py` imports:

```python
from sag.evidence import EvidenceFinding, EvidenceStatus, coerce_evidence_status
```

Add to `Task`:

```python
    evidence_status: EvidenceStatus = EvidenceStatus.UNKNOWN
    evidence_refs: List[str] = Field(default_factory=list)
    conflicts: List[str] = Field(default_factory=list)
    validator_findings: List[EvidenceFinding] = Field(default_factory=list)
```

Add to `BranchContextHistory`:

```python
    previous_task_evidence_digest: str = ""
    current_task_evidence_refs: List[str] = Field(default_factory=list)
```

Add to `TrunkContext`:

```python
    def update_task_evidence(
        self,
        task_id: str,
        evidence_status: EvidenceStatus | str | None = None,
        evidence_refs: Optional[List[str]] = None,
        conflicts: Optional[List[str]] = None,
        validator_findings: Optional[List[EvidenceFinding | Dict[str, Any]]] = None,
    ) -> bool:
        for task in self.todo_list:
            if task.id == task_id:
                task.evidence_status = coerce_evidence_status(evidence_status)
                if evidence_refs is not None:
                    task.evidence_refs = evidence_refs
                if conflicts is not None:
                    task.conflicts = conflicts
                if validator_findings is not None:
                    task.validator_findings = [
                        finding if isinstance(finding, EvidenceFinding) else EvidenceFinding(**finding)
                        for finding in validator_findings
                    ]
                self.update_timestamp()
                return True
        return False
```

Add a `ContextManager.update_task_evidence(...)` wrapper that loads trunk, calls `trunk.update_task_evidence`, saves trunk, and returns the boolean.

- [ ] **Step 4: Build previous evidence digest in start_new_branch**

In `ContextManager.start_new_branch`, after `previous_summary` is computed, build:

```python
previous_evidence_digest = ""
if prev_task.status == TaskStatus.COMPLETED:
    previous_evidence_digest = self._format_previous_task_evidence_digest(prev_task)
```

Add helper:

```python
    def _format_previous_task_evidence_digest(self, task: Task) -> str:
        lines = [f"{task.id} evidence_status: {task.evidence_status.value}"]
        if task.evidence_refs:
            lines.append(f"evidence_refs: {', '.join(task.evidence_refs)}")
        if task.conflicts:
            lines.append(f"conflicts: {', '.join(task.conflicts)}")
        return "\n".join(lines)
```

Pass it to `BranchContextHistory(previous_task_evidence_digest=previous_evidence_digest)`.

- [ ] **Step 5: Include evidence fields in context info**

In `ContextManager.get_current_context_info`, add to each task dict:

```python
"evidence_status": task.evidence_status.value,
"evidence_refs": task.evidence_refs,
"conflicts": task.conflicts,
"validator_findings": [finding.model_dump() for finding in task.validator_findings],
```

- [ ] **Step 6: Run context tests**

Run:

```bash
uv run pytest tests/test_context_evidence_state.py tests/test_context_tool_completion_validation.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/sag/agent/context_manager.py src/sag/tools/context_tool.py tests/test_context_evidence_state.py
git commit -m "Add evidence state to context"
```

### Task 5: Bind Evidence in complete_with_results

**Files:**
- Modify: `src/sag/tools/context_tool.py`
- Modify: `tests/test_context_evidence_state.py`

- [ ] **Step 1: Write failing context tool test**

Append to `tests/test_context_evidence_state.py`:

```python
from sag.tools.context_tool import ContextTool


def test_complete_with_results_preserves_narrative_and_evidence(tmp_path):
    manager = ContextManager(workspace_path=str(tmp_path))
    trunk = manager.create_trunk_context(
        goal="Set up project",
        project_url="https://example.test/demo",
        project_name="demo",
    )
    task_id = trunk.add_task("Run tests")
    manager._save_trunk_context(trunk)
    manager.start_new_branch(task_id)
    tool = ContextTool(manager)

    result = tool.execute(
        action="complete_with_results",
        summary="Maven test command exited zero after ignoring failures.",
        key_results="Tests: 206 / 214 passed, 96.3% pass rate, 3 failed, 5 skipped.",
        evidence_status="partial",
        evidence_refs=["output_abc", "surefire_xml"],
    )

    assert result.success is True
    reloaded = manager.load_trunk_context()
    task = reloaded.todo_list[0]
    assert task.status.value == "completed"
    assert task.key_results.startswith("Tests: 206 / 214 passed")
    assert task.evidence_status.value == "partial"
    assert task.evidence_refs == ["output_abc", "surefire_xml"]
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/test_context_evidence_state.py::test_complete_with_results_preserves_narrative_and_evidence -q
```

Expected: `ContextTool.execute` does not accept evidence fields.

- [ ] **Step 3: Extend ContextTool parameters**

Modify `ContextTool.execute` signature:

```python
        evidence_refs: Optional[List[str]] = None,
        evidence_status: Optional[str] = None,
        conflicts: Optional[List[str]] = None,
```

Thread these through the `complete_with_results` dispatch into `_complete_task_with_results`.

Modify `_complete_task_with_results` signature:

```python
    def _complete_task_with_results(
        self,
        summary: Optional[str],
        key_results: Optional[str],
        force: bool = False,
        evidence_refs: Optional[List[str]] = None,
        evidence_status: Optional[str] = None,
        conflicts: Optional[List[str]] = None,
    ) -> ToolResult:
```

After `fresh_trunk_context.update_task_key_results(...)`, call:

```python
fresh_trunk_context.update_task_evidence(
    current_task_id,
    evidence_status=evidence_status,
    evidence_refs=evidence_refs or [],
    conflicts=conflicts or [],
)
```

Add these fields to `enhanced_result` metadata.

- [ ] **Step 4: Update tool schema docs**

In `ContextTool.get_usage_guide` and parameter schema, add:

```text
evidence_refs: Optional raw/tool/validator refs backing the task result.
evidence_status: Optional agent claim; validator/context may refine it.
conflicts: Optional contradiction ids discovered during the task.
```

Ensure prompt text says natural-language `key_results` remains required.

- [ ] **Step 5: Run context tool tests**

Run:

```bash
uv run pytest tests/test_context_evidence_state.py tests/test_context_tool_completion_validation.py tests/test_tool_contracts.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/sag/tools/context_tool.py tests/test_context_evidence_state.py
git commit -m "Bind evidence to task completion"
```

### Task 6: Preserve Evidence in Tool Orchestration and Agent Observations

**Files:**
- Modify: `src/sag/agent/tool_orchestration.py`
- Modify: `src/sag/agent/react_engine.py`
- Modify: `tests/test_tool_orchestration_models.py`
- Modify: `tests/test_react_engine_tool_orchestration.py`

- [ ] **Step 1: Write failing orchestration formatting test**

Append to `tests/test_tool_orchestration_models.py`:

```python
from sag.evidence import EvidenceStatus
from sag.agent.tool_orchestration import format_tool_result_for_observation
from sag.tools.base import ToolResult


def test_tool_observation_includes_evidence_status_refs_and_conflicts():
    result = ToolResult(
        success=True,
        status=EvidenceStatus.PARTIAL,
        output="Maven exited zero but tests failed.",
        evidence_refs=["output_abc"],
        conflicts=["maven_success_vs_surefire_failures"],
    )

    observation = format_tool_result_for_observation(result)

    assert "Evidence status: partial" in observation
    assert "Evidence refs: output_abc" in observation
    assert "Conflicts: maven_success_vs_surefire_failures" in observation
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/test_tool_orchestration_models.py::test_tool_observation_includes_evidence_status_refs_and_conflicts -q
```

Expected: observation does not include evidence fields.

- [ ] **Step 3: Update observation formatting**

In `src/sag/agent/tool_orchestration.py`, update `format_tool_result_for_observation`:

```python
    lines = []
    lines.append(f"Evidence status: {result.status.value}")
    if result.evidence_refs:
        lines.append(f"Evidence refs: {', '.join(result.evidence_refs)}")
    if result.conflicts:
        lines.append(f"Conflicts: {', '.join(result.conflicts)}")
    if result.test_stats:
        lines.append(f"Test stats: {result.test_stats.as_summary()}")
```

Place this block before the natural-language output so the agent sees state before prose.

- [ ] **Step 4: Ensure ReAct success gating remains legacy-compatible**

Audit `src/sag/agent/react_engine.py` usages of `result.success`. Do not replace all of them in this task. Only add comments or local checks where evidence status must force extra thinking:

```python
if result.status.value in {"partial", "conflict", "unknown"}:
    self._force_thinking_after_success = True
```

Keep `blocked`/legacy failure behavior aligned with existing `.success == False`.

- [ ] **Step 5: Run orchestration and ReAct tests**

Run:

```bash
uv run pytest tests/test_tool_orchestration_models.py tests/test_tool_orchestration_execution.py tests/test_react_engine_tool_orchestration.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/sag/agent/tool_orchestration.py src/sag/agent/react_engine.py tests/test_tool_orchestration_models.py tests/test_react_engine_tool_orchestration.py
git commit -m "Surface evidence in tool observations"
```

### Task 7: Add Maven and Gradle Evidence Findings

**Files:**
- Modify: `src/sag/tools/maven_tool.py`
- Modify: `src/sag/tools/gradle_tool.py`
- Modify: `tests/test_maven_gradle_tool_contracts.py`

- [ ] **Step 1: Add failing Maven partial/conflict tests**

Append to `tests/test_maven_gradle_tool_contracts.py`:

```python
def test_maven_build_success_with_surefire_failures_sets_partial_evidence():
    tool = make_maven_tool_with_output(
        output="[INFO] BUILD SUCCESS\nTests run: 214, Failures: 3, Errors: 0, Skipped: 5",
        exit_code=0,
    )

    result = tool.execute(command="test", working_directory="/workspace/project")

    assert result.status.value in {"partial", "conflict"}
    assert result.test_stats.executed == 214
    assert result.test_stats.passed == 206
    assert result.test_stats.failed == 3
    assert "maven_success_vs_test_failures" in result.conflicts
    assert result.test_stats.pass_rate == 96.3
```

Add this helper near the existing Maven test helpers before using it:

```python
def make_maven_tool_with_output(output: str, exit_code: int = 0):
    orchestrator = FakeBuildToolOrchestrator(
        monitored_result={"success": exit_code == 0, "exit_code": exit_code, "output": output}
    )
    tool = MavenTool(orchestrator)
    tool._resolve_maven_command = lambda *args, **kwargs: "mvn"
    tool._validate_build_artifacts_in_container = lambda *args, **kwargs: {"success": True, "artifacts": []}
    return tool
```

- [ ] **Step 2: Add failing Gradle partial/conflict tests**

Append to `tests/test_maven_gradle_tool_contracts.py`:

```python
def test_gradle_build_success_with_failed_tests_sets_partial_evidence():
    tool = make_gradle_tool_with_output(
        output="BUILD SUCCESSFUL in 10s\n214 tests completed, 3 failed, 5 skipped",
        exit_code=0,
    )

    result = tool.execute(tasks="test", working_directory="/workspace/project")

    assert result.status.value in {"partial", "conflict"}
    assert result.test_stats.executed == 214
    assert result.test_stats.passed == 206
    assert result.test_stats.failed == 3
    assert result.test_stats.pass_rate == 96.3
```

Add this helper near the existing Gradle test helpers before using it:

```python
def make_gradle_tool_with_output(output: str, exit_code: int = 0):
    orchestrator = FakeBuildToolOrchestrator(
        monitored_result={"success": exit_code == 0, "exit_code": exit_code, "output": output}
    )
    tool = GradleTool(orchestrator)
    tool._resolve_gradle_command = lambda *args, **kwargs: "./gradlew"
    tool._validate_build_artifacts_in_container = lambda *args, **kwargs: {"success": True, "artifacts": []}
    return tool
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_maven_gradle_tool_contracts.py -q
```

Expected: new assertions fail or helper is missing.

- [ ] **Step 4: Implement domain evidence helpers**

In `src/sag/tools/maven_tool.py` and `src/sag/tools/gradle_tool.py`, add local helper functions or a shared private helper if both files already share test parsing utilities:

```python
def _status_from_test_stats(exit_success: bool, stats: TestStats | None) -> EvidenceStatus:
    if stats and (stats.failed > 0):
        return EvidenceStatus.PARTIAL if exit_success else EvidenceStatus.BLOCKED
    return EvidenceStatus.SUCCESS if exit_success else EvidenceStatus.BLOCKED
```

When output contains success text plus failed tests:

```python
conflicts.append("maven_success_vs_test_failures")
status = EvidenceStatus.PARTIAL
```

For Gradle use:

```python
conflicts.append("gradle_success_vs_test_failures")
status = EvidenceStatus.PARTIAL
```

Attach `test_stats`, `evidence_refs`, and `conflicts` to returned `ToolResult`.

- [ ] **Step 5: Run Maven/Gradle tests**

Run:

```bash
uv run pytest tests/test_maven_gradle_tool_contracts.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/sag/tools/maven_tool.py src/sag/tools/gradle_tool.py tests/test_maven_gradle_tool_contracts.py
git commit -m "Add build tool evidence findings"
```

### Task 8: Make Physical Validation and Report Evidence-Driven

**Files:**
- Modify: `src/sag/agent/physical_validator.py`
- Modify: `src/sag/tools/report_tool.py`
- Modify: `tests/test_agent_final_status.py`
- Modify: `tests/test_report_contract.py`

- [ ] **Step 1: Add failing validator/report tests**

Append to `tests/test_report_contract.py`:

```python
def test_report_marks_partial_when_tests_fail_but_preserves_percentage(tmp_path):
    tool = ReportTool(output_dir=tmp_path)

    result = tool.execute(
        action="generate",
        summary="Build/package command completed, but tests failed.",
        status="success",
        evidence_status="partial",
        details="Tests: 206 / 214 passed, 96.3% pass rate, 3 failed, 5 skipped.",
        test_stats={"executed": 214, "passed": 206, "failed": 3, "skipped": 5},
        conflicts=["maven_success_vs_test_failures"],
    )

    assert result.success is True
    assert result.status.value == "partial"
    assert "Result: PARTIAL" in result.output
    assert "96.3% pass rate" in result.output
    assert "3 failed" in result.output
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_report_contract.py::test_report_marks_partial_when_tests_fail_but_preserves_percentage -q
```

Expected: report tool does not accept or render evidence status.

- [ ] **Step 3: Extend ReportTool contract**

Modify `ReportTool.execute` to accept:

```python
evidence_status: str | None = None
test_stats: dict[str, int] | None = None
conflicts: list[str] | None = None
evidence_refs: list[str] | None = None
```

Compute report status:

```python
final_status = coerce_evidence_status(evidence_status or status)
```

Render `Result: PARTIAL`, `Result: CONFLICT`, etc. from `final_status`, not from free-text `status`.

If `test_stats` is present, instantiate `TestStats` and include `stats.as_summary()`.

- [ ] **Step 4: Expose validator evidence fields**

In `src/sag/agent/physical_validator.py`, ensure build/test validation dictionaries include:

```python
"evidence_status": "success" | "partial" | "blocked" | "conflict" | "unknown",
"test_stats": {"executed": ..., "passed": ..., "failed": ..., "skipped": ..., "discovered": ...},
"conflicts": [...],
"evidence_refs": [...],
```

For failed/error tests with build success, set `evidence_status` to `partial` and preserve pass rate.

- [ ] **Step 5: Update final status tests**

In `tests/test_agent_final_status.py`, add or update assertions so:

```python
assert validator_result["evidence_status"] == "partial"
assert validator_result["test_stats"]["failed"] > 0
assert validator_result["test_stats"]["pass_rate"] == 96.3
```

Use the existing fake validator/test report fixtures in that file.

- [ ] **Step 6: Run validator/report tests**

Run:

```bash
uv run pytest tests/test_report_contract.py tests/test_agent_final_status.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/sag/agent/physical_validator.py src/sag/tools/report_tool.py tests/test_agent_final_status.py tests/test_report_contract.py
git commit -m "Make reports evidence driven"
```

### Task 9: Update Agent Prompts for Evidence Rules

**Files:**
- Modify: `src/sag/agent/react_prompt_builder.py`
- Modify: prompt YAML/config file that contains ReAct/context prompts if present under `src/sag/config/`
- Modify: `tests/test_react_prompt_builder.py`
- Modify: `tests/test_prompt_reference_comments.py`

- [ ] **Step 1: Find current prompt storage**

Run:

```bash
rg -n "BUILD SUCCESS|complete_with_results|validator|partial|conflict|evidence" src/sag/agent src/sag/config tests/test_react_prompt_builder.py
```

Expected: locate the prompt strings or YAML entries that tell the agent how to interpret task completion and reports.

- [ ] **Step 2: Add failing prompt test**

In `tests/test_react_prompt_builder.py`, add:

```python
def test_action_prompt_explains_evidence_status_rules():
    prompt = build_prompt_for_test_mode()

    assert "completed means the branch task flow ended" in prompt
    assert "BUILD SUCCESS cannot override validator findings" in prompt
    assert "partial, conflict, or unknown" in prompt
    assert "read evidence refs or raw output refs" in prompt
```

Use the existing helper in this file to construct the prompt; if there is no helper, follow the existing test pattern for building prompts.

- [ ] **Step 3: Run prompt test to verify it fails**

Run:

```bash
uv run pytest tests/test_react_prompt_builder.py::test_action_prompt_explains_evidence_status_rules -q
```

Expected: missing prompt text.

- [ ] **Step 4: Add concise evidence prompt rules**

Add this text to the relevant prompt YAML/config entry or prompt builder:

```text
Evidence status rules:
- completed means the branch task flow ended; it does not automatically mean setup succeeded.
- BUILD SUCCESS cannot override validator findings.
- For partial, conflict, or unknown evidence, read evidence refs or raw output refs before making a final claim.
- If evidence is missing, gather more evidence using bash or a domain tool.
- Reports must distinguish success, partial, blocked, conflict, and unknown.
```

If editing a YAML prompt, add the required file path and line reference comment style already used by `tests/test_prompt_reference_comments.py`.

- [ ] **Step 5: Run prompt tests**

Run:

```bash
uv run pytest tests/test_react_prompt_builder.py tests/test_prompt_reference_comments.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/sag/agent/react_prompt_builder.py src/sag/config tests/test_react_prompt_builder.py tests/test_prompt_reference_comments.py
git commit -m "Teach agent evidence status rules"
```

### Task 10: Co-Evolve Backend Web Read Models

**Files:**
- Modify: `src/sag/web/models.py`
- Modify: `src/sag/web/session_registry.py`
- Modify: `src/sag/web/context_map.py`
- Modify: `src/sag/web/evidence.py`
- Modify: `src/sag/web/read_model.py`
- Modify: `src/sag/web/demo_data.py`
- Modify: `tests/test_web_models.py`
- Modify: `tests/test_web_context_map.py`
- Modify: `tests/test_web_evidence.py`
- Modify: `tests/test_web_read_model.py`
- Modify: `tests/test_web_demo_data.py`

- [x] **Step 1: Add failing web model tests**

In `tests/test_web_models.py`, add:

```python
from sag.web.models import ExecutionSessionDetail, TestSummary


def test_session_detail_exposes_flow_status_and_evidence_status():
    detail = ExecutionSessionDetail(
        id="SETUP-1",
        workspace="sag-demo",
        title="Set up demo",
        status="completed",
        evidence_status="partial",
        entry="CLI",
        start="2026-06-08T00:00:00",
        duration="2m",
        outcome="Tests are partial.",
        build={"state": "success", "tool": "Maven", "time": "2m", "note": ""},
        test=TestSummary(state="partial", pass_count=206, fail_count=3, skip_count=5, total=214, pass_rate=96.3),
        report="ready",
        evidence=[],
        logs=[],
    )

    payload = detail.model_dump(by_alias=True)

    assert payload["status"] == "completed"
    assert payload["evidenceStatus"] == "partial"
    assert payload["test"]["passRate"] == 96.3
```

- [x] **Step 2: Run web backend model test to verify it fails**

Run:

```bash
uv run pytest tests/test_web_models.py::test_session_detail_exposes_flow_status_and_evidence_status -q
```

Expected: missing fields.

- [x] **Step 3: Add backend read model fields**

Modify `src/sag/web/models.py`:

```python
class TestSummary(WebModel):
    state: str = "none"
    pass_count: int = Field(default=0, serialization_alias="pass")
    fail_count: int = Field(default=0, serialization_alias="fail")
    skip_count: int = Field(default=0, serialization_alias="skip")
    total: int = 0
    pass_rate: float | None = Field(default=None, serialization_alias="passRate")
    execution_rate: float | None = Field(default=None, serialization_alias="executionRate")
    note: str = ""
```

Add to `ExecutionSessionSummary`, `WorkspaceSummary`, and `ExecutionSessionDetail` as appropriate:

```python
evidence_status: str = Field(default="unknown", serialization_alias="evidenceStatus")
```

Add to `ContextTask`:

```python
evidence_status: str = Field(default="unknown", serialization_alias="evidenceStatus")
evidence_refs: list[str] = Field(default_factory=list, serialization_alias="evidenceRefs")
conflicts: list[str] = Field(default_factory=list)
```

- [x] **Step 4: Map evidence data from session/context files**

Update `src/sag/web/session_registry.py`, `src/sag/web/context_map.py`, and `src/sag/web/evidence.py` so they read:

```python
task.get("evidence_status", "unknown")
task.get("evidence_refs", [])
task.get("conflicts", [])
test_status.get("pass_rate")
test_status.get("execution_rate")
```

When no evidence exists, return `unknown` instead of inventing success.

- [x] **Step 5: Update demo data**

In `src/sag/web/demo_data.py`, include at least one session with:

```python
status="completed"
evidence_status="partial"
test.passRate=96.3
```

- [x] **Step 6: Run web backend tests**

Run:

```bash
uv run pytest tests/test_web_models.py tests/test_web_context_map.py tests/test_web_evidence.py tests/test_web_read_model.py tests/test_web_demo_data.py -q
```

Expected: all tests pass.

- [x] **Step 7: Commit**

```bash
git add src/sag/web/models.py src/sag/web/session_registry.py src/sag/web/context_map.py src/sag/web/evidence.py src/sag/web/read_model.py src/sag/web/demo_data.py tests/test_web_models.py tests/test_web_context_map.py tests/test_web_evidence.py tests/test_web_read_model.py tests/test_web_demo_data.py
git commit -m "Expose evidence state in web models"
```

### Task 11: Co-Evolve React Types and UI

**Files:**
- Modify: `webui/src/api/types.ts`
- Modify: `webui/src/components/common/status.ts`
- Modify: `webui/src/components/session/BuildCard.tsx`
- Modify: `webui/src/components/session/TestCard.tsx`
- Modify: `webui/src/components/session/EvidenceTimeline.tsx`
- Modify: `webui/src/components/session/ContextMap.tsx`
- Modify: `webui/src/pages/Workspace.tsx`
- Modify: `webui/src/pages/SessionDetail.tsx`
- Modify: `webui/src/pages/SessionDetail.test.tsx`
- Modify: `webui/src/pages/Dashboard.test.tsx`
- Modify: `webui/src/components/session/ContextMap.test.tsx`
- Modify: `webui/src/components/common/status.test.ts`

- [x] **Step 1: Add failing React UI tests**

In `webui/src/pages/SessionDetail.test.tsx`, add:

```tsx
it("shows completed flow with partial evidence result and preserves pass rate", () => {
  renderSessionDetail({
    ...baseSession,
    status: "completed",
    evidenceStatus: "partial",
    test: {
      state: "partial",
      pass: 206,
      fail: 3,
      skip: 5,
      total: 214,
      passRate: 96.3,
      note: "3 tests failed",
    },
  })

  expect(screen.getByText(/Completed/i)).toBeInTheDocument()
  expect(screen.getByText(/Partial result/i)).toBeInTheDocument()
  expect(screen.getByText(/96.3% pass rate/i)).toBeInTheDocument()
  expect(screen.getByText(/3 fail/i)).toBeInTheDocument()
})
```

Use the existing test fixture names in `SessionDetail.test.tsx`. If the helper is named differently, adapt only the helper call, not the assertions.

- [x] **Step 2: Run UI test to verify it fails**

Run:

```bash
cd webui
npm test -- SessionDetail.test.tsx --run
```

Expected: missing `evidenceStatus` handling or pass rate display.

- [x] **Step 3: Update TypeScript API types**

Modify `webui/src/api/types.ts`:

```ts
export type EvidenceStatus = "success" | "partial" | "blocked" | "conflict" | "unknown"

export interface TestSummary {
  state: string
  pass: number
  fail: number
  skip: number
  total: number
  passRate?: number | null
  executionRate?: number | null
  note?: string
}
```

Add `evidenceStatus?: EvidenceStatus` to workspace/session/context task interfaces.

- [x] **Step 4: Update status tone mapping**

In `webui/src/components/common/status.ts`, add evidence status tone mapping:

```ts
export function toneForEvidenceStatus(status: string | undefined): Tone {
  switch ((status ?? "unknown").toLowerCase()) {
    case "success":
      return "green"
    case "partial":
    case "conflict":
      return "amber"
    case "blocked":
      return "red"
    default:
      return "neutral"
  }
}
```

Add tests in `status.test.ts` for all five states.

- [x] **Step 5: Update cards and pages**

In session/workspace pages, display flow and evidence separately:

```tsx
<StatusBadge status={session.status} />
{session.evidenceStatus && session.evidenceStatus !== session.status ? (
  <StatusBadge status={`${labelForEvidence(session.evidenceStatus)} result`} />
) : null}
```

In `TestCard.tsx`, prefer provided pass rate:

```tsx
const passRate =
  typeof test.passRate === "number"
    ? test.passRate
    : test.total > 0
      ? Math.round((test.pass / test.total) * 1000) / 10
      : null
```

Render:

```tsx
{passRate !== null ? `${passRate.toFixed(1)}% pass rate` : "no pass rate"}
```

Do not hide `fail`/`skip` counts when pass rate is high.

- [x] **Step 6: Update context map task rendering**

In `ContextMap.tsx`, show each task's evidence status when present:

```tsx
{task.evidenceStatus && task.evidenceStatus !== "unknown" ? (
  <Badge tone={toneForEvidenceStatus(task.evidenceStatus)}>
    {labelForEvidence(task.evidenceStatus)}
  </Badge>
) : null}
```

Keep branch task details expandable and keep output refs clickable/previewable using the existing ref preview behavior.

- [x] **Step 7: Run frontend tests**

Run:

```bash
cd webui
npm test -- --run
```

Expected: all frontend tests pass.

- [x] **Step 8: Commit**

```bash
git add webui/src/api/types.ts webui/src/components/common/status.ts webui/src/components/session/BuildCard.tsx webui/src/components/session/TestCard.tsx webui/src/components/session/EvidenceTimeline.tsx webui/src/components/session/ContextMap.tsx webui/src/pages/Workspace.tsx webui/src/pages/SessionDetail.tsx webui/src/pages/SessionDetail.test.tsx webui/src/pages/Dashboard.test.tsx webui/src/components/session/ContextMap.test.tsx webui/src/components/common/status.test.ts
git commit -m "Show evidence state in web UI"
```

### Task 12: Add Manual Regression Checklist

**Files:**
- Create: `docs/manual-tests/evidence-state-regression.md`

- [ ] **Step 1: Create manual regression document**

Create `docs/manual-tests/evidence-state-regression.md`:

```markdown
# Evidence State Manual Regression

Run these locally before treating the evidence-state implementation as complete.
Do not add GitHub Actions CI for this matrix.

## Common Fields To Record

- session id
- container name
- repo URL
- selected ref/tag/commit
- task flow statuses
- evidence statuses
- build stats
- test stats
- report status
- UI status
- raw output refs
- known conflicts

## apache/commons-cli

Expected:
- Maven path is detected.
- Build and test evidence reaches success when tests pass.
- Report says success only when validator evidence agrees.
- UI shows success and preserves test counts/percentages.

Command:

```bash
uv run sag project https://github.com/apache/commons-cli --record
```

## apache/commons-vfs

Expected:
- UTF-8/RAT behavior remains fixed.
- Maven version recovery remains agent-driven.
- Test failures produce partial or conflict, not success.
- Report preserves pass rate and failed test count.
- UI shows completed flow with partial result.

Command:

```bash
uv run sag project https://github.com/apache/commons-vfs --record
```

## apache/beam

Expected:
- Gradle evidence extraction works.
- No Maven-only assumptions are used.
- Gradle test reports or raw output refs are visible when status is partial/conflict/blocked.

Command:

```bash
uv run sag project https://github.com/apache/beam --record
```

## apache/iceberg

Expected:
- Mixed Gradle/Maven evidence is handled without forcing one build manager.
- Overall status is aggregated from task evidence.
- UI and report agree on evidence status.

Command:

```bash
uv run sag project https://github.com/apache/iceberg --record
```

## Uncertain Behavior Rule

If a run reveals unclear behavior, stop and classify it before fixing:

- tool contract gap
- validator evidence source gap
- context propagation gap
- prompt guidance gap
- project-specific behavior that should not become a generic rule
```

- [ ] **Step 2: Check document is tracked despite docs ignore**

Run:

```bash
git check-ignore -v docs/manual-tests/evidence-state-regression.md
```

Expected: if ignored by `docs/`, use `git add -f` in the commit step.

- [ ] **Step 3: Commit**

```bash
git add -f docs/manual-tests/evidence-state-regression.md
git commit -m "Add evidence state manual regression checklist"
```

### Task 13: Full Local Verification

**Files:**
- No source changes expected unless verification reveals a bug.

- [ ] **Step 1: Run Python unit suite**

Run:

```bash
uv run pytest -q
```

Expected: all tests pass.

- [ ] **Step 2: Run frontend unit suite**

Run:

```bash
cd webui
npm test -- --run
```

Expected: all tests pass.

- [ ] **Step 3: Build frontend static assets if repo convention requires it**

Run:

```bash
cd webui
npm run build
```

Expected: build completes and `src/sag/web/static` contains updated generated assets. Stage generated assets only if this repository expects static assets to be committed.

- [ ] **Step 4: Run focused smoke for Web UI API**

Run:

```bash
uv run sag ui --port 8765
```

Expected: server starts and `/api/dashboard` returns workspaces without 500 errors. Stop the server after the check.

- [ ] **Step 5: Commit generated static assets if needed**

If Step 3 changed tracked static assets and this repo commits them:

```bash
git add src/sag/web/static webui
git commit -m "Build evidence state web assets"
```

If no generated assets are committed, record that in the final implementation summary.

## Self-Review Checklist

- Spec coverage:
  - Natural-language trunk/branch continuity is covered by Tasks 4 and 5.
  - Evidence status, refs, conflicts, validator findings are covered by Tasks 1, 2, 4, 5, 6, 8, and 10.
  - Bash execution-facts-only boundary is covered by Task 3.
  - Maven/Gradle partial/conflict behavior is covered by Task 7.
  - Report/UI percentage preservation is covered by Tasks 8, 10, and 11.
  - Local manual matrix for commons-cli, commons-vfs, Beam, and Iceberg is covered by Task 12.
- Gap scan:
  - No unresolved gaps or unspecified "add tests" steps should remain.
- Type consistency:
  - Backend alias is `evidenceStatus`.
  - Python field is `evidence_status`.
  - Test percentage fields are `pass_rate` in Python and `passRate` in TypeScript.
  - `ToolResult.success` remains available during this implementation to avoid broad breakage.
