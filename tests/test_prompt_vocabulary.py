"""Stage-1 prompt sweep contract (plan Task 8).

Model-facing vocabulary must teach the consolidated tool surface
(build / project / search alongside bash / file_io / manage_context /
report) instead of the legacy maven / gradle / project_setup /
project_analyzer / web_search / output_search / system / env names.

react_engine.yaml section-start line numbers are referenced from
react_prompt_builder.py `# Prompt:` comments — the sweep must keep
those line numbers stable.
"""

import re
from pathlib import Path
from types import SimpleNamespace

import yaml

from sag.agent.agent_state_evaluator import AgentStateEvaluator, AgentStatus
from sag.agent.react_prompt_builder import ReActPromptBuilder
from sag.config.prompt_loader import load_react_engine_prompts
from sag.tools.context_tool import ContextTool

REPO_SRC = Path(__file__).resolve().parents[1] / "src" / "sag"
YAML_PATH = REPO_SRC / "config" / "prompts" / "react_engine.yaml"
BUILDER_PATH = REPO_SRC / "agent" / "react_prompt_builder.py"
AGENT_PATH = REPO_SRC / "agent" / "agent.py"

# Legacy tool-invocation vocabulary that must no longer be taught to the model.
LEGACY_PROMPT_PATTERNS = (
    "project_setup",
    "project_analyzer",
    "web_search",
    "output_search",
    "maven(",
    "gradle(",
    "maven tool",
    "gradle tool",
    "env register",
    "env activate",
    "env block",
    "- system:",
)


def test_react_engine_yaml_drops_legacy_tool_vocabulary():
    text = YAML_PATH.read_text()
    offenders = [pattern for pattern in LEGACY_PROMPT_PATTERNS if pattern in text]
    assert offenders == [], f"legacy tool vocabulary still in react_engine.yaml: {offenders}"


def test_react_engine_yaml_teaches_consolidated_tools():
    text = YAML_PATH.read_text()
    assert "build(action=" in text or "build(action'" in text
    assert "project(action=" in text
    assert "web:" in text and "search" in text


def test_react_engine_yaml_section_line_references_stay_valid():
    """The sweep must not shift the yaml lines referenced from the builder."""
    yaml_lines = YAML_PATH.read_text().splitlines()
    refs = re.findall(
        r"react_engine\.yaml:(\d+) [\w]+\.([\w]+)", BUILDER_PATH.read_text()
    )
    assert refs, "expected # Prompt: react_engine.yaml:<line> comments in builder"
    for line_number, key in refs:
        line = yaml_lines[int(line_number) - 1]
        assert line.lstrip().startswith(f"{key}:"), (
            f"react_engine.yaml:{line_number} expected to start section '{key}:' "
            f"but found: {line!r}"
        )


def test_default_task_templates_use_consolidated_names():
    source = AGENT_PATH.read_text()
    assert "use project_setup tool" not in source
    assert "use project_analyzer tool" not in source
    assert "MUST use project_analyzer tool" not in source
    assert "use maven/gradle tools" not in source
    # Stage 2: the default trunk templates are the phase objectives; the setup
    # prompt prescribes the consolidated build/project facades.
    assert "build(action=" in source
    assert "project(action=" in source


class _BranchHistory:
    def __init__(self, entries):
        self.history = entries


class _AnalyzerEvidenceCM:
    """Just enough ContextManager surface for _check_project_analyzer_execution."""

    current_task_id = "task_2"
    output_storage = None

    def __init__(self, entries):
        self._entries = entries

    def load_branch_history(self, task_id):
        return _BranchHistory(self._entries)

    def load_trunk_context(self):
        return None


def test_analyzer_evidence_accepts_project_facade_analyze_output():
    tool = ContextTool(
        _AnalyzerEvidenceCM(
            [
                {
                    "type": "action",
                    "tool_name": "project",
                    "success": True,
                    "output": "🔍 PROJECT ANALYSIS COMPLETED\n\n📁 Analyzed Path: /workspace/x",
                }
            ]
        )
    )
    assert tool._check_project_analyzer_execution() is True


def test_analyzer_evidence_rejects_project_clone_only_history():
    tool = ContextTool(
        _AnalyzerEvidenceCM(
            [
                {
                    "type": "action",
                    "tool_name": "project",
                    "success": True,
                    "output": "✅ Repository cloned to /workspace/x",
                }
            ]
        )
    )
    assert tool._check_project_analyzer_execution() is False


def test_analyzer_completion_suggestions_use_project_vocabulary():
    tool = ContextTool(_AnalyzerEvidenceCM([]))
    task = SimpleNamespace(
        id="task_2",
        description=(
            "CRITICAL: Run project(action='analyze') to analyze project structure "
            "and generate intelligent execution plan"
        ),
    )

    validation = tool._validate_task_completion(task, "did things", "results")

    assert validation["valid"] is False
    blob = validation["reason"] + " ".join(validation["suggestions"])
    assert "project(action='analyze')" in blob
    assert "project_analyzer" not in blob


class _Task2CM:
    current_task_id = "task_2"


def test_task2_analyzer_requirement_satisfied_by_project_analyze_step():
    evaluator = AgentStateEvaluator(_Task2CM())
    steps = [
        SimpleNamespace(
            tool_name="project",
            tool_params={"action": "analyze"},
            tool_result=SimpleNamespace(success=True),
            input=None,
        ),
        SimpleNamespace(
            tool_name="file_io",
            tool_params={"action": "read"},
            tool_result=SimpleNamespace(success=True),
            input="read pom.xml",
        ),
    ]

    analysis = evaluator._check_task2_project_analyzer_requirement(steps)

    assert analysis.status == AgentStatus.PROCEEDING


def test_task2_analyzer_guidance_uses_project_vocabulary():
    evaluator = AgentStateEvaluator(_Task2CM())
    steps = [
        SimpleNamespace(
            tool_name="bash",
            tool_params=None,
            tool_result=SimpleNamespace(success=True),
            input="cat pom.xml",
        )
    ]

    analysis = evaluator._check_task2_project_analyzer_requirement(steps)

    assert analysis.status == AgentStatus.STUCK
    assert "project(action='analyze'" in analysis.guidance_message
    assert "project_analyzer" not in analysis.guidance_message.lower()


# --- Stage 2 (plan Task 9): setup prompts teach the phase vocabulary -------
#
# Setup runs talk to the engine-owned phase machine through the `phase` tool;
# manage_context is not registered there, so no setup-mode section may teach
# the task ceremony (start_task / complete_with_results / task ids). Run-task
# mode keeps the legacy manage_context surface untouched.

SETUP_FORBIDDEN_CEREMONY = (
    "manage_context",
    "complete_with_results",
    "start_task",
    "task_1",
    "task_id",
)


def _prompt_sections():
    data = yaml.safe_load(YAML_PATH.read_text())
    return {
        f"{group}.{name}": text
        for group, sections in data.items()
        for name, text in sections.items()
    }


def test_setup_yaml_sections_drop_task_ceremony():
    offenders = {
        key: [pattern for pattern in SETUP_FORBIDDEN_CEREMONY if pattern in text]
        for key, text in _prompt_sections().items()
        if not key.split(".", 1)[1].startswith("run_task_")
        and any(pattern in text for pattern in SETUP_FORBIDDEN_CEREMONY)
    }
    assert offenders == {}, f"setup-mode sections still teach task ceremony: {offenders}"


def test_setup_yaml_sections_teach_phase_verbs():
    sections = _prompt_sections()
    lifecycle = sections["initial_system.context_management"]
    assert 'phase(action="done"' in lifecycle or "phase(action='done'" in lifecycle
    assert 'phase(action="blocked"' in lifecycle or "phase(action='blocked'" in lifecycle
    assert "provision" in lifecycle and "report" in lifecycle, (
        "phase order must be visible so the model never tries to reorder phases"
    )


def test_run_task_yaml_sections_keep_manage_context_and_no_phase_tool():
    sections = _prompt_sections()
    assert "manage_context" in sections["initial_system.run_task_context_management"]
    assert "manage_context" in sections["initial_system.run_task_tool_clarification"]
    run_task_text = "\n".join(
        text
        for key, text in sections.items()
        if key.split(".", 1)[1].startswith("run_task_")
    )
    assert "phase(action=" not in run_task_text


class _PromptCM:
    def get_current_context_info(self):
        return {"context_type": "trunk", "context_id": "trunk"}

    def load_trunk_context(self):
        return None


def _initial_prompt(workflow_mode):
    builder = ReActPromptBuilder(
        prompts=load_react_engine_prompts(),
        context_manager=_PromptCM(),
        tools={},
    )
    return builder.build_initial_system_prompt(
        repository_url="https://example.test/repo.git",
        repository_ref=None,
        tool_calling_enabled=True,
        workflow_mode=workflow_mode,
    )


def test_setup_prompt_teaches_phase_verbs_not_task_ceremony():
    prompt = _initial_prompt("setup")
    assert 'phase(action="done"' in prompt or "phase(action='done'" in prompt
    assert 'phase(action="blocked"' in prompt or "phase(action='blocked'" in prompt
    assert "complete_with_results" not in prompt
    assert "manage_context" not in prompt
    assert "start_task" not in prompt


def test_run_task_prompt_keeps_manage_context_surface():
    prompt = _initial_prompt("run_task")
    assert "manage_context" in prompt
    assert "complete_with_results" in prompt
    assert "phase(action=" not in prompt
