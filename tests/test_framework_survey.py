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
from sag.tools.internal.project_analyzer import SURVEY_FACTS_VERSION, ProjectAnalyzerTool


class SurveyOrch:
    """Minimal python-shaped repo: answers probes, captures the manifest write."""

    def __init__(self, *, drop_manifest_writes=False):
        self.files = {}
        self.commands = []
        self.manifest_writes = 0
        self.drop_manifest_writes = drop_manifest_writes
        # The fingerprint probe digests config content AND the package
        # layout; tests mutate either to simulate an edit vs. a rename.
        self.config_seed = "pyproject-v1"
        self.package_layout = ("alpha_pkg",)

    def execute_command(self, command, workdir=None, timeout=None, **kwargs):
        self.commands.append(command)
        if "| cksum" in command:
            if not self.config_seed:  # empty seed simulates a broken probe
                return {"success": False, "exit_code": 1, "output": ""}
            seed = self.config_seed + "|" + "|".join(self.package_layout)
            digest = sum(map(ord, seed))
            return {"success": True, "exit_code": 0, "output": f"{digest} {len(seed)}"}
        if "<<" in command and REQUIREMENTS_PATH in command:
            self.manifest_writes += 1
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
    tool = ProjectAnalyzerTool(orch, IntegrationCM())  # both persisted ends in play
    assert tool.ensure_facts("/workspace/proj") == "created"
    writes_before = orch.manifest_writes
    before = len(orch.commands)
    assert tool.ensure_facts("/workspace/proj") == "present"
    # The identity check (re-review P2: stamp must match THIS project) costs a
    # few discovery probes — but present must never re-analyze or re-write.
    assert orch.manifest_writes == writes_before
    assert len(orch.commands) - before <= 12


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


def test_stale_manifest_with_dropped_rewrite_is_failed_not_created():
    """Re-review P1: the old stale file keeps the readback non-empty when the
    replacement write is dropped — 'created' must verify THIS survey's stamp."""
    orch = SurveyOrch(drop_manifest_writes=True)
    orch.files[REQUIREMENTS_PATH] = '{"survey": {"analyzer_version": 0}}'
    assert ProjectAnalyzerTool(orch).ensure_facts("/workspace/proj") == "failed"


def test_same_version_manifest_for_another_project_resurveys():
    """Re-review P2: version match alone must not pass — project identity too."""
    orch = SurveyOrch()
    orch.files[REQUIREMENTS_PATH] = (
        '{"survey": {"analyzer_version": %d, "project_path": "/workspace/other"}}'
        % SURVEY_FACTS_VERSION
    )
    assert ProjectAnalyzerTool(orch).ensure_facts("/workspace/proj") == "created"
    assert '"/workspace/proj"' in orch.files[REQUIREMENTS_PATH]


def test_config_edit_invalidates_the_fast_path_and_resurveys():
    """Category 2 staleness contract: the facts follow the config they were
    derived from — an edited build file re-surveys instead of serving the
    stale manifest as 'present'."""
    orch = SurveyOrch()
    tool = ProjectAnalyzerTool(orch)
    assert tool.ensure_facts("/workspace/proj") == "created"

    orch.config_seed = "pyproject-v2-edited"  # someone edited the config
    assert tool.ensure_facts("/workspace/proj") == "created"  # NOT 'present'
    assert orch.manifest_writes == 2  # a real re-survey with fresh facts

    # Unchanged config afterwards: back to the fast path.
    assert tool.ensure_facts("/workspace/proj") == "present"


def test_unreadable_fingerprint_degrades_to_present_not_thrash():
    """A flaky fingerprint probe means CANNOT COMPARE — the fast path must
    keep serving the surveyed facts, not re-survey on every intro."""
    orch = SurveyOrch()
    tool = ProjectAnalyzerTool(orch)
    assert tool.ensure_facts("/workspace/proj") == "created"

    orch.config_seed = ""  # probe output becomes empty -> fingerprint None
    assert tool.ensure_facts("/workspace/proj") == "present"
    assert orch.manifest_writes == 1


def test_failed_trunk_save_retries_and_recovers_on_the_next_call():
    """Final review P1: the failed first call leaves a CURRENT-stamp manifest
    behind (the manifest write precedes the trunk save), so the second call
    hit the fast path and returned 'present' — the env-summary was never
    retried and the objective could stay wrong for the whole run. The fast
    path must require the stamp on BOTH persisted ends."""

    class FlakyCM(IntegrationCM):
        def __init__(self):
            super().__init__()
            self.fail_next_save = True

        def _save_trunk_context(self, trunk):
            if self.fail_next_save:
                self.fail_next_save = False
                raise RuntimeError("context store briefly unavailable")
            super()._save_trunk_context(trunk)

    orch = SurveyOrch()
    cm = FlakyCM()
    tool = ProjectAnalyzerTool(orch, cm)

    assert tool.ensure_facts("/workspace/proj") == "failed"
    # The manifest DID land with a current stamp — the exact bug precondition.
    assert REQUIREMENTS_PATH in orch.files
    # The unsaved stamp must not linger on the cached in-memory trunk, or the
    # both-ends check would trust an env-summary that never landed.
    assert cm.trunk.environment_summary.get("survey") is None

    assert tool.ensure_facts("/workspace/proj") == "created"  # NOT 'present'
    assert orch.manifest_writes == 2  # a real re-survey, not a fast path
    assert cm.saves >= 1
    assert cm.trunk.environment_summary["survey"]["project_path"] == "/workspace/proj"


def test_trunk_persistence_failure_means_failed():
    """Re-review P1: the guarantee is manifest AND trunk env metrics — a failed
    trunk save leaves the env stale and the objective wrong."""

    class BrokenCM:
        def load_trunk_context(self):
            raise RuntimeError("trunk store unavailable")

    orch = SurveyOrch()
    tool = ProjectAnalyzerTool(orch, BrokenCM())
    assert tool.ensure_facts("/workspace/proj") == "failed"


def test_fingerprint_command_covers_everything_the_survey_reads():
    """Category-2 review P1s: a root-only concatenation missed parent POMs,
    nested island build files, lockfiles and wrapper markers; the second
    round added the remaining survey inputs — detection markers (Cargo, Go,
    Make), READMEs, outside-root parent POMs, test sources, and the
    module-dir layout. The probe must enumerate recursively by name with
    per-file digests, pruning build output."""
    orch = SurveyOrch()
    ProjectAnalyzerTool(orch).ensure_facts("/workspace/proj")
    cmd = next(c for c in orch.commands if "| cksum" in c)
    assert cmd.strip().startswith("cd /workspace/proj && ")
    for source in (
        "pom.xml",
        "settings.gradle",
        "gradlew",
        "requirements*.txt",
        "poetry.lock",
        "Pipfile.lock",
        "CMakeLists.txt",
        "Cargo.toml",
        "go.mod",
        "Makefile",
        "README*",
    ):
        assert source in cmd
    for pruned in ("target", ".git", "node_modules"):
        assert pruned in cmd
    # Outside-root parent POMs (the maven analysis probes ../<artifact>/pom.xml).
    assert "find .. -maxdepth 2 -type f -name pom.xml" in cmd
    # Test sources: the trunk's annotation counts derive from their content.
    assert "*/src/test/java/*" in cmd
    # Module-layout dirs ride as a listing: existence changes island facts.
    assert "-type d" in cmd and "*/src/main/java" in cmd
    # Per-file cksum lines (name + size + checksum) feed the final cksum:
    # names, existence, and content boundaries are all encoded.
    assert "xargs -r cksum" in cmd and cmd.rstrip().endswith("| cksum")


def test_package_rename_with_unchanged_config_resurveys():
    """Final Category-2 review P1: python_packages derives from __init__.py
    PATHS and rides the manifest into the validator — renaming alpha_pkg to
    beta_pkg changes the fact with zero config-file change. The layout is
    part of the fingerprint domain, so the rename must re-survey, never
    serve the stale package name as 'present'."""
    orch = SurveyOrch()
    tool = ProjectAnalyzerTool(orch)
    assert tool.ensure_facts("/workspace/proj") == "created"

    orch.package_layout = ("beta_pkg",)  # rename; config_seed untouched
    assert tool.ensure_facts("/workspace/proj") == "created"  # NOT 'present'
    assert orch.manifest_writes == 2  # fresh facts, not the stale manifest

    # The probe really sweeps the layout: __init__.py paths ride the command.
    cmd = next(c for c in orch.commands if "| cksum" in c)
    assert "-name __init__.py" in cmd


def test_config_edit_with_dropped_rewrite_is_failed_not_created():
    """Final Category-2 review P1: after a config edit forces a re-survey,
    a DROPPED manifest rewrite leaves the old manifest on disk — it matches
    on version+path (same project, same analyzer), and only THIS survey's
    fingerprint tells the readback apart. 'created' must verify it."""
    orch = SurveyOrch()
    tool = ProjectAnalyzerTool(orch)
    assert tool.ensure_facts("/workspace/proj") == "created"  # S1 lands

    orch.config_seed = "pyproject-v2-edited"
    orch.drop_manifest_writes = True  # S2's rewrite is dropped
    assert tool.ensure_facts("/workspace/proj") == "failed"  # NOT 'created'


# ---- Integration: agent-skips-analyze, NO monkeypatching (re-review P2) ----


class _TrunkTask:
    def __init__(self, task_id):
        self.id = task_id


class StrictSurveyOrch(SurveyOrch):
    """Existence probes default to ABSENT so detection sees a clean python
    repo (the permissive default made pom.xml 'exist' and the analysis came
    out unknown/invalid)."""

    def execute_command(self, command, workdir=None, timeout=None, **kwargs):
        stripped = command.strip()
        if stripped.startswith("test -d "):
            self.commands.append(command)
            return {"success": True, "exit_code": 0, "output": ""}  # dirs exist
        if stripped.startswith(("test -f ", "test -e ")) and "pyproject" not in stripped:
            self.commands.append(command)
            return {"success": False, "exit_code": 1, "output": ""}
        if stripped.startswith("test -f /workspace/pyproject.toml"):
            self.commands.append(command)
            return {"success": True, "exit_code": 0, "output": "exists"}
        if stripped.startswith("cat /workspace/pyproject.toml"):
            self.commands.append(command)
            return {
                "success": True,
                "exit_code": 0,
                "output": '[project]\nname = "proj"\nrequires-python = ">=3.9"\n',
            }
        return super().execute_command(command, workdir=workdir, timeout=timeout, **kwargs)


class IntegrationCM:
    """A context manager whose trunk actually persists — the real
    _update_trunk_context_with_plan path runs against it unmocked."""

    def __init__(self):
        self.trunk = SimpleNamespace(
            environment_summary={},
            todo_list=[_TrunkTask("phase_build"), _TrunkTask("phase_test")],
        )
        self.saves = 0

    def load_trunk_context(self):
        return self.trunk

    def _save_trunk_context(self, trunk):
        self.saves += 1


def test_integration_skipped_analyze_run_ends_with_facts_and_python_objective():
    """The original done-bar criterion, unmocked: an agent that never called
    analyze reaches the build intro; the REAL survey pipeline runs against the
    (scripted) container, the manifest lands stamped, the trunk env is saved,
    and the objective selected in that same intro is the PYTHON one."""
    from test_python_phase_guidance import _engine_at

    cm = IntegrationCM()
    orch = StrictSurveyOrch()
    engine = _engine_at(2, cm.trunk.environment_summary)  # build phase, empty env
    engine.context_manager = cm  # the real survey writes THIS trunk
    engine.physical_validator = SimpleNamespace(docker_orchestrator=orch)

    intro = engine._phase_intro_step().content

    # facts persisted and stamped for this project
    assert REQUIREMENTS_PATH in orch.files
    assert '"project_path": "/workspace' in orch.files[REQUIREMENTS_PATH]
    # trunk env metrics actually saved (not just attempted)
    assert cm.saves >= 1
    assert cm.trunk.environment_summary.get("build_recommendation")
    # the trace names the framework survey, and the objective is python-side
    assert "framework survey ran" in intro
    assert "Never run mvn/gradle via bash" not in intro


class StoreCM(IntegrationCM):
    """A trunk store with real persistence semantics: load returns the last
    SAVED state, not the shared in-memory object — a dropped save must not
    leak through a cached reference."""

    def __init__(self):
        super().__init__()
        self._saved_env = dict(self.trunk.environment_summary)
        self.fail_next_save = False

    def load_trunk_context(self):
        self.trunk = SimpleNamespace(
            environment_summary=dict(self._saved_env),
            todo_list=[_TrunkTask("phase_build"), _TrunkTask("phase_test")],
        )
        return self.trunk

    def _save_trunk_context(self, trunk):
        if self.fail_next_save:
            self.fail_next_save = False
            raise RuntimeError("context store briefly unavailable")
        self.saves += 1
        self._saved_env = dict(trunk.environment_summary)


def test_config_edit_with_failed_trunk_save_does_not_serve_stale_trunk():
    """Final Category-2 review P1, exact repro: survey S1 stamps both ends;
    the config is edited; re-survey S2 lands the manifest (new fingerprint)
    but its trunk save fails. The store still holds S1's stamp — version and
    path MATCH, only the fingerprint disagrees. Without fingerprint agreement
    on both ends the next call returned 'present' over S1's stale env metrics
    forever."""
    orch = SurveyOrch()
    cm = StoreCM()
    tool = ProjectAnalyzerTool(orch, cm)
    assert tool.ensure_facts("/workspace/proj") == "created"  # S1

    orch.config_seed = "pyproject-v2-edited"
    cm.fail_next_save = True
    assert tool.ensure_facts("/workspace/proj") == "failed"  # S2: trunk save dropped
    # The bug precondition: S2's manifest landed, S1's trunk stamp survived.
    assert '"config_fingerprint"' in orch.files[REQUIREMENTS_PATH]

    assert tool.ensure_facts("/workspace/proj") == "created"  # NOT 'present'
    # Recovery leaves BOTH ends stamped with the SAME (new) fingerprint.
    import json

    manifest_stamp = json.loads(orch.files[REQUIREMENTS_PATH])["survey"]
    trunk_stamp = cm._saved_env["survey"]
    assert trunk_stamp["config_fingerprint"] == manifest_stamp["config_fingerprint"] is not None

    # And with both ends agreeing, the fast path is back.
    assert tool.ensure_facts("/workspace/proj") == "present"
