# CI-defined build scope — design

- **Date:** 2026-09-07
- **Status:** Agreed in conversation (owner decision); this note records the
  decisions the implementation plan argues from.
- **Relationship:** refines SAG-MS-1 Part IV §22 (the build axis of
  attainment) and supersedes the "production Java sources N/N" and
  "modules N/M as a verdict input" surfaces introduced by `885ae4a`.

## The principle

> The yardstick for a build is what the project's own CI built on the same
> commit. What SAG's module scan finds on disk has no authority as a
> denominator: a build is a mix of special requirements and settings
> (profiles, excluded modules, native toolchains), and only the CI command
> encodes that mix.

Consequences, each of which the plan implements:

1. **Local surfaces state what SAG did; they do not grade it against a
   scan.** The `production Java sources N/N` line and its
   `source_scope_coverage` derivation are removed. The module grain is
   shown as `built N of M declared on disk (diagnostic)` and never as a rate
   band that feeds the verdict.
2. **The setup verdict word answers execution, not scope.** `success`
   means the build ran and left real artifacts and the test run completed
   with reconciled outcomes; `failed` means it did not; `partial` means
   interrupted. Scan-based scope conflicts (`build_modules_incomplete`,
   `reactor_scope_narrowed`, `build_coverage_scope_unverified`,
   `module_scan_contradicts_physical_build`) stay recorded as facts but no
   longer cap the verdict. The Python ladder keeps its own completeness
   (no CI layer exists for it).
3. **Red tests are judged only against CI.** A test that CI also fails (or
   marks flaky) does not count against SAG; a test CI passes and SAG fails
   is a setup deficiency (`NEW_RED_BEYOND_TARGET`). Locally the Tests line
   reports the execution state and the red count; it never says `FAILED`
   on the project's behalf.
4. **The CI cell carries its build universe.** `CellTarget.modules` is
   filled by the harvester from three sources, graded and taken in this
   precedence:
   - `log` — the job log: Gradle `> Task :path:compileJava` lines, or the
     Maven Reactor Summary (SUCCESS rows). Exact, includes modules without
     tests. Subject to the 90-day log cliff.
   - `declared` — the reactor the CI command resolves to on that commit:
     `settings.gradle(.kts)` includes (with `projectDir` remaps) or the pom
     `<modules>` tree under the command's `-P`/`-pl`. Exact for the
     declared scope, no cliff.
   - `test_bearing` — module prefixes of the JUnit pool paths. A lower
     bound (modules with no tests are invisible), disclosed as such.
5. **One module grammar on both sides.** A canonical key is a
   directory-style path (`connect/api`, root `.`); Gradle project paths
   (`:connect:api`, `:root`) are translated; Maven reactor display names
   (what the Reactor Summary prints, which is also what SAG's Maven
   receipts record) pass through unchanged. A key that cannot be matched
   because the project remaps a project directory (kafka's
   `:storage:storage-api` → `storage/api`) is disclosed as an unmatched
   observed module until the `declared` rung resolves the remap.
6. **Lifecycle parity is its own axis.** The CI command is the
   specification of the build; the comparison states `CI: mvn -V test ·
   SAG: mvn compile; mvn test · parity: equivalent | not_equivalent |
   unknown` with the missing phases/tasks named. It never enters `alpha`.
7. **A project without a usable CI target has no scope judgment.** The
   local verdict is then execution-only and the disk reactor count is a
   disclosure a human can read; nothing turns it into a score. The d3
   battery is CI-anchored by construction, so this costs the battery
   nothing.

## Measured facts the plan pins

- kafka @ `26b251a4`: the CI JUnit pool encodes 34 test-bearing modules by
  Gradle project path; SAG's 2026-08-26 evidence tree encodes 19 by
  directory; 18 match under the canonical grammar and `storage/api`
  (CI: `storage/storage-api`) does not. `alpha_build = 18/34`.
- tomcat-jakartaee-migration: single-module; CI command
  `mvn -V test --file pom.xml`; the job log carries `Tests run: 52` and no
  Reactor Summary.
- None of the 23 frozen d3 target records carries modules today; the
  harvester never wrote the field.
