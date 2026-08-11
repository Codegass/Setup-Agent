from sag.main import _run_coverage_evidence_pass, _run_coverage_pass


def test_run_coverage_pass_invokes_apply(monkeypatch):
    calls = {}

    class Orch:
        def execute_command(self, command, **kwargs):
            if "project_meta.json" in command:
                return {"success": True, "exit_code": 0, "output": '{"project_name": "caffeine"}'}
            return {"success": True, "exit_code": 0, "output": ""}

    import sag.main as m

    monkeypatch.setattr(m, "_detect_coverage_build_system", lambda orch, project_dir: "gradle")

    def fake_apply(orch, project_dir, build_system=None):
        calls["project_dir"] = project_dir
        calls["build_system"] = build_system
        return True

    monkeypatch.setattr(m, "apply_coverage", fake_apply)
    ok = _run_coverage_pass(Orch(), "caffeine")
    assert ok is True
    assert calls["project_dir"] == "/workspace/caffeine"
    assert calls["build_system"] == "gradle"


def test_run_coverage_pass_reuses_the_validators_cached_module_scan(monkeypatch):
    calls = {}

    class Validator:
        def module_scan(self, project_name):
            assert project_name == "dbcp"
            return {
                "summary": {"modules_total": 1, "modules_built": 1},
                "modules": [{"name": ".", "path": ".", "build_status": "success"}],
                "project_dir": "/workspace/dbcp",
            }

    class Orch:
        def execute_control_command(self, command, **kwargs):
            return {"success": True, "exit_code": 0, "output": ""}

    import sag.main as m

    monkeypatch.setattr(m, "_detect_coverage_build_system", lambda *_args: "maven")

    def fake_apply(orch, project_dir, build_system=None, *, baseline_metrics=None):
        calls["baseline"] = baseline_metrics
        return True

    monkeypatch.setattr(m, "apply_coverage", fake_apply)

    assert _run_coverage_pass(Orch(), "dbcp", validator=Validator()) is True
    assert calls["baseline"]["module_summary"] == {
        "modules_total": 1,
        "modules_built": 1,
    }
    assert calls["baseline"]["modules"][0]["path"] == "."


def test_run_coverage_pass_best_effort_on_error(monkeypatch):
    import sag.main as m

    monkeypatch.setattr(m, "_detect_coverage_build_system", lambda orch, project_dir: "maven")
    monkeypatch.setattr(
        m, "apply_coverage", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    class Orch:
        def execute_command(self, command, **kwargs):
            return {"success": True, "exit_code": 0, "output": ""}

    # must not raise
    assert _run_coverage_pass(Orch(), "demo") is False


def test_run_coverage_evidence_pass_returns_the_persisted_rollup(monkeypatch):
    import json

    import sag.main as m

    monkeypatch.setattr(m, "_run_coverage_pass", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        m,
        "read_container_text",
        lambda *_args, **_kwargs: json.dumps(
            {
                "module_summary": {
                    "line_rate": 83.4,
                    "coverage_source": "jacoco-injected",
                }
            }
        ),
    )

    assert _run_coverage_evidence_pass(object(), "dbcp") == {
        "status": "collected",
        "line_rate": 83.4,
        "source": "jacoco-injected",
    }


def test_run_coverage_evidence_pass_never_turns_absence_into_zero(monkeypatch):
    import sag.main as m

    monkeypatch.setattr(m, "_run_coverage_pass", lambda *_args, **_kwargs: False)

    assert _run_coverage_evidence_pass(object(), "dbcp") == {
        "status": "unavailable",
        "reason": "coverage pass produced no persisted reports",
    }
