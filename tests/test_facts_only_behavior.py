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

import json
from types import SimpleNamespace

import pytest
from test_framework_survey import SurveyOrch

from sag.agent.project_fact_projection import render_project_fact_sheet
from sag.agent.tool_orchestration import format_tool_result
from sag.config.prescriptions import (
    PRESCRIPTION_FLAG_NAMES,
    parse_treatment_mask,
    treatment_mask_environment,
)
from sag.project_fact_sheet import (
    project_analysis_error_metadata,
    project_fact_sheet_metadata,
    serialize_project_analysis_error,
    serialize_project_fact_sheet,
    with_project_fact_sheet_identity,
)
from sag.tools.base import ToolResult
from sag.tools.internal.project_analyzer import ProjectAnalyzerTool


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


def test_public_fact_sheet_metadata_excludes_tool_policy_fields():
    tool = ProjectAnalyzerTool(SurveyOrch())
    result = tool.execute(action="analyze", project_path="/workspace/proj")

    python = result.metadata.get("python_config") or {}
    for policy_field in (
        "python_version",
        "python_installer",
        "python_install_commands",
        "python_install_source",
        "python_install_note",
        "python_venv",
        "test_hints",
    ):
        assert policy_field not in python
    assert python.get("python_root") is not None
    assert python.get("python_packages")


def test_documented_commands_are_not_rewritten_by_the_analyzer(monkeypatch):
    from sag.agent import physical_survey

    documented = {
        "build_commands": [],
        "test_commands": ["mvn clean install -Dtest -Dossindex.skip"],
    }
    monkeypatch.setattr(
        physical_survey,
        "analyze_documentation",
        lambda _orchestrator, _path: documented.copy(),
    )

    observed = ProjectAnalyzerTool(SurveyOrch())._analyze_documentation("/workspace/proj")

    assert observed["test_commands"] == documented["test_commands"]


def test_analyzer_emits_fact_sheet_and_engine_projects_the_model_observation():
    """The analyzer result is structured facts; only the engine boundary adds
    the marker and model-facing survey prose."""
    tool = ProjectAnalyzerTool(SurveyOrch())
    result = tool.execute(action="analyze", project_path="/workspace/proj")

    raw_fact_sheet = json.loads(result.output)
    assert raw_fact_sheet["schema"] == "sag.project-facts"
    assert raw_fact_sheet["version"] == 1
    assert "PROJECT ANALYSIS COMPLETED" not in result.output
    assert "Survey complete" not in result.output

    observation = format_tool_result("project", result)
    assert observation.startswith(
        "✅ project executed successfully\n\n" "Output: 🔍 PROJECT ANALYSIS COMPLETED\n"
    )
    assert "No execution plan generated" not in observation
    assert "Analysis failed" not in observation
    assert "Context update failed" not in observation
    assert "Survey complete" in observation


def test_analyzer_failure_is_typed_and_only_engine_projects_explanatory_prose():
    result = ProjectAnalyzerTool(SurveyOrch()).execute(action="unsupported")

    raw_error = json.loads(result.output)
    assert raw_error == {
        "code": "ANALYSIS_INVALID_ACTION",
        "facts": {"action": "unsupported"},
        "schema": "sag.project-analysis-error",
        "version": 1,
    }
    assert result.error == "ANALYSIS_INVALID_ACTION"
    assert result.suggestions == []

    observation = format_tool_result("project", result)
    assert "The project survey action is invalid." in observation
    assert "Use project(action='analyze')." in observation
    assert '"schema":"sag.project-analysis-error"' not in observation


def test_typed_analysis_error_json_remains_valid_and_bounded():
    metadata = project_analysis_error_metadata(
        "ANALYSIS_EXCEPTION",
        nested={
            f"key-{outer}": {f"child-{inner}": "x" * 2_000 for inner in range(32)}
            for outer in range(32)
        },
    )

    encoded = serialize_project_analysis_error(metadata)
    decoded = json.loads(encoded)

    assert len(encoded) < 8_000
    assert decoded["schema"] == "sag.project-analysis-error"
    assert decoded["facts"]["truncated"] is True


def test_raw_fact_sheet_stays_valid_and_bounded_for_large_project_facts():
    fact_sheet = with_project_fact_sheet_identity(
        {
            "project_path": "/workspace/" + "p" * 10_000,
            "project_type": "Python",
            "build_system": "python",
            "existing_files": [f"file-{index}-" + "x" * 2_000 for index in range(100)],
            "documentation": {
                "build_commands": ["python -m build " + "a" * 10_000],
            },
        }
    )

    encoded = serialize_project_fact_sheet(fact_sheet)
    decoded = json.loads(encoded)

    assert len(encoded) < 8_000
    assert decoded["project"]["project_path"].endswith("…")
    assert len(decoded["files"]) == 8
    assert decoded["documentation"]["build_commands"][0].endswith("…")


def test_bounded_fact_lists_preserve_true_totals_through_engine_projection():
    analysis = {
        "project_path": "/workspace/demo",
        "project_type": "Java",
        "build_system": "Maven",
        "existing_files": [f"file-{index}" for index in range(100)],
        "dependencies": [f"dependency-{index}" for index in range(100)],
        "build_recommendation": {
            "build_system": "maven",
            "build_root": "/workspace/demo",
            "build_islands": [
                {"system": "maven", "root": f"/workspace/demo/module-{index}"}
                for index in range(100)
            ],
        },
        "python_config": {
            "python_local_providers": [
                {
                    "distribution_name": f"provider-{index}",
                    "root": f"vendor/provider-{index}",
                }
                for index in range(100)
            ],
        },
    }

    metadata = project_fact_sheet_metadata(analysis)
    raw = json.loads(serialize_project_fact_sheet(metadata))
    projected = render_project_fact_sheet(metadata)

    assert len(metadata["existing_files"]) == 8
    assert metadata["existing_files_total"] == 100
    assert raw["fact_totals"]["existing_files"] == 100
    assert raw["fact_totals"]["build_coordinates.build_islands"] == 100
    assert "... and 95 more files" in projected
    assert "Dependencies: 100 found" in projected
    assert "+92 more in /workspace/.setup_agent/build_requirements.json" in projected
    assert "+97 more in /workspace/.setup_agent/build_requirements.json" in projected


def test_compact_metadata_fallback_is_explicit_in_raw_and_engine_views():
    huge_atom = "x" * 1_000
    analysis = {
        "project_path": "/workspace/demo",
        "project_type": "Native",
        "build_system": "cmake",
        "existing_files": [f"file-{index}" for index in range(100)],
        "dependencies": [f"dep-{index}" for index in range(100)],
        "build_recommendation": {
            "build_system": "cmake",
            "build_root": "/workspace/demo",
            "build_domains": [
                {
                    "root": f"/workspace/demo/domain-{index}",
                    "system": "cmake",
                    "languages": [huge_atom for _ in range(8)],
                    "produces": [
                        {
                            "group": huge_atom,
                            "name": f"producer-{coordinate}",
                            "version": huge_atom,
                        }
                        for coordinate in range(8)
                    ],
                    "requires": [
                        {
                            "group": huge_atom,
                            "name": f"requirement-{coordinate}",
                            "version": huge_atom,
                        }
                        for coordinate in range(8)
                    ],
                }
                for index in range(8)
            ],
        },
    }

    metadata = project_fact_sheet_metadata(analysis)
    raw = json.loads(serialize_project_fact_sheet(metadata))
    projected = render_project_fact_sheet(metadata)

    assert metadata["fact_sheet_truncated"] is True
    assert raw["truncated"] is True
    assert raw["fact_counts"]["existing_files"] == 100
    assert raw["authoritative_source"] == "/workspace/.setup_agent/build_requirements.json"
    assert "Public fact sheet compacted" in projected
    assert "existing_files=100" in projected
    assert "/workspace/.setup_agent/build_requirements.json" in projected


def test_engine_fact_projection_has_a_hard_total_budget():
    huge = {
        "project_path": "/workspace/" + "p" * 50_000,
        "project_type": "unknown",
        "build_system": "unknown",
        "root_listing": "entry\n" * 100_000,
        "existing_files": ["f" * 10_000 for _ in range(100)],
        "dependencies": ["d" * 10_000 for _ in range(100)],
        "documentation": {
            "build_commands": ["cmd " + "x" * 100_000 for _ in range(100)],
        },
        "build_recommendation": {
            "build_system": "maven",
            "build_root": "/workspace/" + "r" * 50_000,
            "build_islands": [
                {"system": "maven", "root": f"/workspace/island-{index}-" + "i" * 10_000}
                for index in range(100)
            ],
        },
    }

    projected = render_project_fact_sheet(huge)

    assert len(projected) <= 12_000
    assert "+92 more" in projected


def test_engine_projection_fails_closed_on_malformed_nested_fact_types():
    malformed = with_project_fact_sheet_identity(
        {
            "project_path": "/workspace/demo",
            "project_type": "Java",
            "build_system": "Maven",
            "build_recommendation": {
                "build_islands": [
                    {"root": "/workspace/producer", "system": "maven"},
                    {"root": "/workspace/consumer", "system": "gradle"},
                ],
                "domain_edges": [
                    {
                        "consumer": "/workspace/consumer",
                        "producer": "/workspace/producer",
                        "status": "version_incompatible",
                        "detail": {"not": "text"},
                    }
                ],
            },
            "documentation": {"build_commands": 9},
            "static_test_count": 10,
            "method_count": "not-a-number",
            "parameterized_info": "not-a-map",
        }
    )

    observation = format_tool_result(
        "project",
        ToolResult.completed_success(output="{}", metadata=malformed),
    )
    projected = render_project_fact_sheet(malformed)

    assert "PROJECT ANALYSIS COMPLETED" in observation
    assert "PROJECT ANALYSIS COMPLETED" in projected
    assert "Coordinate mismatch" not in projected


def test_analyzer_usage_is_survey_only():
    usage = ProjectAnalyzerTool(SurveyOrch()).get_usage_example()
    assert 'project(action="analyze")' in usage
    assert "does not generate an execution plan" in usage
    assert "THREE-STEP" not in usage
    assert "project_analyzer(action" not in usage


def test_split_root_test_hint_is_coordinates_only():
    """A bigtop-shape recommendation renders 'Test coordinates', never
    'Recommended Tests' (dim b)."""
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
    out = render_project_fact_sheet(analysis)
    assert "Recommended Tests" not in out
    assert "Test coordinates: gradle at /workspace/p/tests-live-here" in out
    assert "Recommended Build" not in out


def test_python_fact_projection_is_bounded_and_ignores_prescriptive_fields():
    analysis = {
        "project_path": "/workspace/native",
        "project_type": "Python",
        "build_system": "python",
        "existing_files": ["pyproject.toml"],
        "python_config": {
            "python_distribution_name": "native-dist",
            "python_build_backend": "scikit_build_core.build",
            "python_root": "/workspace/native",
            "python_local_providers": [
                {
                    "distribution_name": f"provider-{index}",
                    "root": f"vendor/provider-{index}",
                    "goal": "install this first",
                }
                for index in range(5)
            ],
            "native_artifact_roots": [f"build/root-{index}" for index in range(5)],
            "python_smoke_candidates": [
                {
                    "path": f"tests/smoke-{index}",
                    "source": "pyproject.toml",
                    "rationale": "run this next",
                }
                for index in range(5)
            ],
        },
    }

    output = render_project_fact_sheet(analysis)

    assert "provider-0 at vendor/provider-0" in output
    assert "provider-2 at vendor/provider-2" in output
    assert "provider-3" not in output
    assert "tests/smoke-2 (pyproject.toml)" in output
    assert "tests/smoke-3" not in output
    assert "build/root-3" not in output
    assert "(+2 more in /workspace/.setup_agent/build_requirements.json)" in output
    assert "install this first" not in output
    assert "run this next" not in output


# ---- dim (b): recommendation is coordinates only ----------------------------


def test_recommendation_keeps_coordinates_drops_actions():
    tool = ProjectAnalyzerTool(SurveyOrch())
    result = tool.execute(action="analyze", project_path="/workspace/proj")
    rec = result.metadata.get("build_recommendation") or {}
    assert rec.get("build_root")  # coordinate facts retained (shared machinery)
    assert "goal" not in rec and "rationale" not in rec
    assert "Recommended Build" not in format_tool_result("project", result)


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


def test_native_smoke_steer_delegates_bounded_target_to_the_tool():
    # Weak models must not fill a path placeholder. The bare call is safe
    # because the tool owns the survey-verified bounded selector (or refuses).
    from sag.agent.react_engine import NATIVE_NOT_BUILT_TEST_GUIDANCE

    assert "bare build(action='test')" in NATIVE_NOT_BUILT_TEST_GUIDANCE
    assert "NO args" in NATIVE_NOT_BUILT_TEST_GUIDANCE
    assert "survey-verified bounded smoke target" in NATIVE_NOT_BUILT_TEST_GUIDANCE
    assert "Never invent, guess, or substitute a test path" in NATIVE_NOT_BUILT_TEST_GUIDANCE
    assert "<that file>" not in NATIVE_NOT_BUILT_TEST_GUIDANCE
    assert "args=" not in NATIVE_NOT_BUILT_TEST_GUIDANCE
    assert "--maxfail=1" not in NATIVE_NOT_BUILT_TEST_GUIDANCE


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

    from test_python_phase_guidance import _engine_at, _python_env

    from sag.tools.internal.build_preflight import REQUIREMENTS_PATH

    manifest = {
        "build_islands": [
            {"root": "/workspace/p/a", "system": "maven", "goal": "install"},
            {"root": "/workspace/p/b", "system": "gradle", "goal": "publishToMavenLocal"},
        ]
    }

    class ManifestOrch:
        def execute_command(self, command, **kwargs):
            if command in (
                f"cat {REQUIREMENTS_PATH}",
                f"cat -- {REQUIREMENTS_PATH}",
            ):
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
    from sag.config.prescriptions import feature_flags_for_mask
    from scripts.collect_control_layer_ab import CollectionError, _verify_prescription_pin

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
