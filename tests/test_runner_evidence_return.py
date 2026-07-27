# tests/test_runner_evidence_return.py
"""What the runner already computed comes back in its result (Plan 7 A2/A3).

Live evidence (commons-cli, `logs/session_20260727_035638_85557`): the model
spent seven actions re-deriving test counts by hand — a search with the right
pattern against the wrong target, three `bash` crashes on shell quoting, one
`python: command not found` — before parsing the surefire XML with python3.
The counts had already been computed by the runner and thrown away, because
the analysis was handed the orchestrator's head-30/tail-50 window instead of
the complete log, and because the truncation notice pointed at `bash`+`grep`,
which cannot reach an `output_<id>` storage reference.

Scripted-orchestrator style (house pattern, shared with
tests/test_maven_gradle_tool_contracts.py).
"""

from test_invocation_receipts import receipts_written
from test_maven_gradle_tool_contracts import FakeBuildToolOrchestrator, FakeToolchainManager

from sag.agent.invocation_receipts import output_content_hash
from sag.docker_orch.orch import DockerOrchestrator
from sag.tools.base import BaseTool, ToolResult
from sag.tools.internal.gradle_tool import GradleTool
from sag.tools.internal.maven_tool import MavenTool

WORKDIR = "/workspace/project"


def clamped(full_output: str) -> str:
    """The orchestrator's own model-facing window — the real clamp, not a copy."""
    return DockerOrchestrator._truncate_output_smartly(None, full_output)


def buried(summary_lines, success_marker):
    """A reactor log whose only aggregate summary sits in the omitted middle."""
    head = [f"[INFO] Building module-{index}" for index in range(60)]
    tail = [f"[INFO] --- packaged module-{index}" for index in range(60)]
    return "\n".join(head + list(summary_lines) + tail + [success_marker])


MAVEN_LOG = buried(
    [
        "[INFO] Results:",
        "[INFO] ",
        "[INFO] Tests run: 982, Failures: 0, Errors: 0, Skipped: 61",
        "[INFO] ",
    ],
    "[INFO] BUILD SUCCESS",
)
GRADLE_LOG = buried(
    ["> Task :test", "214 tests completed, 3 failed, 5 skipped"],
    "BUILD SUCCESSFUL in 10s",
)


class SplitOutputOrchestrator(FakeBuildToolOrchestrator):
    """Hands back a clamped inline `output` beside the complete `full_output`,
    exactly as `collect_detached_result` does for a dispatch-and-poll build."""

    def __init__(self, full_output, exit_code=0):
        super().__init__()
        self.full_output = full_output
        self.exit_code = exit_code

    def execute_command_with_monitoring(self, command, **kwargs):
        self.monitored_commands.append((command, kwargs))
        return {
            "success": self.exit_code == 0,
            "exit_code": self.exit_code,
            "output": clamped(self.full_output),
            "full_output": self.full_output,
            "runner_dispatched": True,
        }


class RecordingTracker:
    def __init__(self):
        self.test_commands = []
        self.build_commands = []

    def track_test_command(self, **kwargs):
        self.test_commands.append(kwargs)

    def track_build_command(self, **kwargs):
        self.build_commands.append(kwargs)


# ---------------------------------------------------------------------------
# A2 — the analysis reads the complete output
# ---------------------------------------------------------------------------


def test_the_fixture_actually_buries_the_summary_in_the_omitted_middle():
    """Without this the regression test below would pass for the wrong reason."""
    assert "Tests run: 982" not in clamped(MAVEN_LOG)
    assert "214 tests completed" not in clamped(GRADLE_LOG)


def test_maven_test_counts_survive_a_log_whose_summary_is_only_in_the_middle():
    orchestrator = SplitOutputOrchestrator(MAVEN_LOG)
    tool = MavenTool(orchestrator, toolchain_manager=FakeToolchainManager())

    result = tool.execute(command="test", working_directory=WORKDIR, use_wrapper=False)

    assert result.test_stats is not None
    assert result.test_stats.executed == 982
    assert result.test_stats.failed == 0
    assert result.test_stats.skipped == 61
    assert result.test_stats.passed == 921
    assert result.metadata["analysis"]["tests_run"] == {
        "total": 982,
        "failures": 0,
        "errors": 0,
        "skipped": 61,
    }


def test_gradle_test_counts_survive_the_same_shaped_log():
    orchestrator = SplitOutputOrchestrator(GRADLE_LOG)
    tool = GradleTool(orchestrator)

    result = tool.execute(tasks="test", working_directory=WORKDIR, use_wrapper=False)

    assert result.test_stats is not None
    assert result.test_stats.executed == 214
    assert result.test_stats.failed == 3
    assert result.test_stats.skipped == 5


def test_the_command_tracker_is_handed_the_complete_output_too():
    orchestrator = SplitOutputOrchestrator(MAVEN_LOG)
    tracker = RecordingTracker()
    tool = MavenTool(
        orchestrator, command_tracker=tracker, toolchain_manager=FakeToolchainManager()
    )

    tool.execute(command="test", working_directory=WORKDIR, use_wrapper=False)

    assert tracker.test_commands
    assert "Tests run: 982" in tracker.test_commands[0]["output"]


# ---------------------------------------------------------------------------
# A3 invariant — the receipt hash covers the complete output
# ---------------------------------------------------------------------------


def test_the_receipt_hash_covers_the_complete_output_never_the_window():
    """Truncation applies only to the model-facing window; a hash over
    truncated text would mean nothing."""
    orchestrator = SplitOutputOrchestrator(MAVEN_LOG)
    tool = MavenTool(orchestrator, toolchain_manager=FakeToolchainManager())

    tool.execute(command="test", working_directory=WORKDIR, use_wrapper=False)

    receipts = receipts_written([command for command, _workdir, _timeout in orchestrator.commands])
    assert receipts
    assert receipts[0]["output_content_hash"] == output_content_hash(MAVEN_LOG)
    assert receipts[0]["output_content_hash"] != output_content_hash(clamped(MAVEN_LOG))


# ---------------------------------------------------------------------------
# A3 — the truncation notice names an affordance that can read the output
# ---------------------------------------------------------------------------

REF = "output_5560fdb2ad7b"


class StubTool(BaseTool):
    """A tool whose result is fixed, so only truncation is under test."""

    def __init__(self, result):
        super().__init__(name="stub", description="stub")
        self._result = result

    def execute(self, **kwargs):
        return self._result


def _truncated(result):
    return StubTool(result).safe_execute().output


LONG = "[INFO] Tests run: 982, Failures: 0, Errors: 0, Skipped: 61\n" * 400


def test_the_notice_states_the_stored_reference_and_the_call_that_reads_it():
    output = _truncated(ToolResult.completed_success(output=LONG, metadata={"output_ref_id": REF}))

    assert "OUTPUT TRUNCATED" in output
    assert f"output_search(action='grep', ref_id='{REF}', grep_pattern='Tests run')" in output


def test_the_notice_reads_the_reference_off_the_canonical_output_ref_field_too():
    result = ToolResult.completed_success(output=LONG, output_ref=REF)

    assert f"ref_id='{REF}'" in _truncated(result)


def test_the_notice_no_longer_points_at_bash_and_grep():
    output = _truncated(ToolResult.completed_success(output=LONG, metadata={"output_ref_id": REF}))

    assert "'bash' tool" not in output
    assert "file_io" not in output


def test_without_a_stored_reference_the_notice_says_there_is_none():
    output = _truncated(ToolResult.completed_success(output=LONG))

    assert "OUTPUT TRUNCATED" in output
    assert "no stored output reference" in output
    assert "output_search" not in output
