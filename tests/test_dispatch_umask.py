"""The project's own commands run with umask 022.

``docker exec`` sessions in these containers start with umask 0000 (PID 1 has
0022), so every file a clone or a build wrote came out world-writable: 777 for
directories, 666 for files. Root never noticed; an unprivileged project user
does (Hadoop's DataNode storage checks are one example), and official CI runs
with 022. Host-control commands are not the project's and keep their own
handling: the detached supervisor's ``umask 077`` for the dispatch directory
and no umask at all on the plain control path.
"""

import os
import shlex
import stat
import subprocess
from types import SimpleNamespace

import pytest
from test_project_user import (
    DISPATCH,
    PROFILE,
    _detached_orchestrator,
    _exec_orchestrator,
    _job_script,
)

from sag.tools.internal.project_setup_tool import ProjectSetupTool


@pytest.mark.parametrize("project_user, flag", [(None, {}), ("sag", {"as_project_user": True})])
def test_a_project_command_starts_with_umask_022(project_user, flag):
    orchestrator, container, _api = _exec_orchestrator(project_user)
    orchestrator.execute_command("mvn -v", workdir="/workspace/p", timeout=30, **flag)
    assert container.calls[0]["cmd"][-1] == f"{DISPATCH}; cd /workspace/p && mvn -v"


@pytest.mark.parametrize("project_user, flag", [(None, {}), ("sag", {"as_project_user": True})])
def test_a_monitored_project_command_starts_with_umask_022(project_user, flag):
    orchestrator, _container, api = _exec_orchestrator(project_user)
    orchestrator.execute_command_with_monitoring(
        "mvn -B test",
        workdir="/workspace/p",
        absolute_timeout=600,
        enable_cpu_monitoring=False,
        optimize_for_maven=False,
        **flag,
    )
    assert api.created[0]["cmd"][-1] == f"{DISPATCH}; cd /workspace/p && mvn -B test"


def test_a_monitored_command_without_a_workdir_starts_with_umask_022():
    orchestrator, _container, api = _exec_orchestrator()
    orchestrator.execute_command_with_monitoring(
        "mvn -B test", enable_cpu_monitoring=False, optimize_for_maven=False
    )
    assert api.created[0]["cmd"][-1] == f"{DISPATCH}; mvn -B test"


def test_control_commands_set_no_umask():
    orchestrator, container, _api = _exec_orchestrator("sag")
    orchestrator.execute_control_command("chown -R sag:sag /workspace/p", timeout=30)
    orchestrator.execute_control_command("cat /tmp/x")
    assert [call["cmd"][-1] for call in container.calls] == [
        f"{PROFILE}; chown -R sag:sag /workspace/p",
        f"{PROFILE}; cat /tmp/x",
    ]


def _runtime_script(supervisor):
    """The ``bash -c`` script the job execs into, and the job script before it."""

    job_script = _job_script(supervisor)
    exec_at = job_script.index("exec ")
    runtime = shlex.split(job_script[exec_at + len("exec ") :])
    assert runtime[-3:-1] == ["/bin/bash", "-c"]
    return job_script[:exec_at], runtime[-1]


@pytest.mark.parametrize("project_user, flag", [(None, {}), ("sag", {"as_project_user": True})])
def test_a_detached_job_sets_umask_022_inside_the_execd_shell(monkeypatch, project_user, flag):
    orchestrator = _detached_orchestrator(monkeypatch, project_user)
    handle = orchestrator.execute_command_detached("mvn -B test", workdir="/workspace/demo", **flag)
    assert handle["started"] is True
    supervisor = orchestrator.detached_api.create_calls[0]["cmd"][-1]
    # The root supervisor keeps its own private umask for the dispatch
    # directory and the pid/pgid/identity files it writes...
    assert supervisor.startswith("set +e; umask 077; mkdir -p /tmp/sag_jobs")
    before_exec, inner = _runtime_script(supervisor)
    assert "umask 022" not in before_exec
    # ...and the project's command sets 022 itself, after the exec.
    assert inner == f"{DISPATCH}; cd /workspace/demo && mvn -B test"
    syntax = subprocess.run(["bash", "-n", "-c", supervisor], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr


def test_a_detached_control_job_keeps_only_the_supervisor_umask(monkeypatch):
    orchestrator = _detached_orchestrator(monkeypatch, "sag")
    handle = orchestrator.execute_control_command_detached("du -s /workspace")
    assert handle["started"] is True
    supervisor = orchestrator.detached_api.create_calls[0]["cmd"][-1]
    _before_exec, inner = _runtime_script(supervisor)
    assert inner == f"{PROFILE}; du -s /workspace"


def test_a_soft_timeout_dispatch_gets_the_project_prefix(monkeypatch):
    orchestrator = _detached_orchestrator(monkeypatch)
    dispatch = orchestrator.execute_command_detached

    def dispatch_then_stop(command, **kwargs):
        dispatch(command, **kwargs)
        # Stop before the poll loop; only the dispatched command matters here.
        return {"started": False, "runner_dispatched": False, "launch_output": "stop"}

    orchestrator.execute_command_detached = dispatch_then_stop
    orchestrator.execute_command_with_soft_timeout("mvn test", workdir="/workspace/demo")
    supervisor = orchestrator.detached_api.create_calls[0]["cmd"][-1]
    _before_exec, inner = _runtime_script(supervisor)
    assert inner == f"{DISPATCH}; cd /workspace/demo && mvn test"


# --------------------------------------------------------------------------
# A real clone through the orchestrator, in a session that starts at 0000
# --------------------------------------------------------------------------


def _run_in_umask_0000(script, cwd=None):
    """Run ``bash -c script`` the way a docker exec session starts: umask 0000."""

    environment = {
        **os.environ,
        # The fixture's submodule lives on the local filesystem; recent git
        # refuses the file transport for submodules unless told otherwise.
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "protocol.file.allow",
        "GIT_CONFIG_VALUE_0": "always",
    }
    return subprocess.run(
        ["/bin/sh", "-c", 'umask 0000; exec /bin/bash -c "$1"', "sh", script],
        capture_output=True,
        cwd=cwd,
        env=environment,
        timeout=120,
    )


class _HostShellContainer:
    """Runs each exec's ``bash -c`` script on this host, starting at umask 0000."""

    id = "c" * 64

    def __init__(self):
        self.scripts = []

    def exec_run(self, cmd, **kwargs):
        script = cmd[-1]
        self.scripts.append(script)
        done = _run_in_umask_0000(script)
        return SimpleNamespace(exit_code=done.returncode, output=(done.stdout, done.stderr))


def _git(*args, cwd):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "protocol.file.allow=always", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _source_repository(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    (library / "lib.txt").write_text("lib\n")
    _git("init", "-q", cwd=library)
    _git("add", ".", cwd=library)
    _git("commit", "-qm", "lib", cwd=library)

    source = tmp_path / "source"
    (source / "src" / "main").mkdir(parents=True)
    (source / "pom.xml").write_text("<project/>\n")
    (source / "src" / "main" / "App.java").write_text("class App {}\n")
    _git("init", "-q", cwd=source)
    _git("submodule", "add", "-q", str(library), "third_party/library", cwd=source)
    _git("add", ".", cwd=source)
    _git("commit", "-qm", "app", cwd=source)
    return source


def _writable_by_others(root):
    found = []
    for directory, subdirectories, files in os.walk(root):
        for name in [*subdirectories, *files]:
            path = os.path.join(directory, name)
            mode = os.lstat(path).st_mode
            if not stat.S_ISLNK(mode) and mode & (stat.S_IWGRP | stat.S_IWOTH):
                found.append((os.path.relpath(path, root), oct(stat.S_IMODE(mode))))
    return found


def test_the_fixture_session_really_starts_at_umask_0000(tmp_path):
    source = _source_repository(tmp_path)
    done = _run_in_umask_0000(f"git clone -q {shlex.quote(str(source))} plain", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert stat.S_IMODE(os.stat(tmp_path / "plain").st_mode) == 0o777
    assert stat.S_IMODE(os.stat(tmp_path / "plain" / "pom.xml").st_mode) == 0o666


def test_a_clone_through_a_project_dispatch_is_755_and_644(tmp_path):
    source = _source_repository(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    orchestrator, _container, _api = _exec_orchestrator()
    container = _HostShellContainer()
    orchestrator.client = SimpleNamespace(
        api=orchestrator.client.api, containers=SimpleNamespace(get=lambda _name: container)
    )

    result = ProjectSetupTool(orchestrator).execute(
        action="clone",
        repository_url=str(source),
        target_directory="proj",
        working_directory=str(workspace),
    )

    assert result.succeeded is True, result.output
    clone = workspace / "proj"
    assert "Submodules: initialized" in result.output
    assert (clone / "third_party" / "library" / "lib.txt").is_file()
    assert stat.S_IMODE(os.stat(clone).st_mode) == 0o755
    assert stat.S_IMODE(os.stat(clone / "src" / "main").st_mode) == 0o755
    assert stat.S_IMODE(os.stat(clone / "pom.xml").st_mode) == 0o644
    assert stat.S_IMODE(os.stat(clone / "third_party" / "library" / "lib.txt").st_mode) == 0o644
    assert _writable_by_others(clone) == []
    # Every git step (clone, submodules, commit lookup) went through the
    # project prefix; none bypassed it.
    git_steps = [script for script in container.scripts if "git " in script]
    assert any("submodule update" in script for script in git_steps)
    assert git_steps and all(script.startswith("umask 022; ") for script in git_steps)
