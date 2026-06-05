# Harness Toolchain Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make SAG default to Ubuntu 24.04 and harden Maven version observations, build-task completion gates, output storage writes, and CLI setup failure exit codes without adding a new tool.

**Architecture:** Keep tool autonomy explicit: existing tools expose better facts and stricter state transitions, while the agent still chooses corrective actions. `MavenTool` owns Maven runtime metadata, `ToolOrchestrator` renders that metadata into observations, `ContextTool` gates task completion, `OutputStorageManager` owns safe container writes, and the CLI returns accurate process status.

**Tech Stack:** Python, Click, Pydantic config, pytest, fake orchestrators, existing SAG tool contracts.

---

## File Structure

- Modify `src/sag/config/settings.py`
  - Change the default Docker base image from `ubuntu:22.04` to `ubuntu:24.04`.
  - Keep `SAG_DOCKER_BASE_IMAGE` override behavior.
- Modify `.env.example`
  - Update the documented default base image to `ubuntu:24.04`.
- Modify `.env`
  - Update the local default base image to `ubuntu:24.04`.
  - Treat this as local-only configuration. Do not stage or commit `.env`,
    because it is ignored and may contain secrets.
- Modify `tests/test_config_settings.py`
  - Add tests for default and env override behavior.
- Modify `src/sag/tools/maven_tool.py`
  - Add Maven runtime metadata to failed Maven results.
  - Keep using existing `maven_version_requirement` and `ToolchainManager`; do not add a new tool or hidden installer.
- Modify `src/sag/agent/tool_orchestration.py`
  - Render Maven version requirement/runtime metadata in observation text.
- Modify `tests/test_maven_gradle_tool_contracts.py`
  - Add failed-result metadata tests for Maven runtime facts.
- Modify `tests/test_tool_orchestration_execution.py`
  - Add observation-format tests for Maven version contracts.
- Modify `src/sag/tools/context_tool.py`
  - Add conservative unresolved-failure detection for analyzer-generated build/test tasks.
- Create `tests/test_context_tool_completion_validation.py`
  - Unit-test completion validation without Docker or LLM calls.
- Modify `src/sag/agent/output_storage.py`
  - Replace `echo "{payload}"` container writes for both full output records and index writes.
- Create `tests/test_output_storage.py`
  - Unit-test backtick-containing output storage with a fake orchestrator.
- Modify `src/sag/main.py`
  - Return a non-zero exit status when `sag project` setup returns `False`.
- Create `tests/test_cli_project_exit_codes.py`
  - Unit-test failed project setup exit code with Click `CliRunner`.

Implementation commits must not include Co-Authorship/authorship trailers.

---

### Task 1: Default Docker Base Image

**Files:**
- Modify: `src/sag/config/settings.py:53-103`
- Modify: `.env.example`
- Modify: `.env`
- Test: `tests/test_config_settings.py`

- [ ] **Step 1: Add failing config tests**

Append these tests to `tests/test_config_settings.py`:

```python
def test_default_docker_base_image_is_ubuntu_2404():
    config = Config()

    assert config.docker_base_image == "ubuntu:24.04"


def test_from_env_uses_ubuntu_2404_docker_default_without_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SAG_DOCKER_BASE_IMAGE", raising=False)

    config = Config.from_env()

    assert config.docker_base_image == "ubuntu:24.04"


def test_from_env_allows_docker_base_image_override(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SAG_DOCKER_BASE_IMAGE", "ubuntu:26.04")

    config = Config.from_env()

    assert config.docker_base_image == "ubuntu:26.04"
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/test_config_settings.py -q
```

Expected: fails because defaults still return `ubuntu:22.04`.

- [ ] **Step 3: Update defaults**

In `src/sag/config/settings.py`, change both default values:

```python
docker_base_image: str = Field(default="ubuntu:24.04")
```

```python
docker_base_image=os.getenv("SAG_DOCKER_BASE_IMAGE", "ubuntu:24.04")
```

In `.env.example` and `.env`, change:

```text
SAG_DOCKER_BASE_IMAGE=ubuntu:24.04
```

Keep the `.env` edit local-only. Do not print or stage `.env` contents.

- [ ] **Step 4: Run tests and verify pass**

Run:

```bash
uv run pytest tests/test_config_settings.py -q
```

Expected: all tests in the file pass.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/sag/config/settings.py .env.example tests/test_config_settings.py
git commit -m "Set default Docker base to Ubuntu 24.04"
```

Do not add Co-Authorship/authorship trailers.
Do not stage `.env`.

---

### Task 2: Maven Version Observation Contract

**Files:**
- Modify: `src/sag/tools/maven_tool.py:158-188`
- Modify: `src/sag/tools/maven_tool.py:410-435`
- Modify: `src/sag/tools/maven_tool.py:1266-1584`
- Modify: `src/sag/agent/tool_orchestration.py:103-146`
- Test: `tests/test_maven_gradle_tool_contracts.py`
- Test: `tests/test_tool_orchestration_execution.py`

- [ ] **Step 1: Add failing Maven metadata test**

In `tests/test_maven_gradle_tool_contracts.py`, update `FakeToolchainManager` to accept optional version/source values:

```python
class FakeToolchainManager:
    def __init__(
        self,
        path="/tmp/apache-maven-3.9.6/bin/mvn",
        version="3.9.6",
        source="registered",
    ):
        self.path = path
        self.version = version
        self.source = source
        self.seen_spec = None
        self.seen_working_directory = None

    def resolve(self, spec, working_directory="/workspace"):
        self.seen_spec = spec
        self.seen_working_directory = working_directory
        return ResolvedToolExecutable(
            candidate=ToolExecutableCandidate(
                name=spec.name,
                executable=spec.executable,
                path=self.path,
                version=self.version,
                source=self.source,
            ),
            reason="test resolver",
        )
```

Then add:

```python
def test_maven_failed_result_metadata_includes_runtime_facts_for_version_error():
    orchestrator = FakeBuildToolOrchestrator(
        {
            "output": (
                "[ERROR] BUILD FAILURE\n"
                "Detected Maven Version: 3.6.3 is not in the allowed range [3.9,)."
            ),
            "exit_code": 1,
        }
    )
    tool = MavenTool(
        orchestrator,
        toolchain_manager=FakeToolchainManager(
            path="/usr/bin/mvn",
            version="3.6.3",
            source="system",
        ),
    )
    tool._record_test_summary = lambda *args, **kwargs: None

    result = tool.execute(command="compile", working_directory="/workspace/project")

    assert result.success is False
    assert result.metadata["maven_version_requirement"]["raw"] == "[3.9,)"
    assert result.metadata["maven_runtime"] == {
        "executable": "/usr/bin/mvn",
        "version": "3.6.3",
        "source": "system",
    }
```

- [ ] **Step 2: Add failing observation-format test**

In `tests/test_tool_orchestration_execution.py`, import `format_tool_result`:

```python
from sag.agent.tool_orchestration import ToolCall, ToolOrchestrator, format_tool_result
```

Add:

```python
def test_format_tool_result_surfaces_maven_version_contract():
    result = ToolResult(
        success=False,
        output="[ERROR] Detected Maven Version: 3.6.3 is not in the allowed range [3.9,).",
        error="Maven build failed",
        error_code="MAVEN_BUILD_FAILED",
        metadata={
            "maven_version_requirement": {
                "raw": "[3.9,)",
                "source": "build_error",
                "kind": "range",
            },
            "maven_runtime": {
                "executable": "/usr/bin/mvn",
                "version": "3.6.3",
                "source": "system",
            },
            "compatible_maven_candidate": None,
        },
    )

    formatted = format_tool_result("maven", result)

    assert "Maven version requirement: [3.9,) (source: build_error)" in formatted
    assert "Current Maven executable: /usr/bin/mvn" in formatted
    assert "Current Maven version: 3.6.3" in formatted
    assert "Compatible Maven candidate: none" in formatted
    assert 'retry maven(..., maven_version_requirement="[3.9,)")' in formatted
```

- [ ] **Step 3: Run tests and verify failure**

Run:

```bash
uv run pytest tests/test_maven_gradle_tool_contracts.py::test_maven_failed_result_metadata_includes_runtime_facts_for_version_error tests/test_tool_orchestration_execution.py::test_format_tool_result_surfaces_maven_version_contract -q
```

Expected: both tests fail because runtime facts and formatted observation text do not exist yet.

- [ ] **Step 4: Add Maven runtime metadata**

In `src/sag/tools/maven_tool.py`, add a small helper near `_resolve_maven_executable`:

```python
    def _maven_runtime_metadata(self, resolved_maven, maven_executable: str) -> Dict[str, Any]:
        if resolved_maven:
            candidate = resolved_maven.candidate
            return {
                "executable": candidate.path,
                "version": candidate.version,
                "source": candidate.source,
            }
        return {
            "executable": maven_executable,
            "version": None,
            "source": "wrapper" if maven_executable == "./mvnw" else "path",
        }
```

After `maven_executable` is computed in `execute()`, compute:

```python
maven_runtime = self._maven_runtime_metadata(resolved_maven, maven_executable)
```

Update `_handle_maven_error(...)` to accept an optional `maven_runtime` parameter:

```python
    def _handle_maven_error(
        self,
        output: str,
        exit_code: int,
        command: str,
        analysis: Dict[str, Any],
        maven_runtime: Dict[str, Any] | None = None,
    ) -> ToolResult:
```

Pass `maven_runtime` at each call site in `execute()`:

```python
return self._handle_maven_error(
    result["output"], result["exit_code"], maven_cmd, analysis, maven_runtime
)
```

Inside `_handle_maven_error`, add:

```python
if maven_runtime:
    metadata["maven_runtime"] = maven_runtime
if analysis.get("maven_version_requirement"):
    metadata["compatible_maven_candidate"] = None
```

Do not add installer behavior.

- [ ] **Step 5: Render Maven metadata in observations**

In `src/sag/agent/tool_orchestration.py`, add a private helper near `format_tool_result()`:

```python
def _format_maven_version_contract(result: ToolResult) -> str:
    metadata = result.metadata or {}
    requirement = metadata.get("maven_version_requirement")
    if not requirement:
        return ""

    lines = [
        "",
        "Maven version contract:",
        f"Maven version requirement: {requirement.get('raw')} (source: {requirement.get('source', 'unknown')})",
    ]

    runtime = metadata.get("maven_runtime") or {}
    if runtime.get("executable"):
        lines.append(f"Current Maven executable: {runtime['executable']}")
    if runtime.get("version"):
        lines.append(f"Current Maven version: {runtime['version']}")

    if metadata.get("compatible_maven_candidate") is None:
        lines.append("Compatible Maven candidate: none")

    raw_requirement = requirement.get("raw")
    if raw_requirement:
        lines.append(
            "Next action: provide or register a Maven executable that satisfies "
            f'{raw_requirement}, then retry maven(..., maven_version_requirement="{raw_requirement}")'
        )

    return "\n".join(lines)
```

Call the helper in the failed-result branch after output and before suggestions:

```python
if tool_name == "maven":
    formatted += _format_maven_version_contract(result)
```

- [ ] **Step 6: Run tests and verify pass**

Run:

```bash
uv run pytest tests/test_maven_gradle_tool_contracts.py tests/test_tool_orchestration_execution.py -q
```

Expected: both files pass.

- [ ] **Step 7: Commit**

Run:

```bash
git add src/sag/tools/maven_tool.py src/sag/agent/tool_orchestration.py tests/test_maven_gradle_tool_contracts.py tests/test_tool_orchestration_execution.py
git commit -m "Surface Maven version requirements in observations"
```

Do not add Co-Authorship/authorship trailers.

---

### Task 3: Build/Test Completion Gate

**Files:**
- Modify: `src/sag/tools/context_tool.py:1-9`
- Modify: `src/sag/tools/context_tool.py:1150-1367`
- Create: `tests/test_context_tool_completion_validation.py`

- [ ] **Step 1: Add failing validation tests**

Create `tests/test_context_tool_completion_validation.py`:

```python
from types import SimpleNamespace

from sag.tools.context_tool import ContextTool


def _tool():
    return ContextTool(SimpleNamespace())


def _task(description):
    return SimpleNamespace(id="task_4", description=description)


def test_compile_task_rejects_blocked_maven_completion():
    result = _tool()._validate_task_completion(
        _task("Compile project using Maven"),
        summary="Maven compile is blocked by the installed Maven version.",
        key_results=(
            "Build failed at maven-enforcer-plugin: RequireMavenVersion; "
            "Detected Maven Version 3.6.3 is not in the allowed range [3.9,). "
            "No compilation artifacts produced."
        ),
    )

    assert result["valid"] is False
    assert "failure" in result["reason"].lower() or "blocked" in result["reason"].lower()


def test_compile_task_allows_resolved_error_language():
    result = _tool()._validate_task_completion(
        _task("Compile project using Maven"),
        summary="Build completed successfully after the Maven version error was fixed.",
        key_results="BUILD SUCCESS; compilation successful; no errors remain.",
    )

    assert result["valid"] is True
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run pytest tests/test_context_tool_completion_validation.py -q
```

Expected: first test fails because `Compile project using Maven` currently passes validation even when blocked.

- [ ] **Step 3: Implement conservative failure detection**

In `src/sag/tools/context_tool.py`, add helpers near `_validate_task_completion()`:

```python
import re

    def _is_build_or_test_task_description(self, task_description: str) -> bool:
        build_terms = ["compile", "build", "package", "install"]
        test_terms = ["test", "tests", "verify"]
        tool_terms = ["maven", "gradle", "npm", "yarn", "pnpm", "pytest"]
        return (
            any(term in task_description for term in build_terms + test_terms)
            and any(term in task_description for term in tool_terms)
        )

    def _has_unresolved_failure_signal(self, text: str) -> bool:
        resolved_phrases = [
            "no errors",
            "no error",
            "error resolved",
            "errors resolved",
            "fixed the error",
            "error was fixed",
            "failure resolved",
        ]
        normalized = " ".join(text.lower().split())
        for phrase in resolved_phrases:
            normalized = normalized.replace(phrase, "")

        failure_signals = [
            "blocked",
            "failed",
            "failure",
            "no artifacts",
            "not in the allowed range",
            "cannot compile",
        ]
        if any(signal in normalized for signal in failure_signals):
            return True
        return re.search(r"(^|\W)errors?(:|\W|$)", normalized) is not None
```

At the beginning of `_validate_task_completion()` after lowercasing fields and
after `validation_result = {"valid": True, "reason": "", "suggestions": []}`,
add:

```python
combined_results = f"{summary_lower}\n{key_results_lower}"
if self._is_build_or_test_task_description(task_description) and self._has_unresolved_failure_signal(combined_results):
    validation_result.update(
        {
            "valid": False,
            "reason": "Build/test task indicates unresolved failure and cannot be marked completed",
            "suggestions": [
                "Resolve the build/test blocker before completing this task",
                "Only complete build/test tasks after successful execution evidence is available",
                "Use force=True only when intentionally recording a manually verified exception",
            ],
        }
    )
    return validation_result
```

- [ ] **Step 4: Run tests and verify pass**

Run:

```bash
uv run pytest tests/test_context_tool_completion_validation.py -q
```

Expected: both tests pass.

- [ ] **Step 5: Run related context tests**

Run:

```bash
uv run pytest tests/test_result_state_contracts.py tests/test_context_tool_completion_validation.py -q
```

Expected: both files pass.

- [ ] **Step 6: Commit**

Run:

```bash
git add src/sag/tools/context_tool.py tests/test_context_tool_completion_validation.py
git commit -m "Reject blocked build task completion"
```

Do not add Co-Authorship/authorship trailers.

---

### Task 4: Safe Output Storage Container Writes

**Files:**
- Modify: `src/sag/agent/output_storage.py:117-183`
- Test: `tests/test_output_storage.py`

- [ ] **Step 1: Add failing output storage test**

Create `tests/test_output_storage.py`:

```python
from pathlib import Path

from sag.agent.output_storage import OutputStorageManager


class FakeOutputStorageOrchestrator:
    def __init__(self):
        self.commands = []
        self.files = {}

    def execute_command(self, command):
        self.commands.append(command)

        if command.startswith("mkdir -p "):
            return {"success": True, "output": "", "exit_code": 0}

        if command.startswith("test -f ") and "output_index.json" in command:
            return {"success": False, "output": "", "exit_code": 1}

        if "wc -l <" in command:
            output = self.files.get("/workspace/.setup_agent/contexts/full_outputs.jsonl", "")
            return {"success": True, "output": str(len(output.splitlines())), "exit_code": 0}

        if command.startswith("cat >> ") or command.startswith("cat > "):
            operator = ">>" if command.startswith("cat >> ") else ">"
            path = command.split()[2]
            payload = command.split("\n", 1)[1].rsplit("\n", 1)[0]
            if operator == ">>":
                self.files[path] = self.files.get(path, "") + payload + "\n"
            else:
                self.files[path] = payload + "\n"
            return {"success": True, "output": "", "exit_code": 0}

        return {"success": True, "output": "", "exit_code": 0}


def test_output_storage_uses_safe_container_writes_for_backticks(tmp_path):
    orchestrator = FakeOutputStorageOrchestrator()
    storage = OutputStorageManager(Path("/workspace/.setup_agent/contexts"), orchestrator)

    ref_id = storage.store_output(
        task_id="task_1",
        tool_name="project_analyzer",
        output="Run tests using documented commands: mvn` without arguments",
    )

    assert ref_id
    assert all('echo "' not in command for command in orchestrator.commands)
    assert "/workspace/.setup_agent/contexts/full_outputs.jsonl" in orchestrator.files
    assert "/workspace/.setup_agent/contexts/output_index.json" in orchestrator.files
    assert "mvn` without arguments" in orchestrator.files["/workspace/.setup_agent/contexts/full_outputs.jsonl"]
    assert "mvn` without arguments" in orchestrator.files["/workspace/.setup_agent/contexts/output_index.json"]
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
uv run pytest tests/test_output_storage.py -q
```

Expected: fails because container writes still use `echo "{payload}"`.

- [ ] **Step 3: Implement safe container write helper**

In `src/sag/agent/output_storage.py`, add:

```python
    def _heredoc_delimiter(self, content: str) -> str:
        digest = hashlib.md5(content.encode()).hexdigest()[:12]
        delimiter = f"SAG_OUTPUT_EOF_{digest}"
        while f"\n{delimiter}\n" in f"\n{content}\n":
            digest = hashlib.md5(f"{content}{delimiter}".encode()).hexdigest()[:12]
            delimiter = f"SAG_OUTPUT_EOF_{digest}"
        return delimiter

    def _write_container_text(self, path: str, content: str, *, append: bool = False) -> bool:
        delimiter = self._heredoc_delimiter(content)
        operator = ">>" if append else ">"
        command = f"cat {operator} {path} <<'{delimiter}'\n{content}\n{delimiter}"
        result = self.orchestrator.execute_command(command)
        if result.get("exit_code") == 0 or result.get("success"):
            return True
        logger.error(f"Failed to write container file {path}: {result.get('output')}")
        return False
```

Use it in `store_output()`:

```python
json_line = json.dumps(record)
if not self._write_container_text(self.container_storage_file, json_line, append=True):
    return ""
```

Use it in `_save_index()`:

```python
index_json = json.dumps(self.current_index, indent=2)
if not self._write_container_text(self.container_index_file, index_json, append=False):
    logger.error("Failed to save index to container")
```

Remove the old `.replace('"', '\\"').replace("$", "\\$")` `echo` commands.

- [ ] **Step 4: Run tests and verify pass**

Run:

```bash
uv run pytest tests/test_output_storage.py -q
```

Expected: test passes.

- [ ] **Step 5: Run output-search related smoke tests**

Run:

```bash
uv run pytest tests/test_import_smoke.py tests/test_output_storage.py -q
```

Expected: tests pass.

- [ ] **Step 6: Commit**

Run:

```bash
git add src/sag/agent/output_storage.py tests/test_output_storage.py
git commit -m "Store output records without shell echo"
```

Do not add Co-Authorship/authorship trailers.

---

### Task 5: CLI Setup Failure Exit Code

**Files:**
- Modify: `src/sag/main.py:390-416`
- Create: `tests/test_cli_project_exit_codes.py`

- [ ] **Step 1: Add failing CLI test**

Create `tests/test_cli_project_exit_codes.py`:

```python
from click.testing import CliRunner

import sag.main as main_module


class FakeProjectOrchestrator:
    def __init__(self, project_name=None):
        self.project_name = project_name

    def container_exists(self):
        return False


class FailingSetupAgent:
    def __init__(self, config, orchestrator):
        self.config = config
        self.orchestrator = orchestrator

    def setup_project(self, **kwargs):
        return False


def test_project_command_returns_nonzero_when_setup_fails(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_module, "DockerOrchestrator", FakeProjectOrchestrator)
    monkeypatch.setattr(main_module, "SetupAgent", FailingSetupAgent)

    result = CliRunner().invoke(
        main_module.cli,
        ["project", "https://github.com/apache/commons-cli.git"],
    )

    assert result.exit_code == 1
    assert "Project setup failed" in result.output


def test_project_command_returns_nonzero_when_ui_setup_fails(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_module, "DockerOrchestrator", FakeProjectOrchestrator)
    monkeypatch.setattr(main_module, "SetupAgent", FailingSetupAgent)

    result = CliRunner().invoke(
        main_module.cli,
        ["project", "https://github.com/apache/commons-cli.git", "--ui"],
    )

    assert result.exit_code == 1
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
uv run pytest tests/test_cli_project_exit_codes.py -q
```

Expected: fails because the command currently prints failure guidance but exits `0`.

- [ ] **Step 3: Implement non-zero exit**

In `src/sag/main.py`, keep the existing non-UI failure guidance, then add the
exit outside the `if not config.ui_mode:` block so both UI and non-UI failures
return non-zero:

```python
        if not success:
            sys.exit(1)
```

Keep the existing exception handler unchanged. Do not change `sag run --task`.

- [ ] **Step 4: Run test and verify pass**

Run:

```bash
uv run pytest tests/test_cli_project_exit_codes.py -q
```

Expected: test passes.

- [ ] **Step 5: Run CLI-related tests**

Run:

```bash
uv run pytest tests/test_cli_project_exit_codes.py tests/test_result_state_contracts.py tests/test_import_smoke.py -q
```

Expected: tests pass.

- [ ] **Step 6: Commit**

Run:

```bash
git add src/sag/main.py tests/test_cli_project_exit_codes.py
git commit -m "Return nonzero status for failed setup"
```

Do not add Co-Authorship/authorship trailers.

---

### Task 6: Full Verification

**Files:**
- No new files unless earlier tasks reveal a test-only adjustment is needed.

- [ ] **Step 1: Run focused contract suite**

Run:

```bash
uv run pytest tests/test_config_settings.py tests/test_maven_gradle_tool_contracts.py tests/test_tool_orchestration_execution.py tests/test_context_tool_completion_validation.py tests/test_output_storage.py tests/test_cli_project_exit_codes.py -q
```

Expected: all focused tests pass.

- [ ] **Step 2: Run full test suite**

Run:

```bash
uv run pytest -q
```

Expected: all tests pass.

- [ ] **Step 3: Check worktree**

Run:

```bash
git status --short
```

Expected: clean worktree.

- [ ] **Step 4: Optional commons-cli smoke**

Run only if Docker is available and the user wants an integration smoke:

```bash
uv run sag remove sag-commons-cli --force
uv run sag project https://github.com/apache/commons-cli.git --name commons-cli
```

Expected: the default base image is `ubuntu:24.04`; if commons-cli still requires Maven `[3.9,)`, the agent sees a clear Maven version contract and cannot mark the compile task complete while blocked.

Do not treat the integration smoke as required for this unit-test implementation phase.
