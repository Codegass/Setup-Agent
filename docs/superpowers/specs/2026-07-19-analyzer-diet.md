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
| **1. Framework mechanics** | manifest (`build_requirements.json`), env-summary facts, brief projection persistence | engine-guaranteed survey (this spec, Category 1) | preflight, gates, finalizer, build tools — machines |
| **2. Physical observation** | filesystem READING: structure scan, config parsing, island enumeration, module scan | shared substrate beside the validator; two roles on top — **surveyor** (what EXISTS, pre-hoc) and **judge** (what HAPPENED, post-hoc) | both layers 1 and 3 |
| **3. Agent tool surface** | `project(action='analyze')` returns the FACT SHEET only — cheap discovery | thin wrapper over the surveyor | the agent |
| **4. Rendering projections** | fact sheet → intro/observation text | engine intro builder | the agent's window |

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

**Change.**
1. `ProjectAnalyzerTool.ensure_facts(project_path)` — the compute+persist core
   (`_perform_comprehensive_analysis` + trunk env metrics) WITHOUT the
   agent-facing output. Idempotent: existing manifest → no-op. Never raises.
2. Engine hook: at build/test phase intro, `_ensure_project_facts()` runs the
   guarantee (no LLM tokens — container commands only). When it actually ran,
   the intro notes it in one line, so the trace shows the framework acted.
3. The agent's `analyze` action is unchanged (same survey, plus the fact-sheet
   output). It remains the RECOMMENDED path — the guarantee is a net, not a
   replacement.

**Done-bar.** Unit: empty manifest → ensure_facts computes and persists,
returns True; second call no-ops (no analysis commands); engine intro invokes
the guarantee and notes a framework-run survey; agent-skips-analyze replay
shape ends with a populated manifest. Zero behavior change when the agent does
call analyze (existing analyzer suites stay green).

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

**Decision instrument, not taste:** the four-probe panel runs
facts-plus-feedback vs. prescriptions-as-today. Parity or better → delete the
prose; regression → keep the minimal prescriptive set. (This is model-strength
dependent; the panel answers it empirically.)

## Non-goals

- No change to verdict semantics, the physical validator's judge role, or the
  phase machine.
- No new prose guidance anywhere — this whole effort is subtraction plus
  coordinates.
