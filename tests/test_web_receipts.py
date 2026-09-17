"""The Workbench can show what each command actually did.

Two halves, and the second is the one that has bitten this plan before: a
summariser that reads a hand-built dict is easy to keep green while the reader
that feeds it in production hands it nothing. So `_read_receipts` is driven
here through a real `MirrorReader` over a real receipt directory — the same
class `ContainerSessionRegistry._reader` returns for every live session.
"""

from __future__ import annotations

import json
from pathlib import Path

from sag.agent.verdict_finalizer import RunVerdictSnapshot
from sag.result_card.build import build_result_card
from sag.web.models import ReceiptSummary, WorkspaceResult
from sag.web.session_mirror import MirrorReader
from sag.web.session_registry import (
    _read_receipts,
    _receipt_summary,
    _workspace_result,
)

from result_card_fakes import snapshot_dict


def _receipt(**overrides) -> dict:
    payload = {
        "schema_version": 3,
        "receipt_id": "inv-maven-1-ee86ae186d94-0002",
        "run_id": "r",
        "tool": "maven",
        "requested_action": "verify",
        "effective_action": "verify",
        "argv": "/opt/apache-maven-3.9.9/bin/mvn clean verify",
        "working_directory": "/workspace/commons-cli",
        "actual_cwd": "/workspace/commons-cli",
        "outcome": "completed",
        "exit_code": 0,
        "lifecycle_state": "finished",
        "toolchain_fingerprint": {
            "executable": "/opt/apache-maven-3.9.9/bin/mvn",
            "version": "Apache Maven 3.9.9",
        },
        "effective_jdk": {
            "major": "17",
            "runtime_authority": "dispatch_probe",
            "provenance": {"dispatch_runtime": {"version": "17.0.20"}},
        },
        "report_delta": {"new": [{"path": "a.xml", "sha256": "x"}], "changed": []},
        "testcase_execution_totals": {"reported": 994},
    }
    payload.update(overrides)
    return payload


def _mirror_with(tmp_path: Path, *receipts: dict) -> MirrorReader:
    """A mirror laid out the way `ensure_mirror` writes one."""

    directory = tmp_path / ".setup_agent" / "invocation_receipts"
    directory.mkdir(parents=True)
    for payload in receipts:
        name = f"{payload['receipt_id']}.json"
        (directory / name).write_text(json.dumps(payload), encoding="utf-8")
    return MirrorReader(tmp_path)


# ── one receipt, as it recorded itself ───────────────────────────────────────


def test_summary_copies_the_command_and_how_it_ended():
    summary = _receipt_summary(_receipt())
    assert summary.receipt_id == "inv-maven-1-ee86ae186d94-0002"
    assert summary.argv == "/opt/apache-maven-3.9.9/bin/mvn clean verify"
    assert summary.exit_code == 0
    assert summary.outcome == "completed"


def test_summary_carries_the_observed_toolchain():
    summary = _receipt_summary(_receipt())
    assert summary.toolchain["version"] == "Apache Maven 3.9.9"
    assert summary.jdk_major == "17"
    assert summary.jdk_version == "17.0.20"


def test_summary_counts_the_reports_the_run_wrote():
    summary = _receipt_summary(_receipt())
    assert summary.reports_new == 1
    assert summary.reports_changed == 0
    assert summary.tests_reported == 994


def test_a_receipt_without_optional_evidence_states_absence():
    summary = _receipt_summary(
        _receipt(
            toolchain_fingerprint=None,
            effective_jdk=None,
            testcase_execution_totals=None,
            exit_code=None,
        )
    )
    assert summary.toolchain is None
    assert summary.jdk_major is None
    assert summary.jdk_version is None
    assert summary.tests_reported is None
    assert summary.exit_code is None


def test_a_malformed_receipt_is_skipped_not_guessed_at():
    assert _receipt_summary({"receipt_id": "x"}) is None
    assert _receipt_summary(None) is None


def test_receipt_summary_serializes_under_camel_case():
    """Every multi-word field, not a sample of two: this is the whole wire."""

    dumped = _receipt_summary(_receipt()).model_dump(mode="json", by_alias=True)

    assert set(dumped) == {
        "receiptId",
        "tool",
        "argv",
        "workingDirectory",
        "actualCwd",
        "exitCode",
        "outcome",
        "lifecycleState",
        "toolchain",
        "jdkMajor",
        "jdkVersion",
        "reportsNew",
        "reportsChanged",
        "testsReported",
    }
    assert dumped["receiptId"] == "inv-maven-1-ee86ae186d94-0002"
    assert dumped["reportsNew"] == 1


# ── the reader the web layer actually uses ───────────────────────────────────


def test_the_mirror_reader_the_web_layer_uses_can_read_a_receipt_directory(tmp_path):
    """The production reader, not a double. A summariser nothing feeds is dead."""

    reader = _mirror_with(tmp_path, _receipt(), _receipt(receipt_id="inv-maven-1-a-0003"))

    receipts = _read_receipts(reader)

    assert receipts is not None
    assert [receipt.receipt_id for receipt in receipts] == [
        "inv-maven-1-a-0003",
        "inv-maven-1-ee86ae186d94-0002",
    ]
    assert receipts[1].argv == "/opt/apache-maven-3.9.9/bin/mvn clean verify"


def test_a_run_that_recorded_no_commands_reads_back_as_none_recorded(tmp_path):
    """An empty directory read cleanly is an empty list, and says so."""

    assert _read_receipts(_mirror_with(tmp_path)) == []


def test_a_stream_that_could_not_be_read_is_not_reported_as_no_commands(tmp_path):
    """`[]` means the run recorded none. A failed read must not borrow that word."""

    class _Unreadable:
        def execute_command(self, command, timeout=None, **_):
            return {"output": "", "exit_code": 1, "success": False}

    assert _read_receipts(_Unreadable()) is None
    assert _read_receipts(object()) is None


# ── the rail's four cells ────────────────────────────────────────────────────


def _snapshot(**overrides) -> RunVerdictSnapshot:
    return RunVerdictSnapshot.model_validate(snapshot_dict(**overrides))


def test_workspace_result_reads_its_verdict_and_ci_off_the_card():
    snapshot = _snapshot()
    card = build_result_card(snapshot).model_dump(mode="json")

    result = _workspace_result(card, snapshot)

    assert result.verdict == card["verdict"]
    assert result.ci.status == card["rows"][5]["status"]


def test_workspace_result_counts_steps_and_tests_from_the_record():
    snapshot = _snapshot()
    card = build_result_card(snapshot).model_dump(mode="json")

    result = _workspace_result(card, snapshot)

    steps = tuple(snapshot.task_completion.steps)
    assert steps, "the fixture states at least one required-task step"
    assert result.task.status == snapshot.task_completion.status
    assert result.task.required == len(steps)
    assert result.task.completed == sum(1 for step in steps if step.status == "complete")
    assert result.tests.executed == snapshot.test_stats.unique.executed == 994
    assert result.tests.passed == snapshot.test_stats.unique.passed == 933


def test_a_task_the_record_could_not_measure_states_no_fraction():
    """No invented `0/0`: a record with no steps declined to state a denominator."""

    snapshot = _snapshot(
        verdict="partial",
        task_completion={
            "run_id": snapshot_dict()["run_id"],
            "task_sha256": None,
            "status": "unavailable",
            "steps": [],
            "reasons": ["task_run_pin_unavailable"],
        },
    )
    card = build_result_card(snapshot).model_dump(mode="json")

    assert _workspace_result(card, snapshot).task is None


def test_a_run_with_no_card_has_no_result_strip():
    assert _workspace_result(None, _snapshot()) is None


def test_workspace_result_serializes_the_whole_strip():
    result = _workspace_result(
        build_result_card(_snapshot()).model_dump(mode="json"), _snapshot()
    )

    dumped = result.model_dump(mode="json", by_alias=True)

    assert set(dumped) == {"verdict", "task", "tests", "ci"}
    assert set(dumped["task"]) == {"status", "completed", "required"}
    assert set(dumped["tests"]) == {"executed", "passed", "failed", "errors", "skipped"}
    assert set(dumped["ci"]) == {"status"}
    assert isinstance(WorkspaceResult.model_validate(dumped), WorkspaceResult)
