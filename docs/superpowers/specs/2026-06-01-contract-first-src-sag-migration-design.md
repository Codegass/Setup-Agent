# Contract-First `src/sag` Migration Design

Date: 2026-06-01
Status: Approved for implementation planning

## Goal

Introduce a single runtime package namespace, `sag`, and stabilize the core contracts that currently make Setup-Agent hard to maintain. This phase should make the project installable, importable, and guarded by focused contract tests before larger architectural refactors.

The selected scope is a lightweight `src/sag/` migration plus minimal contract repairs. It is an intentional breaking change: old internal import paths such as `agent.*`, `tools.*`, `config.*`, `docker_orch.*`, `reporting.*`, `ui.*`, and `testcases.*` will be replaced rather than preserved through compatibility shims.

## Non-Goals

- Do not decompose `ReActEngine`, `ReportTool`, or `PhysicalValidator` in this phase.
- Do not redesign validation/report ownership or build a new evidence model yet.
- Do not add Docker or LLM end-to-end tests in this phase.
- Do not preserve legacy import compatibility layers for old top-level packages.
- Do not change agent behavior except where required to fix confirmed contract bugs.

## Constraints

- Commit messages must not include Co-Authorship or similar authorship trailers.
- Existing unrelated worktree changes must not be reverted or included in the spec/implementation commits.
- The implementation should keep changes reviewable: mechanical namespace migration, contract fixes, and tests should be easy to inspect separately.

## Architecture

The runtime package will be rooted at `src/sag/`.

Planned package layout:

```text
src/sag/
  __init__.py
  main.py
  agent/
  config/
  docker_orch/
  reporting/
  testcases/
  tools/
  ui/
```

`pyproject.toml` will publish only the `sag` package and point the CLI entrypoint at `sag.main:cli`.

All internal imports in runtime code will use absolute `sag.*` imports. For example:

```python
from sag.agent.agent import SetupAgent
from sag.tools.base import BaseTool, ToolResult
from sag.ui.events import UIEventEmitter
```

The first phase keeps the current module responsibilities intact. The package namespace becomes stable first; later phases can split large modules behind that stable namespace.

## Components And Contracts

### Packaging Contract

The package must be installable from a built wheel. After installation, these must work:

- `import sag`
- imports for core runtime modules such as `sag.agent.react_engine`, `sag.tools.base`, `sag.reporting`, and `sag.testcases.catalog`
- loading the CLI entrypoint `sag`

The old top-level module paths are not part of the new contract.

### Tool Schema Contract

Every tool must expose one public parameter schema API:

```python
tool.get_parameter_schema()
```

`BaseTool` should provide this public method and delegate to the existing schema storage. Tools may still define custom schema data, but callers should not need to know about private method names such as `_get_parameters_schema()`.

`ReActEngine` should consume the public API and should not silently fall back to an empty schema for tools that already declare a schema through the standard mechanism.

### Tool Result Contract

`ToolResult` must explicitly support the structured fields that runtime callers rely on. In particular, report generation needs a safe place to return full report data and snapshots without depending on undeclared Pydantic fields that may be dropped.

The chosen contract is a declared `raw_data: Optional[Dict[str, Any]]` field on `ToolResult`. `ReportTool` should use `raw_data` for full report payloads and keep `metadata` for execution metadata such as completion flags, verified status, timestamps, and summary snapshots.

### Agent State Contract

`AgentStateAnalysis` and `AgentStatus` must use consistent names and values.

Required corrections:

- Define `AgentStatus.STUCK = "stuck"` because existing evaluator branches already use that semantic state.
- Use `guidance_message` and `guidance_priority` consistently.
- Do not construct `AgentStateAnalysis` with undeclared fields such as `guidance` or `priority`.

The contract should make guidance output deterministic and easy for `ReActEngine` to consume.

### Review Agent Gate

After implementation, an independent review agent will check correctness only. It will not reopen the design direction.

The review should verify:

- `src/sag` migration completeness
- no runtime old absolute imports remain
- package build/install/import behavior works
- tool schema, result, state, and report contracts are tested
- no unnecessary behavior changes were introduced

## Migration Flow

1. Create the `src/sag/` package structure and move runtime modules into it.
2. Update internal imports to use absolute `sag.*` paths.
3. Update `pyproject.toml` package discovery and CLI entrypoint.
4. Fix the tool schema contract by exposing and using `get_parameter_schema()`.
5. Fix the `ToolResult` structured data contract used by report generation.
6. Fix `AgentStateAnalysis` and `AgentStatus` inconsistencies.
7. Add focused tests for packaging/import smoke and contract behavior.
8. Run the test suite and static guards.
9. Ask the independent review agent to inspect implementation correctness.
10. Address review findings before considering the phase complete.

## Error Handling

This phase should prefer deterministic contract failures over silent fallback behavior.

- Missing or invalid tool schema should fail tests.
- Unexpected result fields should either be declared or removed from the caller contract.
- Invalid state analysis fields or enum values should fail tests.
- Old runtime import paths under `src/sag` should fail static guard tests.
- Packaging omissions should fail wheel/import smoke tests.

Runtime fallback behavior that is unrelated to these contracts should be left alone.

## Testing Strategy

Use focused, fast tests.

Required coverage:

- Import smoke tests:
  - `import sag`
  - import core runtime modules
  - load the CLI object
- Packaging smoke test:
  - build a wheel
  - install it in an isolated environment
  - verify import and CLI loading
- Tool contract tests:
  - `BaseTool.get_parameter_schema()`
  - schemas for representative tools are non-empty and include expected parameters
- Result/state contract tests:
  - `ToolResult` preserves the structured report data contract
  - `AgentStateEvaluator` guidance output uses declared fields and valid statuses
- Report contract test:
  - `ReportTool` returns structured report data through the chosen declared field
- Static import guard:
  - scan runtime code under `src/sag` for disallowed old absolute imports

Do not add Docker/LLM integration tests in this phase.

## Success Criteria

The phase is complete when:

- runtime code lives under `src/sag`
- internal imports use `sag.*`
- the package builds and installs from a wheel
- the CLI entrypoint resolves to `sag.main:cli`
- contract tests cover the known schema/result/state/report issues
- static guard catches old runtime import paths
- all focused tests pass
- the independent review agent finds no blocking correctness issues

## Risks And Mitigations

Risk: The namespace migration touches many files and may introduce mechanical import mistakes.
Mitigation: Use static import guards, import smoke tests, and review agent verification.

Risk: Moving to `src/sag` is a breaking change for users importing old top-level packages.
Mitigation: Document the breaking change and avoid partial compatibility that could hide stale imports.

Risk: Contract fixes could drift into behavioral refactoring.
Mitigation: Keep changes limited to confirmed breakpoints and their tests.

Risk: Packaging smoke tests may be slower than pure unit tests.
Mitigation: Keep them focused on build/install/import rather than full agent execution.
