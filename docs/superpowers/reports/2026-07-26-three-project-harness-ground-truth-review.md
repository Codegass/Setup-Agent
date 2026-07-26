# Three-Project Harness Ground-Truth Review

**Date:** 2026-07-26  
**Scope:** commons-cli, Apache Bigtop, Apache TVM  
**SAG code under test:** `4b5ad6f40d30dfe1b78b144444072e8f12fdc888`  
**Current main:** `86c864b` (only acceptance/report changes after the tested code)  
**Action/advisor model:** `gpt-5.4-mini`, temperature `0`  
**Prompt bundle:** `aca9a9f3d401d5eb17450748572b32ea1e3393b414cab42d7c893ab9dc24147c`  
**Docker image ID:** `sha256:1f701c2d4555be2b976cb1846aaf0d73c955ccdc143c0bce5581ffbac7705feb`

## Executive verdict

The user's concern is confirmed: the current failure analysis cannot be
reduced to whether three project-specific probes pass. The three runs expose
general harness defects in five shared boundaries:

1. survey facts are promoted into claims the survey never proved;
2. the build facade changes the semantic meaning of the model's action;
3. evidence from different domains and attempts is aggregated without scope;
4. phase outcome is used as a proxy for artifact or capability state;
5. gate, finalizer, and report do not preserve one coherent verdict algebra.

The weak model is not blameless, but it is not the primary cause:

- On commons-cli, the harness supplies a structured Maven version constraint
  plus concrete activation/retry call shapes; the same model correctly
  chooses Maven 3.9.9, downloads it, activates it, and retries successfully.
- On Bigtop, the model follows the harness's incorrect island map, then
  reports the observed failures more honestly than the phase gate and final
  report.
- On TVM, the prompt and advisor explicitly give the model the false fact
  that the native core was not built. The model repeats that fact instead of
  independently reverse-engineering LLVM, CMake, environment overlays, and
  the project's CI dependency policy.

That last behavior is a limitation of a small model, but absorbing exactly
that complexity is the purpose of a weak-model harness.

The projects also contain real problems:

- Bigtop's pinned checkout has stale 3.5/3.6 consumer coordinates against a
  3.7 producer; after a diagnostic 3.7-as-3.6 alias, Spark test compilation
  reaches a missing API; parts of test-framework require host capabilities
  and its broader integration suites require external services or a cluster.
- TVM's pinned checkout has a root `apache-tvm-ffi>=0.1.13` requirement whose
  pinned submodule reports `0.1.13.dev47`, and its unconstrained NumPy
  dependency resolves to a version incompatible with the LLVM smoke.

Those project defects explain why a truthful result need not be globally
green. They do not excuse the harness for reporting false build facts,
polluted test counts, or vacuous capability receipts.

No product fix was made in this review. This report is the decision boundary
before implementation.

## Re-grading the current acceptance claim

The existing Plan 4 report's `23/23` assertions prove a narrower property:
TVM's first test call is bounded and no longer sweeps 11,702 tests. That is a
real improvement. They do not prove native capability closure or overall
harness correctness.

| Claim area | Revised grade | Reason |
|---|---|---|
| TVM full-sweep prevention | **PASS** | The first bare call uses the surveyed three-test smoke. |
| TVM capability receipt | **FAIL** | `3 executed / 3 skipped` mints a receipt despite zero positive capability evidence. |
| TVM native diagnosis | **FAIL** | Five native libraries exist, but prompt/advisor/report say the core was not built. |
| Bigtop primary test scope | **FAIL** | Reported `54/54` includes four auxiliary test-framework passes; the primary is `50/50`. |
| Bigtop build truth | **FAIL** | Scala `compileJava NO-SOURCE` becomes success; the correct test-framework build is reported failed. |
| Commons recovery | **PASS** | A structured version constraint and concrete activation/retry call shapes let the model select Maven 3.9.9 and complete. |
| Cross-layer report truth | **FAIL** | Domain failures, artifacts, skip semantics, and runtimes are lost or contradicted. |

The acceptance verifier at
`scripts/verify_native_test_policy.py:104-140` checks filtered scope, path,
bounded selection, and then trusts `smoke_receipt_written`. It does not test
the second bare call, all-skipped behavior, receipt staleness, or native
repair. The conclusion in
`docs/superpowers/reports/2026-07-26-plan4-machine-verified-acceptance.md`
therefore exceeds what its assertions establish.

This was re-run during the review:

```bash
UV_CACHE_DIR=/tmp/setup-agent-uv-cache \
  uv run python scripts/verify_native_test_policy.py \
  logs/session_20260726_153134_67903 --profile tvm
```

It still reports `7 passed, 0 failed` against the live all-skipped receipt,
confirming the verifier blind spot rather than merely inferring it from code.

## Method

### Fixed comparison pins

| Project | Target SHA | Recorded session |
|---|---|---|
| commons-cli | `afb0fd148517b1bf8316ebbc44ec9ec8b201452a` | `logs/session_20260726_153129_67815` |
| Bigtop | `e32423c444a9311b802946d5b695767a9b921e1e` | `logs/session_20260726_153131_67860` |
| TVM | `828d117ebdb90e4474e5b7a9ead4e88b35865a58` | `logs/session_20260726_153134_67903` |

Manual checks used the exact image ID recorded above and the same target
commits. The retained reference containers are:

- `commons-cli-groundtruth-20260726-163315`
- `sag-ref-bigtop-e324`
- `sag-ref-tvm-gt-828d117`

The run pin calls its image field `container_image_digest`, but its value is
the local image ID. The actual repository digest is
`ubuntu@sha256:786a8b558f7be160c6c8c4a54f9a57274f3b4fb1491cf65146521ae77ff1dc54`.
This is a provenance-label defect, not a cause of the project failures.

### What “the model thought” means here

The recorded sessions contain almost no private reasoning text; most model
response bodies are zero characters. This review therefore does **not**
claim access to hidden chain-of-thought.

The model's observable decision basis is reconstructed only from:

- the rendered phase objective;
- advisor output;
- ordered tool calls and arguments;
- tool-visible observations;
- the model's phase-close summaries;
- gate responses and the final report.

This is enough to establish causal responsibility without inventing an
unobservable rationale.

### Manual-build rule

The manual runs distinguish three states:

1. **works on the original checkout**;
2. **works after adding a documented environment capability**;
3. **works only after a diagnostic mutation**, which proves the root cause
   but is not a valid SAG success path.

Bigtop's 3.5/3.6 local aliases belong to state 3. They were used only to
separate coordinate staleness from API incompatibility; no repository source
was changed.

## Ground truth at a glance

| Project | Correct Docker result | Project-intrinsic remainder | Current SAG distortion |
|---|---|---|---|
| commons-cli | Full success; 982 reported, 921 pass, 61 skips with explicit legacy-parser reasons, 0 fail/error; four JARs | None blocking | Missing ToolResult totals forces manual XML parsing; skips lower “pass rate”; report says `None JARs`. |
| Bigtop | Truthful partial; data-generators `50/50`; test-framework packages successfully; two consumers have stale coordinates; Spark test compilation fails after a diagnostic alias | 3.5/3.6 vs 3.7 mismatch; host-conditioned and broader integration tests; Spark's next-layer cause is not fully isolated | Reports polluted `54/54`, false Scala compile success, false test-framework build failure, and “independent” domains. |
| TVM | Native build succeeds; with documented LLVM + NumPy 1.26, smoke is `1 passed / 2 project-defined skips` | tvm-ffi pre-release metadata mismatch; NumPy upper-bound omission | Says native core was not built, drops five `.so` artifacts, mints receipt from all-skipped smoke. |

## commons-cli: the causal control

### Manual Docker truth

With OpenJDK `8u492` and Maven `3.9.9`:

```bash
JAVA_HOME=/usr/lib/jvm/java-8-openjdk-arm64 \
  /opt/apache-maven-3.9.9/bin/mvn --fail-at-end compile

JAVA_HOME=/usr/lib/jvm/java-8-openjdk-arm64 \
  /opt/apache-maven-3.9.9/bin/mvn --fail-at-end test
```

Results:

- compile: exit `0`, about 18 seconds, 56 main `.class` files;
- tests: exit `0`, about 12 seconds;
- 982 selected/reported nodes, 921 passed, 0 failed, 0 errors, 61 skipped;
- 47 Surefire XML files, 59 test `.class` files;
- four project JARs;
- clean worktree.

The 61 skips have explicit legacy-parser or parameterization reasons:

- 27 BasicParser unsupported cases;
- 22 GnuParser unsupported cases;
- 10 PosixParser unsupported cases;
- 2 cases already covered by parameterized execution.

A no-argument Maven build also succeeds under UTF-8 and runs RAT, coverage,
binary compatibility, style, static analysis, and Javadoc. Without UTF-8,
only Javadoc encoding fails. SAG already sets UTF-8, so this is a positive
harness feature.

Maven `3.8.7` correctly fails the project's Enforcer requirement `[3.9,)`.

### What the model did

1. It used the surveyed root and Java 8.
2. Maven 3.8.7 was rejected.
3. The harness supplied the `[3.9,)` constraint, a generic download/unpack
   recovery direction, and concrete activation/retry call shapes.
4. The model itself selected Maven 3.9.9, chose the Apache download location
   and local path, registered it, and retried successfully.
5. The test command succeeded.
6. Because the ToolResult lacked structured totals, the model spent seven
   additional actions finding and parsing XML: a failed search, `find`, a
   broken grep, a missing `python` executable, a regex that returned zeroes,
   and finally a successful Python 3 ElementTree parse.

This run is the strongest control in the study: the same weak model can
combine a truthful version contract with concrete tool-call shapes and
successfully fill in a missing tool version, URL, and local path.

### Harness defects exposed by a successful project

`src/sag/tools/internal/maven_tool.py:506-519` analyzes the bounded inline
`result["output"]` before assigning the available untruncated `full_output`.
The same bounded output is passed to the command tracker at lines 643-650 and
to `_record_test_summary` at lines 716-718. The roughly 605k-character Maven
output contains the aggregate Surefire summary in its omitted middle, so
`test_stats` is absent even though the runner has the truth.

`src/sag/agent/physical_validator.py:4134-4153` calculates
`passed / total`, where total includes skipped tests. This mixes correctness
with applicability and produces a 93.8% warning despite zero failures and
errors.

The phase gate initially treats that ratio as partial, while the sealed
finalizer/report ultimately returns success. The report also hardcodes
`jar_files: None` at `src/sag/tools/report_tool.py:1590-1591`.

Correct semantics:

- **correctness:** 921 applicable tests passed, 0 failed/error — success;
- **skip/applicability:** 61 reported skips with reason categories — separate
  from correctness;
- **breadth:** 982 selected/reported nodes — informational;
- **artifacts:** four JARs — present.

## Bigtop: a real partial result reached through false evidence

### What the model was told and what it did

The survey found four filesystem build roots. The analyzer promoted them to
“independent build islands” and the advisor ordered the model to attempt:

1. `bigtop-test-framework`;
2. `bigpetstore-spark`;
3. `bigpetstore-transaction-queue`;
4. `bigtop-data-generators`.

The model followed that order in one batch:

- it requested `compile` for all four roots;
- the facade silently changed the Maven root to `install`;
- it changed data-generators to local publication;
- Spark's `compile` became `compileJava`;
- transaction failed on the missing 3.5 coordinate;
- data-generators published 3.7.

The model then closed build as `partial`, explicitly naming:

- test-framework test failures;
- transaction's missing 3.5 coordinate;
- Spark and data-generators as the two apparent successes.

The gate answered:

```text
Phase 'build' terminal claim accepted with validated outcome 'success'
```

In test, the model attempted all four roots again. It reported the primary
success plus the 3.5/3.6 resolution failures and test-framework integration
errors, then again closed as `partial`. The gate again validated success.

This is important: the model's visible summary was more truthful than the
harness's refinement.

### Manual Docker truth by build domain

#### data-generators

```bash
/workspace/bigtop/gradlew \
  --no-daemon --build-cache \
  publishToMavenLocal test
```

Result:

- exit `0`, about 20 seconds;
- exactly `50/50` primary tests, zero failures;
- 156 `.class` files (115 main, 41 test);
- five JARs;
- producer coordinate `3.7.0-SNAPSHOT`.

The SAG report's `54/54` is not the primary result. Four passing
test-framework cases leaked into the project-root XML aggregation.

#### test-framework

The README lifecycle is:

```bash
mvn clean install \
  -DskipTests -DskipITs -DperformRelease \
  -f ./bigtop-test-framework/pom.xml
```

Result:

- exit `0`, 61.68 seconds;
- four JAR variants created and installed.

SAG's naked `mvn install` ran tests during the build phase and manufactured a
build failure.

A separate bare-container Maven test/Surefire attempt selects 45 tests:

- 31 passed;
- 1 failed;
- 12 errors;
- 1 skipped.

The direct causes observed in this 45-test run are narrower:

- the container's root identity breaks `regularUserShell`;
- missing `lsb_release` prevents OS/package-manager initialization and
  contributes the error cascade;
- one `ServiceTest` is skipped.

Separately, the project's broader Failsafe/integration lifecycle documents
requirements such as sudo, cron, iptables, remote hosts, external services,
or a cluster. The harness must classify individual suites/targets and their
capabilities; it must not label the whole test-framework as either universally
runnable or universally cluster-only.

#### transaction queue

The original checkout requests:

```text
org.apache.bigtop:bigpetstore-data-generator:3.5.0-SNAPSHOT
```

The checkout only produces 3.7, so the original build cannot resolve.

For diagnosis only, the same 3.7 JAR was registered under 3.5. Then:

```bash
/workspace/bigtop/gradlew --no-daemon clean test fatJar
```

succeeded in 2m21s with 2 passed and 1 ignored test plus a fat JAR. This
proves that coordinate staleness blocks the original path and that the
specific exercised consumer path can run against this 3.7 artifact. It does
not prove full producer/consumer API compatibility. SAG must report the
mismatch; it must not create the alias automatically.

#### Spark

The original checkout requests 3.6 and therefore cannot resolve against the
3.7 producer.

With a diagnostic 3.6 alias:

- `clean shadowJar` succeeds in 24 seconds;
- it creates a 270,901,024-byte JAR;
- `clean test shadowJar` still fails because the test calls
  `PetStoreStatistics.productMap`, which no longer exists.

Thus the original Spark path is definitely blocked by its stale coordinate.
The alias experiment additionally exposes a test/source API mismatch, but it
does not prove that this is independent of substituting a 3.7 artifact for
the requested 3.6 producer. A true 3.6 producer would be required to isolate
that second cause.

### General harness defects

1. **Unproved independence.**  
   `physical_survey.py:1329-1438` groups directories by the nearest build
   marker. It never parses produced/required artifact coordinates.
   `project_analyzer.py:787-823` nevertheless labels the roots independent,
   and `attempt_policy.py:817-845` states that one island's failure says
   nothing about the others. Bigtop falsifies that claim.

2. **Semantic action mutation.**  
   `build_tool.py:228-273` promotes the model's `compile` to install or local
   publication. `backends.py:144` maps every Gradle compile to
   `compileJava`. Spark is Scala, so `compileJava NO-SOURCE` becomes a false
   success. A requested action, effective action, and actual lifecycle are
   not interchangeable.

3. **Build and test are conflated.**  
   Packaging test-framework should skip unit and integration tests; the test
   phase should classify and run applicable tests. A build-phase install
   should never fail merely because it ran host/cluster tests that belong to
   another environment.

4. **Evidence has no invocation scope.**  
   `physical_validator.py:377-383` recursively scans every XML under the
   project root. That lets an auxiliary domain add four passes to the
   primary result. Class and JAR counts similarly become project-global:
   the report says 279 classes and `None JARs`, while the clean manual
   Docker inventory is 156 `.class` files and five JARs.

5. **Attempts can contaminate one another.**  
   One test-framework execution has 45 tests, while the Java-recovery path
   surfaced roughly 90. Old and new attempt reports are still liable to be
   merged.

6. **Partial claims are upgraded.**  
   A global artifact presence check turns a truthful `2/4` model claim into
   validated success. The test gate similarly lets the primary green result
   erase auxiliary failures. Domain-level facts disappear before sealing.

The correct overall result for this exact checkout is **partial**, but for
specific and durable reasons: a healthy 50/50 primary, a successfully
packaged test framework with a mixture of runnable and environment-dependent
tests, two stale consumer coordinates, and a Spark test/source mismatch
observed after—but not yet isolated from—the diagnostic alias.

## TVM: native artifacts exist, compiler capability does not

### What the model was told and what it did

The Python build objective directed the model to:

1. run `build(action='deps')`;
2. verify build readiness with `build(action='compile')`.

The model complied:

- it recovered `ensurepip`;
- installed the surveyed local `tvm-ffi` provider;
- used the root `--no-deps` recovery;
- installed remaining dependencies;
- byte-compiled 881/881 Python sources.

The root PEP 517 install built native libraries during `deps`. Build closure
was rejected only because `pip check` retained the `tvm-ffi` version conflict,
so the model honestly closed build as partial.

The test steer then asserted:

```text
The NATIVE core was not built in the build phase.
```

The advisor repeated the same assertion. The model ran the required bounded
smoke and saw only three `SKIPPED` labels; model-visible output did not
include structured skip reasons. It then repeated the harness diagnosis and
closed failed.

`react_engine.py:219-227` contains the false statement. Its trigger at
`react_engine.py:2855-2884` is not a native artifact probe; it is merely
“build phase outcome is not success.” A packaging-integrity warning is
therefore converted into a claim that native code does not exist.

### Default Docker build truth

After the local provider recovery:

```bash
/opt/tvm-default/bin/python -m pip install -e . --no-deps
```

The cold build completes in 6m41.86s and creates:

| Artifact | Size |
|---|---:|
| `libtvm_compiler.so` | 104,689,384 bytes |
| `libtvm_runtime.so` | 3,192,336 bytes |
| `libtvm_runtime_extra.so` | 1,379,760 bytes |
| `libtvm_ffi.so` | 2,240,688 bytes |
| `libtvm_ffi_testing.so` | 1,881,752 bytes |

Python imports load the compiler and runtime libraries. The actual state is:

```text
native artifacts: present
USE_LLVM: OFF
LLVM capability: absent
```

The three smoke skips say:

1. `need llvm`;
2. `LLVM enablement only asserted during wheel validation`;
3. `CUDA runtime not expected in this wheel`.

The current report instead says `No artifacts` and “native core was not
built.” Both statements are false.

### Documented LLVM-positive path

The project itself supplies the needed policy in CI and install
documentation. In the same Docker image:

```bash
apt-get install -y llvm-dev libxml2-dev

python3 -m venv /opt/tvm-llvm
/opt/tvm-llvm/bin/python -m pip install --upgrade pip

# Required when the pinned tvm-ffi submodule was fetched with shallow history;
# its version is derived from tags.
git -C 3rdparty/tvm-ffi fetch --tags --unshallow

/opt/tvm-llvm/bin/python -m pip install -e ./3rdparty/tvm-ffi

CMAKE_ARGS="-DUSE_LLVM=ON -DBUILD_TESTING=OFF" \
  /opt/tvm-llvm/bin/python -m pip install -e . --no-deps

/opt/tvm-llvm/bin/python -m pip install \
  ml_dtypes typing_extensions pytest "numpy==1.26.*"

/opt/tvm-llvm/bin/python -m pytest \
  tests/python/all-platform-minimal-test \
  --maxfail=1 -rs \
  --junitxml=/evidence/llvm-numpy126-smoke.xml
```

Results:

- LLVM packages: 6.78 seconds;
- LLVM rebuild: 26.24 seconds with a warm ccache, so not comparable to the
  cold-build duration;
- `USE_LLVM=ON`;
- `libtvm_compiler.so` links `libLLVM.so.18.1`;
- `tvm.runtime.enabled("llvm") == True`;
- `test_llvm_add_pipeline` compiles and executes an LLVM kernel;
- final smoke: `1 passed, 2 skipped` in 0.58 seconds.

The two remaining skips are project-defined wheel/CUDA applicability checks,
not failed compiler evidence.

### Real project defects

1. Root metadata requires `apache-tvm-ffi>=0.1.13`, but the pinned submodule
   reports `0.1.13.dev47+g21e30c3b1`. A development release does not satisfy
   the stable lower bound, and the index has no matching stable release.
   Ordinary `pip install -e .` therefore fails. SAG's local-provider plus
   `--no-deps` recovery is necessary and correct; the remaining `pip check`
   warning is real packaging-integrity evidence.

2. Root metadata leaves NumPy unconstrained. The resolver selects NumPy
   2.5.1, after which the LLVM smoke raises:

   ```text
   ValueError: Could not convert T.float32 to a NumPy dtype
   ```

   TVM's own Ubuntu test setup pins `numpy==1.26.*`. With 1.26.4, the same
   test passes. This is a project dependency-constraint omission. The harness
   may adopt the project-owned CI pin only with provenance; otherwise it must
   report the incompatibility honestly.

### Capability-receipt defect

`python_tool.py:1377-1402` mints a native smoke receipt when:

- the command succeeded;
- at least one test node executed;
- no failure or error exists.

It does not require any non-skipped pass. The live receipt is:

```json
{
  "attempt": 1,
  "candidate": "tests/python/all-platform-minimal-test",
  "project_root": "/workspace/tvm",
  "stats": {
    "errors": 0,
    "executed": 3,
    "failed": 0,
    "selected": 3,
    "skipped": 3
  }
}
```

`python_tool.py:1729-1769` validates only `project_root`. It does not bind
the receipt to target SHA, survey/config fingerprint, native build
fingerprint, or capability state. A second bare test call can therefore
escape the bounded-smoke gate after zero positive evidence, and an old
receipt can survive a materially different build.

The bounded smoke itself is still the right safety mechanism. The defect is
claiming that all-skipped proves capability.

### Why this is a harness problem before a model problem

The current facade can technically express an LLVM repair through a
non-obvious combination of:

- `project(action='provision', packages=[...])`;
- `project(action='env', tool='python',
  executable='/workspace/tvm/.venv/bin/python',
  env={'CMAKE_ARGS': ...})`;
- `build(action='deps')`.

But the model would have to discover the system packages, CMake flags, CI
NumPy constraint, environment-overlay semantics, and the fact that
`compileall` is unrelated to compiler capability. At the same time, the
prompt explicitly tells it the wrong root cause.

A stronger model might overcome the harness. A framework designed for weaker
models must instead turn observed project policy into a typed, executable
native-build contract.

## Cross-project causal model

The failures follow one shared chain:

```text
survey observation
    ↓ unjustified promotion
manifest/advisor claim
    ↓ semantic action mutation
runner invocation
    ↓ unscoped evidence aggregation
phase gate
    ↓ lossy projection
sealed verdict/report
```

### 1. Observation and prescription are still entangled

The surveyor may correctly observe:

- a build marker;
- a README command;
- a native artifact root;
- a smoke path.

Those observations do not by themselves prove:

- build-domain independence;
- the right lifecycle task;
- required environment capabilities;
- that native compilation happened with a particular feature.

The analyzer currently fills those gaps with policy and renders the result as
fact. The advisor, using the same false projection, cannot independently
correct it.

### 2. Requested, effective, and actual actions lack a conservation law

Today:

- `compile` may become Maven `install`;
- Gradle `compile` becomes `compileJava` even for Scala;
- Maven `test` becomes `verify`;
- build actions can run tests and test actions can ignore failures.

The metadata sometimes records the delta after execution, but the weak model
does not receive a stable contract before choosing the action. There is also
no invariant that the effective action satisfies the requested semantics.

### 3. Evidence is snapshot-global instead of receipt-scoped

The validator searches the filesystem after multiple calls and treats every
matching XML/class/JAR as current evidence. It cannot reliably answer:

- which domain created this artifact;
- which invocation created this report;
- whether it belongs to the primary coordinate;
- whether it predates a retry;
- what changed during this invocation.

This is the direct cause of Bigtop's `54/54` and inflated class counts.

### 4. Outcome, integrity, artifacts, capabilities, and tests are collapsed

TVM demonstrates that these are independent:

- package integrity: partial;
- native artifacts: present;
- LLVM capability: absent by default, present after documented rebuild;
- bounded test applicability: all skipped by default;
- compiler smoke: passes with LLVM and the project-owned NumPy policy.

A single red/partial/green value cannot safely substitute for these facts.

### 5. Verdict semantics differ by layer

Commons skips reduce a phase pass rate but not the final verdict. Bigtop
partial model claims become gate success. TVM all-skipped becomes a capability
receipt yet a failed test verdict. Reports then omit or mislabel artifacts.

A sealed snapshot cannot be trusted while each layer recomputes the meaning
of the same evidence.

## Responsibility matrix

| Failure | Prompt | Model/advisor | Harness | Project | Primary owner |
|---|---|---|---|---|---|
| Commons Maven recovery | Gives constraint and call shapes | Selects version/URL/path and succeeds | Activation/retry contract is usable | Requires Maven 3.9 | shared model/harness success |
| Commons XML parsing churn | Does not expose totals | Uses brittle shell/XML sequence | Drops full-output TestStats | None | harness |
| Commons 93.8% warning | Repeats derived ratio | Accepts it | Includes skips in correctness denominator | Skips have explicit reasons | harness |
| Bigtop “independent” roots | States unproved fact | Follows wrong order/map | No coordinate graph | Consumers request stale versions | harness + project |
| Bigtop test-framework build failure | Lacks README args | Does not rediscover them | compile→install runs tests | Some Surefire tests need host facts; broader suites need integration environment | harness |
| Bigtop Spark compile success | Says compile | Trusts green result | Scala mapped to `compileJava NO-SOURCE` | Scala source exists | harness |
| Bigtop `54/54` | Displays polluted count | Cannot detect provenance | Root-recursive XML aggregation | Auxiliary reports coexist | harness |
| Bigtop global partial | Model reports partial | Mostly honest | Gate upgrades coordinates | Checkout cannot fully close | project result, harness distortion |
| TVM “core not built” | Explicit false statement | Repeats it | Uses phase outcome as proxy | Default LLVM off | harness |
| TVM no LLVM repair | Gives no executable call | Does not invent CMake workflow | No typed native-build affordance | Docs/CI provide recipe | harness, model secondary |
| TVM all-skipped receipt | Hidden | Cannot see consequence | Receipt needs no positive pass | Smoke encodes applicability | harness |
| TVM NumPy failure | Policy not surveyed | No chance to act | Ignores project-owned CI pin | Metadata lacks upper bound | project + harness reporting |

## Repair plan

### P0-A — Make every runner call produce an immutable scoped receipt

Introduce an `InvocationReceipt` that is persisted before phase validation:

```text
receipt_id
schema_version
supersedes_receipt_ids
target_sha
survey_fingerprint
config_fingerprint
build_fingerprint
domain_id
attempt_id
requested_action
effective_action
actual_command_argv
semantic_delta
environment_class
toolchain_fingerprint
user_and_permission_fingerprint
relevant_environment_fingerprint
started_at / finished_at
exit_status
artifact_delta
test_report_delta
structured_test_stats
capability_delta
full_output_content_hash
test_report_content_hashes
artifact_content_hashes
```

Validators consume receipts, not a recursive scan of the current project
filesystem. A primary test verdict reads only current primary receipts.
Auxiliary results remain visible but cannot alter the primary numerator or
denominator. Retries supersede or coexist explicitly by receipt ID and
attempt ID.

Receipt persistence must be atomic. Persistence failure is an
evidence-closure failure and cannot close the phase. Before/after content
hashes, rather than path presence alone, distinguish a newly written report
from a same-path overwrite or stale file.

### P0-B — Replace “independent islands” with typed build domains

The survey should emit neutral build domains:

```text
root
system
languages
role: required | optional | example | integration | unknown
environment: bare-container | host-dependent | cluster-required | unknown
test_targets:
  selector
  environment
  required_capabilities
produces: coordinates + versions
requires: coordinates + version constraints
documented_lifecycle: argv + provenance
```

Independence is a conclusion derived from the dependency graph, never a
directory heuristic. Before execution:

1. form produced/required edges;
2. detect version-incompatible edges;
3. topologically order only compatible required domains;
4. assign every remaining domain a structured disposition.

For Bigtop, this must expose producer 3.7 versus consumers 3.5/3.6 before any
attempt. Test-framework must be classified per suite/target: the 31 runnable
Surefire cases remain visible, while root/non-root, OS-tooling, external
service, and cluster requirements are attached only to the tests that need
them.

### P0-C — Enforce semantic conservation across the build facade

Define action contracts instead of generic verb rewrites:

- build/compile may compile only;
- package/install/publish may produce local artifacts but skip unit and
  integration tests by default;
- test owns test execution;
- a source-bearing domain cannot close on `NO-SOURCE`;
- language-aware Gradle lifecycle selects Scala/Kotlin/Groovy/Java tasks or
  the documented `build`/`assemble` task;
- requested action, effective action, actual argv, and semantic delta are
  model-visible before or with the first result.

README/CI lifecycle arguments are executable facts only when retained with
source provenance. Bigtop's `skipTests`, `skipITs`, and `performRelease` must
not disappear between survey and execution.

### P0-D — Model native setup as artifacts plus named capabilities

Replace the binary “native ready” state with:

```text
native_artifacts_present
native_artifact_fingerprint
capabilities:
  llvm: present | absent | unknown
  cuda: present | absent | not_applicable | unknown
package_integrity
```

Add a typed native build/configure affordance, for example:

```text
build(
  action="native",
  features=["llvm"],
  system_packages=["llvm-dev", "libxml2-dev"],
  definitions={"USE_LLVM": "ON", "BUILD_TESTING": "OFF"},
  provenance=[...]
)
```

The exact public shape can differ, but the model must not be required to
invent a hidden environment-overlay sequence. Capability probes close the
receipt. Phase outcome must never be used to infer artifact absence.

### P0-E — Fix capability receipts

A native smoke receipt requires:

- filtered surveyed candidate;
- no collection error;
- no failed/error tests;
- at least one non-skipped pass;
- matching project root and target SHA;
- matching survey/config fingerprint;
- matching native artifact and capability fingerprint.

All-skipped means “capability not proven.” It does not mint or refresh a
receipt. The next bare call remains bounded. A valid positive receipt proves
the named capability; it does not itself authorize an unbounded collect.
Any later expansion requires an explicit scope and budget policy.

### P0-F — Preserve coordinate outcomes through sealing

The sealed snapshot must carry:

- per-domain build state and disposition;
- primary and auxiliary test states separately;
- package integrity;
- artifact inventory;
- capability state;
- conflicts with their concrete coordinates and causes.

Primary success cannot erase auxiliary failure; auxiliary success cannot
supplement the primary count. Domain closure follows this truth table:

| Domain state | Effect on global verdict |
|---|---|
| Required + successful current receipt | Eligible for global success |
| Required + blocked/version-incompatible/environment-missing | Global success forbidden; partial or failed according to completed useful work |
| Required + unknown/untried | Global success forbidden; incomplete/unknown |
| Optional + proven not-applicable or explicitly out of scope | No downgrade, with provenance |
| Optional + attempted failure | Retained as auxiliary failure; policy decides partial versus warning, never silently erased |

A “disposition” is therefore not success by itself. Required domains cannot
be waived green merely because their blocker was classified accurately.

### P1-A — Return structured runner evidence directly

Maven, Gradle, and pytest tools should parse the untruncated output and
current invocation report delta before returning:

- discovered/selected/executed;
- passed/failed/errors/skipped;
- skip-reason histogram;
- collection-error summary;
- report paths and attempt IDs.

For Maven specifically, analyze `full_output`, not bounded `output`.
The model should never need to shell-parse XML to close a phase.

### P1-B — Separate correctness, applicability, and breadth

Use distinct fields:

- `correctness = failures == 0 && errors == 0`;
- `positive_evidence = passed >= 1`;
- `skip/applicability = non_skipped / selected`, supplemented by structured
  reason categories rather than treated as a correctness score;
- `breadth = executed/discovered` when a reliable denominator exists.

Do not collapse them into one pass-rate threshold.

Examples:

- commons: correctness success, positive evidence yes, 61 skips reported;
- TVM default: correctness not contradicted, positive evidence no,
  capability unproven;
- TVM LLVM: correctness success, positive evidence yes, two
  not-applicable/wheel-only skips.

### P1-C — Survey authoritative project policy with provenance

The survey already reads documentation; it should distinguish:

- prose hints;
- executable README commands;
- CI build definitions;
- project Docker dependency pins;
- package metadata.

When these conflict, present the conflict instead of silently choosing.
Project-owned CI/Docker pins may guide setup when tied to the current target
SHA and ecosystem. This would surface TVM's LLVM CMake definitions and NumPy
1.26 policy without hard-coding TVM.

### P1-D — Scope provisioning and artifact inventory

Provision toolchains only for selected required domains. TVM should not
install Java merely because an unrelated JVM subtree exists.

Inventory artifacts by ecosystem and domain:

- JVM classes/JARs;
- Python wheels/editable metadata;
- native shared/static libraries;
- generated binaries.

Remove the report's hardcoded `jar_files: None` and report measured
wall-clock time.

### P1-E — Make advisor corrective, not an echo

The advisor should consume the independent structured state, not prose
derived from a lossy phase outcome. Its recommendation must include an
executable next action and the fact that triggered it:

```text
Observed: native artifacts present; llvm capability absent.
Project policy: .github/workflows/main.yml enables USE_LLVM=ON.
Next action: build(action="native", features=["llvm"], ...).
```

When no proven repair exists, it should say so. It must not convert unknown
into absent.

### P2 — Correct provenance labels and task-description drift

- Rename or correctly populate image ID versus repository digest fields.
- Ensure the stored phase task and the rendered phase objective have one
  source of truth.
- Keep exact policy/version provenance in the run pin and sealed report.

## Falsifiable acceptance matrix

The next implementation is not complete merely because the same three
profile scripts pass. These state transitions and invariants must be tested:

| Case | Required assertion |
|---|---|
| Maven output exceeds inline bound | Structured TestStats are still populated from full output; the model performs zero XML-parsing calls. |
| Reasoned skips with zero failures | Correctness remains success; skips appear only in skip/applicability and breadth fields. |
| Primary and auxiliary XML coexist | Primary count includes only current primary receipt; Bigtop remains exactly `50/50`. |
| JDK/toolchain retry | Old attempt reports cannot double the new attempt count. |
| A retry overwrites the same XML path | Before/after content hashes identify the new attempt; stale content cannot masquerade as a fresh report. |
| Receipt persistence fails | Phase closure is rejected; an in-memory success cannot substitute for durable evidence. |
| Toolchain, user/permissions, or relevant environment changes | Receipt identity changes and prior environment evidence cannot silently validate the new attempt. |
| Scala sources plus `compileJava NO-SOURCE` | Domain cannot close compile success. |
| Build lifecycle includes environment tests | Packaging skips those tests; test phase classifies the required environment. |
| Producer 3.7, consumer 3.5/3.6 | Domains are not called independent; incompatibility is sealed before execution. |
| Model claims partial with required domain failures | Gate cannot refine it to success from unrelated artifacts. |
| Required domain has a classified blocker | Global success remains forbidden; classification is not a green waiver. |
| Native `.so` exists while package integrity is partial | No prompt/advisor/report may say “native core not built.” |
| Native smoke is all skipped | No receipt is written; the second bare call is still bounded. |
| One native smoke passes and others skip | Receipt is written with named capabilities and positive evidence. |
| Positive capability receipt exists | A later broader test still needs an explicit scope and budget; no automatic unbounded collect. |
| Source/config/native build changes | Old capability receipt is rejected. |
| Project CI supplies a compatible dependency pin | Harness may use it only with target-bound provenance. |
| No project-owned repair policy exists | Harness reports unknown/blocker; it does not invent packages or flags. |
| ToolResult → gate → sealed snapshot → report | Domain IDs, counts, artifacts, capabilities, conflicts, and outcomes remain projection-invariant. |

### Same-pin end-to-end anchors

After the generative/state-transition suite passes, repeat the same weak-model
runs:

#### commons-cli

- success;
- Maven 3.9.9 recovery;
- 982 selected/reported, 921 passed, 61 skipped, zero failed/errors;
- four JARs;
- no model-side XML parsing.

#### Bigtop

- truthful partial;
- data-generators exactly 50/50, 156 `.class` files, five JARs;
- test-framework packaging success with documented skip flags;
- test-framework's runnable Surefire subset and capability-dependent targets
  are reported separately, not collapsed into a build failure;
- transaction 3.5 and Spark 3.6 mismatches explicitly sealed;
- Spark's alias-observed test/API mismatch retained without claiming it is
  independent of the substituted producer version;
- no `compileJava NO-SOURCE` success;
- no cross-domain count contamination.

#### TVM

Minimum honest path:

- five native artifacts reported present;
- package-integrity conflict reported separately;
- LLVM absent reported as a capability fact;
- all-skipped smoke writes no receipt and remains bounded on the next call.

Full documented-repair path:

- project-owned LLVM/NumPy policy is cited;
- LLVM capability probe passes;
- smoke reaches `1 passed / 2 project-defined skips`;
- receipt binds the build and capability fingerprints;
- no unbounded expansion; any broader run has an explicit scope and budget.

## Recommended implementation order

1. **Receipt scoping and projection invariants** — without these, later
   measurements remain contaminated.
2. **Build-domain graph and semantic action contracts** — removes Bigtop's
   false map and false actions.
3. **Native capability state and receipt repair** — removes TVM's false
   diagnosis and unsafe state transition.
4. **Structured full-output evidence and verdict axes** — removes
   commons-cli XML churn and skip contradictions.
5. **Project-policy provenance, provisioning scope, and artifact reporting.**
6. **Generative tests, then same-pin three-project reruns.**

Prompt wording should be updated only after the underlying structured facts
and action contracts are correct. Otherwise prompt, advisor, gate, and report
will continue to repeat the same false state in different prose.

## Decision boundary

The evidence supports implementing the P0 set before another acceptance
campaign. It does **not** support:

- declaring the current Plan 4 acceptance complete;
- treating Bigtop's `54/54` as primary ground truth;
- treating TVM's `3/3 skipped` as native capability proof;
- attributing the two difficult runs primarily to model quality;
- adding project-name-specific special cases as the main repair.

The correct target is a general harness in which a weaker model chooses among
small, truthful, executable contracts while the framework owns dependency
closure, action semantics, evidence scope, and verdict consistency.

## Evidence ledger

Recorded SAG evidence:

- `logs/session_20260726_153129_67815/control_events.jsonl`
- `logs/session_20260726_153129_67815/setup-report-20260726-153706.md`
- `logs/session_20260726_153131_67860/.setup_agent/contexts/phase_build.json`
- `logs/session_20260726_153131_67860/.setup_agent/contexts/phase_test.json`
- `logs/session_20260726_153131_67860/setup-report-20260726-154347.md`
- `logs/session_20260726_153134_67903/.setup_agent/contexts/phase_build.json`
- `logs/session_20260726_153134_67903/.setup_agent/contexts/phase_test.json`
- `logs/session_20260726_153134_67903/.setup_agent/native_smoke_receipt.json`
- `logs/session_20260726_153134_67903/.setup_agent/pytest-reports/pytest-attempt-000001.xml`
- `logs/session_20260726_153134_67903/setup-report-20260726-155513.md`

Manual Docker evidence:

- `/tmp/sag-ref-commons-cli/06-maven38-compile.log`
- `/tmp/sag-ref-commons-cli/08-maven399-compile.log`
- `/tmp/sag-ref-commons-cli/09-maven399-test.log`
- `/tmp/sag-ref-commons-cli/11-maven399-default-build-utf8.log`
- `/tmp/sag-ref-commons-cli/13-skip-reasons.log`
- `/tmp/sag-ref-bigtop/10-data-generators.log`
- `/tmp/sag-ref-bigtop/11-spark-as-is.log`
- `/tmp/sag-ref-bigtop/12-transaction-as-is.log`
- `/tmp/sag-ref-bigtop/13-test-framework-official-build.log`
- `/tmp/sag-ref-bigtop/14-test-framework-unit.log`
- `/tmp/sag-ref-bigtop/20-alias-current-generator.log`
- `/tmp/sag-ref-bigtop/21-spark-with-coordinate-alias.log`
- `/tmp/sag-ref-bigtop/22-transaction-with-coordinate-alias.log`
- `/tmp/sag-ref-bigtop/23-spark-shadowjar-with-coordinate-alias.log`
- `/tmp/sag-ref-bigtop/24-data-generators-inventory.log`
- `/tmp/sag-ref-tvm/17-default-root-nodeps-install.log`
- `/tmp/sag-ref-tvm/19-default-artifacts.log`
- `/tmp/sag-ref-tvm/23-default-smoke-run.log`
- `/tmp/sag-ref-tvm/24-llvm-system-packages.log`
- `/tmp/sag-ref-tvm/29-llvm-root-nodeps-install.log`
- `/tmp/sag-ref-tvm/31-llvm-artifacts.log`
- `/tmp/sag-ref-tvm/32-llvm-import-capability.log`
- `/tmp/sag-ref-tvm/36-project-numpy-policy.log`
- `/tmp/sag-ref-tvm/39-llvm-ffi-metadata-corrected.log`
- `/tmp/sag-ref-tvm/45-llvm-numpy126.log`
- `/tmp/sag-ref-tvm/46-llvm-numpy126-smoke.log`
- `/tmp/sag-ref-tvm/llvm-numpy126-smoke.xml`

The manual containers were retained for inspection. No product code,
project source, commit, push, or Docker cleanup was performed by this review.
