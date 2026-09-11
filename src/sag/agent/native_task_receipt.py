"""Bind a declared native executable run to existing contracts and receipts.

This observes only a foreground, literal invocation of an ELF executable in
the checked-out repository. Scripts, background jobs and opaque shell commands
retain Bash behavior and make no native completion claim.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass

from sag.agent.invocation_contracts import (
    ARGV_EXECUTION_BINDING,
    authorized_facade_envelope_id,
    compliance_class,
    current_action_context,
    freeze_contract,
)
from sag.agent.invocation_receipts import (
    active_receipt_run_id,
    build_receipt,
    next_receipt_id,
    output_content_hash,
    target_sha,
    write_receipt_result,
)


def executable_sha256(execute, cwd, executable, project_root):
    # Inspect bytes without executing the program, including with --version.
    script = """import hashlib, os, pathlib, sys
p = pathlib.Path(sys.argv[1]).resolve(strict=True)
root = pathlib.Path(sys.argv[2]).resolve(strict=True)
assert p.is_relative_to(root) and p.is_file() and os.access(p, os.X_OK)
with p.open('rb') as f:
    assert f.read(4) == b'\\x7fELF'
    f.seek(0)
    print(hashlib.file_digest(f, 'sha256').hexdigest())
"""
    try:
        result = (
            execute(
                shlex.join(["python3", "-c", script, posixpath.join(cwd, executable), project_root])
            )
            or {}
        )
        value = str(result.get("output") or "").strip()
        if result.get("exit_code") == 0 and re.fullmatch(r"[0-9a-f]{64}", value):
            return value
    except Exception:
        pass
    return None


@dataclass(frozen=True)
class NativeTaskDispatch:
    contract: dict
    command: str
    cwd: str
    executable: str
    executable_sha256: str
    project_root: str


def prepare_native_task_receipt(orchestrator, *, command, cwd, environment, timeout):
    """Best-effort evidence preparation; absence never blocks Bash execution."""
    task = getattr(orchestrator, "acceptance_task", None)
    root = getattr(orchestrator, "acceptance_task_root", None)
    if task is None or not root or environment:
        return None
    step = next(
        (
            step
            for step in task.steps
            if step.runner == "native"
            and step.command == command
            and posixpath.normpath(posixpath.join(root, step.cwd)) == cwd
        ),
        None,
    )
    if step is None:
        return None
    scope = current_action_context()
    envelope = authorized_facade_envelope_id(scope)
    params = scope.intent_exact_params
    if envelope is None or not isinstance(params, dict):
        return None
    # Compare the actual call with the engine-owned exact intent. Defaults can
    # be absent in the intent; provided values must agree with the dispatch.
    if (
        set(params) - {"command", "working_directory", "environment", "timeout"}
        or params.get("command") != command
        or params.get("working_directory", "/workspace") != cwd
        or params.get("environment") not in (None, {})
        or int(params.get("timeout", 60)) != timeout
    ):
        return None
    execute = orchestrator.execute_command
    if target_sha(execute, cwd) != task.sha:
        return None
    digest = executable_sha256(execute, cwd, step.argv[0], root)
    if digest is None:
        return None
    contract = freeze_contract(
        execute,
        run_id=active_receipt_run_id(),
        envelope_id=envelope,
        tool="bash",
        params=params,
        effective_tool="bash",
        effective_action="run",
        expected_cwd=cwd,
        expected_argv=shlex.join(step.argv[1:]),
        execution_binding=ARGV_EXECUTION_BINDING,
        intent_source=scope.intent_source,
        intent_id=scope.intent_id,
        intent_domain_id=scope.intent_domain_id,
        intent_exact_params=params,
        action_fingerprint=scope.action_fingerprint,
        trigger_assessment_id=scope.trigger_assessment_id,
        repair_context_id=scope.repair_context_id,
        repair_context_sha256=scope.repair_context_sha256,
    )
    if contract is None:
        return None
    return NativeTaskDispatch(contract, command, cwd, step.argv[0], digest, root)


def finish_native_task_receipt(orchestrator, dispatch, result):
    if dispatch is None:
        return {}
    if type(result.get("exit_code")) is not int or result.get("dispatch_status") not in (
        None,
        "",
        "completed_detached",
    ):
        return {"task_receipt_unavailable": "native_terminal_status_unavailable"}
    try:
        execute = orchestrator.execute_command
        post_sha = target_sha(execute, dispatch.cwd)
        digest = executable_sha256(
            execute, dispatch.cwd, dispatch.executable, dispatch.project_root
        )
        stable = digest is not None and digest == dispatch.executable_sha256
        contract = dispatch.contract
        receipt = build_receipt(
            receipt_id=next_receipt_id("bash", "native"),
            run_id=contract["run_id"],
            tool="bash",
            requested_action="run",
            effective_action="run",
            argv=dispatch.command,
            working_directory=dispatch.cwd,
            exit_code=result["exit_code"],
            before={},
            after={},
            lifecycle_state=result.get("lifecycle_state") or "finished",
            termination_reason=result.get("termination_reason"),
            target_sha=post_sha,
            output_content_hash=output_content_hash(str(result.get("output") or "")),
            contract_id=contract["contract_id"],
            contract_hash=contract["contract_hash"],
            execution_binding=contract["execution_binding"],
            compliance=compliance_class(contract.get("expected_argv"), dispatch.command),
            capability_observations=(
                [
                    {
                        "feature": "native_executable_sha256",
                        "probe": "ELF header and identical executable SHA256 before and after dispatch",
                        "probe_exit_code": "0",
                        "observation": digest,
                    }
                ]
                if stable
                else None
            ),
        )
        persistence = write_receipt_result(execute, receipt)
        if persistence.persisted:
            return {"receipt_id": receipt["receipt_id"], "runner_dispatched": True}
        return {"task_receipt_unavailable": "native_receipt_persistence_failed"}
    except Exception:
        return {"task_receipt_unavailable": "native_receipt_observation_failed"}
