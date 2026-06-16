from sag.main import _run_coverage_pass


def test_run_coverage_pass_invokes_apply(monkeypatch):
    calls = {}

    class Orch:
        def execute_command(self, command, **kwargs):
            if "project_meta.json" in command:
                return {"success": True, "exit_code": 0,
                        "output": '{"project_name": "caffeine"}'}
            return {"success": True, "exit_code": 0, "output": ""}

    import sag.main as m

    monkeypatch.setattr(m, "_detect_coverage_build_system",
                        lambda orch, project_dir: "gradle")

    def fake_apply(orch, project_dir, build_system=None):
        calls["project_dir"] = project_dir
        calls["build_system"] = build_system
        return True

    monkeypatch.setattr(m, "apply_coverage", fake_apply)
    ok = _run_coverage_pass(Orch(), "caffeine")
    assert ok is True
    assert calls["project_dir"] == "/workspace/caffeine"
    assert calls["build_system"] == "gradle"


def test_run_coverage_pass_best_effort_on_error(monkeypatch):
    import sag.main as m
    monkeypatch.setattr(m, "_detect_coverage_build_system",
                        lambda orch, project_dir: "maven")
    monkeypatch.setattr(m, "apply_coverage",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    class Orch:
        def execute_command(self, command, **kwargs):
            return {"success": True, "exit_code": 0, "output": ""}

    # must not raise
    assert _run_coverage_pass(Orch(), "demo") is False
