# SAG (Setup-Agent)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

SAG uses an LLM agent to configure a repository, provision its toolchain, and run builds and tests in Docker. It supports Maven, Gradle, and Python workflows, with reports and a local Web Workbench for inspecting what ran, what failed, and what evidence was collected.

## Quick start

You need Python 3.10+, `uv`, a running Docker daemon, and a configured model that supports native tool calling.

```bash
git clone https://github.com/Codegass/Setup-Agent.git
cd Setup-Agent
uv sync
cp .env.example .env
```

Edit `.env`: set `SAG_ACTION_MODEL`, `SAG_ACTION_PROVIDER`, and the matching provider API key. See [`.env.example`](.env.example) for endpoint and other configuration options.

Start with a small project:

```bash
uv run sag project https://github.com/apache/commons-cli.git --record
```

This creates the `sag-commons-cli` container and clones the repository into `/workspace/commons-cli`. Use `--ref <commit-or-tag>` to choose a revision; use a full commit SHA for reproducible comparisons. `--name` changes the container name, not the repository directory.

Open the Workbench to inspect workspaces, reports, evidence, and the agent's phase history:

```bash
uv run sag ui --port 8765
```

Visit [http://127.0.0.1:8765](http://127.0.0.1:8765). The frontend is bundled; Node.js is needed only for frontend development. Keep Docker running for live workspace inspection and terminals. `uv run sag ui --demo --port 8765` starts a preview with demo data.

## Configuration

Settings are read from environment variables and the repository's `.env` file.

| Setting | Purpose |
|---|---|
| `SAG_ACTION_MODEL` / `SAG_ACTION_PROVIDER` | Executor model and provider. The current loop uses one executor; retained `SAG_THINKING_*` fields do not create a second execution loop. |
| `SAG_ADVISOR_MODE` | `same-model` (default), another model name, or `off`. |
| `SAG_ADVISOR_REASONING_EFFORT` | Independent advisor reasoning setting; blank keeps the provider default. |
| `SAG_ADVISOR_CONTEXT_WINDOW` | Advisor deployment window, default `65536` tokens. Budgeting also respects known model limits and reserves space for output and safety. |
| `SAG_ADVISOR_CONTEXT_COMPRESSION` | `extractive` (default) or `semantic`. Semantic compression uses the executor to summarize background material when needed. |
| `SAG_ADVISOR_MAX_TOKENS` / `SAG_ADVISOR_PHASE_CAP` | Advisor response limit and consults per phase; defaults `2048` and `4`. |
| `SAG_MAX_ITERATIONS` | Run iteration limit, default `50`. |
| `SAG_MAX_WALL_CLOCK_SECONDS` | Whole-run time limit, default `7200` seconds. |
| `SAG_DISPATCH_SOFT_TIMEOUT_SECONDS` | Time before a long command returns as a background job, default `900` seconds. This is not a command kill timeout. |

Set the advisor window for the deployment you actually use. A blank override uses the local model catalog, with a disclosed fallback for unknown models. `SAG_ADVISOR_SUMMARY_CONTEXT_WINDOW` can separately bound the summarizer's window.

## Execution and architecture

A project setup follows five engine-owned phases:

```text
provision → analyze → build → test → report
```

The executor chooses tool calls within each phase. The engine handles dispatch, persisted evidence, phase transitions, and context rebuilding. Completion claims are checked against execution evidence; unresolved work remains visible in the final result. `sag run --task` provides a separate, lighter flow for follow-up work in an existing container.

| Tool | Role |
|---|---|
| `project` | Clone, provision toolchains, analyze the repository, and configure its runtime environment. |
| `build` | Route to Maven, Gradle, or Python using the project survey. Actions include `deps`, `compile`, `test`, `verify`, `package`, `install`, and `native`, subject to backend support. |
| `bash` / `files` | Run container commands and inspect or edit files. |
| `search` | Retrieve stored output, search files and background logs, or search the web. |
| `advisor` | Request guidance from a fresh-context reviewer. The advisor receives task, evidence, history, and tool definitions, but cannot call tools. |
| `phase` / `manage_context` | Manage setup phase signals or follow-up task context. |
| `report` | Render the result from the sealed evidence snapshot. |

The execution path preserves information the models need to recover:

- **Runtime evidence:** JVM build receipts record the command, exit status, report paths and hashes, and observed toolchain. Dispatch observations survive retries and background settlement; a later shell environment does not certify an earlier run's version.
- **Complete output access:** large output is persisted with a retrieval reference and a concrete file path. The agent can inspect it through `search`, `rg`, or `sed` instead of relying only on the visible excerpt. Storage failures are disclosed.
- **Advisor context:** task requirements and current evidence, including bounded failure diagnostics, receive priority. Background material can be extracted or summarized; oversized protected material is reviewed in ordered slices. Original sources and compression records remain available.
- **Report recovery:** a missing report can be submitted through the repair flow after evidence is sealed, without reopening build execution or changing the recorded result.

Project files live in the container; the host retains orchestration records, session logs, and evidence publications. No host project directory is mounted by default.

For implementation details, start with:

| Area | Source |
|---|---|
| CLI and container lifecycle | [`main.py`](src/sag/main.py), [`docker_orch/`](src/sag/docker_orch/) |
| Executor, phases, and recovery | [`react_engine.py`](src/sag/agent/react_engine.py), [`phase_gates.py`](src/sag/agent/phase_gates.py) |
| Receipts and final results | [`invocation_receipts.py`](src/sag/agent/invocation_receipts.py), [`verdict_finalizer.py`](src/sag/agent/verdict_finalizer.py) |
| Fixed tasks and CI comparison | [`acceptance_task.py`](src/sag/agent/acceptance_task.py), [`ci_comparison.py`](src/sag/agent/ci_comparison.py), [`metrics/`](src/sag/metrics/) |
| Advisor context | [`advisor_context.py`](src/sag/agent/advisor_context.py), [`advisor_compaction.py`](src/sag/agent/advisor_compaction.py) |
| Workbench | [`src/sag/web/`](src/sag/web/), [`webui/`](webui/) |

## Reading a result

Keep these measurements separate:

| Measurement | What it answers |
|---|---|
| Setup verdict (`success`, `partial`, `failed`) | Did the setup finish with the required execution evidence? The CLI exits `0` for `success`, otherwise `1`. |
| Required task completion | Did every frozen command finish successfully, in order, with any required runtime versions? Available when an acceptance task is supplied. |
| Build and test outcomes | What was built, and how many tests passed, failed, errored, or skipped? |
| Official CI comparison | Did this run reach the selected CI scope and outcomes for the same repository and commit? |

The CLI and report read the sealed result; the Workbench exposes its evidence and comparison data. A local `success` alone does not establish CI parity. With a fixed acceptance task, a failed or missing required command prevents complete task status even if earlier tests passed.

Test failures and errors are reported separately. The pass rate uses non-skipped outcomes: `passed / (passed + failed + errors)`. A value below 100% is never rounded into an unqualified 100%. `outcomes accounted` describes evidence accounting, not the fraction of tests that passed.

Static source declarations, class-file counts, and modules discovered on disk are diagnostics. They do not define the official build scope. `unavailable` means the evidence is missing or cannot support the comparison; it is not zero. A lower bound such as `≥N` is not a complete total. JaCoCo collection is optional via `--coverage`.

## Fixed tasks and official CI

CI comparison is integrated into live `sag project` runs. Supply the inputs before execution; finalization does not fetch CI or choose a different job after seeing the result.

- An **acceptance task** freezes the repository, full commit SHA, and ordered commands independently of the agent's plan. Steps can specify the launcher Java major and an exact Maven version. A task can be used without CI evidence.
- A **CI target** records the official job or matrix cell, commit, command, build scope, test outcomes, and source references. It supplies the comparison target, not proof that SAG executed anything.

After preparing `task.json` and `target.json` for the same revision:

```bash
uv run sag project https://github.com/apache/commons-cli.git \
  --acceptance-task-file task.json \
  --ci-target-file target.json \
  --record
```

The task selects its frozen commit automatically. If `--ref` is also supplied, it must equal that full SHA. See the [`AcceptanceTask`](src/sag/agent/acceptance_task.py) and [`TargetRecord`](src/sag/metrics/target_record.py) definitions for the input schemas. For a single Maven or Gradle command, `--acceptance-command` is also available and requires `--ci-target-file`.

Official evidence can be collected with [`d3_select_anchor.py`](scripts/d3_select_anchor.py) and [`d3_harvest_target.py`](scripts/d3_harvest_target.py). [`small_ci_bench.py`](scripts/small_ci_bench.py) provides the download, freeze, prepare, and run workflow for CI-anchored experiments:

```bash
PYTHONPATH=. uv run python scripts/small_ci_bench.py --help
```

Missing CI scope receives no scope score. A missing target, unmatched matrix cell, revision mismatch, or incomplete evidence is shown with a reason; it must not be counted as CI attainment. Missing scope never supplies a fallback `1/1` score. Lifecycle-command parity is disclosed separately from scope attainment.

For model or strategy ablations, keep the project commits, task definitions, CI cells, resource limits, and run budgets fixed. Compare task completion and concrete build/test counts before time and cost. Equal test totals alone do not prove identical test coverage, and changing the evaluator does not constitute a new successful run.

## Inspecting and managing runs

```bash
# List workspaces and enter a container
uv run sag list
uv run sag shell sag-commons-cli

# Inspect what the agent saw and did
uv run sag inspect sag-commons-cli --phase build
uv run sag inspect sag-commons-cli --phase build --iter 23

# Read a recorded session after the container is gone
uv run sag inspect sag-commons-cli --session logs/session_X --phase build
uv run sag inspect sag-commons-cli --session logs/session_X --turn 12
uv run sag trajectory logs/session_X
uv run sag result logs/session_X

# Piping either one into a JSON reader needs the machine-readable form
uv run sag trajectory logs/session_X --format json --detail full | jq .
uv run sag result logs/session_X --json | jq .

# Continue work, or remove the container and its filesystem
uv run sag run sag-commons-cli --task "Investigate the failing tests"
uv run sag remove sag-commons-cli
```

`--record` exports setup artifacts into the session directory under `logs/`. Container context and output files live under `/workspace/.setup_agent/`; reports use `setup-report-*.md`. Use `uv run sag --verbose project ...` for detailed console logging, or inspect the Workbench phase trace and full output references.

For a version mismatch, inspect the failing invocation's dispatch observations and receipt. A plain `java -version` in a later shell may use a different environment. For missing counts or CI scores, read the associated evidence reason before attributing the result to the model.

Run `uv run sag --help` or `uv run sag <command> --help` for the complete CLI options.

## Development

```bash
uv sync
PYTHONPATH=. uv run pytest

# Focused regression checks
PYTHONPATH=. uv run pytest tests/test_high_advisor_execution_repairs.py

# Frontend tests and bundled production assets
npm ci --prefix webui
npm test --prefix webui
npm run build --prefix webui
```

Bug reports and pull requests should include the revision, command, runtime observations, and relevant receipts or logs so failures can be reproduced.

## License

[MIT](LICENSE).

## Cite this work

BibTeX:

```bibtex
@inproceedings{Wei2026SAG,
  author    = {Wei, Chenhao and Zhao, Gengwu and Ye, Billy and Xiao, Lu and Li, Xinyi},
  title     = {Setup AGent (SAG): A Dual-Model LLM Agent for Autonomous End-to-End Java Project Configuration},
  booktitle = {Proceedings of the 48th International Conference on Software Engineering: New Ideas and Emerging Results (ICSE-NIER '26)},
  year      = {2026},
  address   = {Rio de Janeiro, Brazil},
  publisher = {ACM},
  isbn      = {979-8-4007-2425-1},
  month     = apr,
  doi       = {10.1145/3786582.3786818}
}
```
