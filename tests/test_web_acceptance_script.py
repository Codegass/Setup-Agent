import subprocess
import sys


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
