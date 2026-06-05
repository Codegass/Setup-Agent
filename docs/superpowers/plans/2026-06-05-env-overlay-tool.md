# Env Overlay Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an agent-maintained environment overlay so installed runtimes and tool paths can be registered once, activated consistently across shell/build tools, and blocked when a concrete executable is proven incompatible.

**Architecture:** Keep the overlay as a shallow runtime module, not as logic embedded into every tool. The source of truth is `/workspace/.setup_agent/env_overlay.json`; a derived shell script at `/workspace/.setup_agent/env_overlay.sh` is generated whenever the overlay changes. `DockerOrchestrator` sources the shell script before command execution, while build tools and `ToolchainManager` read the JSON through a small store API.

**Tech Stack:** Python stdlib, existing `BaseTool`, existing `DockerOrchestrator`, existing toolchain manager tests with fake orchestrators, pytest.

---

## Design Constraints

- The env tool does not download, install, or update software. Agents install with `bash`, then register the result with `env`.
- The env tool must not edit project configuration such as `pom.xml`, `.mvn/`, `build.gradle`, `gradle.properties`, `.sdkmanrc`, `package.json`, or `pyproject.toml`.
- Overlay state lives under `/workspace/.setup_agent/`, which is agent/runtime state rather than repository source.
- Tool blockers are scoped to exact executable/version evidence. Blocking `/usr/bin/mvn` does not ban Maven as a family.
- Direct tool parameters remain highest priority. Env overlay only participates when the tool is otherwise resolving a runtime.
- Runtime injection should be generic: tools that execute through `DockerOrchestrator` inherit the active overlay without each tool reimplementing `PATH` logic.

## Step 1: Add failing tests for the overlay store

Create `tests/test_env_overlay.py`.

Test cases:

- `test_register_activate_writes_json_and_shell_script`
  - Use a fake orchestrator with an in-memory file map.
  - Register Maven at `/opt/apache-maven-3.9.9/bin/mvn` with version `3.9.9`.
  - Activate Maven.
  - Assert `/workspace/.setup_agent/env_overlay.json` contains `tools.maven.active`.
  - Assert `/workspace/.setup_agent/env_overlay.sh` exports a `PATH` prefix containing `/opt/apache-maven-3.9.9/bin`.

- `test_block_records_exact_executable_without_blocking_other_versions`
  - Register `/usr/bin/mvn` version `3.6.3`.
  - Block `/usr/bin/mvn` with requirement `[3.9,)`.
  - Register `/opt/apache-maven-3.9.9/bin/mvn` version `3.9.9`.
  - Assert `is_blocked("maven", "/usr/bin/mvn", "3.6.3", "[3.9,)")` is true.
  - Assert `is_blocked("maven", "/opt/apache-maven-3.9.9/bin/mvn", "3.9.9", "[3.9,)")` is false.

- `test_invalid_overlay_json_recovers_to_empty_state`
  - Put invalid JSON in the fake file map.
  - Assert `inspect()` returns an inactive empty overlay plus a warning string.
  - Assert `register()` replaces the invalid JSON with a valid overlay.

Implementation sketch:

```python
class FakeOverlayOrchestrator:
    def __init__(self):
        self.files = {}
        self.commands = []

    def execute_command(self, command, timeout=30):
        self.commands.append(command)
        if command.startswith("test -f "):
            path = command.split(" ", 2)[-1]
            return {"success": path in self.files, "exit_code": 0 if path in self.files else 1, "stdout": "", "stderr": ""}
        if command.startswith("cat "):
            path = command.split(" ", 1)[1]
            return {"success": path in self.files, "exit_code": 0 if path in self.files else 1, "stdout": self.files.get(path, ""), "stderr": ""}
        raise AssertionError(command)

    def write_file(self, path, content):
        self.files[path] = content
        return {"success": True, "exit_code": 0, "stdout": "", "stderr": ""}
```

Expected status before implementation:

```bash
uv run pytest tests/test_env_overlay.py -q
```

The command fails because `src/sag/runtime/env_overlay.py` does not exist.

## Step 2: Implement `EnvOverlayStore`

Add `src/sag/runtime/__init__.py`.

Add `src/sag/runtime/env_overlay.py` with:

- `DEFAULT_OVERLAY_JSON = "/workspace/.setup_agent/env_overlay.json"`
- `DEFAULT_OVERLAY_SCRIPT = "/workspace/.setup_agent/env_overlay.sh"`
- `EnvOverlayStore`
- `EnvOverlayWarning`

Public API:

```python
class EnvOverlayStore:
    def __init__(
        self,
        orchestrator,
        overlay_json_path: str = DEFAULT_OVERLAY_JSON,
        overlay_script_path: str = DEFAULT_OVERLAY_SCRIPT,
    ) -> None:
        ...

    def inspect(self) -> dict[str, Any]:
        ...

    def register(
        self,
        tool: str,
        executable: str,
        *,
        version: str | None = None,
        source: str = "agent_registered",
        env: dict[str, str] | None = None,
        path_prepend: list[str] | None = None,
        activate: bool = False,
    ) -> dict[str, Any]:
        ...

    def activate(self, tool: str, executable: str) -> dict[str, Any]:
        ...

    def block(
        self,
        tool: str,
        executable: str,
        *,
        version: str | None = None,
        requirement: str | None = None,
        reason: str | None = None,
        source: str = "build_error",
    ) -> dict[str, Any]:
        ...

    def clear(self, tool: str | None = None) -> dict[str, Any]:
        ...

    def active_candidate(self, tool: str) -> dict[str, Any] | None:
        ...

    def is_blocked(
        self,
        tool: str,
        executable: str,
        version: str | None = None,
        requirement: str | None = None,
    ) -> bool:
        ...
```

Schema:

```json
{
  "version": 1,
  "tools": {
    "maven": {
      "active": "/opt/apache-maven-3.9.9/bin/mvn",
      "candidates": {
        "/opt/apache-maven-3.9.9/bin/mvn": {
          "version": "3.9.9",
          "source": "agent_registered",
          "env": {"MAVEN_HOME": "/opt/apache-maven-3.9.9"},
          "path_prepend": ["/opt/apache-maven-3.9.9/bin"]
        }
      },
      "blocked": [
        {
          "executable": "/usr/bin/mvn",
          "version": "3.6.3",
          "requirement": "[3.9,)",
          "reason": "Project requires Maven 3.9+",
          "source": "build_error"
        }
      ]
    }
  }
}
```

Shell script generation rules:

- Only active candidates contribute shell exports.
- Add all active `env` key/value pairs.
- Add all active `path_prepend` entries to `PATH`.
- Quote values with `shlex.quote`.
- Write an empty but valid script when no tools are active:

```sh
# Generated by Setup-Agent env overlay.
```

Store write path:

- Ensure `/workspace/.setup_agent` exists with `mkdir -p`.
- Write JSON and script with a safe quoted here-doc command through `orchestrator.execute_command`.
- In tests, allow the fake orchestrator to expose `write_file`; in production, use the shell write command.

Run:

```bash
uv run pytest tests/test_env_overlay.py -q
```

## Step 3: Add the agent-facing env tool

Create `src/sag/tools/env_tool.py`.

Tool contract:

```python
class EnvTool(BaseTool):
    name = "env"
    description = "Manage runtime environment overlay entries for tool paths and environment variables."
```

Supported actions:

- `inspect`: returns active tools, registered candidates, blockers, warning metadata.
- `register`: records an executable and optional version/env/path entries.
- `activate`: marks a registered executable active.
- `block`: records negative evidence about an executable/version.
- `clear`: clears one tool or the whole overlay.

Agent guidance in the tool description:

- Use `bash` to download/install runtimes.
- Use `env register` after installation.
- Use `env activate` before retrying a build with that runtime.
- Use `env block` when a specific executable/version is proven incompatible.
- Do not use `env` to edit project build files.

Register the tool:

- Add `EnvTool` export in `src/sag/tools/__init__.py`.
- Add `self._register_tool(EnvTool(...))` in `src/sag/agent/agent.py` near system/build tools.
- Include the env tool in any CLI/tool listing tests if those tests assert the complete tool set.

Tests:

- Add `test_env_tool_register_activate_inspect` to `tests/test_env_overlay.py`.
- Instantiate `EnvTool` with the fake orchestrator.
- Call `execute({"action": "register", ...})`, then `activate`, then `inspect`.
- Assert tool responses are structured and include the active Maven executable.

Run:

```bash
uv run pytest tests/test_env_overlay.py -q
```

## Step 4: Source the overlay script in Docker command execution

Modify `src/sag/docker_orch/orch.py`.

Add constants:

```python
ENV_OVERLAY_SCRIPT = "/workspace/.setup_agent/env_overlay.sh"
```

Add helper:

```python
def _runtime_profile_prefix(self) -> str:
    return (
        f"source {ENV_OVERLAY_SCRIPT} 2>/dev/null || true; "
        "source /etc/profile 2>/dev/null || true; "
        "source ~/.bashrc 2>/dev/null || true"
    )
```

Use it in both command paths:

- `execute_command`
- `execute_command_with_monitoring`

Keep the existing working-directory behavior:

```python
full_cmd = f"{self._runtime_profile_prefix()}; cd {self.work_dir} && {command}"
```

Add tests in `tests/test_docker_orchestrator_command_wrapping.py` or the closest existing orchestrator test file:

- Construct an orchestrator-like object with `work_dir="/workspace"`.
- Assert command wrapping includes `/workspace/.setup_agent/env_overlay.sh`.
- Assert both regular and monitored execution share the same prefix helper.

Run:

```bash
uv run pytest tests/test_docker_orchestrator_command_wrapping.py -q
```

If there is no existing lightweight orchestrator wrapper test file, keep the tests focused on the helper method to avoid Docker.

## Step 5: Integrate overlay candidates into `ToolchainManager`

Modify `src/sag/tools/toolchain_manager.py`.

Changes:

- Add `env_overlay` to `CandidateSource`.
- Instantiate `EnvOverlayStore` when an orchestrator is available.
- During discovery, insert the active overlay candidate for the requested tool before wrapper/registered/PATH candidates.
- Apply blockers before ranking.
- Keep explicit executable parameters outside manager priority, since tools already pass explicit paths directly.

Source priority:

```python
SOURCE_PRIORITY = {
    "env_overlay": 0,
    "wrapper": 1,
    "registered": 2,
    "path": 3,
    "standalone": 4,
    "system": 5,
}
```

Blocker behavior:

- If a candidate executable exactly matches a blocker and its version or requirement evidence overlaps, exclude that candidate.
- Do not exclude other executables for the same tool.
- Include blocked candidates in debug metadata, not in the selected candidate list.

Tests in `tests/test_toolchain_manager.py`:

- `test_env_overlay_candidate_wins_over_system_path`
  - Fake overlay active Maven 3.9.9.
  - PATH candidate Maven 3.6.3.
  - Requirement `[3.9,)`.
  - Assert the overlay candidate is selected.

- `test_env_overlay_blocker_excludes_exact_path_only`
  - Block `/usr/bin/mvn` 3.6.3.
  - Register active `/opt/apache-maven-3.9.9/bin/mvn`.
  - Assert `/opt/.../mvn` is selected.

- `test_explicit_executable_still_wins_in_maven_tool`
  - Existing Maven test style: pass explicit executable in tool params.
  - Assert explicit path is used even when overlay has a different active Maven.

Run:

```bash
uv run pytest tests/test_toolchain_manager.py tests/test_maven_gradle_tool_contracts.py -q
```

## Step 6: Make Maven and Gradle env-aware through manager resolution

Maven:

- Keep existing `ToolchainManager` entry path.
- Ensure `maven_version_requirement` is passed to manager resolution.
- Add response metadata:
  - `resolved_source`
  - `resolved_executable`
  - `version_requirement`
  - `env_overlay_active` when source is `env_overlay`
  - `blocked_candidates` when present

Gradle:

- Add a narrow manager resolution path matching Maven’s shape.
- Respect explicit Gradle executable when provided.
- Otherwise resolve through `ToolchainManager` so active overlay Gradle can be used.
- Keep Gradle wrapper behavior visible; if both overlay and wrapper exist, use manager ranking from Step 5.

Tests in `tests/test_maven_gradle_tool_contracts.py`:

- `test_maven_uses_active_env_overlay_candidate`
- `test_maven_does_not_retry_blocked_system_maven`
- `test_gradle_uses_active_env_overlay_candidate`
- `test_gradle_explicit_executable_overrides_env_overlay`

Run:

```bash
uv run pytest tests/test_maven_gradle_tool_contracts.py -q
```

## Step 7: Register runtime installs from setup/system flows without taking over installation

Modify `src/sag/tools/project_setup_tool.py` and `src/sag/tools/system_tool.py`.

Behavior:

- After a successful install or verified runtime discovery, register that executable with `EnvOverlayStore`.
- Activate only when the install action was explicitly requested by the agent or when the runtime is already the selected toolchain.
- Do not remove existing profile/update-alternatives behavior in this pass.
- Do not edit project files.

Maven example after successful install:

```python
store.register(
    "maven",
    "/opt/apache-maven-3.9.9/bin/mvn",
    version="3.9.9",
    env={"MAVEN_HOME": "/opt/apache-maven-3.9.9"},
    path_prepend=["/opt/apache-maven-3.9.9/bin"],
    activate=True,
)
```

Java example:

```python
store.register(
    "java",
    "/usr/lib/jvm/java-21-openjdk-amd64/bin/java",
    version="21",
    env={"JAVA_HOME": "/usr/lib/jvm/java-21-openjdk-amd64"},
    path_prepend=["/usr/lib/jvm/java-21-openjdk-amd64/bin"],
    activate=True,
)
```

Tests:

- Add fake orchestrator tests around successful install result handlers rather than live package installation.
- Assert env overlay registration is attempted only after success.
- Assert failed install does not activate a runtime.

Run:

```bash
uv run pytest tests/test_project_setup_tool.py tests/test_system_tool.py -q
```

If those files do not currently exist, add focused tests with existing fake tool patterns.

## Step 8: Improve command monitoring failure semantics

Modify `src/sag/docker_orch/orch.py`.

Current risk:

- Monitored execution can report success when Docker returns `exit_code is None`.
- This lets a failed `mvn` command look successful to `bash`, which causes the agent to keep using the wrong runtime.

Change:

- Keep actual nonzero exit codes as failures.
- If exit code is unknown, infer failure only from explicit terminal failure indicators in stdout/stderr.
- Use a small shared helper so this behavior is visible and testable:

```python
BUILD_FAILURE_MARKERS = (
    "BUILD FAILURE",
    "BUILD FAILED",
    "Could not resolve",
    "Compilation failure",
    "not in the allowed range",
)

def _infer_unknown_exit_code(self, output: str) -> int:
    return 1 if any(marker in output for marker in BUILD_FAILURE_MARKERS) else 0
```

Tests:

- Add a monitored execution fake where Docker exit code is `None` and output includes `BUILD FAILURE`.
- Assert result `success` is false and `exit_code` is `1`.
- Add a monitored execution fake where exit code is `None` and output is ordinary command output.
- Assert result remains successful.

Run:

```bash
uv run pytest tests/test_docker_orchestrator_command_wrapping.py tests/test_bash_tool.py -q
```

## Step 9: Surface overlay evidence in reports and replay-adjacent tools

Modify `src/sag/tools/report_tool.py`.

Behavior:

- Add an optional “Runtime environment overlay” section when overlay state exists.
- Show active tool executables and blocked candidates.
- Do not treat overlay state as project source configuration.

Modify `src/sag/tools/command_tracker.py` only if replay currently reconstructs environment outside `DockerOrchestrator`.

- If replay calls `DockerOrchestrator`, no replay-specific env injection is needed.
- If replay bypasses `DockerOrchestrator`, source `/workspace/.setup_agent/env_overlay.sh` before replay commands.

Tests:

- Report test with fake overlay state asserts active Maven appears in report evidence.
- Replay test, only if needed, asserts command execution uses overlay script through the shared orchestrator prefix.

Run:

```bash
uv run pytest tests/test_report_tool.py tests/test_command_tracker.py -q
```

If exact test filenames differ, use `rg "ReportTool|CommandTracker" tests` and place tests next to the existing coverage.

## Step 10: Update prompts/config docs so agents know when to use env

Modify `src/sag/config/prompts.yaml`.

Add guidance near tool-use or recovery instructions:

```yaml
env_overlay_recovery:
  description: Runtime tools can be registered into the env overlay after installation.
  guidance: |
    Use bash to install missing runtimes. After installation, use env register and env activate so maven, gradle, bash, validation, and report flows share the same runtime path. If a concrete executable is proven incompatible, use env block for that exact executable/version before retrying with another runtime. Do not use env to rewrite project build configuration.
```

If `react_engine.py` references a recovery prompt that discusses Maven/Gradle fallback, replace that text with the YAML key through the existing prompt loader.

Tests:

- Existing prompt-loading tests should pass.
- Add a focused test if prompt keys are validated elsewhere.

Run:

```bash
uv run pytest tests -q
```

## Step 11: Run a fresh commons-cli harness validation

After unit tests pass, run a fresh end-to-end setup for `commons-cli`.

Preparation:

```bash
uv run sag list
```

Remove only the SAG-created Docker project/container for the commons-cli test target. Do not delete unrelated user containers.

Run the setup with current default config:

```bash
uv run sag project https://github.com/apache/commons-cli.git
```

During the run, watch for:

- Agent installs a compatible Maven with `bash` when project requirements demand it.
- Agent calls `env register` and `env activate` after installation.
- `maven` uses the active overlay executable on retry.
- Failed Maven candidates are recorded as blocked exact paths.
- `bash` does not convert monitored Maven failure into success.
- Final report shows the active runtime overlay evidence.

If Docker/network is unavailable, record the exact blocker and still run all unit tests.

## Step 12: Final verification

Run:

```bash
uv run pytest tests/test_env_overlay.py tests/test_toolchain_manager.py tests/test_maven_gradle_tool_contracts.py -q
uv run pytest tests -q
uv run ruff check src tests
```

Manual checks:

```bash
git status --short
```

Expected:

- Unit tests pass.
- No new authorship trailer is present.
- Only intended files changed.

## Implementation Order

- [ ] Step 1: Add failing overlay store tests.
- [ ] Step 2: Implement `EnvOverlayStore`.
- [ ] Step 3: Add and register `EnvTool`.
- [ ] Step 4: Source env overlay script in both orchestrator command paths.
- [ ] Step 5: Integrate overlay candidates and blockers into `ToolchainManager`.
- [ ] Step 6: Make Maven and Gradle consume overlay-aware manager resolution.
- [ ] Step 7: Register successful setup/system runtime installs.
- [ ] Step 8: Fix monitored command unknown-exit failure semantics.
- [ ] Step 9: Surface overlay evidence in reports and replay where needed.
- [ ] Step 10: Update prompt YAML guidance.
- [ ] Step 11: Run fresh commons-cli harness validation.
- [ ] Step 12: Run final verification.
