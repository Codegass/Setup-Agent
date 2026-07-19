"""The framework guarantees the project survey (analyzer diet, Category 1).

Live evidence: the manifest (`build_requirements.json`) that EIGHT framework
components read was written only inside the agent-invoked
`project(action='analyze')`. The 2026-07-13 pyyaml run skipped analyze — the
whole install chain starved on an empty manifest. Mechanical plumbing must not
depend on the agent's tool choices: the engine now ensures the survey at
build/test entry, with zero LLM tokens (container commands only).
"""

from types import SimpleNamespace

from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.project_analyzer import ProjectAnalyzerTool


class SurveyOrch:
    """Minimal python-shaped repo: answers probes, captures the manifest write."""

    def __init__(self):
        self.files = {}
        self.commands = []

    def execute_command(self, command, workdir=None, timeout=None, **kwargs):
        self.commands.append(command)
        if "<<" in command and REQUIREMENTS_PATH in command:
            body = command.split("<<'SAGEOF'\n", 1)[1].rsplit("\nSAGEOF", 1)[0]
            self.files[REQUIREMENTS_PATH] = body
            return {"success": True, "exit_code": 0, "output": ""}
        if command == f"cat {REQUIREMENTS_PATH}":
            if REQUIREMENTS_PATH in self.files:
                return {"success": True, "exit_code": 0, "output": self.files[REQUIREMENTS_PATH]}
            return {"success": False, "exit_code": 1, "output": ""}
        if command.startswith("test -f /workspace/proj/pyproject.toml"):
            return {"success": True, "exit_code": 0, "output": "exists"}
        if "find /workspace/proj" in command and "pyproject.toml" in command:
            return {"success": True, "exit_code": 0, "output": "/workspace/proj/pyproject.toml"}
        if command.startswith("cat /workspace/proj/pyproject.toml"):
            return {
                "success": True,
                "exit_code": 0,
                "output": '[project]\nname = "proj"\nrequires-python = ">=3.9"\n',
            }
        if command.startswith("ls /workspace/proj") or command.startswith("ls -la /workspace"):
            return {"success": True, "exit_code": 0, "output": "pyproject.toml\nsrc\n"}
        return {"success": True, "exit_code": 0, "output": ""}


def _analyzer(orch):
    tool = ProjectAnalyzerTool.__new__(ProjectAnalyzerTool)
    tool.orchestrator = orch
    tool.docker_orchestrator = orch
    tool.context_manager = None
    return tool


def test_ensure_facts_computes_and_persists_when_manifest_absent():
    orch = SurveyOrch()
    ran = _analyzer(orch).ensure_facts("/workspace/proj")
    assert ran is True
    assert REQUIREMENTS_PATH in orch.files  # the mechanical chain is fed


def test_ensure_facts_is_idempotent_when_manifest_exists():
    orch = SurveyOrch()
    analyzer = _analyzer(orch)
    assert analyzer.ensure_facts("/workspace/proj") is True
    before = len(orch.commands)
    assert analyzer.ensure_facts("/workspace/proj") is False
    # second call: only the manifest existence probe, no re-analysis
    assert len(orch.commands) - before <= 2


def test_ensure_facts_never_raises_on_broken_container():
    class Exploding:
        def execute_command(self, command, **kwargs):
            raise RuntimeError("container gone")

    assert _analyzer(Exploding()).ensure_facts("/workspace/proj") is False


def test_engine_intro_ensures_facts_and_notes_a_framework_survey(monkeypatch):
    from test_python_phase_guidance import _engine_at, _python_env

    calls = []
    monkeypatch.setattr(
        ProjectAnalyzerTool,
        "ensure_facts",
        lambda self, path="/workspace": calls.append(path) or True,
    )
    engine = _engine_at(2, _python_env())  # build phase
    engine.physical_validator = SimpleNamespace(docker_orchestrator=SurveyOrch())
    intro = engine._phase_intro_step().content
    assert calls, "the engine must guarantee the survey at build entry"
    assert "framework survey" in intro.lower()


def test_engine_intro_stays_quiet_when_survey_already_done(monkeypatch):
    from test_python_phase_guidance import _engine_at, _python_env

    monkeypatch.setattr(
        ProjectAnalyzerTool, "ensure_facts", lambda self, path="/workspace": False
    )
    engine = _engine_at(2, _python_env())
    engine.physical_validator = SimpleNamespace(docker_orchestrator=SurveyOrch())
    assert "framework survey" not in engine._phase_intro_step().content.lower()
