import json

from sag.runtime.env_overlay import DEFAULT_OVERLAY_JSON
from sag.tools.project_setup_tool import ProjectSetupTool

JAVA_HOME = "/usr/lib/jvm/java-17-openjdk-amd64"
JAVA_BIN = f"{JAVA_HOME}/bin/java"
JAVAC_BIN = f"{JAVA_HOME}/bin/javac"
MVN_BIN = "/usr/bin/mvn"


class FakeProjectSetupOrchestrator:
    def __init__(
        self,
        *,
        install_success=True,
        java_setup_verification_success=True,
        maven_path=MVN_BIN,
    ):
        self.install_success = install_success
        self.java_setup_verification_success = java_setup_verification_success
        self.maven_path = maven_path
        self.commands = []
        self.files = {}

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append((command, workdir, timeout))

        if command == "apt-get update":
            return {"success": True, "output": "", "exit_code": 0}

        if command.startswith("DEBIAN_FRONTEND=noninteractive apt-get install"):
            if self.install_success:
                return {"success": True, "output": "installed", "exit_code": 0}
            return {"success": False, "output": "install failed", "exit_code": 100}

        if command.startswith("ls -d /usr/lib/jvm/java-17-openjdk-"):
            return {"success": True, "output": JAVA_HOME, "exit_code": 0}

        if command == f"test -f {JAVA_BIN} && test -f {JAVAC_BIN} && echo 'verified'":
            return {"success": True, "output": "verified", "exit_code": 0}

        if command.startswith("echo 'export JAVA_HOME="):
            return {"success": True, "output": "", "exit_code": 0}

        if command.startswith("echo 'export PATH=$JAVA_HOME/bin:$PATH'"):
            return {"success": True, "output": "", "exit_code": 0}

        if command.startswith("update-alternatives"):
            return {"success": True, "output": "", "exit_code": 0}

        if command == "java -version 2>&1 && echo '---' && javac -version 2>&1":
            if not self.java_setup_verification_success:
                return {"success": False, "output": "verification failed", "exit_code": 1}
            return {
                "success": True,
                "output": 'openjdk version "17.0.10"\n---\njavac 17.0.10',
                "exit_code": 0,
            }

        if command == "command -v mvn":
            if self.maven_path:
                return {"success": True, "output": f"{self.maven_path}\n", "exit_code": 0}
            return {"success": False, "output": "", "exit_code": 1}

        if command == "mkdir -p /workspace/.setup_agent":
            return {"success": True, "output": "", "exit_code": 0}

        return {"success": True, "output": "", "exit_code": 0}

    def read_file(self, path):
        if path not in self.files:
            return {"success": False, "content": "", "exit_code": 1}
        return {"success": True, "content": self.files[path], "exit_code": 0}

    def write_file(self, path, content):
        self.files[path] = content
        return {"success": True, "output": "", "exit_code": 0}


class FakeCloneOrchestrator:
    def __init__(self, *, checkout_success=True):
        self.checkout_success = checkout_success
        self.commands = []

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append((command, workdir, timeout))

        if command == "which git":
            return {"success": True, "output": "/usr/bin/git\n", "exit_code": 0}

        if command == "git clone https://github.com/apache/commons-cli.git commons-cli":
            return {"success": True, "output": "Cloning into 'commons-cli'...", "exit_code": 0}

        if command == "ls -la /workspace/commons-cli":
            return {"success": True, "output": "total 8", "exit_code": 0}

        if command == "git -C /workspace/commons-cli fetch --tags --force":
            return {"success": True, "output": "", "exit_code": 0}

        if command.startswith("git -C /workspace/commons-cli checkout --detach "):
            if self.checkout_success:
                return {
                    "success": True,
                    "output": "HEAD is now at ae44dcd release",
                    "exit_code": 0,
                }
            return {
                "success": False,
                "output": "error: pathspec 'missing-ref' did not match any file(s) known to git",
                "exit_code": 1,
            }

        if command == "git -C /workspace/commons-cli rev-parse HEAD":
            return {
                "success": True,
                "output": "ae44dcdffd28d6a1a32dc4e0801b715adcef162e\n",
                "exit_code": 0,
            }

        if command.startswith("find /workspace/commons-cli "):
            return {
                "success": True,
                "output": "/workspace/commons-cli/pom.xml\n",
                "exit_code": 0,
            }

        if command == "cat /workspace/commons-cli/pom.xml":
            return {
                "success": True,
                "output": (
                    "<project><properties><maven.compiler.release>8</maven.compiler.release>"
                    "</properties></project>"
                ),
                "exit_code": 0,
            }

        return {"success": True, "output": "", "exit_code": 0}


def test_project_setup_clone_checks_out_ref_and_records_resolved_commit():
    orchestrator = FakeCloneOrchestrator()
    tool = ProjectSetupTool(orchestrator)

    result = tool.execute(
        action="clone",
        repository_url="https://github.com/apache/commons-cli.git",
        ref="rel/commons-cli-1.11.0",
        auto_install_deps=False,
    )

    assert result.success is True
    assert (
        "git -C /workspace/commons-cli fetch --tags --force",
        "/workspace",
        None,
    ) in orchestrator.commands
    assert (
        "git -C /workspace/commons-cli checkout --detach rel/commons-cli-1.11.0",
        "/workspace",
        None,
    ) in orchestrator.commands
    assert result.metadata["ref"] == "rel/commons-cli-1.11.0"
    assert result.metadata["resolved_commit"] == "ae44dcdffd28d6a1a32dc4e0801b715adcef162e"
    assert "🔖 Ref: rel/commons-cli-1.11.0" in result.output
    assert "🧾 Commit: ae44dcdffd28d6a1a32dc4e0801b715adcef162e" in result.output


def test_project_setup_clone_bad_ref_fails_without_project_detection():
    orchestrator = FakeCloneOrchestrator(checkout_success=False)
    tool = ProjectSetupTool(orchestrator)

    result = tool.execute(
        action="clone",
        repository_url="https://github.com/apache/commons-cli.git",
        ref="missing-ref",
        auto_install_deps=False,
    )

    assert result.success is False
    assert result.error_code == "REF_CHECKOUT_FAILED"
    assert result.metadata["ref"] == "missing-ref"
    assert not any(
        command.startswith("find /workspace/commons-cli ")
        for command, _, _ in orchestrator.commands
    )


def test_project_setup_legacy_branch_maps_to_ref_when_ref_absent():
    orchestrator = FakeCloneOrchestrator()
    tool = ProjectSetupTool(orchestrator)

    result = tool.execute(
        action="clone",
        repository_url="https://github.com/apache/commons-cli.git",
        branch="rel/commons-cli-1.11.0",
        auto_install_deps=False,
    )

    assert result.success is True
    assert (
        "git -C /workspace/commons-cli checkout --detach rel/commons-cli-1.11.0",
        "/workspace",
        None,
    ) in orchestrator.commands
    assert not any("git clone -b" in command for command, _, _ in orchestrator.commands)
    assert result.metadata["ref"] == "rel/commons-cli-1.11.0"
    assert result.metadata["branch"] == "rel/commons-cli-1.11.0"


def test_maven_dependency_install_registers_java_and_maven_overlay():
    orchestrator = FakeProjectSetupOrchestrator()
    tool = ProjectSetupTool(orchestrator)

    result = tool._install_dependencies_for_project_type(
        {"type": "maven"},
        "/workspace/project",
        "17",
    )

    assert result["success"] is True
    overlay = json.loads(orchestrator.files[DEFAULT_OVERLAY_JSON])

    java_entry = overlay["tools"]["java"]
    assert java_entry["active"] == JAVA_BIN
    assert java_entry["candidates"][JAVA_BIN]["version"] == "17"
    assert java_entry["candidates"][JAVA_BIN]["env"] == {"JAVA_HOME": JAVA_HOME}
    assert java_entry["candidates"][JAVA_BIN]["path_prepend"] == [f"{JAVA_HOME}/bin"]

    maven_entry = overlay["tools"]["maven"]
    assert maven_entry["active"] == MVN_BIN
    assert maven_entry["candidates"][MVN_BIN]["path_prepend"] == ["/usr/bin"]


def test_failed_maven_dependency_install_does_not_activate_overlay_runtime():
    orchestrator = FakeProjectSetupOrchestrator(install_success=False)
    tool = ProjectSetupTool(orchestrator)

    result = tool._install_dependencies_for_project_type(
        {"type": "maven"},
        "/workspace/project",
        "17",
    )

    assert result["success"] is False
    assert DEFAULT_OVERLAY_JSON not in orchestrator.files


def test_failed_project_java_verification_does_not_activate_java_overlay_runtime():
    orchestrator = FakeProjectSetupOrchestrator(java_setup_verification_success=False)
    tool = ProjectSetupTool(orchestrator)

    result = tool._install_dependencies_for_project_type(
        {"type": "maven"},
        "/workspace/project",
        "17",
    )

    assert result["success"] is True
    overlay = json.loads(orchestrator.files[DEFAULT_OVERLAY_JSON])
    assert "java" not in overlay["tools"]
    assert overlay["tools"]["maven"]["active"] == MVN_BIN
