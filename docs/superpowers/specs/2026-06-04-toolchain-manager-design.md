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
- Prefer verified higher-version standalone tools over older system defaults
  when the project or previous failure evidence requires it.
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
    min_version: str | None = None
    prefer_wrapper: bool = True


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

`resolve()` is a deterministic selection pipeline:

1. Build the effective requirement from `ToolchainSpec`.
   - `name` identifies the toolchain family, such as `maven`.
   - `executable` identifies the command name, such as `mvn`.
   - `min_version` is optional. If present, incompatible lower-version
     candidates are not selected.
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
   - If `min_version` is set, remove candidates with a known version below it.
   - If every candidate is below the minimum, return `None` with enough
     metadata for the caller to explain the unmet requirement.
5. Rank compatible candidates.
   - Executable wrapper in the project directory wins when `prefer_wrapper` is
     true and the wrapper satisfies `min_version`.
   - Registered candidates come next because they represent verified runtime
     facts from prior tool actions.
   - Standalone candidates are ranked by version, highest first.
   - PATH/system candidates are the fallback, also ranked by version when
     available.
6. Return `ResolvedToolExecutable` with the selected candidate and reason.
7. Do not install inside `resolve()`. Installation is a separate caller action.
   The caller may install or download a new tool, register it, then call
   `resolve()` again.

This keeps `resolve()` pure enough to test and reason about: it can inspect the
container and persisted registry, but it should not mutate the environment
except for optional diagnostics-free cache refreshes that do not change PATH or
install packages.

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
6. Choose the highest compatible candidate when `min_version` is known.
7. If no `min_version` is known, prefer wrappers, then registered candidates,
   then higher-version standalone candidates, then PATH/system candidates.

Version comparison should parse semantic numeric segments and ignore suffixes
that do not affect ordering for the common Maven format.

## Maven Integration

`MavenTool` should receive or create a `ToolchainManager`.

Before building a Maven command, it should call:

```python
resolved = toolchain_manager.resolve(
    ToolchainSpec(name="maven", executable="mvn", min_version=required_version),
    working_directory=working_directory,
)
```

Then `_build_maven_command()` should use `resolved.candidate.path` rather than
hardcoded `mvn`. If no executable is resolved, `_install_maven()` may run and
then register the resulting candidate.

`MavenTool` should infer `required_version` from reliable evidence:

- Maven Enforcer output such as `allowed range [3.9,)`.
- POM metadata when a simple `requireMavenVersion` rule is visible.
- No requirement when the evidence is ambiguous.

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

- `ToolchainManager` discovers multiple Maven candidates and chooses the
  highest compatible candidate for `min_version="3.9"`.
- Registered candidates are persisted and loaded from
  `.setup_agent/toolchains.json`.
- `MavenTool` uses the resolved absolute Maven path instead of hardcoded
  `mvn`.
- `MavenTool` can infer `min_version="3.9"` from Enforcer output and resolve a
  newer candidate on the next run.
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
