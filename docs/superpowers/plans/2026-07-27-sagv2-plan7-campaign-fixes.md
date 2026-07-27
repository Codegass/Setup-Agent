# SAG v2 Plan 7 — Campaign Fixes (toolchain + evidence return)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the four faults the 23-project campaign exposed, in the two
packages Chenhao approved: the toolchain we choose must be the toolchain the
build runs and must come from what the project itself asks for; and the
numbers a runner already computed must come back in its result instead of
being re-derived by the model.

**Evidence:** `reports/2026-07-27-23-project-campaign-report.md` and the
recorded sessions named per task. Every fault below has a live session
behind it.

**Tech Stack:** Python 3.12, pytest, existing fake-orchestrator test patterns.

## Global Constraints

- Base commit: `a475c13`. Lanes verify HEAD and `git reset --hard a475c13`
  if the worktree seeded stale.
- NEVER use `git stash` in lanes (refs/stash is repo-shared).
- Absent facts = absent keys. Recorded replay fixtures stay green WITHOUT
  editing them. No project-name-conditioned policy (the AST guard in
  `tests/test_no_project_name_policy.py` enforces this).
- Commit messages carry no Co-Authored-By trailer.
- Before committing: full suite green AND the four Plan 5/6 verifier
  profiles unchanged (cli `logs/session_20260727_035638_85557` 9/9,
  bigtop `logs/session_20260727_041818_87528` 13/13, tvm
  `logs/session_20260727_035643_85621` 18/18 and
  `logs/session_20260727_052810_93090` 15/15).

---

## Lane A — Maven runtime + evidence return

Files: `src/sag/tools/internal/maven_tool.py`, `src/sag/tools/base.py`,
`src/sag/tools/internal/gradle_tool.py` (analysis call only), NEW
`tests/test_project_wrapper_preference.py`, NEW
`tests/test_runner_evidence_return.py`.

### Task A1 — Prefer the project's own build wrapper

**Evidence (camel, `logs/session_20260727_054707_94153`):** the build failed
before compiling with `NoSuchMethodError` in
`org.eclipse.aether.SessionData.computeIfAbsent`. The checkout ships
`mvnw` and `.mvn/wrapper/maven-wrapper.properties` pinning Maven
**3.9.11**, and `.mvn/extensions.xml` loads extensions built against that
resolver. We ran a registered Maven instead.

**Root cause:** `GradleTool.execute` takes `use_wrapper: bool = True`;
`MavenTool.execute` takes `use_wrapper: bool = False`. The same rule is
honoured for one build system and not the other.

**Change:** when `mvnw` exists at the invocation's working directory (or at
the surveyed project root, whichever the invocation targets) and is
executable, it is the runner — unless the caller passed an explicit
`use_wrapper=False`. Record the choice as a visible fact
(`[toolchain] using the project's own ./mvnw (pins Maven 3.9.11)` when the
wrapper properties state a version, otherwise without the parenthetical).
Fall back to the registered Maven with a recorded reason when the wrapper
is absent, not executable, or its first invocation fails to start — a
wrapper that downloads its distribution needs network, and losing the
network must not lose the build.

The invocation receipt already records `argv` and the toolchain
fingerprint, so the choice is auditable with no new field.

### Task A2 — Runner analysis reads the complete output

**Evidence (commons-cli, `logs/session_20260727_035638_85557`):** the model
spent seven actions recovering test counts by hand — a search with the
right pattern against the wrong target, three `bash` crashes on shell
quoting, one `python: command not found` — before parsing the XML with
python3. The counts it was reconstructing had already been computed by the
runner and discarded.

**Root cause:** `maven_tool.py:507` passes `result["output"]` — the
orchestrator's head-30/tail-50 clamp — to `_analyze_maven_output`.
`full_output` (complete) is assigned twelve lines later at 519. The
aggregate `Tests run:` line lives in the omitted middle of a large log.

**Change:** the internal analysis reads the complete output. It is parsed by
regex and never reaches the model's context, so no truncation applies to
it. Same audit for gradle's analysis call. `_record_test_summary` and the
command tracker read the same complete text. Nothing about the
model-facing window changes here.

### Task A3 — The truncation notice names the affordance that exists

**Evidence:** same commons-cli run. `base.py:1053` tells the model to use
`bash` with `grep`, but the complete output is persisted as a storage
reference (`output_<id>`) that grep cannot reach; the tool that reads it,
`output_search`, is not mentioned.

**Change:** when a truncated result carries a stored output reference, the
notice states that reference and the exact call that reads it — e.g.
`output_search(ref='output_5560fdb2ad7b', pattern='Tests run')` — instead
of pointing at bash. When there is no stored reference, the notice says so
rather than naming a tool that has nothing to read.

**Invariant to preserve (assert it):** the receipt's
`output_content_hash` covers the complete output. Truncation applies only
to the model-facing window; a hash over truncated text would mean nothing.

---

## Lane B — Toolchain activation and environment ambiguity

Files: `src/sag/tools/internal/toolchain_manager.py`,
`src/sag/tools/internal/env_tool.py`, `src/sag/tools/internal/build_preflight.py`
(activation path only), NEW `tests/test_toolchain_activation.py`.
Do NOT touch `maven_tool.py` or `base.py` (Lane A owns them).

### Task B1 — The registered runtime is the runtime the build uses

**Evidence (polaris, `logs/session_20260727_065557_97847`):** the build
phase closed failed with the harness's own conclusion — "The build tool is
not inheriting the registered Java 21 runtime; activating the runtime with
explicit PATH/JAVA_HOME should allow the compile to proceed." Java 21 was
installed and registered; the compile ran on something else.

**Change:** find where the registered runtime is meant to reach the
dispatched command (registry → env overlay → dispatch environment) and fix
the break. Start from the recorded session: the env overlay file and the
toolchain registry are both in
`logs/session_20260727_065557_97847/.setup_agent/`, and the control events
carry the argv of every dispatch, so the layer that dropped it is
identifiable from evidence rather than by guessing. Add a check that the
runtime a build dispatch runs under is the registered one, and state the
mismatch as a fact when it is not.

### Task B2 — Two usable versions is not a usable answer

**Evidence (camel-quarkus, `logs/session_20260727_063915_96714`):** the test
phase closed unknown — "blocked by the harness environment overlay:
`/workspace/.setup_agent/env_overlay.json` marks both
`/usr/local/bin/mvn` (3.8.7) and `/workspace/tools/apache-maven-3.9.9/bin/mvn`
(3.9.9) as [usable]". The build had already established that 3.9.9 was
required.

**Change:** an overlay that lists more than one executable for the same
tool resolves deterministically instead of blocking: a version the project
requires wins; failing that, the highest registered version wins; the
choice and the rule that produced it are recorded. An overlay can no
longer present a question the next phase has no way to answer.

---

## Acceptance

1. Full suite green; the four recorded verifier profiles unchanged.
2. Same-pin reruns of the four projects this plan is about:
   **camel** (wrapper is used, the extension error is gone),
   **polaris** (the compile runs on the registered Java),
   **camel-quarkus** (the test phase is not blocked by our own overlay),
   **commons-cli** (982/921/0/61 holds, and the model performs zero
   XML-parsing actions — the counts arrive in the result).
3. A short report recording what each rerun showed, including any fault
   that survived.
