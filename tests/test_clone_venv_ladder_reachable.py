# tests/test_clone_venv_ladder_reachable.py
"""Clone-time python provisioning must fall into the ensure_venv_pip ladder
when plain `python3 -m venv` fails (2026-07-24 TVM run: Debian ensurepip
split; the ladder existed but sat AFTER an early return)."""

from types import SimpleNamespace

import sag.tools.internal.project_setup_tool as pst
from sag.tools.internal.project_setup_tool import ProjectSetupTool

VENV = "/workspace/tvm/.venv"

ENSUREPIP_ERROR = (
    "The virtual environment was not created successfully because ensurepip is not\n"
    "available.  On Debian/Ubuntu systems, you need to install the python3-venv\n"
    "package using the following command.\n\n    apt install python3.12-venv\n"
)


class CloneVenvOrch:
    """TVM shape: plain venv creation fails; apt rung restores pip."""

    def __init__(self, apt_ok=True):
        self.apt_ok = apt_ok
        self.apt_installed = False
        self.commands = []

    def execute_command(self, command, workdir=None, timeout=None, **kwargs):
        self.commands.append(command)
        if "test -x" in command and ".venv/bin/python" in command:
            return {"success": True, "exit_code": 0, "output": "MISSING"}
        if command == f"python3 -m venv {VENV}":
            return {"success": False, "exit_code": 1, "output": ENSUREPIP_ERROR}
        if "-m pip --version" in command:
            if self.apt_installed:
                return {"success": True, "exit_code": 0, "output": "pip 24.0"}
            return {"success": False, "exit_code": 1, "output": "No module named pip"}
        if "-m ensurepip" in command:
            return {"success": False, "exit_code": 1, "output": "No module named ensurepip"}
        if "apt-get install" in command and "python3-venv" in command:
            if self.apt_ok:
                self.apt_installed = True
                return {"success": True, "exit_code": 0, "output": "installed"}
            return {"success": False, "exit_code": 100, "output": "apt failed"}
        if "python3 --version" in command:
            return {"success": True, "exit_code": 0, "output": "Python 3.12.3"}
        if command.startswith("ls -A1"):
            return {"success": True, "exit_code": 0, "output": ""}
        return {"success": True, "exit_code": 0, "output": ""}


def _tool(orch, monkeypatch):
    monkeypatch.setattr(
        pst,
        "PythonPreflight",
        lambda orchestrator: SimpleNamespace(
            run=lambda *a, **k: SimpleNamespace(
                provisioned=False, narration=None, active_version=None
            )
        ),
    )
    monkeypatch.setattr(pst, "read_build_requirements", lambda orchestrator: {})
    tool = ProjectSetupTool.__new__(ProjectSetupTool)
    tool.orchestrator = orch
    return tool


def test_venv_creation_failure_enters_the_repair_ladder(monkeypatch):
    orch = CloneVenvOrch(apt_ok=True)
    result = _tool(orch, monkeypatch)._install_python_dependencies("/workspace/tvm")

    assert result["success"] is True
    apt_commands = [c for c in orch.commands if "python3-venv" in c]
    assert apt_commands, "ladder rung 4 (apt python3-venv) never ran"


def test_exhausted_ladder_still_fails_honestly(monkeypatch):
    orch = CloneVenvOrch(apt_ok=False)
    result = _tool(orch, monkeypatch)._install_python_dependencies("/workspace/tvm")

    assert result["success"] is False
    assert "repair ladder exhausted" in result["error"]
