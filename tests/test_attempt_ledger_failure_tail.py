from types import SimpleNamespace

from sag.agent.attempt_ledger import AttemptLedger, compact_steps
from sag.tools.base import ToolResult


def test_cmake_failure_keeps_fatal_tail_and_full_ref():
    ledger = AttemptLedger()
    output = "configure start\n" + ("ordinary line\n" * 400) + "CMake Error: target missing\n"

    ledger.record_failed_action(
        action_key="run_command:cmake",
        output=output,
        output_ref="log://build-4/full",
        error_code="BUILD_FAILED",
        failure_signature="cmake:target-missing",
    )

    entry = ledger.prompt_entries()[0]
    assert "CMake Error: target missing" in entry.preview
    assert entry.output_ref == "log://build-4/full"
    assert entry.error_code == "BUILD_FAILED"
    assert entry.failure_signature == "cmake:target-missing"


def test_identical_failure_signatures_dedupe_without_losing_latest_ref():
    ledger = AttemptLedger()
    ledger.record_failed_action(
        action_key="build:compile",
        output="old fatal tail",
        output_ref="log://old",
        error_code="BUILD_FAILED",
        failure_signature="cmake:same",
    )
    ledger.record_failed_action(
        action_key="build:compile",
        output="new fatal tail",
        output_ref="log://new",
        error_code="BUILD_FAILED",
        failure_signature="cmake:same",
    )

    entries = ledger.prompt_entries()
    assert len(entries) == 1
    assert entries[0].occurrence_count == 2
    assert entries[0].output_ref == "log://new"
    assert "new fatal tail" in entries[0].preview


def test_identical_signature_dedupes_across_compaction_waves():
    def failure(output, ref):
        return SimpleNamespace(
            step_type=SimpleNamespace(value="action"),
            tool_name="build",
            tool_result=ToolResult.completed_failure(
                output=output,
                error=output,
                error_code="BUILD_FAILED",
                failure_signature="cmake:same",
                metadata={"output_ref_id": ref},
            ),
            content="",
        )

    thought = SimpleNamespace(
        step_type=SimpleNamespace(value="thought"),
        tool_name=None,
        tool_result=None,
        content="later",
    )
    first, remaining = compact_steps([failure("old fatal", "log://old"), thought], keep_recent=1)
    ledger_step = SimpleNamespace(
        step_type=SimpleNamespace(value="system_guidance"),
        tool_name=None,
        tool_result=None,
        content=first,
    )

    second, _ = compact_steps(
        [ledger_step, *remaining, failure("new fatal", "log://new"), thought],
        keep_recent=1,
    )

    assert second.count("signature=cmake:same") == 1
    assert "log://new" in second
    assert "×2" in second
