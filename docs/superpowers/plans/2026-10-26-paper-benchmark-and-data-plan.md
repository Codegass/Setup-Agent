# Paper-grade benchmark and paper data preparation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce the measurements and the data files a full paper on SAG needs (target: ISSTA 2027, abstract 2027-01-08), from one frozen SAG version, under conditions that are the same for every arm, with repeated runs, three backbone models run once in every arm, more baseline agents (a published setup agent and the current coding-agent CLIs), ablations, a validated verdict, and one external benchmark.

**Architecture:** Three layers. (1) Harness changes so that one campaign controller can run any arm (SAG, Claude Code, OpenCode, Codex CLI, Qwen Code, an OpenHands or mini-SWE-agent control, ExecutionAgent) with any backbone model, as the same unprivileged user, with the launcher-only tracer, `k` times. (2) One campaign directory under `output/paper-fifty-<date>/` whose `protocol.json` freezes every input, image, commit and model, whose `runs/` hold the sealed evidence, and whose `readout.py` turns sealed files into one `data.json` with a run index. (3) Versioned analysis scripts under `scripts/` with tests (statistics, self-report agreement, CI parity, cost, coding agreement) and a `paper/` package that renders every table and figure from `data.json` alone.

**Tech Stack:** Python 3.12 in the shared `.venv` (uv), pytest; Docker through OrbStack; the benchmark gateway (`scripts/benchmark_model_gateway.py`, litellm 1.106.2); `scripts/run_portable_harness.py`; the campaign controllers already in `output/baselines-fifty-20261021/` (`campaign.py`, `project_wave_runner.py`, `supplement.py`, `readout.py`) as the model for the new one; matplotlib for figures.

**Spec:** The evidence requirements in `output/reports/SAG 论文 投稿会议 推荐.md` (section "46/37/31 只是预实验：审稿人会盯住五类证据"), the measurement rules of `output/baselines-fifty-20261021/README.md` ("Reading rule", "Setup", "Limits"), and the agent and benchmark survey in `output/reports/基线 agent 与外部 benchmark 候选.md` with its notes under `output/research_notes/基线 agent 与外部 benchmark 候选/`. All travel with this plan. The venue decision (ISSTA versus ACL) is not part of this plan; both need the same data.

## Global Constraints

- **Serial machine use.** One campaign at a time on this machine; never a pytest run beside a campaign in the same tree; never `uv sync` or any change to the shared `.venv` while a harness, gateway or rerun may run or is queued. Check `pgrep -fl "[r]un_portable_harness|[c]ampaign.py run|[s]upplement.py run"` and `docker ps -q` before any such change. A waiter never uses a `pgrep -f` pattern that matches its own shell.
- **Evidence before deletion.** Archive sealed run files and probe logs into the campaign directory before any worktree, container or image is removed; Chenhao verifies by hand.
- **Authoritative sources.** Every number comes from sealed files (`result.json`, `score.json`, `gateway/<id>/finished.json`, `agent-records/`, `verdict.json`, `control_events.jsonl`), never from console renders. Analysis scripts read only those files.
- **Frozen inputs.** The 52 definitions are `output/benchmark-fifty-20261021/inputs/<project>/` (20 formal, 32 review-required). Copy them; never run that campaign's `prepare` again (its source directories are gone). Review-required definitions are admitted with `--allow-review-required`.
- **Frozen runtime.** 4 CPUs, 8 GiB, no swap, 7,200 s per attempt; one image for all arms (Task 2 fixes its digest); the tracer on the Maven launcher only (commit bc62b2fd, harness-r5); `LANG`/`LC_ALL=C.UTF-8` in every arm; project commands as the unprivileged user `sag` in every arm (Task 2).
- **Frozen SAG.** The newest `main` after Tasks 1 to 5 have merged is the freeze, recorded in `protocol.json` as `sag_commit`; no SAG change after the technical validation run (Task 12, step 2) starts. Harness or controller fixes during the campaign become numbered revisions (`supplement.py revise`), and affected cells are rerun; nothing is edited in place.
- **Models (decided 2026-10-26).** Primary: `gpt-5.4-mini` (as in the 52-project campaigns). Frontier closed: `gpt-6-luna` (OpenAI; Responses and Chat Completions; function calling in Chat Completions only with `reasoning_effort: none`, which the gateway already sets for the baseline arms; 1,050,000-token context; $0.10 / $0.01 cached / $0.50 output per million tokens, read 2026-10-26). Local open-weight: `qwen3.8:27b-nvfp4` served by Ollama 0.40.10 on this host (Apple M5 Pro, 64 GiB; family qwen3_5, 27.8B, 262,144-token context; capabilities completion, tools, thinking; probed 2026-10-26: `/v1/responses` and `/v1/chat/completions` with a tool call both answer). The gateway reaches it with `OPENAI_BASE_URL=http://localhost:11434/v1` and any non-empty `OPENAI_API_KEY`. The local model runs in one lane; its wall times are not comparable with the cloud models' and are reported apart; its tokens are.
- **Repeats.** Every model runs once in every arm (`k = 1`); the primary model additionally has `k = 3` on the three original arms (SAG, Claude Code, OpenCode). Ablations `k = 1` on the primary model. A repeat is a fresh container and a fresh gateway ledger; nothing is shared between repeats.
- **Randomness.** `seed = 20261026` for every resampling procedure; bootstrap draws `B = 10000`; the resampling unit is the project, never the attempt.
- **Copy.** Plain English in every document and table label; no invented house terms; a measurement and an interpretation are marked apart. Commit messages plain English; no attribution trailers.
- **Anonymity.** No author name, host name, home directory, e-mail or repository owner in any file of the paper package (Task 11 checks this mechanically).

## Review Focus

- A backbone model whose API rejects what the gateway's Anthropic-dialect translation sends (tool schemas, system blocks): one request per client dialect per model must succeed before any cell runs (Task 1, step 9).
- Clients run as `sag` instead of root: Claude Code's settings path, OpenCode's config path, the hook script under `/opt/`, and the `/work` and `/workspace` ownership all change; the smoke must show a bound hook event and a bound Maven launch, not just an exit code (Task 2, step 7).
- Three lanes with repeats and several models must never share a gateway port or a container name; a collision silently mixes two ledgers (Task 3, step 6).
- Statistics over three attempts per project are correlated within a project; a bootstrap over attempts, or a McNemar over 156 pairs, would overstate certainty. Every test resamples or pairs by project (Task 6, steps 1 and 5).
- Self-reports are free text in three dialects; a parser that reads "failed" inside "no tests failed" as a failure inverts the agreement table. The parser's fixtures include negated phrasings from the real transcripts (Task 7, step 1).

---

### Task 1: One model parameter through the gateway and the harness

**Files:**
- Modify: `scripts/benchmark_model_gateway.py` (the `MODEL = "gpt-5.4-mini"` constant at line 21; `litellm.token_counter(model=MODEL, …)`; the provider call sites)
- Modify: `scripts/run_portable_harness.py` (`claude_arm_environment`, `launch_environment`, the OpenCode `config` dict near line 385, the SAG `model_config` merge near line 95, the argparse block near line 581)
- Test: `tests/test_portable_harness_config.py`, `tests/test_benchmark_model_gateway.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `run_portable_harness.py --model <model id>` (default `gpt-5.4-mini`) and `--provider-base-url <url>` (default `https://api.openai.com/v1`; the Ollama model uses `http://localhost:11434/v1`), both recorded in `launch.json`; `benchmark_model_gateway.py --model <model id>` (its `prepare_request` permits only that model, as today); pure helpers `claude_arm_environment(seconds: int, model: str) -> dict[str, str]`, `opencode_config(endpoint: str, model: str) -> dict`, `sag_model_config(protocol: dict, *, image: str, seconds: int, no_advisor: bool, model: str | None) -> dict`; `launch.json["model"]` and `result.json["model"]` carry the string.

- [ ] **Step 1: Write the failing tests**

```python
def test_claude_arm_environment_carries_the_model():
    env = claude_arm_environment(7200, "anthropic/claude-sonnet-5-5")
    assert {env["ANTHROPIC_MODEL"], env["ANTHROPIC_DEFAULT_OPUS_MODEL"],
            env["ANTHROPIC_DEFAULT_SONNET_MODEL"], env["ANTHROPIC_DEFAULT_HAIKU_MODEL"]} == {"anthropic/claude-sonnet-5-5"}

def test_opencode_config_names_the_model_once():
    cfg = opencode_config("http://host.docker.internal:19160", "openrouter/qwen/qwen3-coder")
    assert cfg["model"] == cfg["small_model"] == "benchmark/openrouter/qwen/qwen3-coder"
    assert list(cfg["provider"]["benchmark"]["models"]) == ["openrouter/qwen/qwen3-coder"]

def test_sag_model_config_overrides_actor_and_advisor_models():
    cfg = sag_model_config({"model_config": {"thinking_model": "gpt-5.4-mini", "action_model": "gpt-5.4-mini"}},
                           image="sha256:x", seconds=7200, no_advisor=False, model="anthropic/claude-sonnet-5-5")
    assert cfg["thinking_model"] == cfg["action_model"] == "anthropic/claude-sonnet-5-5"
    assert cfg["advisor_mode"] == "anthropic/claude-sonnet-5-5"

def test_sag_model_config_without_model_keeps_the_protocol_models():
    cfg = sag_model_config({"model_config": {"thinking_model": "gpt-5.4-mini", "action_model": "gpt-5.4-mini"}},
                           image="sha256:x", seconds=7200, no_advisor=False, model=None)
    assert cfg["action_model"] == "gpt-5.4-mini" and cfg["advisor_mode"] == "openai/gpt-5.4-mini"
```

For the gateway: a test that `build_app(model="anthropic/claude-sonnet-5-5", …)` records `"model": "anthropic/claude-sonnet-5-5"` in `start.json` and that the estimate path calls `litellm.token_counter` with that model (monkeypatch `litellm.token_counter`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=.:src .venv/bin/python -m pytest -q tests/test_portable_harness_config.py tests/test_benchmark_model_gateway.py -k "model"`
Expected: FAIL (`claude_arm_environment() takes 1 positional argument`, `opencode_config` not defined, `sag_model_config` not defined).

- [ ] **Step 3: Implement the helpers and thread `--model` through both scripts**

In the harness: extract `opencode_config` and `sag_model_config` from the inline code (same values as today when `model` is the default), add `--model` to argparse, pass it to the gateway launch (`--model`), write it to `launch.json` and `result.json`. In the gateway: replace the constant with `args.model`; the `/health` reply includes the model. SAG's `advisor_mode` takes the same string when a model is given (the advisor is the same model as the actor in every campaign so far; keep that).

- [ ] **Step 4: Run the tests to verify they pass**

Run: same command. Expected: PASS.

- [ ] **Step 5: Run the harness and gateway test files**

Run: `PYTHONPATH=.:src .venv/bin/python -m pytest -q tests/test_portable_harness_config.py tests/test_benchmark_model_gateway.py tests/test_benchmark_gateway_usage.py`
Expected: all pass.

- [ ] **Step 6: Record the three models in the campaign README draft**

One line each: id, provider and base URL, context window, price per million input / cached / output tokens with the date read (`gpt-6-luna`: 0.10 / 0.01 / 0.50 USD, 2026-10-26; the local model: no price, tokens only), and the reasoning setting each arm sends. For the Ollama model also record `ollama --version`, the model digest (`ollama show qwen3.8:27b-nvfp4 --modelfile | head -1` and `/api/show`'s digest) and that Ollama's `/v1/responses` returns a `reasoning` output item, which the gateway's usage reader must tolerate.

- [ ] **Step 7: Commit**

```bash
git add scripts/benchmark_model_gateway.py scripts/run_portable_harness.py tests/test_portable_harness_config.py tests/test_benchmark_model_gateway.py
git commit -m "feat(benchmark): the harness and the gateway take the backbone model as a parameter"
```

- [ ] **Step 8: Smoke per client dialect per model (needs `OPENAI_API_KEY` with access to `gpt-6-luna` and the Ollama server running)**

For each model and each arm in (sag, claude, opencode): one attempt of `vscode-remote-try-java` with `--seconds 900` into `output/paper-fifty-<date>/smoke/<model>-<arm>/`. Pass condition: `result.json` has `agent_exit_code == 0`, the gateway ledger has at least one `finished.json` with `status: completed` and non-null usage, and `score.json["commands"][0]["status"] == "passed"`.

- [ ] **Step 9: Record the smoke table in the campaign README draft** (model × arm → passed / the first error line / tokens per second for the local model, from the gateway ledger's timestamps and usage) and stop here if any cell fails; the model list is final only when all nine pass.

### Task 2: The same unprivileged user and the same image in every arm

**Files:**
- Modify: `scripts/run_portable_harness.py` (`provision_baseline` near line 316: the `docker run` and the `dex` exec helper; the Claude Code and OpenCode launch commands)
- Modify: `docker/project-user/Dockerfile`, `docker/project-user/README.md` (home directories, `/work` ownership)
- Test: `tests/test_portable_harness_config.py`

**Interfaces:**
- Consumes: Task 1's `--model`.
- Produces: `run_portable_harness.py --run-as sag` (default `root`, to keep old campaigns reproducible); `launch.json["run_as"]`; the image tag `sag-paper:<date>` built by `scripts/build_project_user_image.sh --tag sag-paper:<date>` and its digest written to the campaign README and `protocol.json["image"]`.

- [ ] **Step 1: Write the failing test**

```python
def test_run_as_user_changes_the_exec_identity_and_home():
    argv, env = baseline_exec_prefix(container="c1", run_as="sag")
    assert argv[:5] == ["docker", "exec", "-u", "sag", "-w"] or "--user" in argv
    assert env["HOME"] == "/home/sag"
```

(`baseline_exec_prefix(container: str, run_as: str) -> tuple[list[str], dict[str, str]]` is the seam to extract from the inline `dex` helper; with `run_as="root"` it must equal today's argv.)

- [ ] **Step 2: Run it to verify it fails.** Expected: `baseline_exec_prefix` not defined.

- [ ] **Step 3: Implement**

The agent process (claude / opencode) and every `dex` call that runs on the agent's behalf run as `run_as`; the controller's own calls (recorder upload, boundary snapshots, trace reads under `/opt/benchmark-process-traces`) stay root. The checkout is `chown`ed to `run_as` after the controller's integrity snapshot, as SAG's orchestrator does after clone. The tracer still attaches as root to the launcher pid.

- [ ] **Step 4: Run the test to verify it passes.**

- [ ] **Step 5: Build the image**

Run: `scripts/build_project_user_image.sh --tag sag-paper:<date>` (add the `--tag` option if the script lacks it; default stays `sag-advisor-five-cold-user:20261021`). Record `docker image inspect --format '{{.Id}}' sag-paper:<date>` in the campaign README draft.

- [ ] **Step 6: Commit**

```bash
git add scripts/run_portable_harness.py scripts/build_project_user_image.sh docker/project-user tests/test_portable_harness_config.py
git commit -m "feat(benchmark): baseline agents run as the project user in the same image as SAG"
```

- [ ] **Step 7: Smoke as the user, both baseline arms**

`vscode-remote-try-java` and `commons-io` (the permission-dependent project), `--run-as sag --image sag-paper:<date>`, `--seconds 1800`. Pass condition per cell: `agent-records/events/` holds PreToolUse and PostToolUse events for the pinned shell call; `agent-records/run.json["invocations"]` has one bound invocation with `exit_code == 0`; `score.json` commands passed; and for commons-io the read-only-file tests passed (grep the command log for `CanReadFileFilterTest` with `Failures: 0`). Record both in the README draft.

- [ ] **Step 8: Freeze SAG.** Chenhao names the SAG commit (default: the `main` head at the time). Record `sag_commit`, the image digest, the harness commit and the three model strings at the top of the campaign README draft. From this point no SAG change enters the matrix.

### Task 3: The paper campaign controller

**Files:**
- Create: `output/paper-fifty-<date>/campaign.py`, `readout.py`, `supplement.py`, `README.md` (copy and adapt from `output/baselines-fifty-20261021/`; the SAG arm from `output/benchmark-fifty-20261021/campaign.py`)
- Create: `output/paper-fifty-<date>/matrix.json`
- Test: `output/paper-fifty-<date>/test_campaign.py` (run with the same `PYTHONPATH` as `readout.py`; campaign directories are gitignored, so these tests live beside the scripts)

**Interfaces:**
- Consumes: Task 1 `--model`, Task 2 `--run-as`, `--image`.
- Produces: `matrix.json`: a list of blocks `{"block": "primary-k3", "arms": [...], "model": "...", "repeats": 3, "variant": "default"}`; slot keys `<project>-<arm>-<model slug>-<variant>-r<repeat>` ; `protocol.json["slots"]` with `block`, `model`, `variant`, `repeat`, `revision`; `analysis/data.json` rows with the same five fields plus `commands_passed`, `status`, `provider` tokens, `process_seconds`, `agent_seconds`, `outer_timeout`.

- [ ] **Step 1: Write the matrix**

```json
[
 {"block": "validation-k1", "arms": ["sag", "claude", "opencode"], "model": "gpt-5.4-mini", "repeats": 1, "variant": "default"},
 {"block": "primary-k3",    "arms": ["sag", "claude", "opencode"], "model": "gpt-5.4-mini", "repeats": 3, "variant": "default"},
 {"block": "luna-k1",       "arms": ["sag", "claude", "opencode"], "model": "gpt-6-luna", "repeats": 1, "variant": "default"},
 {"block": "local-k1",      "arms": ["sag", "claude", "opencode"], "model": "qwen3.8:27b-nvfp4", "repeats": 1, "variant": "default", "lanes": 1},
 {"block": "ablations-k1",  "arms": ["sag"], "model": "gpt-5.4-mini", "repeats": 1, "variant": ["no-advisor", "no-provision-from-task", "no-test-phase-reuse", "gate-advisory"]},
 {"block": "arms-k1",       "arms": ["codex", "qwencode", "control"], "model": ["gpt-5.4-mini", "gpt-6-luna", "qwen3.8:27b-nvfp4"], "repeats": 1, "variant": "default"},
 {"block": "execagent-k1",  "arms": ["executionagent"], "model": "gpt-5.4-mini", "repeats": 1, "variant": "default"}
]
```

`validation-k1` is the technical validation of the whole pipeline (Task 12, step 2); when no harness revision follows it, its attempts count as repeat 1 of `primary-k3` and the block runs the remaining two repeats. `control` is the published-baseline control arm chosen in Task 4b (OpenHands headless, else mini-SWE-agent). `lanes: 1` serializes a block whose model is served locally.

Blocks run in this order; a block's results are usable before the next starts.

- [ ] **Step 2: Write the failing tests**

```python
def test_slots_are_unique_and_cover_the_matrix():
    slots = build_slots(matrix, projects)
    assert len(slots) == len({s["key"] for s in slots})
    assert sum(1 for s in slots if s["block"] == "primary-k3") == 52 * 3 * 3

def test_lanes_never_share_a_port_or_a_project():
    waves = build_waves(slots, lanes=3)
    for wave in waves:
        assert len({s["port"] for s in wave}) == len(wave)
        assert len({s["project"] for s in wave}) == len(wave)

def test_readout_keeps_every_repeat_and_marks_reruns():
    rows = readout(protocol_with_one_rerun)
    assert sorted(r["repeat"] for r in rows if r["project"] == "gson" and r["arm"] == "sag") == [1, 2, 3]
    assert rows_for("gson-sag-mini-default-r2")["superseded_by"] == "gson-sag-mini-default-r2-again"
```

- [ ] **Step 3: Run them to verify they fail.**

- [ ] **Step 4: Implement `build_slots(matrix, projects) -> list[dict]`, `build_waves(slots, lanes) -> list[list[dict]]`, `readout(protocol) -> list[dict]`** in the campaign scripts; `campaign.py` takes `prepare` (copies the 52 definitions, writes `protocol.json` with hashes, image digest, `sag_commit`, models, harness commit) and `run --block <name>`; `supplement.py` keeps the `revise` / `run <key>` contract of the baselines campaign, with the rerun key `<key>-again<n>`.

- [ ] **Step 5: Run the tests to verify they pass.**

- [ ] **Step 6: Port and name audit**

A test that the ports a wave uses are `BASE_PORT + lane` and that a container name embeds the full slot key; run `docker ps -a --format '{{.Names}}'` before `run` and refuse to start if any `sag-bench-` container exists.

- [ ] **Step 7: Budget table in the README draft**

Rates from the 2026-10 campaigns: baselines about 0.18 h per cell with three lanes (104 cells in 19 h); SAG about 0.25 h per cell. Expected: validation-k1 156 cells ≈ 1.3 days (then 312 more for primary-k3 ≈ 2.7 days); luna-k1 156 cells ≈ 1.3 days; local-k1 156 cells in one lane, time measured in the smoke (expect 3 to 5 days); ablations 208 cells ≈ 1.5 days; arms-k1 468 cells ≈ 4 days, the local-model third of it in one lane; execagent 52 cells ≈ 0.5 day; the external benchmark (Task 13) 5 to 7 days. About three weeks of machine time in all. Write the planned start and end date per block; the venue report's window closes 2026-12-15 for experiments, so Chenhao cuts from the end of this list (arms-k1's extra models first, then the external benchmark's sample size) if the dates do not hold.

- [ ] **Step 8: Commit nothing to git** (campaign directories are gitignored); record the controller's file hashes in `protocol.json["controller_files"]` as the baselines campaign does.

### Task 4a: ExecutionAgent as a specialised setup-agent arm

**Files:**
- Create: `scripts/benchmark_executionagent.py` (driver), `docs/superpowers/specs/2026-10-26-executionagent-arm.md` (the spike's findings and the protocol chosen)
- Modify: `scripts/run_portable_harness.py` (`--arm executionagent`)
- Test: `tests/test_benchmark_executionagent.py`

**Interfaces:**
- Consumes: Task 1 `--model`; the gateway's OpenAI-compatible endpoint (`OPENAI_BASE_URL`).
- Produces: a cell directory with the same `result.json` / `score.json` shape as the other arms plus `executionagent/` (its `run.jsonl`, `success_artifacts/` or `forced_exit_cycle/`), and `score.json["evidence_mode"] = "replayed"` or `"observed"`.

- [ ] **Step 1: Spike (time-boxed to one day; write the answers into the spec file)**

Install `michaelpradel/ExecutionAgent` at a pinned commit into its own venv. Answer with evidence: (a) does it honour `OPENAI_BASE_URL` (its README names only `OPENAI_API_KEY`); (b) does it need Docker on the host, and can it run inside our benchmark container with a Docker socket or a sibling daemon; (c) what its success criterion is (`success_artifacts/` present) and whether its generated `commands.sh` contains the pinned CI command verbatim for three Java projects (`gson`, `commons-dbutils`, `curator`).

- [ ] **Step 2: Decide the evidence mode (recorded in the spec)**

Preferred, if (b) allows it: ExecutionAgent runs inside our container as user `sag` with the Maven witness in place; launches bind through the witness only (no hook events), which needs the observer to accept `arm == "executionagent"` with a witness-only binding rule. Fallback: ExecutionAgent runs on the host with its own Docker; our evaluator rebuilds its `success_artifacts/Dockerfile`, runs `commands.sh` once in a fresh container with the witness and the launcher-only tracer, and scores the pinned command from that replay. The paper then reports the ExecutionAgent column as replay-verified and says so in the table note; the other arms stay passively observed.

- [ ] **Step 3: Write the failing tests for the driver**

```python
def test_driver_writes_the_project_metadata_executionagent_reads(tmp_path):
    meta = project_metadata(task_json, budget_seconds=7200)
    assert meta["language"] == "Java" and meta["commit"] == task_json["sha"] and meta["project_url"].endswith("/curator.git")

def test_success_is_read_from_artifacts_not_from_text(tmp_path):
    assert read_outcome(run_dir_with_success_artifacts) == {"claimed": "success", "commands_sh": [...]}
    assert read_outcome(run_dir_with_forced_exit) == {"claimed": "budget_exhausted", "commands_sh": [...]}
```

- [ ] **Step 4: Run them to verify they fail.**

- [ ] **Step 5: Implement `project_metadata(task: dict, *, budget_seconds: int) -> dict`, `read_outcome(run_dir: Path) -> dict`, and the arm's launch in the harness** per the mode chosen.

- [ ] **Step 6: Run the tests to verify they pass; then one real cell (`gson`) end to end** with `score.json` produced; record its `evidence_mode`.

- [ ] **Step 7: Commit**

```bash
git add scripts/benchmark_executionagent.py scripts/run_portable_harness.py tests/test_benchmark_executionagent.py docs/superpowers/specs/2026-10-26-executionagent-arm.md
git commit -m "feat(benchmark): ExecutionAgent runs as a fourth arm on the same tasks"
```

### Task 4b: Current coding-agent CLIs as arms: Codex CLI, Qwen Code, and a published-baseline control

**Files:**
- Modify: `scripts/run_portable_harness.py` (`--arm codex|qwencode|openhands|miniswe`), `scripts/benchmark_native_capture.py` (settings files per client), `scripts/benchmark_native_observer.py` (`expected_shell_calls` and `native_result` for the new transcripts)
- Create: `docker/paper-arms/Dockerfile` (the pinned client versions installed on the Task 2 image), `docs/superpowers/specs/2026-10-26-cli-arms.md` (per-arm recipe as verified)
- Test: `tests/test_benchmark_native_observer.py`, `tests/test_portable_harness_config.py`

**Interfaces:**
- Consumes: Task 1 `--model` / `--provider-base-url`, Task 2 `--run-as`.
- Produces: per arm a `launch.json` with the exact argv and environment; hook events in the same `agent-records/events/` shape as Claude Code's (`PreToolUse` / `PostToolUse`, `tool_input.command`, `tool_response`); `expected_shell_calls(arm, transcript)` for each new transcript format; a `client_versions` entry in `protocol.json`.

Recipes to verify (from the survey; versions as of 2026-10-26, pin them in the image):
- **Codex CLI 0.164.0** (Apache-2.0): `codex exec --json --skip-git-repo-check -a never -s danger-full-access -m <model> "<brief>"`; provider through `$CODEX_HOME/config.toml` (`model_providers.benchmark.base_url = <gateway>/v1`, `env_key`, `wire_api = "responses"`); hooks of the Claude Code shape in its hooks config, run with `--dangerously-bypass-hook-trust`; `rollout-*.jsonl` is the second record. Open question to settle in step 1: whether hooks fire under `codex exec`.
- **Qwen Code 0.26.1** (Apache-2.0): `qwen -p "<brief>" --yolo --output-format stream-json` with `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL` (or the `modelProviders` setting with `wireApi`); hooks of the Claude Code shape, confirmed to run under `-p` by its docs.
- **Control arm, one of:** OpenHands CLI 1.16.0 (MIT): `openhands --headless --json --override-with-envs -t "<brief>"` with `LLM_MODEL=openai/<model> LLM_BASE_URL=<gateway>/v1`; no hooks, but JSONL `action/run` events with the command and exit code. Else mini-SWE-agent 2.4.6 (MIT): `mini -t "<brief>" -y --exit-immediately -o traj.json`, litellm `api_base`, `.traj.json` with every command and `returncode`; raise its `environment.timeout` from 30 s to the attempt budget.
- Every new arm: stdin from `/dev/null`, telemetry and auto-update off, network on, no read-only sandbox, the client's config under `/work` so the checkout stays clean.

- [ ] **Step 1: Spike each recipe once by hand in the Task 2 image on `vscode-remote-try-java`** and write into the spec file, per arm: the exact command that worked, whether hook events carried the command and an exit code, and the transcript file that records shell commands when hooks do not. Drop an arm whose shell commands cannot be bound by hooks or by a complete transcript; say so in the spec.
- [ ] **Step 2: Write the failing tests**: `expected_shell_calls("codex", transcript)` and `("qwencode", …)`, `("openhands", …)` / `("miniswe", …)` on fixture transcripts copied from the spike; `native_result` for each `PostToolUse` shape; `launch_recipe(arm, model, base_url) -> dict` returning argv and environment per arm.
- [ ] **Step 3: Run them to verify they fail.** **Step 4: Implement.** **Step 5: Run to verify they pass.**
- [ ] **Step 6: Smoke each arm on `vscode-remote-try-java` and `commons-io` as user `sag`** with the Task 2 pass conditions; record the table in the campaign README draft.
- [ ] **Step 7: Commit**

```bash
git add scripts/run_portable_harness.py scripts/benchmark_native_capture.py scripts/benchmark_native_observer.py docker/paper-arms tests docs/superpowers/specs/2026-10-26-cli-arms.md
git commit -m "feat(benchmark): Codex CLI, Qwen Code and a published-baseline control run as arms"
```

Excluded by the survey, with the reason recorded in the spec: Gemini CLI (Gemini API only), Antigravity (closed, cached-credential auth), Aider (no hooks, offers rather than runs commands), Cursor and Copilot CLI (no documented custom base URL), SWE-agent (maintenance only), Kimi Code CLI (`-p` cannot combine with `--yolo`), Cline CLI (provider ids undocumented).

### Task 5: SAG ablation variants

**Files:**
- Modify: `src/sag/config/settings.py` (one new setting), the phase-gate seam (`src/sag/agent/physical_validator.py` and the gate decision writer that emits `control_events.jsonl` `code` / `validator_state`; the implementer locates the close path from those field names)
- Modify: `scripts/run_portable_harness.py` (`--variant`), `output/paper-fifty-<date>/campaign.py`
- Test: `tests/test_settings_variants.py`, `tests/test_phase_gate_advisory.py`

**Interfaces:**
- Consumes: Task 3's `variant` field.
- Produces: `variant_environment(variant: str) -> dict[str, str]` mapping `no-advisor` → `{"SAG_ADVISOR_MODE": "off"}`, `no-provision-from-task` → `{"SAG_PROVISION_FROM_TASK": "false"}`, `no-test-phase-reuse` → `{"SAG_REUSE_VERIFIED_TEST_PHASE": "false"}`, `gate-advisory` → `{"SAG_PHASE_GATE": "advisory"}`; `Config.phase_gate: Literal["enforced", "advisory"] = "enforced"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_variant_environment_is_exact():
    assert variant_environment("gate-advisory") == {"SAG_PHASE_GATE": "advisory"}
    with pytest.raises(KeyError): variant_environment("unknown")

def test_advisory_gate_closes_a_phase_on_the_claim_and_records_the_grade():
    # a success claim the enforced gate refuses (no receipt) closes under advisory,
    # and control_events.jsonl still carries the gate's grade with code "GATE_ADVISORY_OVERRIDE"
```

- [ ] **Step 2: Run them to verify they fail.**

- [ ] **Step 3: Implement.** The advisory gate grades exactly as the enforced gate and writes the same event, then lets the phase close; the verdict pipeline is unchanged, so an advisory run's `verdict.json` still derives its word from the pinned command. Nothing else in SAG changes.

- [ ] **Step 4: Run the tests to verify they pass; run `tests/test_settlement_triggers.py tests/test_job_barrier.py tests/test_verdict_finalizer*.py`.** Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/sag/config/settings.py src/sag/agent scripts/run_portable_harness.py tests/test_settings_variants.py tests/test_phase_gate_advisory.py
git commit -m "feat(ablation): named SAG variants, including an advisory phase gate"
```

Note: this task lands before the SAG freeze of Task 2 step 8, because the variants are part of the frozen version.

### Task 6: Statistics module

**Files:**
- Create: `scripts/benchmark_stats.py`
- Test: `tests/test_benchmark_stats.py`

**Interfaces:**
- Consumes: `analysis/data.json` rows (Task 3).
- Produces: `mcnemar_exact(a_pass: list[bool], b_pass: list[bool]) -> dict` (`b`, `c`, `p`); `bootstrap_ci(values_by_project: dict[str, list[float]], stat: Callable, *, B=10000, seed=20261026) -> tuple[float, float, float]` (point, low, high; resamples projects, then averages within project); `pass_rate_table(rows, *, by=("arm", "model", "variant")) -> list[dict]` with `mean_pass_rate`, `ci_low`, `ci_high`, `any_pass`, `all_pass`; `paired_tokens(rows, arm_a, arm_b) -> dict` (Wilcoxon signed-rank on per-project median log tokens, Cliff's delta); `write_stats(rows, out_dir)` → `stats.md` + `stats.json`.

- [ ] **Step 1: Write the failing tests**

```python
def test_mcnemar_counts_only_discordant_pairs():
    r = mcnemar_exact([1,1,0,0,1], [1,0,0,1,1])
    assert (r["b"], r["c"]) == (1, 1) and r["p"] == 1.0

def test_bootstrap_resamples_projects_not_attempts():
    values = {"p1": [1,1,1], "p2": [0,0,0]}
    point, lo, hi = bootstrap_ci(values, mean, B=2000, seed=1)
    assert point == 0.5 and lo == 0.0 and hi == 1.0   # two projects: the CI must reach both extremes

def test_pass_rate_table_uses_mean_over_repeats():
    rows = [row("gson","sag",True,1), row("gson","sag",False,2), row("gson","sag",True,3)]
    t = pass_rate_table(rows)
    assert t[0]["mean_pass_rate"] == pytest.approx(2/3) and t[0]["any_pass"] == 1 and t[0]["all_pass"] == 0
```

- [ ] **Step 2: Run them to verify they fail.** Expected: module not found.

- [ ] **Step 3: Implement** with `scipy.stats` (`binomtest` for McNemar, `wilcoxon`); add `scipy` to `pyproject.toml` dev extras only if absent (check first; do not touch the venv while a campaign runs).

- [ ] **Step 4: Run the tests to verify they pass.**

- [ ] **Step 5: Run `write_stats` on the existing 2026-10 data as a dry run** (`output/baselines-fifty-20261021/analysis/data.json`, one attempt per cell) and read the output: the three pairwise McNemar p-values on the 52 projects and the bootstrap CIs on 46/52, 38/52, 34/52; write them into the campaign README draft under "pre-experiment".

- [ ] **Step 6: Commit**

```bash
git add scripts/benchmark_stats.py tests/test_benchmark_stats.py pyproject.toml
git commit -m "feat(analysis): paired tests and project-level bootstrap for the benchmark"
```

### Task 7: Verdict validity, part one: self-report against the evaluator

**Files:**
- Create: `scripts/benchmark_self_report.py`, `tests/fixtures/self_report/` (final messages copied from real cells)
- Test: `tests/test_benchmark_self_report.py`

**Interfaces:**
- Consumes: a cell directory.
- Produces: `claimed_outcome(cell_dir: Path, arm: str) -> Literal["success", "failure", "unclear"]` using fixed rules: Claude Code, the last `type: result` event's text; OpenCode, the last assistant text part; SAG, `verdict.json["verdict"]` (`success` → success, `failed`/`partial` → failure); ExecutionAgent, `read_outcome` from Task 4. `agreement_table(rows_with_claims) -> list[dict]` per arm: `claimed_success_and_passed`, `claimed_success_and_failed`, `claimed_failure_and_passed`, `claimed_failure_and_failed`, `unclear`; `write_agreement(campaign_dir)` → `analysis/self-report.md` + `.json` + `analysis/self-report-disagreements.csv`.

- [ ] **Step 1: Write the failing tests with real phrasings**

Fixtures: curator-claude-r4 ("The build failed in `curator-recipes` during tests") → failure; rocketmq-claude-r2 ("Result: - The build failed …") → failure; jackrabbit-claude-r2 ("Result: **BUILD SUCCESS**") → success; a negated line ("No tests failed; BUILD SUCCESS") → success; a message with both ("initial attempt failed … then completed successfully") → success (the last status verb wins); an empty message → unclear.

- [ ] **Step 2: Run them to verify they fail.**

- [ ] **Step 3: Implement** with an ordered rule list, no model calls; each rule names the phrase it matched into the JSON row (`"rule": "last_build_status"`).

- [ ] **Step 4: Run the tests to verify they pass; run `write_agreement` over `output/baselines-fifty-20261021/` and `output/benchmark-fifty-20261021/`** and read the table; put it in the campaign README draft under "pre-experiment".

- [ ] **Step 5: Commit**

```bash
git add scripts/benchmark_self_report.py tests/test_benchmark_self_report.py tests/fixtures/self_report
git commit -m "feat(analysis): the agent's claimed outcome beside the evaluator's"
```

### Task 8: Verdict validity, part two: manual audit protocol

**Files:**
- Create: `output/paper-fifty-<date>/analysis/audit/PROTOCOL.md`, `analysis/audit/sample.csv`, `analysis/audit/coder-A.csv`, `analysis/audit/coder-B.csv`, `analysis/audit/adjudicated.csv`
- Create: `scripts/benchmark_audit_sample.py` (+ test `tests/test_benchmark_audit_sample.py`)

**Interfaces:**
- Consumes: `data.json`, `self-report-disagreements.csv`.
- Produces: `draw_sample(rows, disagreements, *, per_stratum=4, seed=20261026) -> list[dict]`: strata = arm × {evaluator passed, evaluator did not pass, self-report disagrees}; audit columns `key, auditor, pinned_command_ran_in_checkout (y/n), exit_zero (y/n), tests_count_matches_log (y/n), evaluator_right (y/n), note`; `audit_precision(adjudicated) -> dict` (evaluator precision and recall on the sample, with the Wilson interval).

- [ ] **Step 1: Write the failing test** (`draw_sample` is deterministic for the seed and returns `per_stratum` per non-empty stratum, never the same key twice).
- [ ] **Step 2: Run it to verify it fails.**
- [ ] **Step 3: Implement; write PROTOCOL.md**: what the auditor opens (`command.log`, `invocation.json`, the witness trace, `score.json`), the four yes/no questions, the rule that the two auditors work blind to each other and to the evaluator's status, adjudication by a third reader, Cohen's kappa reported (Task 10's script).
- [ ] **Step 4: Run the test to verify it passes; commit the script and test** (`git commit -m "feat(analysis): stratified audit sample for the evaluator"`). The audit itself happens after the primary block finishes (Task 12).

### Task 9: Secondary metric for all 52: CI parity from logs

**Files:**
- Create: `scripts/benchmark_ci_parity.py` (port of `output/benchmark-fifty-20261021/ci-rescore/ci_rescore.py`'s log-derived comparison, which already handles `-q` logs, Develocity output and TestNG double counts as `unavailable` with a reason)
- Test: `tests/test_benchmark_ci_parity.py`, fixtures from the CI logs under `output/benchmark-fifty-20261021/inputs/<project>/ci/`

**Interfaces:**
- Produces: `parity(cell_dir, ci_inputs_dir) -> dict` with `tests_agent`, `tests_ci`, `ratio`, `failures_agent`, `modules_equal`, `reason`; `write_parity(campaign_dir)` → `analysis/ci-parity.md` + `.json`.

- [ ] **Step 1: Write the failing tests** (curator: CI 267 + 295 … per module against an agent log; cayenne `-q` → `unavailable: ci log quiet`; casbin TestNG → counted once).
- [ ] **Step 2: Run to verify they fail.** **Step 3: Implement.** **Step 4: Run to verify they pass.**
- [ ] **Step 5: Commit** (`git commit -m "feat(analysis): test-count parity with the official CI log for every arm"`).

### Task 10: Cost accounting and coding agreement

**Files:**
- Create: `scripts/benchmark_cost.py`, `paper/prices.json` (per model: input, cached input, output USD per million tokens, the date and the page read), `scripts/benchmark_coding_agreement.py`
- Test: `tests/test_benchmark_cost.py`, `tests/test_benchmark_coding_agreement.py`

**Interfaces:**
- Produces: `cost_usd(usage: dict, model: str, prices: dict) -> float` (`uncached * in + cached * cached_in + output * out`); `cost_table(rows, prices) -> list[dict]` per arm × model: median and mean USD, and tokens; `cohen_kappa(a: list[str], b: list[str]) -> float`; `write_coding_agreement(coder_a_csv, coder_b_csv, out_md)`.

- [ ] **Step 1: Write the failing tests** (a usage of 1,000 uncached, 9,000 cached, 100 output at prices 1 / 0.25 / 4 per million costs `0.001 + 0.00225 + 0.0004`; kappa of identical codings is 1.0, of independent codings about 0).
- [ ] **Step 2: Run to verify they fail.** **Step 3: Implement.** **Step 4: Run to verify they pass.**
- [ ] **Step 5: Commit** (`git commit -m "feat(analysis): dollar cost from the gateway ledger and coder agreement"`).

### Task 11: The paper data package

**Files:**
- Create: `paper/make_tables.py`, `paper/make_figures.py`, `paper/README.md`, `paper/out/` (gitignored), `scripts/anonymize_campaign.py`
- Test: `tests/test_paper_tables.py`, `tests/test_anonymize_campaign.py`

**Interfaces:**
- Consumes: `analysis/data.json`, `stats.json`, `self-report.json`, `audit/adjudicated.csv`, `ci-parity.json`, `prices.json`, `why-not.json` (the paper campaign's, produced by the same `why_not.py` pattern as the baselines campaign).
- Produces, each as CSV and LaTeX under `paper/out/`:
  - T0 `delta-from-nier.tex`: the NIER paper's 15 projects / 4 model configurations / 78.9% build success / 48.4% test execution beside this paper's 52 projects / 3 models / pinned-command pass rate, with one line that says the two criteria differ.
  - T1 `main-results`: arm × model (every arm of Task 4a/4b included, the ExecutionAgent column marked with its evidence mode): mean pass rate over repeats with 95% CI, formal 20 and review-required 32 apart, `any_pass`, McNemar p against SAG.
  - T6 `external-benchmark`: SAG on the external benchmark of Task 13 beside that benchmark's published numbers, under both its metric and ours.
  - T2 `cost`: tokens (median, mean, p90; uncached and output apart) and USD per project, per arm × model.
  - T3 `failure-owners`: the why-not classes per arm, counts and share.
  - T4 `ablations`: SAG variants: pass rate, tokens, phases closed without a model turn.
  - T5 `verdict-validity`: self-report agreement per arm; audit precision and recall; CI parity (projects at ratio 1.0, below, unavailable).
  - F1 pass rates with CIs (grey bars, SAG in the one accent colour); F2 per-project token distributions (log scale, paired); F3 failure classes per arm (stacked, four greys plus one accent for "ours").
- `anonymize_campaign.py <campaign_dir> <out_dir>`: copies the campaign without `gateway/*/request.json` bodies, scrubs `/Users/<name>`, e-mail addresses, GitHub usernames of the authors and the host name; refuses to finish if any of a fixed list of strings (given on the command line) survives.

- [ ] **Step 1: Write the failing tests** (`make_tables` on a three-row fixture produces T1 with the right CI columns and a LaTeX file that compiles under `pdflatex` if present, else is well-formed; `anonymize` leaves no forbidden string and keeps `score.json` byte-identical).
- [ ] **Step 2: Run to verify they fail.** **Step 3: Implement** (matplotlib, no seaborn; one accent colour; every figure has a title stating the finding, as in `dataviz`). **Step 4: Run to verify they pass.**
- [ ] **Step 5: Commit** (`git commit -m "feat(paper): tables, figures and the anonymized replication package from sealed data"`).

### Task 12: Run the campaign and finish the write-ups

**Files:**
- Modify: `output/paper-fifty-<date>/README.md` (the campaign report, same structure as `output/baselines-fifty-20261021/README.md`: reading rule, headline, setup with revisions, why cells did not pass, tokens and cost, per project, limits, files)
- Create: `output/paper-fifty-<date>/analysis/` outputs of Tasks 6 to 11

- [ ] **Step 1: Pre-flight checklist (all yes before `run`)**: Tasks 1 to 5 merged to `main` and pushed; `sag_commit`, image digest, models and keys recorded; disk free ≥ 60 GB (`df -h /`); no container running; no harness process; the `.venv` will not be touched until the matrix ends; a waiter exists that reports each finished cell (pattern-safe `pgrep`).
- [ ] **Step 2: `campaign.py prepare`**, then `campaign.py run --block primary-k3`. While it runs: nothing else on the machine that uses Docker or the venv.
- [ ] **Step 3: After each block**: `readout.py`; `why_not.py` readings for every non-passing cell (one line each, with the file cited; the classes of `CLASSES` in the baselines campaign are the codebook); `benchmark_stats.write_stats`; update the README headline. A cell that failed for a controller, gateway or capture fault (not an agent or project outcome) is rerun through `supplement.py` on a numbered revision; never edited.
- [ ] **Step 4: After primary-k3**: run `benchmark_self_report.write_agreement`, draw the audit sample (Task 8), hand `coder-A.csv` and `coder-B.csv` to two independent readers (agents are acceptable if they do not share context), adjudicate, run `benchmark_coding_agreement` on the why-not codings of the same two readers.
- [ ] **Step 5: Remaining blocks in order** (closed-k1, open-k1, ablations-k1, execagent-k1), each with step 3.
- [ ] **Step 6: `paper/make_tables.py` and `paper/make_figures.py`**; read every table against `stats.md` once more; `anonymize_campaign.py` into `paper/replication/`; verify the forbidden-string check passes.
- [ ] **Step 7: The campaign README's "Limits" names, at least**: the evidence mode of the ExecutionAgent arm; the clients' versions and their known stop modes (Claude Code `-p` session end, OpenCode client stops) with counts from this campaign; the review-required tier's evaluator groups being `unavailable` by design; the test-count parity coverage; what the audit sample does and does not establish.

### Task 13: SAG on an external benchmark

**Files:**
- Create: `scripts/benchmark_external_tasks.py` (+ test `tests/test_benchmark_external_tasks.py`), `output/paper-external-<date>/` (its own campaign directory with the same `campaign.py` / `readout.py` / `README.md` shape)

**Interfaces:**
- Produces: `envbench_tasks(split_jsonl: Path, *, build_tool="maven") -> list[dict]` turning an EnvBench JVM row (`repository`, `revision`) into SAG's task shape (`repo`, `sha`, one step `["mvn", "-B", "compile"]`, `java_major` from the POM when declared else 17); `swebench_live_tasks(rows) -> list[dict]` turning a SWE-bench-Live MultiLang Java row (`base_commit`, `rebuild_cmds`, `test_cmds`) into tasks with those commands as the pinned steps; `external_sample(tasks, n, seed=20261026)` stratified by repository size.

Candidates from the survey:
- **EnvBench, JVM split, Maven subset** (JetBrains; ICLR 2025 workshop): 268 Maven repositories of 665 JVM; metric `mvn compile` exit 0; best published JVM result 29.47% (GPT-4o Bash agent); code MIT, dataset licence unstated; no overlap with the 52 projects. Expected 250 to 350 machine hours for all 268 at a 2 h budget; a stratified sample of 120 fits a week.
- **SWE-bench-Live MultiLang, Java split, setup side** (Microsoft; RepoLaunch, arXiv 2603.05026): 160 instances over 64 repositories on Hugging Face (MIT), each with `rebuild_cmds`, `test_cmds`, `FAIL_TO_PASS` / `PASS_TO_PASS` and a reference image; the closest analogue to the pinned-command protocol; overlap: apache/rocketmq (4 instances). Deduplicated to 64 repositories: about 60 to 130 machine hours.
- Appendix only: ExecutionAgent's 10 Java projects (no commit pins in the artifact; rocketmq and commons-csv overlap).

- [ ] **Step 1: Write the failing tests** (an EnvBench row becomes a task with one `mvn -B compile` step and the pinned `sha`; a SWE-bench-Live row's `rebuild_cmds` then `test_cmds` become ordered steps; the sample is deterministic and covers every size stratum).
- [ ] **Step 2: Run to verify they fail.** **Step 3: Implement.** **Step 4: Run to verify they pass.**
- [ ] **Step 5: Decision by Chenhao**: EnvBench-JVM Maven sample of 120 (the default; reports against the 29.47% baseline under their metric and ours), or SWE-bench-Live Java deduplicated to 64 repositories, or both if the dates hold.
- [ ] **Step 6: Run SAG once per task on the primary model**, score with our evaluator and with the benchmark's own metric; the campaign README states both and the benchmark's revision (EnvBench split file hash; the Hugging Face dataset revision for SWE-bench-Live).
- [ ] **Step 7: Commit the scripts and tests** (`git commit -m "feat(benchmark): EnvBench-JVM and SWE-bench-Live Java tasks in SAG's task shape"`).

## Decisions

Made on 2026-10-26: the three models (`gpt-5.4-mini`, `gpt-6-luna`, local `qwen3.8:27b-nvfp4`), each run once in every arm, the primary model three times on the original three arms; the SAG freeze is the newest `main` after Tasks 1 to 5; a technical validation run of all 52 projects precedes the formal matrix; more baseline agents and one external benchmark are in scope.

Still Chenhao's:
1. Which added arms stay after the Task 4b spike (recommended: Codex CLI and Qwen Code, plus OpenHands or mini-SWE-agent as the published-baseline control).
2. Whether a replay-verified ExecutionAgent column is acceptable if in-container observation proves impossible (Task 4a, step 2).
3. The external benchmark and its sample size (Task 13, step 5).
4. What to cut if the 2026-12-15 experiment window does not hold (Task 3, step 7 names the order).
