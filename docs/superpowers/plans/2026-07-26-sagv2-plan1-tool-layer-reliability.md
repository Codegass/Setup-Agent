# SAG v2 — Plan 1: Tool-Layer Reliability (P0/P1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the §3.4 tool-layer fixes of the advisor-mode redesign spec on the current engine: reachable venv repair ladder, shell-safe Maven commands, no automatic test-exclusion, a Groovy-honest XML oracle, Gradle-wrapper unzip recovery, primary-test-coordinate enforcement, and build-receipt-gated phase closure.

**Architecture:** Pure tool/policy-layer changes — no interaction-protocol changes (those are Plan 2/3). Every fix is driven by a defect reproduced in the 2026-07-24 cold runs (sessions `20260724_021304_92677` bigtop, `20260724_022039_92960` tvm) and lands with a regression test in the house scripted-orchestrator style.

**Tech Stack:** Python 3.11+, pytest, existing SAG internals (`sag.tools.internal.*`, `sag.agent.*`).

**Spec:** `docs/superpowers/specs/2026-07-25-advisor-mode-harness-redesign.md` §3.4, §3.3 (rejection-message standard, applied here only where §3.4 touches gates).

## Global Constraints

- Never add `Co-Authored-By` trailers to git commits (repo owner rule).
- Tools must never coach or perform evidence destruction (`-Dtest=!`, `-DskipTests=true` suggestions, automatic exclusions) — spec §3.4-3.
- All rejection messages must name a concrete, machine-derived repair action — spec §3.3.
- Full suite must stay green: baseline is 2,439 passed (`python -m pytest tests/ -q`).
- Tests follow the house scripted-orchestrator pattern (see `tests/test_venv_repair_ladder.py`, `tests/test_test_attempt_policy.py`): plain classes whose `execute_command(command, workdir=None, timeout=None)` returns `{"success": bool, "exit_code": int, "output": str}` dicts, first matching substring rule wins, every command recorded.
- `docs/` is gitignored; plan/spec commits need `git add -f`. Source and test commits are normal.
- Scope guard: do NOT touch `react_engine.py`, `reasoning_scheduler.py`, `agent_state_evaluator.py`, or prompts — protocol and signal-layer changes belong to Plan 2/3.

## Deviations from spec (deliberate staging)

- **§3.4-1 full side-effect-free clone is deferred to Plan 2.** Removing all
  dependency auto-install from the clone verb changes the model-visible flow
  that the current THINK/ACTION protocol (and the commons-cli acceptance
  baseline) depends on; the new protocol's system prompt absorbs it. Plan 1
  lands the ladder-reachability half, which removes the exact 2026-07-24 TVM
  death (Task 1).
- **Report-layer fixes** (a failed build cannot display "Blockers (0)";
  recommendations from surveyed roots) ride with Plan 3.

---

### Task 1: Make the venv repair ladder reachable from clone-time Python provisioning

The 2026-07-24 TVM run died because `_install_python_dependencies` returns
immediately when plain `python3 -m venv` fails — the `ensure_venv_pip` ladder
five lines below (whose rung 4 apt-installs `python3.12-venv`, the exact
missing package) is unreachable for creation failures. Spec §3.4-1 (minimal
compatible repair).

**Files:**
- Modify: `src/sag/tools/internal/project_setup_tool.py` (in `_install_python_dependencies`, the venv-creation block at ~line 1170)
- Test: `tests/test_clone_venv_ladder_reachable.py` (new)

**Interfaces:**
- Consumes: `ensure_venv_pip(orchestrator, venv, python_version=...)` → `{"ok": bool, "action": ..., "ladder": [...]}` (unchanged, `src/sag/tools/internal/python_env.py:302`).
- Produces: `_install_python_dependencies` no longer hard-fails on venv-creation failure unless the ladder is also exhausted; failure dict gains `"(repair ladder exhausted: ...)"` in `error`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_clone_venv_ladder_reachable.py`:

```python
# tests/test_clone_venv_ladder_reachable.py
"""Clone-time python provisioning must fall into the ensure_venv_pip ladder
when plain `python3 -m venv` fails (2026-07-24 TVM run: Debian ensurepip
split; the ladder existed but sat AFTER an early return)."""

from types import SimpleNamespace

import sag.tools.internal.project_setup_tool as pst
from sag.tools.internal.project_setup_tool import ProjectSetupTool

VENV = "/workspace/tvm/.venv"

ENSUREPIP_ERROR = (
    "The virtual environment was not created successfully because ensurepip is not\n"
    "available.  On Debian/Ubuntu systems, you need to install the python3-venv\n"
    "package using the following command.\n\n    apt install python3.12-venv\n"
)


class CloneVenvOrch:
    """TVM shape: plain venv creation fails; apt rung restores pip."""

    def __init__(self, apt_ok=True):
        self.apt_ok = apt_ok
        self.apt_installed = False
        self.commands = []

    def execute_command(self, command, workdir=None, timeout=None, **kwargs):
        self.commands.append(command)
        if "test -x" in command and ".venv/bin/python" in command:
            return {"success": True, "exit_code": 0, "output": "MISSING"}
        if command == f"python3 -m venv {VENV}":
            return {"success": False, "exit_code": 1, "output": ENSUREPIP_ERROR}
        if "-m pip --version" in command:
            if self.apt_installed:
                return {"success": True, "exit_code": 0, "output": "pip 24.0"}
            return {"success": False, "exit_code": 1, "output": "No module named pip"}
        if "-m ensurepip" in command:
            return {"success": False, "exit_code": 1, "output": "No module named ensurepip"}
        if "apt-get install" in command and "python3-venv" in command:
            if self.apt_ok:
                self.apt_installed = True
                return {"success": True, "exit_code": 0, "output": "installed"}
            return {"success": False, "exit_code": 100, "output": "apt failed"}
        if "python3 --version" in command:
            return {"success": True, "exit_code": 0, "output": "Python 3.12.3"}
        if command.startswith("ls -A1"):
            return {"success": True, "exit_code": 0, "output": ""}
        return {"success": True, "exit_code": 0, "output": ""}


def _tool(orch, monkeypatch):
    monkeypatch.setattr(
        pst,
        "PythonPreflight",
        lambda orchestrator: SimpleNamespace(
            run=lambda *a, **k: SimpleNamespace(
                provisioned=False, narration=None, active_version=None
            )
        ),
    )
    monkeypatch.setattr(pst, "read_build_requirements", lambda orchestrator: {})
    tool = ProjectSetupTool.__new__(ProjectSetupTool)
    tool.orchestrator = orch
    return tool


def test_venv_creation_failure_enters_the_repair_ladder(monkeypatch):
    orch = CloneVenvOrch(apt_ok=True)
    result = _tool(orch, monkeypatch)._install_python_dependencies("/workspace/tvm")

    assert result["success"] is True
    apt_commands = [c for c in orch.commands if "python3-venv" in c]
    assert apt_commands, "ladder rung 4 (apt python3-venv) never ran"


def test_exhausted_ladder_still_fails_honestly(monkeypatch):
    orch = CloneVenvOrch(apt_ok=False)
    result = _tool(orch, monkeypatch)._install_python_dependencies("/workspace/tvm")

    assert result["success"] is False
    assert "repair ladder exhausted" in result["error"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_clone_venv_ladder_reachable.py -v`
Expected: FAIL — first test gets `result["success"] is False` (early return fires before the ladder), second test's error lacks "repair ladder exhausted".

- [ ] **Step 3: Implement the fix**

In `src/sag/tools/internal/project_setup_tool.py`, `_install_python_dependencies`, replace:

```python
        # Venv next. A provisioning pre-flight already created it (uv/apt).
        if not outcome.provisioned and not self._python_venv_exists(venv):
            made = self.orchestrator.execute_command(f"python3 -m venv {venv}", workdir=directory)
            if not made.get("success"):
                return {
                    "success": False,
                    "error": f"could not create venv at {venv}: {made.get('output', '')}",
                    "exit_code": made.get("exit_code"),
                    "venv": venv,
                }
```

with:

```python
        # Venv next. A provisioning pre-flight already created it (uv/apt).
        creation_failure: Optional[Dict[str, Any]] = None
        if not outcome.provisioned and not self._python_venv_exists(venv):
            made = self.orchestrator.execute_command(f"python3 -m venv {venv}", workdir=directory)
            if not made.get("success"):
                # Do NOT return here. The exact live failure (Debian splits
                # ensurepip out of the system python — 2026-07-24 TVM run) is
                # what the ensure_venv_pip ladder below repairs; an early
                # return made that ladder unreachable for creation failures.
                creation_failure = made
```

Then, directly after the existing `repair = ensure_venv_pip(...)` /
`repair_note` block, add:

```python
        if creation_failure is not None and not repair.get("ok"):
            return {
                "success": False,
                "error": (
                    f"could not create venv at {venv}: "
                    f"{creation_failure.get('output', '')} "
                    f"(repair ladder exhausted: {', '.join(repair.get('ladder') or [])})"
                ),
                "exit_code": creation_failure.get("exit_code"),
                "venv": venv,
            }
```

(`Optional`, `Dict`, `Any` are already imported in this module.)

- [ ] **Step 4: Run the new tests and the neighbouring suites**

Run: `python -m pytest tests/test_clone_venv_ladder_reachable.py tests/test_venv_repair_ladder.py tests/test_python_preflight.py tests/test_provision_priority.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sag/tools/internal/project_setup_tool.py tests/test_clone_venv_ladder_reachable.py
git commit -m "fix: venv creation failure enters ensure_venv_pip ladder at clone time"
```

---

### Task 2: Shell-safe Maven commands and a distinct launcher parse error

The 2026-07-24 bigtop run died on `bash: -c: line 1: syntax error near
unexpected token '('` — `_build_maven_command` joins raw tokens with spaces
and the detached runner embeds them in `bash -c` unquoted. Spec §3.4-2.

**Files:**
- Modify: `src/sag/tools/internal/maven_tool.py:913-989` (`_build_maven_command`)
- Modify: `src/sag/docker_orch/orch.py:1236-1241` (`collect_detached_result`, vanished-process branch)
- Test: `tests/test_maven_command_quoting.py` (new)

**Interfaces:**
- Consumes: nothing new. `shlex` is already imported in both files (`maven_tool.py:5`; verify in `orch.py` — it uses `shlex.quote` at :1227).
- Produces: `_build_maven_command(...)` still returns a `str`, but every token is `shlex.quote`d; `collect_detached_result` marks bash parse failures with `"[launcher error: ..."` instead of the generic no-exit-code note.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_maven_command_quoting.py`:

```python
# tests/test_maven_command_quoting.py
"""Maven command construction must be shell-safe token-by-token (2026-07-24
bigtop run: internally generated -Dtest=!name(pkg#Class) tokens reached
bash -c unquoted and killed the launcher before the exit marker)."""

import re
import shlex

import pytest

from sag.docker_orch.orch import DockerOrchestrator
from sag.tools.internal.maven_tool import MavenTool

SUREFIRE_EXCLUSION = "test=!installBash(org.apache.bigtop.itest.pmanager#PackageManagerTest),!regularUserShell(org.apache.bigtop.itest.shell#ShellTest)"


def _tool():
    tool = MavenTool.__new__(MavenTool)
    tool.orchestrator = None
    return tool


def test_exclusion_tokens_are_quoted_at_the_shell_boundary():
    cmd = _tool()._build_maven_command(
        command="compile",
        goals="",
        profiles="",
        properties=[SUREFIRE_EXCLUSION],
        fail_at_end=True,
    )
    # The QUOTED literal must appear in the command string: an unquoted
    # -Dtest=!name(pkg#Class) token survives shlex.split round-trips by luck
    # but kills bash -c. shlex.quote is the contract.
    assert shlex.quote(f"-D{SUREFIRE_EXCLUSION}") in cmd
    tokens = shlex.split(cmd)
    assert tokens[0] == "mvn"
    assert "--fail-at-end" in tokens
    assert f"-D{SUREFIRE_EXCLUSION}" in tokens


def test_plain_command_is_unchanged_in_shape():
    cmd = _tool()._build_maven_command(command="clean install", goals="", profiles="", properties="")
    assert shlex.split(cmd) == ["mvn", "clean", "install"]


BASH_PARSE_LOG = "bash: -c: line 1: syntax error near unexpected token `('\nbash: -c: line 1: `set +e; ...'"


class _FakeOrch(DockerOrchestrator):
    def __init__(self):
        pass

    def execute_command(self, command, workdir=None, timeout=None, truncate_output=True):
        return {"success": True, "exit_code": 0, "output": BASH_PARSE_LOG}


def test_bash_parse_failure_is_a_distinct_launcher_error(monkeypatch):
    orch = _FakeOrch()
    monkeypatch.setattr(_FakeOrch, "_detached_poll_state", lambda self, poll: "vanished")
    monkeypatch.setattr(
        _FakeOrch, "_truncate_output_smartly", lambda self, text: text, raising=False
    )
    result = orch.collect_detached_result(
        {"log_path": "/tmp/sag_jobs/x.log", "job_id": "x", "pid": 1},
        {"exit_code": None},
    )
    assert result["exit_code"] == 1
    assert "[launcher error:" in result["full_output"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_maven_command_quoting.py -v`
Expected: FAIL — the first test fails on `shlex.quote(...) in cmd` (the current builder joins raw tokens, so the quoted literal is absent), and the launcher test fails with the generic `"ended without recording an exit code"` marker instead of `"[launcher error:"`.

- [ ] **Step 3: Implement the quoting fix**

In `_build_maven_command`, replace the tail of the method — from
`# Add command and goals` down to `return " ".join(cmd_parts)` — with:

```python
        # Add command and goals as real argv tokens
        if isinstance(command, list):
            cmd_parts.extend(str(part) for part in command)
        else:
            cmd_parts.extend(shlex.split(str(command or "")))
        if goals:
            cmd_parts.extend(shlex.split(str(goals)))

        # Extra args appended verbatim after main command
        if extra_args:
            if isinstance(extra_args, list):
                cmd_parts.extend(str(arg) for arg in extra_args)
            else:
                cmd_parts.extend(shlex.split(extra_args))

        # Quote every token at the single shell boundary: the detached runner
        # embeds this string in bash -c. Raw joins let internally generated
        # arguments containing (), #, ! or commas reach bash unquoted — the
        # 2026-07-24 bigtop launcher died on exactly that.
        return " ".join(shlex.quote(part) for part in cmd_parts)
```

(Everything above — executable/wrapper selection, `--fail-at-end`,
profiles, properties, `-f pom` — stays as is; those append single tokens
into `cmd_parts`.)

- [ ] **Step 4: Implement the launcher-error marker**

In `src/sag/docker_orch/orch.py`, `collect_detached_result`, replace:

```python
        if exit_code is None and state == "vanished":
            # A vanished process with no exit file is explicit crash evidence.
            exit_code = 1
            full_output += "\n[detached command ended without recording an exit code]"
```

with:

```python
        if exit_code is None and state == "vanished":
            # A vanished process with no exit file is explicit crash evidence.
            exit_code = 1
            if re.search(r"bash: -c: line \d+: .*syntax error", full_output):
                # The launcher itself failed bash parsing — no inner process
                # ever ran. Keep this distinct from an inner-command crash.
                full_output += (
                    "\n[launcher error: the dispatched command failed bash "
                    "parsing before execution — no inner process ran]"
                )
            else:
                full_output += "\n[detached command ended without recording an exit code]"
```

Verify `import re` exists at the top of `orch.py` (`grep -n "^import re" src/sag/docker_orch/orch.py`); add it if absent.

- [ ] **Step 5: Run the new tests and Maven suites**

Run: `python -m pytest tests/test_maven_command_quoting.py tests/test_build_tool.py -v`
Expected: all PASS. If any existing test asserts on the exact unquoted command string, update its expectation to the quoted form (quoting is the new contract).

- [ ] **Step 6: Commit**

```bash
git add src/sag/tools/internal/maven_tool.py src/sag/docker_orch/orch.py tests/test_maven_command_quoting.py
git commit -m "fix: shell-quote every Maven token; classify bash parse failures as launcher errors"
```

---

### Task 3: Delete automatic Maven failed-test/module exclusion recovery

The harness auto-converted failing tests into `-Dtest=!...` reruns
(`tool_recovery.py:484-545`) — destroying evidence and, combined with Task 2's
bug, killing the bigtop launcher. Spec §3.4-3: the test phase never converts
failures into exclusions.

**Files:**
- Modify: `src/sag/agent/tool_recovery.py` — remove the `_recover_maven_exclusions` call block in `_recover_maven_error` (~line 336) and delete methods `_recover_maven_exclusions` (:484), `_normalize_properties` (:993), `_ensure_flag` (:1002), `_set_property` (:1007), `_format_test_exclusion` (:1012) — grep confirms all four helpers are referenced only by `_recover_maven_exclusions`.
- Test: `tests/test_no_auto_test_exclusion.py` (new)

**Interfaces:**
- Consumes: `ToolRecovery(tools=..., context_manager=..., successful_states=..., repository_url=..., add_system_guidance=...)` (`tool_recovery.py:19`).
- Produces: `_recover_maven_error` returns the `maven_no_strategy` no-strategy decision for analysis-bearing test failures; `RecoveryDecision.should_recover` is False for them.

- [ ] **Step 1: Write the failing test**

Create `tests/test_no_auto_test_exclusion.py`:

```python
# tests/test_no_auto_test_exclusion.py
"""Failed tests/modules must never be auto-converted into Maven exclusions
(spec §3.4-3: exclusion is evidence destruction, not recovery)."""

from types import SimpleNamespace

from sag.agent.tool_recovery import ToolRecovery
from sag.tools.base import ToolResult


class FakeMaven:
    def __init__(self):
        self.calls = []

    def safe_execute(self, **params):
        self.calls.append(params)
        return ToolResult.completed_success(output="BUILD SUCCESS")


def _recovery(maven):
    return ToolRecovery(
        tools={"maven": maven},
        context_manager=SimpleNamespace(orchestrator=None),
        successful_states={},
        repository_url=None,
        add_system_guidance=lambda *a, **k: None,
    )


def test_failed_tests_are_not_excluded_and_not_rerun():
    maven = FakeMaven()
    failed = ToolResult.completed_failure(
        output="Tests run: 45, Failures: 1, Errors: 12",
        error="test failures",
        error_code="TEST_FAILURE",
        metadata={
            "analysis": {
                "failed_tests": ["regularUserShell(org.apache.bigtop.itest.shell.ShellTest)"],
                "failed_modules": [{"artifact_id": "itest-common", "pom_path": "pom.xml"}],
            }
        },
    )
    decision = _recovery(maven)._recover_maven_error(
        {"command": "install", "working_directory": "/workspace/bigtop"}, failed
    )
    assert decision.should_recover is False
    assert maven.calls == []


def test_exclusion_machinery_is_gone():
    assert not hasattr(ToolRecovery, "_recover_maven_exclusions")
    assert not hasattr(ToolRecovery, "_format_test_exclusion")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_no_auto_test_exclusion.py -v`
Expected: FAIL — the recovery currently reruns Maven with `-Dtest=!...` (`maven.calls` non-empty, `should_recover` True) and the methods exist.

- [ ] **Step 3: Delete the machinery**

In `_recover_maven_error`, delete:

```python
        if analysis:
            decision = self._recover_maven_exclusions(params, analysis)
            if decision.should_recover:
                return decision
```

Then delete the whole method bodies of `_recover_maven_exclusions`,
`_normalize_properties`, `_ensure_flag`, `_set_property`,
`_format_test_exclusion`. Run
`grep -n "_normalize_properties\|_ensure_flag\|_set_property\|_format_test_exclusion\|_recover_maven_exclusions" src/sag/agent/tool_recovery.py`
— expected: no matches. Remove now-unused imports if flagged
(`python -m pyflakes src/sag/agent/tool_recovery.py` or rely on the suite).

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_no_auto_test_exclusion.py tests/test_stage1_review_fixes.py -v`
Expected: PASS. If an existing test asserts the exclusion behavior, delete that test with a comment pointing at spec §3.4-3.

- [ ] **Step 5: Commit**

```bash
git add src/sag/agent/tool_recovery.py tests/test_no_auto_test_exclusion.py
git commit -m "fix: remove automatic Maven failed-test/module exclusion recovery"
```

---

### Task 4: Remove evidence-destruction coaching from Maven suggestions

`maven_tool.py:1986-2028` tells the model to skip failing tests
(`-Dtest=!X`), combine exclusions, exclude failing modules, and use
`-DskipTests=true`. Spec §3.4-3: tools must not coach evidence destruction.

**Files:**
- Modify: `src/sag/tools/internal/maven_tool.py:1980-2030`
- Test: `tests/test_no_exclusion_coaching.py` (new)

**Interfaces:**
- Produces: failure suggestions list failing modules/tests informationally and point at Surefire reports; no exclusion/skip instructions anywhere in the module.

- [ ] **Step 1: Write the failing test**

Create `tests/test_no_exclusion_coaching.py`:

```python
# tests/test_no_exclusion_coaching.py
"""Source-level tripwire: no SAG tool may coach test exclusion or skipping
(spec §3.4-3). The strings below were live in the 2026-07-24 bigtop run."""

from pathlib import Path

BANNED = ["-Dtest=!", "-DskipTests=true", "-pl !"]
MODULES = [
    "src/sag/tools/internal/maven_tool.py",
    "src/sag/agent/tool_recovery.py",
]


def test_no_module_coaches_exclusions():
    for module in MODULES:
        source = Path(module).read_text()
        for banned in BANNED:
            assert banned not in source, f"{module} still contains {banned!r}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_no_exclusion_coaching.py -v`
Expected: FAIL naming `maven_tool.py` and `-Dtest=!`.

- [ ] **Step 3: Rewrite the suggestion blocks**

In `maven_tool.py` (~1980), replace the `failed_modules` suggestion loop
(both `artifact_id` and `pom_path` branches plus the
"Excluding the failing module lets the remaining reactor modules finish..."
line) with:

```python
            for module_info in analysis["failed_modules"][:3]:
                module_hint = module_info.get("pom_path") or module_info.get("artifact_id")
                if module_hint:
                    error_suggestions.append(
                        f"Module failed: {module_hint} — read its build output above for the root cause"
                    )
```

Replace the `failed_tests` skip-suggestion loop plus the
"Combine multiple exclusions with commas..." line with:

```python
            for failed_test in analysis["failed_tests"][:5]:
                error_suggestions.append(f"Failing test: {failed_test}")
```

Replace the line
`"Fix failing tests or use -DskipTests=true to skip tests temporarily",`
with:

```python
                    "Fix the failing tests; the Surefire reports above carry the full failure context",
```

(The `_suggested_build_action` helper may become unused — check with
`grep -n "_suggested_build_action" src/sag/tools/internal/maven_tool.py`;
delete it only if this was its last call site.)

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_no_exclusion_coaching.py tests/test_build_tool.py -v`
Expected: PASS (update any existing assertion that expected the old coaching strings).

- [ ] **Step 5: Commit**

```bash
git add src/sag/tools/internal/maven_tool.py tests/test_no_exclusion_coaching.py
git commit -m "fix: replace exclusion/skip coaching with informational failure suggestions"
```

---

### Task 5: XML oracle counts every runtime testcase, including Groovy

The sealed bigtop verdict reported 4/4 passed while raw Surefire XML held
45 tests / 1 failure / 12 errors — `physical_validator.py` drops every
runtime testcase whose class name matches a `src/test/groovy` source file.
Spec §3.4-4: runtime JUnit XML is ground truth; implementation language is
not an exclusion criterion.

**Files:**
- Modify: `src/sag/agent/physical_validator.py:1431-1444` (groovy set computation), `:1470-1472` (call site), `:1727-1830` (`_parse_single_test_xml` signature + 3 filter branches)
- Test: `tests/test_groovy_tests_survive_oracle.py` (new)

**Interfaces:**
- Produces: `_parse_single_test_xml(self, xml_content: str, file_path: str)` — two-argument signature; returns `{"total", "passed", "failed", "errors", "skipped", "testcases"}` computed over ALL testcases.

- [ ] **Step 1: Write the failing test**

Create `tests/test_groovy_tests_survive_oracle.py`:

```python
# tests/test_groovy_tests_survive_oracle.py
"""Bigtop-shaped regression: Groovy testcase failures/errors must survive
canonical XML aggregation (spec §3.4-4 — the 2026-07-24 run sealed 4/4
after filtering the failing Groovy classes out)."""

import inspect

from sag.agent.physical_validator import PhysicalValidator

BIGTOP_SUITE = """<testsuite name="org.apache.bigtop.itest.pmanager.PackageManagerTest" tests="3" failures="1" errors="1" skipped="0">
  <testcase classname="org.apache.bigtop.itest.pmanager.PackageManagerTest" name="testGetDeps" time="0.1"><failure message="boom"/></testcase>
  <testcase classname="org.apache.bigtop.itest.pmanager.PackageManagerTest" name="testGetDocs" time="0.1"><error message="crash"/></testcase>
  <testcase classname="org.apache.bigtop.itest.pmanager.PackageManagerTest" name="installBash" time="0.1"/>
</testsuite>"""


def _validator():
    return PhysicalValidator.__new__(PhysicalValidator)


def test_groovy_named_testcases_are_counted():
    stats = _validator()._parse_single_test_xml(BIGTOP_SUITE, "TEST-PackageManagerTest.xml")
    assert stats["total"] == 3
    assert stats["failed"] == 1
    assert stats["errors"] == 1
    assert stats["passed"] == 1


def test_parser_has_no_language_filter_parameter():
    signature = inspect.signature(PhysicalValidator._parse_single_test_xml)
    assert "groovy_test_classes" not in signature.parameters
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_groovy_tests_survive_oracle.py -v`
Expected: the first test PASSES pre-fix only if no groovy set is passed — the signature test FAILS (`groovy_test_classes` present). (The live defect needs the set populated; the signature deletion is what makes it structurally impossible.)

- [ ] **Step 3: Delete the filter**

1. Delete the block at `physical_validator.py:1431-1444` — from
   `# First, identify which test classes are from Groovy sources` through the
   `logger.info(f"📊 Identified {len(groovy_test_classes)} Groovy test classes to exclude")` line.
2. Change the call at :1470 to
   `stats = self._parse_single_test_xml(xml_content, report_file)`.
3. Change the signature to
   `def _parse_single_test_xml(self, xml_content: str, file_path: str) -> Optional[Dict[str, int]]:`
   and drop the "Excludes Groovy test classes if provided." docstring line.
4. In all three format branches (`testsuite`, `testsuites`, fallback
   `.//testsuite`), replace the
   `if groovy_test_classes: ... else: testcase_entries = all_testcases`
   conditional with the single line
   `testcase_entries = all_testcases`.

Run `grep -n "groovy" src/sag/agent/physical_validator.py` — expected: no
remaining hits in the runtime-aggregation path (hits in the static-scan
region around line 336 count Groovy *sources* and stay).

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_groovy_tests_survive_oracle.py tests/test_physical_validator.py tests/test_snapshot_surface_agreement.py -v`
Expected: PASS. Any existing test that asserts Groovy filtering must be inverted (comment: spec §3.4-4).

- [ ] **Step 5: Commit**

```bash
git add src/sag/agent/physical_validator.py tests/test_groovy_tests_survive_oracle.py
git commit -m "fix: XML oracle counts every runtime testcase — remove Groovy class filter"
```

---

### Task 6: Gradle wrapper unzip prerequisite recovery

The bigtop Spark island failed with `/workspace/bigtop/gradlew: line 180:
unzip: command not found` and no recovery fired. Spec §3.4-5: lazily
install `unzip` when the wrapper needs it, retry once.

**Files:**
- Modify: `src/sag/agent/tool_recovery.py` — `_recover_gradle_error` (:596), add a branch before the final `_no_strategy` return
- Test: `tests/test_gradle_unzip_recovery.py` (new)

**Interfaces:**
- Consumes: `self._execute_workspace_recovery_command(command)` (:793) — runs via `context_manager.orchestrator` or the bash tool.
- Produces: recovery strategy `"gradle_install_unzip"`; one-shot latch `successful_states["gradle_unzip_installed"]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gradle_unzip_recovery.py`:

```python
# tests/test_gradle_unzip_recovery.py
"""Gradle wrapper unzip prerequisite: install once, retry once (spec §3.4-5;
2026-07-24 bigtop: gradlew line 180 unzip: command not found, no recovery)."""

from types import SimpleNamespace

from sag.agent.tool_recovery import ToolRecovery
from sag.tools.base import ToolResult

UNZIP_ERROR = "/workspace/bigtop/gradlew: line 180: unzip: command not found"


class RecordingOrch:
    def __init__(self):
        self.commands = []

    def execute_command(self, command, workdir=None, timeout=None, **kwargs):
        self.commands.append(command)
        return {"success": True, "exit_code": 0, "output": "ok"}


class FakeGradle:
    def __init__(self):
        self.calls = []

    def safe_execute(self, **params):
        self.calls.append(params)
        return ToolResult.completed_success(output="BUILD SUCCESSFUL")


def _recovery(orch, gradle, states=None):
    return ToolRecovery(
        tools={"gradle": gradle},
        context_manager=SimpleNamespace(orchestrator=orch),
        successful_states=states if states is not None else {},
        repository_url=None,
        add_system_guidance=lambda *a, **k: None,
    )


def _failed():
    return ToolResult.completed_failure(
        output=UNZIP_ERROR, error=UNZIP_ERROR, error_code="GRADLE_BUILD_FAILED"
    )


def test_unzip_missing_installs_and_retries_once():
    orch, gradle = RecordingOrch(), FakeGradle()
    decision = _recovery(orch, gradle)._recover_gradle_error(
        {"tasks": "test", "working_directory": "/workspace/bigtop/bigtop-bigpetstore/bigpetstore-spark"},
        _failed(),
    )
    assert decision.should_recover is True
    assert any("apt-get install -y unzip" in c for c in orch.commands)
    assert len(gradle.calls) == 1


def test_unzip_install_is_one_shot():
    orch, gradle = RecordingOrch(), FakeGradle()
    states = {"gradle_unzip_installed": True}
    decision = _recovery(orch, gradle, states)._recover_gradle_error(
        {"tasks": "test", "working_directory": "/workspace/bigtop"}, _failed()
    )
    assert decision.should_recover is False
    assert orch.commands == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_gradle_unzip_recovery.py -v`
Expected: FAIL — no strategy fires today (`should_recover` False, no apt command).

- [ ] **Step 3: Implement the recovery branch**

In `_recover_gradle_error`, immediately before the final
`return self._no_strategy("gradle_no_strategy", ...)`, add:

```python
        combined = f"{error_msg}\n{failed_result.output or ''}"
        if "unzip: command not found" in combined and not self.successful_states.get(
            "gradle_unzip_installed"
        ):
            # Gradle wrapper prerequisite (spec §3.4-5): the wrapper needs
            # unzip to unpack its distribution. Install the one allowlisted
            # package and retry the identical invocation once.
            install = self._execute_workspace_recovery_command(
                "DEBIAN_FRONTEND=noninteractive apt-get update -qq && "
                "DEBIAN_FRONTEND=noninteractive apt-get install -y unzip"
            )
            if install.get("success"):
                self.successful_states["gradle_unzip_installed"] = True
                result = gradle_tool.safe_execute(**params)
                return self._attempted(
                    strategy="gradle_install_unzip",
                    message=(
                        "Recovered by installing unzip (Gradle wrapper "
                        "prerequisite) and retrying once"
                    ),
                    result=result,
                    recovery_params=dict(params),
                )
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_gradle_unzip_recovery.py tests/test_stage1_review_fixes.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sag/agent/tool_recovery.py tests/test_gradle_unzip_recovery.py
git commit -m "feat: install unzip and retry once when the Gradle wrapper lacks it"
```

---

### Task 7: The primary test coordinate must receive the receipt

`terminal_test_receipts` discharges the mandatory test attempt on a receipt
from ANY survey candidate — the bigtop run satisfied it with the auxiliary
Maven island and never ran `bigtop-data-generators`. Spec §3.4-6: one
receipt at manifest `test_root`/`test_system` is mandatory; auxiliary
islands cannot substitute.

**Files:**
- Modify: `src/sag/agent/attempt_policy.py` — `TestCandidateResolution` (:69), `resolve_survey_test_candidates` (:211), `required_test_attempt` (:622)
- Test: extend `tests/test_test_attempt_policy.py`

**Interfaces:**
- Produces: `TestCandidateResolution.primary: TestAttemptRequirement | None` (the candidate built from manifest `test_root`/`test_system`; `None` when the manifest has no valid test_root). `required_test_attempt` discharges only on receipts/refusals/polls bound to `primary` when it exists, and returns `primary` as the required action.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_test_attempt_policy.py` (reusing the file's
`ManifestOrchestrator`, `_ready_state`, `_record_gradle_test` helpers):

```python
def _terminal_gradle_result():
    return ToolResult.completed_success(
        output="BUILD SUCCESSFUL\n2 actionable tasks: 2 executed",
        metadata={"runner_dispatched": True, "command": "./gradlew test"},
    )


def test_auxiliary_island_receipt_does_not_discharge_the_primary():
    orchestrator = ManifestOrchestrator()
    orchestrator.manifest["test_islands"] = [
        {"root": "/workspace/bigtop/bigtop-test-framework", "system": "gradle"},
        {"root": "/workspace/bigtop/bigtop-data-generators", "system": "gradle"},
    ]
    state = _ready_state()
    _record_gradle_test(
        state, _terminal_gradle_result(), root="/workspace/bigtop/bigtop-test-framework"
    )
    requirement = required_test_attempt(
        state, orchestrator, phase="test", attempt_id="test-1"
    )
    assert requirement is not None
    assert requirement.root == "/workspace/bigtop/bigtop-data-generators"


def test_primary_receipt_discharges_the_requirement():
    orchestrator = ManifestOrchestrator()
    state = _ready_state()
    _record_gradle_test(
        state, _terminal_gradle_result(), root="/workspace/bigtop/bigtop-data-generators"
    )
    assert (
        required_test_attempt(state, orchestrator, phase="test", attempt_id="test-1")
        is None
    )


def test_resolution_exposes_the_primary_candidate():
    resolution = resolve_survey_test_candidates(ManifestOrchestrator())
    assert resolution.primary is not None
    assert resolution.primary.root == "/workspace/bigtop/bigtop-data-generators"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_test_attempt_policy.py -k "primary or auxiliary" -v`
Expected: FAIL — `TestCandidateResolution` has no `primary`; the auxiliary receipt currently discharges.

- [ ] **Step 3: Implement**

1. Add the field to the dataclass (`attempt_policy.py:69`):

```python
    status: CandidateResolutionStatus
    candidates: tuple[TestAttemptRequirement, ...] = ()
    project_root: str | None = None
    workspace_root: str | None = None
    primary: TestAttemptRequirement | None = None
```

2. In `resolve_survey_test_candidates`, replace the raw-candidate assembly

```python
    raw_candidates: list[tuple[Any, Any]] = []
    islands = manifest.get("test_islands") or ()
    ...
    for island in islands:
        if isinstance(island, Mapping):
            raw_candidates.append(
                (island.get("root"), island.get("system") or manifest.get("test_system"))
            )
    raw_candidates.append((manifest.get("test_root"), manifest.get("test_system")))
```

with a primary-first assembly that tags provenance:

```python
    raw_candidates: list[tuple[Any, Any, bool]] = []
    islands = manifest.get("test_islands") or ()
    if not isinstance(islands, (list, tuple)):
        return TestCandidateResolution(
            status="coordinates_missing",
            project_root=project_root,
            workspace_root=workspace_root,
        )
    # The manifest test_root/test_system pair is the PRIMARY coordinate and
    # is processed first (spec §3.4-6): auxiliary islands may add evidence
    # but can never substitute for it.
    raw_candidates.append((manifest.get("test_root"), manifest.get("test_system"), True))
    for island in islands:
        if isinstance(island, Mapping):
            raw_candidates.append(
                (island.get("root"), island.get("system") or manifest.get("test_system"), False)
            )
```

then thread the flag through the existing loop
(`for raw_root, raw_system in raw_candidates:` becomes
`for raw_root, raw_system, is_primary in raw_candidates:`), and where a
candidate is appended (and at the dedup `continue`), record the primary:

```python
        requirement = _candidate_requirement(root, system)
        if (root, system) in seen:
            if is_primary and primary is None:
                primary = next(
                    (c for c in candidates if c.root == root and c.system == system),
                    None,
                )
            continue
        seen.add((root, system))
        candidates.append(requirement)
        if is_primary and primary is None:
            primary = requirement
```

(initialize `primary: TestAttemptRequirement | None = None` before the
loop) and return it:

```python
    return TestCandidateResolution(
        status="available",
        candidates=tuple(candidates),
        project_root=project_root,
        workspace_root=workspace_root,
        primary=primary,
    )
```

3. In `required_test_attempt`, after `candidates = resolved.candidates`,
   bind the discharge set to the primary:

```python
    candidates = resolved.candidates
    primary = resolved.primary
    gate_candidates = (primary,) if primary is not None else candidates
    if terminal_test_receipts(state, attempt_id=attempt_id, candidates=gate_candidates):
        return None
    if forced_test_refusal_receipts(
        state,
        attempt_id=attempt_id,
        candidates=gate_candidates,
    ):
        return None
    pending = _pending_test_dispatches(
        state,
        attempt_id=attempt_id,
        candidates=gate_candidates,
    )
```

and change the final fallthrough `return candidates[0]` to
`return primary if primary is not None else candidates[0]`.

- [ ] **Step 4: Run the whole policy suite**

Run: `python -m pytest tests/test_test_attempt_policy.py tests/test_forced_build_graph.py tests/test_recommend_test_root.py -v`
Expected: PASS. Any pre-existing test that encoded the any-island discharge policy must be updated to the primary policy (comment: spec §3.4-6). The ordering test `test_candidates_keep_the_manifest_primary_coordinate_first` must still pass (primary is now structurally first).

- [ ] **Step 5: Commit**

```bash
git add src/sag/agent/attempt_policy.py tests/test_test_attempt_policy.py
git commit -m "fix: mandatory test attempt discharges only at the primary survey coordinate"
```

---

### Task 8: Build closure requires a build receipt; local prerequisites are not blockers

The TVM run closed the build phase `blocked` with zero build invocations,
citing a missing OS package as an external impediment. Spec §3.4-7 and the
§3.3 rejection-message standard.

**Files:**
- Modify: `src/sag/agent/attempt_policy.py` (new functions at module end, before `__all__`)
- Modify: `src/sag/tools/phase_tool.py` (in the terminal-claim handler, directly after the `required_test_attempt` rejection block at ~line 184)
- Test: `tests/test_build_closure_policy.py` (new)

**Interfaces:**
- Produces (in `attempt_policy`):
  - `has_build_attempt_receipt(state, *, attempt_id) -> bool` — True when this build attempt holds any `build`/`maven`/`gradle`/`python` observation whose metadata attests `runner_dispatched: True` with a non-empty `command`.
  - `build_attempt_requirement(state, orchestrator, *, phase, attempt_id) -> str | None` — rejection text when the build phase may not close; `None` when closure is legal (receipt present, or survey-proven no-target).
  - `local_prerequisite_signature(text) -> str | None` — the matched signature for local repairable prerequisites (`ensurepip is not available`, `command not found`, `no module named pip`, `no module named ensurepip`), else `None`.
- Consumes (in `phase_tool`): the two policy functions above; `PhaseOutcome` (already imported).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_build_closure_policy.py`:

```python
# tests/test_build_closure_policy.py
"""Build phase closure policy (spec §3.4-7): no blocked/failed closure
without one real build attempt receipt, and a missing OS package/venv module
is a local repairable prerequisite, never an external blocker."""

import json
from types import SimpleNamespace

from sag.agent.attempt_policy import (
    build_attempt_requirement,
    has_build_attempt_receipt,
    local_prerequisite_signature,
)
from sag.agent.evidence_state import RunEvidenceState, StateScope
from sag.tools.base import ToolResult
from sag.tools.phase_tool import PhaseTool


class BuildManifestOrch:
    def __init__(self, manifest=None):
        self.manifest = manifest if manifest is not None else {
            "build_system": "python",
            "test_root": "/workspace/tvm",
        }

    def execute_command(self, command, workdir=None, timeout=None):
        return {"success": True, "exit_code": 0, "output": json.dumps(self.manifest)}


def _state_with_build_receipt(attempt_id="build-1"):
    state = RunEvidenceState(run_id="build-policy")
    state.ingest_tool_result(
        StateScope.ARTIFACTS,
        "build",
        ToolResult.completed_failure(
            output="pip failed",
            error="deps failed",
            error_code="DEPS_FAILED",
            metadata={"runner_dispatched": True, "command": "pip install -e ."},
        ),
        params={"action": "deps", "working_directory": "/workspace/tvm"},
        source_phase="build",
        source_attempt_id=attempt_id,
    )
    return state


def test_no_build_attempt_blocks_closure():
    state = RunEvidenceState(run_id="build-policy")
    message = build_attempt_requirement(
        state, BuildManifestOrch(), phase="build", attempt_id="build-1"
    )
    assert message is not None
    assert "build attempt" in message


def test_a_real_attempt_allows_closure():
    state = _state_with_build_receipt()
    assert has_build_attempt_receipt(state, attempt_id="build-1") is True
    assert (
        build_attempt_requirement(
            state, BuildManifestOrch(), phase="build", attempt_id="build-1"
        )
        is None
    )


def test_local_prerequisites_are_classified():
    assert local_prerequisite_signature("ensurepip is not available") is not None
    assert local_prerequisite_signature("gradlew: line 180: unzip: command not found") is not None
    assert local_prerequisite_signature("network unreachable: proxy denied") is None


def _phase_tool(orch, state, gate):
    machine = SimpleNamespace(
        current_phase="build", is_complete=False, current_attempt_id="build-1"
    )
    tool = PhaseTool(
        machine=machine,
        validator=None,
        orchestrator=orch,
        project_name="tvm",
        gate_fn=gate,
    )
    tool.run_evidence_state = state
    return tool


def test_blocked_without_build_attempt_is_rejected_with_repair():
    state = RunEvidenceState(run_id="build-policy")
    gate_calls = []
    tool = _phase_tool(BuildManifestOrch(), state, lambda *a: gate_calls.append(a))
    result = tool.execute(
        action="blocked",
        outcome="failed",
        reason="ensurepip is not available in the environment",
        evidence=["output_x"],
    )
    assert result.succeeded is False
    assert result.error_code in ("BUILD_ATTEMPT_REQUIRED", "LOCAL_PREREQUISITE_NOT_BLOCKER")
    assert gate_calls == []  # rejected before the gate
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_build_closure_policy.py -v`
Expected: FAIL — ImportError on the three new names.

- [ ] **Step 3: Implement the policy functions**

At the end of `src/sag/agent/attempt_policy.py` (before `__all__`), add:

```python
_LOCAL_PREREQUISITE_SIGNATURES = (
    "ensurepip is not available",
    "command not found",
    "no module named pip",
    "no module named ensurepip",
)

_BUILD_RUNNER_TOOLS = frozenset({"build", "maven", "gradle", "python"})


def local_prerequisite_signature(text: str) -> str | None:
    """Match text against known local, mechanically repairable prerequisites."""
    lowered = (text or "").lower()
    for signature in _LOCAL_PREREQUISITE_SIGNATURES:
        if signature in lowered:
            return signature
    return None


def has_build_attempt_receipt(
    state: RunEvidenceState | None, *, attempt_id: str | None
) -> bool:
    """One real build-runner dispatch in this build attempt (terminal or not)."""
    if state is None or not attempt_id:
        return False
    for observation in state.tool_observations:
        if observation.source_phase != "build":
            continue
        if observation.source_attempt_id != attempt_id:
            continue
        if observation.tool_name not in _BUILD_RUNNER_TOOLS:
            continue
        metadata = observation.result.metadata or {}
        if metadata.get("runner_dispatched") is True and str(
            metadata.get("command") or ""
        ).strip():
            return True
    return False


def build_attempt_requirement(
    state: RunEvidenceState | None,
    orchestrator: Any,
    *,
    phase: str | None,
    attempt_id: str | None,
) -> str | None:
    """Reject build closure without one real build attempt (spec §3.4-7).

    Fail-closed: an unreadable survey manifest never proves a no-target
    project, so it still requires an attempt."""
    if state is None or phase != "build":
        return None
    if has_build_attempt_receipt(state, attempt_id=attempt_id):
        return None
    manifest: Any = None
    try:
        result = orchestrator.execute_command(f"cat {REQUIREMENTS_PATH}")
        if isinstance(result, Mapping) and result.get("success"):
            manifest = json.loads(str(result.get("output") or ""))
    except Exception:
        manifest = None
    if isinstance(manifest, Mapping) and manifest:
        islands = manifest.get("build_islands") or ()
        build_system = manifest.get("build_system") or (
            (manifest.get("build_recommendation") or {}).get("build_system")
        )
        if not islands and not build_system:
            return None  # survey-proven no-target project
    return (
        "Build phase cannot terminate before one real build attempt receipt. "
        "NEXT REQUIRED ACTION: build(action='compile') at the surveyed build "
        "root, or build(action='deps') when dependencies are the failure. "
        "Missing OS packages or venv modules are local repairable "
        "prerequisites, not external blockers."
    )
```

Add the three names to `__all__`.

- [ ] **Step 4: Hook the phase tool**

In `src/sag/tools/phase_tool.py`, import the two functions alongside the
existing `required_test_attempt` import, then insert directly AFTER the
`required_test_attempt` rejection block (and before `claim = PhaseClaim(...)`):

```python
        if verb == "blocked" or claimed_outcome is PhaseOutcome.FAILED:
            build_requirement = build_attempt_requirement(
                self.run_evidence_state,
                self.orchestrator,
                phase=phase,
                attempt_id=getattr(self.machine, "current_attempt_id", None),
            )
            if build_requirement is not None:
                return ToolResult.completed_failure(
                    output=build_requirement,
                    error="build phase has no build attempt receipt",
                    error_code="BUILD_ATTEMPT_REQUIRED",
                    suggestions=[
                        "Run build(action='compile') at the surveyed build root",
                        "If dependencies fail, run build(action='deps') first",
                    ],
                    metadata={"phase": phase},
                )

        if verb == "blocked":
            prerequisite = local_prerequisite_signature(
                " ".join((reason or "", *tuple(evidence or ())))
            )
            if prerequisite is not None:
                return ToolResult.completed_failure(
                    output=(
                        f"blocked-claim rejected: '{prerequisite}' is a local, "
                        "repairable prerequisite, not an external impediment. "
                        "Install the missing piece (OS packages via bash "
                        "apt-get install, python venv/pip via "
                        "build(action='deps')) and retry before claiming blocked."
                    ),
                    error="local prerequisite misclassified as external blocker",
                    error_code="LOCAL_PREREQUISITE_NOT_BLOCKER",
                    suggestions=[
                        "Install the missing prerequisite, then retry the failed action",
                        "Claim blocked only after the tool-owned repair ladder is exhausted",
                    ],
                    metadata={"phase": phase, "prerequisite": prerequisite},
                )
```

Note: `self.run_evidence_state` may be absent on old constructions — use
`getattr(self, "run_evidence_state", None)` when passing it if the
attribute is optional in `PhaseTool.__init__` (check the constructor; the
attempt-policy tests set it post-construction).

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_build_closure_policy.py tests/test_phase_tool.py tests/test_phase_tool_outcome.py tests/test_test_attempt_policy.py -v`
Expected: PASS. Existing phase-tool tests constructed for non-build phases are unaffected (`phase != "build"` short-circuits); build-phase tests without evidence state pass `run_evidence_state=None` → policy returns `None`.

- [ ] **Step 6: Commit**

```bash
git add src/sag/agent/attempt_policy.py src/sag/tools/phase_tool.py tests/test_build_closure_policy.py
git commit -m "feat: build closure requires a build receipt; local prerequisites rejected as blockers"
```

---

### Task 9: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the entire suite**

Run: `python -m pytest tests/ -q`
Expected: **0 failures**; total count ≥ baseline 2,439 plus the ~15 new tests. Fix any breakage inside the task that introduced it (each earlier task names its neighbouring suites).

- [ ] **Step 2: Verify the tripwires end-to-end**

Run: `grep -rn "Dtest=!" src/sag/ ; grep -rn "groovy_test_classes" src/sag/agent/physical_validator.py`
Expected: no output from either.

- [ ] **Step 3: Commit any stragglers and report**

```bash
git status --short
```
Expected: clean. Report the final test count and the list of task commits.

Cold-run acceptance (bigtop/tvm ×2, spec §3.7) is exercised after Plan 2/3 —
but a single optional smoke cold run of TVM on this branch is worthwhile:
`python -m sag ... tvm @ 828d117e...` should now show the venv ladder
executing during clone instead of the 2026-07-24 dead-end.
