# Project Version Ref Handle Design

Date: 2026-06-07
Status: Draft for spec review

## Goal

Allow `sag project` users to pin the repository version that SAG sets up:

```bash
uv run sag project https://github.com/apache/commons-cli.git --ref rel/commons-cli-1.11.0
uv run sag project https://github.com/apache/commons-cli.git --ref ae44dcd
uv run sag project https://github.com/apache/dubbo.git --ref dubbo-3.2.19
```

The handle is intentionally Git-ref-like. It may be a branch, tag, release tag,
short commit, or full commit hash. SAG should not treat the absence of a ref as
"latest release"; when no ref is provided, current default-branch behavior stays
unchanged.

## Non-Goals

- Do not call GitHub Releases APIs or introduce GitHub-only version resolution.
- Do not infer semantic version ranges such as "latest 1.x".
- Do not silently fall back to the default branch when a requested ref fails.
- Do not rename the cloned project directory based on the ref.
- Do not remove the existing `branch` tool parameter immediately; keep it as a
  compatibility alias while prompts and docs move to `ref`.

## Semantics

`--ref <handle>` means "clone this repository, then check out this exact
Git-resolvable handle before project analysis starts."

Examples:

- `ae44dcd` or `ae44dcdffd28d6a1a32dc4e0801b715adcef162e`: commit handle.
- `rel/commons-cli-1.11.0`: tag with slash.
- `dubbo-3.2.19`: GitHub release tag, handled as a normal tag.
- `master`, `main`, or `release/foo`: branch or ref name.

SAG does not need to classify the handle before checkout. Git is the source of
truth. The resolved commit should be recorded after checkout with
`git rev-parse HEAD`.

## CLI Surface

Add an option to `sag project`:

```text
--ref <handle>
```

The command should display the ref in non-UI startup output when present and pass
it to `SetupAgent.setup_project`.

The generated default goal can remain unchanged. The setup prompt should carry
the ref separately from the URL so the agent never has to parse version
information out of a modified URL.

## Agent Data Flow

Thread the requested ref through:

1. `sag.main.project(..., ref)`.
2. `SetupAgent.setup_project(project_ref=...)`.
3. Project metadata under `/workspace/.setup_agent/project_meta.json`.
4. `ReActEngine.set_repository_url(...)` or an equivalent repository target
   setter that stores both URL and ref.
5. `ReactPromptBuilder` prompt snippets that currently mention only
   `repository_url`.
6. `ToolParameterNormalizer` and `ToolRecoveryHandler` so recovered
   `project_setup(action="clone")` calls include the same ref.
7. `ProjectSetupTool.execute(action="clone", ref=...)`.

The agent-facing examples should move from:

```python
project_setup(action="clone", repository_url="...")
```

to:

```python
project_setup(action="clone", repository_url="...", ref="...")
```

when a ref exists. If no ref exists, examples should omit `ref` rather than pass
an empty value.

## Clone Tool Behavior

`project_setup` should accept a new `ref` parameter. The old `branch` parameter
stays supported and maps into `ref` only when `ref` is absent.

Recommended checkout sequence:

```bash
git clone <repository_url> <target_directory>
git -C <target_directory> fetch --tags --force
git -C <target_directory> checkout --detach <ref>
git -C <target_directory> rev-parse HEAD
```

For branch names, detached checkout is acceptable because SAG's setup workflow is
read-oriented. It avoids accidentally creating local branch state and gives the
same behavior for branches, tags, and commits.

The clone output and metadata should include:

- `repository_url`
- `target_directory`
- `clone_path`
- requested `ref`
- resolved commit hash
- compatibility `branch` field only when the legacy branch parameter was used

Command construction must shell-quote URL, directory, and ref. Refs such as
`rel/commons-cli-1.11.0` must work.

## Error Handling

If `git checkout --detach <ref>` fails:

- Return a failed `ToolResult`.
- Include the requested ref and repository URL in metadata.
- Explain that the handle was not found as a branch, tag, release tag, or commit.
- Suggest verifying the ref with commands such as `git ls-remote --heads --tags`
  or checking the repository releases/tags.
- Do not continue project type detection against the default branch.

If `git fetch --tags --force` fails after clone, fail the clone action. A tag or
release handle may depend on tag refs being present, and continuing would make
the requested version ambiguous.

## Places To Audit For "Latest" Assumptions

- CLI help and README examples for `sag project`.
- `SetupAgent._run_unified_setup` prompt text.
- `src/sag/config/prompts/react_engine.yaml` repository URL notices and clone
  examples.
- `ReactPromptBuilder` formatting of repository notices and stuck guidance.
- `ToolParameterNormalizer` repository URL injection/recovery.
- `ToolRecoveryHandler` clone recovery.
- `ProjectSetupTool` help text, schema, clone output, metadata, and examples.
- Tests that assume clone always uses the default branch.

Web UI labels such as "latest session" are unrelated to repository version
selection and should not be changed as part of this feature.

## Testing

Add focused unit tests before implementation:

- CLI passes `--ref` into `SetupAgent.setup_project`.
- Agent prompt contains ref guidance when a ref is present and omits it when
  absent.
- Parameter normalizer/recovery injects both repository URL and ref into clone
  calls.
- `ProjectSetupTool` clones first, fetches tags, checks out the requested ref,
  and records the resolved commit.
- Legacy `branch` still works by mapping to `ref` when `ref` is absent.
- Checkout failure stops setup and does not proceed to project analysis.

Add or update README examples after code behavior is verified.

## Acceptance Criteria

- `sag project <repo> --ref rel/commons-cli-1.11.0` results in the cloned
  workspace being checked out at that tag before analysis.
- `sag project <repo> --ref ae44dcd` supports a seven-character commit handle
  when Git can resolve it.
- `sag project <repo> --ref dubbo-3.2.19` works when that release has a matching
  Git tag.
- A bad ref fails loudly and does not set up the default branch by accident.
- All user-facing clone instructions use `ref` for version pinning.
