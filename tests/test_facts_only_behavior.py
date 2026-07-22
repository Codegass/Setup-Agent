"""Facts-only behavior — the permanent post-Category-3 contract.

The A/B panel authorized deleting all five prescription dimensions (analyzer-
diet spec, Category 3; evidence logs/panel-category3/report.md, 72 runs). The
former arm-F behavior is now the ONLY behavior: no runtime switch, no
`SAG_PRESCRIPTIONS` env. One test per channel that a prescription used to reach
the agent through — each asserts the channel is permanently closed to prose
advice and open only to survey FACTS:

  (a) plan pipeline: no generator call, no plan field, no plan text
  (b) recommendation fields: coordinates only — no goal/rationale
  (c) project brief: not composed by the analyzer
  (d) objectives wording: facts wording, no "Recommended Build/Tests"
  (e) pre-hoc python guidance: closed; the REACTIVE smoke steer stays

The corrective-loop allowlist (island checklist, loop redirect, native smoke
steer) and the shared mechanical machinery (workdir default, manifest reads)
are retained — this file asserts they still behave.
"""

from types import SimpleNamespace

import pytest

from sag.config.prescriptions import (
    PRESCRIPTION_FLAG_NAMES,
    parse_treatment_mask,
    treatment_mask_environment,
)
from sag.tools.internal.project_analyzer import ProjectAnalyzerTool

from test_framework_survey import SurveyOrch


def _analysis(tool, path="/workspace/proj"):
    return tool._perform_comprehensive_analysis(path)


# ---- dim (a): plan pipeline is deleted --------------------------------------


def test_analyzer_has_no_plan_generator():
    """The generator and its fallback are gone — no method to call, so no plan
    can be produced (the deletion is real, not gated)."""
    assert not hasattr(ProjectAnalyzerTool, "_generate_execution_plan")
    assert not hasattr(ProjectAnalyzerTool, "_generate_three_step_fallback_plan")


def test_analysis_has_no_execution_plan_field():
    """The field is ABSENT from the fact sheet — never an empty list (an empty
    list is still an observable plan-shaped signal)."""
    tool = ProjectAnalyzerTool(SurveyOrch())
    analysis = _analysis(tool)
    assert "execution_plan" not in analysis


def test_metadata_and_output_have_no_plan():
    tool = ProjectAnalyzerTool(SurveyOrch())
    result = tool.execute(action="analyze", project_path="/workspace/proj")
    assert "execution_plan" not in result.metadata
    assert "EXECUTION PLAN" not in result.output


def test_output_renders_facts_success_not_plan_failure():
    """A successful facts-only survey renders as success — the deleted plan
    path never injects a 'No execution plan generated' / 'Analysis failed'
    negative signal."""
    tool = ProjectAnalyzerTool(SurveyOrch())
    result = tool.execute(action="analyze", project_path="/workspace/proj")
    assert "No execution plan generated" not in result.output
    assert "Analysis failed" not in result.output
    assert "Context update failed" not in result.output
    assert "Survey complete" in result.output


def test_split_root_test_hint_is_coordinates_only():
    """A bigtop-shape recommendation renders 'Test coordinates', never
    'Recommended Tests' (dim b)."""
    tool = ProjectAnalyzerTool(SurveyOrch())
    analysis = {
        "project_type": "Java",
        "build_system": "Maven",
        "existing_files": ["pom.xml"],
        "build_recommendation": {
            "build_system": "maven",
            "build_root": "/workspace/p",
            "goal": "install",
            "rationale": "aggregator",
            "test_root": "/workspace/p/tests-live-here",
            "test_system": "gradle",
        },
    }
    out = tool._format_analysis_output(analysis)
    assert "Recommended Tests" not in out
    assert "Test coordinates: gradle at /workspace/p/tests-live-here" in out
    assert "Recommended Build" not in out


# ---- dim (b): recommendation is coordinates only ----------------------------


def test_recommendation_keeps_coordinates_drops_actions():
    tool = ProjectAnalyzerTool(SurveyOrch())
    result = tool.execute(action="analyze", project_path="/workspace/proj")
    rec = result.metadata.get("build_recommendation") or {}
    assert rec.get("build_root")  # coordinate facts retained (shared machinery)
    assert "goal" not in rec and "rationale" not in rec
    assert "Recommended Build" not in result.output


def test_trunk_recommendation_is_stripped():
    from test_framework_survey import IntegrationCM

    cm = IntegrationCM()
    tool = ProjectAnalyzerTool(SurveyOrch(), cm)
    assert tool.ensure_facts("/workspace/proj") == "created"
    rec = cm.trunk.environment_summary["build_recommendation"]
    assert rec.get("build_root")
    assert "goal" not in rec and "rationale" not in rec


# ---- dim (c): project brief is not composed by the analyzer -----------------


def test_analyzer_does_not_compose_a_brief():
    assert not hasattr(ProjectAnalyzerTool, "_compose_project_brief")
    tool = ProjectAnalyzerTool(SurveyOrch())
    analysis = _analysis(tool)
    assert "project_brief_ref" not in analysis
    assert "project_brief_projection" not in analysis


# ---- dim (d): objectives carry facts wording --------------------------------


def test_objectives_carry_no_recommendation_wording():
    from sag.agent.react_engine import phase_objective

    build = phase_objective("build")
    analyze = phase_objective("analyze")
    test = phase_objective("test")
    python_test = phase_objective("test", "python")
    for text in (build, analyze, test, python_test):
        assert "Recommended Build" not in text
        assert "Recommended Tests" not in text
    # The surviving semantics are intact: honest blocking and the bash ban.
    assert "compile target" in build
    assert "Never run mvn/gradle via bash" in build
    assert "pytest" in python_test  # ecosystem override still selected


def test_kickoff_tasks_carry_no_recommendation_wording():
    from sag.agent.react_engine import kickoff_phase_objectives

    tasks = kickoff_phase_objectives()
    for name in ("analyze", "build", "test"):
        assert "Recommended Build" not in tasks[name]
        assert "Recommended Tests" not in tasks[name]
    # The kickoff softening survives the facts wording.
    assert "not a Python/other-ecosystem project" in tasks["build"]


def test_python_objectives_carry_no_recommendation_wording():
    from sag.agent.react_engine import PYTHON_PHASE_OBJECTIVES, phase_objective

    # Python objectives never carried "Recommended" wording; dim (d) leaves
    # the ecosystem override path exactly as it was.
    assert phase_objective("build", "python") == PYTHON_PHASE_OBJECTIVES["build"]
    assert "Recommended" not in PYTHON_PHASE_OBJECTIVES["build"]


# ---- dim (e): pre-hoc python guidance closed; reactive steer stays ----------


def _engine_with_python_rec(env=None):
    from test_python_phase_guidance import _engine_at, _python_env

    return _engine_at(2, env if env is not None else _python_env())


def test_no_prehoc_python_guidance_block_renders():
    """dim (e) deleted: the pre-hoc python/native-first block is gone. There is
    no `_python_phase_guidance` method and its distinctive wording never
    reaches the intro."""
    from test_python_phase_guidance import _engine_at, _python_env

    assert not hasattr(_engine_with_python_rec(), "_python_phase_guidance")
    build_intro = _engine_at(2, _python_env())._phase_intro_step().content
    assert "build(action='deps') to create the venv" not in build_intro
    assert "This package has a NATIVE core" not in build_intro


def test_reactive_smoke_steer_is_allowlisted_not_a_dimension():
    from sag.agent.react_engine import NATIVE_NOT_BUILT_TEST_GUIDANCE

    engine = _engine_with_python_rec()
    # The steer keys off observed build-phase evidence, never a mask.
    if engine._build_phase_lacked_success():
        assert engine._native_smoke_guidance("test") == NATIVE_NOT_BUILT_TEST_GUIDANCE


def test_native_smoke_steer_carries_the_args_invocation_form():
    # The smoke steer must show the STRUCTURED invocation coordinates —
    # build(action='test', args=...) with a bounded --maxfail=1 — not just
    # prose. The TVM 357-sweep root cause was a steer that read as pure prose,
    # so the agent fell back to a bare full-suite build(action='test') sweep.
    from sag.agent.react_engine import NATIVE_NOT_BUILT_TEST_GUIDANCE

    assert "args=" in NATIVE_NOT_BUILT_TEST_GUIDANCE
    assert "--maxfail=1" in NATIVE_NOT_BUILT_TEST_GUIDANCE
    assert "action='test'" in NATIVE_NOT_BUILT_TEST_GUIDANCE


# ---- shared machinery: retained coordinates + gates -------------------------


def test_analysis_validity_is_facts_based():
    """Validity keys off the survey facts (project identified + files found),
    never plan generation (shared gate rework #1)."""
    analysis = {
        "project_type": "Python",
        "build_system": "pip/poetry",
        "existing_files": ["pyproject.toml"],
    }
    tool = ProjectAnalyzerTool(SurveyOrch())
    assert tool._is_analysis_valid(analysis) is True


def test_loop_redirect_reads_island_goals_from_the_shared_manifest():
    """The redirect reads island goals from the shared manifest (not the
    stripped trunk rec), so the coordinates carry the recommended goal per
    island."""
    import json

    from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
    from test_python_phase_guidance import _engine_at, _python_env

    manifest = {
        "build_islands": [
            {"root": "/workspace/p/a", "system": "maven", "goal": "install"},
            {"root": "/workspace/p/b", "system": "gradle", "goal": "publishToMavenLocal"},
        ]
    }

    class ManifestOrch:
        def execute_command(self, command, **kwargs):
            if command == f"cat {REQUIREMENTS_PATH}":
                return {"success": True, "exit_code": 0, "output": json.dumps(manifest)}
            return {"success": True, "exit_code": 0, "output": ""}

    engine = _engine_at(2, _python_env())
    engine.physical_validator = SimpleNamespace(docker_orchestrator=ManifestOrch())
    line = engine._untried_island_targets()
    assert "'install'" in line and "'publishToMavenLocal'" in line


def test_island_checklist_renders_coordinates_not_none():
    from sag.agent.module_coverage import coverage_checklist_line

    coverage = {"built_islands": [], "total_islands": 2}
    line = coverage_checklist_line(
        coverage,
        islands=[
            {"root": "/workspace/p/a", "system": "gradle"},
            {"root": "/workspace/p/b", "system": "maven"},
        ],
        limit=6,
    )
    if line:
        assert "None" not in line  # stripped islands render coordinates, not 'None'


# ---- the registered prompt surface (facade) --------------------------------


def _initial_prompt_with_project_tool():
    from sag.agent.react_prompt_builder import ReActPromptBuilder
    from sag.config.prompt_loader import load_react_engine_prompts
    from sag.tools.project_tool import ProjectTool

    class _PromptCM:
        def get_current_context_info(self):
            return {"context_type": "trunk", "context_id": "trunk"}

        def load_trunk_context(self):
            return None

    builder = ReActPromptBuilder(
        prompts=load_react_engine_prompts(),
        context_manager=_PromptCM(),
        tools={"project": ProjectTool()},
    )
    return builder.build_initial_system_prompt(
        repository_url="https://example.test/repo.git",
        repository_ref=None,
        tool_calling_enabled=True,
        workflow_mode="setup",
    )


def test_initial_prompt_describes_analyze_as_survey_not_plan():
    """The registered surface is the ProjectTool facade — the analyze
    description is the survey wording, with no 'plan' claim."""
    prompt = _initial_prompt_with_project_tool()
    assert "analyze (detect build system, plan)" not in prompt
    assert "plan)" not in prompt
    assert "survey the project; persist build facts" in prompt


# ---- historical collector harness: mask naming/parsing still intact ---------
#
# The scripts under scripts/ (collect_control_layer_ab.py, run_category3_*.py)
# are HISTORICAL EVIDENCE TOOLING — they run against pinned old SHAs and still
# express/verify treatment masks so the sealed panel evidence stays
# reproducible. prescriptions.py keeps the PURE naming/parsing helpers they
# call (no env reads, no process state). These tests guard that surface.


def test_treatment_mask_parse_and_environment_round_trip():
    mask = parse_treatment_mask("10010")
    assert mask["plan_pipeline"] is True and mask["objectives_wording"] is True
    assert mask["recommendation_fields"] is False
    env = treatment_mask_environment(mask)
    assert env["SAG_PRESCRIPTION_PLAN_PIPELINE"] == "on"
    assert env["SAG_PRESCRIPTION_RECOMMENDATION_FIELDS"] == "off"
    with pytest.raises(ValueError):
        parse_treatment_mask("1001")  # wrong width
    with pytest.raises(ValueError):
        parse_treatment_mask("offf")


def test_stage_mask_binding():
    from scripts.collect_control_layer_ab import CollectionError, _stage_treatment_mask

    # Canonical stages derive their mask from the NAME.
    assert all(_stage_treatment_mask("P", None).values())
    assert not any(_stage_treatment_mask("F", None).values())
    s2 = _stage_treatment_mask("S2-10010", None)
    assert s2["plan_pipeline"] is True and s2["recommendation_fields"] is False
    # A disagreeing explicit mask is rejected — stage and mask are BOUND.
    with pytest.raises(CollectionError):
        _stage_treatment_mask("F", "on")
    # Agreement is fine.
    assert not any(_stage_treatment_mask("F", "off").values())
    # Non-canonical stages have NO default arm: explicit or refuse.
    with pytest.raises(CollectionError):
        _stage_treatment_mask("ws7", None)
    assert all(_stage_treatment_mask("ws7", "on").values())


def test_pin_verification_uses_the_shared_naming_and_catches_drift():
    from scripts.collect_control_layer_ab import CollectionError, _verify_prescription_pin
    from sag.config.prescriptions import feature_flags_for_mask

    f_mask = parse_treatment_mask("off")
    good_pin = SimpleNamespace(feature_flags=feature_flags_for_mask(f_mask))
    _verify_prescription_pin(good_pin, f_mask)  # match: no raise

    # An 11111 run must never archive as arm F.
    p_pin = SimpleNamespace(feature_flags=feature_flags_for_mask(parse_treatment_mask("on")))
    with pytest.raises(CollectionError):
        _verify_prescription_pin(p_pin, f_mask)

    # Missing keys (pre-mask pin) are drift too, not a silent pass.
    with pytest.raises(CollectionError):
        _verify_prescription_pin(SimpleNamespace(feature_flags={}), f_mask)


def test_flag_names_are_the_five_dimensions():
    assert PRESCRIPTION_FLAG_NAMES == (
        "plan_pipeline",
        "recommendation_fields",
        "project_brief",
        "objectives_wording",
        "python_prehoc_guidance",
    )
