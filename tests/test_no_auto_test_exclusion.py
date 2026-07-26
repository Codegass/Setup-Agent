# tests/test_no_auto_test_exclusion.py
"""Failed tests/modules must never be auto-converted into Maven exclusions
(spec §3.4-3: exclusion is evidence destruction, not recovery)."""

from types import SimpleNamespace

from sag.agent.tool_recovery import ToolRecoveryHandler
from sag.tools.base import ToolResult


class FakeMaven:
    def __init__(self):
        self.calls = []

    def safe_execute(self, **params):
        self.calls.append(params)
        return ToolResult.completed_success(output="BUILD SUCCESS")


def _recovery(maven):
    return ToolRecoveryHandler(
        tools={"maven": maven},
        context_manager=SimpleNamespace(orchestrator=None),
        successful_states={},
        repository_url=None,
        add_system_guidance=lambda *a, **k: None,
    )


def test_failed_tests_are_not_excluded_and_not_rerun():
    maven = FakeMaven()
    failed = ToolResult.completed_failure(
        output="Tests run: 45, Failures: 1, Errors: 12",
        error="test failures",
        error_code="TEST_FAILURE",
        metadata={
            "analysis": {
                "failed_tests": ["regularUserShell(org.apache.bigtop.itest.shell.ShellTest)"],
                "failed_modules": [{"artifact_id": "itest-common", "pom_path": "pom.xml"}],
            }
        },
    )
    decision = _recovery(maven)._recover_maven_error(
        {"command": "install", "working_directory": "/workspace/bigtop"}, failed
    )
    assert decision.should_recover is False
    assert maven.calls == []


def test_exclusion_machinery_is_gone():
    assert not hasattr(ToolRecoveryHandler, "_recover_maven_exclusions")
    assert not hasattr(ToolRecoveryHandler, "_format_test_exclusion")
