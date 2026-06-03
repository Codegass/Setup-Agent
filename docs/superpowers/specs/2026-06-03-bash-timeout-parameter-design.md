# Bash Timeout Parameter Design

## Goal

Allow the agent to pass `timeout` to the `bash` tool as the command's maximum
total execution time in seconds.

## Current Problem

`BashTool._get_parameters_schema()` already exposes `timeout` and the tool
parameter normalizer fills the default `timeout=60`, but `BashTool.execute()`
does not accept `timeout`. The value falls into `**kwargs` and is rejected as an
invalid parameter. Separately, some callers already pass `timeout` to
`DockerOrchestrator.execute_command()`, but the real method signature does not
accept it.

## Design

- `BashTool.execute()` accepts `timeout: int = 60`.
- For normal commands, `timeout` is passed to `DockerOrchestrator.execute_command()`.
- `DockerOrchestrator.execute_command()` accepts optional `timeout` and wraps the
  shell command with GNU `timeout --preserve-status` when a positive value is
  provided.
- For long-running monitored commands, `timeout` overrides the monitored
  `absolute_timeout`. The automatically selected `silent_timeout` remains in
  use and is capped so it cannot exceed the total timeout.
- Background commands ignore `timeout` for the detached process; they still
  return immediately with the background PID.
- Bash timeout recovery remains guidance-only. Recovery metadata and executed
  params preserve the timeout value so UI/evidence/diagnosis can show what was
  attempted.
- The bash tool schema, invalid-parameter help, and usage examples mention
  `timeout`.

## Non-Goals

- Do not add a separate public `silent_timeout` parameter.
- Do not change Maven/Gradle dedicated tool timeout behavior.
- Do not retry bash timeout failures automatically.

## Testing

- Contract tests prove the real bash schema exposes timeout and the ReAct tool
  schema preserves it.
- Bash tool tests prove normal commands pass timeout to Docker execution and
  monitored commands use timeout as absolute timeout.
- Orchestration/recovery tests prove `timeout` remains in executed params and
  bash timeout guidance metadata.
