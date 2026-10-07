"""One bounded program over the existing SAG action path.

No project verdict is derived here. The engine owns each child's validation,
execution, physical evidence, loop checks and job lifecycle.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import time
import uuid
from collections import Counter
from typing import Any

from sag.code_runtime.program_rpc import run_program
from sag.tools.base import ToolResult
from sag.tools.output_paging import read_text_page, render_text_page
from sag.utils.container_io import write_container_text_atomic

from .react_types import ReActStep, StepType

FORBIDDEN = frozenset({"code", "phase", "report", "advisor", "manage_context"})
DIRECTORY = "/workspace/.setup_agent/code_programs"
NODE = "/opt/sag-code-mode/node/bin/node"
WORKER = "/opt/sag-code-mode/runtime/worker.mjs"


def child_value(result: ToolResult) -> dict[str, Any]:
    """Expose observed, bounded tool content; never turn absence into zero."""
    metadata = result.metadata
    page = None
    if metadata.get("output_page"):
        page = {key: metadata.get(key) for key in (
            "start_line", "end_line", "column_offset", "max_chars", "complete", "eof", "next"
        )}
    return {
        "text": result.raw_output if (page is not None and "start_line" in metadata
                                      and result.raw_output is not None) else result.output,
        "invocation_status": result.invocation_status.value,
        "operation_outcome": result.operation_outcome.value,
        "error_code": result.error_code,
        "error": result.error,
        "poll_ref": result.poll_ref,
        "output_ref": result.output_ref,
        "output_path": metadata.get("output_path") or metadata.get("path") or metadata.get("source_path"),
        "page": page,
        "receipt_view": metadata.get("receipt_view"),
        "receipt_view_path": metadata.get("receipt_view_path"),
        "facts": result.facts,
    }


def execute_program(engine, program: str) -> ToolResult:
    program_id = "program-" + uuid.uuid4().hex
    path = f"{DIRECTORY}/{program_id}.json"
    selection_path = f"{DIRECTORY}/{program_id}.txt"
    parent = getattr(engine, "_active_action_step", None)
    parent_call_id = getattr(parent, "tool_call_id", None)
    phase = getattr(getattr(engine, "phase_machine", None), "current_phase", None)
    machine_attempt = getattr(getattr(engine, "phase_machine", None), "current_attempt_id", None)
    records: list[dict[str, Any]] = []
    children: list[dict[str, Any]] = []
    started = time.monotonic()
    max_calls = max(0, int(getattr(engine.config, "max_logical_tool_calls", 150))
                    - int(getattr(engine, "_logical_tool_calls", 0)))
    deadline = engine._hold_deadline()
    remaining = min(7200., deadline - time.time()) if deadline is not None else 7200.
    if max_calls <= 0 or remaining <= 0:
        return ToolResult.completed_failure(output="No program was started.", error="Run execution budget exhausted",
            error_code="CODE_BUDGET_EXHAUSTED", metadata={"code_stop_reason": "run execution budget exhausted"})
    contract = {
        "type": "run", "program_id": program_id, "code": program,
        "tools": [{"name": name, "description": tool.description}
                  for name, tool in engine.tools.items() if name not in FORBIDDEN and engine._tool_available(name)],
        "timeout_ms": max(1, int(remaining * 1000)), "cpu_ms": 10000, "max_calls": min(150, max_calls),
    }
    allowed = {item["name"] for item in contract["tools"]}
    initial = {"schema": "sag.code-program.v1", "program_id": program_id,
               "parent_tool_call_id": parent_call_id, "phase": phase, "attempt_id": machine_attempt,
               "contract": contract, "status": "started", "verdict_authority": False}
    persist = getattr(engine, "_code_persist", None) or (
        lambda where, body: write_container_text_atomic(engine.orchestrator, where, body, validate_json=where.endswith('.json'))
    )
    if not persist(path, json.dumps(initial, ensure_ascii=False, indent=2) + "\n").persisted:
        return ToolResult.completed_failure(output="No program was started.", error="Program provenance could not be persisted",
            error_code="CODE_PROVENANCE_FAILED")

    def dispatch(event):
        name, args = event["name"], event["args"]
        if name not in allowed:
            raise ValueError("Child is outside the frozen program tool set")
        call_id = f"{program_id}:child:{event['id']}"
        step = ReActStep(step_type=StepType.ACTION, content=name, tool_name=name,
            tool_params=dict(args), timestamp=engine._get_timestamp(),
            model_used=getattr(parent, "model_used", None), tool_call_id=call_id,
            parent_program_id=program_id)
        engine.steps.append(step)
        reason = engine._execute_action_step(step)
        result = step.tool_result
        if result is None:
            raise RuntimeError("Child has no canonical execution result")
        if reason is None and engine._capture_job_barrier_from_result():
            reason = "controller job barrier active"
        if (phase != getattr(getattr(engine, "phase_machine", None), "current_phase", None)
                or engine._evidence_is_sealed()):
            reason = reason or "phase or evidence scope closed"
        value = child_value(result)
        audit = {"id": event["id"], "tool": name, "tool_call_id": call_id,
                 "envelope_id": step.control_envelope_id, "execution_id": step.control_execution_id,
                 "invocation_status": result.invocation_status.value,
                 "operation_outcome": result.operation_outcome.value, "error_code": result.error_code,
                 "output_ref": result.output_ref, "output_path": result.metadata.get("output_path"),
                 "poll_ref": result.poll_ref,
                 "value_sha256": hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
        children.append(audit)
        return {"status": "pending" if result.invocation_status.value == "pending" else result.operation_outcome.value,
                "value": value, "audit": audit, **({"stop_reason": reason} if reason else {})}

    run = getattr(engine, "_code_run", None)
    engine._active_code_program = program_id
    try:
        if run is None:
            container = engine.orchestrator.client.containers.get(engine.orchestrator.container_name)
            command = ["docker", "exec", "-i", container.id, "env", "-i", "HOME=/root",
                       "PATH=/usr/bin:/bin", NODE, "--max-old-space-size=512", WORKER]
            cli_env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME"} or k.startswith("DOCKER_")}
            run = lambda init, action, record: run_program(command, init, action, record=record, env=cli_env)
        transport = run(contract, dispatch, records.append)
    except BaseException as exc:
        failure = {**initial, "status": "host_interrupted", "children": children,
                   "events": records, "error": f"{type(exc).__name__}: {exc}"}
        persist(path, json.dumps(failure, ensure_ascii=False, indent=2) + "\n")
        raise
    finally:
        engine._active_code_program = None

    terminal = transport.get("terminal") or {}
    result = terminal.get("result") or {}
    stop = terminal.get("stop_reason")
    ok = bool(result.get("ok") and not transport.get("transport_error") and transport.get("exit_code") == 0 and not stop)
    # Actual child results outrank the VM's cancellation status. Requests that
    # never crossed the host dispatcher remain explicitly not executed.
    requested = {r["id"]: r for r in records if r.get("type") == "requested"}
    executed = {row["id"] for row in children}
    unexecuted = [{"id": ident, "tool": row["name"], "status": "not_executed"}
                  for ident, row in requested.items() if ident not in executed]
    unsettled_vm_calls = any(row.get("status") in {"cancelled", "not_executed", "running", "requested"}
                             for row in terminal.get("calls", []))
    if unexecuted or unsettled_vm_calls:
        ok = False
        stop = stop or "program ended with unawaited or unexecuted child calls; actual host results are retained"
    errors = [row for row in children if row["operation_outcome"] != "success"
              or row["invocation_status"] != "completed"]
    if any(row["invocation_status"] == "pending" for row in children):
        stop = stop or "controller job barrier active"
        ok = False
    selection = "\n".join(str(item.get("text", "[unsupported non-text selection]"))
                          for item in result.get("output", []))
    if "value" in result:
        selection += "\nReturn value: " + json.dumps(result["value"], ensure_ascii=False)
    archive = {**initial, "status": "completed" if ok else "failed", "children": children,
               "not_executed": unexecuted, "events": records, "transport": transport,
               "seconds": time.monotonic() - started, "stop_reason": stop}
    body = json.dumps(archive, ensure_ascii=False, indent=2) + "\n"
    sealed = persist(path, body)
    selected = persist(selection_path, selection)
    if not sealed.persisted or not selected.persisted:
        ok = False
        stop = stop or "program archive persistence failed"
    counts = dict(Counter(f"{row['invocation_status']}/{row['operation_outcome']}" for row in children))
    overview = {
        "program_status": "completed" if ok else "failed", "is_build_test_verdict": False,
        "program_id": program_id, "children_executed": len(children), "child_status_counts": counts,
        "not_executed": len(unexecuted), "stop_reason": stop,
        "nonpassing_children": errors[:10], "more_nonpassing_children": max(0, len(errors)-10),
        "program_archive": path if sealed.persisted else None,
        "program_archive_sha256": hashlib.sha256(body.encode()).hexdigest() if sealed.persisted else None,
    }
    page = read_text_page(io.StringIO(selection), max_chars=8000)
    displayed = json.dumps(overview, ensure_ascii=False, indent=2)
    displayed += "\n\nScript-selected text (claims, not a verifier decision):\n"
    displayed += render_text_page(page, {"action": "read", "path": selection_path}) if selected.persisted else "Unavailable: persistence failed."
    error = transport.get("transport_error") or terminal.get("error") or result.get("error") or stop
    return ToolResult.completed(output=displayed, raw_output=body,
        operation_outcome="success" if ok else "failed",
        error=None if ok else str(error or "program did not complete"), error_code=None if ok else "CODE_PROGRAM_FAILED",
        metadata={"output_page": True, "code_program_id": program_id, "code_program_archive": overview["program_archive"],
                  "code_program_sha256": overview["program_archive_sha256"], "code_stop_reason": stop,
                  "code_child_count": len(children), "code_nonpassing_children": len(errors),
                  "code_not_executed": len(unexecuted), "code_selection_path": selection_path if selected.persisted else None})
