# tests/test_maven_command_quoting.py
"""Maven command construction must be shell-safe token-by-token (2026-07-24
bigtop run: internally generated -Dtest=!name(pkg#Class) tokens reached
bash -c unquoted and killed the launcher before the exit marker)."""

import re
import shlex

import pytest

from sag.docker_orch.orch import DockerOrchestrator
from sag.tools.internal.maven_tool import MavenTool

SUREFIRE_EXCLUSION = "test=!installBash(org.apache.bigtop.itest.pmanager#PackageManagerTest),!regularUserShell(org.apache.bigtop.itest.shell#ShellTest)"


def _tool():
    tool = MavenTool.__new__(MavenTool)
    tool.orchestrator = None
    return tool


def test_exclusion_tokens_are_quoted_at_the_shell_boundary():
    cmd = _tool()._build_maven_command(
        command="compile",
        goals="",
        profiles="",
        properties=[SUREFIRE_EXCLUSION],
        fail_at_end=True,
    )
    # The QUOTED literal must appear in the command string: an unquoted
    # -Dtest=!name(pkg#Class) token survives shlex.split round-trips by luck
    # but kills bash -c. shlex.quote is the contract.
    assert shlex.quote(f"-D{SUREFIRE_EXCLUSION}") in cmd
    tokens = shlex.split(cmd)
    assert tokens[0] == "mvn"
    assert "--fail-at-end" in tokens
    assert f"-D{SUREFIRE_EXCLUSION}" in tokens


def test_plain_command_is_unchanged_in_shape():
    cmd = _tool()._build_maven_command(command="clean install", goals="", profiles="", properties="")
    assert shlex.split(cmd) == ["mvn", "clean", "install"]


BASH_PARSE_LOG = "bash: -c: line 1: syntax error near unexpected token `('\nbash: -c: line 1: `set +e; ...'"


class _FakeOrch(DockerOrchestrator):
    def __init__(self):
        pass

    def execute_command(self, command, workdir=None, timeout=None, truncate_output=True):
        return {"success": True, "exit_code": 0, "output": BASH_PARSE_LOG}


def test_bash_parse_failure_is_a_distinct_launcher_error(monkeypatch):
    orch = _FakeOrch()
    monkeypatch.setattr(_FakeOrch, "_detached_poll_state", lambda self, poll: "vanished")
    monkeypatch.setattr(
        _FakeOrch, "_truncate_output_smartly", lambda self, text: text, raising=False
    )
    result = orch.collect_detached_result(
        {"log_path": "/tmp/sag_jobs/x.log", "job_id": "x", "pid": 1},
        {"exit_code": None},
    )
    assert result["exit_code"] == 1
    assert "[launcher error:" in result["full_output"]
