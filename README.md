# SAG (Setup-Agent)
🤖 **An LLM-Powered Engine for Automated Project Setup & Configuration** 🤖

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> ICSE-NIER ’26 paper: [Setup AGent (SAG)](https://doi.org/10.1145/3786582.3786818) — Wei et al. The paper will be available soon this April.



**SAG (Setup-Agent)** is an advanced AI agent designed to fully automate the initial setup, configuration, and ongoing tasks for any software project. It operates within an isolated Docker environment, intelligently interacting with project files, shell commands, and web resources to transform hours—or even days—of manual setup into a process that takes just a few minutes.

## 🔦 Highlights
- **Container-native execution** powered by `src/sag/docker_orch/`, ensuring each project is built inside an isolated Docker workspace; project files and agent-readable context live in the container. The host stores the control ledger, session logs, launch queue, and result mirrors.
- **Engine-owned phase machine** (`src/sag/agent/phase_machine.py`): a `sag project` run advances through a fixed sequence — provision → analyze → build → test → report — with a clean context window per phase, evidence-gated transitions, and an honest `blocked` escape valve that degrades the verdict instead of looping.
- **Single-executor tool-calling loop** in `src/sag/agent/react_engine.py` with live token telemetry (`src/sag/agent/token_tracker.py`); long builds run **detached** (dispatch-and-poll) instead of being killed by a per-command timeout, bounded by a global wall-clock cap.
- **One verdict everywhere** via the kernel in `src/sag/verdict.py`: the report header, CLI banner, and exit code derive from a single policy and can no longer disagree. The verdict word answers one question — did SAG's setup run to completion with evidence — and the project's own test failures are reported beside it, never folded into it.
- **Numbers that mean what they say**: test headlines state their accounting population (`987/987 test outcomes accounted`); module and class-file scans are diagnostics, never build-scope scores. A number SAG cannot prove is shown as `unavailable — <reason>` or as a lower bound (`≥2,048`), never as a percentage.
- **Intent-driven tools** (`src/sag/tools/`): `bash`, `files`, `build` (Maven, Gradle, and Python behind one verb set), `project` (clone/provision/analyze/env), `search` (refs/files/job-logs/web), `report`, `advisor`, and one lifecycle tool (`phase` for setups, `manage_context` for tasks), all returning a uniform result envelope where large output becomes a retrievable reference ("links, not dumps").
- **Evidence-based validation**: every build/test dispatch leaves a **receipt** (what ran, which report files it wrote, their digests, and the totals those reports declared), the run seals a snapshot from those receipts, and a per-iteration **context journal** is inspectable with `sag inspect` and the Workbench context trace.
- **An external yardstick**: a run can be measured against the project's own CI on the same commit (`src/sag/metrics/`), so "success" can mean "reached what the project's CI reaches" rather than "reached what the agent planned". Today this runs offline from recorded evidence; see *Measuring against the project's CI*.

---

## 📖 Philosophy: Solving the "Getting Started" Problem

In software development, configuring a new project—especially a large open-source one—is often a tedious, time-consuming, and error-prone task. Developers must read extensive documentation, resolve dependency conflicts, and understand the project's structure before writing a single line of effective code.

**SAG's core mission is to solve this problem.** It aims to be an intelligent "Project Initialization Specialist" by adhering to these core principles:

- **Container Workspaces**: Project commands run inside Docker containers. Orchestration and durable control records run on the host; containers provide separate development environments, with no host workspace mounted by default.
- **Phase-Structured Execution**: A project setup runs as an engine-owned sequence of phases (provision → analyze → build → test → report). The agent works freely inside a phase; the engine validates real evidence before advancing, so the run cannot drift or quietly give up.
- **Evidence Over Assertion**: Build and test verdicts come from the receipts of the commands SAG actually ran — the report files a dispatch wrote and the totals those files declare — routed through a single verdict policy so the CLI, report, and exit code always agree. A model's claim that something worked never counts as evidence; a count SAG cannot back is shown as unavailable rather than guessed.
- **Executor and Advisor**: One model chooses and calls tools. A separate advisor consult reviews the phase transcript and evidence when needed; it gives guidance and cannot execute tools. The advisor can use the executor model, another configured model, or be disabled for an ablation.

## ✨ Core Concepts

### 1. Native Tool-Calling Engine

SAG uses one executor model call per iteration. The model receives the conversation, chooses native tool calls, and observes their results before its next decision. The engine preserves tool-call identities and owns dispatch, persistence, and phase transitions.

The `advisor()` tool asks a fresh-context reviewer for strategic guidance. The engine supplies the phase transcript and evidence; the reviewer cannot call tools. `SAG_ADVISOR_MODE` selects the same model, an explicit model, or `off` for an ablation. This is an optional consult within the executor loop.

For `sag project`, this loop runs inside the engine-owned phase machine below. For free-form `sag run --task` work, it runs against a model-managed task list.

### 2. Phase Machine & Context Journal

A project setup is the same shape every time, so the **engine** owns that shape instead of asking the model to manage it:

- **Trunk → Phase → Iteration.** The trunk holds the goal and the phase record; each phase (provision → analyze → build → test → report) is a context branch with its own history. The model's entire lifecycle surface is one tool — `phase` with `done` / `blocked` / `note`.
- **Clean window per phase.** When the engine advances, it rebuilds the context window from scratch: goal digest + prior phases' key results + the new phase's objective, and nothing else. Each phase gets the full window for its own work.
- **Evidence-gated, never trapped.** A `done` claim is validated against physical evidence (artifacts, test reports). A phase that genuinely cannot finish is recorded with `blocked`, which is always accepted and degrades the run verdict honestly instead of looping to the iteration cap.
- **Iteration floors, not quotas.** No phase has a fixed budget; a phase is only cut short when continuing would starve the minimum needs of later phases, so a hard build can use the iterations an easy clone saved while the run still always reaches the report phase.
- **Context journal.** Every iteration records what composed its prompt (segments, token counts, deltas, the window intro and the attempt-ledger compaction) to an in-container journal — replayable with `sag inspect` or the Workbench context trace.

`sag run --task` keeps a lighter model-managed flow with the `manage_context` tool for arbitrary follow-up work.

### 3. Focused Tool Set

A single `bash` tool could handle every interaction, but it would force the agent to manage immense complexity — command syntax, output parsing, toolchain selection — which is inefficient and error-prone. SAG gives the model a small set of **intent-driven** tools instead, each returning the same result envelope (`verdict` / `facts` / `output` / `suggestions` / `refs`):

- **`bash`** — the granular fallback for anything without a specialized tool; long-running commands dispatch detached and hand back a pollable log.
- **`files`** — safe, container-aware file read/write/list.
- **`build`** — one tool over Maven, Gradle, and Python projects behind verb actions (`deps` / `compile` / `test` / `package`). The build system comes from the survey `analyze` wrote (a mixed repository is not re-guessed from marker files), the registered toolchain is resolved for it, and every dispatch leaves a receipt. The model never hand-rolls `mvn`/`gradlew` against a stale PATH.
- **`project`** — `clone` / `provision` / `analyze` / `env` for repository and toolchain setup. `analyze` publishes the build-requirements survey that `build` routes on.
- **`search`** — one retrieval tool over stored output refs, container files, background-job logs, and the web. Large tool output is stored and referenced rather than dumped into the window ("links, not dumps").
- **`report`** — renders the final setup report from the sealed evidence snapshot.
- **`advisor`** — a zero-argument consult the model can call when stuck; the engine answers it (configurable, and ablatable without changing what the model sees).
- **`phase`** (setups) / **`manage_context`** (tasks) — the one lifecycle verb, described above.

The implementation delegates (Maven/Gradle/Python/system/env/analyzer runners) live under `src/sag/tools/internal/` and are never exposed to the model directly. This lets the agent focus on *what* it needs, not *how* to drive each command.

## 🏗️ System Architecture

SAG is composed of several core components:

1. **CLI (`src/sag/main.py`)**: Entry point for `project`, `run`, `list`, `shell`, `remove`, `ui`, `inspect`, and `trajectory`, with optional artifact recording for post-run inspection.
2. **Configuration Layer (`src/sag/config/`)**: Loads `.env` settings, provider credentials, model presets, and run bounds (iteration cap, wall-clock cap, dispatch windows), and wires logging streams.
3. **Setup Agent & Contexts (`src/sag/agent/agent.py`, `src/sag/agent/context_manager.py`)**: Orchestrates the workflow, persists trunk/phase contexts inside the container workspace, and initializes the mode-aware tool set.
4. **Phase Machine (`src/sag/agent/phase_machine.py`, `src/sag/agent/phase_gates.py`, `src/sag/agent/attempt_ledger.py`, `src/sag/agent/context_journal.py`)**: Drives the provision → analyze → build → test → report sequence, gates each transition on physical evidence, compacts long phases, and journals every iteration's context window.
5. **ReAct Engine & State Evaluation (`src/sag/agent/react_engine.py`, `src/sag/agent/agent_state_evaluator.py`)**: Single-executor native tool-calling loop with advisor consults, phase-signal handling, clean-window resets, dispatch-and-poll for long builds, a global wall-clock cap, and live token telemetry.
6. **Receipts, Verdict & Measurement (`src/sag/agent/invocation_receipts.py`, `src/sag/agent/verdict_finalizer.py`, `src/sag/verdict_rates.py`, `src/sag/verdict.py`)**: Every build/test dispatch is sealed into a receipt (command, exit, report files written with digests, declared totals); the finalizer folds receipts and the physical build check into one sealed snapshot; the rates layer turns that snapshot into the headline counts; the verdict kernel feeds the report, CLI, and exit code.
7. **Tool Set (`src/sag/tools/`)**: Model-facing tools (`bash`, `files`, `build`, `project`, `search`, `report`, `advisor`, `phase`/`manage_context`) over delegates in `src/sag/tools/internal/`.
8. **Reporting (`src/sag/tools/report_tool.py`, `src/sag/tools/report_metrics.py`)**: Renders markdown setup reports and the evidence-layer metrics (claimed test executions, verified per-test results, diagnostics) that the report and the Web UI both read.
9. **Success certificates & CI targets (`src/sag/agent/java_success_certificates.py`, `src/sag/metrics/`)**: A typed certificate of what a run proved (scope, build, test execution, test outcome, integrity), and the external-target layer that harvests a project's own CI results for the same commit and grades the run against them. Offline today (see *Measuring against the project's CI*).
10. **Docker Orchestrator (`src/sag/docker_orch/orch.py`)**: Container lifecycle, project files in the container filesystem, detached dispatch, and shell connectivity for every project.
11. **Web Workbench (`src/sag/web/`, `webui/`)**: A FastAPI + React dashboard for managing workspaces, reading reports/evidence, and inspecting the phase timeline and context journal.

## 🧠 The Tool Set
The model-facing tools, each returning the uniform envelope (`verdict` / `facts` / `output` / `suggestions` / `refs`):

- **`bash`** (`src/sag/tools/bash.py`): container-aware shell for anything without a specialized tool; long-running commands dispatch detached and return a pollable in-container log instead of being hard-killed.
- **`files`** (`src/sag/tools/file_io.py`): safe file read/write/list inside the container.
- **`build`** (`src/sag/tools/build/`): one tool over Maven, Gradle, and Python — `build(action='deps'|'compile'|'test'|'package')`. Routes on the surveyed build system, resolves the registered toolchain (correct Maven/JDK), and seals a receipt per dispatch, with a backend per ecosystem.
- **`project`** (`src/sag/tools/project_tool.py`): `clone` / `provision` / `analyze` / `env` — repository cloning, JDK/Maven provisioning, project analysis, and runtime-overlay registration.
- **`search`** (`src/sag/tools/search_tool.py`): one retrieval tool over stored output refs, container files, background-job logs, and the web.
- **`report`** (`src/sag/tools/report_tool.py`): renders `setup-report-*.md` from the sealed evidence snapshot.
- **`advisor`** (`src/sag/agent/advisor.py`): one delegated consult, always registered so the advisor mode can be switched off without changing the tool surface.
- **`phase`** / **`manage_context`** (`src/sag/tools/phase_tool.py`, `src/sag/tools/context_tool.py`): the lifecycle verb for setups and tasks respectively.

Delegates (`maven_tool`, `gradle_tool`, `python_tool`, `project_setup_tool`, `project_analyzer`, `system_tool`, `env_tool`, `output_search_tool`, `web_search`, `toolchain_manager`) live under `src/sag/tools/internal/`.

## ✅ Validation & Observability
- **Receipts** (`src/sag/agent/invocation_receipts.py`): every build/test dispatch is sealed with its command line, exit code, the report files it wrote (paths and digests, so a stale report from an earlier run can never be counted), and for Gradle the totals each report declared. Counts that reach the report come only from receipts.
- **Physical Validator** (`src/sag/agent/physical_validator.py`): checks that a claimed build left real artifacts and records the observed modules. Disk-scan counts describe the result; the project's CI defines the build-scope comparison.
- **Verdict Kernel & Rates** (`src/sag/verdict.py`, `src/sag/verdict_rates.py`, `src/sag/agent/verdict_finalizer.py`): one sealed snapshot per run; the verdict word (`success` / `partial` / `failed`) and the headline counts are derived from it and consumed by the report header, CLI banner, exit code, and Web UI so they can never diverge.
- **Context Journal** (`src/sag/agent/context_journal.py`): records each iteration's window composition (segments, token counts, deltas, intro/ledger text) to `/workspace/.setup_agent/contexts/journal/` — replayable via `sag inspect`.
- **Test Case Catalog** (`src/sag/testcases/catalog.py`): normalizes runtime results, parameterized expansions, and Groovy/Kotlin discovery. Static test declarations are kept as a diagnostic only; they are never the denominator of a rate.
- **Output Storage & Token Tracker** (`src/sag/agent/output_storage.py`, `src/sag/agent/token_tracker.py`): persist verbose tool output under `.setup_agent/` (referenced via `search`), and capture per-step token usage for cost analysis.
- **Trajectory** (`sag trajectory`): derives a machine-readable trajectory of a recorded session from the authoritative control ledger only — console logs are never read.

## 📏 Reading a Result

A setup answers three different questions, and SAG keeps them apart instead of blending them into one word:

| Question | Where it is answered | Example |
|---|---|---|
| Did SAG actually run the build and tests and leave complete evidence? | verdict word, exit code | `SUCCESS`, exit 0 |
| What did the project's build and tests produce? | the Build / Tests lines, the report's *Test Outcome Accounting* table | `Tests: … failed 0 · errors 0` |
| Did the run reach what the project's own CI reaches on this commit? | the CI comparison (offline today) | `Test attainment 987/987 · MET` |

When a setup finishes, the CLI prints the verdict and three headline lines:

```text
🎯 SETUP COMPLETED: ✅ SUCCESS
Build: SUCCESS · modules built 1 of 1 declared on disk (diagnostic) · class files 56 (diagnostic) · production Java sources 36 (diagnostic)
Tests: EXECUTED · outcomes accounted 987/987 · non-skipped passed 926/926 · skipped 61 · failed 0 · errors 0 · static declarations 468 (diagnostic)
Coverage: unavailable — not collected
```

How to read them:

- **The Build line states what SAG built; it does not grade it.** `modules built 21 of 26 declared on disk` is a disclosure, never a percentage or a verdict input. Builds select profiles, exclude modules, and require specific toolchains; the project's own CI command defines the comparison. Whether 21 was enough is answered by the CI comparison below. Counts taken from the build command's module results are labeled `in the build run` instead of `declared on disk`.
- **`SUCCESS` on the Build line means the build ran and left real artifacts.** `FAILED` means it did not. The setup verdict adds test execution: `success` means the build ran and tests completed with every outcome accounted for. An interrupted run is `partial`.
- **`Tests: EXECUTED · failed 12`** reports the project's result. The CI comparison distinguishes failures CI also has from tests CI passes and SAG fails. Red outcomes alone do not change the local execution status. The exit code is `0` for a `success` setup verdict and `1` otherwise.
- **`outcomes accounted 987/987`** checks that every recorded execution has one outcome. **`non-skipped passed 926/926`** describes those outcomes, with skips reported separately.
- **Static declarations** and class-file counts are diagnostic. Parameterized, inherited, and dynamic tests make source scans unreliable as a test denominator; one Java source may produce zero, one, or many class files.
- **`unavailable`** always comes with a reason, and **`≥N`** means a lower bound (for example when a huge test run's per-test list was kept only in part). SAG never turns a partial count into `100%`.

The full setup report (`setup-report-*.md`) carries the same numbers with their sources, and the Web UI's overview shows them as *Build*, *Tests*, and *Coverage* tiles plus a per-workspace breakdown.

## 🧪 Measuring against the project's CI

A successful local setup means the build and test execution completed with the required evidence. To know whether the run reached what the project itself considers a working checkout, SAG can compare a run against the project's official CI on the **same commit**:

1. `scripts/d3_select_anchor.py` picks a commit that has completed CI runs, and `scripts/d3_harvest_target.py` collects that commit's CI evidence into a *target record* — per CI job: build conclusion, the tests it ran (with retries folded to their final outcome), the modules it built, the command it ran, and whether `continue-on-error` masked a failure. Modules come first from the job log (completed build-task participants, including cached or empty projects), otherwise from the reactor selected by the CI command on that commit (exact for the declared scope), otherwise from JUnit report paths (test-bearing modules only, disclosed as a lower bound).
2. A run's evidence is turned into a *success certificate* (`scripts/evaluate_java_success_certificates.py`): what scope it proved, whether the build and test execution completed, whether the outcome was clean, and whether the evidence chain is intact.
3. `sag.metrics.attainment` grades certificate against target: `met` (reached the CI's build and test universe with no new failures), `exceeded`, `partial`, `not_met` (new failures beyond what CI shows), or `invalid` (authority is missing or the repository/revision differs, so no fraction is stated). Missing CI modules or test counts leave the overall score unavailable; any measured single axis stays visible. Missing evidence cannot supply a 1/1 scope score or upgrade a run to `met`.

Beside the score, the comparison states lifecycle parity — `CI: mvn -V test · SAG: mvn compile; mvn test · parity: equivalent` — and names phases or plugin goals SAG did not reach. Lifecycle parity never changes the score.

A project whose CI cannot be harvested gets no scope judgment: the local verdict covers execution and the disk reactor count remains a disclosure.

This layer is implemented and tested but not yet wired into live runs; the measurement standard it follows is documented in `docs/superpowers/specs/2026-08-27-sag-ms-1-measurement-standard.md`, and the design rationale in `docs/superpowers/specs/2026-08-27-sag-metrics-v2-proposal.md`.

## 🧭 End-to-End Flow

```mermaid
flowchart TD
    CLI["CLI (`src/sag/main.py`)<br/>`sag project <url>`"]
    Config["Load configuration & session logging<br/>`src/sag/config/`"]
    Docker["Docker orchestrator provisions container workspace<br/>`src/sag/docker_orch/orch.py`"]
    AgentInit["SetupAgent + PhaseMachine constructed<br/>`src/sag/agent/agent.py`, `phase_machine.py`"]
    PhaseStart["Enter next phase<br/>Engine rebuilds a clean window:<br/>goal digest + prior key results + phase objective"]

    subgraph PhaseWork[Work inside one phase]
        Act["Executor makes native tool calls<br/>bash · files · build · project · search · report · advisor"]
        Dispatch["Long build? dispatch detached,<br/>poll the in-container log"]
        Observe["OBSERVATION: envelope (verdict/facts/refs)<br/>large output stored, referenced via `search`"]
        Journal["Context journal records the iteration window<br/>compaction → attempt ledger when long"]
    end

    Claim{"model: phase(done | blocked | note)"}
    Gate["Evidence gate (`phase_gates.py`)<br/>artifacts / test reports present?"]
    Advance["Mark phase done/blocked in trunk<br/>advance: provision→analyze→build→test→report"]
    Verdict["Sealed snapshot → verdict kernel<br/>failed < partial < success · Build / Tests / Coverage lines"]
    Report["`report` renders setup-report-*.md<br/>from the sealed snapshot"]
    Completion["CLI banner + exit code from the same verdict<br/>optional `--record` artifact export"]

    CLI --> Config --> Docker --> AgentInit --> PhaseStart --> Act
    Act --> Dispatch --> Observe --> Journal --> Claim
    Claim -- "note / keep working" --> Act
    Claim -- "done" --> Gate
    Gate -- "evidence missing" --> Act
    Gate -- "evidence present" --> Advance
    Claim -- "blocked (honest)" --> Advance
    Advance -- "more phases" --> PhaseStart
    Advance -- "report phase done" --> Verdict --> Report --> Completion
```

The CLI bootstraps an isolated Docker workspace, and the **phase machine** drives a fixed provision → analyze → build → test → report sequence. The model works freely inside each phase (any tool, any order) and signals with one `phase` verb; the engine validates `done` against physical evidence, accepts `blocked` honestly, rebuilds a clean window for the next phase, and journals every iteration. A single verdict kernel then drives the report, CLI banner, and exit code. (`sag run --task` uses a lighter model-managed loop for arbitrary follow-up work.)

## 🚀 Quick Start

### 1. Prerequisites
- [Docker](https://www.docker.com/)
- [Python 3.10+](https://www.python.org/)
- [uv](https://github.com/astral-sh/uv) (The recommended Python package manager)

### 2. Installation & Configuration

```bash
# (Optional) Install uv globally if it is not already available
pip install uv

# 1. Clone the repository
git clone https://github.com/Codegass/Setup-Agent.git
cd Setup-Agent

# 2. Install dependencies with uv (this will also create a virtual environment)
uv sync

# 3. Create and edit your configuration file
cp .env.example .env
nano .env  # Fill in your API keys and other settings
```

### 3. Create or Attach to a Workspace

```bash
# Start setting up a new project
uv run sag project https://github.com/fastapi/fastapi.git

# Start from a specific branch, tag, release tag, short commit, or full commit
uv run sag project https://github.com/apache/commons-cli.git --ref rel/commons-cli-1.11.0

# List all managed projects and their status
uv run sag list
```

Use the Git repository URL with `--ref` for versioned setup targets. For example,
use `https://github.com/apache/dubbo.git --ref dubbo-3.2.19` rather than a
GitHub `/releases` page URL.

SAG creates Docker containers named `sag-<project>` by default. The Web UI reads
those managed containers and shows their latest setup state, sessions, evidence,
reports, and follow-up task entry points.

### 4. Start the Web UI

```bash
# Start the local SAG Workbench on a stable browser URL
uv run sag ui --port 8765

# Then open:
# http://127.0.0.1:8765
```

The Web UI is the recommended way to inspect SAG-managed workspaces after setup.
It provides:

- A dashboard of all SAG Docker workspaces and their current container state,
  with one-click workspace deletion (including stopped or already-gone containers).
- Workspace detail pages with current status, latest evidence, reports, build/test
  summaries, and changed files.
- A **Phases** tab: a context trace of the run — trunk goal → phases → iterations
  → tool actions, with thoughts, observations, output refs, and the per-iteration
  context journal.
- Session detail pages for setup results and later task runs.
- A workspace terminal tab for running an interactive shell inside a running SAG
  container.
- A task form for assigning follow-up work to an existing workspace.

Useful launch options:

```bash
# Use an automatically assigned local port; uvicorn prints the selected URL
uv run sag ui

# Bind to a specific host and port
uv run sag ui --host 127.0.0.1 --port 8765

# Preview the UI with deterministic demo data instead of Docker discovery
uv run sag ui --demo --port 8765
```

Keep Docker running when using live data. The terminal tab only connects to
workspaces whose containers are currently running.
Terminal connections must come from the same Workbench origin. Loopback hosts
are allowed by default; an explicit `--host` adds that exact hostname or address.
Wildcard binds do not grant terminal access through arbitrary hostnames.

### 5. Common CLI Operations

```bash

# Run a new task on an existing project
uv run sag run sag-fastapi --task "add a new endpoint to handle /healthz"

# Access the project container's shell
uv run sag shell sag-fastapi

# Remove a project and its container filesystem
uv run sag remove sag-fastapi
```

### 6. Debugging & Troubleshooting

When a setup fails or you want to understand what the agent did, SAG provides several debugging tools:

#### Enable Verbose Mode & Recording

```bash
# Run with verbose output for detailed logs
uv run sag --verbose project https://github.com/example/repo.git

# Save artifacts locally for post-run inspection
uv run sag project https://github.com/example/repo.git --record

# Combine both for maximum visibility
uv run sag --verbose project https://github.com/example/repo.git --record
```

#### Inspect the Run

The easiest way to see what the agent did is `sag inspect` (reads the live
container or a `--record` session):

```bash
# List the phases and their iteration spans
sag inspect sag-<project>

# Replay one phase, then drill into a specific iteration's context window
sag inspect sag-<project> --phase build
sag inspect sag-<project> --phase build --iter 23
```

The underlying context lives inside the container under `/workspace/.setup_agent/`:

```bash
# List all context files (trunk + phase_* for setups, task_* for run --task)
docker exec sag-<project> ls -la /workspace/.setup_agent/contexts/

# Read the trunk context (goal, phase records, overall status)
docker exec sag-<project> cat /workspace/.setup_agent/contexts/trunk_*.json | python3 -m json.tool

# A specific phase's branch history (e.g. the build phase)
docker exec sag-<project> cat /workspace/.setup_agent/contexts/phase_build.json | python3 -m json.tool

# Per-iteration context journals
docker exec sag-<project> ls /workspace/.setup_agent/contexts/journal/

# Search for errors across all context files
docker exec sag-<project> grep -r "error\|failed\|ERROR" /workspace/.setup_agent/contexts/
```

#### Review Setup Reports

```bash
# List generated reports
docker exec sag-<project> ls -la /workspace/setup-report-*.md

# Read the setup report
docker exec sag-<project> cat /workspace/setup-report-*.md
```

#### Check Session Logs (with --record)

When using `--record`, artifacts are saved to local session logs:

```bash
# Find the session log directory
ls -la logs/session_*/

# Review the main session log
cat logs/session_<timestamp>/main.log

# Check for specific error patterns
grep -r "BUILD FAILURE\|compilation error" logs/session_<timestamp>/
```

#### Common Debugging Scenarios

| Scenario | What to Check |
|---|---|
| Build failed | Read the *Build* line and the reason under it in `setup-report-*.md`; then `sag inspect sag-<project> --phase build` for the iterations |
| A count shows `unavailable` or `≥N` | The reason is printed beside it in the report; it means SAG could not prove the full number, not that the number is zero |
| Java version mismatch | `docker exec sag-<project> java -version` and check for `RequireJavaVersion` in logs |
| Missing dependencies | `docker exec sag-<project> which mvn npm gradle` |
| Empty tool outputs | Check if stderr is captured in context files |
| Agent stuck in loop | `sag inspect sag-<project> --phase <phase>` to replay the iterations |

#### Interactive Debugging

```bash
# Connect to the container shell for manual investigation
uv run sag shell sag-<project>

# Inside the container, you can:
# - Run build commands manually
# - Check environment variables
# - Inspect project files
# - Review logs in /workspace/.setup_agent/
```

## 🛠️ CLI Command Reference

SAG provides a clean and powerful set of CLI commands.

### Commands

| Command | Description | Example |
|---|---|---|
| `sag project <url>` | Initializes the setup for a new project from a Git repository URL. | `sag project https://github.com/pallets/flask.git` |
| `sag list` | Lists all projects managed by SAG, showing their container name, status, and last comment. | `sag list` |
| `sag run <name>` | Runs a specified task on an existing project. | `sag run sag-flask --task "add unit tests for the application factory"` |
| `sag shell <name>` | Connects to an interactive shell inside the specified project's container. | `sag shell sag-flask` |
| `sag ui` | Starts the local SAG Workbench web UI. | `sag ui --port 8765` |
| `sag remove <name>` | Permanently deletes the project container and its filesystem; also cleans up a legacy volume if present. | `sag remove sag-flask --force` |
| `sag inspect <name>` | Replays a run's phase timeline and per-iteration context windows from the container or a recorded session. | `sag inspect sag-flask --phase build` |
| `sag trajectory <session_dir>` | Derives a machine-readable trajectory (JSON) of a recorded session from its authoritative ledger; console logs are never read. | `sag trajectory logs/session_X --detail full` |
| `sag version` | Displays SAG's version information. | `sag version` |
| `sag --help` | Shows the help message. | `sag --help` |

### Global Options

| Option | Description |
|---|---|
| `--log-level [DEBUG\|INFO\|WARNING\|ERROR]` | Overrides the log level set in the `.env` file. |
| `--log-file <path>` | Specifies a custom path for the log file. |
| `--verbose` | Enable verbose debugging output with detailed logs. |
| `--ui` | Enable the Rich live progress display for supported `project` and `run` executions. Cannot be combined with `--verbose`. |

### Command-Specific Options

#### `sag project <url>`

| Option | Description |
|---|---|
| `--name <name>` | Override the Docker container name (default: extracted from URL). **Note:** This only affects the Docker container naming (`sag-<name>`), not the project directory name. The cloned repository will always use the directory name from the URL. |
| `--goal <goal>` | Custom setup goal (default: auto-generated based on project name). |
| `--ref <handle>` | Set up a specific Git ref, such as a branch, tag, release tag, short commit, or full commit hash. SAG clones the repository, checks out this ref, and records the resolved commit. |
| `--record` | Save setup artifacts (contexts, reports) to local session logs for debugging and auditing. |
| `--coverage` | After the verdict is sealed, run an isolated JaCoCo coverage pass (best-effort; fills the *Coverage* line). |
| `--ui` | Enable the Rich live progress display for this run. |

**Example with a version handle and custom Docker name:**
```bash
# Clone commons-cli at a release tag but name the Docker container "cli-test"
sag project https://github.com/apache/commons-cli.git --ref rel/commons-cli-1.11.0 --name cli-test

# Result:
# - Docker container: sag-cli-test
# - Project directory: /workspace/commons-cli (always matches git repo name)
# - Git checkout: rel/commons-cli-1.11.0, with resolved commit recorded in metadata
# - To run tasks later: sag run sag-cli-test --task "..."
```

#### `sag run <name>`

| Option | Description |
|---|---|
| `--task <description>` | **(Required)** The task or requirement for the agent to execute. |
| `--max-iterations <n>` | Maximum number of agent iterations (overrides `SAG_MAX_ITERATIONS` in configuration). |
| `--record` | Save setup artifacts (contexts, reports) to local session logs for debugging and auditing. |
| `--coverage` | Run an isolated JaCoCo coverage pass after the task (best-effort). |
| `--ui` | Enable the Rich live progress display for this run. |

A task ends only when the model replies `TASK COMPLETE: <summary>` and no tool call is still failing or running; an early claim is refused and the model is told what is unresolved.

#### `sag trajectory <session_dir>`

| Option | Description |
|---|---|
| `--follow` | Tail a running session: one JSON delta per line until interrupted. |
| `--detail [summary\|full]` | `summary` (default): names, codes, timing, tokens. `full`: also the bytes each ref names. |

#### `sag shell <name>`

| Option | Description |
|---|---|
| `--shell <path>` | Shell to use in the container (default: `/bin/bash`). |

#### `sag ui`

| Option | Description |
|---|---|
| `--host <host>` | Host for the local Web UI (default: `127.0.0.1`). |
| `--port <port>` | Port for the local Web UI. Use `0` for an automatically assigned port. |
| `--demo` | Use deterministic demo data instead of discovering live Docker workspaces. |

#### `sag remove <name>`

| Option | Description |
|---|---|
| `--force` | Force removal without confirmation prompt. |

#### `sag inspect <name>`

| Option | Description |
|---|---|
| `--phase <name>` | Phase to inspect (`provision`/`analyze`/`build`/`test`/`report`). With no phase, lists all phases and their iteration spans. |
| `--iter <n>` | Show the reconstructed context window at a specific iteration (the intro/ledger the model saw, plus the surrounding actions). |
| `--session <dir>` | Read from a local `--record` artifact directory (e.g. `logs/session_X`) instead of the live container. |

## ✅ Running Tests Locally

```bash
# Run the full pytest suite (integration + smoke tests)
uv run pytest

# Or execute a focused contract/smoke scenario for faster feedback
uv run pytest tests/test_report_contract.py

# Web UI unit tests (vitest) and a production build of the bundled assets
cd webui && npm test -- --run && npm run build
```

## ⚙️ Configuration Explained

All configuration is managed through the `.env` file in the project's root directory.

**Key Configuration Options:**
- `SAG_ACTION_MODEL` / `SAG_ACTION_PROVIDER`: The executor model and provider. The model must support native tool calling.
- `SAG_ADVISOR_MODE`: `same-model` (default) reviews with the executor model in a fresh context; an explicit model name selects another reviewer; `off` disables advisor consults and their associated guarantees for an ablation.
- `SAG_ADVISOR_MAX_TOKENS` / `SAG_ADVISOR_PHASE_CAP`: Limit the length and number of advisor consults.
- `SAG_THINKING_MODEL` / `SAG_THINKING_PROVIDER`: Retained configuration fields; the current executor loop does not alternate thinking and action models.
- `SAG_REASONING_EFFORT`: For reasoning models, controls reasoning depth (`low`, `medium`, `high`).
- `SAG_THINKING_BUDGET_TOKENS`: For Claude models, controls the thinking budget (e.g. 1024, 2048, 4096).
- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.: API keys for the respective LLM providers.
- `SAG_LOG_LEVEL`: Logging verbosity. `DEBUG` is highly detailed and includes LiteLLM's internal logs.
- `SAG_MAX_ITERATIONS`: Maximum iterations for a single `run` or `project` command.
- `SAG_MAX_WALL_CLOCK_SECONDS`: Global wall-clock cap for a whole run (default `7200`); the run ends with a clear status once exceeded, independent of per-command behavior.
- `SAG_DISPATCH_SOFT_TIMEOUT_SECONDS`: Soft window before a long build is handed back as a pollable detached job (default `900`).

## 🔍 How It Works: A Look Under the Hood

When you run `sag project <url>`, the phase machine drives the run:

1.  **Environment Initialization**: SAG's Docker Orchestrator starts a container whose filesystem holds the project workspace. The host retains orchestration logs and control records.
2.  **Trunk & Phases**: A **Trunk Context** is created with the goal and the five phases — provision → analyze → build → test → report.
3.  **Provision**: The agent clones the repository and installs the toolchain the project needs (e.g. the detected JDK for a Gradle project, a Maven that satisfies the pom's enforced minimum), then claims the phase done.
4.  **Analyze**: `project(action='analyze')` surveys the build system, the module scope, the Java requirement, and where the tests live, and publishes that survey as the build-requirements manifest that `build` routes on. An honest "unknown" with evidence is acceptable.
5.  **Build**: `build(action='compile')` compiles via the registered toolchain and seals a receipt. Long builds run detached; the agent polls the in-container log instead of the build being killed.
6.  **Test**: `build(action='test')` runs the suite and seals a receipt of the report files it wrote. Failing tests are recorded as the project's result and do not fail the setup; if tests genuinely cannot run, the agent records `phase(action='blocked')` with evidence.
7.  **Per-phase mechanics**: For each phase the engine opens a clean context window (goal digest + prior phases' key results + the phase objective), validates the model's `done` claim against physical evidence, advances on success, accepts `blocked` honestly, and journals every iteration. A phase is only cut short if continuing would starve the iterations later phases need.
8.  **Report & Verdict**: the finalizer seals one snapshot from the receipts and the physical build check; `report` renders `setup-report-*.md` from it, and the verdict kernel produces one outcome (success / partial / failed) shared by the report, CLI banner, and exit code. The container is left fully configured for follow-up `sag run --task` work.

## 🎯 Use Cases

- **Rapid Prototyping**: Set up and run any open-source project in minutes to evaluate its suitability.
- **Standardized Dev Environments**: Create consistent, one-click development environments for team members.
- **CI/CD Automation**: Automate complex project setups and testing environments in your CI pipelines.
- **Learning New Technologies**: Quickly get hands-on with an unfamiliar framework or stack by letting SAG handle the setup.
- **Secure Experimentation**: Safely test unfamiliar or untrusted code in an isolated sandbox.

## 🤝 Contributing

We warmly welcome contributions of all kinds! Whether it's a bug report, a feature suggestion, or a pull request, your help is invaluable to the project.

## 📝 License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

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
