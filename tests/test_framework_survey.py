"""The framework guarantees the project survey (analyzer diet, Category 1).

Live evidence: the manifest that EIGHT framework components read was written
only inside the agent-invoked ``project(action='analyze')`` — the 2026-07-13
pyyaml run skipped analyze and the install chain starved. Review 2026-07-19
added three hard requirements covered here: the guarantee must work through
the PRODUCTION constructor (the first cut read ``self.orchestrator`` which the
constructor never sets — and the fixture masked it by injecting both
attributes); ``created`` may only be returned after the manifest is VERIFIED
on disk; and the survey must run BEFORE the phase objective is selected, or a
Python repo gets the Java objective in the same intro as Python guidance.
"""

from types import SimpleNamespace

from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.project_analyzer import ProjectAnalyzerTool


class SurveyOrch:
    """Minimal python-shaped repo: answers probes, captures the manifest write."""

    def __init__(self, *, drop_manifest_writes=False):
        self.files = {}
        self.commands = []
        self.drop_manifest_writes = drop_manifest_writes

    def execute_command(self, command, workdir=None, timeout=None, **kwargs):
        self.commands.append(command)
        if "<<" in command and REQUIREMENTS_PATH in command:
            if not self.drop_manifest_writes:
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


def test_ensure_facts_works_through_the_production_constructor():
    """Review P1: the first cut read self.orchestrator, which the REAL
    constructor never sets — production silently no-oped while a hand-built
    fixture (injecting both attributes) passed. This test uses the production
    constructor only."""
    orch = SurveyOrch()
    tool = ProjectAnalyzerTool(orch)  # the real __init__, nothing injected
    assert tool.ensure_facts("/workspace/proj") == "created"
    assert REQUIREMENTS_PATH in orch.files


def test_created_requires_the_manifest_verified_on_disk():
    """Review P1: success is what the READERS can see, not what was attempted."""
    orch = SurveyOrch(drop_manifest_writes=True)
    assert ProjectAnalyzerTool(orch).ensure_facts("/workspace/proj") == "failed"


def test_present_when_manifest_exists_and_no_reanalysis_happens():
    orch = SurveyOrch()
    tool = ProjectAnalyzerTool(orch)
    assert tool.ensure_facts("/workspace/proj") == "created"
    before = len(orch.commands)
    assert tool.ensure_facts("/workspace/proj") == "present"
    assert len(orch.commands) - before <= 2  # only the manifest probe


def test_agent_written_manifest_without_stamp_counts_as_present():
    """Zero behavior change when the agent DID call analyze (pre-stamp
    manifests stay authoritative)."""
    orch = SurveyOrch()
    orch.files[REQUIREMENTS_PATH] = '{"java_version": "17"}'
    assert ProjectAnalyzerTool(orch).ensure_facts("/workspace/proj") == "present"


def test_stale_analyzer_version_triggers_resurvey():
    orch = SurveyOrch()
    orch.files[REQUIREMENTS_PATH] = '{"survey": {"analyzer_version": 0}}'
    assert ProjectAnalyzerTool(orch).ensure_facts("/workspace/proj") == "created"


def test_never_raises_on_broken_container():
    class Exploding:
        def execute_command(self, command, **kwargs):
            raise RuntimeError("container gone")

    assert ProjectAnalyzerTool(Exploding()).ensure_facts("/workspace/proj") == "failed"
    assert ProjectAnalyzerTool(None).ensure_facts("/workspace/proj") == "failed"


# ---- Engine ordering: survey BEFORE the objective is selected ----


def _mutable_engine(phase_done_count, env):
    from test_python_phase_guidance import _engine_at

    engine = _engine_at(phase_done_count, env)
    engine.physical_validator = SimpleNamespace(docker_orchestrator=SurveyOrch())
    return engine


def test_survey_runs_before_objective_selection(monkeypatch):
    """Review P1: with analyze skipped on a Python repo, the objective was
    chosen from the STALE env (Java) while the same intro carried Python
    guidance. The survey must feed the objective."""
    env = {}  # analyze skipped: nothing on the trunk yet

    def fake_survey(self):
        env["build_recommendation"] = {
            "build_system": "python",
            "build_root": "/workspace/proj",
            "goal": "deps",
            "rationale": "Python project (pip).",
        }
        return "created"

    from sag.agent.react_engine import ReActEngine

    monkeypatch.setattr(ReActEngine, "_ensure_project_facts", fake_survey)
    engine = _mutable_engine(2, env)  # build phase
    intro = engine._phase_intro_step().content
    assert "framework survey ran" in intro
    # the objective must be the PYTHON one, selected AFTER the survey
    assert "Never run mvn/gradle via bash" not in intro  # java objective marker
    assert "build(action='deps')" in intro


def test_no_trace_line_and_no_behavior_change_when_survey_present(monkeypatch):
    from test_python_phase_guidance import _python_env

    from sag.agent.react_engine import ReActEngine

    monkeypatch.setattr(ReActEngine, "_ensure_project_facts", lambda self: "present")
    engine = _mutable_engine(2, _python_env())
    assert "framework survey" not in engine._phase_intro_step().content.lower()


def test_test_phase_intro_also_runs_the_guarantee(monkeypatch):
    from test_python_phase_guidance import _python_env

    from sag.agent.react_engine import ReActEngine

    calls = []
    monkeypatch.setattr(
        ReActEngine, "_ensure_project_facts", lambda self: calls.append(1) or "present"
    )
    _mutable_engine(3, _python_env())._phase_intro_step()
    assert calls
