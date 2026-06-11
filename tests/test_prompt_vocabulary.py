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

from sag.agent.agent_state_evaluator import AgentStateEvaluator, AgentStatus
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
    assert "use the build tool" in source


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
