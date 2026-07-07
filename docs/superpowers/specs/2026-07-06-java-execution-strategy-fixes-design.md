# SAG Java Execution-Strategy Fixes — Design

**Date:** 2026-07-06
**Status:** Approved (brainstormed with Chenhao; based on Billy Ye's Claude-vs-SAG benchmark report, week of 6/28)
**Sub-project:** A of 2 (B = Python project support + verifier, separate spec)

## Goal

Close the execution gap measured in the 23-project Claude-vs-SAG comparison
(194 vs 1,491 modules built; 64,502 vs 92,255 tests executed; ~51 vs ~5 avg
iterations) by making three things **guaranteed by construction** instead of
advisory:

1. The detected JDK is actually provisioned before any JVM build runs.
2. Multi-module reactors get a root `install` before anything else needs
   sibling artifacts.
3. Tests run at reactor scope by default, not in a single leaf module.

Non-goal: changing verdict semantics. The tri-state verdict (PR #9) stays; it
is the safety net that lets execution be aggressive without producing false
greens.

## Design principle (settled during brainstorming)

**Check-and-fix for execution, verifier for honesty, never a hard block.**

- Loops come from gates that reject without a remedy. Every check here either
  fixes the problem itself or degrades to an honest verdict conflict — there
  is nothing for the agent to loop on.
- Retry is bounded to **exactly once**, and only when triggered by a
  version-shaped build error (the authoritative signal static analysis cannot
  see).
- The pre-flight **consumes** the phase-1 analysis; it is a guarantee layer,
  not a second analyzer. When phase 1 + the agent already set the environment
  correctly, the pre-flight is a ~100ms no-op.

## Component 1: JDK pre-flight

**New file:** `src/sag/tools/internal/build_preflight.py`
**Callers:** `maven_tool` and `gradle_tool`, at the top of every build/test
execution path.

### 1a. Detection hardening (in `project_analyzer.py`, existing detection)

The existing regex detection (`project_analyzer.py:511-545`) stays the ranked
first guess (enforcer > `maven.compiler.release` > `target` > `source` >
`java.version` > docs), with three fixes:

- Reject captures containing `${...}` (property indirection). Today the
  literal string flows into `java_version`, fails `isdigit()` downstream, and
  silently falls back to `default-jdk`.
- Normalize legacy versions: `1.8` → `8`. (The current enforcer pattern
  `\[?(\d+),?\)?` captures `"1"` from `[1.8,)` — worse than nothing.)
- Enforcer ranges `[11,17)`: take the lower bound, mark
  `java_version_enforced=True`.

### 1b. Pre-flight flow (`JdkPreflight.run(requirement)`)

1. Query the active JDK (`java -version`).
2. **Match** → no-op. Proceed to the build with no narration beyond a single
   debug log line.
3. **Mismatch** → provision:
   - `apt-get install -y openjdk-{N}-jdk`;
   - if apt has no such package (e.g. JDK 8 on newer Debian), add the
     Adoptium/Temurin apt repository and retry the install once;
   - activate via `update-alternatives` and register `JAVA_HOME` in the
     existing `EnvOverlayStore` (same store `project_setup_tool` uses —
     `_register_java_runtime_overlay`, `project_setup_tool.py:985`).
4. Narrate in the tool observation, before the build output:

   ```
   [pre-flight] Required: Java 17 (source: maven-enforcer). Active: Java 11.
   → installed openjdk-17-jdk, JAVA_HOME=/usr/lib/jvm/java-17-openjdk-arm64 (overlay registered)
   ```

5. **Provisioning impossible** (all sources exhausted) → do NOT block:
   proceed with the active JDK, narrate the failure, and record a
   `jdk_mismatch` note for the verifier (Component 4).

**There is no skip flag.** Settled: the pre-flight is idempotent and cheap
when the environment is right, so a skip only enables deliberately building
with a mismatched JDK; the `bash` tool remains the visible,
trajectory-recorded escape hatch, and the verifier back-stops it.

### 1c. Error-driven retry (bounded to one)

If a build fails AND its output matches a version-shaped error —
`requireJavaVersion`, `UnsupportedClassVersionError`,
`invalid target release`, `release version N not supported` — the classifier
extracts the version **from the error message**, re-provisions via 1b, and
reruns the same command exactly once, narrated as
`[pre-flight] enforcer requires 17, re-provisioned, retry 1/1`.
A second failure is a normal build failure. This is the only mechanism that
covers what static analysis cannot know: remote parent poms, property
indirection resolved at build time, and bytecode-target ≠ required-JDK
(cross-compilation with `--release 8` under JDK-17-only plugins).

## Component 2: Root-health classifier + targeting policy

**Location:** `project_analyzer.py` (it already computes packaging, module
lists, and source presence — this is one new classification function feeding
the existing `_recommend_build_approach`/`_recommend_test_approach`).

`classify_root()` returns one of three shapes:

| Shape | Signals | Build target | Test target |
|---|---|---|---|
| `healthy_reactor` | root `<modules>` present AND ≥1 source-bearing module reachable from the root reactor | `mvn install -fae -DskipTests` **at root** | `mvn test -fae` **at root** |
| `pathological_aggregator` | modules empty / vendored-jar layout / all modules profile-gated (the Bigtop case) | PR #9 leaf path, unchanged | PR #9 test-cluster path, unchanged |
| `single_module` | no `<modules>` | current behavior | current behavior |

Gradle analog for `healthy_reactor`: `./gradlew build -x test --continue` /
`./gradlew test --continue` at root (`--continue` = fail-at-end).

Rationale (settled): root-first fail-at-end is what won the benchmark —
`install` at root populates `~/.m2` so sibling SNAPSHOT dependencies resolve
(fix 3), root-scope `test` runs every module's suite (fix 2), and `-fae`
prevents one broken module from hiding the rest. The tri-state verdict
absorbs partial reactor failures honestly (PARTIAL + per-module counts).

## Component 3: Deterministic consumption, narrated narrowing

**Location:** `maven_tool` / `gradle_tool`.

- When the agent invokes the build/test tool **without explicit scoping**,
  the tool defaults its working directory and goals from the Component-2
  recommendation (root, `-fae`).
- When the agent **explicitly narrows** (leaf workdir, `-pl`, single-module
  goal), the tool proceeds — the agent stays in charge — but prepends to the
  observation:

  ```
  [scope] narrower than the recommended reactor root — sibling deps may be
  unresolved; tests outside this module will not run
  ```

  Narrowing is always visible, never silent, and never blocked.

## Component 4: Verifier additions

**Location:** `physical_validator.py`, existing conflict machinery.

- **`jdk_mismatch`** — at validation time, compare the required version
  (analysis) against the active one (env overlay + `java -version`
  evidence). Mismatch caps the verdict at PARTIAL with the reason.
- **`reactor_scope_narrowed`** — `modules_tested` is a strict subset of
  test-bearing `modules_detected` (both counts already exist post-PR #9).
  Caps at PARTIAL with per-module counts, so a leaf-scoped "all green"
  cannot masquerade as full success.

Both are report-only verdict inputs. Neither ever blocks execution.

## Error handling

| Failure | Behavior |
|---|---|
| apt update/install fails | Temurin fallback → else proceed on active JDK + `jdk_mismatch`, narrated |
| Version-shaped error after provisioning | One error-driven retry, then honest failure |
| Overlay registration fails | Log warning, continue (report loses one evidence line; run unaffected) |
| Reactor partially fails under `-fae` | Tri-state verdict absorbs it: PARTIAL + per-module reactor summary (existing) |
| Root classified pathological by mistake | Leaf path still builds real modules; `reactor_scope_narrowed` surfaces the undercoverage in the verdict |

## Testing & acceptance

**Unit (pytest, mocked orchestrator; new dedicated test files per the PR #9
convention):**
- Detection-hardening table tests: `${...}` rejection, `1.8` → `8`, range
  lower bound, ranking order.
- Pre-flight state machine: match/no-op, mismatch/provision, apt-miss/
  Temurin, all-fail/degrade-with-note.
- Retry classifier corpus: real enforcer / `UnsupportedClassVersionError` /
  `invalid target release` outputs → correct version extraction; non-version
  errors → no retry.
- Root classifier fixtures: healthy reactor, Bigtop-style aggregator,
  profile-gated modules, single module.
- Both verifier conflicts, including "narrowed but agent did it explicitly"
  (still PARTIAL) and "root run covered everything" (no conflict).

**Integration (live container):**
- commons-vfs regression: still SUCCESS, no scope conflict, sibling deps
  resolve after root install.
- One big-swing project (httpcomponents-client): expect reactor-scale test
  counts (~2,255, not 16).

**Acceptance (the done bar, settled):** rerun Billy's 23-project benchmark.
Success = the big-swing projects (cassandra-java-driver, tapestry-5,
jackrabbit, cayenne, httpcomponents-client) reach reactor-scale test counts,
average iterations drop from ~51 toward single digits, and no new false
greens (verdict honesty preserved).

## Out of scope

- Python project support and its verifier (Sub-project B, separate spec).
- Billy's fix 4 (JUnit3/parameterized discovery, execution-rate denominator,
  skip-verification gates) — partially handled already, tracked separately.
- npm/Rust/Go execution strategy.
- Any change to verdict semantics or the web UI.
