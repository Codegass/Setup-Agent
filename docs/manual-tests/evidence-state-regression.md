# Evidence State Manual Regression

Run these locally before treating the evidence-state implementation as complete.
Do not add GitHub Actions CI for this matrix.

## Common Fields To Record

- session id
- container name
- repo URL
- selected ref/tag/commit
- task flow statuses
- evidence statuses
- build stats
- test stats
- report status
- UI status
- raw output refs
- known conflicts

## apache/commons-cli

Expected:

- Maven path is detected.
- Build and test evidence reaches success when tests pass.
- Report says success only when validator evidence agrees.
- UI shows success and preserves test counts/percentages.

Command:

```bash
uv run sag project https://github.com/apache/commons-cli --record
```

## apache/commons-vfs

Expected:

- UTF-8/RAT behavior remains fixed.
- Maven version recovery remains agent-driven.
- Test failures produce partial or conflict, not success.
- Report preserves pass rate and failed test count.
- UI shows completed flow with partial result.

Command:

```bash
uv run sag project https://github.com/apache/commons-vfs --record
```

## apache/beam

Expected:

- Gradle evidence extraction works.
- No Maven-only assumptions are used.
- Gradle test reports or raw output refs are visible when status is partial/conflict/blocked.

Command:

```bash
uv run sag project https://github.com/apache/beam --record
```

## apache/iceberg

Expected:

- Mixed Gradle/Maven evidence is handled without forcing one build manager.
- Overall status is aggregated from task evidence.
- UI and report agree on evidence status.

Command:

```bash
uv run sag project https://github.com/apache/iceberg --record
```

## Uncertain Behavior Rule

If a run reveals unclear behavior, stop and classify it before fixing:

- tool contract gap
- validator evidence source gap
- context propagation gap
- prompt guidance gap
- project-specific behavior that should not become a generic rule
