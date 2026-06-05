# Harness Toolchain Observability Design

Date: 2026-06-05
Status: Draft for spec review

## Goal

Modernize SAG's default container baseline and make build-tool failures easier
for the agent to act on without adding a new tool. The immediate failure mode is
the commons-cli setup run looping after Maven reports that the project requires
Maven `[3.9,)`, while the active container Maven is older.

The design should preserve agent autonomy: SAG should expose accurate facts,
clear tool contracts, and strict completion gates. It should not hide a new
autonomous installer behind the harness.

## Important Version Fact

Changing the default base image from `ubuntu:22.04` to `ubuntu:24.04` is useful
baseline modernization, but it is not itself a complete Maven 3.9 fix.

Ubuntu Noble 24.04 publishes Maven `3.8.7-2`, while the observed commons-cli
enforcer rule requires Maven `[3.9,)`. Ubuntu Resolute 26.04 carries Maven
3.9.x, but switching SAG's default base directly to 26.04 would be a broader and
more aggressive compatibility change.

## Current Problem

The previous commons-cli run exposed four separate contract gaps:

- `project_setup` installs Maven through apt. On the current base image this
  installed Maven 3.6.3, which satisfied "maven package exists" but not the
  project's Maven Enforcer requirement.
- `MavenTool` detects the Maven version requirement from build output and stores
  it in `ToolResult.metadata`, but the observation shown to the agent does not
  make that requirement explicit enough as a next-step contract.
- `ContextTool` allowed the generated task `Compile project using Maven` to be
  completed even when the summary and key results said the compile was blocked.
- `OutputStorageManager` writes JSONL through shell `echo`, so backticks in task
  text can be interpreted by the shell.

There is also a CLI correctness issue: `sag project ...` can print setup failed
without returning a non-zero process exit code.

## Non-Goals

- Do not add a new `toolchain install` tool.
- Do not make the harness silently download or activate a build tool version on
  behalf of the agent.
- Do not switch the default image directly to Ubuntu 26.04 in this phase.
- Do not create a custom SAG Docker image in this phase.
- Do not refactor unrelated React loop, UI, or report behavior.

## Proposed Approach

### 1. Base Image Default

Change SAG's default Docker base image from `ubuntu:22.04` to `ubuntu:24.04` in:

- `src/sag/config/settings.py`
- `.env.example`
- `.env`

Keep `SAG_DOCKER_BASE_IMAGE` as the override mechanism. Existing users can pin
`ubuntu:22.04`, try `ubuntu:26.04`, or provide a custom image without code
changes.

### 2. Maven Observation Contract

Keep Maven version resolution inside the existing `maven` tool and
`ToolchainManager`. When a Maven build failure includes a required Maven range,
the observation text should expose the structured requirement in plain text:

```text
Maven version requirement: [3.9,) (source: build_error)
Current Maven executable: /usr/bin/mvn
Current Maven version: 3.6.3
Compatible Maven candidate: none
Next action: provide or register a Maven executable that satisfies [3.9,), then
retry maven(..., maven_version_requirement="[3.9,)")
```

The agent can then choose its own action, usually through existing `bash`
commands, to download or install a compatible Maven binary. The harness should
not decide that action autonomously.

### 3. Completion Gate

`ContextTool` should reject completion for build/test-like tasks when the
summary or key results contain unresolved failure language such as:

- `blocked`
- `failed`
- `failure`
- `error`
- `no artifacts`
- `not in the allowed range`
- `cannot compile`

This gate should apply to analyzer-generated build/test descriptions such as
`Compile project using Maven`, not only to older `CORE SETUP` wording. The
check should remain conservative: it should block obvious false completions
without trying to prove success by parsing every possible build log. The
implementation should avoid blocking resolved or negated phrases such as
`fixed the error`, `error resolved`, or `no errors`.

### 4. Output Storage Safety

Replace every shell-mediated container write performed by
`OutputStorageManager.store_output()` with a safe write strategy that does not
interpret output text as shell syntax. This includes both:

- appending the full output record to `full_outputs.jsonl`
- saving `output_index.json`, which stores searchable snippets such as
  `first_100_chars` and `last_100_chars`

The implementation can use an existing Docker file API helper if available, or
a single-quoted heredoc with a delimiter that cannot collide with the payload.

The output record should remain the same JSONL schema, and the output index
behavior should remain compatible with current retrieval and search code.

### 5. CLI Failure Exit Code

When `agent.setup_project(...)` returns `False`, `sag project ...` should return
a non-zero process exit code after printing the failure guidance.

This should not affect `sag run --task` success semantics.

## Data Flow

1. The container starts from the configured base image, defaulting to
   `ubuntu:24.04`.
2. `project_setup` may install system Maven through apt, but that only means a
   Maven executable exists.
3. `maven` runs the build and detects version requirements from Maven Enforcer
   output.
4. `ToolResult.metadata` carries the structured requirement, current executable,
   current version, and candidate-resolution outcome when available.
5. `ToolOrchestrator.format_tool_result()` renders those fields into the
   observation.
6. The agent decides whether to use existing `bash`, `system`, or another
   current tool to provide a compatible executable.
7. `ContextTool` prevents blocked build/test tasks from being recorded as
   completed.

## Testing

Add focused unit tests:

- `Config()` and `Config.from_env()` default to `ubuntu:24.04`, while
  `SAG_DOCKER_BASE_IMAGE` still overrides the value.
- Maven failed observations include detected
  `metadata["maven_version_requirement"]` and actionable version text.
- `ContextTool` rejects completion of `Compile project using Maven` when the
  summary/key results say the compile is blocked or failed.
- `OutputStorageManager` stores output and index snippets containing backticks
  without invoking shell command substitution.
- `sag project` returns non-zero when setup returns `False`.

Use fake orchestrators and existing unit-test patterns. Do not require Docker or
LLM integration for this implementation phase.

## Acceptance Criteria

- Default configuration and example env files use `ubuntu:24.04`.
- No new tool is added.
- A Maven `[3.9,)` failure is visible to the agent as a concrete requirement,
  not only buried in raw build output.
- A blocked Maven compile task cannot be marked complete without `force=True`.
- Output storage safely persists both full output records and index files when
  text contains backticks.
- CLI setup failure has a non-zero exit status.
- Existing tests pass, plus new focused tests for the contracts above.
