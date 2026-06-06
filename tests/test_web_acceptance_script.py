import importlib.util
import subprocess
import sys
from pathlib import Path


def load_acceptance_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "accept_web_ui.py"
    spec = importlib.util.spec_from_file_location("accept_web_ui", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_acceptance_script_skeleton_phase_passes_after_task_zero():
    result = subprocess.run(
        [sys.executable, "scripts/accept_web_ui.py", "--phase", "skeleton", "--skip-commands"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "phase=skeleton" in result.stdout


def test_acceptance_script_rejects_unknown_phase():
    result = subprocess.run(
        [sys.executable, "scripts/accept_web_ui.py", "--phase", "unknown", "--skip-commands"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "invalid choice" in result.stderr


def test_workspace_session_phase_checks_task_fifteen_files_without_terminal(tmp_path, monkeypatch):
    module = load_acceptance_module()
    monkeypatch.setattr(module, "ROOT", tmp_path)

    for path in module.BACKEND_FILES + module.FRONTEND_SESSION_FILES:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")

    failures: list[str] = []

    module.check_phase_files("workspace-session", failures)

    assert failures == []


def test_workspace_session_phase_reports_missing_session_components(tmp_path, monkeypatch):
    module = load_acceptance_module()
    monkeypatch.setattr(module, "ROOT", tmp_path)

    for path in module.BACKEND_FILES:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")

    failures: list[str] = []

    module.check_phase_files("workspace-session", failures)

    assert "missing required file: webui/src/pages/Workspace.tsx" in failures
    assert "missing required file: webui/src/pages/SessionDetail.tsx" in failures
    assert not any("TerminalPanel.tsx" in failure for failure in failures)


def test_workspace_session_phase_checks_task_fifteen_patterns(tmp_path, monkeypatch):
    module = load_acceptance_module()
    monkeypatch.setattr(module, "ROOT", tmp_path)

    for path in module.FRONTEND_SESSION_FILES:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")

    for _label, (path, pattern) in module.WORKSPACE_SESSION_PATTERNS.items():
        target = tmp_path / path
        target.write_text(target.read_text(encoding="utf-8") + f"\n{pattern}\n", encoding="utf-8")

    failures: list[str] = []

    module.check_phase_patterns("workspace-session", failures)

    assert failures == []


def test_workspace_session_phase_reports_missing_task_fifteen_patterns(tmp_path, monkeypatch):
    module = load_acceptance_module()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    target = tmp_path / "webui/src/App.tsx"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("fetchSession\n", encoding="utf-8")

    failures: list[str] = []

    module.check_phase_patterns("workspace-session", failures)

    assert "missing product-boundary pattern app submits workspace tasks: submitTask" in failures


def test_backend_web_tests_are_expanded_to_concrete_paths(tmp_path, monkeypatch):
    module = load_acceptance_module()
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    later = tests_dir / "test_web_zeta.py"
    earlier = tests_dir / "test_web_alpha.py"
    later.write_text("def test_zeta():\n    pass\n", encoding="utf-8")
    earlier.write_text("def test_alpha():\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", tmp_path)

    failures: list[str] = []

    assert module.backend_web_test_paths(failures) == [
        "tests/test_web_alpha.py",
        "tests/test_web_zeta.py",
    ]
    assert failures == []


def test_backend_web_tests_report_clear_failure_when_missing(tmp_path, monkeypatch):
    module = load_acceptance_module()
    (tmp_path / "tests").mkdir()
    monkeypatch.setattr(module, "ROOT", tmp_path)

    failures: list[str] = []

    assert module.backend_web_test_paths(failures) == []
    assert failures == ["no backend web tests found"]


def test_backend_web_tests_exclude_acceptance_script(tmp_path, monkeypatch):
    module = load_acceptance_module()
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    acceptance_script = tests_dir / "test_web_acceptance_script.py"
    acceptance_script.write_text("def test_acceptance():\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", tmp_path)

    failures: list[str] = []

    assert module.backend_web_test_paths(failures) == []
    assert failures == ["no backend web tests found"]
