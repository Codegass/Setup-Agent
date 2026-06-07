# Toolchain Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a generic ToolchainManager and wire Maven through it so SAG resolves the correct executable from explicit version constraints, project/build evidence, persisted state, and PATH.

**Architecture:** `ToolchainManager` becomes the deep module for executable discovery, requirement matching, candidate registration, and PATH persistence. `MavenTool` remains responsible for Maven-specific evidence parsing and command execution, but it asks the manager for a resolved Maven executable instead of hardcoding `mvn`. `ToolParameterNormalizer` keeps bash command rewriting conservative so Maven-only flags are applied only to actual Maven commands.

**Tech Stack:** Python, pytest, Docker command orchestration fakes, existing SAG tool contract tests.

---

## File Structure

- Create: `src/sag/tools/toolchain_manager.py`
  - Dataclasses: `ToolVersionRequirement`, `ToolchainSpec`, `ToolExecutableCandidate`, `ResolvedToolExecutable`.
  - Version parsing and constraint matching.
  - Candidate discovery, registration persistence, resolution, and PATH persistence.
- Modify: `src/sag/tools/maven_tool.py`
  - Add `maven_version_requirement: str | None = None` public parameter and schema entry.
  - Use `ToolchainManager.resolve()` before building Maven commands.
  - Parse Maven Enforcer output into a structured requirement for follow-up runs.
- Modify: `src/sag/agent/tool_parameters.py`
  - Replace broad substring `mvn` rewriting with conservative Maven-command detection.
- Test: `tests/test_toolchain_manager.py`
  - Manager unit tests for exact, range, minimum, persistence, and no-newest-by-default behavior.
- Test: `tests/test_maven_gradle_tool_contracts.py`
  - MavenTool integration tests for resolved executable usage and schema exposure.
- Test: `tests/test_tool_orchestration_parameters.py`
  - Bash normalization tests for no `--fail-at-end` injection on non-Maven commands.

---

### Task 1: ToolchainManager Contracts

**Files:**
- Create: `tests/test_toolchain_manager.py`
- Create: `src/sag/tools/toolchain_manager.py`

- [ ] **Step 1: Write failing tests for version requirement matching**

```python
def test_resolve_exact_requirement_does_not_upgrade_to_newer_version():
    manager = ToolchainManager(FakeOrchestrator({
        "/tmp/apache-maven-3.8.8/bin/mvn": "Apache Maven 3.8.8",
        "/tmp/apache-maven-3.9.6/bin/mvn": "Apache Maven 3.9.6",
    }))

    resolved = manager.resolve(
        ToolchainSpec(
            name="maven",
            executable="mvn",
            version_requirement=ToolVersionRequirement(
                raw="3.8.8",
                source="tool_parameter",
                kind="exact",
            ),
        )
    )

    assert resolved.candidate.version == "3.8.8"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_toolchain_manager.py -q`

Expected: FAIL because `sag.tools.toolchain_manager` does not exist.

- [ ] **Step 3: Implement minimal dataclasses and constraint matching**

Add dataclasses plus parsing helpers for exact, range, minimum, maximum, and preferred requirements.

- [ ] **Step 4: Implement discovery against fake orchestrator**

Use orchestrator commands:

```text
test -x <path>
<path> -version
find /tmp /opt /usr/local -path '*/apache-maven-*/bin/mvn' -type f
command -v mvn
```

- [ ] **Step 5: Run tests to verify pass**

Run: `uv run pytest tests/test_toolchain_manager.py -q`

Expected: PASS.

---

### Task 2: Toolchain Registry Persistence

**Files:**
- Modify: `tests/test_toolchain_manager.py`
- Modify: `src/sag/tools/toolchain_manager.py`

- [ ] **Step 1: Write failing test for register/load**

```python
def test_registered_candidate_persists_and_is_loaded_for_resolution():
    orchestrator = FakeOrchestrator({})
    manager = ToolchainManager(orchestrator)
    manager.register(ToolExecutableCandidate(
        name="maven",
        executable="mvn",
        path="/opt/apache-maven-3.9.6/bin/mvn",
        version="3.9.6",
        source="registered",
    ))

    reloaded = ToolchainManager(orchestrator)
    resolved = reloaded.resolve(ToolchainSpec(name="maven", executable="mvn"))

    assert resolved.candidate.path == "/opt/apache-maven-3.9.6/bin/mvn"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_toolchain_manager.py::test_registered_candidate_persists_and_is_loaded_for_resolution -q`

Expected: FAIL because registry persistence is not implemented.

- [ ] **Step 3: Implement registry read/write**

Persist to `/workspace/.setup_agent/toolchains.json`. Use JSON object keyed by tool name and executable. Do not store secrets.

- [ ] **Step 4: Run toolchain manager tests**

Run: `uv run pytest tests/test_toolchain_manager.py -q`

Expected: PASS.

---

### Task 3: MavenTool Uses Resolved Executable

**Files:**
- Modify: `tests/test_maven_gradle_tool_contracts.py`
- Modify: `src/sag/tools/maven_tool.py`

- [ ] **Step 1: Write failing MavenTool executable test**

```python
def test_maven_tool_uses_resolved_toolchain_executable():
    orchestrator = FakeBuildToolOrchestrator()
    toolchain_manager = FakeToolchainManager("/tmp/apache-maven-3.9.6/bin/mvn")
    tool = MavenTool(orchestrator, toolchain_manager=toolchain_manager)

    result = tool.execute(command="compile", working_directory="/workspace/project")

    assert result.success is True
    assert orchestrator.monitored_commands[0][0].startswith("/tmp/apache-maven-3.9.6/bin/mvn ")
```

- [ ] **Step 2: Write failing schema test**

```python
def test_maven_tool_schema_exposes_maven_version_requirement():
    schema = MavenTool(FakeBuildToolOrchestrator()).get_parameter_schema()
    assert "maven_version_requirement" in schema["properties"]
```

- [ ] **Step 3: Run tests to verify fail**

Run: `uv run pytest tests/test_maven_gradle_tool_contracts.py -q`

Expected: FAIL because `MavenTool` does not accept `toolchain_manager` or `maven_version_requirement`.

- [ ] **Step 4: Add MavenTool parameter and manager injection**

Update `MavenTool.__init__()` to accept optional `toolchain_manager`. Update `execute()` and `_get_parameters_schema()`.

- [ ] **Step 5: Use resolved executable in `_build_maven_command()`**

Pass resolved executable into `_build_maven_command()` and use it instead of literal `mvn` when `use_wrapper` is false.

- [ ] **Step 6: Run Maven contract tests**

Run: `uv run pytest tests/test_maven_gradle_tool_contracts.py -q`

Expected: PASS.

---

### Task 4: Maven Version Requirement Evidence

**Files:**
- Modify: `tests/test_maven_gradle_tool_contracts.py`
- Modify: `src/sag/tools/maven_tool.py`

- [ ] **Step 1: Write failing test for explicit parameter precedence**

```python
def test_maven_tool_turns_explicit_version_parameter_into_requirement():
    toolchain_manager = RecordingToolchainManager()
    tool = MavenTool(FakeBuildToolOrchestrator(), toolchain_manager=toolchain_manager)

    tool.execute(
        command="compile",
        working_directory="/workspace/project",
        maven_version_requirement="[3.9,4.0)",
    )

    assert toolchain_manager.seen_spec.version_requirement.raw == "[3.9,4.0)"
    assert toolchain_manager.seen_spec.version_requirement.source == "tool_parameter"
```

- [ ] **Step 2: Write failing test for Enforcer output parsing**

```python
def test_maven_tool_extracts_version_requirement_from_enforcer_output():
    assert MavenTool.extract_version_requirement_from_output(
        "Detected Maven Version: 3.6.3 is not in the allowed range [3.9,)."
    ).raw == "[3.9,)"
```

- [ ] **Step 3: Run tests to verify fail**

Run: `uv run pytest tests/test_maven_gradle_tool_contracts.py -q`

Expected: FAIL until parsing and requirement forwarding are implemented.

- [ ] **Step 4: Implement requirement parsing and metadata**

Keep manager generic. MavenTool parses Enforcer messages and stores requirement metadata in failed results for the next call/context.

- [ ] **Step 5: Run Maven tests**

Run: `uv run pytest tests/test_maven_gradle_tool_contracts.py -q`

Expected: PASS.

---

### Task 5: Conservative Bash Maven Flag Rewriting

**Files:**
- Modify: `tests/test_tool_orchestration_parameters.py`
- Modify: `src/sag/agent/tool_parameters.py`

- [ ] **Step 1: Write failing tests for non-Maven commands**

```python
def test_bash_parameter_normalizer_does_not_append_fail_at_end_to_non_maven_commands():
    fixes = []
    normalizer = ToolParameterNormalizer(
        tools={"bash": BashLikeTool()},
        successful_states={"working_directory": "/workspace/project"},
        repository_url=None,
    )

    params = normalizer.validate_and_fix(
        "bash",
        {"command": "find /workspace -name '*.java' | tail -5"},
        fixes,
    )

    assert params["command"] == "find /workspace -name '*.java' | tail -5"
```

- [ ] **Step 2: Write passing-intent test for real Maven command**

```python
def test_bash_parameter_normalizer_appends_fail_at_end_to_simple_maven_command():
    fixes = []
    normalizer = ToolParameterNormalizer(
        tools={"bash": BashLikeTool()},
        successful_states={"working_directory": "/workspace/project"},
        repository_url=None,
    )

    params = normalizer.validate_and_fix("bash", {"command": "mvn test"}, fixes)

    assert params["command"] == "mvn test --fail-at-end"
```

- [ ] **Step 3: Run tests to verify fail**

Run: `uv run pytest tests/test_tool_orchestration_parameters.py -q`

Expected: FAIL on non-Maven command because current logic checks broad substring.

- [ ] **Step 4: Implement conservative token detection**

Split simple command segments on `&&` and `;`, parse each with `shlex.split()`, and rewrite only when first token is `mvn`, `./mvnw`, ends with `/mvn`, or ends with `/mvnw`. Skip complex segments with pipes or redirects.

- [ ] **Step 5: Run parameter tests**

Run: `uv run pytest tests/test_tool_orchestration_parameters.py -q`

Expected: PASS.

---

### Task 6: Verification

**Files:**
- No new files unless failures require targeted fixes.

- [ ] **Step 1: Run focused tests**

Run:

```bash
uv run pytest tests/test_toolchain_manager.py tests/test_maven_gradle_tool_contracts.py tests/test_tool_orchestration_parameters.py -q
```

Expected: PASS.

- [ ] **Step 2: Run full tests**

Run: `uv run pytest`

Expected: PASS.

- [ ] **Step 3: Run formatting checks**

Run:

```bash
uv run black --check src tests
uv run isort --check-only src tests
git diff --check
```

Expected: PASS.

- [ ] **Step 4: Optional CLI smoke**

If Docker is available, rerun the commons-cli setup flow enough to confirm MavenTool uses the resolved executable path instead of falling back to `/usr/bin/mvn`.

Run:

```bash
uv run sag run sag-commons-cli --task "compile commons-cli with the Maven version required by the project" --max-iterations 8 --record
```

Expected: logs show resolved Maven executable satisfies the requirement.
