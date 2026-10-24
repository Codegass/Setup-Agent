# SAG (Setup-Agent)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

SAG uses an LLM agent to configure a repository, provision its toolchain, and run builds and tests in Docker. It supports Maven, Gradle, and Python workflows. Every run leaves a record of what happened; the terminal, the written report, and the local Web Workbench all read that one record, so they cannot describe the same run differently.

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

While the run goes, the terminal prints one line per turn — what was asked, what came back, and how long it took — grouped by phase:

```text
▸ provision
  #1   project   clone apache/commons-c…  e171117 → /workspace/commons-cli  4.8s
  #2   advisor   consult                  advice delivered                  4.9s
  #3   project   provision openjdk 8      java 1.8.0_502                   40.0s
  #4   project   provision maven 3.9.9    ok                                6.9s
  #5   phase     done success             workspace_present · workspace /…  0.4s
✓ provision advanced
```

When the run ends it prints the result card (seven rows, described below) and writes the report to the session directory under `logs/`. Read either again later with `sag result` and `sag trajectory`.

Open the Workbench to inspect workspaces, turns, evidence, and reports:

```bash
uv run sag ui --port 8765
```

Visit [http://127.0.0.1:8765](http://127.0.0.1:8765). The frontend is bundled; Node.js is needed only for frontend development. Keep Docker running for live workspace inspection and terminals. `uv run sag ui --demo --port 8765` starts a preview with demo data.

## Configuration

Settings are read from environment variables and the repository's `.env` file.

| Setting | Purpose |
|---|---|
| `SAG_ACTION_MODEL` / `SAG_ACTION_PROVIDER` | The model that runs the setup and its provider. The loop uses one executing model; retained `SAG_THINKING_*` fields do not create a second execution loop. |
| `SAG_ADVISOR_MODE` | `same-model` (default), another model name, or `off`. |
| `SAG_ADVISOR_REASONING_EFFORT` | Independent advisor reasoning setting; blank keeps the provider default. |
| `SAG_ADVISOR_CONTEXT_WINDOW` | Advisor deployment window, default `65536` tokens. Budgeting also respects known model limits and reserves space for output and safety. |
| `SAG_ADVISOR_CONTEXT_SELECTION` | `all` (legacy default), `relevant`, `brief`, or `on-demand`. `brief` sends the task and observed facts; `on-demand` also allows bounded searches and paged reads of archived evidence. |
| `SAG_ADVISOR_TRIGGER_POLICY` | `phase-entry` (legacy default), `problems`, or `adaptive`. `adaptive` leaves routine consultations to the actor and adds a fallback for an existing repeated-no-progress signal or evidence conflict. It does not cancel planned work to demand advice. |
| `SAG_ADVISOR_CONTEXT_COMPRESSION` | `extractive` (default) or `semantic` for legacy context modes. `brief` and `on-demand` use programmatic excerpts/splits and make no extra summary-model calls. |
| `SAG_ADVISOR_ACTOR_NOTE` | Include the actor's optional question/hypothesis, default `true`. Machine observations are supplied independently; an actor note never becomes evidence. |
| `SAG_ADVISOR_MAX_READ_ROUNDS` / `SAG_ADVISOR_READ_MAX_CHARS` | In `on-demand` mode, allow up to `3` evidence reads and `12000` source characters per read, then one final advice response. Every provider request is recorded and charged. |
| `SAG_ADVISOR_MAX_TOKENS` / `SAG_ADVISOR_PHASE_CAP` | Advisor response limit and consults per phase; defaults `2048` and `4`. |
| `SAG_MAX_ITERATIONS` | Run iteration limit, default `50`. |
| `SAG_CODE_MODE` | Experimental opt-in `code` tool, default `false`. Requires a [prepared container runtime](src/sag/code_runtime/README.md); native tools remain available. |
| `SAG_MAX_LOGICAL_TOOL_CALLS` | Shared limit for native actions and code child calls, default `150`. |
| `SAG_MAX_WALL_CLOCK_SECONDS` | Whole-run time limit, default `7200` seconds. |
| `SAG_DISPATCH_SOFT_TIMEOUT_SECONDS` | Time before a long command returns as a background job, default `900` seconds. This is not a command kill timeout. |
| `SAG_PROJECT_USER` | An unprivileged account for the project's own commands (Maven, Gradle, pip, pytest, the bash tool), as official CI runs them; SAG's control work stays root. Unset keeps every command as root. The account must exist in the image; [`docker/project-user/`](docker/project-user/README.md) adds `sag` to the pinned benchmark image. |
| `SAG_PROVISION_FROM_TASK` | With a pinned task, the harness checks out the pinned commit and installs the pinned JDK and Maven before the first model turn, and closes the provision phase itself when all of that succeeded. Default `true`. |
| `SAG_REUSE_VERIFIED_TEST_PHASE` | At test-phase entry the harness grades a success claim through the real phase gate; when the gate accepts it green with the pinned task complete, the phase closes without a model turn. Default `true` (−27.6% tokens over 7 paired projects). |

Set the advisor window for the deployment you actually use. A blank override uses the local model catalog, with a disclosed fallback for unknown models. `SAG_ADVISOR_SUMMARY_CONTEXT_WINDOW` can separately bound the summarizer's window.

The actor may call `advisor(question, context)` with a short uncertainty and proposed next step; both arguments are optional. In `on-demand` mode the advisor receives a consultation-local catalog with source hashes. It can search literal text, read line ranges and continue long lines. These tools only inspect frozen recorded bytes. Missing live source or environment information must be obtained by the actor. Advice cannot execute commands, change requirements, or certify success; phases and the physical verifier remain authoritative.

## Execution and architecture

A project setup follows five engine-owned phases:

```text
provision → analyze → build → test → report
```

The model chooses tool calls within each phase. The engine handles dispatch, persisted evidence, phase transitions, and context rebuilding. A phase's "done" statement is checked against execution evidence before the run advances; unresolved work stays visible in the final result. `sag run --task` provides a separate, lighter flow for follow-up work in an existing container.

| Tool | Role |
|---|---|
| `project` | Clone, provision toolchains, analyze the repository, and configure its runtime environment. |
| `build` | Route to Maven, Gradle, or Python using the project survey. Actions include `deps`, `compile`, `test`, `verify`, `package`, `install`, and `native`, subject to backend support. |
| `bash` / `file_io` | Run container commands and read, write, or edit files. |
| `search` | Retrieve stored output, search files and background logs, or search the web. |
| `advisor` | Request a fresh review with an optional question/hypothesis. Context selection and automatic triggers are configurable; `on-demand` enables archived-evidence reads only. Advice has no execution or verdict authority. |
| `phase` | Signal that a phase is done, blocked, or worth a note. (`manage_context` serves the same purpose for `sag run` follow-up tasks.) |
| `report` | Write the phase's deliverable inside the container; the reader's report is produced on the host when the run ends. |

The execution path preserves information the models need to recover:

- **Runtime evidence:** JVM build receipts record the command, exit status, report paths and hashes, and observed toolchain. Dispatch observations survive retries and background settlement; a later shell environment does not certify an earlier run's version.
- **Complete output access:** large output is persisted with a retrieval reference and a concrete file path. The agent can inspect it through `search`, `rg`, or `sed` instead of relying only on the visible excerpt. Storage failures are disclosed.
- **Advisor context:** task requirements and current evidence, including bounded failure diagnostics, receive priority. Background material can be extracted or summarized; oversized protected material is reviewed in ordered slices. Original sources and compression records remain available.
- **Report recovery:** a missing report can be submitted through the repair flow after the evidence is closed, without reopening build execution or changing the recorded result.

Project files live in the container; the host retains orchestration records, session logs, and evidence publications. No host project directory is mounted by default.

### The run's record

Everything a run says about itself is derived from one append-only ledger, `control_events.jsonl`, which the engine writes on the host and mirrors into the container. It records every tool call the model asked for, every result, every phase decision and grading, and a record of each turn's own timing and token spend. Console logs are never a source: they show what the loop was doing, not what it recorded.

A `--record` session directory (`logs/session_<timestamp>_<id>/`) holds:

| Path | What it is |
|---|---|
| `control_events.jsonl` | The ledger above. |
| `.setup_agent/verdict.json` | The final judgment, written once when evidence closes. |
| `.setup_agent/` | Receipts, the toolchain overlay, the report metrics, and the other documents the run wrote in the container. |
| `run-pin.json` | The models used, the advisor's calls, and the run's configuration. |
| `token_usage.csv` | One row per model call, including the advisor's own calls. |
| `setup-report-<timestamp>.md` | The report, rendered on the host at run end (see below). |
| `contexts/`, `main.log`, `agent_execution.log` | Context traces and logs, for debugging only. |

Two read-only derivations sit on top of the ledger and every surface uses them:

- The **trajectory** (`sag trajectory`, `src/sag/trajectory/`) folds the ledger into turns: for each turn, the call, its result, the gate's grading, its recorded span, and its token bill — the model's own response and, on turns that consulted the advisor, the advisor's spend, kept apart.
- The **result card** (`sag result`, `src/sag/result_card/`) states the run as seven rows in a fixed order. The terminal block, the written report, and the Workbench's result band are three renderings of that one card.

For implementation details, start with:

| Area | Source |
|---|---|
| CLI, run-end sequence, and container lifecycle | [`main.py`](src/sag/main.py), [`docker_orch/`](src/sag/docker_orch/) |
| Executor, phases, and recovery | [`react_engine.py`](src/sag/agent/react_engine.py), [`phase_gates.py`](src/sag/agent/phase_gates.py) |
| The ledger | [`control_events.py`](src/sag/agent/control_events.py) |
| Receipts and the final judgment | [`invocation_receipts.py`](src/sag/agent/invocation_receipts.py), [`verdict_finalizer.py`](src/sag/agent/verdict_finalizer.py) |
| Trajectory, result card, report | [`trajectory/`](src/sag/trajectory/), [`result_card/`](src/sag/result_card/), [`report_document/`](src/sag/report_document/) |
| Terminal turn stream and result block | [`console/turn_stream.py`](src/sag/console/turn_stream.py), [`console/result_block.py`](src/sag/console/result_block.py) |
| Fixed tasks and CI comparison | [`acceptance_task.py`](src/sag/agent/acceptance_task.py), [`ci_comparison.py`](src/sag/agent/ci_comparison.py), [`metrics/`](src/sag/metrics/) |
| Advisor context | [`advisor_context.py`](src/sag/agent/advisor_context.py), [`advisor_review.py`](src/sag/agent/advisor_review.py), [`advisor_compaction.py`](src/sag/agent/advisor_compaction.py) |
| Workbench | [`src/sag/web/`](src/sag/web/), [`webui/`](webui/) |

## Reading a result

The result card has seven rows, always in this order: **CI command**, **Official CI**, **Not confirmed by SAG**, **Run**, **Summary**, **Coverage**, **Report**. The first four are measurements with a unit, a denominator and a source in the record; the Summary word is derived from them and labelled so. A row the run did not measure says so in words rather than showing a zero.

| Row | What it answers |
|---|---|
| CI command (`passed`, `failed`, `did not finish`) | Did the pinned CI command run to completion and exit 0, at the pinned commit, with the pinned Java and Maven versions, with the sources unchanged? Shows `k of n steps passed`, the first step that did not pass and its exit code. Without a pinned task the row says so and the build and tests below are what the run did. |
| Official CI | This run beside the official CI cell it was handed: modules built of CI's modules, tests run of CI's tests, and this run's failures and errors. When no CI cell was supplied, the row says why and shows this run's own counts. |
| Not confirmed by SAG | One plain sentence per check SAG could not confirm from its own records (the Java requirement decided, the built modules confirmed, the test outcome graded, the test reports tied to the command), or "SAG confirmed each of the above from its own records." These lines describe SAG's evidence, never the project's result. |
| Run | `ended normally`, `stopped at the time limit with N commands still running`, or `stopped after a command could not be stopped cleanly`; with turns, tool calls and wall-clock time. |
| Summary (`success`, `partial`, `failed`) | Derived from the rows above. With a pinned task: the CI command's result and nothing else (passed → success, failed → failed, did not finish → partial). Without one: the terminal build receipt's exit code, then the test outcome. The CLI exits `0` for `success`, otherwise `1`. |
| Coverage | Collected only with `--coverage`. |
| Report | Where the report was written. |

The same measurements are sealed in `verdict.json` (`ci_command`, `official_ci`, `sag_checks`, `run_control`, beside the word) and exposed as `dimensions` on the card API for the Workbench. A local `success` alone does not establish CI parity; the Official CI row carries the comparison, and only for the cell the run was handed.

Test failures and errors are reported separately. The pass rate uses non-skipped outcomes: `passed / (passed + failed + errors)`. A value below 100% is never rounded into an unqualified 100%. `outcomes accounted` describes evidence accounting, not the fraction of tests that passed.

Static source declarations, class-file counts, and modules discovered on disk are diagnostics. They do not define the official build scope. `unavailable` means the evidence is missing or cannot support the comparison; it is not zero. A lower bound such as `≥N` is not a complete total.

**Tokens are two numbers, never one.** The executing model's spend and the advisor's spend are recorded and shown apart — on the card (`sag result --json`: `tokens_in/out` and `advisor_tokens_in/out`), in the report's Tokens table, on the Workbench's Overview and per-turn strips. Where a total is shown it is labelled as the sum of the two rows above it.

### The report

When a run ends, the host renders `setup-report-<timestamp>.md` from the record and writes it into the session directory; it is also copied back into the container, best-effort, so `/workspace/setup-report-*.md` holds the same document. Its sections, in order:

1. Header — repository and commit, run id, version, and when the run ended.
2. **Result** — the seven-row card, CI command first and the derived Summary word last.
3. **What was set up** — the toolchain the run installed, with versions and paths, and any candidate it refused with the reason.
4. **The run** — one row per turn, grouped by phase: what was asked, what came back, how long it took.
5. **Tokens** — the model's and the advisor's calls and spend, apart.
6. **Evidence** — what the run cited, counted by kind, and the commands that open it.
7. **Evidence accounting** — how every test observation was accounted for.

A run with blockers lists them directly under the Result table. Every sentence traces to a field of the record; nothing the run did not measure is written.

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

The optional **requirements v2** analysis separates compilation, packaging, installation, tests, documentation and quality checks within a frozen task. Add `--requirements-file requirements.json` together with an explicit full `--ref`, acceptance task and CI target. Reviewed definitions and recorded worktree/runtime evidence are required; draft definitions cannot start a formal campaign. SAG and the portable recorder share the offline scorer. Its sidecar does not replace the existing sealed verdict or reinterpret historical scores. See the [recording and evaluation protocol](docs/benchmark-requirements-v2.md) for commands, intervention records, token-accounting limits and current readiness.

Primary comparisons must use **agent-owned execution evidence**. An external build after the agent exits measures environment readiness and cannot fill missing agent work. The campaign runner records the evidence location in `primary-evidence.json`. Native tools are retained: baseline hooks and a passive Maven process observer collect evidence without issuing build/test commands. The observer binds a pinned launch to the one open shell that names the step, also behind leading `NAME=value` prefixes, through a launcher named `mvn-<version>`, and for sequential pinned builds inside one shell call; Gradle and native-image steps still cannot bind. With that capture the baseline arms (Claude Code and OpenCode) ran the same 52 projects as SAG; the reports are under `output/benchmark-fifty-20261021/` (SAG) and `output/baselines-fifty-20261021/` (the comparison, with every non-passing cell classified by owner). Definitions whose requirement review is not complete are admitted with `--allow-review-required`: their task completion is scored and their requirement groups stay `unavailable`; [`scripts/freeze_benchmark_definitions.py`](scripts/freeze_benchmark_definitions.py) builds such definitions from a project's CI log and POM. Historical 60-run scores retain their original post-agent acceptance meaning. See the [execution-boundary qualification](docs/superpowers/reports/2026-09-26-native-execution-qualification.md).

The [research dataset guide](docs/benchmark-dataset.md) links the versioned Java project registry, its selection rules, and evidence archives. It distinguishes candidate collection, admission as an official CI reference, and readiness for an agent experiment.

## Inspecting and managing runs

```bash
# List workspaces and enter a container
uv run sag list
uv run sag shell sag-commons-cli

# The run as turns: a table to read, or the whole trajectory as JSON
uv run sag trajectory logs/session_X
uv run sag trajectory logs/session_X --format json --detail full | jq .
uv run sag trajectory logs/session_X --follow          # tail a running session

# The result card, from a container or a recorded session
uv run sag result sag-commons-cli
uv run sag result logs/session_X --json | jq .

# One turn end to end, or a phase, from a container or a recorded session
uv run sag inspect sag-commons-cli --phase build
uv run sag inspect sag-commons-cli --session logs/session_X --turn 12

# Continue work, or remove the container and its filesystem
uv run sag run sag-commons-cli --task "Investigate the failing tests"
uv run sag remove sag-commons-cli
```

`--record` exports the container's `/workspace/.setup_agent/` documents and the report into the session directory under `logs/`, beside the ledger the host wrote during the run. On the console, the turn stream owns the screen and the log prints only warnings; `uv run sag --verbose project ...` opens the full debug log on the console as well.

The Workbench shows the same record per workspace: the result band, then tabs for the Overview (time and tokens, anything that needs attention), Turns (every turn with its call, result, grading and bill, under three strips — model tokens, advisor tokens, and duration per turn), Tests, Build, Official CI, Evidence, Logs, and the Report.

For a version mismatch, inspect the failing invocation's dispatch observations and receipt. A plain `java -version` in a later shell may use a different environment. For missing counts or CI scores, read the associated evidence reason before attributing the result to the model.

Run `uv run sag --help` or `uv run sag <command> --help` for the complete CLI options.

## Development

```bash
uv sync
uv run pytest

# Frontend tests and bundled production assets
npm ci --prefix webui
npm test --prefix webui
npm run build --prefix webui

# Everything at once: vitest, the TypeScript build, the bundle, and pytest
uv run python scripts/ship_gate.py
```

The bundled frontend under `src/sag/web/static/` is committed; rebuild it in the same commit as the source that changed it. Archived runs under `tests/fixtures/trajectory/` and `tests/fixtures/report_document/` replay real ledgers end to end, so a change to any surface is checked against what a run actually recorded, not only against hand-written fixtures.

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
