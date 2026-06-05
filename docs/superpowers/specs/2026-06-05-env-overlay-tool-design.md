# Env Overlay Tool Design

Date: 2026-06-05
Status: Draft for spec review

## Goal

Add an agent-maintained runtime environment overlay for SAG container tools.
The overlay acts like a lightweight, project-local virtual environment for
tool paths and environment variables. It lets the agent download or install a
runtime with existing tools, then register and activate that runtime so all
subsequent tools use it consistently.

The immediate motivating failure is commons-cli requiring Maven `[3.9,)` while
the system Maven in Ubuntu 24.04 is `3.8.7`. The broader goal is to prevent any
tool from repeatedly using a runtime executable that has already been proven
incompatible with the project.

## Non-Goals

- Do not make the new env tool download, install, or update toolchains.
- Do not hide autonomous installation behind the harness.
- Do not modify project files such as `pom.xml`, `.mvn/`, `build.gradle`,
  `gradle.properties`, `.sdkmanrc`, `package.json`, `pyproject.toml`, or
  repository-tracked config.
- Do not replace `ToolchainManager`; extend it with an overlay-backed candidate
  source.
- Do not store secrets in the overlay.
- Do not make every tool implement bespoke environment logic.

## Current Problem

SAG currently spreads runtime environment state across several places:

- `project_setup`, `system`, and `maven` may append exports to `/etc/profile`
  or `~/.bashrc`.
- `ToolchainManager` persists executable candidates in
  `/workspace/.setup_agent/toolchains.json`, but it does not represent the
  currently active runtime overlay.
- `MavenTool` can detect a project Maven requirement from build output, but the
  requirement does not become a durable execution constraint.
- `BashTool` can download or inspect tool binaries, but those discoveries are
  local to one command unless the agent manually repeats the path later.
- `DockerOrchestrator` sources profile files before running commands, but there
  is no structured, container-local overlay that all commands can share.

The result is a weak memory boundary: the agent can correctly infer that
`/usr/bin/mvn` 3.8.7 violates `[3.9,)`, but later tool calls can still fall back
to the same executable because the incompatible runtime was not recorded as an
execution-layer fact.

## Design Principles

- Keep installation explicit. The agent uses `bash` or `system` to install,
  download, unpack, and verify tools.
- Keep activation structured. The env tool records verified runtime facts and
  exposes them to all tools.
- Preserve raw evidence. The overlay should supplement tool output, not filter
  or replace build logs.
- Prefer deep modules. Most tools should consume the overlay through
  `DockerOrchestrator` or `ToolchainManager`, not parse overlay JSON directly.
- Keep project settings untouched. The overlay config belongs under
  `/workspace/.setup_agent`, not inside the cloned repository.
- Make blockers durable. Once a runtime executable is rejected by project
  evidence, repeated use of that exact incompatible executable should require a
  deliberate override or a changed constraint.

## Overlay File

Store the overlay in:

```text
/workspace/.setup_agent/env_overlay.json
```

The file is container-local and non-secret. It should travel with other SAG
setup artifacts and survive `sag continue`.

Example:

```json
{
  "version": 1,
  "active": {
    "maven": {
      "executable": "/opt/apache-maven-3.9.9/bin/mvn",
      "version": "3.9.9",
      "source": "agent_registered",
      "registered_at": "2026-06-05T12:00:00Z"
    }
  },
  "env": {
    "MAVEN_HOME": "/opt/apache-maven-3.9.9"
  },
  "path_prepend": [
    "/opt/apache-maven-3.9.9/bin"
  ],
  "blocked": [
    {
      "tool": "maven",
      "executable": "/usr/bin/mvn",
      "version": "3.8.7",
      "requirement": "[3.9,)",
      "source": "build_error",
      "reason": "Project Maven Enforcer rejected this runtime"
    }
  ]
}
```

The schema should stay intentionally small:

- `active`: selected executable per tool family.
- `env`: exported environment variables such as `JAVA_HOME`, `MAVEN_HOME`, or
  `GRADLE_HOME`.
- `path_prepend`: directories to prepend to `PATH`.
- `blocked`: executable/version pairs known to violate a project or user
  requirement.

## Env Tool API

Add a new `env` tool with these actions:

```text
env(action="inspect")
env(action="register", tool="maven", executable="/opt/apache-maven-3.9.9/bin/mvn", version="3.9.9")
env(action="activate", tool="maven", executable="/opt/apache-maven-3.9.9/bin/mvn")
env(action="block", tool="maven", executable="/usr/bin/mvn", version="3.8.7", requirement="[3.9,)", reason="...")
env(action="clear", tool="maven")
```

`inspect` returns the current overlay, active shell-visible values, discovered
toolchain candidates, and blockers.

`register` records a verified executable as a candidate. It should validate
that the path exists and is executable. When practical, it should probe the
version if the caller does not provide one.

`activate` selects an existing registered or discovered executable and updates
`active`, `env`, and `path_prepend` for that tool family. Activation may derive
home variables from known executable layouts, such as Maven's parent directory.

`block` records negative evidence. The tool should not infer project policy on
its own; callers pass the requirement and reason based on build output or
analysis.

`clear` removes the active selection for a tool family without deleting the
registered candidate history.

The tool must refuse paths under the cloned project when activation would imply
rewriting project settings. Project-local wrappers such as `./mvnw` remain a
separate, project-owned input and should be handled by `ToolchainManager`
wrapper discovery.

## Data Flow

1. A build tool fails and exposes evidence, such as Maven Enforcer requiring
   `[3.9,)`.
2. The agent decides what to do. It may use `bash` to download and unpack a
   compatible Maven distribution.
3. The agent verifies the downloaded executable with `bash` or a build tool
   version command.
4. The agent calls `env(action="register", ...)` to persist the candidate.
5. The agent calls `env(action="activate", ...)` to make that candidate the
   active runtime for the tool family.
6. `DockerOrchestrator` injects the overlay into all subsequent container
   command executions.
7. `ToolchainManager` sees the overlay active candidate before falling back to
   system PATH.
8. `maven`, `gradle`, `bash`, validators, and reports all observe the same
   runtime environment.

## Tool Coverage Review

### Unified Injection Layer

`DockerOrchestrator.execute_command()` and
`execute_command_with_monitoring()` should load the overlay before sourcing
profile files and before changing directory. This makes the overlay visible to
all container commands, including tools that do not explicitly understand env
state.

The injected shell prefix should be generated from parsed JSON, not by shelling
through untrusted text. Values must be quoted safely.

### Explicitly Env-Aware Tools

These tools need direct overlay semantics:

- `env`: owns overlay inspection, registration, activation, and blockers.
- `ToolchainManager`: includes active overlay executables as a candidate source
  and excludes blocked executables for hard requirements.
- `maven`: resolves Maven through explicit parameters, overlay active
  executable, wrapper, registered candidates, then system PATH.
- `gradle`: follows the same pattern for Gradle while preserving wrapper-first
  behavior when requested.
- `bash`: inherits the overlay for every command and includes overlay metadata
  in results when commands use common runtime binaries such as `mvn`, `gradle`,
  `java`, `javac`, `node`, `npm`, `python`, or `pip`.

### Indirectly Env-Aware Tools

These tools should read or inherit overlay state but should not own runtime
selection:

- `project_setup`: installs basic dependencies, but activation of discovered
  Java, Maven, or Gradle runtimes should move toward `env` rather than appending
  permanent exports to profile files.
- `system`: can install packages and verify Java, but verification should
  distinguish system defaults from overlay-active Java.
- `project_analyzer`: should include active overlay and blockers in generated
  execution plans when relevant.
- `PhysicalValidator`: command replay and build/test validation must inherit
  overlay so validation uses the same runtime as setup.
- `CommandTracker`: replayed build/test commands must inherit overlay.
- `report`: report generation should include active runtime and blockers as
  evidence, but should not modify overlay state.

### Passive Tools

These tools do not need custom overlay logic:

- `file_io`
- `context`
- `output_search`
- report sections that only read prior evidence

They inherit overlay behavior only when they happen to execute container
commands through the orchestrator.

### Out of Scope

These should not use the project runtime overlay:

- `web_search`
- LiteLLM/OpenAI/Anthropic provider configuration
- host-side `.env` configuration
- UI-only state rendering, except for read-only display of overlay facts

## Toolchain Resolution Priority

For build tools, effective executable selection should be:

1. Explicit tool parameter, if present.
2. Active env overlay executable, if it satisfies the effective requirement and
   is not blocked.
3. Project wrapper, when requested or when the tool's existing behavior prefers
   wrappers.
4. Registered candidates from `toolchains.json`.
5. Standalone discovered candidates in common install locations.
6. System PATH.

Hard requirements from project metadata, tool parameters, or build errors must
filter candidates before ranking. Version sorting is only a tie-breaker among
compatible candidates.

## Blocker Semantics

A blocker is negative evidence about a specific runtime executable, not a ban
on the tool family.

For example:

```json
{
  "tool": "maven",
  "executable": "/usr/bin/mvn",
  "version": "3.8.7",
  "requirement": "[3.9,)"
}
```

This means `/usr/bin/mvn` must not satisfy a Maven call that carries the same
hard requirement. It does not prevent the agent from using Maven with a
different executable, checking `mvn -version`, or using `/usr/bin/mvn` for a
project that has no Maven 3.9 requirement.

Tool observations should keep blockers visible in plain language:

```text
Blocked Maven runtime:
- /usr/bin/mvn 3.8.7 violates [3.9,)
Next action: activate a compatible Maven executable with env(action="activate", ...)
```

## Error Handling

- If the overlay file is missing, tools behave as they do today.
- If the overlay file is invalid JSON, tools should ignore it for command
  execution, surface a warning, and allow `env(action="inspect")` to diagnose
  the problem.
- If an active executable is missing, `ToolchainManager` should skip it and
  surface a stale-overlay warning.
- If all compatible candidates are blocked or missing, the build tool should
  fail before running the build command and explain which runtime fact blocks
  execution.
- If the agent intentionally wants to test a blocked executable, it should use
  an explicit override parameter in that tool call rather than relying on PATH
  fallback.

## Testing

Add focused tests with fake orchestrators:

- `env register` writes a candidate to `env_overlay.json` without touching
  project files.
- `env activate` updates `active`, `env`, and `path_prepend` for Maven.
- `DockerOrchestrator` command wrapping applies overlay exports before command
  execution.
- `ToolchainManager` prefers an overlay-active Maven candidate over system
  Maven when both satisfy the requirement.
- `ToolchainManager` rejects a blocked system Maven candidate for the matching
  hard requirement.
- `MavenTool` uses the overlay-active executable after activation.
- `BashTool` inherits overlay PATH and reports overlay metadata for Maven-like
  commands.
- `PhysicalValidator` and `CommandTracker` replay commands through the overlay
  injection path.
- Invalid overlay JSON is diagnosed but does not crash unrelated commands.

Do not require live Docker or external network for the unit tests.

## Acceptance Criteria

- The agent can download a tool with `bash`, register it with `env`, activate
  it with `env`, and then use it through `maven` or `bash` without repeating
  the executable path.
- The overlay file lives under `/workspace/.setup_agent` and contains no
  secrets.
- No project-tracked config is modified by `env`.
- A Maven `[3.9,)` blocker prevents silent fallback to `/usr/bin/mvn` 3.8.7 for
  that requirement.
- All container command execution paths inherit the overlay.
- Existing project setup behavior works when no overlay exists.
- The design remains generic for Maven, Gradle, Java, Node, Python, and future
  tool families.
