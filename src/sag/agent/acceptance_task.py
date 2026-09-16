"""Host-pinned ordered task completion, separate from test counts and CI parity.

These are user requirements, not an agent-authored execution plan. They never
grant permission to execute or manufacture a test case. One JVM invocation may
already satisfy both physical build and test evidence.
"""

from __future__ import annotations

import json
import posixpath
import shlex
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from sag.agent.control_events import canonical_sha256

# Snapshot-level reason strings, named for enumeration. Values are unchanged.
TASK_EXECUTION_SCOPE_UNAVAILABLE = "task_execution_scope_unavailable"
TASK_RUN_PIN_UNAVAILABLE = "task_run_pin_unavailable"
TASK_DEFINITION_MISSING_OR_CHANGED = "task_definition_missing_or_changed"
TASK_REPOSITORY_OR_REVISION_MISMATCH = "task_repository_or_revision_mismatch"
TASK_CURRENT_CHECKOUT_MISMATCH = "task_current_checkout_mismatch"
TASK_SOURCE_CHANGED_OR_UNVERIFIED = "task_source_changed_or_unverified"
TASK_RECEIPT_PUBLICATION_UNAVAILABLE = "task_receipt_publication_unavailable"

TASK_REASON_CODES: frozenset[str] = frozenset(
    {
        TASK_EXECUTION_SCOPE_UNAVAILABLE,
        TASK_RUN_PIN_UNAVAILABLE,
        TASK_DEFINITION_MISSING_OR_CHANGED,
        TASK_REPOSITORY_OR_REVISION_MISMATCH,
        TASK_CURRENT_CHECKOUT_MISMATCH,
        TASK_SOURCE_CHANGED_OR_UNVERIFIED,
        TASK_RECEIPT_PUBLICATION_UNAVAILABLE,
    }
)


class AcceptanceStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")

    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    runner: Literal["maven", "gradle", "native", "shell"]
    argv: tuple[str, ...] = Field(min_length=1, max_length=256)
    cwd: str = "."
    # The launcher JVM only. This does not assert the compiler/toolchain vendor.
    java_major: int | None = Field(default=None, strict=True, ge=1, le=99)
    maven_version: str | None = Field(default=None, pattern=r"^\d+\.\d+\.\d+$", max_length=32)

    @model_serializer(mode="wrap")
    def _preserve_unversioned_task_bytes(self, handler):
        data = handler(self)
        if self.maven_version is None:
            data.pop("maven_version", None)
        return data

    @model_validator(mode="after")
    def _bounded_literal_command(self):
        if self.maven_version is not None and self.runner != "maven":
            raise ValueError("only a Maven step can require a Maven version")
        if any("\x00" in part or "\n" in part or "\r" in part for part in self.argv):
            raise ValueError("task argv cannot contain NUL or line breaks")
        if not self.argv[0] or len(self.command.encode()) > 2048:
            raise ValueError("task command is empty or exceeds 2048 bytes")
        if (
            not self.cwd
            or posixpath.isabs(self.cwd)
            or posixpath.normpath(self.cwd) != self.cwd
            or self.cwd == ".."
            or self.cwd.startswith("../")
        ):
            raise ValueError("task cwd must be a canonical repository-relative directory")
        if self.runner == "native":
            executable = self.argv[0]
            if not executable.startswith("./") or ".." in executable.split("/"):
                raise ValueError("native task must name a repository-relative executable")
            if self.java_major is not None:
                raise ValueError("a native executable has no launcher JVM")
        elif self.runner in {"maven", "gradle"}:
            from sag.tools.build.backends import parse_runner_command

            runner, _, _ = parse_runner_command(self.command)
            if runner != self.runner:
                raise ValueError("task runner differs from its literal command")
        elif self.java_major is not None:
            raise ValueError("an opaque shell step cannot assert a launcher JVM")
        return self

    @property
    def command(self) -> str:
        return shlex.join(self.argv)


class AcceptanceTask(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")

    schema_version: Literal[1] = 1
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    steps: tuple[AcceptanceStep, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _unique_steps(self):
        if len({step.id for step in self.steps}) != len(self.steps):
            raise ValueError("task step ids must be unique")
        if len(self.model_dump_json().encode()) > 64 * 1024:
            raise ValueError("task definition exceeds 64 KiB")
        return self

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))

    def prompt(self, project_root: str) -> str:
        lines = [
            "Required task steps (fixed user input; an optional plan cannot remove them):",
            f"Repository: {self.repo}@{self.sha}; task: {self.sha256}",
        ]
        for step in self.steps:
            runtime = f"; launcher Java {step.java_major}" if step.java_major else ""
            if step.maven_version:
                runtime += f"; Apache Maven exactly {step.maven_version}"
            lines.append(
                f"{step.id}: cwd={posixpath.join(project_root, step.cwd)}{runtime}\n"
                f"  {step.command}"
            )
        lines.append(
            "Complete every step in this order. Reuse a completed invocation's build/test "
            "evidence across phases. Missing or failed later steps mean the whole task is "
            "incomplete, even when earlier tests passed. Native steps must run the produced "
            "executable directly through Bash, using the literal command shown above. "
            "Other commands remain available for investigation and repair."
        )
        return "\n".join(lines)


def load_acceptance_task(path: str | Path) -> AcceptanceTask:
    with Path(path).open("rb") as source:
        raw = source.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise ValueError("task file exceeds 64 KiB")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate task field: {key}")
            result[key] = value
        return result

    return AcceptanceTask.model_validate(json.loads(raw, object_pairs_hook=unique))


def pinned_acceptance_task(scope) -> AcceptanceTask | None:
    """Read the task from the existing host-authorized run pin, not model text."""
    if not scope.available:
        raise ValueError(TASK_RUN_PIN_UNAVAILABLE)
    if not scope.acceptance_task_declared:
        return None
    try:
        task = AcceptanceTask.model_validate(scope.acceptance_task_definition)
    except (TypeError, ValueError) as exc:
        raise ValueError(TASK_DEFINITION_MISSING_OR_CHANGED) from exc
    if task.sha256 != scope.acceptance_task_sha256 or task.sha != scope.target_sha:
        raise ValueError(TASK_DEFINITION_MISSING_OR_CHANGED)
    return task


def task_maven_requirement(orchestrator, contract):
    """Resolve a matching fixed step's version independently of agent parameters."""
    from sag.agent.attempt_policy import resolve_current_build_receipt_scope
    from sag.tools.internal.toolchain_manager import ToolVersionRequirement

    root = getattr(orchestrator, "acceptance_task_root", None)
    if not isinstance(root, str):
        return None  # This runner was not configured with a host task.
    if not contract or not contract.get("run_id"):
        raise ValueError("task_dispatch_contract_unavailable")
    scope = resolve_current_build_receipt_scope(
        orchestrator, run_id=contract["run_id"], workspace_root="/workspace", project_root=root
    )
    task = pinned_acceptance_task(scope)
    if task is None:
        return None
    argv = tuple(shlex.split(contract.get("expected_argv") or ""))
    versions = {
        step.maven_version
        for step in task.steps
        if step.runner == "maven"
        and step.maven_version
        and posixpath.normpath(posixpath.join(root, step.cwd)) == contract.get("expected_cwd")
        and step.argv[1:] == argv
    }
    if len(versions) > 1:
        raise ValueError("matching_task_steps_require_different_maven_versions")
    return (
        ToolVersionRequirement.from_raw(versions.pop(), source="acceptance_task")
        if versions
        else None
    )


def task_maven_version_problem(step, receipt) -> str | None:
    from sag.tools.internal.maven_versions import parse_maven_version

    if step.maven_version is None:
        return None
    fingerprint = receipt.get("toolchain_fingerprint") or {}
    actual = parse_maven_version(fingerprint.get("version"))
    if not fingerprint.get("executable") or actual != step.maven_version:
        return f"Required Apache Maven {step.maven_version}; invocation observed {actual or 'unknown'}."
    return None


class TaskStepResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")

    id: str = Field(min_length=1, max_length=64)
    command: str = Field(min_length=1, max_length=2048)
    status: Literal["complete", "failed", "missing", "out_of_order", "unavailable"]
    receipt_id: str | None = None
    exit_code: int | None = Field(default=None, strict=True)
    reason: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def _terminal_witness(self):
        if self.status == "complete" and (
            not self.receipt_id or self.exit_code != 0 or self.reason
        ):
            raise ValueError("completed task step needs a successful terminal receipt")
        if self.status == "failed" and (not self.receipt_id or self.exit_code in (None, 0)):
            raise ValueError("failed task step needs a nonzero terminal receipt")
        if self.status == "missing" and (self.receipt_id is not None or self.exit_code is not None):
            raise ValueError("missing task step cannot claim an invocation")
        return self


class TaskCompletionSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")

    run_id: str
    task_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    status: Literal["complete", "incomplete", "unavailable"]
    steps: tuple[TaskStepResult, ...] = Field(default=(), max_length=32)
    reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _completion_requires_every_step(self):
        complete = bool(self.steps) and all(step.status == "complete" for step in self.steps)
        if self.status == "complete" and (not complete or not self.task_sha256 or self.reasons):
            raise ValueError("task completion requires all bound steps without blockers")
        if complete and self.status != "complete":
            raise ValueError("complete steps contradict task status")
        if len({step.id for step in self.steps}) != len(self.steps):
            raise ValueError("task completion step ids must be unique")
        return self


def build_task_completion(
    orchestrator, state, *, validator, project_root, repository, task, output_storage=None
):
    """Read existing authorized receipts; never run or rewrite a task here."""
    from sag.agent.attempt_policy import resolve_current_build_receipt_scope
    from sag.agent.ci_comparison import repository_identity
    from sag.agent.evidence_assessments import contract_receipt_binding_problem
    from sag.agent.invocation_contracts import read_frozen_contract
    from sag.agent.invocation_receipts import output_content_hash, target_sha
    from sag.agent.job_obligations import read_settled_job_output
    from sag.agent.receipt_structure import dispatch_terminated
    from sag.runtime.container_io import resolve_control_execute
    from sag.tools.base import ToolResult, is_output_storage_ref, canonical_full_output_source

    expected = task.sha256 if task else None

    def unavailable(reason):
        return TaskCompletionSnapshot(
            run_id=state.run_id, task_sha256=expected, status="unavailable", reasons=(reason,)
        )

    if (
        validator is None
        or not project_root
        or not isinstance(getattr(validator, "project_path", None), str)
    ):
        return unavailable(TASK_EXECUTION_SCOPE_UNAVAILABLE) if task else None
    scope = resolve_current_build_receipt_scope(
        orchestrator,
        run_id=state.run_id,
        workspace_root=validator.project_path,
        project_root=project_root,
    )
    if scope.acceptance_task_declared:
        expected = scope.acceptance_task_sha256
    elif task is None:
        return None  # Historical runs did not declare this completion contract.
    if not scope.available:
        return unavailable(TASK_RUN_PIN_UNAVAILABLE)
    if task is None or expected != task.sha256 or not scope.acceptance_task_declared:
        return unavailable(TASK_DEFINITION_MISSING_OR_CHANGED)
    if task.repo != repository_identity(repository) or task.sha != scope.target_sha:
        return unavailable(TASK_REPOSITORY_OR_REVISION_MISMATCH)
    execute = resolve_control_execute(orchestrator)
    if not callable(execute) or target_sha(execute, project_root) != task.sha:
        return unavailable(TASK_CURRENT_CHECKOUT_MISMATCH)
    try:
        unchanged = execute(f"git -C {shlex.quote(project_root)} diff --quiet HEAD --") or {}
    except Exception:
        unchanged = {}
    if type(unchanged.get("exit_code")) is not int or unchanged["exit_code"] != 0:
        return unavailable(TASK_SOURCE_CHANGED_OR_UNVERIFIED)
    receipts = validator._current_scoped_receipts(project_root)
    if receipts is None:
        return unavailable(TASK_RECEIPT_PUBLICATION_UNAVAILABLE)

    results = []
    previous_sequence = -1
    remaining = Counter((step.runner, step.cwd, step.argv) for step in task.steps)
    retained_outputs = {}
    for item in state.tool_observations:
        result = item.result
        if not isinstance(result, ToolResult) or not is_output_storage_ref(result.output_ref):
            continue
        retained_outputs[result.metadata.get("receipt_id")] = result

    def output_available(receipt):
        result = retained_outputs.get(receipt["receipt_id"])
        if result is None or result.invocation_status.value != "completed":
            # A detached invocation closes through its existing job ledger,
            # not a second tool call. Its published receipt still binds bytes.
            return read_settled_job_output(orchestrator, receipt) is not None
        if output_storage is None:
            return False
        try:
            output = canonical_full_output_source(
                raw_output=result.raw_output,
                output=result.output,
                error=result.error,
            )
            return output_storage.retrieve_output(
                result.output_ref
            ) == output and output_content_hash(output) == receipt.get("output_content_hash")
        except Exception:
            return False

    for step in task.steps:
        if step.runner == "shell":
            results.append(
                TaskStepResult(
                    id=step.id,
                    command=step.command,
                    status="unavailable",
                    reason="Opaque shell-step completion is not proven by the current receipt reader. The command remains executable.",
                )
            )
            continue
        cwd = posixpath.normpath(posixpath.join(project_root, step.cwd))
        tool = "bash" if step.runner == "native" else step.runner
        candidates = []
        for receipt in receipts:
            if receipt.get("tool") != tool or receipt.get("actual_cwd") != cwd:
                continue
            try:
                argv = shlex.split(receipt.get("argv") or "")
            except ValueError:
                continue
            if (tuple(argv) if tool == "bash" else tuple(argv[1:])) == (
                step.argv if tool == "bash" else step.argv[1:]
            ):
                candidates.append(receipt)
        key = (step.runner, step.cwd, step.argv)
        required_occurrences = remaining[key]
        remaining[key] -= 1
        if len(candidates) < required_occurrences:
            results.append(
                TaskStepResult(
                    id=step.id,
                    command=step.command,
                    status="missing",
                    reason="No retained invocation matches this command and directory.",
                )
            )
            continue
        # A later failed/unverifiable retry cannot borrow an older green receipt.
        receipt = sorted(candidates, key=validator._receipt_sequence)[-required_occurrences]
        sequence = validator._receipt_sequence(receipt)[0]
        status, reason = "complete", None
        contract = read_frozen_contract(execute, receipt.get("contract_id"))
        if (
            contract_receipt_binding_problem(contract, receipt)
            or receipt.get("compliance") != "exact"
        ):
            status, reason = "unavailable", "Invocation contract binding is unavailable."
        elif not receipt.get("output_content_hash") or not output_available(receipt):
            status, reason = "unavailable", "Invocation output provenance is unavailable."
        elif not dispatch_terminated(receipt) or type(receipt.get("exit_code")) is not int:
            status, reason = "unavailable", "Invocation did not prove normal termination."
        elif receipt["exit_code"] != 0:
            status, reason = "failed", "Required command returned a nonzero exit code."
        elif step.java_major is not None and (
            (receipt.get("effective_jdk") or {}).get("major") != str(step.java_major)
            or (receipt.get("effective_jdk") or {}).get("runtime_authority") != "dispatch_probe"
        ):
            jdk = receipt.get("effective_jdk") or {}
            status = "unavailable"
            if jdk.get("runtime_authority") != "dispatch_probe":
                reason = (
                    f"Required launcher Java {step.java_major}; the receipt has no authorized "
                    f"dispatch JVM probe (recorded major: {jdk.get('major') or 'unknown'})."
                )
            else:
                reason = (
                    f"Required launcher Java {step.java_major}; dispatch observed "
                    f"Java {jdk.get('major') or 'unknown'}."
                )
        elif version_problem := task_maven_version_problem(step, receipt):
            status, reason = "unavailable", version_problem
        elif tool == "bash" and not any(
            item.get("feature") == "native_executable_sha256"
            and item.get("probe_exit_code") == "0"
            and len(item.get("observation", "")) == 64
            for item in receipt.get("capability_observations", ())
        ):
            status, reason = (
                "unavailable",
                "Native executable bytes were not bound before and after execution.",
            )
        elif sequence <= previous_sequence:
            status, reason = "out_of_order", "This invocation preceded an earlier required step."
        previous_sequence = max(previous_sequence, sequence)
        results.append(
            TaskStepResult(
                id=step.id,
                command=step.command,
                status=status,
                receipt_id=receipt["receipt_id"],
                exit_code=receipt.get("exit_code"),
                reason=reason,
            )
        )
    status = (
        "complete"
        if all(item.status == "complete" for item in results)
        else (
            "incomplete"
            if any(item.status in {"failed", "missing", "out_of_order"} for item in results)
            else "unavailable"
        )
    )
    return TaskCompletionSnapshot(
        run_id=state.run_id,
        task_sha256=expected,
        status=status,
        steps=tuple(results),
    )


def render_task_completion_lines(value) -> list[str]:
    if value is None:
        return []
    snapshot = TaskCompletionSnapshot.model_validate(value)
    completed = sum(step.status == "complete" for step in snapshot.steps)
    extent = (
        f"{completed}/{len(snapshot.steps)} steps"
        if snapshot.steps
        else "step definition unavailable"
    )
    lines = [f"Required task: {snapshot.status} ({extent})"]
    lines.extend(f"Task evidence: {reason}" for reason in snapshot.reasons)
    lines.extend(
        f"Task {step.id}: {step.status} — {step.command}"
        + (f"; {step.reason}" if step.reason else "")
        + (f"; exit_code={step.exit_code}" if step.exit_code is not None else "")
        + (f"; receipt={step.receipt_id}" if step.receipt_id else "")
        for step in snapshot.steps
    )
    return lines
