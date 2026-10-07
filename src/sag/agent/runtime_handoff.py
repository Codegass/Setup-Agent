"""Export already observed per-step runtime paths for an external caller.

This is a delivery artifact, not completion evidence. It installs nothing,
changes no environment, and cannot replace the caller's version probes.
"""
from pathlib import PurePosixPath
import json

from .acceptance_task import build_task_completion, task_maven_version_problem
from sag.runtime.container_io import resolve_control_execute
from sag.utils.container_io import write_container_text_atomic

RUNTIME_HANDOFF_PATH = "/workspace/.setup_agent/runtime-handoff.json"


def observed_step_paths(step, receipt):
    jdk = receipt.get("effective_jdk") or {}
    if step.runner not in {"maven", "gradle"}:
        return {}, "step_has_no_jvm_launcher"
    if jdk.get("runtime_authority") != "dispatch_probe":
        return {}, "dispatch_jvm_unavailable"
    if step.java_major is not None and jdk.get("major") != str(step.java_major):
        return {}, "dispatch_jvm_requirement_mismatch"
    observation = (jdk.get("provenance") or {}).get("dispatch_runtime") or {}
    executable = observation.get("executable")
    if not isinstance(executable, str) or not executable.startswith("/"):
        return {}, "dispatch_java_path_unavailable"
    java = PurePosixPath(executable)
    # /usr/bin/java can be an alternatives shim. Never guess its JDK home.
    if java.name != "java" or java.parent.name != "bin" or str(java.parent.parent) in {"/", "/usr", "/usr/local"}:
        return {}, "dispatch_java_home_unresolved"
    hint = {"java_home": str(java.parent.parent)}
    if step.runner == "maven" and step.argv[0] == "mvn":
        if task_maven_version_problem(step, receipt):
            return {}, "dispatch_maven_requirement_mismatch"
        path = (receipt.get("toolchain_fingerprint") or {}).get("executable")
        if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
            return {}, "dispatch_maven_path_unavailable"
        hint["maven_bin"] = path
    return hint, None


def deliver_runtime_handoff(orchestrator, state, *, validator, task, project_root, repository, output_storage):
    if state is None or state.sealed or validator is None:
        return None
    snapshot = build_task_completion(orchestrator, state, validator=validator,
        project_root=project_root, repository=repository, task=task, output_storage=output_storage)
    if snapshot is None:
        return None
    # Reuse the live publication reader. Container-only registry entries are
    # not a substitute for a receipt tied to this run and pinned command.
    receipts = validator._read_live_invocation_receipts()
    if receipts is None:
        return None
    by_id = {r["receipt_id"]: r for r in receipts if r.get("run_id") == state.run_id and r.get("target_sha") == task.sha}
    steps = {step.id: step for step in task.steps}
    document = {"schema_version": 1, "run_id": state.run_id, "task_sha256": task.sha256,
                "steps": {}, "sources": {}, "unavailable": {},
                "semantics": "Observed runtime path hints only; independently probe versions before execution. No task completion authority."}
    for result in snapshot.steps:
        receipt = by_id.get(result.receipt_id)
        if receipt is None or result.status not in {"complete", "failed"}:
            document["unavailable"][result.id] = "no_current_command_bound_receipt"
            continue
        hint, reason = observed_step_paths(steps[result.id], receipt)
        if reason:
            document["unavailable"][result.id] = reason
        else:
            document["steps"][result.id] = hint
            document["sources"][result.id] = {"receipt_id": result.receipt_id,
                "execution_status": result.status, "exit_code": result.exit_code}
    body = json.dumps(document, sort_keys=True)
    if getattr(orchestrator, "_runtime_handoff_body", None) != body:
        written = write_container_text_atomic(resolve_control_execute(orchestrator),
                                              RUNTIME_HANDOFF_PATH, body, validate_json=True)
        if not written.persisted:
            return None
        orchestrator._runtime_handoff_body = body
    return document
