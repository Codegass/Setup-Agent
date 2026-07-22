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

## Role taxonomy: the surveyor is NOT a verifier (settled 2026-07-19)

Same instruments — both read the container disk directly, no LLM — but two
DISTINCT roles. Four dividing lines:

1. **Temporal axis — relative to the action/claim under audit, not global
   time.** The surveyor reads BEFORE the action it informs (what the world
   looks like); the judge reads AFTER the action whose claim it audits
   (what that action left behind). A mid-run re-survey is still pre-hoc
   with respect to the NEXT action; a validator pass is post-hoc with
   respect to the claim in front of it.
2. **A claim under test, or none.** The judge's essence is COMPARISON: the
   agent claims "build succeeded", the judge holds that claim against the
   disk — its output takes a position (real / refuted / unknown). The
   surveyor has no claim in front of it; it takes inventory. Its output has
   no position, only description. This is the root of both hard rules:
   verdicts-never-recommends, describes-never-prescribes. One produces a
   RULING, the other produces a MAP.
3. **Cost of being wrong.** A wrong map costs the agent a detour; the
   corrective loops (island checklist, loop-redirect) pull it back —
   recoverable. A wrong ruling is a phantom-green run or a false report —
   the system's credibility, unrecoverable. Hence the validator is 立身之本
   while the analyzer is a convenience: their failure classes differ by an
   order of magnitude, and so must the caution applied when changing them.
4. **Opposite service directions.** The surveyor works FOR the agent (the
   charter: lower discovery cost, amplify through the framework); the
   verifier keeps the agent HONEST (evidence over claims). Folding helper
   and referee into one component blurs the trust boundary.

**Placement** — the surveyor is a ROLE of the physical observation layer
(layer 2), beside the judge, over shared reading machinery:

    Layer 2: physical observation (shared readers: module scan, build-system
             detection, package-layout scan)
      ├── surveyor (pre-hoc, draws the map)
      │     → feeds analyze's fact sheet (layer 3) and ensure_facts (layer 1)
      └── judge = verifier (post-hoc, audits)
            → feeds gates, finalizer, report

One set of instruments; a mapmaker and an auditor using them. Sharing the
instruments is deliberate (Category 2's whole point — the fact and its
staleness domain must use the same eyes); merging the roles is deliberately
refused.

**Why the surveyor must not merge into the verifier.** Ruling independence,
stated precisely (review 2026-07-19: the absolute form was wrong — the
validator DOES read the survey manifest to pick its checking coordinates:
which JDK to expect, which python packages to import-probe, which module
scope to scan). The rule, stated to what the implementation
actually guarantees (round-2 review: the manifest supplies WHAT to expect —
JDK level, package names, module scope — not merely where to look): **the
map defines the checking ASSUMPTIONS — scope and expectations — but only a
FRESH PHYSICAL PROBE may satisfy an assumption; the map itself is never
verdict evidence and never overrides what a probe returned.** A wrong map
can therefore misdirect or narrow a check (a scoped-green risk we
acknowledge rather than deny — absolute independence would require the
validator to re-derive requirements from the ground, which is not claimed
or implemented); what it cannot do is stand in for the probe. Housed
together, even that boundary erodes — map errors would become verdict
errors: the second-hand-evidence class the control-layer redesign existed
to eliminate.

**Two gray zones, stated honestly.**
- `module_coverage` stands between the roles: one computation feeding the
  finalizer's verdict conflicts (audit side) AND the gates' island checklist
  (map side). The precedent it sets: share the ALGORITHM, never the role.
- The survey fingerprint looks like the surveyor verifying — but its object
  is whether the MAP is current, not whether the JOURNEY succeeded. That is
  a mapmaker's self-calibration, not an audit.

**Consequence for Category 3.** The A/B panel tries the PRESCRIPTION layer
(tool-layer goal/plan composition) — neither the surveyor nor the judge is
on trial. The observation layer is the shared foundation under both arms
and does not move.

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

## Category 2 (implemented 2026-07-19): surveying moves to the physical layer

Move the filesystem-reading functions (structure/config/island/module scan)
beside the validator's reading machinery as the shared **physical observation**
substrate (the validator already owns `scan_modules` / `_detect_build_system`;
`module_coverage` already consumes them — this formalizes what exists).
Surveyor and judge stay distinct ROLES over one substrate. Prescriptive fields
(`goal`, preferred-module lines) dissolve: facts carry what exists
(`applies_maven_publish: true`), the agent decides the action, the corrective
loops carry coordinates when it drifts.

**As implemented** — `sag/agent/physical_survey.py` (the surveyor role,
module_coverage's light-dependency posture), six slices, each committed with
the full suite at zero new failures:

1. pure parsers (java-version normalization, gradle content extractors,
   markdown command cleaning) + shared helpers;
2. discovery/structure/docs readers (path validation and discovery,
   structure scan with the TVM native-core fallback, README extraction,
   fallback build-file re-detection);
3. build/test config readers (maven untruncated-pom + parent-POM +
   enforcer/compiler ladder, gradle, python depth, test-framework detection);
4. java test-counting readers (the in-container annotation scan; the
   per-project cache is an explicit parameter the analyzer owns);
5. island enumeration DESCRIPTIVE: the substrate emits
   `{root, system, applies_maven_publish}` only — no goal, no rationale, no
   preferred-first ordering. The analyzer composes the prescription on top
   from those facts (maven→install, gradle+publish→publishToMavenLocal,
   else build; preferred island first), keeping the emitted island shape
   byte-identical until Category 3's A/B gate;
6. the source fingerprint: `config_fingerprint` digests the build-config
   files (java markers + python metadata) via POSIX cksum in one container
   command; the survey stamp carries it (`SURVEY_FACTS_VERSION` 1→2, v1
   stamps re-survey once); the fast path re-surveys when the fingerprint
   mismatches, and treats an unreadable probe on either end as CANNOT
   COMPARE (present, no thrash).

The analyzer keeps thin delegating wrappers throughout, so every call site,
external import (`project_setup_tool`, tests), and test monkeypatch surface
is unchanged.

**Category-2 review outcomes (2026-07-19, all five fixed).**
- P1 both-ends fingerprint: the trunk stamp now carries the SAME
  `config_fingerprint` as the manifest stamp (computed once per survey), and
  the fast path requires agreement — the repro (config edit, re-survey lands
  the manifest, trunk save fails; the old trunk still matches version+path)
  now re-surveys instead of serving stale env metrics forever.
- P1 fingerprint coverage: recursive `find` by name (parent POMs, nested
  island build files, `requirements*.txt`, `poetry.lock`, `Pipfile(.lock)`,
  `gradlew`, `CMakeLists.txt`), pruning build output, with PER-FILE `cksum`
  lines feeding the final digest — names, existence and content boundaries
  are all encoded. `SURVEY_FACTS_VERSION` 2→3.
- P2 module scan fully sunk: `scan_root_build_markers`,
  `scan_source_modules`, `scan_test_module_dirs`, `build_system_at` join the
  substrate; the recommendation methods consume facts only.
- P2 surveyor no longer prescribes: `read_python_metadata` returns
  descriptive metadata; the installer ladder (`detect_installer`) composes
  at the analyzer beside setup/python tools' own calls; README command
  repair (the `-Dtest` fix) moved to the tool layer — the surveyor extracts
  commands AS DOCUMENTED.
- P2 judge logic evicted: `count_actual_test_executions` (surefire-report
  reading — post-hoc evidence) deleted with zero callers (grep proof in the
  commit).

**Second-round review outcomes (2026-07-19, all three fixed).**
- P1 `created` verifies THIS survey's fingerprint on the re-read manifest —
  version+path cannot tell two surveys of the same project apart, so a
  dropped rewrite after a config edit passed as `created` over the old
  facts. Both-None (probe down) is equality; a non-None mismatch in either
  direction means the readback is not this survey's write.
- P1 fingerprint domain now covers EVERYTHING the survey reads: detection
  markers (`Cargo.toml`, `go.mod`, `Makefile`), READMEs to depth 2 (the
  documentation facts derive from them), outside-root parent POMs
  (`find .. -maxdepth 2 -name pom.xml`, mirroring the maven analysis's
  probe), test sources under `src/test` (the trunk's annotation counts),
  and the module-layout dir LISTING (source/test scans key off dir
  existence). `SURVEY_FACTS_VERSION` 3→4.
- P2 `resolve_python_version` moved to the analyzer: the CONSTRAINT is the
  observed fact; picking the newest satisfying version from our supported
  list is policy and composes in `_compose_python_config` at the tool
  layer.

**Third-round review outcome (2026-07-19, fixed).**
- P1 python package layout joins the fingerprint: `python_packages` derives
  from `__init__.py` PATHS and rides the manifest into the validator — a
  package rename with zero config change served the stale name as
  `present`. The probe now lists `__init__.py` paths to depth 4 (covering
  every base `discover_packages` scans: root, `src/`, declared
  `package_dir`, native-core `python/`) as text beside the module-dir
  listing. `SURVEY_FACTS_VERSION` 4→5; behavior test: rename with unchanged
  config re-surveys, never `present`.

**Fourth-round review outcome (2026-07-19, fixed).**
- P1 isomorph: the fixed-maxdepth `__init__.py` mirror missed an
  ARBITRARY-depth declared `package_dir` (e.g. `{'': 'lib/generated/
  python'}` → depth 5), and its predicates drifted from discovery's
  (`-type f` rejected symlinks discovery accepts; prunes hid dirs discovery
  scans). Root fix, per the reviewer's suggestion: the layout section now
  CONSUMES discovery's own machinery — `python_env._package_layout_scan`
  (one set of bases, one find predicate; lazy, so `discover_packages`
  keeps its historical command sequence) with `package_layout_listing`
  draining all bases for the staleness union, rooted at the surveyed
  python root and folded into the digest locally. The fact and its
  staleness domain are now inseparable by construction.
  `SURVEY_FACTS_VERSION` 5→6. The fixture no longer hand-mixes layout into
  the digest: the fake answers the REAL find probes, the rename tests
  assert discovery's exact predicate shape (maxdepth 2 per base, hidden
  dirs excluded, no `-type f`, no prunes), and a deep-package_dir
  regression drives rename-at-depth-5 end-to-end (manifest carries
  `beta_pkg`, old name gone).

**Fifth-round review outcome (2026-07-19, fixed).**
- P1 probe-failure vs empty-layout: the shared scan echoes a trailing
  sentinel per base probe — a MISSING base (find fails, sentinel echoes)
  is a legitimately empty listing; NO sentinel means the probe never ran,
  `package_layout_listing` returns None, and the whole fingerprint is
  CANNOT COMPARE. Without this, a transient find failure over a real
  layout digested as L0: spurious re-survey, and the re-survey could write
  `python_packages=[]` over good facts. Discovery keeps its historical
  both-are-empty behavior (the sentinel is filtered before parsing).
- P2 order-sensitivity: the listing is SORTED before the crc32 (find
  order is unspecified); a reversed-order probe no longer flips the
  digest. `SURVEY_FACTS_VERSION` 6→7. Regressions: broken probe →
  `present`, no rewrite, recovery stays `present`; reversed order →
  `present`, no rewrite.

**Sixth-round review outcome (2026-07-19, fixed).**
- P1 (continuation): the sentinel was echoed UNCONDITIONALLY (`; echo`),
  which rides over find's nonzero exit — a permission/IO failure mid-scan
  (with partial or no output) still looked like a successful empty layout.
  The sentinel is now conditional and left-associative
  (`find … && echo S || { test ! -e base && echo S; }`): it echoes only
  when find COMPLETED or the base is ABSENT; a mid-scan failure is
  sentinel-less → unknowable → CANNOT COMPARE. Verified against a real
  shell (success / absent / chmod-000 permission failure). The find-first
  command shape keeps every scripted-orch fixture matching. Regression:
  find dies mid-scan with partial output → `present`, no rewrite,
  recovery stays `present`; the probe shape itself is asserted
  (`&& echo`, never `; echo`). No version bump — healthy-path digests are
  unchanged.

## Category 3 (prep done 2026-07-19; deletion behind A/B): prescriptions and dead weight

**Reader census (grep proof, 2026-07-19; precision fixed on review).**
`execution_plan` has ZERO production SEMANTIC readers outside
`project_analyzer.py` — nothing outside the analyzer branches on its
content. Two observability caveats, stated exactly: tests read it, and the
full `analysis_result` (plan included) flows into `ToolResult.metadata` and
the control record — an observable DATA SHAPE whose disappearance is a
recorded-artifact change (replay/webui tolerate absent fields, but the
deletion PR must say so, not assume so). Within the analyzer:

| Symbol | Callers | Status |
|---|---|---|
| `_generate_fallback_execution_plan` | 0 | DELETED (dead) |
| `_generate_basic_setup_plan` | 0 | DELETED (dead) |
| `context_manager.validate_execution_plan_completeness` | 0 | DELETED (dead) |
| `_generate_execution_plan` (+ its `_generate_three_step_fallback_plan`) | 1 (`_perform_comprehensive_analysis`) | LIVE — behind the panel |

The live generator's couplings the deletion PR must rework — three inside
the analyzer plus one OUTSIDE it that the first census missed (review P1):
1. `_is_analysis_valid` requires plan length ≥ 2 — analysis validity (and
   therefore `ensure_facts`) currently depends on plan GENERATION
   succeeding; validity must key off facts instead.
2. `_update_trunk_context_with_plan`'s LEGACY branch rewrites the todo list
   from the plan — phase trunks return before reaching it; only pre-phase
   flows consume it.
3. `_format_analysis_output` renders the plan text — the prescription
   surface the panel judges.
4. **Legacy `ContextTool` analyzer-success gate** (`context_tool.py`
   ~1694): completing an analyze task on a legacy trunk requires
   `todo_list > 4` — plan→todo expansion treated as PROOF of analysis.
   With plan→todo off, a legitimate facts-only completion is rejected.
   Rework: the gate verifies persisted survey facts (the stamped manifest
   / analyzer-success marker), not todo arithmetic; plus a legacy
   `run_task` facts-only regression test.

**The A/B panel (the decision instrument, not taste).**

*Arms.* One binary, one runtime switch (`SAG_PRESCRIPTIONS=on|off`),
recorded in every run pin. Review P1: hiding the plan TEXT alone tests
nothing — prescriptions also reach the run through metadata, the trunk, and
the phase intros, so arm F must close EVERY channel. The treatment matrix
is the switch's implementation contract:

| Surface (channel) | Arm P | Arm F |
|---|---|---|
| `_generate_execution_plan` (+ three-step fallback) | runs | **NOT CALLED** — the deletion is simulated, not hidden |
| `_is_analysis_valid` | facts-based validity (SHARED — coupling #1 rework runs in BOTH arms; the plan-length ≥ 2 criterion is SUPERSEDED and appears in neither) | same as P |
| plan→todo rewrite (legacy trunks) | as today | not executed |
| legacy ContextTool analyzer gate | persisted-survey-facts marker (SHARED — coupling #4 rework runs in BOTH arms; the `todo_list > 4` criterion is SUPERSEDED and appears in neither) | same as P |
| analyze ToolResult TEXT | plan + recommendation prose | fact sheet only |
| `ToolResult.metadata` + control record | full analysis incl. plan | facts-only — no `execution_plan`, no goal/rationale strings |
| trunk `build_recommendation` | {system, root, goal, rationale} | {system, root, islands-as-facts} — no goal, no rationale |
| `project_brief` (WHOLE artifact — round-2 review: it carries `actions` and `recommended-build`, persists, and analyze exposes its file ref; hiding only the projection is not facts-only) | composed + persisted + projection rendered | **NOT generated** — no artifact, no ref, no projection |
| `PHASE_OBJECTIVES` text (analyze/build objectives say "Record/Follow the analyzer's Recommended Build") | as today | objective VARIANT with the recommendation references removed (facts wording: detected build system + manifest coordinates) |
| python fallback guidance (`_python_phase_guidance`, fires when the brief projection is absent — which in arm F is ALWAYS) | full, incl. the PRE-HOC native-first block | pre-hoc native-first prose OFF (it is a prescription); only the REACTIVE smoke steer from the allowlist remains |
| `_recommended_workdir` (tool_orchestration: enforced workdir default from `build_root`/`test_root` when the model omits one) | enforced | **SHARED-MECHANICAL, both arms** — it consumes coordinate FACTS that arm F retains ({system, roots}), is overridable per call, and contains no action choice; classified, not overlooked |
| phase-intro recommendation line | with recommended action | coordinates line only (detected system + roots) |
| installer ladder (`python_install_commands`) | unchanged | unchanged — mechanical tool input, NOT on trial |
| **Corrective-loop allowlist**: island checklist, loop redirect, native smoke steer (REACTIVE, evidence-triggered) | ON | ON — the replacement mechanism, identical in both arms |

The classification rule the matrix applies: PRE-HOC advice (what to do
before evidence exists) is a prescription and arm F closes it; REACTIVE
evidence-with-coordinates (fires only on observed failure) is a corrective
loop and both arms keep it; mechanical consumption of retained fact
coordinates is shared.

**Field-level dimension mapping (round-4 review: several matrix ROWS mix
dimensions — the mapping is by FIELD, and this table is authoritative for
the treatment mask):**

| Field / behavior | Dim |
|---|---|
| `_generate_execution_plan` + three-step fallback execution | a |
| plan section of analyze TEXT | a |
| `execution_plan` in ToolResult.metadata / control record | a |
| plan→todo rewrite (legacy trunks) | a |
| recommendation prose section of analyze TEXT | b |
| goal/rationale strings in metadata / control record | b |
| trunk `build_recommendation.goal`/`rationale` | b |
| phase-intro recommendation line (action wording) | b |
| `project_brief` artifact + projection + analyze file ref | c |
| `PHASE_OBJECTIVES` recommendation wording | d |
| pre-hoc python/native-first guidance block | e |
| `_is_analysis_valid` facts-based rework; ContextTool survey-facts gate | SHARED (both arms, both stages) — enabling infrastructure, not a prescription; tying them to mask bit *a* would make validity logic flip with the mask and break dimension orthogonality |
| `_recommended_workdir`, installer ladder, corrective-loop allowlist | SHARED (as in the matrix) |

The run pin records five NAMED booleans —
`plan_pipeline, recommendation_fields, project_brief, objectives_wording,
python_prehoc_guidance` — which fit the existing
`feature_flags: dict[str, bool]` shape (a `"11111"` string does not).

*Probes and anchors.* Four probes, each anchoring a failure class fixed
this month. Anchors are MACHINE-CHECKABLE predicates over the REAL
artifact schema (verified against sealed campaign verdicts, schema_version
3): top-level `verdict`, `build_evidence.{judgment,source,compiled_classes}`,
`test_stats.unique.{executed,failed,errors}`. Quantities the verdict does
not carry are derived from NAMED envelopes: build/test invocation workdirs
and success come from the control record's tool invocations. The pytest
collection count is NOT read from summary text (round-3 review: the
summary line is unstructured and drifts across collection-error shapes) —
the panel-prep implementation adds `collected_after_deselection` as a
structured field of the python tool's recorded result, and the anchor
reads that field. Only if tool emission proves infeasible does the
fallback apply: a VERSIONED parser whose derived record carries the hash
of the raw output it parsed.

Numeric floors are PRE-REGISTERED ABSOLUTE values from historical campaign
evidence, written to the ledger before the panel starts — no mid-panel
calibration, so the P/F interleave stays intact. A probe with no usable
historical floor (pyyaml — its only prior run starved before tests) gets a
SEPARATE calibration phase, EXCLUDED from the 24-run panel and completed
before its first run: THREE arm-P calibration runs; a calibration run is
VALID only if every non-count anchor passes AND `unique.executed > 0` — an
invalid run ABORTS calibration (fix the probe, restart); no floor is ever
registered from invalid runs (round-3 review: a starved single-run
calibration would have registered floor 0 and made the count anchor
vacuous). Floor = max(1, ⌊0.8 × min(unique.executed over the three valid
runs)⌋) — the max(1, …) guard because ⌊0.8×1⌋ = 0 would void the
never-zero promise (round-4 review); formula and repetition count
pre-registered here, result appended to the ledger before the panel
starts.

| Probe (pinned SHA) | Class | Anchor predicates |
|---|---|---|
| bigtop | pathological aggregator / archipelago | PHYSICAL evidence, calibrated to what the archived P baseline (final7 ×3) actually achieves — its verdicts are honest `"failed"` (test-framework build invocations failed 3/3) while 121 compiled classes sit on disk and 50/50 tests ran, so invocation-success over both islands is NOT the baseline and must not be the anchor (round-3 review): `verdict != "unknown"`; NOT(`verdict=="success"` ∧ `build_evidence.compiled_classes==0`) — phantom-green guard; `build_evidence.compiled_classes ≥ 96` (⌊0.8 × min(121,121,121)⌋, physical artifacts regardless of which invocation produced them); at least one SUCCESSFUL data-generators build invocation in the control record (the one invocation-level fact the baseline does meet); `test_stats.unique.executed ≥ 50` ∧ `test_stats.unique.failed == 0` (baseline 50/50). Caveat recorded: final7 predates the oracle fix (`build_evidence.judgment/source` are null there) — floors use only fields the archive actually carries; judgment/source predicates apply to fresh runs only |
| TVM | python native-core | **Build anchor:** `build_evidence.judgment=="failed"` ∧ `build_evidence.source=="physical"`, OR the honest native middle state `build_evidence.judgment=="partial"` ∧ `build_evidence.source=="physical"` ∧ `build_evidence.green==false` (native library absent while pure-python evidence exists), OR strictly better (`verdict ∈ {"partial","success"}` with `build_evidence.green`). **HARD per-run SAFETY anchor `never_sweep_while_unbuilt` (SPLIT — round-review):** while the native core is unbuilt, EVERY *execution-bearing* pytest invocation (one carrying a recorded pytest command) carries a NODE-ID path or `-k` filter (`--maxfail` alone does NOT select — round-2 review) AND the recorded python-tool result's `collected_after_deselection` field ≤ 50 (the structured field defined above — never the summary text; the campaign lock pre-registers the evidence source mode, tool-emitted vs fallback parser, and it may not switch mid-campaign). CRITICAL: **ZERO pytest invocations PASSES this safety anchor — nothing was swept** (the old "no pytest invocation" FAIL conflated an idle-but-safe run with a sweeping one and re-punished arm-independent 5/6 smoke-compliance noise); a strictly-better run (build green) is exempt and may run the full suite (round-review P2-1). `verdict != "unknown"`. **REPORTED METRIC `smoke_liveness` (NOT a per-run must-pass anchor — round-review split):** the count of reps in which the agent landed a REAL smoke (an execution-bearing pytest carrying a node-id/-k filter); aggregated across reps as a fleet-health signal, never gating an individual run. |
| pyyaml | skipped-analyze / package_dir layout | stamped manifest exists even though the agent skipped analyze; `"yaml" ∈ python_packages` (calibration pyyaml-cal-r1: the C-extension package `_yaml` is real and discovered beside it — exact equality mis-failed correct discovery); `verdict ∈ {success, partial}` ∧ `unique.failed == 0` (calibration: 1,281/1,281 passed while the honest ladder verdicts partial — the C extension is unbuilt; success-only was unachievable-by-construction, the bigtop round-3 lesson re-learned) ∧ `unique.executed ≥ ⌊calibration⌋` |
| httpcomponents-client | healthy reactor scoping | **EXECUTION-BEARING test invocations ONLY (round-review item 3):** every build(action='test') invocation that ACTUALLY RAN a command (non-empty recorded command, or a success) has working_directory == project root (control record) AND at least one execution-bearing root test invocation exists. A REJECTED ATTEMPT — empty recorded command with a non-success outcome (the two failing rerun campaigns' non-root `/workspace` events) — is NOT a test execution and is excluded; scoring it as a mis-scoped test punished arm-independent noise. `test_stats.unique.executed ≥ 1500` (historical: 1,856; the 16-test mis-scope must be impossible); `verdict=="success"` ∧ `build_evidence.source=="physical"` |

*Protocol (pinned).*
- **3 repetitions per arm per probe = 24 runs** — the control-layer
  campaign-gate norm (≥ 3); the earlier 2-rep sketch is superseded rather
  than deviated from.
- **Run pin, recorded per run:** target repo SHA, SAG SHA, image digest,
  model id + config hash, prompt bundle hash, cache mode, host arch, run
  order index, and `SAG_PRESCRIPTIONS`.
- **Interleaved order** (P,F,P,F,…) per probe — drift affects both arms
  symmetrically.
- **Collector:** extend the existing panel collector (today:
  TVM/Bigtop/Paramiko/Cassandra) with pyyaml + httpcomponents probes and a
  Category-3 evaluator that computes the anchor predicates from the
  structured artifacts. It launches from a CLEAN worktree at a committed
  SHA — never the developer's dirty tree.
- **Evidence:** --record artifacts + probe logs archived into repo `logs/`
  with checksums and a campaign-ledger append; containers/worktrees cleaned
  per ledger only AFTER all three complete (standing evidence rule).

*Verdict (three-outcome, per probe — review P1: a single global switch
cannot identify a minimal subset, and a double failure indicts the probe,
not the prescriptions).*
- **P pass ∧ F pass** → the probe votes DELETE.
- **P pass ∧ F fail** → attributable regression → stage-2 ablation on that
  probe (below) — the ONLY way a keep-set is ever derived.
- **P fail** (either F) → shared failure or invalid experiment: NO
  retention conclusion from this probe; fix the probe or environment and
  rerun.

*Stage-2 ablation (round-2 review: single-surface restoration only finds
singleton-sufficient prescriptions and misses interactions; and the
surfaces are not independent — plan text presupposes the generator).* The
restorable DIMENSIONS — round-3 review: every F→OFF row of the treatment
matrix must map to exactly one dimension, or stage 2 silently restores an
uncontrolled prescription and can emit a keep-set that hides it (the
python guidance was exactly such a leak):
  (a) plan pipeline (generator + plan text + plan metadata — one dimension,
      restored together);
  (b) recommendation action fields (trunk goal/rationale + intro line);
  (c) `project_brief` artifact + projection;
  (d) `PHASE_OBJECTIVES` recommendation wording;
  (e) pre-hoc python/native-first guidance block (the `_python_phase_guidance`
      prescription the matrix closes in F; the REACTIVE smoke steer stays
      allowlisted and is never a dimension).
The five-bit treatment mask (a–e) is part of EVERY run pin — stage-1 runs
are mask 11111 (P) / 00000 (F); stage-2 runs record their exact mask.
Procedure: start from FULL restoration on the failing probe (= arm P,
known to pass), then greedy BACKWARD ELIMINATION: attempt to remove each
dimension in turn (pins identical EXCEPT the treatment mask, same 3-rep
rule, anchors re-evaluated); a removal that keeps every anchor passing
stays removed; iterate until no single dimension can be removed. The result is the probe's keep-set —
LOCALLY minimal (every retained member is individually necessary given the
others), which is the claim made; the escalation if a locally-minimal
result is disputed is the full mask search — five dimensions give 31
non-empty masks, 30 of them new after reusing the already-run 11111.

Global deletion proceeds only when every probe either votes delete or has
its keep-set identified by stage 2; every dimension not in any probe's
keep-set is deleted.

*Dimension-applicability gate (round-4 review — added 2026-07-19 after the
httpcomponents Stage-2 keep-set search).* A candidate rep may NOT KEEP a
dimension whose treatment is a BYTE-IDENTICAL NO-OP for the probe under test.
An inactive dimension is non-identifying: toggling it does not change a single
byte the agent sees, so a rep that "fails with it removed" cannot attribute
that failure to the dimension — the correct verdict for that dimension is
ABSTAIN, not KEEP. Applicability is ruled MECHANICALLY by intro byte-equality:
if the two masks that differ only in that one dimension produce byte-identical
phase intros for the probe, the dimension is INACTIVE for that probe and
CANNOT enter its keep-set. A keep-set may retain only dimensions that are (i)
applicable (change some byte of the probe's inputs) AND (ii) individually
necessary given the others.

**Reviewer ruling filed verbatim (httpcomponents dim e, 2026-07-19):** e's
HTTP keep vote lacked causal force (failing rep's partial came from
jdk_mismatch in provision, phase_provision.json evidence; dim e reads only
python build/test guidance, react_engine.py returns None for non-python;
00001 vs 00000 Maven build+test intros byte-identical) → e reclassified
INVALID/non-identifying for HTTP; with pyyaml+tvm providing e's
applicable-domain evidence, e is ALSO authorized for deletion.

The gate is guarded by the Maven dual-mask byte-equality regression
(`tests/test_python_phase_guidance.py::test_maven_dual_mask_e_is_byte_identical_noop`):
the build+test intros are byte-equal between masks 00001 and 00000 for a
maven-shaped engine fixture, proving dim e is a byte-identical no-op on a
non-python probe.

**Category 2 done-bar (met 2026-07-19).** Reading functions relocated beside
the validator's substrate with zero call-site behavior change (full suite at
zero new failures after every slice); surveyor emits no `goal`/
preferred-module fields (slice 5); manifest gains the source fingerprint
completing the staleness contract (slice 6 + two review rounds;
`tests/test_framework_survey.py` 24 tests — config edit re-surveys,
unreadable probe degrades to present, failed-trunk-save-after-edit re-surveys
via both-ends fingerprint agreement, config-edit-plus-dropped-rewrite is
`failed` not `created`, probe command covers everything the survey reads).

**Category 3 done-bar (authoritative — supersedes every earlier binary
phrasing).** The panel ran per the pinned protocol; each probe resolved via
the three-outcome verdict; regressing probes carry a stage-2 keep-set; the
removal PR deletes exactly the dimensions in no keep-set (dead fallbacks
already deleted 2026-07-19 with grep proof) and reworks couplings #1–#4.
There is no unconditional "generator must go": if a stage-2 keep-set
retains the plan pipeline, it stays and the spec records which probe kept
it and why.

**Category 4 done-bar.** All rendering lives in the engine projection with
marker-based snapshot tests; the analyzer tool result contains the fact sheet
only.

**Decision instrument, not taste:** the panel is empirical because the
answer is model-strength dependent. The authoritative decision rule is the
three-outcome verdict + stage-2 ablation in the Category 3 section — this
paragraph no longer states its own (superseded) binary rule.

## Panel precondition: baseline reds (registered 2026-07-19)

The panel's pass/fail semantics require a KNOWN suite baseline. Fixed
before the panel: the two `test_native_build_guidance` ladder tests (the
fixture predated the scripted `cache_from_source` metrics probe; its empty
default parsed as 'metrics unavailable' and capped an all-green ladder —
fixture now answers the real probe; this mattered because the TVM probe's
better-branch anchor reads the same ladder). REGISTERED as the accepted
baseline (6, all pre-existing, none touching panel anchor paths):

- `test_evidence_ingestion` ×1 — asserts a `control_event_sink` attribute
  the agent no longer exposes under that name (wiring drift);
- `test_stage1_review_fixes` ×2 — recovery re-runs `verify` where the
  fixtures expect `test` (recovery semantics evolved past the fixtures);
- `test_lineage_idempotence_followup` ×2 — same recovery-era drift;
- `test_packaging_smoke` ×1 — needs network (hatchling download) in an
  isolated build; environment-dependent, not code.

These six are recorded in the campaign ledger at panel start; any NEW
failure beyond them blocks the panel.

## Non-goals

- No change to verdict semantics, the physical validator's judge role, or the
  phase machine.
- No new prose guidance anywhere — this whole effort is subtraction plus
  coordinates.

## Category 3 — executed (2026-07-20)

The panel ran per the pinned protocol (evidence `logs/panel-category3/report.md`,
72 evidence runs; filed reviewer rulings, incl. the httpcomponents dim-e
applicability ruling). Verdict: **all five dimensions a–e authorized for
deletion.** The arm-F behavior (mask 00000, all prescriptions OFF) is now the
ONLY behavior — there is no runtime `SAG_PRESCRIPTIONS` switch. Deleted per
dimension:

- **(a) plan_pipeline** — `_generate_execution_plan` + `_generate_three_step_fallback_plan`,
  the `execution_plan` metadata field, the plan TEXT section of the analyze
  output, and the plan→todo rewrite (`_update_trunk_context_with_plan` →
  `_update_trunk_context_with_facts`, facts-only; `_is_execution_plan_valid`
  gone). `_is_analysis_valid` is facts-based (no plan-length check); the legacy
  ContextTool analyzer gate verifies persisted survey facts (not `todo_list > 4`).
- **(b) recommendation_fields** — goal/rationale prose composition; the intro
  line and analyze output are coordinates-only (`_coordinates_line` is the sole
  rendering). Dead prose helpers deleted (`_island_build_line`, `_island_test_line`,
  `_render_recommended_build_output_prescriptive`). Islands become mechanical
  `{root, system}`; per-island goals survive ONLY on the manifest for the shared
  loop-redirect reader.
- **(c) project_brief** — `_compose_project_brief` + its projection + the trunk
  brief keys + the analyze file ref; the validator's readiness marker no longer
  reads `project_brief_ref`/`_fingerprint`. (The self-contained
  `sag/agent/project_brief.py` module and its direct unit tests are left in
  place — dead in production, removable as a separate Category-4 cleanup.)
- **(d) objectives_wording** — the "Recommended Build/Tests" objective variants
  and the `.replace()`-derived FACTS_* selection chain collapsed: the facts
  wording IS `PHASE_OBJECTIVES`/`PYTHON_PHASE_OBJECTIVES`/`KICKOFF_PHASE_OBJECTIVES`,
  and `phase_objective`/`kickoff_phase_objectives` no longer select between
  variants (FACTS_* names retained as aliases for importers).
- **(e) python_prehoc_guidance** — the pre-hoc python/native-first block
  (`_python_phase_guidance`, `PYTHON_BUILD/TEST_PHASE_GUIDANCE`,
  `NATIVE_FIRST_BUILD_GUIDANCE`). KEPT: the reactive `NATIVE_NOT_BUILT_TEST_GUIDANCE`
  smoke steer with its invocation coordinates (allowlisted corrective loop).

**Switch machinery.** The runtime-gating functions (`prescription_flags`,
`prescription_feature_flags`, `reset_prescription_flags_cache`) and all their
call sites are gone; `prescription_feature_flags()` dropped from the run-pin
feature_flags. `sag/config/prescriptions.py` is retained as PURE naming/parsing
helpers (`PRESCRIPTION_FLAG_NAMES`, `parse_treatment_mask`,
`feature_flags_for_mask`, `treatment_mask_environment`) because the historical
A/B harness under `scripts/` still imports them to reproduce the sealed panel
evidence — the scripts run against pinned old SHAs and were left untouched.

**KEPT (shared substrate, both arms):** island checklist, loop redirects,
`_recommended_workdir`, installer ladder, manifest mechanical fields, framework
survey guarantee, all physical-observation substrate.

**Tests.** `test_prescription_switch.py` → `test_facts_only_behavior.py`
(F-arm/off-behavior assertions became permanent-behavior tests; flag-parsing
and arm-P tests deleted; collector-harness mask tests retained). Other suites
adjusted to the facts-only rendering. Full suite: 2158 passed, 1 skipped, 6
failed — exactly the registered baseline reds (test_evidence_ingestion×1,
test_stage1_review_fixes×2, test_lineage_idempotence_followup×2,
test_packaging_smoke×1); no new failures.
