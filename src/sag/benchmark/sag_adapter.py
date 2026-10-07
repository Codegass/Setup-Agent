"""Export SAG's authorized evidence to the common offline requirements format.

The existing verdict remains authoritative for its original protocol. This
adapter writes a separate, versioned analysis; absent observers stay unknown.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import shlex
from pathlib import Path

from .evaluator import evaluate
from .recorder import no_symlinks, reference, write_json
from .requirements import evaluation_identity, validate_requirements


def _published_pin(orchestrator, *, workspace, run_id):
    from sag.agent.control_events import RunPin
    from sag.agent.evidence_publications import RUN_PIN_LOGICAL_ARTIFACT_ID
    from sag.agent.evidence_records import (
        EvidencePublicationBinding, read_live_published_mutable_json_object,
    )

    read = read_live_published_mutable_json_object(
        orchestrator, posixpath.join(workspace, ".setup_agent/run-pin.json"),
        record_kind="run_pin", record_id=RUN_PIN_LOGICAL_ARTIFACT_ID,
        validator=lambda payload, _expected: RunPin.model_validate(payload).model_dump(mode="json"),
        publication_binding=lambda payload: EvidencePublicationBinding(
            run_id=str(payload["run_id"]), logical_artifact_id=RUN_PIN_LOGICAL_ARTIFACT_ID),
        record_scope=lambda payload, current: "current" if payload.get("run_id") == current else "foreign",
    )
    if not read.complete or read.conflict is not None or not read.payload or read.payload.get("run_id") != run_id:
        raise ValueError("Current host-authorized run pin is unavailable")
    return read.payload


def _bound_output(orchestrator, state, receipt, storage):
    from sag.agent.invocation_receipts import output_content_hash
    from sag.agent.job_obligations import read_settled_job_output
    from sag.tools.base import ToolResult, canonical_full_output_source, is_output_storage_ref

    matching = [item.result for item in state.tool_observations
                if isinstance(item.result, ToolResult)
                and item.result.metadata.get("receipt_id") == receipt["receipt_id"]
                and is_output_storage_ref(item.result.output_ref)
                and item.result.invocation_status.value == "completed"]
    if matching and storage is not None:
        value = matching[-1]
        text = canonical_full_output_source(raw_output=value.raw_output, output=value.output, error=value.error)
        if storage.retrieve_output(value.output_ref) == text and output_content_hash(text) == receipt.get("output_content_hash"):
            return text
    text = read_settled_job_output(orchestrator, receipt)
    if text is not None and output_content_hash(text) == receipt.get("output_content_hash"):
        return text
    raise ValueError("Full receipt-bound output is unavailable")


def _command_matches(step, receipt, project_root):
    if receipt.get("actual_cwd") != posixpath.normpath(posixpath.join(project_root, step.cwd)):
        return False
    argv = shlex.split(receipt.get("argv") or "")
    if not argv or tuple(argv[1:]) != step.argv[1:]:
        return False
    if argv[0] == step.argv[0]:
        return True
    fingerprint = receipt.get("toolchain_fingerprint") or {}
    # Dispatch resolves ./mvnw relative to the frozen step directory. Accept
    # only that exact path, also witnessed by the recorded toolchain probe;
    # basename equality must never admit another wrapper or a system Maven.
    if "/" in step.argv[0] and not posixpath.isabs(step.argv[0]):
        resolved = posixpath.normpath(posixpath.join(receipt["actual_cwd"], step.argv[0]))
        return argv[0] == resolved and fingerprint.get("executable") == resolved
    # Resolve a literal PATH launcher only with its recorded executable. A
    # wrapper and a system Maven command are never silently interchangeable.
    return (step.argv[0] in {"mvn", "gradle"} and posixpath.isabs(argv[0])
            and posixpath.basename(argv[0]) == step.argv[0]
            and fingerprint.get("executable") == argv[0])


def _copy_claimed_reports(orchestrator, receipt, spec, step_id, project_root, base, out):
    from sag.runtime.container_io import read_container_text

    reports, errors = [], []
    delta = receipt.get("report_delta") or {}
    for bucket in ("new", "changed"):
        for claim in delta.get(bucket, []):
            try:
                source = claim["path"]
                relative = posixpath.relpath(source, project_root)
                if relative.startswith("../") or posixpath.isabs(relative) or not relative.endswith(".xml"):
                    raise ValueError("Report path is outside the Java task report scope")
                text = read_container_text(orchestrator, source, exact_bytes=True)
                if text is None or hashlib.sha256(text.encode()).hexdigest() != claim["sha256"]:
                    raise ValueError("Report no longer matches receipt bytes")
                owners = set()
                # Parent modules can overlap descendants: require the longest
                # frozen module path rather than assigning by a display name.
                candidates = [row for row in spec["requirements"] if row["step_id"] == step_id and row["kind"] == "test"
                              and isinstance(row.get("scope", {}).get("module_path"), str)
                              and (relative.startswith(row["scope"]["module_path"].rstrip("/") + "/") or row["scope"]["module_path"] == ".")]
                if candidates:
                    def specificity(row):
                        path = row["scope"]["module_path"]
                        return 0 if path == "." else len(path)
                    longest = max(map(specificity, candidates))
                    owners = {row.get("module") for row in candidates if specificity(row) == longest}
                if len(owners) != 1 or None in owners:
                    raise ValueError("Report module has no unique frozen path mapping")
                destination = out / f"report-{len(reports)}.xml"
                destination.write_text(text)
                kind = "integration" if "/failsafe-reports/" in source else "unit" if "/surefire-reports/" in source or "/test-results/test/" in source else None
                reports.append(reference(base, destination, producer_relative_path=relative, module=owners.pop(),
                                         test_kind=kind, fresh=True, freshness_source=f"receipt_delta_{bucket}"))
            except (OSError, KeyError, TypeError, ValueError) as exc:
                errors.append(str(exc))
    if delta.get("cached"):
        errors.append("Cached report lineage is not admitted by requirements-v2")
    return reports, errors


def export_sag_requirements(orchestrator, state, *, validator, project_root, task,
                            completion, spec, protocol, output_storage, session_dir):
    """Archive one final acceptance selection using the same evaluator as portable."""
    from sag.agent.evidence_assessments import contract_receipt_binding_problem
    from sag.agent.invocation_contracts import read_frozen_contract
    from sag.agent.receipt_structure import dispatch_terminated
    from sag.runtime.container_io import resolve_control_execute

    definition = task.model_dump(mode="json")
    validate_requirements(spec, definition)
    expected = evaluation_identity(spec)
    base = no_symlinks(session_dir).resolve()
    if base.is_relative_to(Path(project_root).resolve()):
        raise ValueError("Requirements evidence must be outside the checkout")
    out = base / "requirements-evidence" / hashlib.sha256(state.run_id.encode()).hexdigest()
    no_symlinks(out)
    out.mkdir(parents=True, exist_ok=True)
    if (out / "run.json").exists():
        raise ValueError("Requirements evidence has already been closed")
    errors = []
    run = {"schema_version": 2, "agent": "sag", "producer": "sag_authorized_receipt_adapter",
           "execution_origin": "agent",
           "evidence_base": "session_directory",
           "run_id": state.run_id, "repo": task.repo, "commit": task.sha,
           "evaluation_identity": expected, "invocations": [], "worktree": [],
           "adapter_limitations": ["Historical missing observations are not backfilled.",
                                   "Artifacts and execution configuration require the boundary observer.",
                                   "Receipt report deltas alone do not prove full report collection coverage."]}
    write_json(out / "task.json", definition)
    write_json(out / "requirements.json", spec)
    if getattr(orchestrator, "benchmark_sources", None) is not None:
        run["requirements_sources"] = orchestrator.benchmark_sources
    try:
        if validator is None or not isinstance(getattr(validator, "project_path", None), str):
            raise ValueError("Authorized receipt scope is unavailable")
        pin = _published_pin(orchestrator, workspace=validator.project_path, run_id=state.run_id)
        if pin.get("target_repo_sha") != task.sha or pin["sanitized_config"].get("evaluation_protocol") != protocol:
            raise ValueError("Published evaluation protocol differs from requested analysis")
        if {k: v for k, v in protocol.items() if k != "requirements_file_sha256"} != expected:
            raise ValueError("Requirement definition differs from its frozen protocol")
        if pin["sanitized_config"].get("acceptance_task") != {"sha256": task.sha256, "definition": definition}:
            raise ValueError("Published acceptance task differs")
        if completion is None or completion.run_id != state.run_id or completion.task_sha256 != task.sha256:
            raise ValueError("Final task receipt selection is unavailable")
        receipts = validator._current_scoped_receipts(project_root)
        if receipts is None:
            raise ValueError("Current receipt publication is unavailable")
        write_json(out / "authorized-run-pin.json", pin)
        run["run_pin"] = reference(base, out / "authorized-run-pin.json")
        by_id = {row["receipt_id"]: row for row in receipts}
        if len(by_id) != len(receipts):
            raise ValueError("Duplicate authorized receipt identity")
        selected = {row.id: row for row in completion.steps}
        execute = resolve_control_execute(orchestrator)
        for step in task.steps:
            witness = selected.get(step.id)
            if witness is None or not witness.receipt_id:
                continue
            try:
                receipt = by_id[witness.receipt_id]
                if receipt.get("run_id") != state.run_id or not _command_matches(step, receipt, project_root):
                    raise ValueError("Selected receipt command or run differs")
                contract = read_frozen_contract(execute, receipt.get("contract_id"))
                if contract_receipt_binding_problem(contract, receipt) or receipt.get("compliance") != "exact":
                    raise ValueError("Selected receipt contract is not exact")
                text = _bound_output(orchestrator, state, receipt, output_storage)
                directory = out / step.id
                directory.mkdir(exist_ok=True)
                write_json(directory / "receipt.json", receipt)
                write_json(directory / "contract.json", contract)
                (directory / "output.log").write_text(text)
                receipt_ref = reference(base, directory / "receipt.json")
                reports, report_errors = _copy_claimed_reports(orchestrator, receipt, spec, step.id, project_root, base, directory)
                sequence = validator._receipt_sequence(receipt)[0]
                record = {"run_id": state.run_id, "evaluation_identity": expected, "commit": task.sha,
                          "execution_origin": "agent",
                          "project_root": project_root,
                          "step_id": step.id, "invocation_id": receipt["contract_id"],
                          "receipt_id": receipt["receipt_id"], "sequence": sequence,
                          "runner": step.runner, "argv": list(step.argv), "observed_argv": shlex.split(receipt["argv"]),
                          "cwd": step.cwd, "status": "completed" if dispatch_terminated(receipt) else "unavailable",
                          "exit_code": receipt.get("exit_code"), "log": reference(base, directory / "output.log"),
                          "log_complete": True, "source_receipt": receipt_ref,
                          "source_contract": reference(base, directory / "contract.json"),
                          "runtime": {"source": "sag_host_authorized_receipt", "sag_dispatch_receipt": receipt_ref},
                          "effective_execution": {"inputs_complete": False, "serial": False, "wrapper_reviewed": False},
                          "reports": reports, "reports_collection_complete": False, "artifacts": [],
                          "adapter_errors": report_errors}
                observer = getattr(orchestrator, "requirement_observer", None)
                if observer is not None:
                    observation = observer.export_invocation(receipt["contract_id"], base=base)
                    if observation is None:
                        record["adapter_errors"].append("No boundary observation for selected invocation")
                    elif not isinstance(observation, dict) or observation.get("run_id") != state.run_id or observation.get("contract_id") != receipt["contract_id"]:
                        raise ValueError("Boundary observer identity mismatch")
                    else:
                        if observation.get("receipt_id", receipt["receipt_id"]) != receipt["receipt_id"] or observation.get("evaluation_identity", expected) != expected:
                            raise ValueError("Boundary observer receipt or requirements mismatch")
                        for key in ("effective_execution", "reports", "reports_collection_complete", "artifacts", "artifacts_collection_complete", "artifact_observations", "local_repository", "native_executable", "native_image_probe", "compilation_evidence", "evidence_refs", "preparation_seconds", "observation_seconds"):
                            if key in observation:
                                record[key] = observation[key]
                        record["adapter_errors"].extend(observation.get("errors", []))
                write_json(directory / "invocation.json", record)
                run["invocations"].append(reference(base, directory / "invocation.json"))
            except (OSError, KeyError, TypeError, ValueError) as exc:
                errors.append(f"{step.id}: {exc}")
        recorder = getattr(orchestrator, "worktree_evidence_recorder", None)
        if recorder is not None and recorder.run_id == state.run_id:
            for path in sorted(recorder.directory.glob("*.json")):
                if json.loads(path.read_bytes()).get("run_id") == state.run_id:
                    run["worktree"].append(reference(base, path))
    except (OSError, KeyError, TypeError, ValueError) as exc:
        errors.append(str(exc))
        run["evaluation_identity"] = None
    run["adapter_errors"] = errors
    write_json(out / "run.json", run)
    score = evaluate(definition, spec, run, base, required_execution_origin="agent")
    score["adapter_errors"] = errors
    score["adapter_limitations"] = run["adapter_limitations"]
    write_json(out / "requirement-results.json", score)
    return {"run": reference(base, out / "run.json"),
            "result": reference(base, out / "requirement-results.json"), "status": score["status"]}
