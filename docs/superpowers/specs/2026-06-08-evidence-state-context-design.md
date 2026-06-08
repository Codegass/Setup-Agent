# Evidence State and Context Design

## Purpose

SAG currently relies too heavily on natural-language task summaries to decide
whether a setup step succeeded. This can produce contradictory outcomes, such as
a Maven command reporting `BUILD SUCCESS` while surefire reports still contain
test failures. The system then risks presenting a successful setup even though
the evidence is only partial.

This design adds a structured evidence state layer beside the existing
trunk/branch context model. It does not replace natural-language context.
Natural language remains the agent's reasoning and compression layer; structured
evidence becomes the trusted state layer for validators, reports, UI, and final
status decisions.

## Goals

- Preserve the current trunk-to-branch context management model.
- Keep natural-language `summary`, `key_results`, and
  `previous_task_summary` available to future branch tasks.
- Add structured evidence state so task completion is not decided only by text
  such as `BUILD SUCCESS`.
- Make raw tool output traceable whenever a tool returns a success, partial,
  blocked, conflict, or unknown result.
- Keep bash generic: it reports command execution facts only, not build/test
  domain meaning.
- Support Maven, Gradle, mixed Maven/Gradle projects, and generic shell-driven
  setup flows.
- Provide local repeatable validation without adding GitHub Actions CI.

## Non-Goals

- Do not replace trunk/branch context with event sourcing.
- Do not make the harness install dependencies, rewrite commands, or perform
  recovery on behalf of the agent.
- Do not turn bash into a domain classifier for every possible tool.
- Do not remove percentage-based build/test statistics from reports or UI.
- Do not hide raw output behind friendly natural-language summaries.

## Core Model

The system will carry two related but separate chains of information.

### Narrative Context

Narrative context remains the continuity layer for agent reasoning:

- `summary`
- `key_results`
- `previous_task_summary`
- branch history entries
- context compression summaries

This information tells the next branch task what happened in a compact,
human-readable form.

### Evidence State

Evidence state is the authority layer for validators, report generation, UI
status, and final setup classification:

- `evidence_status`
- `evidence_refs`
- `conflicts`
- `validator_findings`
- raw output refs
- test/build statistics

Natural-language context can describe evidence, but it cannot override evidence
state.

## State Model

Evidence status is intentionally constrained to five states:

- `success`: evidence and validator findings support success.
- `partial`: meaningful progress happened, but required validation still has
  remaining failures or missing pieces.
- `blocked`: the task cannot make progress without external input, unavailable
  dependencies, credentials, network access, or another blocking condition.
- `conflict`: tool summary, raw output, validator findings, or report content
  contradict each other.
- `unknown`: evidence is insufficient to classify the result.

Task flow status and evidence status are separate. A task may be flow-completed
while its evidence status is partial or conflict.

Example:

```text
task.status = completed
task.evidence_status = partial
task.key_results = Maven package completed, but surefire reported 3 failures.
task.evidence_refs = [maven_output_ref, surefire_report_ref, validator_ref]
task.conflicts = [maven_success_vs_surefire_failures]
```

## Tool Observation Contract

Tools should return observations with structured facts and raw output refs. A
domain tool may include an initial domain verdict, but that verdict is not the
final authority.

Recommended shape:

```text
tool
command or operation
params
execution facts
initial_status
findings
raw_output_ref
artifact_refs
duration
```

Domain tools such as Maven and Gradle can inspect domain-specific evidence:

- process exit code
- build output
- surefire XML
- failsafe XML
- Gradle test result XML
- generated reports
- known build artifacts

If domain evidence contradicts the tool's first impression, the observation
must preserve the contradiction rather than returning a clean success.

## Bash Tool Contract

Bash must stay generic. It reports whether the command execution itself worked,
not whether the project-level goal succeeded.

Required facts:

```text
command
cwd
executed
exit_code
timed_out
stdout_ref
stderr_ref
combined_output_ref
duration
```

Suggested execution semantics:

- `executed=true`, `exit_code=0`: the shell command ran and returned zero.
- `executed=true`, `exit_code!=0`: the shell command ran and returned non-zero.
- `executed=false`: the command could not start, such as invalid cwd,
  container exec failure, permission issue, or executable not found.
- `timed_out=true`: the command started but exceeded its timeout.

Bash does not assign `success`, `partial`, `blocked`, or `conflict` as domain
meaning. Domain meaning belongs to agent reasoning, domain tools, and
validators.

## Validator Boundary

Validators perform trusted evidence checks. They do not modify the workspace,
install tools, rewrite commands, or choose recovery actions for the agent.

Validator responsibilities:

- read tool observations and raw output refs;
- inspect known evidence artifacts;
- detect contradictions;
- produce authoritative evidence status;
- record findings and raw refs.

Example:

```text
tool_observation:
  tool = maven
  exit_code = 0
  initial_status = success
  raw_output_ref = output_abc

validator_finding:
  type = contradiction
  reason = surefire_reports_contain_failures
  authoritative_status = partial
```

The agent remains responsible for strategy. When it sees partial, conflict, or
unknown evidence, it decides whether to inspect logs, rerun a command, use a
domain tool, ask the user, or report a blocker.

## Context Manager Changes

The current trunk/branch model remains the main interface.

Trunk task records should continue to store narrative fields:

```text
id
description
status
key_results
```

They should gain evidence fields:

```text
evidence_status
evidence_refs
conflicts
validator_findings
```

Branch history should continue to store:

```text
previous_task_summary
history_entries
compression_summary
```

It should gain compact evidence context:

```text
previous_task_evidence_digest
current_task_evidence_refs
```

`previous_task_evidence_digest` should be concise. It is not a raw log replay.
It gives the next branch task a trustworthy summary, such as:

```text
Previous task evidence:
- task_4 evidence_status: partial
- Maven command exited 0
- Surefire reports: 206 passed, 3 failed, 5 skipped
- Raw output: output_abc
```

## Task Completion Flow

`complete_with_results` remains the preferred completion path and continues to
require narrative context:

```text
summary
key_results
```

It should be extended to bind evidence:

```text
evidence_refs optional
evidence_status optional
```

Agent-supplied `evidence_status` is a claim, not the authority. Context Manager
and validators may override or refine it based on evidence.

Completion should:

1. Preserve `summary` and `key_results`.
2. Bind current task tool observations and evidence refs.
3. Read validator findings.
4. Compute final `evidence_status`.
5. Write narrative and evidence state back to trunk.

This keeps the next branch task informed without trusting free text as the sole
completion signal.

## Report Rules

Reports should be evidence-driven. Natural-language prose should explain the
structured evidence, not replace it.

Report inputs:

```text
overall_status = aggregate(task.evidence_status)
build_status = validator/tool evidence
test_status = validator/tool evidence
known_conflicts = conflicts
raw_refs = evidence_refs
```

Test statistics must keep both counts and percentages:

```text
tests:
  discovered: optional
  executed: 214
  passed: 206
  failed: 3
  skipped: 5
  pass_rate: 96.3%
  execution_rate: optional
```

Percentages help users understand the size of the remaining problem, but they
must not convert failures into success.

Example report wording:

```text
Result: PARTIAL

Build/package command completed, but test validation found failures.

Tests: 206 / 214 passed, 96.3% pass rate, 3 failed, 5 skipped.

Conflict:
- Maven returned exit 0 after test-failure-ignore, but surefire reports contain
  failures.
```

## UI Rules

The UI should show task flow and evidence result separately when they differ.

Example:

```text
Completed · Partial result
```

Default display order:

1. status summary;
2. evidence findings;
3. task narrative and key results;
4. raw outputs.

This keeps the default view result-first and traceable without flooding users
with logs.

## Agent Prompt Rules

Prompts should teach the agent these constraints:

- `completed` means the branch task flow ended; it does not automatically mean
  setup succeeded.
- `partial`, `conflict`, and `unknown` require reading evidence refs or raw
  output refs before making a final claim.
- Natural-language phrases such as `BUILD SUCCESS` cannot override validator
  findings.
- If a validator finding is missing or insufficient, the agent should gather
  more evidence using bash or a domain tool.
- Reports must distinguish fully successful, partially successful, blocked, and
  conflicting outcomes.

## Overall Status Aggregation

Initial aggregation can use a simple rule:

```text
any blocked -> blocked
else any conflict -> conflict
else any partial -> partial
else all success -> success
else unknown
```

Some tasks may eventually need weights. For example, report generation being
partial is not always equivalent to tests being partial. The first
implementation should use explicit rules rather than complex scoring.

## Testing Strategy

Testing should be layered and local. Do not add GitHub Actions CI for this
change.

### Unit and Contract Tests

Cover:

- evidence status enum parsing and transitions;
- bash execution facts;
- context manager storage of narrative and evidence fields;
- branch start receiving both previous summary and evidence digest;
- report aggregation;
- percentage preservation in test statistics;
- raw output ref propagation.

### Tool-Level Golden Tests

Use fixed raw output fixtures for Maven, Gradle, and bash:

- Maven `BUILD SUCCESS` plus surefire failures -> partial or conflict.
- Maven dependency resolution failure -> blocked.
- Maven compile success without package/test scope -> success only for compile
  scope.
- Gradle `BUILD SUCCESS` plus failing test reports -> partial or conflict.
- Gradle task failure -> blocked or partial based on evidence.
- Bash exit zero -> command execution succeeded only.
- Bash non-zero -> command executed with non-zero exit, no domain judgment.

### Manual End-to-End Regression Matrix

Run local manual validation on:

- `apache/commons-cli`: Maven happy path.
- `apache/commons-vfs`: Maven version recovery, UTF-8/RAT behavior, test
  failures must be partial rather than success.
- `apache/beam`: Gradle evidence extraction without Maven-only assumptions.
- `apache/iceberg`: mixed Gradle and Maven evidence without forcing one build
  manager.

For each run, record:

```text
session id
container name
selected ref/tag/commit
task flow statuses
evidence statuses
build stats
test stats
report status
UI status
raw output refs
known conflicts
```

If an end-to-end run reveals uncertain behavior, pause and classify the issue
before adding a fix:

- tool contract gap;
- validator evidence source gap;
- context propagation gap;
- prompt guidance gap;
- project-specific behavior that should not become a generic rule.

## Acceptance Criteria

- Natural-language trunk/branch continuity still works.
- A branch completion can write both `key_results` and evidence refs back to
  trunk.
- A completed task can have `evidence_status=partial` or `conflict`.
- Bash reports execution facts only.
- Maven/Gradle evidence can detect test failures even when process exit code is
  zero.
- Reports and UI preserve test counts and percentages.
- Reports do not call a setup successful when required tests still fail.
- Raw output remains traceable from tool observation to report/UI.
- Local validation covers commons-cli, commons-vfs, Beam, and Iceberg before the
  implementation is treated as complete.
