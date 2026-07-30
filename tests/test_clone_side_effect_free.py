# tests/test_clone_side_effect_free.py
"""`project(action='clone')` is side-effect free (spec §3.4-1).

Clone fetches the repo, initialises submodules, detects the project type and
STOPS. No venv, no pip, no apt/JDK install rides along: provisioning happens
only when the model asks for it by name (`project(action='provision')` for the
toolchain, `build(action='deps')` for dependencies). The old auto-install made
clone a hidden multi-minute mutation whose failures were reported as clone
warnings, which is exactly the opacity the spec removes."""

import re

from sag.tools.internal.project_setup_tool import ProjectSetupTool

MUTATING = re.compile(r"venv|\bpip\b|apt-get|apt install|update-alternatives|sdkman")


class CloneOrchestrator:
    """Clone of a repo whose root marker decides the detected project type."""

    def __init__(self, marker="pyproject.toml", name="proj"):
        self.marker = marker
        self.name = name
        self.commands = []
        self.files = {}

    def execute_command(self, command, workdir=None, timeout=None, **kwargs):
        self.commands.append(command)
        root = f"/workspace/{self.name}"

        if command == "which git":
            return {"success": True, "output": "/usr/bin/git\n", "exit_code": 0}
        if command == f"test -f {root}/.gitmodules":
            return {"success": False, "output": "", "exit_code": 1}
        if command.startswith("git clone "):
            return {"success": True, "output": "Cloning into ...", "exit_code": 0}
        if command == f"ls -la {root}":
            return {"success": True, "output": "total 8", "exit_code": 0}
        if command == f"git -C {root} rev-parse HEAD":
            return {"success": True, "output": "deadbeef\n", "exit_code": 0}
        if command.startswith(f"find {root} "):
            return {"success": True, "output": f"{root}/{self.marker}\n", "exit_code": 0}
        if command.startswith("cat ") and command.endswith("pom.xml"):
            return {
                "success": True,
                "output": (
                    "<project><properties><maven.compiler.release>17"
                    "</maven.compiler.release></properties></project>"
                ),
                "exit_code": 0,
            }
        return {"success": True, "output": "", "exit_code": 0}

    def read_file(self, path):
        if path not in self.files:
            # §3.9 absence protocol: absence is STATED (None), never implied
            # by an ordinary failure — a failed read now raises on the exact
            # path, because "could not look" is not "looked and found nothing".
            return None
        return {"success": True, "content": self.files[path], "exit_code": 0}

    def write_file(self, path, content):
        self.files[path] = content
        return {"success": True, "output": "", "exit_code": 0}


def _clone(orchestrator):
    return ProjectSetupTool(orchestrator).execute(
        action="clone",
        repository_url="https://github.com/example/proj.git",
    )


def test_python_clone_runs_no_venv_pip_or_apt_commands():
    orchestrator = CloneOrchestrator(marker="pyproject.toml")
    result = _clone(orchestrator)

    assert result.succeeded is True
    assert result.metadata["project_type"]["type"] == "python"
    mutations = [c for c in orchestrator.commands if MUTATING.search(c)]
    assert mutations == [], f"clone must not provision anything, ran: {mutations}"


def test_maven_clone_installs_no_jdk():
    orchestrator = CloneOrchestrator(marker="pom.xml")
    result = _clone(orchestrator)

    assert result.succeeded is True
    assert result.metadata["project_type"]["type"] == "maven"
    mutations = [c for c in orchestrator.commands if MUTATING.search(c)]
    assert mutations == [], f"clone must not provision anything, ran: {mutations}"


def test_clone_output_names_the_explicit_provisioning_actions():
    orchestrator = CloneOrchestrator(marker="pyproject.toml")
    output = _clone(orchestrator).output

    assert "Installing dependencies automatically" not in output
    assert "build(action='deps')" in output
    assert "project(action='provision'" in output


def test_maven_clone_output_names_provision_for_the_jdk():
    orchestrator = CloneOrchestrator(marker="pom.xml")
    result = _clone(orchestrator)

    # Java version detection is READ-ONLY and stays: it tells the model which
    # JDK to ask for, it does not install one.
    assert result.metadata["java_version_required"] == "17"
    assert "project(action='provision', java_version='17')" in result.output
    assert "Installing dependencies automatically" not in result.output


def test_clone_reports_no_dependency_metadata():
    orchestrator = CloneOrchestrator(marker="pyproject.toml")
    metadata = _clone(orchestrator).metadata

    assert "dependencies_installed" not in metadata
    assert "dependencies_error" not in metadata
