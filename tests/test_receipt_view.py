import copy
import hashlib
import json

import pytest
from test_invocation_receipts import minimal_valid_receipt, FakeExecute

from sag.agent.receipt_view import build_receipt_view, render_receipt_view, write_receipt_view
from sag.agent.tool_orchestration import format_tool_result
from sag.tools.base import ToolResult


def digest(receipt):
    return hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()


def test_view_copies_observations_without_filling_missing_tests_or_modules():
    receipt = minimal_valid_receipt("inv-view-1")
    before = copy.deepcopy(receipt)
    view = build_receipt_view(receipt, source_sha256=digest(receipt))
    assert view["observed"]["argv"] == receipt["argv"]
    assert view["observed"]["testcase_execution_totals"] is None
    assert view["module_outcomes"]["known_count"] is None
    assert "not a task" in view["purpose"]
    assert receipt == before
    view["observed"]["argv"] = "changed view"
    assert receipt == before


def test_view_does_not_upgrade_failed_invocation_or_missing_runtime():
    receipt = minimal_valid_receipt("inv-view-2")
    receipt.update(outcome="failed", exit_code=1)
    view = build_receipt_view(receipt, source_sha256=digest(receipt))
    text = render_receipt_view(view, "/workspace/.setup_agent/receipt_views/view.json")
    assert "Invocation: failed; exit_code=1" in text
    assert "JDK=None" in text
    assert "Report execution totals: null" in text


def test_view_rejects_wrong_raw_receipt_binding():
    receipt = minimal_valid_receipt("inv-view-3")
    with pytest.raises(ValueError, match="exact published"):
        build_receipt_view(receipt, source_sha256="0" * 64)


def test_view_is_written_outside_project_and_raw_receipt_collection():
    receipt = minimal_valid_receipt("inv-view-4")
    execute = FakeExecute()
    meta = write_receipt_view(execute, receipt, source_sha256=digest(receipt))
    assert meta["receipt_view_status"] == "available"
    assert meta["receipt_view_path"].startswith("/workspace/.setup_agent/receipt_views/")
    assert all("/workspace/proj/" not in cmd for cmd in execute.commands)
    result = ToolResult.completed_success(
        output="runner log", metadata={"receipt_id": receipt["receipt_id"], **meta}
    )
    rendered = format_tool_result("build", result)
    assert meta["receipt_view_path"] in rendered and digest(receipt) in rendered
    assert "Report execution totals: null" in rendered


def test_view_write_failure_keeps_existing_source_and_does_not_claim_available():
    receipt = minimal_valid_receipt("inv-view-5")
    meta = write_receipt_view(
        FakeExecute(default={"success": False, "exit_code": 1}),
        receipt,
        source_sha256=digest(receipt),
    )
    assert meta["receipt_view_status"] == "unavailable"
    assert meta["receipt_path"].endswith("inv-view-5.json")
    assert "receipt_view_path" not in meta
