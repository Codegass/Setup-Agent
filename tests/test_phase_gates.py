"""Phase-boundary evidence gates (spec §3.1). Descriptive: a failed gate
returns evidence + options, never blocks tool use; probe errors fail OPEN."""

from types import SimpleNamespace

from sag.agent.phase_gates import check_phase_done


class FakeValidator:
    def __init__(self, build_success=True, build_system="maven", has_test_reports=True):
        self._b, self._s, self._t = build_success, build_system, has_test_reports

    def validate_build_status(self, project_name=None):
        return {"success": self._b, "evidence": {"build_system": self._s}, "reason": "scripted"}

    def validate_test_status(self, project_name=None):
        return {"has_test_reports": self._t, "status": "scripted"}


def _orch(java_ok=True, workspace_exists=True):
    def execute_command(command, **kwargs):
        if "java -version" in command:
            return {"exit_code": 0 if java_ok else 127, "output": "openjdk 17" if java_ok else ""}
        if "test -d" in command:
            return {"exit_code": 0, "output": "exists" if workspace_exists else "missing"}
        if "setup-report-" in command:
            return {"exit_code": 0, "output": "/workspace/setup-report-x.md"}
        return {"exit_code": 0, "output": ""}
    return SimpleNamespace(execute_command=execute_command)


def test_build_done_rejected_without_artifacts():
    verdict = check_phase_done(
        "build", validator=FakeValidator(build_success=False),
        orchestrator=_orch(), project_name="demo",
    )
    assert verdict["ok"] is False
    assert "artifact" in verdict["reason"].lower() or "build" in verdict["reason"].lower()
    assert verdict["suggestions"], "must offer options"


def test_build_done_accepted_with_artifacts():
    verdict = check_phase_done(
        "build", validator=FakeValidator(build_success=True),
        orchestrator=_orch(), project_name="demo",
    )
    assert verdict["ok"] is True


def test_build_gate_fails_open_for_non_jvm_systems():
    verdict = check_phase_done(
        "build", validator=FakeValidator(build_success=False, build_system="nodejs"),
        orchestrator=_orch(), project_name="demo",
    )
    assert verdict["ok"] is True, "artifact gate is maven/gradle-scoped (round-3 over-block fix)"


def test_test_done_rejected_without_reports():
    verdict = check_phase_done(
        "test", validator=FakeValidator(has_test_reports=False),
        orchestrator=_orch(), project_name="demo",
    )
    assert verdict["ok"] is False
    assert "report" in verdict["reason"].lower()


def test_provision_rejected_without_workspace():
    verdict = check_phase_done(
        "provision", validator=FakeValidator(),
        orchestrator=_orch(workspace_exists=False), project_name="demo",
    )
    assert verdict["ok"] is False


def test_gate_fails_open_on_probe_error():
    class Exploding:
        def validate_build_status(self, project_name=None):
            raise RuntimeError("docker down")

    verdict = check_phase_done(
        "build", validator=Exploding(), orchestrator=_orch(), project_name="demo",
    )
    assert verdict["ok"] is True, "infrastructure failure must never trap the model"


def test_analyze_always_passes_with_note():
    # Analysis quality is advisory; an honest 'unknown' must not trap the run.
    verdict = check_phase_done(
        "analyze", validator=FakeValidator(), orchestrator=_orch(), project_name="demo",
    )
    assert verdict["ok"] is True
