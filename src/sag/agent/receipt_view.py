"""Deterministic reading aid for a published receipt; never verdict evidence."""

from copy import deepcopy
import hashlib
import json

from sag.utils.container_io import write_container_text_atomic

VIEW_DIR = "/workspace/.setup_agent/receipt_views"
# Presentation bounds only. Counts come from receipt totals, never this sample.
CASE_EXAMPLE_LIMIT = 10
MODULE_EXAMPLE_LIMIT = 50


def build_receipt_view(receipt, *, source_sha256):
    from sag.agent.invocation_receipts import RECEIPT_DIR, validate_receipt_v2

    receipt = validate_receipt_v2(receipt)
    canonical_sha256 = hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
    if source_sha256 != canonical_sha256:
        raise ValueError("receipt view must bind the exact published receipt bytes")
    fields = (
        "receipt_id",
        "run_id",
        "target_sha",
        "tool",
        "requested_action",
        "effective_action",
        "argv",
        "working_directory",
        "actual_cwd",
        "exit_code",
        "outcome",
        "lifecycle_state",
        "termination_reason",
        "effective_jdk",
        "toolchain_fingerprint",
        "contract_id",
        "contract_hash",
        "compliance",
        "output_content_hash",
        "testcase_execution_totals",
        "testcase_row_disclosure",
        "gradle_row_disclosure",
        "gradle_suite_summaries",
        "evidence_omissions",
    )
    # Absent evidence stays null. In particular, no report is not zero tests.
    observed = {key: deepcopy(receipt.get(key)) for key in fields}
    modules = receipt.get("module_outcomes")
    cases = [
        {"source_index": i, **deepcopy(row)}
        for i, row in enumerate((receipt.get("testcase_outcomes") or {}).get("nodes", []))
        if row.get("status") != "passed"
    ]
    return {
        "schema_version": 1,
        "purpose": "receipt reading view; not a task or phase success verdict",
        "source": {
            "path": f"{RECEIPT_DIR}/{receipt['receipt_id']}.json",
            "sha256": source_sha256,
            "observed_fields": "Each observed key names the same top-level source field.",
        },
        "observed": observed,
        "module_outcomes": {
            "source_field": "module_outcomes",
            "known_count": len(modules) if modules is not None else None,
            "sample": deepcopy(modules[:MODULE_EXAMPLE_LIMIT]) if modules is not None else None,
            "omitted": max(0, len(modules) - MODULE_EXAMPLE_LIMIT) if modules is not None else None,
        },
        "nonpassing_case_examples": {
            "source_field": "testcase_outcomes.nodes",
            "sample": cases[:CASE_EXAMPLE_LIMIT],
            "omitted_from_source_sample": max(0, len(cases) - CASE_EXAMPLE_LIMIT),
            "scope": "diagnostic sample, not the complete test case population",
            "full_execution_rows_field": "testcase_execution_rows",
        },
    }


def write_receipt_view(execute, receipt, *, source_sha256):
    """Publish a regenerable view only after its canonical receipt was stored."""
    view = build_receipt_view(receipt, source_sha256=source_sha256)
    path = f"{VIEW_DIR}/{receipt['receipt_id']}.json"
    body = json.dumps(view, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    result = write_container_text_atomic(execute, path, body, validate_json=True)
    metadata = {"receipt_path": view["source"]["path"], "receipt_sha256": source_sha256}
    if result.persisted:
        metadata.update(
            receipt_view_status="available",
            receipt_view_path=path,
            receipt_view_sha256=hashlib.sha256(body.encode()).hexdigest(),
            receipt_view=view,
        )
    else:
        metadata.update(receipt_view_status="unavailable", receipt_view_error=result.code)
    return metadata


def render_receipt_view(view, path=None):
    """Small facts at the tool boundary; full details remain in the view file."""
    value = view["observed"]
    runtime = value["effective_jdk"] or {}
    toolchain = value["toolchain_fingerprint"] or {}
    return "\n".join(
        [
            (f"Receipt view: {path}" if path else "Receipt observations")
            + " (reading aid; not a task verdict)",
            f"Receipt source: {view['source']['path']} sha256={view['source']['sha256']}",
            f"Invocation: {value['outcome']}; exit_code={value['exit_code']}; "
            f"lifecycle={value['lifecycle_state']}; cwd={value['actual_cwd']}",
            "Command: "
            + (
                value["argv"]
                if len(value["argv"]) <= 512
                else value["argv"][:512] + " [excerpt; full command in view]"
            ),
            f"Runtime observed: JDK={runtime.get('major')}; build_tool={toolchain.get('version')}",
            "Report execution totals: "
            + json.dumps(value["testcase_execution_totals"], sort_keys=True),
            f"Recorded module outcomes: {view['module_outcomes']['known_count']} (null means unavailable)",
        ]
    )


def receipt_observation(receipt):
    """Same projection for a settled job and the next phase, without new IO."""
    source_sha256 = hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
    return render_receipt_view(build_receipt_view(receipt, source_sha256=source_sha256))
