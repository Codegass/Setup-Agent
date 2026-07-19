# Project Analyzer Diet — Four-Layer Split

**Date:** 2026-07-19
**Status:** Design agreed with Chenhao (conversation 2026-07-18/19); Category 1 implementing now, 2–4 staged behind panel A/B.
**The tool's charter (Chenhao's formulation, the test for every future change):**

> The analyzer exists to LOWER THE AGENT'S COST OF DISCOVERY — so it does not
> have to hunt file-by-file. It amplifies the agent through the framework.
> The moment it DISPLACES the agent's judgment, it is doing harm.

## Why now (evidence)

`project_analyzer.py` is 3,082 lines / 49 methods spanning detection, parsing,
counting, recommending, planning, persisting, composing, and rendering. The
July live probes showed the failure mode of that accumulation three times:
competing prescriptions caused single-island fixation (bigtop5); guidance
seams dropped runtime steers (tvm4); and the mechanical chain starved when the
agent skipped `analyze` (pyyaml, 2026-07-13 — the manifest that 8 framework
components read is written ONLY inside the agent-invoked tool).

## The four layers

| Layer | Contents | Owner | Consumer |
|---|---|---|---|
| **1. Framework mechanics** | manifest (`build_requirements.json`), env-summary RAW facts | engine-guaranteed survey (this spec, Category 1) | preflight, gates, finalizer, build tools — machines |
| **2. Physical observation** | filesystem READING: structure scan, config parsing, island enumeration, module scan | shared substrate beside the validator; two roles on top — **surveyor** (what EXISTS, pre-hoc) and **judge** (what HAPPENED, post-hoc) | both layers 1 and 3 |
| **3. Agent tool surface** | `project(action='analyze')` returns the FACT SHEET only — cheap discovery | thin wrapper over the surveyor | the agent |
| **4. Rendering projections** | fact sheet → intro/observation text, **brief projection** (review 2026-07-19: projections are renderings, not layer-1 facts) | engine intro builder | the agent's window |

Hard rules carried over from the control-layer work:
- The surveyor DESCRIBES, never prescribes; the judge VERDICTS, never recommends.
- Corrective loops (gate coverage checklist, loop-redirect, smoke steer) are the
  mechanism that replaces prescriptions — evidence with coordinates, fed back
  mid-run, on EVERY intro branch (the tvm4 seam lesson: runtime-reactive
  guidance may not hide behind the brief projection).

## Category 1 (now): the framework guarantees the survey

**Problem.** Manifest + env-summary facts are produced only when the agent
happens to call `analyze`. Eight mechanical readers depend on them
(`build_preflight`, both build tools, python tool, facade, validator, gates,
setup tool). Live pyyaml: agent skipped analyze → deps no-oped → run wasted.

**Change (as implemented — v2 after the 2026-07-19 re-review).**
1. `ProjectAnalyzerTool.ensure_facts(project_path)` runs the SAME survey
   pipeline as agent-invoked analyze (including brief composition — the
   declared temporary compatibility below) minus the tool-result text.
   Fast path: an agent-era stampless manifest is `present`; a stamped
   manifest is `present` only when the current stamp (`analyzer_version` AND
   this project's validated path) matches on BOTH persisted ends — manifest
   and trunk env-summary. The two stores fail independently: a failed trunk
   save leaves a current-stamp manifest behind, and a manifest-only fast path
   would skip the env-summary retry forever (final review 2026-07-19).
   Older-version or other-project stamps re-survey. `created` requires (a) a
   valid analysis, (b) the trunk env metrics SAVED (a save failure also
   strips the stamp from the in-memory trunk, so a caching store cannot
   serve an unsaved stamp), and (c) the re-read manifest carrying this
   survey's stamp. Never raises.
2. Engine hook: the guarantee runs at build/test intro BEFORE
   `phase_objective()` selects by build system; the trace line renders only
   on `created`.
3. The agent's `analyze` action is unchanged. It remains the RECOMMENDED
   path — the guarantee is a net, not a replacement.

**Review 2026-07-19 outcomes (implemented).**
- The guarantee resolves the orchestrator the constructor actually sets
  (production-constructor test locks it; the first cut silently no-oped).
- `ensure_facts` returns `created | present | failed`; `created` only after
  the manifest is re-read and VERIFIED on disk — success is what the readers
  can see, not what was attempted.
- The survey runs BEFORE `phase_objective()` selects by build system, so a
  skipped-analyze Python repo gets the Python objective, never the Java
  objective beside Python guidance.
- The manifest carries a `survey {project_path, analyzer_version}` stamp; an
  older-version manifest re-surveys instead of serving stale facts (full
  source-fingerprint invalidation lands with Category 2).

**Declared temporary compatibility (review P2, option b).** The guarantee
reuses the SAME full pipeline as agent-invoked analyze — including brief
composition, whose projection the intro then renders. Splitting a facts-only
pipeline now would create two divergent survey paths (a worse seam than the
prescriptions themselves). The prescriptive output is therefore acknowledged
as temporary: it is removed by Category 3's A/B gate, and its removal
criterion is explicit — panel parity of facts-plus-feedback vs. prescriptions.

**Done-bar (`tests/test_framework_survey.py`, 14 tests: 13 unit + 1
UNMOCKED integration).** Unit: production constructor path; `created`
requires the re-read stamp (version + project path) — a stale file over a
dropped rewrite is `failed`; trunk-save failure is `failed` AND the next
call re-surveys to `created` (fail-then-recover: the fast path requires the
stamp on both persisted ends, so a manifest-only partial survey retries the
trunk save); `present` (with both ends stamped) never re-analyzes or
re-writes; agent-era stampless manifest is `present`;
same-version other-project stamp re-surveys; survey precedes objective
selection; test phase runs the guarantee; broken container never raises.
Integration (no monkeypatch): a skipped-analyze run reaches the build intro,
the REAL survey pipeline executes against the scripted container, the stamped
manifest lands, the trunk env is saved, and the objective selected in that
same intro is the Python one. Not covered (stated, not overclaimed): a full
recorded-transcript replay — that harness lands with the Category 2/3 panel
work.

## Category 2 (next): surveying moves to the physical layer

Move the filesystem-reading functions (structure/config/island/module scan)
beside the validator's reading machinery as the shared **physical observation**
substrate (the validator already owns `scan_modules` / `_detect_build_system`;
`module_coverage` already consumes them — this formalizes what exists).
Surveyor and judge stay distinct ROLES over one substrate. Prescriptive fields
(`goal`, preferred-module lines) dissolve: facts carry what exists
(`applies_maven_publish: true`), the agent decides the action, the corrective
loops carry coordinates when it drifts.

## Category 3 (behind A/B): prescriptions and dead weight

- `_generate_execution_plan` + fallback plans: `validate_execution_plan_completeness`
  has zero callers — delete the generator after confirming phase-mode never
  reads `execution_plan` from metadata.
- Output slimming: the fact sheet replaces the plan text; rendering moves to
  the engine projection (Category 4, low priority — agreed acceptable as-is).

**Category 2 done-bar.** Reading functions relocated beside the validator's
substrate with zero call-site behavior change (suites green byte-for-byte);
surveyor emits no `goal`/preferred-module fields; manifest gains the source
fingerprint (config files digest) completing the staleness contract.

**Category 3 done-bar.** `_generate_execution_plan` + fallback deleted with a
grep proof of zero readers; panel A/B attached to the removal PR showing
facts-plus-feedback ≥ prescriptions on all four probes.

**Category 4 done-bar.** All rendering lives in the engine projection with
marker-based snapshot tests; the analyzer tool result contains the fact sheet
only.

**Decision instrument, not taste:** the four-probe panel runs
facts-plus-feedback vs. prescriptions-as-today. Parity or better → delete the
prose; regression → keep the minimal prescriptive set. (This is model-strength
dependent; the panel answers it empirically.)

## Non-goals

- No change to verdict semantics, the physical validator's judge role, or the
  phase machine.
- No new prose guidance anywhere — this whole effort is subtraction plus
  coordinates.
