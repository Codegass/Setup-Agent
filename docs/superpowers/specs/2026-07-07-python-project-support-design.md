# SAG Python Project Support — Design

**Date:** 2026-07-07
**Status:** Approved (brainstormed with Chenhao)
**Sub-project:** B of 2 (A = Java execution-strategy fixes,
`docs/superpowers/specs/2026-07-06-java-execution-strategy-fixes-design.md`,
implemented on branch `feat/exec-strategy-fixes`)

## Goal

SAG sets up mixed-era Python projects (legacy `setup.py` +
`requirements.txt` through modern `pyproject.toml`) end-to-end, with a
verifier whose honesty matches the Java side: physical evidence, tri-state
verdict (SUCCESS / PARTIAL / BLOCKED), no false greens.

Today Python is detection-only and non-functional: the analyzer labels a repo
"Python" (`project_analyzer.py:281`), setup ends at "not implemented"
(`project_setup_tool.py:980+`), and the validator has zero Python logic.

## Settled decisions

| Decision | Choice |
|---|---|
| Dataset shape | Mixed real-world (legacy setup.py → modern pyproject), mirroring the Java benchmark's realism |
| Build-green evidence | `pip check` clean AND top-level package imports AND `compileall` coverage ≥ threshold; wheel build recorded as extra evidence, never required |
| Interpreter provisioning | uv (`uv python install X.Y`), apt fallback; policy: newest stable CPython satisfying the declared constraint |
| Dependency installation | The project's OWN declared tool (poetry / pipenv / pip ladder); pip fallback narrated as a deviation |
| Test execution | Direct `pytest --junitxml` once per suite; `tox.ini` / `noxfile.py` read as metadata only, never executed |
| Architecture | Mirror the Java architecture: `PythonPreflight` beside `JdkPreflight`, new `python_tool` beside `maven_tool`/`gradle_tool`, Python branches in `physical_validator` reusing the tri-state verdict + JUnit-XML parser |

Design principle inherited from sub-project A: **check-and-fix for
execution, verifier for honesty, never a hard block; retries bounded to
exactly once.** The pre-flight consumes phase-1 analysis; when the
environment is already right it is a no-op.

## Component 1: Analyzer Python depth (`project_analyzer.py`)

- **Interpreter requirement**: parse `requires-python` (pyproject
  `[project]`), `python_requires` (setup.py / setup.cfg). Resolve the
  constraint to a concrete version with the policy *newest stable CPython
  satisfying it* (what a human following the docs would install). Normalize
  like Java versions; reject unresolvable/templated values.
- **Installer ladder detection** (faithfulness order):
  1. `poetry.lock` → poetry
  2. `Pipfile.lock` → pipenv
  3. `pyproject.toml` with `[project]` deps → `pip install -e .[<test/dev extras>]`
  4. `requirements*.txt` → `pip install -r` (all matching files, `requirements.txt` first)
  5. bare `setup.py` → `pip install -e .`
- **Test metadata**: read `tox.ini`, `noxfile.py`, `setup.cfg` for test
  dependencies and pytest args — metadata only, never executed.
- **Packages as "modules"**: detect the top-level import package(s)
  (src-layout and flat-layout). Monorepos with multiple pyprojects are
  recorded as package rows reusing the module-metrics row shape. v1 scope:
  single-package focus; multi-package scope conflicts are a later follow-up.
- **Manifest**: persists into the same `build_requirements.json`
  (`/workspace/.setup_agent/build_requirements.json`, sub-project A) with new
  keys: `python_version`, `python_installer`, `python_packages` (list),
  `test_hints` (dict: extra deps, pytest args).

## Component 2: `PythonPreflight` (`build_preflight.py`)

Same contract as `JdkPreflight` (`PreflightOutcome`, narration, overlay,
never raises, no skip flag):

1. Query active interpreter (`python3 --version`); satisfies the requirement
   → no-op.
2. Mismatch → provision: `uv python install X.Y` (installing uv first if
   absent), create the project venv on that interpreter; register in
   `EnvOverlayStore` (`VIRTUAL_ENV`, PATH prepend `venv/bin`); narrate
   `[pre-flight]` in the tool observation.
3. uv unavailable and apt has no matching `python3.X` → do NOT block:
   continue on the active interpreter and record `python_version_mismatch`
   for the verifier.
4. **Retry-once classifier** learns pip's version-shaped errors
   (`Requires-Python >=3.10` rejections; `SyntaxError` from newer-syntax
   source on an old interpreter): extract the version from the error,
   re-provision, rerun once.

## Component 3: `python_tool.py` (new, beside maven/gradle tools)

Three operations, all narrated, all manifest-driven:

- **`setup_env`** — create the venv (on the pre-flight's interpreter) and
  install dependencies with the detected native tool. A pip fallback after a
  poetry/pipenv failure is allowed but narrated as a faithfulness deviation
  (the generated setup docs must reflect what actually ran).
- **`test`** — `pytest --collect-only -q` first to record the
  **detected-tests denominator**, then one
  `pytest --junitxml=/workspace/.setup_agent/pytest-reports/<run>.xml`
  execution (pytest natively runs unittest-style suites). Single honest run
  per suite — comparable to the Java benchmark counts.
- **`build`** (optional) — attempt `python -m build --wheel`; success is
  recorded as extra evidence; failure never blocks or reddens the verdict on
  its own.

## Component 4: Validator Python branches (`physical_validator.py`)

**Build evidence ladder** (tri-state):

| Verdict | Condition |
|---|---|
| SUCCESS | venv exists AND `pip check` clean AND top-level package(s) import AND `compileall` coverage ≥ `build_coverage_threshold` AND declared C-extensions have `.so` artifacts |
| PARTIAL | package imports but `pip check` reports breakage, or `compileall` coverage below threshold, or a declared C-extension is missing — with the concrete reason |
| BLOCKED | no venv, or the top-level package fails to import |

- `compileall` coverage is source-weighted: `.pyc` count / `.py` count over
  the package source (tests and examples excluded) — the direct analog of
  the Java class-coverage ratio, reusing `build_coverage_threshold`.
- **Test verification**: pytest's JUnit XML is parsed by the EXISTING XML
  parser unchanged; report discovery adds
  `/workspace/.setup_agent/pytest-reports/`. The execution-coverage gate
  reuses `tests_not_fully_executed` with the collect-only denominator.
- **New conflict**: `python_version_mismatch` (exact mirror of
  `jdk_mismatch`). All existing conflicts apply unchanged.

## Error handling

| Failure | Behavior |
|---|---|
| uv missing / interpreter uninstallable | apt `python3.X` fallback → else `python_version_mismatch`, narrated, run continues |
| Native installer fails | pip fallback, narrated as deviation in the setup docs |
| `compileall` errors on legacy files | counted as uncompiled → honest PARTIAL, per-file reasons sampled |
| C-extension build fails | PARTIAL with reason (pure-Python import may still pass) |
| No tests collected | `0 detected` recorded honestly — no invented green |
| Overlay registration fails | log warning, continue |

## Testing & acceptance

- **Unit** (scripted-orchestrator fakes, new dedicated test files, mirroring
  sub-project A's patterns): requirement parsing table tests (constraint →
  concrete version policy), installer-ladder selection, `PythonPreflight`
  state machine (match / provision / degrade), pip version-error classifier,
  evidence-ladder tri-state (each PARTIAL and BLOCKED condition), junitxml
  round-trip through the existing parser, `python_version_mismatch` conflict,
  collect-only denominator wiring.
- **Live probes across the dataset's eras**: one modern pyproject repo, one
  legacy setup.py repo, one C-extension repo — all three set up with honest
  verdicts.
- **Acceptance milestone**: Billy's Python benchmark dataset (extracted from
  the Python-benchmark papers), once it lands — same role as the 23-project
  Java benchmark for sub-project A.

## Out of scope

- Multi-package monorepo scope conflicts (the `reactor_scope_narrowed`
  analog) — packages are recorded as rows now; the conflict comes after the
  single-package path is proven.
- conda / mamba environments; Python 2.
- tox/nox execution (metadata only, per the settled decision).
- Any change to Java behavior, verdict semantics, or the web UI.
