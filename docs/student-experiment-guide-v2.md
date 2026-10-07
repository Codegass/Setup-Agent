# SAG harness experiment guide

Student protocol update • 23 September 2026 • requirements v2

## Start with protocol qualification

Use the accompanying five-project kit to qualify the recorder and scorer before launching a new comparison of SAG, Claude Code and OpenCode. SAG is a harness: the experiment compares how harness design helps a fixed small model complete setup, including build and test. A hosted gpt-5.4-mini experiment does not establish results for locally deployed models.

This update builds on Billy Ye's “Experiment setup and current state”, supplied as `2026-09-22-experiment-setup.docx`. Statements about Billy's runs, machine and runner below are attributed to Billy; they have not been independently rerun here. The new verifier results come from replaying archived evidence. No new agent calls or project builds were made for this update.

### What the five archived fixtures establish

| Project | Local requirements passed | Official assessed tests | CI scope comparison |
|---|---|---|---|
| Commons DbUtils | 8 of 8 | 523 | Met |
| Commons CSV | 23 of 23 | 974 | Met |
| Commons DBCP | 22 of 23 | 1,596 | Not met because Javadoc failed |
| Gson | 51 of 51 | 4,884 | Met |
| Google Java Format | 21 of 21 | 1,625 parsed | Unavailable under its old admission rule |

These are historical protocol fixtures from different harness versions, not five new controlled attempts and not a success-rate estimate. Local test totals match the selected CI pools for the first four. Case identity equivalence is unavailable for all five in this interface. GJF retains `comparison_admitted=false`; its parsed counts are not an official comparison score.

### Keep the cohorts separate

Billy reports that his release-tag cohort completed 20 projects across three harnesses using an earlier class/count protocol. Preserve those results under their original definition. The previous pinned 20-project v1 bundle and the historical SAG 18-of-20 headline also remain historical. This kit contains five v2 tasks; its `historical-cohort-v1.json` is an inventory, not a ready 20-task v2 manifest. In particular, the current Gson task is not the original Gson smoke task.

Before a 20-project comparison, freeze all 20 task/requirement identities and their admission decisions, qualify every recorder adapter, and rerun every arm under that single version. Do not replace a task or drop an unavailable attempt after observing an arm's result.

<!-- page -->

## Measure the declared task

`task.json` freezes the repository commit, ordered commands, working directories and runtime constraints. `requirements.json` freezes the actual obligations: compilation, tests, packaging, installation, documentation and quality checks. Its obligations take precedence over the old broad `stages` labels. A successful task satisfies every applicable requirement and precondition with bound evidence.

| Measure | Definition and interpretation |
|---|---|
| Task success | Complete tasks divided by all planned tasks. Also report incomplete and unavailable counts. |
| Build and test results | Report each project's build and test groups separately, with passed and required counts. A group is complete only when every required member passes. Documentation and quality remain mandatory overall. |
| Requirement coverage | Passed obligations divided by applicable frozen obligations within a project. Do not pool modules or obligations across projects to weight large projects more heavily. |
| CI scope | Passed frozen requirements divided by the official matched requirement scope, only after reference validation and frozen comparison admission. |
| CI test counts | Preserve reported, skipped, assessed, passed, failures and errors per test pool. Assessed equals reported minus skipped. Count equality is not case identity equality. |
| Autonomous success | Task complete and a trusted, complete intervention record observes zero task-affecting human interventions. Unknown intervention count is not zero. |
| Efficiency | All actor, advisor, compression and retry tokens plus elapsed time for successes, failures and timeouts. Report incomplete usage as a known sum with missingness, not a complete total. |

An observed failure or violated precondition makes the task incomplete. Without a confirmed failure, insufficient mandatory evidence makes it unavailable. Requirement states are `passed`, `failed`, `not_run` or `unavailable`; `not_run` requires an observed skip or a supported complete-log inference. Missing output alone is insufficient. These states must not be collapsed into a single failed percentage.

Class files prove physical compilation output, not that every Java source independently generated a class. JARs are required only where the pinned task declares packaging; `pom` packaging has no primary JAR. An install requirement also needs fresh, matching installed bytes. A test that passed before a later Javadoc failure can remain passed while the overall task remains incomplete.

CI count examples are 985 reported minus 11 skipped equals 974 assessed for CSV; 1,605 minus 9 equals 1,596 for DBCP; and 4,906 minus 22 equals 4,884 for Gson. Excess local occurrences trigger a scope conflict, not a better-than-CI score. Gson's source-proven empty Surefire selection is excluded without pass credit; its real integration tests remain mandatory.

<!-- page -->

## Freeze the comparison before running

Use the same selected task and requirement digests, model service and resolved model settings, base image digest, CPU architecture, resource limits, initial cache policy and network policy for every arm. Freeze agent and runner versions, prompts, tool permissions, advisor settings and stopping rules. Preserve execution order or randomization and planned repetitions in the campaign manifest. One repetition per task and arm is a pilot, not an estimate of model variability.

Billy proposed 7,200 seconds, 4 CPUs and 12 GiB per attempt on his 4-CPU, 16-GB x86_64 Docker VM. These are proposed pilot limits, not thresholds derived from CI or a guarantee that every project fits. Confirm headroom before preregistration; use the same limits for every arm. Run one project at a time. Monitor free host/container disk, container memory peak and OOM events. Archive evidence before removing that attempt's resources. Never silently give one failed arm more resources or retry only its difficult tasks.

The measured interval starts when the agent receives its task in a ready pinned checkout and ends after acceptance recording, including setup, advice, retries and observer preparation. Provision the common harness/image before this interval and report it separately. Charge acceptance to the remaining common deadline; do not grant a second 7,200-second budget after agent termination. Retain timeout, OOM and infrastructure-failure outcomes and any missing evidence.

### Protect the measurement boundary

The trusted controller owns task definitions, official CI archives, record directories and scoring. Give each agent the task brief, checkout and required commands/runtime constraints. Keep CI results, expected scores, old transcripts, other arms' workspaces and fixtures outside its readable tools and mounts. These files are an answer key even when the upstream CI is public. Verify the boundary using an actual read attempt before measured runs, not only a claimed configuration rule.

Start from the exact commit with a clean tracked tree and capture worktree evidence before the agent runs. Protected source and build configuration may not change under this task. Preserve both tracked changes and untracked-file inventories at the required boundaries. Untracked files are not automatically disqualifying, but can cause RAT or another real check to fail; retain that outcome and its attribution. Do not run blanket `git clean -xfd` after the agent has worked to erase such evidence. Any controller cleanup must be preregistered and identical across arms.

Agents may rehearse commands within the same attempt budget. The controller records the frozen acceptance commands afterward with fresh before/after observations. Rehearsal outputs cannot be reused as fresh acceptance evidence unless the definition explicitly supports and proves that reuse. An agent's `toolpaths.json` is a path hint; only the recorder's observed launcher versions establish runtime conformity. Keep recorder files outside the tested checkout.

<!-- page -->

## Run the portable recorder and scorer

The kit requires Python 3.11 or later and Git; the scorer uses only the Python standard library. Real acceptance additionally needs the task's Java and build tools. Extract to a controller directory outside all measured checkouts. From the extracted directory run:

```sh
python3 -I verify_bundle.py
```

This checks packaged hashes, independently parses the five CI references, replays five archived records and checks that missing attempts stay in the denominator. It does not run Java, contact a model, test a student's launcher or authorize CI admission for GJF. The ZIP's published SHA-256 is the external integrity anchor; an internal checksum file is not a signature.

For each prospective attempt, set absolute paths and a unique run ID. The following example is for the one-step DbUtils task. Set `REMAINING_SECONDS` from the controller's common deadline and `JAVA_HOME_ACTUAL` and `MAVEN_BIN_ACTUAL` from observed paths. `MAVEN_BIN_ACTUAL` is the absolute path to the `mvn` executable, not its directory.

```sh
KIT=/absolute/path/to/extracted-kit
P="$KIT/projects/commons-dbutils"
CHECKOUT=/absolute/path/to/pinned-checkout
RECORDS=/absolute/path/to/new-record-directory
RUN_ID=commons-dbutils-arm-repetition
export PYTHONPATH="$KIT"
```

Before handing the checkout to the agent, invoke the recorder with a fresh record directory. Run the agent between `start` and `step`; the recorder does not launch the agent.

```sh
python3 -m sag.benchmark.recorder start \
  --task "$P/task.json" --requirements "$P/requirements.json" \
  --repo-root "$CHECKOUT" --records "$RECORDS" \
  --run-id "$RUN_ID" --agent YOUR_ARM
# Run the configured agent under the trusted controller here.
python3 -m sag.benchmark.recorder step \
  --task "$P/task.json" --requirements "$P/requirements.json" \
  --repo-root "$CHECKOUT" --records "$RECORDS" --run-id "$RUN_ID" \
  --step-id ci-step-1 --timeout "$REMAINING_SECONDS" \
  --java-home "$JAVA_HOME_ACTUAL" --maven-bin "$MAVEN_BIN_ACTUAL"
python3 -m sag.benchmark.recorder close \
  --task "$P/task.json" --requirements "$P/requirements.json" \
  --repo-root "$CHECKOUT" --records "$RECORDS" --run-id "$RUN_ID"
python3 -m sag.benchmark score \
  --task "$P/task.json" --requirements "$P/requirements.json" \
  --run "$RECORDS/run.json" --evidence-root "$RECORDS" \
  --ci-source-root "$P" --output "$RECORDS/score.json"
```

For another task, read its ordered `steps` and record every required command with its own remaining budget and required runtime. Never substitute a simplified command, add skipTests, or trust the recorder CLI's own exit code as task success. Inspect the score. Close and preserve interrupted attempts; never fabricate a late task-start snapshot.

<!-- page -->

## Configure SAG and preserve telemetry

The kit includes `harness-source.tar.gz` with the current source files and a per-file digest manifest. Use this implementation snapshot when qualifying SAG. The base Git commit alone does not identify these files because the source checkout contains uncommitted work. The archive is source, not a prebuilt Docker image; dependencies and image digest still need pinning. `sag-mini.env.example` contains a proposed mini-only pilot configuration and no credentials.

SAG uses an actor, an optional advisor, phases and physical verification. Some configuration keys still use legacy ACTION and THINKING names. Explicitly pin gpt-5.4-mini for the actor and advisor; do not let a default gpt-4o or Terra enter this comparison. Export the resolved configuration, model endpoint identity, tool schema and prompts with secrets removed. Count advisor and compression calls in SAG's cost. The same-model high-effort advisor proposal is a treatment setting, not evidence that all archived fixtures used that setting.

For SAG's native requirements adapter, run from the extracted harness source after setting its environment. Remove the portable scorer's PYTHONPATH from this shell so it cannot shadow the full SAG package.

```sh
unset PYTHONPATH
uv run sag project REPOSITORY_URL --ref FULL_COMMIT_SHA \
  --acceptance-task-file "$P/task.json" \
  --ci-target-file "$P/ci-target.json" \
  --requirements-file "$P/requirements.json" --record
```

Read the repository URL and full SHA from the packaged task, not from a release tag or branch head. Supply all three definition files. Inspect the emitted `benchmark_analysis` and the bound requirements evidence. The sealed SAG verdict and the benchmark sidecar are distinct outputs. Historical seals are not rewritten by a new analysis.

For the cross-harness comparison, prefer the common portable acceptance controller for all arms. If using SAG's native adapter instead, qualify that adapter against the same scorer and demonstrate equivalent task, evidence and time boundaries first. Do not run both acceptance paths in the same scored attempt and then select the better result. Billy's `run_sag.sh` and tool-path handoff were not available in this checkout and are not certified by this package.

### Record autonomy and cost prospectively

The portable recorder observes acceptance-command boundaries, not human input during agent work; it deliberately emits `telemetry: null`. Its `start/step/close` CLI alone cannot claim autonomous success. A trusted campaign runner must register an intervention policy before launch, disable stdin, record allowed external actions and close the ledger at process termination. The package includes `sag.benchmark.intervention_protocol`; the runner must bind its records to the actual run. Unknown or absent coverage remains unknown. Headless execution alone does not prove zero interventions.

Use request-level usage records for actor, advisor, compression and retries. Include cached input tokens once in total input, reasoning tokens once within completion, and wall time on failures and timeouts. Missing usage or opaque provider retries leaves a known partial sum. Verify the student's runner emits these records before interpreting token efficiency or autonomous success. Do not backfill zero interventions or tokens into historical records.

<!-- page -->

## Decide whether the pilot is ready

Before a live pilot, archive the exact kit digest, harness source digest, resolved arm configurations, trusted runner code, planned run manifest and image digest. Verify the pinned checkout and tool permissions. Confirm that the controller enforces the common deadline, captures disk and memory observations, and closes interrupted attempts. No live student launcher was run during preparation of this update.

After the first live attempt, replay its raw evidence from a separate directory using the exported scorer. Check command and runtime binding, tracked and untracked worktree observations, physical outputs, fresh test pools, invocation linkage and CI source closure. Also inspect the complete intervention ledger and request accounting. If these records are missing, repair the recorder integration before expanding to five or twenty projects. A project need not pass to validate the recorder: a well-evidenced failure is useful data.

Keep one immutable directory per planned attempt. Include task and requirement definitions, run identity, raw invocation logs, receipts/runtime probes, worktree snapshots, class/artifact inventories and retained artifact bytes, test reports, official CI sources, resource observations, intervention records, usage records and the final score. Preserve failures and unavailable records. Keep credentials out of the shared archive. Historical fixtures in this kit contain local path strings for provenance; those strings are not paths to follow outside the archive.

When aggregating the five-task pilot, include all five frozen definitions and every planned slot. The GJF slot remains in local task reporting but cannot enter an admitted CI-comparison denominator. Keep cohort size, admitted-reference coverage and per-metric missingness visible. This package supports one result per project in a single arm/repetition; aggregate separate arms and repetitions separately.

```sh
python3 -m sag.benchmark aggregate \
  --requirements-dir "$KIT/requirements" \
  --scores /absolute/path/to/arm-repetition/*/score.json \
  --output /absolute/path/to/arm-repetition-summary.json
```

### Changes to Billy's setup note

Billy reports that his earlier three-harness cohort, path migration, answer-key access rules, volume-performance fix and synthetic runner checks are complete. Preserve those as Billy's reported engineering history, not as validation of this v2 package. His earlier source revision and 18-of-20 reference are not the new implementation identity or a new baseline score.

This update replaces the v1 class/count interpretation with frozen obligations and verified CI scope; removes the claim that more tests is automatically better; requires prospective evidence for zero interventions; and avoids post-agent cleanup that can hide introduced files. It also distinguishes an observed protected-source/runtime violation from missing evidence. DBCP's Javadoc failure stays recorded without forcing a repair. GJF's old CI restriction stays in force even though its native counts can be parsed.

The immediate handoff is a qualified evaluator and five archived fixtures. The next gate is one prospectively recorded live attempt through the student's controller, followed by the five-project pilot. The 20-project campaign starts only after its definitions and every arm's recording path pass the same checks. Do not mix old and new runs to fill that campaign.

### Source files

Consult `manifest.json` for every project's exact commit and selected CI URL/cell; `projects/<id>/task.json` for commands and runtime; `projects/<id>/requirements.json` for obligations and admission; `PROTOCOL.md` for the evidence rules; and `fixtures/<id>/expected.json` for the archived replay result. Every packaged byte is listed in `checksums.json`. New task definitions, admission changes or protocol revisions require a new version and a labelled new analysis.
