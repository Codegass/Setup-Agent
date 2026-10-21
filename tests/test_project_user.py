"""The project's own commands run as an unprivileged user when one is configured.

In the 52-project run commons-io, commons-vfs and xmlgraphics-batik failed only
because their tests rely on ``File.setReadOnly()``/``chmod`` while the container
ran everything as root, where file permissions are not enforced. Official CI
runs as an ordinary user. With ``project_user`` set, the orchestrator drops to
that user for Maven, Gradle, pip, pytest and the bash tool, and keeps root for
its own control work (apt, alternatives, receipts, probes). Unset, every
command is byte-for-byte what it was before; the pins below hold that.
"""

import base64
import hashlib
import json
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_bash_tool_timeout import FakeBashOrchestrator
from test_dispatch_and_poll import build_orchestrator
from test_dispatch_stall_window import _DiagnosticHarness, _stall_job
from test_maven_gradle_tool_contracts import FakeBuildToolOrchestrator
from test_venv_repair_ladder import LadderOrch

import sag.agent.stall_diagnostics as stall_diagnostics
from sag.agent.worktree_evidence import WorktreeEvidenceRecorder
from sag.benchmark import compilation_evidence
from sag.benchmark.sag_observer import _PROGRAM, SAGRequirementObserver
from sag.config.settings import Config
from sag.coverage import runner as coverage_runner
from sag.docker_orch import orch as orch_module
from sag.docker_orch.orch import DockerOrchestrator
from sag.tools.bash import BashTool
from sag.tools.base import ToolError
from sag.tools.build.backends import NATIVE_FEATURE_RESOLVER
from sag.tools.internal.build_preflight import PythonPreflight
from sag.tools.internal.gradle_tool import GradleTool
from sag.tools.internal.maven_tool import MavenTool
from sag.tools.internal.project_setup_tool import ProjectSetupTool
from sag.tools.internal.python_env import ensure_venv_pip
from sag.tools.internal.python_tool import PYTEST_REPORT_DIR, PythonTool
from sag.tools.internal.system_tool import SystemTool

SETPRIV = ["/usr/bin/setpriv", "--reuid=sag", "--regid=sag", "--init-groups"]
IDENTITY = ["HOME=/home/sag", "USER=sag", "LOGNAME=sag"]
PROFILE = "export LANG=${LANG:-C.UTF-8}; export LC_ALL=${LC_ALL:-C.UTF-8}"
SUDO_HINT = (
    "This command ran as the unprivileged project user. Installing system "
    "packages or changing files outside the project needs sudo, for example: "
    "sudo apt-get install -y <package>"
)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _user(name="sag"):
    return SimpleNamespace(project_user=name)


# --------------------------------------------------------------------------
# 1. Configuration
# --------------------------------------------------------------------------


def test_project_user_is_unset_by_default():
    assert Config().project_user is None


def test_project_user_reads_its_environment_variable(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SAG_PROJECT_USER", "sag")
    assert Config.from_env().project_user == "sag"


@pytest.mark.parametrize("value", [None, ""])
def test_an_absent_or_empty_project_user_variable_keeps_root(monkeypatch, tmp_path, value):
    monkeypatch.chdir(tmp_path)
    if value is None:
        monkeypatch.delenv("SAG_PROJECT_USER", raising=False)
    else:
        monkeypatch.setenv("SAG_PROJECT_USER", value)
    assert Config.from_env().project_user is None


@pytest.mark.parametrize("name", ["sag; id", "Sag", "-sag", "a b", "", "x" * 40])
def test_a_project_user_must_be_a_plain_account_name(name):
    with pytest.raises(ValidationError):
        Config(project_user=name)


# --------------------------------------------------------------------------
# 2. Orchestrator: argv, exec environment, detached supervision
# --------------------------------------------------------------------------


def test_isolated_argv_without_a_user_is_todays_argv():
    argv = DockerOrchestrator._isolated_exec_argv(
        "echo hi", runtime_environment={"A": "1"}, timeout_seconds=30
    )
    assert argv == [
        "/usr/bin/timeout",
        "--preserve-status",
        "30",
        "/usr/bin/env",
        "A=1",
        "/bin/bash",
        "-c",
        "echo hi",
    ]


def test_isolated_argv_drops_to_the_user_between_timeout_and_env():
    argv = DockerOrchestrator._isolated_exec_argv(
        "echo hi", runtime_environment={"A": "1"}, timeout_seconds=30, project_user="sag"
    )
    assert argv == [
        "/usr/bin/timeout",
        "--preserve-status",
        "30",
        *SETPRIV,
        "/usr/bin/env",
        "A=1",
        "/bin/bash",
        "-c",
        "echo hi",
    ]
    assert "--reset-env" not in argv


def test_isolated_argv_without_a_timeout_starts_with_setpriv():
    argv = DockerOrchestrator._isolated_exec_argv(
        "echo hi", runtime_environment={"A": "1"}, project_user="sag"
    )
    assert argv == [*SETPRIV, "/usr/bin/env", "A=1", "/bin/bash", "-c", "echo hi"]


def test_the_control_path_never_drops_privileges():
    argv = DockerOrchestrator._isolated_exec_argv(
        "id", runtime_environment=None, timeout_seconds=5, project_user="sag"
    )
    assert argv == ["/usr/bin/timeout", "--preserve-status", "5", "/bin/bash", "-c", "id"]


class _ExecContainer:
    id = "c" * 64

    def __init__(self):
        self.calls = []

    def exec_run(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, **kwargs})
        return SimpleNamespace(exit_code=0, output=(b"ok", b""))


class _ExecAPI:
    def __init__(self):
        self.created = []

    def exec_create(self, container, cmd, **kwargs):
        self.created.append({"cmd": cmd, **kwargs})
        return {"Id": "e" * 64}

    def exec_start(self, exec_id, **kwargs):
        return iter(())


def _exec_orchestrator(project_user=None):
    orchestrator = DockerOrchestrator.__new__(DockerOrchestrator)
    orchestrator.container_name = "sag-demo"
    orchestrator.config = _user(project_user)
    container = _ExecContainer()
    api = _ExecAPI()
    orchestrator.client = SimpleNamespace(
        api=api, containers=SimpleNamespace(get=lambda _name: container)
    )
    orchestrator.is_container_running = lambda: True
    orchestrator._default_exec_environment = lambda environment=None: {
        "PATH": "/opt/jdk/bin:/usr/bin",
        **(environment or {}),
    }
    orchestrator._monitor_execution_with_timeouts = lambda *args, **kwargs: {
        "success": True,
        "exit_code": 0,
        "output": "",
    }
    return orchestrator, container, api


EXECUTE_ARGV_TODAY = [
    "/usr/bin/timeout",
    "--preserve-status",
    "30",
    "/usr/bin/env",
    "PATH=/opt/jdk/bin:/usr/bin",
    "/bin/bash",
    "-c",
    f"{PROFILE}; cd /workspace/p && mvn -v",
]
MONITORED_ARGV_TODAY = [
    "/usr/bin/timeout",
    "--preserve-status",
    "600",
    "/usr/bin/env",
    "PATH=/opt/jdk/bin:/usr/bin",
    "/bin/bash",
    "-c",
    f"{PROFILE}; cd /workspace/p && mvn -B test",
]


@pytest.mark.parametrize("project_user, flag", [(None, {}), (None, {"as_project_user": True}), ("sag", {})])
def test_execute_command_default_path_is_todays_argv(project_user, flag):
    orchestrator, container, _api = _exec_orchestrator(project_user)
    orchestrator.execute_command("mvn -v", workdir="/workspace/p", timeout=30, **flag)
    assert container.calls[0]["cmd"] == EXECUTE_ARGV_TODAY
    assert container.calls[0]["environment"] == orchestrator._control_exec_environment()


def test_a_project_command_runs_as_the_user_with_its_login_identity():
    orchestrator, container, _api = _exec_orchestrator("sag")
    orchestrator.execute_command("mvn -v", workdir="/workspace/p", timeout=30, as_project_user=True)
    assert container.calls[0]["cmd"] == [
        "/usr/bin/timeout",
        "--preserve-status",
        "30",
        *SETPRIV,
        "/usr/bin/env",
        "PATH=/opt/jdk/bin:/usr/bin",
        *IDENTITY,
        "/bin/bash",
        "-c",
        f"{PROFILE}; cd /workspace/p && mvn -v",
    ]
    # Docker itself still starts the exec as root under the control environment.
    assert container.calls[0]["environment"] == orchestrator._control_exec_environment()
    assert "user" not in container.calls[0]


def test_control_commands_stay_root_even_with_a_project_user():
    orchestrator, container, _api = _exec_orchestrator("sag")
    orchestrator.execute_control_command("chown -R sag:sag /workspace/p", timeout=30)
    assert container.calls[0]["cmd"] == [
        "/usr/bin/timeout",
        "--preserve-status",
        "30",
        "/bin/bash",
        "-c",
        f"{PROFILE}; chown -R sag:sag /workspace/p",
    ]


@pytest.mark.parametrize("project_user, flag", [(None, {}), (None, {"as_project_user": True}), ("sag", {})])
def test_monitored_default_path_is_todays_argv(project_user, flag):
    orchestrator, _container, api = _exec_orchestrator(project_user)
    orchestrator.execute_command_with_monitoring(
        "mvn -B test",
        workdir="/workspace/p",
        absolute_timeout=600,
        enable_cpu_monitoring=False,
        optimize_for_maven=False,
        **flag,
    )
    assert api.created[0]["cmd"] == MONITORED_ARGV_TODAY


def test_monitored_project_command_runs_as_the_user():
    orchestrator, _container, api = _exec_orchestrator("sag")
    orchestrator.execute_command_with_monitoring(
        "mvn -B test",
        workdir="/workspace/p",
        absolute_timeout=600,
        enable_cpu_monitoring=False,
        optimize_for_maven=False,
        as_project_user=True,
    )
    assert api.created[0]["cmd"] == [
        "/usr/bin/timeout",
        "--preserve-status",
        "600",
        *SETPRIV,
        "/usr/bin/env",
        "PATH=/opt/jdk/bin:/usr/bin",
        *IDENTITY,
        "/bin/bash",
        "-c",
        f"{PROFILE}; cd /workspace/p && mvn -B test",
    ]


class _FixedUUID:
    hex = "0123456789abcdef0123456789abcdef"


# sha256 of the supervisor script the unchanged orchestrator produced for
# execute_command_detached("mvn -B test", workdir="/workspace/demo",
# environment={"MAVEN_OPTS": "-Xmx1g"}) under the fixed job id below.
DETACHED_LAUNCHER_TODAY_SHA256 = "1e6b5e88a9983f3de119d4fde25bfeb6d347ff759804dd93f3a8b51a9ade1622"


def _detached_orchestrator(monkeypatch, project_user=None):
    monkeypatch.setattr(orch_module.uuid, "uuid4", lambda: _FixedUUID())
    orchestrator = build_orchestrator()
    if project_user is not None:
        orchestrator.config = _user(project_user)

    def fake_execute(command, **kwargs):
        return {"exit_code": 0, "output": "PID:4242\nPGID:4242\nIDENTITY:" + "a" * 64}

    orchestrator.execute_command = fake_execute
    orchestrator.execute_control_command = fake_execute
    orchestrator._default_exec_environment = lambda environment=None: {
        "PATH": "/opt/jdk/bin:/usr/bin",
        "JAVA_HOME": "/opt/jdk",
        **(environment or {}),
    }
    return orchestrator


def _job_script(supervisor):
    tokens = shlex.split(supervisor)
    at = tokens.index("/usr/bin/setsid")
    assert tokens[at : at + 5] == ["/usr/bin/setsid", "--fork", "--wait", "/bin/bash", "-c"]
    return tokens[at + 5]


@pytest.mark.parametrize("project_user, flag", [(None, {}), (None, {"as_project_user": True}), ("sag", {})])
def test_detached_default_path_is_byte_identical_to_today(monkeypatch, project_user, flag):
    orchestrator = _detached_orchestrator(monkeypatch, project_user)
    handle = orchestrator.execute_command_detached(
        "mvn -B test", workdir="/workspace/demo", environment={"MAVEN_OPTS": "-Xmx1g"}, **flag
    )
    assert handle["started"] is True
    cmd = orchestrator.detached_api.create_calls[0]["cmd"]
    assert cmd[:2] == ["/bin/bash", "-c"]
    assert hashlib.sha256(cmd[-1].encode()).hexdigest() == DETACHED_LAUNCHER_TODAY_SHA256


def test_detached_job_keeps_root_supervision_and_drops_privileges_at_the_exec(monkeypatch):
    orchestrator = _detached_orchestrator(monkeypatch, "sag")
    handle = orchestrator.execute_command_detached(
        "mvn -B test", workdir="/workspace/demo", as_project_user=True
    )
    assert handle["started"] is True
    created = orchestrator.detached_api.create_calls[0]
    # The Docker exec and its supervisor start as root: they open the log and
    # run setsid before anything changes identity.
    assert created["cmd"][:2] == ["/bin/bash", "-c"]
    assert "user" not in created["kwargs"]
    supervisor = created["cmd"][-1]
    assert supervisor.startswith("set +e; umask 077; mkdir -p /tmp/sag_jobs")
    job_script = _job_script(supervisor)
    drop = "exec /usr/bin/setpriv --reuid=sag --regid=sag --init-groups /usr/bin/env "
    exec_at = job_script.index(drop)
    assert job_script.count("/usr/bin/setpriv") == 1
    # pid = pgid = sid is checked, and the identity files written, as root and
    # before the exec; the exec replaces the process in place, so the recorded
    # pid stays the job's pid.
    for marker in (
        '[ "$job_pid" = "$job_pgid" ] && [ "$job_pid" = "$job_sid" ] || exit 125',
        "mv /tmp/sag_jobs/0123456789ab.pid.tmp /tmp/sag_jobs/0123456789ab.pid",
        "mv /tmp/sag_jobs/0123456789ab.pgid.tmp /tmp/sag_jobs/0123456789ab.pgid",
        "mv /tmp/sag_jobs/0123456789ab.identity.tmp /tmp/sag_jobs/0123456789ab.identity",
    ):
        assert job_script.index(marker) < exec_at
    runtime = shlex.split(job_script[exec_at + len("exec ") :])
    assert runtime[:5] == [*SETPRIV, "/usr/bin/env"]
    assert runtime[5:10] == ["PATH=/opt/jdk/bin:/usr/bin", "JAVA_HOME=/opt/jdk", *IDENTITY]
    assert runtime[10:12] == ["/bin/bash", "-c"]
    assert runtime[12] == f"{PROFILE}; cd /workspace/demo && mvn -B test"
    assert "--reset-env" not in job_script
    syntax = subprocess.run(["bash", "-n", "-c", supervisor], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr


def test_soft_timeout_forwards_the_user_flag_only_when_asked():
    orchestrator = build_orchestrator()
    seen = []

    def detached(command, **kwargs):
        seen.append(kwargs)
        return {"started": False, "runner_dispatched": False, "launch_output": "refused"}

    orchestrator.execute_command_detached = detached
    orchestrator.execute_command_with_soft_timeout("mvn test", workdir="/w", soft_timeout=1)
    orchestrator.execute_command_with_soft_timeout(
        "mvn test", workdir="/w", soft_timeout=1, as_project_user=True
    )
    assert seen == [
        {"workdir": "/w", "environment": None},
        {"workdir": "/w", "environment": None, "as_project_user": True},
    ]


@pytest.mark.parametrize("project_user, chowns_root", [(None, True), ("sag", False)])
def test_workspace_setup_drops_the_root_chown_only_with_a_project_user(project_user, chowns_root):
    orchestrator = DockerOrchestrator.__new__(DockerOrchestrator)
    orchestrator.config = SimpleNamespace(project_user=project_user, workspace_path="/workspace")
    commands = []

    def control(command, **kwargs):
        commands.append(command)
        return {"success": True, "exit_code": 0, "output": "ok"}

    orchestrator.execute_control_command = control
    assert orchestrator._setup_container_environment() is True
    assert ("chown -R root:root /workspace" in commands) is chowns_root
    assert "mkdir -p /workspace" in commands


# --------------------------------------------------------------------------
# 3. Dispatch sites pass the flag; root-only harness work does not
# --------------------------------------------------------------------------


class _UserBuildOrchestrator(FakeBuildToolOrchestrator):
    config = _user()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.flags = []

    def execute_command(self, command, workdir=None, timeout=None, as_project_user=False):
        self.flags.append((command, as_project_user))
        return super().execute_command(command, workdir=workdir, timeout=timeout)

    def execute_command_with_monitoring(self, command, as_project_user=False, **kwargs):
        self.flags.append((command, as_project_user))
        return super().execute_command_with_monitoring(command, **kwargs)


def _flagged(orchestrator):
    return [command for command, flag in orchestrator.flags if flag]


@pytest.mark.usefixtures("facade_contract_authority", "exact_internal_runner_authority")
def test_maven_build_and_its_runner_probe_run_as_the_user():
    orchestrator = _UserBuildOrchestrator()
    tool = MavenTool(orchestrator)
    tool._record_test_summary = lambda *args, **kwargs: None

    tool.execute(command="test", working_directory="/workspace/project")

    build = orchestrator.monitored_commands[0][0]
    flagged = _flagged(orchestrator)
    assert build in flagged
    assert any("--version" in command and "command -v" in command for command in flagged)
    # Everything else the tool did (report snapshots, receipts, contracts,
    # file probes) stayed on root.
    assert len(flagged) == 2


@pytest.mark.usefixtures("facade_contract_authority", "exact_internal_runner_authority")
def test_gradle_build_and_its_runner_probe_run_as_the_user():
    orchestrator = _UserBuildOrchestrator({"output": "BUILD SUCCESSFUL", "exit_code": 0})
    tool = GradleTool(orchestrator)

    tool.execute(tasks="test", working_directory="/workspace/project", use_wrapper=False)

    build = orchestrator.monitored_commands[0][0]
    flagged = _flagged(orchestrator)
    assert build in flagged
    assert any("--version" in command and "command -v" in command for command in flagged)
    assert len(flagged) == 2


def test_maven_and_gradle_installs_stay_root():
    orchestrator = _UserPythonOrchestrator()
    MavenTool(orchestrator)._install_maven()
    GradleTool(orchestrator)._install_gradle("/workspace/project")
    assert len([c for c, _ in orchestrator.flags if "apt-get" in c]) == 3
    assert _flagged(orchestrator) == []


class _UserPythonOrchestrator:
    config = _user()

    def __init__(self, outputs=None):
        self.flags = []
        self.outputs = outputs or {}

    def _answer(self, command):
        for needle, result in self.outputs.items():
            if needle in command:
                return dict(result)
        return {"success": True, "exit_code": 0, "output": "ok"}

    def execute_command(self, command, workdir=None, as_project_user=False, **kwargs):
        self.flags.append((command, as_project_user))
        return self._answer(command)

    def execute_command_with_monitoring(self, command, as_project_user=False, **kwargs):
        self.flags.append((command, as_project_user))
        return self._answer(command)


def test_python_project_commands_run_as_the_user():
    orchestrator = _UserPythonOrchestrator()
    tool = PythonTool(orchestrator)
    tool._run("python3 -m venv /workspace/p/.venv", "/workspace/p", 60)
    tool._run("/workspace/p/.venv/bin/python -m pytest -q", "/workspace/p", 60)
    assert orchestrator.flags == [
        ("python3 -m venv /workspace/p/.venv", True),
        ("/workspace/p/.venv/bin/python -m pytest -q", True),
    ]


class _Stop(Exception):
    pass


def test_python_native_packages_install_as_root_and_probe_as_the_user():
    orchestrator = _UserPythonOrchestrator()
    tool = PythonTool(orchestrator)
    feature = next(iter(NATIVE_FEATURE_RESOLVER))
    tool._validated_native = lambda bundle: ([feature], {}, None)

    def stop(*args, **kwargs):
        raise _Stop()

    tool._setup_env = stop
    with pytest.raises(_Stop):
        tool._native("/workspace/p", 60, {}, "/workspace/p/.venv", {"features": [feature]})
    apt = [(c, flag) for c, flag in orchestrator.flags if "apt-get" in c]
    probes = [(c, flag) for c, flag in orchestrator.flags if "apt-get" not in c]
    assert len(apt) == 2 and all(flag is False for _, flag in apt)
    assert probes and all(flag is True for _, flag in probes)


@pytest.mark.parametrize(
    "project_user, expected",
    [
        (None, f"mkdir -p {PYTEST_REPORT_DIR}"),
        ("sag", f"install -d -o sag {PYTEST_REPORT_DIR}"),
    ],
)
def test_pytest_report_directory_belongs_to_the_project_user(project_user, expected):
    orchestrator = _UserPythonOrchestrator()
    orchestrator.config = _user(project_user)
    PythonTool(orchestrator)._ensure_pytest_report_dir()
    # Created by root (its parent is the harness's), owned by the user who
    # writes the JUnit report into it.
    assert orchestrator.flags == [(expected, False)]


def test_venv_repair_runs_as_the_user_and_apt_stays_root():
    class UserLadder(LadderOrch):
        config = _user()

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.flags = []

        def execute_command(self, cmd, workdir=None, as_project_user=False):
            self.flags.append((cmd, as_project_user))
            return super().execute_command(cmd, workdir=workdir)

    orchestrator = UserLadder(apt_ok=True, uv_install_ok=True, uv_venv_restores_pip=True)
    result = ensure_venv_pip(orchestrator, "/workspace/p/.venv")
    assert result["action"] == "uv"
    for command, flag in orchestrator.flags:
        root_only = "apt-get" in command or command == "python3 --version 2>&1"
        assert flag is (not root_only), command


def test_python_preflight_provisions_the_venv_as_the_user():
    orchestrator = _UserPythonOrchestrator(
        {"command -v uv": {"success": False, "exit_code": 1, "output": ""}}
    )
    preflight = PythonPreflight(orchestrator)
    preflight._uv_available()
    preflight._uv_provision("3.12", "/workspace/p/.venv")
    preflight._apt_provision("3.12", "/workspace/p/.venv")
    for command, flag in orchestrator.flags:
        assert flag is ("apt-get" not in command), command


def _flagged_system_orchestrator():
    orchestrator = _UserPythonOrchestrator()
    return orchestrator


def test_system_package_installs_stay_root():
    orchestrator = _flagged_system_orchestrator()
    SystemTool(orchestrator)._install_packages(["libssl-dev"])
    assert orchestrator.flags and all(flag is False for _, flag in orchestrator.flags)


def test_graalvm_stage_directory_is_readable_by_the_project_user():
    for project_user, readable in ((None, False), ("sag", True)):
        orchestrator = _UserPythonOrchestrator({"uname": {"success": True, "exit_code": 0, "output": "Linux x86_64"}})
        orchestrator.config = _user(project_user)
        SystemTool(orchestrator)._install_graalvm("21")
        script = next(c for c, _ in orchestrator.flags if "mktemp -d /opt/.sag-graalvm" in c)
        assert ('chmod 755 "$sag_graal_stage"' in script) is readable
        assert all(flag is False for _, flag in orchestrator.flags)


class _UserBashOrchestrator(FakeBashOrchestrator):
    config = _user()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.flags = []

    def execute_command(
        self,
        command,
        workdir=None,
        capture_stderr=True,
        environment=None,
        timeout=None,
        truncate_output=True,
        as_project_user=False,
    ):
        self.flags.append((command, as_project_user))
        return super().execute_command(
            command, workdir, capture_stderr, environment, timeout, truncate_output
        )

    def execute_command_with_monitoring(self, as_project_user=False, **kwargs):
        self.flags.append((kwargs["command"], as_project_user))
        return super().execute_command_with_monitoring(**kwargs)

    def execute_command_with_soft_timeout(self, command, as_project_user=False, **kwargs):
        self.flags.append((command, as_project_user))
        return {"success": True, "exit_code": 0, "output": "done", "stdout": "done", "stderr": ""}

    def execute_command_detached(self, command, as_project_user=False, **kwargs):
        self.flags.append((command, as_project_user))
        return {"started": False, "start_accepted": False, "runner_dispatched": False,
                "runner_dispatch_state": "not_accepted", "dispatch_status": "dispatch_failed"}


@pytest.mark.parametrize("command", ["echo hi", "npm install", "sleep 5 &"])
def test_bash_commands_run_as_the_user(command):
    orchestrator = _UserBashOrchestrator()
    tool = BashTool(orchestrator)
    tool.execute(command=command, timeout=120)
    flagged = _flagged(orchestrator)
    assert command.rstrip(" &") in " ".join(flagged) or command in flagged


def test_permission_denied_as_the_project_user_adds_one_sudo_hint():
    denied = {
        "success": False,
        "exit_code": 1,
        "output": "touch: cannot touch '/etc/motd': Permission denied",
        "stdout": "",
        "stderr": "touch: cannot touch '/etc/motd': Permission denied",
    }
    as_user = BashTool(_UserBashOrchestrator(command_result=denied)).execute(
        command="touch /etc/motd", timeout=30
    )
    as_root = BashTool(FakeBashOrchestrator(command_result=denied)).execute(
        command="touch /etc/motd", timeout=30
    )
    assert as_user.suggestions.count(SUDO_HINT) == 1
    assert SUDO_HINT not in as_root.suggestions


def test_coverage_runs_maven_and_gradle_as_the_user():
    class CoverageOrchestrator:
        config = _user()

        def __init__(self):
            self.flags = []

        def execute_command(self, command, as_project_user=False, **kwargs):
            self.flags.append((command, as_project_user))
            return {"success": True, "exit_code": 0, "output": ""}

        def execute_control_command(self, command, **kwargs):
            self.flags.append((command, False))
            return {"success": True, "exit_code": 0, "output": ""}

    orchestrator = CoverageOrchestrator()
    coverage_runner._inject_and_run(orchestrator, "/workspace/p", "maven")
    coverage_runner._inject_and_run(orchestrator, "/workspace/p", "gradle")
    coverage_runner._maven_report_only(orchestrator, "/workspace/p")
    runs = [(c, flag) for c, flag in orchestrator.flags if "mvn -B" in c or "gradlew" in c]
    assert len(runs) == 3 and all(flag is True for _, flag in runs)
    assert all(flag is False for c, flag in orchestrator.flags if c.startswith("cat > "))


# --------------------------------------------------------------------------
# 4. Checkout ownership and harness git calls
# --------------------------------------------------------------------------


class _CloneOrchestrator:
    def __init__(self, project_user=None, chown_ok=True):
        self.config = _user(project_user)
        self.commands = []
        self.chown_ok = chown_ok

    def execute_command(self, command, workdir=None, timeout=None, **kwargs):
        self.commands.append(("runtime", command))
        root = "/workspace/proj"
        if command == "which git":
            return {"success": True, "output": "/usr/bin/git\n", "exit_code": 0}
        if command == f"test -f {root}/.gitmodules":
            return {"success": True, "output": "", "exit_code": 0}
        if command == f"git -C {root} rev-parse HEAD":
            return {"success": True, "output": "a" * 40 + "\n", "exit_code": 0}
        if command.startswith(f"find {root} "):
            return {"success": True, "output": f"{root}/pom.xml\n", "exit_code": 0}
        return {"success": True, "output": "", "exit_code": 0}

    def execute_control_command(self, command, **kwargs):
        self.commands.append(("control", command))
        ok = self.chown_ok or not command.startswith("chown")
        return {"success": ok, "output": "" if ok else "chown: invalid user", "exit_code": 0 if ok else 1}


def _clone(orchestrator):
    return ProjectSetupTool(orchestrator).execute(
        action="clone", repository_url="https://github.com/example/proj.git", ref="v1.0"
    )


def test_the_checkout_is_handed_to_the_project_user_after_clone_ref_and_submodules():
    orchestrator = _CloneOrchestrator("sag")
    assert _clone(orchestrator).succeeded is True
    commands = [command for _channel, command in orchestrator.commands]
    chown = commands.index("chown -R sag:sag /workspace/proj")
    assert orchestrator.commands[chown][0] == "control"
    assert chown > max(i for i, c in enumerate(commands) if "checkout" in c or "submodule" in c)


def test_without_a_project_user_the_clone_changes_no_ownership():
    orchestrator = _CloneOrchestrator(None)
    assert _clone(orchestrator).succeeded is True
    assert not [c for _channel, c in orchestrator.commands if c.startswith("chown")]


def test_a_failed_handover_fails_the_clone_in_plain_english():
    result = _clone(_CloneOrchestrator("sag", chown_ok=False))
    assert result.succeeded is False
    assert result.error_code == "CHECKOUT_OWNERSHIP_FAILED"
    assert "project user" in result.error
    assert "chown: invalid user" in result.output


@pytest.mark.parametrize("project_user, prefix", [(None, ""), ("sag", "GIT_OPTIONAL_LOCKS=0 ")])
def test_harness_worktree_probe_never_takes_the_index_lock(tmp_path, project_user, prefix):
    seen = []

    class Owner:
        config = _user(project_user)

        def execute_control_command(self, command, **kwargs):
            seen.append(command)
            return {"success": True, "exit_code": 0, "output": "{}"}

    recorder = WorktreeEvidenceRecorder(
        tmp_path, run_id="run-1", project_root="/workspace/p",
        execute=Owner().execute_control_command,
    )
    with pytest.raises(ValueError):
        recorder._probe()
    assert seen[0].startswith(prefix + "python3 -c ")


# --------------------------------------------------------------------------
# 5. Stall diagnostics and benchmark observer probes
# --------------------------------------------------------------------------


def test_jvm_thread_dump_attaches_as_the_project_user():
    job = {"log_path": "/tmp/sag_jobs/abc.log", "working_directory": "/workspace/p"}
    as_root = stall_diagnostics._diagnostic_command(job_id="abc", pid=10, pgid=10, job=job)
    as_user = stall_diagnostics._diagnostic_command(
        job_id="abc", pid=10, pgid=10, job=job, project_user="sag"
    )
    drop = " ".join(SETPRIV)
    assert f'timeout --kill-after=2 10 {drop} jcmd "$java_pid" Thread.print' in as_user
    assert f'timeout --kill-after=2 10 {drop} jstack "$java_pid"' in as_user
    assert "setpriv" not in as_root
    assert as_user.replace(f" {drop}", "") == as_root


def test_stall_diagnostic_takes_the_user_from_the_orchestrator(monkeypatch):
    class UserHarness(_DiagnosticHarness):
        config = _user()

    harness = UserHarness()
    monkeypatch.setattr(stall_diagnostics, "write_container_text_atomic", harness.writer)
    stall_diagnostics.collect_stall_diagnostic(harness.execute_control_command, _stall_job())
    probe = next(c for c in harness.commands if "sag-job-diagnostic" in c)
    assert f"{' '.join(SETPRIV)} jcmd" in probe


def test_observer_environment_carries_the_project_user():
    class Owner:
        config = _user()

        def _default_exec_environment(self):
            return {"PATH": "/usr/bin"}

        def execute_control_command(self, command, **kwargs):
            return {}

    observer = SAGRequirementObserver.__new__(SAGRequirementObserver)
    observer.execute = Owner().execute_control_command
    assert observer._environment() == {
        "runtime_environment": {"PATH": "/usr/bin", "HOME": "/home/sag", "USER": "sag", "LOGNAME": "sag"},
        "environment_observed": True,
        "project_user": "sag",
    }
    Owner.config = _user(None)
    assert observer._environment() == {
        "runtime_environment": {"PATH": "/usr/bin"},
        "environment_observed": True,
    }


def test_observer_maven_probe_runs_through_setpriv(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "pom.xml").write_text("<project/>")
    subprocess.run(["git", "-C", str(tmp_path), "add", "pom.xml"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"],
        check=True,
    )
    head = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    request = {
        "root": str(tmp_path),
        "commit": head,
        "operation": "probe",
        "argv": ["sh", "-c", "echo RAN"],
        "cwd": ".",
        "timeout": 10,
        "project_user": "sag",
    }
    program = compilation_evidence.capture_program() + "\n" + _PROGRAM
    out = json.loads(
        subprocess.run(
            [sys.executable, "-c", program, json.dumps(request)], capture_output=True, text=True, check=True
        ).stdout
    )
    assert out["probe_argv"] == ["sh", "-c", "echo RAN"]
    assert out["probe_user"] == "sag"
    # The command never ran unwrapped as the caller (here setpriv is absent or
    # the account does not exist, so nothing ran at all).
    assert b"RAN" not in base64.b64decode(out["probe_stdout"])


# --------------------------------------------------------------------------
# 6. The default (unset) path issues today's commands, over the existing fakes
# --------------------------------------------------------------------------
#
# Each digest is sha256 over the JSON of every command (and keyword set) the
# unchanged code issued through the existing fixed-signature fakes. A fake
# without ``**kwargs`` also fails loudly if the default path grew an argument.

# Harness evidence writes (receipts and their views) carry random temporary
# names and timestamped content; they are root control I/O either way and are
# left out of the pin.
_EVIDENCE_WRITES = ("/workspace/.setup_agent/invocation_receipts", "/workspace/.setup_agent/receipt_views")


def _project_facing(commands):
    return [c for c in commands if not any(marker in c[0] for marker in _EVIDENCE_WRITES)]


PIN_MAVEN = "41b5f22ac2988b2faca6b1a0294090d7bca455b46fb3e91e4de3708f6166eb23"
PIN_GRADLE = "1d606f0cd8499a7e5f8d7f3b223794806249cf65d8a392c103347121987d7c0b"
PIN_VENV_LADDER = "757a9ed5d376fc04f4957fed303d49a3c456e07910a92b1e59a888ecacbcb3b5"
PIN_BASH = "8cb19b0e40e97e298c958b357ef2f98a5117a1786332b5e0b2daa9165fc362c1"


@pytest.mark.usefixtures("facade_contract_authority", "exact_internal_runner_authority")
def test_default_maven_commands_are_todays():
    orchestrator = FakeBuildToolOrchestrator()
    tool = MavenTool(orchestrator)
    tool._record_test_summary = lambda *args, **kwargs: None
    tool.execute(command="test", working_directory="/workspace/project")
    recorded = _project_facing([list(c) for c in orchestrator.commands]) + [
        [c, sorted(k.items())] for c, k in orchestrator.monitored_commands
    ]
    assert _digest(recorded) == PIN_MAVEN, recorded


@pytest.mark.usefixtures("facade_contract_authority", "exact_internal_runner_authority")
def test_default_gradle_commands_are_todays():
    orchestrator = FakeBuildToolOrchestrator({"output": "BUILD SUCCESSFUL", "exit_code": 0})
    GradleTool(orchestrator).execute(
        tasks="test", working_directory="/workspace/project", use_wrapper=False
    )
    recorded = _project_facing([list(c) for c in orchestrator.commands]) + [
        [c, sorted(k.items())] for c, k in orchestrator.monitored_commands
    ]
    assert _digest(recorded) == PIN_GRADLE, recorded


def test_default_venv_repair_commands_are_todays():
    orchestrator = LadderOrch(apt_ok=True, uv_install_ok=True, uv_venv_restores_pip=True)
    ensure_venv_pip(orchestrator, "/workspace/p/.venv")
    assert _digest(orchestrator.commands) == PIN_VENV_LADDER, orchestrator.commands


def test_default_bash_commands_are_todays():
    orchestrator = FakeBashOrchestrator()
    tool = BashTool(orchestrator)
    tool.execute(command="echo hi", timeout=7)
    tool.execute(command="npm install", timeout=120)
    recorded = [
        orchestrator.command_calls,
        [sorted(call.items()) for call in orchestrator.monitoring_calls],
    ]
    assert _digest(recorded) == PIN_BASH, recorded
