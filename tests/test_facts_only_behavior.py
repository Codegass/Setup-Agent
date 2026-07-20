"""The prescription treatment mask (analyzer diet, Category 3 A/B panel).

The panel spec's treatment matrix is an implementation contract: arm F must
close EVERY channel a prescription reaches the agent through — generator,
metadata, trunk, brief, objectives wording, pre-hoc python guidance — while
arm P stays byte-identical to today, and the corrective-loop allowlist plus
the shared gates behave the same in both arms. One test per channel here.
"""

from types import SimpleNamespace

import pytest

from sag.config.prescriptions import (
    PRESCRIPTION_FLAG_NAMES,
    parse_treatment_mask,
    prescription_feature_flags,
    prescription_flags,
    reset_prescription_flags_cache,
    treatment_mask_environment,
)
from sag.tools.internal.project_analyzer import ProjectAnalyzerTool

from test_framework_survey import SurveyOrch


@pytest.fixture(autouse=True)
def _fresh_mask(monkeypatch):
    """The mask is process-cached (pin/behavior identity) — every test starts
    from a clean cache and a clean env, and resets after."""
    for name in PRESCRIPTION_FLAG_NAMES:
        monkeypatch.delenv(f"SAG_PRESCRIPTION_{name.upper()}", raising=False)
    monkeypatch.delenv("SAG_PRESCRIPTIONS", raising=False)
    reset_prescription_flags_cache()
    yield
    reset_prescription_flags_cache()


@pytest.fixture()
def arm_f(monkeypatch):
    monkeypatch.setenv("SAG_PRESCRIPTIONS", "off")
    reset_prescription_flags_cache()


def _analysis(tool, path="/workspace/proj"):
    return tool._perform_comprehensive_analysis(path)


# ---- the flag surface itself ----------------------------------------------


def test_default_is_arm_p_all_on():
    assert all(prescription_flags().values())


def test_off_closes_all_five_and_per_dimension_override_wins(monkeypatch):
    monkeypatch.setenv("SAG_PRESCRIPTIONS", "off")
    monkeypatch.setenv("SAG_PRESCRIPTION_PLAN_PIPELINE", "on")
    reset_prescription_flags_cache()
    flags = prescription_flags()
    assert flags["plan_pipeline"] is True  # a stage-2 mask bit
    assert flags["recommendation_fields"] is False


def test_unrecognized_value_raises_never_defaults(monkeypatch):
    """Panel review P1: SAG_PRESCRIPTIONS=offf silently becoming arm P would
    archive a run into the wrong experimental arm."""
    monkeypatch.setenv("SAG_PRESCRIPTIONS", "offf")
    reset_prescription_flags_cache()
    with pytest.raises(ValueError):
        prescription_flags()


def test_mask_is_process_cached_for_pin_identity(monkeypatch):
    """The run pin snapshots the mask once; behavior must not drift from it
    when the env mutates mid-process."""
    first = prescription_flags()
    monkeypatch.setenv("SAG_PRESCRIPTIONS", "off")
    assert prescription_flags() == first  # cached — no drift
    reset_prescription_flags_cache()
    assert not any(prescription_flags().values())


def test_run_pin_carries_the_five_named_keys(arm_f):
    flags = prescription_feature_flags()
    assert sorted(flags) == sorted(f"prescription_{n}" for n in PRESCRIPTION_FLAG_NAMES)
    assert not any(flags.values())


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


# ---- dim (a): plan pipeline ------------------------------------------------


def test_arm_f_never_calls_the_generator(arm_f, monkeypatch):
    called = []
    monkeypatch.setattr(
        ProjectAnalyzerTool,
        "_generate_execution_plan",
        lambda self, analysis: called.append(1) or [],
    )
    tool = ProjectAnalyzerTool(SurveyOrch())
    analysis = _analysis(tool)
    assert not called  # NOT CALLED — simulated deletion, not hidden output
    assert "execution_plan" not in analysis  # the FIELD is absent, not empty


def test_arm_f_metadata_has_no_plan_field_at_all(arm_f):
    """Spec: the field is ABSENT from metadata/the control record — not an
    empty list (an empty list is still an observable plan-shaped signal)."""
    tool = ProjectAnalyzerTool(SurveyOrch())
    result = tool.execute(action="analyze", project_path="/workspace/proj")
    assert "execution_plan" not in result.metadata
    assert "EXECUTION PLAN" not in result.output


def test_arm_f_renders_facts_success_not_plan_failure(arm_f):
    """Panel review P1: 'No execution plan generated' / 'Analysis failed'
    injected a NEGATIVE signal into arm F beyond the deletion under test. A
    successful facts-only survey renders as success."""
    tool = ProjectAnalyzerTool(SurveyOrch())
    result = tool.execute(action="analyze", project_path="/workspace/proj")
    assert "No execution plan generated" not in result.output
    assert "Analysis failed" not in result.output
    assert "Context update failed" not in result.output
    assert "Survey complete" in result.output


def test_arm_f_recommended_tests_hint_is_coordinates_only(arm_f):
    """Panel review P1: the split-root test hint bypassed dim (b) — a
    bigtop-shape recommendation must not render 'Recommended Tests' in F."""
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


def test_arm_p_generator_and_metadata_unchanged(monkeypatch):
    monkeypatch.delenv("SAG_PRESCRIPTIONS", raising=False)
    tool = ProjectAnalyzerTool(SurveyOrch())
    result = tool.execute(action="analyze", project_path="/workspace/proj")
    assert result.metadata["execution_plan"]  # today's behavior intact


# ---- dim (b): recommendation action fields ---------------------------------


def test_arm_f_recommendation_keeps_coordinates_drops_actions(arm_f):
    tool = ProjectAnalyzerTool(SurveyOrch())
    result = tool.execute(action="analyze", project_path="/workspace/proj")
    rec = result.metadata.get("build_recommendation") or {}
    assert rec.get("build_root")  # coordinate facts retained (shared machinery)
    assert "goal" not in rec and "rationale" not in rec
    assert "Recommended Build" not in result.output


def test_arm_f_trunk_recommendation_is_stripped(arm_f):
    from test_framework_survey import IntegrationCM

    cm = IntegrationCM()
    tool = ProjectAnalyzerTool(SurveyOrch(), cm)
    assert tool.ensure_facts("/workspace/proj") == "created"
    rec = cm.trunk.environment_summary["build_recommendation"]
    assert rec.get("build_root")
    assert "goal" not in rec and "rationale" not in rec


# ---- dim (c): project brief -----------------------------------------------


def test_arm_f_brief_not_generated(arm_f, monkeypatch):
    composed = []
    monkeypatch.setattr(
        ProjectAnalyzerTool,
        "_compose_project_brief",
        lambda self, path, analysis: composed.append(1),
    )
    tool = ProjectAnalyzerTool(SurveyOrch())
    analysis = _analysis(tool)
    assert not composed  # no artifact, no ref, no projection
    assert "project_brief_ref" not in analysis


# ---- dim (d): objectives wording -------------------------------------------


def test_arm_f_objectives_lose_recommendation_wording(arm_f):
    from sag.agent.react_engine import phase_objective

    build = phase_objective("build")
    analyze = phase_objective("analyze")
    test = phase_objective("test")
    python_test = phase_objective("test", "python")
    for text in (build, analyze, test, python_test):
        assert "Recommended Build" not in text
        assert "Recommended Tests" not in text  # panel review P1: test leaked
    # The surviving semantics are intact: honest blocking and the bash ban.
    assert "compile target" in build
    assert "Never run mvn/gradle via bash" in build
    assert "pytest" in python_test  # ecosystem override still selected


def test_arm_f_kickoff_tasks_lose_recommendation_wording(arm_f):
    from sag.agent.react_engine import kickoff_phase_objectives

    tasks = kickoff_phase_objectives()
    for name in ("analyze", "build", "test"):
        assert "Recommended Build" not in tasks[name]
        assert "Recommended Tests" not in tasks[name]
    # The kickoff softening survives the facts variant.
    assert "not a Python/other-ecosystem project" in tasks["build"]


def test_arm_p_kickoff_byte_identical():
    from sag.agent.react_engine import KICKOFF_PHASE_OBJECTIVES, kickoff_phase_objectives

    assert kickoff_phase_objectives() == KICKOFF_PHASE_OBJECTIVES


def test_arm_p_objectives_byte_identical(monkeypatch):
    monkeypatch.delenv("SAG_PRESCRIPTIONS", raising=False)
    from sag.agent.react_engine import PHASE_OBJECTIVES, phase_objective

    assert phase_objective("build") == PHASE_OBJECTIVES["build"]
    assert phase_objective("analyze") == PHASE_OBJECTIVES["analyze"]


def test_python_objectives_unaffected_by_dim_d(arm_f):
    from sag.agent.react_engine import PYTHON_PHASE_OBJECTIVES, phase_objective

    # Python objectives carry no recommendation wording — dim (d) must not
    # touch the ecosystem override path.
    assert phase_objective("build", "python") == PYTHON_PHASE_OBJECTIVES["build"]


# ---- dim (e): pre-hoc python guidance vs the reactive allowlist ------------


def _engine_with_python_rec(env=None):
    from test_python_phase_guidance import _engine_at, _python_env

    return _engine_at(2, env if env is not None else _python_env())


def test_arm_f_prehoc_python_guidance_closed(arm_f):
    engine = _engine_with_python_rec()
    assert engine._python_phase_guidance("build") is None
    assert engine._python_phase_guidance("test") is None


def test_reactive_smoke_steer_is_allowlisted_not_a_dimension(arm_f):
    from sag.agent.react_engine import NATIVE_NOT_BUILT_TEST_GUIDANCE

    engine = _engine_with_python_rec()
    # The steer keys off observed build-phase evidence, not the mask.
    if engine._build_phase_lacked_success():
        assert engine._native_smoke_guidance("test") == NATIVE_NOT_BUILT_TEST_GUIDANCE


def test_native_smoke_steer_carries_the_args_invocation_form():
    # Reviewer-flagged missing regression: the smoke steer must show the
    # STRUCTURED invocation coordinates — build(action='test', args=...) with a
    # bounded --maxfail=1 — not just prose. The TVM 357-sweep root cause was a
    # steer that read as pure prose, so the agent fell back to a bare
    # build(action='test') full-suite sweep.
    from sag.agent.react_engine import NATIVE_NOT_BUILT_TEST_GUIDANCE

    assert "args=" in NATIVE_NOT_BUILT_TEST_GUIDANCE
    assert "--maxfail=1" in NATIVE_NOT_BUILT_TEST_GUIDANCE
    assert "action='test'" in NATIVE_NOT_BUILT_TEST_GUIDANCE


# ---- shared machinery identical in both arms --------------------------------


def test_shared_gates_identical_across_arms(monkeypatch):
    analysis = {
        "project_type": "Python",
        "build_system": "pip/poetry",
        "existing_files": ["pyproject.toml"],
    }
    tool = ProjectAnalyzerTool(SurveyOrch())
    monkeypatch.delenv("SAG_PRESCRIPTIONS", raising=False)
    in_p = tool._is_analysis_valid(analysis)
    monkeypatch.setenv("SAG_PRESCRIPTIONS", "off")
    in_f = tool._is_analysis_valid(analysis)
    assert in_p is in_f is True


def test_loop_redirect_reads_the_shared_manifest_identically_in_both_arms(monkeypatch):
    """Panel review P1: the redirect read island goals from the trunk rec,
    which dim (b) strips — P said 'maven install', F degraded to 'build'.
    Both arms now read the shared manifest, goals included."""
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

    def line_for_arm(env_value):
        if env_value is None:
            monkeypatch.delenv("SAG_PRESCRIPTIONS", raising=False)
        else:
            monkeypatch.setenv("SAG_PRESCRIPTIONS", env_value)
        reset_prescription_flags_cache()
        engine = _engine_at(2, _python_env())
        engine.physical_validator = SimpleNamespace(docker_orchestrator=ManifestOrch())
        return engine._untried_island_targets()

    p_line = line_for_arm(None)
    f_line = line_for_arm("off")
    assert p_line == f_line
    assert "'install'" in p_line and "'publishToMavenLocal'" in p_line


def test_island_checklist_renders_without_goals(arm_f):
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


def test_arm_f_initial_prompt_carries_no_plan_claim(arm_f):
    """Panel review P1: the REGISTERED surface is the ProjectTool facade —
    masking only the inner analyzer description left 'analyze (detect build
    system, plan)' in the initial prompt. Full prompt-level regression."""
    prompt = _initial_prompt_with_project_tool()
    assert "plan)" not in prompt
    assert "survey the project; persist build facts" in prompt


def test_arm_p_initial_prompt_unchanged():
    prompt = _initial_prompt_with_project_tool()
    assert "analyze (detect build system, plan)" in prompt


# ---- collector: stage->mask binding + pin verification ---------------------


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
