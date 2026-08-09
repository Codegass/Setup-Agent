# tests/test_no_exclusion_coaching.py
"""Source-level tripwire: no SAG tool may coach test exclusion or skipping
(spec §3.4-3). The strings below were live in the 2026-07-24 bigtop run."""

from pathlib import Path

BANNED = ["-Dtest=!", "-DskipTests=true", "skipTests=true", "-pl !"]
BANNED_HARNESS_PROPOSALS = [
    "accept the repair it proposes",
    "using: project(action='provision'",
    "Or manually: bash(command='apt-get",
    "recovery_actions",
    "diagnostic_commands",
]
ROOT = Path(__file__).parents[1]
ACTIVE_COACHING_MODULES = [
    ROOT / "src/sag/agent/react_engine.py",
    ROOT / "src/sag/agent/react_prompt_builder.py",
    ROOT / "src/sag/agent/tool_orchestration.py",
    ROOT / "src/sag/config/prompts/react_engine.yaml",
    ROOT / "src/sag/tools/bash.py",
    ROOT / "src/sag/tools/build/build_tool.py",
    ROOT / "src/sag/tools/internal/maven_tool.py",
    ROOT / "src/sag/tools/internal/project_setup_tool.py",
    ROOT / "src/sag/tools/internal/python_tool.py",
    ROOT / "src/sag/tools/internal/system_tool.py",
    ROOT / "src/sag/tools/report_tool.py",
]
DEAD_RECOVERY_MODULES = [
    ROOT / "src/sag/agent/repair_contracts.py",
    ROOT / "src/sag/agent/retry_authority.py",
    ROOT / "src/sag/agent/tool_recovery.py",
]
JUDGE_FEEDBACK_MODULES = [
    ROOT / "src/sag/agent/phase_gates.py",
    ROOT / "src/sag/tools/phase_tool.py",
]


def test_no_active_module_coaches_exclusions():
    # Evidence readers may need to recognize an exclusion flag after it ran;
    # only model/tool guidance surfaces are forbidden from recommending one.
    for module in ACTIVE_COACHING_MODULES:
        source = module.read_text(encoding="utf-8")
        for banned in BANNED:
            assert banned not in source, f"{module} still contains {banned!r}"


def test_harness_authored_recovery_modules_are_absent():
    assert [path for path in DEAD_RECOVERY_MODULES if path.exists()] == []


def test_active_build_guidance_has_no_harness_authored_repair_call():
    for module in ACTIVE_COACHING_MODULES:
        source = module.read_text(encoding="utf-8")
        for banned in BANNED_HARNESS_PROPOSALS:
            assert banned not in source, f"{module} still contains {banned!r}"


def test_retired_live_repair_projections_do_not_return():
    forbidden = [
        "maven_pom_recovery",
        "maven_multimodule_testing",
        "replacement_args",
        "_recommended_workdir",
        "Untried recommended islands",
        "_untried_island_targets",
        "Suggested next steps",
        "Appended Maven fail-at-end",
        "changing action to",
        "Injected working directory from successful state",
        "Inferred working directory from repository URL",
    ]
    for module in ACTIVE_COACHING_MODULES:
        source = module.read_text(encoding="utf-8")
        for phrase in forbidden:
            assert phrase not in source, f"{module} still contains {phrase!r}"


def test_judge_feedback_does_not_select_project_or_terminal_calls():
    forbidden = [
        "Clone first: project(action='clone'",
        "Run build(action='compile')",
        "Run build(action='test')",
        "claim phase(action='done'",
        "end the phase with phase(action='done'",
        "Generate it with the report tool, then re-claim",
    ]
    for module in JUDGE_FEEDBACK_MODULES:
        source = module.read_text(encoding="utf-8")
        for phrase in forbidden:
            assert phrase not in source, f"{module} still contains {phrase!r}"
