# Bash Timeout Parameter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `bash(timeout=N)` usable as the maximum total execution time.

**Architecture:** Keep timeout semantics at the bash/Docker execution boundary. The
tool schema already exposes the parameter, the normalizer supplies defaults, and
the BashTool passes explicit timeout values into either ordinary Docker
execution or monitored execution.

**Tech Stack:** Python, pytest, Docker SDK command execution, existing SAG tool
orchestration contracts.

---

### Task 1: Add Red Tests For Bash Timeout

**Files:**
- Modify: `tests/test_tool_contracts.py`
- Modify: `tests/test_tool_orchestration_recovery.py`
- Create: `tests/test_bash_tool_timeout.py`

- [ ] **Step 1: Add tests proving real bash accepts and forwards timeout**

Create tests that instantiate `BashTool` with a fake orchestrator and assert:

```python
result = BashTool(fake).execute(command="echo hi", timeout=7)
assert result.success is True
assert fake.calls[-1]["timeout"] == 7
```

Also assert a long-running command uses `execute_command_with_monitoring()` with
`absolute_timeout=120` when called with `timeout=120`.

- [ ] **Step 2: Add schema/prompt contract assertions**

Assert `BashTool().get_parameter_schema()["properties"]["timeout"]` exists and
that the ReAct tool schema includes the same property.

- [ ] **Step 3: Add recovery assertion**

Update bash timeout recovery tests so `validated_params` include `timeout` and
`execution.executed_params` plus `execution.metadata["recovery"]["recovery_params"]`
preserve it.

- [ ] **Step 4: Run red tests**

Run:

```bash
uv run pytest tests/test_bash_tool_timeout.py tests/test_tool_contracts.py tests/test_tool_orchestration_recovery.py::test_bash_timeout_guidance_adds_system_guidance -v
```

Expected: fail because `BashTool.execute()` rejects `timeout`.

### Task 2: Implement Timeout Plumbing

**Files:**
- Modify: `src/sag/tools/bash.py`
- Modify: `src/sag/docker_orch/orch.py`

- [ ] **Step 1: Accept timeout in BashTool**

Change `BashTool.execute()` to accept `timeout: int = 60`, validate it as a
positive integer, include it in valid-parameter help, and pass it to normal
Docker execution.

- [ ] **Step 2: Apply timeout to monitored commands**

Use `timeout` as monitored `absolute_timeout`. Cap `silent_timeout` to a value
no greater than the total timeout.

- [ ] **Step 3: Add timeout to normal Docker execution**

Change `DockerOrchestrator.execute_command(..., timeout: Optional[int] = None)`.
When timeout is positive, wrap the already-built command body with:

```bash
timeout --preserve-status <seconds> bash -c '<escaped command>'
```

Default `None` preserves existing behavior.

- [ ] **Step 4: Run green tests**

Run the red-test command again. Expected: pass.

### Task 3: Verification And Commit

**Files:**
- Modify as needed from Tasks 1-2.

- [ ] **Step 1: Run focused tests**

```bash
uv run pytest tests/test_bash_tool_timeout.py tests/test_tool_contracts.py tests/test_tool_orchestration_parameters.py tests/test_tool_orchestration_recovery.py::test_bash_timeout_guidance_adds_system_guidance -v
```

- [ ] **Step 2: Run full tests and guards**

```bash
uv run pytest
uv run black --check src tests
uv run isort --check-only src tests
git diff --check
git status --short
```

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-06-03-bash-timeout-parameter-design.md docs/superpowers/plans/2026-06-03-bash-timeout-parameter.md src/sag/tools/bash.py src/sag/docker_orch/orch.py tests/test_bash_tool_timeout.py tests/test_tool_contracts.py tests/test_tool_orchestration_recovery.py
git commit -m "Support bash tool timeout parameter"
```
