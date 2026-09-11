from pathlib import Path

import pytest

from sag.tools.internal.maven_tool import MavenTool
from test_maven_gradle_tool_contracts import FakeBuildToolOrchestrator, FakeOutputStorage

pytestmark = pytest.mark.usefixtures("facade_contract_authority", "exact_internal_runner_authority")


@pytest.mark.parametrize(
    "frame",
    [
        "[ERROR] \tat example.Test.run(Test.java:42)",
        "[ERROR]     at java.base/java.lang.Thread.run(Thread.java:840)",
        "[ERROR] at org.example.Fork$2.call(Fork.java:465)",
    ],
)
def test_java_runtime_stack_frames_are_not_compiler_diagnostics(frame):
    analysis = MavenTool(None)._analyze_maven_output(frame, 1)
    assert analysis["compilation_errors"] == []
    assert analysis["build_success"] is False


@pytest.mark.parametrize(
    "diagnostic",
    [
        "[ERROR] /workspace/demo/src/Thing.java:[12,3] cannot find symbol",
        "[ERROR] Thing.java:12: error: illegal start of expression",
        "[ERROR] COMPILATION ERROR :",
    ],
)
def test_compiler_diagnostics_survive_neighboring_runtime_frames(diagnostic):
    output = diagnostic + "\n[ERROR] \tat example.Compiler.run(Compiler.java:42)"
    analysis = MavenTool(None)._analyze_maven_output(output, 1)
    assert analysis["compilation_errors"] == [diagnostic]


def failed_result(output):
    orchestrator = FakeBuildToolOrchestrator({"output": output, "exit_code": 1})
    tool = MavenTool(orchestrator)
    tool.output_storage = FakeOutputStorage("output_maven_failure_feedback")
    tool._record_test_summary = lambda *args, **kwargs: None
    return tool.execute(command="verify", working_directory="/workspace/project")


def test_curator_fork_crash_is_reported_as_execution_failure():
    output = (
        Path(__file__).parent / "fixtures/maven-failure-feedback/curator-fork-exit.txt"
    ).read_text()
    result = failed_result(output)
    assert result.succeeded is False
    assert result.error_code == "MAVEN_EXECUTION_ERROR"
    assert "Test JVM did not complete normally" in result.error
    assert "Compilation errors found" not in result.error
    assert result.metadata["analysis"]["compilation_errors"] == []
    assert result.metadata["analysis"]["test_failure_count"] == 0
    assert result.metadata["exit_code"] == 1
    assert result.raw_output == output
    assert "Process Exit Code: 2" in result.raw_output
    assert "OUT_OF_MEMORY" not in result.error_code


@pytest.mark.parametrize(
    "diagnostic",
    [
        "The forked VM terminated without properly saying goodbye. VM crash or System.exit called?",
        "Error occurred in starting fork, check output in log",
    ],
)
def test_fork_error_keeps_partial_counts_separate_from_runner_completion(diagnostic):
    output = (
        "[INFO] Tests run: 4, Failures: 0, Errors: 0, Skipped: 0\n"
        f"[ERROR] {diagnostic}\n[INFO] BUILD FAILURE"
    )
    result = failed_result(output)
    assert result.succeeded is False
    assert result.error_code == "MAVEN_EXECUTION_ERROR"
    assert result.metadata["analysis"]["tests_run"]["total"] == 4
    assert "do not prove completion" in " ".join(result.suggestions)


def test_ordinary_assertion_failure_remains_test_failure():
    output = (
        "[ERROR] Tests run: 4, Failures: 1, Errors: 0, Skipped: 0\n"
        "[ERROR] at example.Test.run(Test.java:42)\n[INFO] BUILD FAILURE"
    )
    result = failed_result(output)
    assert result.error_code == "TEST_FAILURE"
    assert result.metadata["analysis"]["compilation_errors"] == []
    assert result.metadata["analysis"]["test_failure_count"] == 1


def test_unclassified_stack_trace_does_not_invent_a_test_runtime_failure():
    result = failed_result("[ERROR] at example.Plugin.run(Plugin.java:42)")
    assert result.succeeded is False
    assert result.error_code == "MAVEN_BUILD_ERROR"
    assert "Compilation errors found" not in result.error
