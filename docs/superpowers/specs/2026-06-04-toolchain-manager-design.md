# Toolchain Manager Design

Date: 2026-06-04
Status: Draft for spec review

## Goal

Create a generic ToolchainManager that gives SAG one source of truth for
runtime tools such as Maven, Java, Gradle, Node, Python, and future build
systems. The immediate bug is Maven falling back to `/usr/bin/mvn` after a
newer Maven binary was discovered, but the fix should not be Maven-only.

The first implementation phase should make Maven use the manager end to end
while leaving room for other toolchains to adopt the same interface later.

## Current Problem

Toolchain state is currently split across several layers:

- `SystemTool` installs packages through apt and returns command output, but it
  does not record the resulting executable or version as shared state.
- `BashTool` can run commands that download or verify standalone binaries, but
  those discoveries are local to a single shell command.
- `MavenTool` checks only `which mvn` and builds commands with the literal
  executable `mvn`, so it falls back to the system Maven even when a newer
  Maven exists elsewhere in the container.
- `DockerOrchestrator` sources `/etc/profile` and `~/.bashrc`, but there is no
  structured contract for updating those files or resolving the intended tool
  executable.
- Build-tool parameter rewriting currently detects `"mvn"` by substring. That
  caused `--fail-at-end` to be appended to unrelated commands such as `find`
  and `tail` when their command text happened to contain Maven-related text.

In the commons-cli run, Maven 3.9.6 was downloaded and verified at
`/tmp/apache-maven-3.9.6/bin/mvn`, but later `MavenTool` invocations still used
`/usr/bin/mvn` 3.6.3. The project's Maven Enforcer rule rejected that version,
and SAG spent the remaining loop iterations repeating the same invalid build.

## Design Principles

- Keep a deep module boundary: callers ask for a toolchain by name and
  requirement; they do not inspect PATH, parse versions, or know installation
  directories.
- Store durable runtime facts in the container, not in a Python object that
  disappears between tool calls or `sag continue`.
- Prefer project-local wrappers when explicitly requested or when they exist
  and are executable.
- Prefer verified tools that satisfy the active requirement over system defaults
  that violate it.
- Keep installation separate from resolution. Resolution should discover and
  choose; installation should add candidates and then register them.
- Make command rewriting precise. Build-tool behavior should inspect command
  tokens, not broad substrings.

## Proposed Module

Add `src/sag/tools/toolchain_manager.py`.

The manager owns a small API:

```python
@dataclass(frozen=True)
class ToolchainSpec:
    name: str
    executable: str
    version_requirement: ToolVersionRequirement | None = None
    prefer_wrapper: bool = True


@dataclass(frozen=True)
class ToolVersionRequirement:
    raw: str
    source: Literal[
        "tool_parameter",
        "project_metadata",
        "build_error",
        "conversation",
        "registered_state",
    ]
    kind: Literal["exact", "range", "minimum", "maximum", "preferred"]


@dataclass(frozen=True)
class ToolExecutableCandidate:
    name: str
    executable: str
    path: str
    version: str | None
    source: Literal["wrapper", "registered", "standalone", "path", "system"]


@dataclass(frozen=True)
class ResolvedToolExecutable:
    candidate: ToolExecutableCandidate
    reason: str


class ToolchainManager:
    def resolve(self, spec: ToolchainSpec, working_directory: str = "/workspace") -> ResolvedToolExecutable | None:
        ...

    def register(self, candidate: ToolExecutableCandidate) -> None:
        ...

    def discover(self, spec: ToolchainSpec, working_directory: str = "/workspace") -> list[ToolExecutableCandidate]:
        ...

    def ensure_path(self, candidate: ToolExecutableCandidate) -> None:
        ...
```

`ToolExecutableCandidate` is intentionally not a whole toolchain. It is one
possible executable for one tool command, such as `./mvnw`,
`/tmp/apache-maven-3.9.6/bin/mvn`, or `/usr/bin/mvn`.

`ResolvedToolExecutable` is the manager's decision. It wraps the selected
candidate and records a short reason for logs, tool metadata, and future
debugging.

The manager persists registered candidates in:

```text
/workspace/.setup_agent/toolchains.json
```

This file is container-local and copied with other setup artifacts. It should
contain only non-secret runtime facts: tool name, executable path, version,
source, timestamps, and optional metadata.

## Resolve Logic

`resolve()` is a deterministic constraint-satisfaction pipeline. It is not a
"pick the newest version" helper.

1. Build the effective requirement from `ToolchainSpec`.
   - `name` identifies the toolchain family, such as `maven`.
   - `executable` identifies the command name, such as `mvn`.
   - `version_requirement` is optional and may express an exact version, a
     range, a minimum, a maximum, or a preferred version.
   - `prefer_wrapper` controls whether project-local wrappers outrank other
     candidates when they satisfy the requirement.
2. Call `discover()` to collect candidates from project wrappers, persisted
   registrations, common standalone install locations, and PATH.
3. Verify each candidate before ranking it.
   - The path must exist and be executable.
   - Version probing must use the candidate path directly, not PATH.
   - Candidates that cannot be probed may stay in the list only when the tool
     family has a valid no-version fallback rule. Maven candidates without a
     parseable version are lower priority than parseable Maven candidates.
4. Filter by requirement.
   - `exact`: keep only candidates matching that version.
   - `range`: keep only candidates inside the declared range.
   - `minimum`: keep only candidates at or above the minimum.
   - `maximum`: keep only candidates at or below the maximum.
   - `preferred`: prefer that version when present, but allow a fallback when
     no exact preferred candidate exists.
   - If every candidate violates a hard requirement, return `None` with enough
     metadata for the caller to explain the unmet requirement.
5. Rank compatible candidates.
   - Executable wrapper in the project directory wins when `prefer_wrapper` is
     true and the wrapper satisfies the version requirement.
   - Registered candidates come next because they represent verified runtime
     facts from prior tool actions.
   - PATH/system candidates are the normal fallback for unconstrained
     resolution.
   - Standalone candidates that were not registered are used only when they
     satisfy a requirement or no PATH/system candidate exists.
   - Version ordering is only a tie-breaker among candidates that already
     satisfy the same requirement and source priority. It must not override an
     exact project requirement or user-provided parameter.
6. Return `ResolvedToolExecutable` with the selected candidate and reason.
7. Do not install inside `resolve()`. Installation is a separate caller action.
   The caller may install or download a new tool, register it, then call
   `resolve()` again.

This keeps `resolve()` pure enough to test and reason about: it can inspect the
container and persisted registry, but it should not mutate the environment
except for optional diagnostics-free cache refreshes that do not change PATH or
install packages.

## Requirement Sources

The manager should receive version requirements; it should not scrape prompts or
chat logs directly. Requirement extraction belongs at the caller boundary where
the evidence is visible.

Requirement source precedence:

1. Explicit tool parameter from the agent or user-facing tool call, such as
   `maven_version="3.9.6"` or `version_requirement="[3.9,4.0)"`.
2. Project metadata, such as a Maven Enforcer `requireMavenVersion` rule or a
   wrapper configuration.
3. Build error evidence, such as `Detected Maven Version: 3.6.3 is not in the
   allowed range [3.9,)`.
4. Conversation or task history when the current setup context already records
   a concrete requirement from the user or the agent's prior analysis.
5. Registered state from previous verified tool actions.

Conversation-derived requirements must be converted into a structured
`ToolVersionRequirement` before calling `resolve()`. This keeps
`ToolchainManager` deterministic and testable while still allowing the agent to
use prior context when the project version is not discoverable from files.

## Maven Resolution Order

For Maven in the first implementation phase:

1. If `use_wrapper=True` or `prefer_wrapper=True`, check an executable
   `./mvnw` in the requested working directory.
2. Load registered candidates from `.setup_agent/toolchains.json`.
3. Discover standalone Maven distributions in common locations such as
   `/tmp/apache-maven-*/bin/mvn`, `/opt/apache-maven-*/bin/mvn`, and
   `/usr/local/apache-maven-*/bin/mvn`.
4. Resolve the current PATH executable with `command -v mvn`.
5. Verify candidate versions with `<path> -version`.
6. Choose a candidate satisfying the version requirement when one is known.
7. If no version requirement is known, prefer wrappers, then registered
   candidates, then PATH/system candidates, with unregistered standalone
   candidates used only when no normal executable exists.

Version comparison should parse semantic numeric segments and ignore suffixes
that do not affect ordering for the common Maven format. Version comparison is
used to satisfy constraints and break ties; it is not the primary policy.

## Maven Integration

`MavenTool` should receive or create a `ToolchainManager`.

Before building a Maven command, it should call:

```python
resolved = toolchain_manager.resolve(
    ToolchainSpec(
        name="maven",
        executable="mvn",
        version_requirement=required_version,
    ),
    working_directory=working_directory,
)
```

Then `_build_maven_command()` should use `resolved.candidate.path` rather than
hardcoded `mvn`. If no executable is resolved, `_install_maven()` may run and
then register the resulting candidate.

`MavenTool` should infer `required_version` from reliable evidence:

- Maven Enforcer output such as `allowed range [3.9,)`.
- POM metadata when a simple `requireMavenVersion` rule is visible.
- Explicit Maven version or version requirement parameters supplied by the
  agent.
- Current task or conversation history when it contains a concrete Maven
  requirement.
- No requirement when the evidence is insufficient.

This requirement should be passed to the manager. The manager should not know
about Maven Enforcer messages; parsing build output stays in `MavenTool`.

## Environment Persistence

`ToolchainManager.ensure_path()` may update `/etc/profile` and `/root/.bashrc`
only after a candidate has been verified. It should prepend the candidate's
directory to PATH using a stable SAG-managed block so repeated runs do not
append duplicate exports.

Example:

```text
# SAG_TOOLCHAIN_PATH_BEGIN
export PATH="/tmp/apache-maven-3.9.6/bin:$PATH"
# SAG_TOOLCHAIN_PATH_END
```

Direct tool execution should not rely on PATH. Dedicated tools should use the
resolved absolute path. PATH persistence exists for shell compatibility and
future `sag continue` sessions.

## Bash Command Rewriting

`ToolParameterNormalizer` should stop appending Maven flags by substring. It
should tokenize shell commands conservatively with `shlex` for simple commands
and only append Maven-specific flags when the executable token is `mvn`,
`./mvnw`, or an absolute Maven executable path.

For compound commands, only the Maven segment should be considered. If the
command is too complex to parse safely, do not rewrite it. Let `MavenTool` own
Maven behavior.

## Non-Goals

- Do not implement full installers for every ecosystem in the first phase.
- Do not replace all existing system setup logic at once.
- Do not force every bash command through ToolchainManager.
- Do not introduce a global dependency solver.
- Do not make Maven-specific version parsing part of the generic manager.
- Do not change project analysis or ReAct prompt behavior.

## Testing

Add tests before implementation:

- `ToolchainManager` discovers multiple Maven candidates and chooses a
  candidate satisfying an exact version requirement without upgrading to a
  newer incompatible version.
- `ToolchainManager` chooses a compatible candidate for a range requirement
  such as `[3.9,4.0)`.
- `ToolchainManager` uses version ordering only as a tie-breaker among
  compatible candidates.
- Registered candidates are persisted and loaded from
  `.setup_agent/toolchains.json`.
- `MavenTool` uses the resolved absolute Maven path instead of hardcoded
  `mvn`.
- `MavenTool` can infer a structured version requirement from Enforcer output
  and resolve a satisfying candidate on the next run.
- Bash parameter normalization does not append `--fail-at-end` to non-Maven
  commands containing Maven text, `find`, `tail`, or `curl`.
- Existing Maven timeout and property behavior remains unchanged.

## Rollout

Phase 1:

- Add ToolchainManager with Maven discovery, registration, version comparison,
  and PATH persistence.
- Wire `MavenTool` to use it.
- Tighten bash Maven flag rewriting.

Phase 2:

- Let `SystemTool` register installed Java and Maven candidates after package
  installation.
- Move Java environment discovery toward the same manager.

Phase 3:

- Add Gradle, Node, and Python adapters only when a real workflow needs them.
  Each adapter should use the same manager API without broadening it.
