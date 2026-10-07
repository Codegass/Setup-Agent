# CI requirements v2: recording and offline analysis

This protocol evaluates a **frozen CI task**, not an agent's self-reported phase.
It is an additive analysis layer. Existing SAG verdicts, portable v1 scores and
the original September 17 ZIP retain their original meanings and bytes.

## Inputs and identity

Each attempt freezes the canonical acceptance task, a `requirements.json`, and
the `ci-requirements-v2` policy. The canonical digests bind the repository commit,
ordered commands and mandatory requirement definitions. The raw input file
digests are also retained by the formal campaign. Requirements replace the old
manifest's `stages` for v2 analysis. Do not change the commands to make a failing
run pass.

Requirement kinds overlap: compile, package, install, test, documentation,
quality_check, native_compile. Unresolved work remains unclassified. A task is
complete only when every required command, requirement and worktree/runtime
precondition is proven. A confirmed failure makes it incomplete; otherwise
missing or conflicting evidence makes it unavailable. No applicable requirement
means not applicable, never a free pass. Test counts do not prove case identity.

`annotation_completeness=review_required` is a preparation artifact, **not a
campaign-ready task**. The initial 20-project export now includes reviewed
definitions for DbUtils, Commons Net, Commons CSV, Commons DBCP, Tomcat Migration,
Sling MIME, Sling OSGi, Tentacles, HttpClient, HttpCore and Commons JCS
(11 of 20). All 19 Maven projects have captured effective models;
the remaining 9 task definitions retain explicit execution-plan/output-scope
gaps and known positive labels only. Inspect each
file's status. This is not a new 20-project experiment and cannot upgrade history.
Individual known outcomes remain useful, but incomplete annotations cannot
certify an entire Build/Test category or prove that a category is absent.

Effective models and tracked POM files are metadata inventories, not observed
CI module denominators. Curator resolves 13 models for its first command and
9 for its second; 22 counts appearances across steps, not distinct modules.
The offline inventory derives ordinary artifact defaults and lists custom
packaging/attached-output exceptions without making a definition complete.

## Execution ownership (2026-09-26 correction)

The primary benchmark asks whether the agent **and its own harness** execute
the frozen task. An external controller must not start missing commands after
the agent exits and attribute those results to autonomous completion. The old
three-harness campaign did exactly that; its original scores remain frozen and
describe post-agent environment acceptance, not necessarily agent execution.

New primary evaluation uses `agent-execution-v1`: both the run and every selected
invocation must have trusted-recorder `execution_origin: agent`. Invoke the
offline scorer with `--required-execution-origin agent`. Unknown, mixed or
independent-replay origins cannot satisfy this boundary. An origin label is
recorder provenance, not a replacement for command, runtime, worktree, artifact
or test checks. The agent cannot assign its own success through its final prose.

`scripts/run_portable_harness.py` no longer starts build/test commands after agent
exit. Its `score.json` is a primary execution evaluation and
`primary-evidence.json` identifies the evidence base and byte-bound run record.
SAG uses its authorized native receipt adapter. Baseline execution-capture
uses native Claude hooks / an OpenCode plugin, with a host-owned passive observer.
The original tools, arguments and responses are retained. An actual Maven launch
callback binds resolved argv, cwd, environment and PID before the JVM starts;
a process-only trace observes Maven's OS exit. The observer does not trace the
LLM client. Byte-bound Maven session records supply the actual JVM/Maven identity,
and the existing physical observer supplies worktree, class, artifact and test
evidence. Collector version/guarded metadata probes are shared with SAG, are
charged to the execution window, and never run a build/test lifecycle. The offline
scorer itself only reads sealed records.

A shell returning zero cannot hide a nonzero Maven exit. Claude background
execution must be joined through its native TaskOutput result, with the same task
identity and a terminal OS process. Full native output files are archived when
the tool returns a truncated preview. Retries retain every invocation and select
the final ordered attempt; a later pending retry invalidates an earlier success.
Late notifications cannot mutate a sealed run.

The 2026-09-26 Commons CLI smoke qualification completed on SAG, OpenCode 1.18.32
and Claude Code 2.1.267. It used preinstalled Java 17 / Maven 3.9.16 and establishes
instrumentation coverage, not comparative setup effectiveness or efficiency.
Current baseline coverage is a direct Maven launch with one frozen build command
per native shell call (surrounding shell operations are allowed). Gradle, Maven
wrappers, multiple build launches within one call, overlapping shell execution
and platforms without a verified process tracer remain unqualified. Missing
capture is unavailable, never inferred build failure or success.

Formal baseline admission remains disabled before container creation or model
requests. `--native-capture-qualification` explicitly opts into bounded qualification
only. Do not restart a comparative campaign until the actual task/launcher paths
in that campaign pass qualification. Details and archived results are in the
[qualification report](superpowers/reports/2026-09-26-native-execution-qualification.md).

SAG phase routing, completion claims and physical validation are retained.
Waiting for background work belongs to the harness's execution budget. A process
still running has no successful terminal receipt. Independent build repetition
may be performed separately, but its records, results and time must remain
separate and cannot fill missing primary evidence. The scorer rejects aggregate
tables mixing different execution boundaries.

Historical archives without prospective origin fields can still be evaluated
under their original requirements-v2 protocol by omitting the new option. This
compatibility path does not establish compliance with the new execution boundary.
`scripts/audit_execution_boundaries.py` produces a separate provenance audit;
archived native SAG results and old public-replay scores are distinct columns,
and missing baseline observations remain unavailable.

## Portable independent recording

The exported evaluator uses Python's standard library. Run these commands from
the bundle root (inside the SAG repository use `uv run python` instead).
The trusted runner must keep `RECORDS` outside the agent's checkout and assign a
fresh attempt directory and run ID. Start before launching the agent, then record
each frozen acceptance step in order, then close. The standalone recorder defaults
to `execution_origin: independent_replay`; this sequence measures environment
acceptance and cannot claim autonomous task completion. An early failure remains in
the planned denominator; never combine successful pieces from different retries.

```sh
python -m sag.benchmark.recorder start --task TASK.json --requirements REQUIREMENTS.json --repo-root CHECKOUT --records RECORDS --run-id RUN --agent AGENT
python -m sag.benchmark.recorder step --task TASK.json --requirements REQUIREMENTS.json --repo-root CHECKOUT --records RECORDS --run-id RUN --step-id STEP --timeout REMAINING_SECONDS
python -m sag.benchmark.recorder close --task TASK.json --requirements REQUIREMENTS.json --repo-root CHECKOUT --records RECORDS --run-id RUN
python -m sag.benchmark score --task TASK.json --requirements REQUIREMENTS.json --run RECORDS/run.json --required-execution-origin independent_replay --output RECORDS/requirement-results.json
python -m sag.benchmark aggregate --requirements-dir requirements --scores RECORDS/requirement-results.json --output summary.json
```

The recorder preserves raw Git probes and exit codes, full command logs, actual
launcher probes, fresh reports and declared artifact evidence. Unknown wrapper
inputs, cache provenance and unsupported output scopes remain unavailable.
Untracked files are recorded and do not automatically disqualify a task. An
agent-introduced RAT failure needs file content and write provenance as well as
the rejecting report; a newly observed filename alone cannot establish authorship.

The recorder's three separate commands do not observe all human input between
commands. They cannot claim zero human interventions. Independent replay never
earns `autonomous_success`, even with an observed zero intervention count.
Autonomous success requires agent-owned execution evidence as well as
an independent trusted runner record covering the complete interval and input
channels. Initial task instructions do not count as interventions.

## SAG integration

Formal SAG invocations supply **all three files and the exact commit**:

```sh
uv run sag project REPOSITORY_URL --ref FULL_SHA --acceptance-task-file TASK.json --ci-target-file CI.json --requirements-file REQUIREMENTS.json --record
```

The CLI rejects mismatched or incomplete annotations before Docker work. The
`requirements-v2` campaign checks the archived inputs against the actual run pin
for every arm. Missing inputs are recorded as unavailable planned slots. A smoke
task needs a truthful unmatched CI target, not a fabricated successful reference.

Worktree snapshots are saved in the host session at clone completion, exact
acceptance invocation boundaries and evidence close. Post-invocation snapshots
occur at receipt publication, which can be delayed for detached jobs. Snapshots
do not prove the absence of transient modifications between observations.

The requirements sidecar is separate from the sealed production verdict. A
publication/adapter failure must leave an unavailable analysis, not alter a
historical success definition. Verify the actual exported sidecar before a
campaign, rather than inferring integration from unit tests alone.

SAG sidecars use evidence references relative to their host session directory.
For an offline score recomputation, supply `--evidence-root SESSION_DIRECTORY` to the
`score` command; its `--run` file lives below `requirements-evidence/`.
The scorer never follows an implicit `../../` reference to discover evidence.

For installation requirements, the observer resolves the actual Maven local
repository with a bounded metadata probe and archives its output and duration.
This is separate from the frozen acceptance command. Maven extensions may run
during metadata resolution; this preparation and its time are disclosed. The
recorder keeps produced and installed bytes outside the checkout and binds them
to the invocation. A successful goal with an old artifact is insufficient.

The frozen command may select its tracked `pom.xml` with `-f pom.xml`,
`-f ./pom.xml`, or `--file=pom.xml`. Both recorders check the selected bytes against
the pinned commit. This legitimate option is not treated as an opaque wrapper.
Other unresolved input sources remain explicitly unknown.

Each Gradle JUnit requirement freezes its checkout-relative
`validation.report_directories`. Both recorders retain producer-relative paths;
the common scorer uses directory boundaries, not only a module/test-kind label.
Overlapping physical report directories within one step are rejected across
all module and test-kind labels, including Maven's implicit default directories.
Different steps may reuse a directory only with their own invocation and
freshness evidence. A native `No tests to run.` is not a successful test run.

When a frozen runtime precondition declares `requires_native_image=true`, both
adapters observe the actual launcher JVM home and probe that installation's
`native-image --version`. Java 21 alone, or a binary from another JDK on PATH,
cannot satisfy the constraint. Missing observations stay unavailable; an
observed launch/probe failure is failed. Vendor or patch-version constraints
are not invented. Internal compiler/test toolchains remain distinct from the
launcher constraint and require separate declarations/evidence if evaluated.

The Sling and Tentacles reviews preserve the previously declared `deploy` to
`install` variant. Their original selected-node HTML or timestamped whole-job
log, independent stage/build/index records, exact extracted command and excluded
publication goal are retained. They do not certify deployment.

Reactor reviews bind every module to its pinned POM, effective coordinates and
ordered native goal occurrences. Repeated lifecycle goals remain mandatory;
one logical output may require several ordered occurrences. Surefire's explicit
same-configuration reuse is preserved separately from the first executed test
pool and must also be observed during replay. A second execution cannot be
certified from the first pool's XML. Disabled or inapplicable goals need specific
native or effective-configuration evidence and never become passed checks.

The reviewed Apache Maven only-script wrapper profile binds its exact script,
properties and complete checkout `.mvn` input inventory. Each invocation records
before/after pinned bytes, actual launcher version, startup and environment
inputs; the scorer verifies those raw observations again. Changed or unreviewed
wrappers still execute the frozen command but cannot gain inferred native
completion from an unproven serial execution. This is template support, not a
project-name exception. The older JAR-based Curator wrapper remains unreviewed.

Configuration inputs and generated runtime state have different roles. The
source-reviewed Develocity Maven extension 2.5.0 may create its fixed workspace
identifier under `.mvn`; both recorders preserve its raw before/after contents
without treating a valid generated identifier as an added build option. This
classification requires the pinned producer declaration, reviewed producer
bytes and the exact path/format. A tracked identifier remains immutable input.
Unknown producers, invalid contents, symlinks and other added files still leave
launcher certification unavailable. This rule does not exempt untracked files
from worktree recording or from the project's own checks.

Whisker and RAT retain additional known Invoker integration-test obligations;
RAT also has AntUnit targets. These are different test units from Surefire cases.
Their definition gaps and unsupported evidence rules remain visible; the old
JUnit totals cannot certify the entire test task.

## Prospective intervention and cost records

A formal unattended campaign can declare `sag-unattended-v1` before launch. Its
scope is task-affecting human actions between process start and termination:
initial task instructions are excluded; guidance, manual file/environment edits,
restoration and manual cancellation must be recorded. Stdin is disabled, runner
controls are disabled or logged, and the experimenter commits to prohibiting or
recording external actions. The runner initializes a ledger before launch and
closes it at termination. A valid closed empty ledger supports a protocol-scoped
zero; missing closure, unknown events or missing policy leave the value unknown.
Host-administrator actions outside the protocol are not technically monitored;
this limitation is reported, not described as an enforced isolation boundary.

New SAG runs persist a request ledger before and after each actor, advisor and
compression call. It preserves retries, missing usage, errors and unfinished
requests without recording prompt text or credentials. Campaign analysis uses
this ledger first, falling back to the legacy returned-response CSV only when
the ledger is absent. It never adds the two sources. The known sum is reported
as `known_tokens`.
`total_tokens` remains unknown when missing usage, interrupted exports or opaque
provider retries prevent complete accounting. Reasoning tokens are included in
completion tokens once. Failure and timeout time remain in the cost boundary.
These records do not yet certify a complete token denominator for a paper's
efficiency comparison.

## Reporting and comparison

- Task success: complete tasks / all frozen planned tasks, with incomplete and
  unavailable counts. Missing attempts remain in the denominator.
- Build/test requirement success: project-level aggregates of the applicable
  obligations. Documentation and quality checks remain mandatory overall.
- Overlapping group success: complete tasks in the group / planned tasks in the
  group. Modules, JARs and testcase counts do not give a project extra weight.
- Conditional group pass: one vote per project, only with known satisfied
  external prerequisites and observable passed/failed outcomes. `reached` is
  this adjudicable subset, not every task that physically started. Report
  `prerequisites_satisfied`, reached, not_run, unavailable and unresolved
  prerequisites with the ratio. Independent `started`, `not_started` and
  `started_unknown` counters preserve observed entry even when completion or
  fresh outputs are unavailable. A project starts a group when any required
  native member really starts; false requires every member to be explicitly
  skipped/not reached. Missing evidence remains unknown. Gradle SKIPPED,
  NO-SOURCE, UP-TO-DATE and FROM-CACHE do not establish fresh task execution.
- CI scope/count/case comparisons remain independent. This v2 layer never creates
  missing CI denominators or manufactures case equivalence from equal counts.

CI count admission must declare `reported_count`, `skipped_count` and
`assessed_count = reported_count - skipped_count`, plus passed/failed/errors where
the native source distinguishes them. Retain a combined `failed_or_error_count`
when the source does not distinguish failures from errors. Unknown skipped counts
leave assessed counts unknown. Reconcile native totals with complete case rows;
keep reported identities and non-skipped identities separate, preserving duplicate
occurrences and module mapping. Historical `executed_count`/`executed_ids` include
skipped final rows in the Jenkins importers: they must not silently become an
assessed-test denominator. For example, Commons Net CI has 557 reported, 555
assessed and 2 skipped; the current recorder matches all three. This semantic
check is required before admitting subsequent dataset references. The [archived
count audit](../output/requirements-v2-live-validation-20260922/ci-count-audit.md)
records hashes and the descriptive case comparison; historical records and
existing scores remain unchanged.

Every new formal definition explicitly stores `ci_alignment.test_count_semantics`
as available or unavailable with a reason. Available Jenkins references bind the
selected URL/cell, independent CI index and raw test report by digest and byte
length. Import, campaign relocation and launch recheck this source closure.
An available count describes that report's test pool, not automatically every
test type required by the task. The current 20-task inventory has 10 available
count references; 8 also have complete requirement definitions. Count admission
does not implement a CI attainment or case-identity score. A preregistered campaign
requiring count comparability sets `require_ci_count_comparability=true`; an
explicit unavailable reference may otherwise accompany a separately labelled
smoke task, but cannot contribute a CI comparison denominator.

Maven not_run inference is limited to byte-bound complete serial logs, resolved
same-module occurrences, a confirmed upstream failure and verified fail-fast
effective inputs. Quiet, parallel, unknown wrapper/configuration, forked execution
or continue-on-failure modes prevent this inference. Gradle independent task
results are retained when their completion is observable under `--continue`.

Diagnostic ablations need their own frozen task and requirements identities.
New skipTests variants are diagnostic only and cannot count as full CI task
success. Full-task comparisons use the same versioned criteria and cost boundary
for every agent, including failures and retries.

## Selected official CI verification (September 23)

`sag.benchmark.evaluator.evaluate(..., ci_source_base=SOURCE_ROOT)` now returns
`ci_verification` under the independently versioned
`selected-ci-verification-v2` protocol. This compares an independently recomputed
local result, never an agent-authored score. The CLI accepts `--ci-source-root`;
without it a bound source manifest in the run is used first, otherwise the
supplied requirements file location determines the source root.
SAG archives the required CI/model sources under the host session's
`benchmark-inputs/` before execution. A bound manifest links that archive to the
requirements identity. The final adapter and campaign re-evaluation both reload
it. No recorder files are placed in the measured checkout.

The initial transport supports complete serial Maven commands in GitHub Actions:
repository/commit, selected run/attempt/job, exact cell, independent selection,
ZIP member and raw log hashes/lengths must agree. Commands are selected from whole
runner headings in the frozen order; truncated/repeated commands, parallel output
and changed commands are not admitted. Runtime banners, or contiguous commands
with the same observed launcher environment, must match the frozen JDK/Maven
constraints. Effective POMs and pinned source POMs resolve module paths and names;
ambiguous aliases cannot become missing-module scores. Native goal inventories
must cover the frozen requirements, including documentation and quality checks.

The API keeps three axes separate:

| Field | Meaning | Missing/conflicting evidence |
|---|---|---|
| `scope` | Passed frozen requirements / all frozen requirements, plus Build/Test/Documentation/Quality groups | `unavailable`, no fraction |
| `test_counts` | Per-pool reported/skipped/assessed/passed/failed/error occurrences and assessed/passed ratios | `unavailable`; excess pool occurrences are `scope_conflict`, never "exceeded" |
| `case_identity` | Equivalence of complete officially bound case identities | Unavailable in this transport; equal counts do not establish it |

Test class summaries are not added to their enclosing Results totals. Repeated,
empty, all-skipped, missing or conflicting pool totals are not valid denominators.
Missing artifacts or runtime/worktree evidence still affect local requirements;
a nonzero original command or a Javadoc failure still prevents task completion.
DBCP's Javadoc repair is deferred; the requirement is not removed or forgiven.

The old `ci_scope_attainment` and `ci_test_count_attainment` fields are populated
only when their corresponding new comparison is scoreable. Unavailability now
has an explicit reason in `ci_verification`. A frozen
`comparison_admitted=false` stays false on historical replay even if its native
counts can now be verified. Changing admission requires a new task-definition
identity and a separately labelled analysis/run.

Requirements runs no longer publish the legacy display-name-based CI score in
the sealed production verdict. That field explicitly directs consumers to
`benchmark_analysis`; the local certificate/verdict is retained. The new comparison
is a closed benchmark sidecar, not a mutation of the sealed verdict. Non-benchmark
legacy comparisons also withhold scores when their module namespaces differ and
no source-bound mapping establishes comparability.

The Jenkins transport also binds a complete serial `MavenModuleSetBuild` to its
independent build API, SCM revision, checkout, workspace, exact Maven invocation,
observed launcher versions, final console result and native test report. It
normalizes Jenkins's absolute `-f` argument only against the observed workspace.
Report child builds and the build API counters must agree with the selected job.
This does not certify arbitrary Jenkins pipelines or matrix fan-out.

The verifier reuses the importer's existing source review for an empty default
Surefire selection. It verifies the complete pinned Git tree, source blobs,
effective configuration, plugin sources and compiler-discovery evidence again.
Gson's shrinker Surefire invocation can therefore be excluded with a reason; its
real Failsafe tests remain required. An empty invocation receives no pass credit
and contributes no test denominator. The rule is based on bound evidence, not a
project-name exception. Incomplete proof still leaves comparison unavailable.

This is not universal CI support. Gradle/native tasks and full case identity
comparison remain explicit gaps. The legacy count-admission
preflight is not automatically upgraded by this parser; prospective comparable
campaigns still need properly frozen admission and count metadata.

The five-subject archived replay is saved at
`output/ci-verification-completion-20260923/analysis-02/` (the preceding
`output/ci-verification-repair-20260923/analysis-02/` remains unchanged).
It changes no original run,
requirement denominator, acceptance command or model. It verifies measurement
behavior, not an improvement in agent setup success rate.

## Historical replay

Replaying a v1 archive under v2 never invents requirements pins, clean-worktree
snapshots or an intervention zero. An unavailable v2 certification means that
the new evidence contract cannot be verified from that archive. It does not
mean the old build was rerun and failed. Historical verdicts and raw XML totals
are retained separately; report inventory is not freshness or scope proof.

## Reviewed Maven evidence and online phase completion

The requirements observer may publish `test_scope_completed` when the exact
frozen command's complete, fresh report pools and returned native test goals
all pass the independent evaluator's rules. The same receipt must prove its
runtime and execution inputs. This can establish test completion when a later
documentation or quality goal fails. It does not change that command's exit
code, the build verdict, or overall task completion. Missing/interrupted report
collection and tests spread across multiple commands are not discharged by a
single-command certificate. Online phase feedback preserves the reason when
completion remains unknown.

Additional source reviews are explicit frozen input data:

- A nonconventional Maven goal prefix comes from the byte-bound plugin JAR's
  `META-INF/maven/plugin.xml`, whose coordinates must match the effective POM.
  A literal native prefix ending in `-plugin` is not automatically shortened.
- A generated consumer POM is associated with its observed producer and exact
  installation coordinate. Both native producer and install completion are
  mandatory. The installed POM remains an artifact obligation; a temporary
  checkout POM need not survive JVM shutdown merely to count as packaged.
- A direct Maven `.mvn/jvm.config` can be reviewed at its pinned commit, with
  exact bytes, literal tokens and a disposition for every system property.
  Both recorders compare before/after inputs. This does not permit arbitrary
  environment overrides, changed files, nested argument files or shell expansion.
- Explicit `compilation_reuse` reviews bind an earlier production/test compile
  obligation to a later matching obligation in the same task. Four boundary
  observations retain the reviewed input trees and physical class inventories.
  The earlier command must actually compile, produce fresh class outputs, and
  match the later observed compiler/runtime. Input/output content must remain
  unchanged from producer exit through consumer exit; the current native
  compiler must independently return its up-to-date decision. Missing evidence
  stays unavailable. This proves continuity of the declared scope plus the
  compiler's decision, not a complete transitive dependency model or arbitrary
  external-cache provenance. Each project's source/configuration scope requires
  review before admission. Gradle caches and cross-run reuse remain unsupported.

These changes require a new implementation snapshot and prospective run. An old
archive cannot acquire unobserved compiler boundary inventories retroactively.

The installation recorder resolves the actual Maven local repository before
capturing artifact freshness. `help:evaluate` may initialize project extensions
and resolve cold dependencies, so it must not inherit the 30-second version
probe limit. The portable recorder charges preparation against the remaining
step budget; SAG uses the requested tool timeout (the public 600-second default
when omitted), with preparation included in campaign wall time. SAG's transport
retains its existing 120-second overhead allowance outside the probe deadline
to return timeout evidence. Probe budgets and observed durations are archived.
Timeout or a missing repository observation leaves installation requirements
unavailable; neither a default `.m2` guess nor command exit zero repairs the
missing before/after evidence. Metadata resolution can warm dependency caches
inside an attempt and is included in that attempt's setup cost.
