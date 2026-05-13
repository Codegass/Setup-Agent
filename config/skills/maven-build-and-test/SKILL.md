---
name: maven-build-and-test
description: Use when building or running tests on a Maven project, especially multi-module ones (Tika, Struts, Spring). Replaces ad-hoc memory of fail_at_end, surefire-reports, and Enforcer-plugin gotchas.
---

# Build and test a Maven project — including multi-module

Use after `clone-and-bootstrap-repo` (or any time you encounter a Maven
project). Covers the four common scenarios that previously lived in the
system prompt.

## 1. Always use the `maven` tool, never `bash(command="mvn ...")`

The `maven` tool wraps long-timeout monitoring, output truncation, and Maven
Enforcer recovery. `bash` calls to `mvn` lose that machinery.

```
maven(command="compile", working_directory="/workspace/<dir>")
maven(command="test", working_directory="/workspace/<dir>")
```

## 2. Multi-module projects need `fail_at_end=True`

Detect a multi-module project: `<modules>` in the root `pom.xml`.

By default, Maven stops at the first module with a test failure. For coverage
you want every module to attempt its tests:

```
maven(command="test", working_directory="/workspace/<dir>", fail_at_end=True)
```

Without this flag a project like Tika reports ~300 tests instead of 3000+.

## 3. POM parsing errors — recover, don't panic

If you see `[FATAL] Non-parseable POM` or `Unrecognised tag`:

1. Identify the offending file/line from the error message.
2. Inspect: `bash(command="sed -n 'L-5,L+5p' /path/to/pom.xml")` (replace L).
3. Common fixes:
   - Orphan tag outside a `<dependency>` block — move inside, or remove
   - Missing closing tag — add it
   - Invalid element inside `<properties>` — relocate
4. Validate: `bash(command="xmllint --noout /path/to/pom.xml")` should exit 0.
5. Retry: `maven(command="validate", working_directory="<module-dir>")`.

If unfixable after **two** attempts: exclude only that module:
`maven(command="test", properties="pl=!<module-name>", working_directory="/workspace/<dir>", fail_at_end=True)`
and note the exclusion in the final report.

## 4. Don't skip modules for fixable problems

| Problem | Skip module? |
|---|---|
| POM unparseable after recovery | **Yes** — exclude with `pl=!<module>` |
| Compilation error | **No** — fix the compile error |
| Missing dependency | **No** — investigate `mvn dependency:resolve` |
| Test failures | **No** — that's a test result, not a skip reason |
| Timeout | **No** — adjust `silent_timeout` if needed |

## Verification (do not skip)

Before declaring a build or test task complete:

1. **Build artifacts exist** (for compile/package):
   `bash(command="find /workspace/<dir> -path '*/target/classes' -type d | head -5")` — at least one match.

2. **Surefire reports exist** (for test):
   `bash(command="find /workspace/<dir> -path '*/surefire-reports' -type d | wc -l")` — at least one.

3. **Module coverage check** (multi-module test):
   - List modules with `bash(command="find /workspace/<dir> -name pom.xml -not -path '*/target/*'")` and count.
   - Of those, count how many have `surefire-reports/`. The ratio should be ≥ 80%
     unless modules are explicitly excluded.

4. **Test count sanity-check**:
   `bash(command="find /workspace/<dir> -name 'TEST-*.xml' | wc -l")` — should be ≥
   the static test count from `project_analyzer`.

If any verification step fails, do **not** call `manage_context(action="complete_with_results")` — diagnose and re-run.

## Anti-patterns

- **Don't** declare a multi-module build successful when only one module's
  tests ran. That's the failure mode `fail_at_end=True` was designed to fix.
- **Don't** use `skipTests=true` unless explicitly asked — it makes the test
  task vacuously "succeed".
- **Don't** `mvn clean` if you've already compiled successfully and only want
  to retry tests; you'll throw away the build for no reason.
