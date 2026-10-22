"""What a run measured, kept apart from what SAG could confirm about it.

A sealed verdict used to say one word for three different questions: did the
project's pinned CI command pass, how much of the official CI run's work did
this run do, and could SAG confirm its own reading of the evidence. The
2026-10-22 analysis of 52 runs found that the word ``partial`` covered six runs
whose CI command passed, five whose command failed and one that never
finished. This module states the answers separately:

* ``ci_command``   — the pinned CI command's result, step by step, and whether it
  ran at the pinned Java/Maven versions on unchanged sources;
* ``official_ci``  — this run's modules and tests beside the official CI cell the
  run was handed, never a ratio when one side is missing;
* ``sag_checks``   — the four things SAG must establish to stand behind its own
  reading, each confirmed or not with one plain sentence;
* ``run_control``  — how the run itself ended.

Every function here is pure over sealed facts: the finalizer gathers them from
the live run, and an offline reader can gather the same facts from a sealed
session directory and get the same answer.
"""

from __future__ import annotations

import posixpath
import shlex
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CICommandOutcome = Literal["passed", "failed", "not_finished", "unavailable"]
YesNo = Literal["yes", "no", "unavailable"]

CHECK_NAMES: tuple[str, ...] = (
    "java requirement decided",
    "built modules confirmed",
    "test outcome graded",
    "test reports tied to the command",
)

OFFICIAL_CI_BASIS = "ci-target cell summary"

_STEP_PASSED = "complete"
_STEP_FAILED = "failed"

_WORD_FOR_RESULT: dict[str, str] = {
    "passed": "success",
    "failed": "failed",
    "not_finished": "partial",
    "unavailable": "unknown",
}

# The prefix `acceptance_task._unattributed_detail` writes before the file list.
_UNATTRIBUTED_PREFIX = (
    "Tracked files differ from the task commit and no required command was "
    "observed changing them: "
)
_SOURCE_CHANGED = "task_source_changed_or_unverified"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FirstUnpassedStep(_Frozen):
    id: str = Field(min_length=1, max_length=64)
    status: Literal["failed", "missing", "out_of_order", "unavailable"]
    exit_code: int | None = Field(default=None, strict=True)


class CICommand(_Frozen):
    """The pinned CI command's result in this run."""

    result: CICommandOutcome
    steps_passed: int = Field(ge=0)
    steps_total: int = Field(ge=0)
    first_unpassed_step: FirstUnpassedStep | None = None
    toolchain_as_pinned: YesNo
    pinned_java: tuple[int, ...] = ()
    pinned_maven: tuple[str, ...] = ()
    toolchain_reason: str | None = None
    source_unchanged: YesNo
    #: "yes": the files the command's own run rewrote, if any. "no": the files
    #: changed by something else. "unavailable": the reason the check did not run.
    source_detail: str | None = None

    @model_validator(mode="after")
    def _consistent(self):
        if self.steps_passed > self.steps_total:
            raise ValueError("more steps passed than the task has")
        all_passed = self.steps_total > 0 and self.steps_passed == self.steps_total
        if (self.result == "passed") != all_passed:
            raise ValueError("a passed command is one whose every step passed")
        if (self.result == "unavailable") != (self.steps_total == 0):
            raise ValueError("an unavailable command result is one with no step results")
        if (self.first_unpassed_step is None) != (self.result in {"passed", "unavailable"}):
            raise ValueError("an unpassed command names its first unpassed step")
        if self.toolchain_as_pinned != "yes" and not self.toolchain_reason:
            raise ValueError("a toolchain that is not as pinned says why")
        return self


class OfficialCI(_Frozen):
    """This run's modules and tests beside the official CI cell it was handed."""

    basis: Literal["ci-target cell summary", "unavailable"]
    reason: str | None = None
    cell: str | None = None
    #: Tests that ran (skipped ones left out), on both sides.
    ci_tests_executed: int | None = Field(default=None, ge=0)
    ci_tests_red: int | None = Field(default=None, ge=0)
    ci_modules: tuple[str, ...] | None = None
    agent_tests_executed: int | None = Field(default=None, ge=0)
    agent_modules_built: tuple[str, ...] | None = None
    agent_failures: int | None = Field(default=None, ge=0)
    agent_errors: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _consistent(self):
        if self.basis == "unavailable":
            if not self.reason:
                raise ValueError("unavailable official CI numbers say why")
            if any(
                value is not None
                for value in (self.cell, self.ci_tests_executed, self.ci_tests_red, self.ci_modules)
            ):
                raise ValueError("unavailable official CI numbers carry no CI side")
        elif not self.cell:
            raise ValueError("official CI numbers name the cell they came from")
        return self


class SagCheck(_Frozen):
    check: Literal[
        "java requirement decided",
        "built modules confirmed",
        "test outcome graded",
        "test reports tied to the command",
    ]
    confirmed: bool
    reason: str | None = None

    @model_validator(mode="after")
    def _reason_when_unconfirmed(self):
        if self.confirmed == bool(self.reason):
            raise ValueError("an unconfirmed check says why, a confirmed one does not")
        return self


class RunControl(_Frozen):
    ended: Literal["normally", "time_limit", "job_control_failure", "aborted"]
    commands_still_running: int = Field(default=0, ge=0)
    detail: str | None = None


def validate_sag_checks(checks: Sequence[SagCheck]) -> None:
    if tuple(check.check for check in checks) != CHECK_NAMES:
        raise ValueError("SAG's checks are exactly four, in their fixed order")


# --- the pinned CI command --------------------------------------------------


def _step_label(status: str) -> str:
    if status == _STEP_PASSED:
        return "passed"
    if status == _STEP_FAILED:
        return "failed"
    # Missing, unavailable or out of order: no completed invocation proves it,
    # and none failed. The step did not finish as the task requires.
    return "not_finished"


def ci_command_result(completion: Any) -> CICommandOutcome:
    """passed / failed / not_finished from the step statuses; unavailable without steps."""

    steps = tuple(getattr(completion, "steps", ()) or ())
    if not steps:
        return "unavailable"
    labels = [_step_label(str(step.status)) for step in steps]
    if all(label == "passed" for label in labels):
        return "passed"
    if "failed" in labels:
        return "failed"
    return "not_finished"


def verdict_for_ci_command(result: str) -> str:
    """The run's word when a pinned task is bound: the command's result, nothing else."""

    return _WORD_FOR_RESULT.get(result, "unknown")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _major(jdk: Mapping[str, Any]) -> str | None:
    if _text(jdk.get("runtime_authority")) != "dispatch_probe":
        return None
    return _text(jdk.get("major")) or None


def _maven_version(receipt: Mapping[str, Any]) -> str | None:
    from sag.tools.internal.maven_versions import parse_maven_version

    fingerprint = receipt.get("toolchain_fingerprint")
    if not isinstance(fingerprint, Mapping) or not fingerprint.get("executable"):
        return None
    return parse_maven_version(fingerprint.get("version"))


def _toolchain(task: Any, completion: Any, receipts: Mapping[str, Mapping[str, Any]]):
    if task is None:
        return "unavailable", (), (), "the pinned Java and Maven versions are not in this record"
    steps = tuple(task.steps)
    pinned_java = tuple(sorted({step.java_major for step in steps if step.java_major}))
    pinned_maven = tuple(sorted({step.maven_version for step in steps if step.maven_version}))
    if not pinned_java and not pinned_maven:
        return "unavailable", (), (), "the task pins no Java or Maven version"
    results = {str(step.id): step for step in getattr(completion, "steps", ()) or ()}
    if not results:
        return "unavailable", pinned_java, pinned_maven, "no step of the task was evaluated"
    mismatch: str | None = None
    unchecked: str | None = None
    for step in steps:
        if not step.java_major and not step.maven_version:
            continue
        what = " and ".join(
            name
            for name, pinned in (("Java", step.java_major), ("Maven", step.maven_version))
            if pinned
        )
        result = results.get(step.id)
        receipt_id = getattr(result, "receipt_id", None)
        if not receipt_id:
            unchecked = unchecked or (
                f"step {step.id} did not finish, so its {what} version was not recorded"
            )
            continue
        receipt = receipts.get(receipt_id)
        if not isinstance(receipt, Mapping):
            unchecked = unchecked or f"the record of step {step.id}'s command could not be read"
            continue
        if step.java_major:
            jdk = receipt.get("effective_jdk")
            major = _major(jdk) if isinstance(jdk, Mapping) else None
            if major is None:
                unchecked = (
                    unchecked or f"the Java version step {step.id} ran with was not recorded"
                )
            elif major != str(step.java_major):
                mismatch = (
                    mismatch or f"step {step.id} ran Java {major}, not Java {step.java_major}"
                )
        if step.maven_version:
            actual = _maven_version(receipt)
            if actual is None:
                unchecked = (
                    unchecked or f"the Maven version step {step.id} ran with was not recorded"
                )
            elif actual != step.maven_version:
                mismatch = mismatch or (
                    f"step {step.id} ran Maven {actual}, not Maven {step.maven_version}"
                )
    if mismatch:
        return "no", pinned_java, pinned_maven, mismatch
    if unchecked:
        return "unavailable", pinned_java, pinned_maven, unchecked
    return "yes", pinned_java, pinned_maven, None


def _sources(completion: Any) -> tuple[str, str | None]:
    steps = tuple(getattr(completion, "steps", ()) or ())
    reasons = tuple(str(reason) for reason in getattr(completion, "reasons", ()) or ())
    if steps or str(getattr(completion, "status", "")) in {"complete", "incomplete"}:
        # Step results exist only past the source check: the tracked tree is
        # the task commit, or every difference was written by a required command.
        changed = tuple(getattr(completion, "source_changes_by_task", ()) or ())
        return "yes", (", ".join(changed) or None)
    for reason in reasons:
        if reason.startswith(_UNATTRIBUTED_PREFIX):
            return "no", reason[len(_UNATTRIBUTED_PREFIX) :].strip() or None
    return "unavailable", (reasons[0] if reasons else _SOURCE_CHANGED)


def ci_command(
    completion: Any,
    *,
    task: Any = None,
    receipts: Mapping[str, Mapping[str, Any]] | None = None,
) -> CICommand | None:
    """The pinned CI command's result, or None when no task was bound."""

    if completion is None:
        return None
    receipts = receipts or {}
    steps = tuple(getattr(completion, "steps", ()) or ())
    first = next((step for step in steps if str(step.status) != _STEP_PASSED), None)
    result = ci_command_result(completion)
    toolchain, pinned_java, pinned_maven, toolchain_reason = _toolchain(task, completion, receipts)
    source, source_detail = _sources(completion)
    return CICommand(
        result=result,
        steps_passed=sum(1 for step in steps if str(step.status) == _STEP_PASSED),
        steps_total=len(steps),
        first_unpassed_step=(
            FirstUnpassedStep(id=first.id, status=first.status, exit_code=first.exit_code)
            if first is not None
            else None
        ),
        toolchain_as_pinned=toolchain,
        pinned_java=pinned_java,
        pinned_maven=pinned_maven,
        toolchain_reason=toolchain_reason,
        source_unchanged=source,
        source_detail=source_detail,
    )


# --- the official CI cell ---------------------------------------------------


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _step_receipts(completion: Any, receipts: Mapping[str, Mapping[str, Any]]):
    found = []
    for step in getattr(completion, "steps", ()) or ():
        receipt = receipts.get(getattr(step, "receipt_id", None) or "")
        if isinstance(receipt, Mapping):
            found.append(receipt)
    return found


def _agent_tests(step_receipts) -> tuple[int | None, int | None, int | None]:
    totals = [
        receipt["testcase_execution_totals"]
        for receipt in step_receipts
        if isinstance(receipt.get("testcase_execution_totals"), Mapping)
    ]
    if not totals:
        return None, None, None
    ran = failures = errors = 0
    for total in totals:
        reported = _count(total.get("reported")) or 0
        skipped = _count(total.get("skipped")) or 0
        ran += max(reported - skipped, 0)
        failures += _count(total.get("failed")) or 0
        errors += _count(total.get("errors")) or 0
    return ran, failures, errors


def _agent_modules(step_receipts, ci_modules: tuple[str, ...] | None) -> tuple[str, ...] | None:
    from sag.metrics.module_keys import module_key

    maven = [receipt for receipt in step_receipts if _text(receipt.get("tool")) == "maven"]
    listed = [receipt for receipt in maven if receipt.get("module_outcomes")]
    if listed:
        built = set()
        for receipt in listed:
            for entry in receipt.get("module_outcomes") or ():
                if (
                    not isinstance(entry, Mapping)
                    or _text(entry.get("status")).lower() != "success"
                ):
                    continue
                try:
                    built.add(module_key(entry.get("module")))
                except ValueError:
                    continue
        return tuple(sorted(built))
    # Maven prints no reactor summary for a one-module build, so a CI cell of
    # exactly the root module is answered by the command's own exit code.
    if maven and ci_modules == (".",):
        return (".",) if any(receipt.get("exit_code") == 0 for receipt in maven) else ()
    return None


def official_ci(
    target: Any,
    *,
    completion: Any,
    receipts: Mapping[str, Mapping[str, Any]] | None = None,
) -> OfficialCI:
    """This run beside the official CI cell. A missing side stays missing."""

    step_receipts = _step_receipts(completion, receipts or {})
    agent_tests, agent_failures, agent_errors = _agent_tests(step_receipts)
    agent = {
        "agent_tests_executed": agent_tests,
        "agent_failures": agent_failures,
        "agent_errors": agent_errors,
    }
    record = getattr(target, "record", None)
    if record is None:
        return OfficialCI(
            basis="unavailable",
            reason="no official CI record was supplied for this run",
            agent_modules_built=_agent_modules(step_receipts, None),
            **agent,
        )
    matched = getattr(record, "matched_cell", None)
    cell = next((cell for cell in record.cells if cell.cell_id == matched), None)
    if cell is None:
        return OfficialCI(
            basis="unavailable",
            reason="none of the official CI jobs in the record was matched to this run",
            agent_modules_built=_agent_modules(step_receipts, None),
            **agent,
        )
    # A cell that states no test count is a cell whose count is unknown, not a
    # CI run that ran nothing: the record cannot tell those apart.
    counted = cell.executed_count > 0
    ci_modules = tuple(cell.modules) or None
    return OfficialCI(
        basis=OFFICIAL_CI_BASIS,
        cell=cell.cell_id,
        ci_tests_executed=max(cell.executed_count - cell.skipped, 0) if counted else None,
        ci_tests_red=cell.red_count if counted else None,
        ci_modules=ci_modules,
        agent_modules_built=_agent_modules(step_receipts, ci_modules),
        **agent,
    )


# --- SAG's own checks -------------------------------------------------------


def _quoted_list(names: Sequence[str]) -> str:
    quoted = [f"'{name}'" for name in names]
    if len(quoted) <= 1:
        return "".join(quoted)
    return f"{', '.join(quoted[:-1])} and {quoted[-1]}"


def _java_gap(unresolved: Sequence[str] | None) -> str:
    if unresolved is None:
        return "the record of what the project requires could not be read"
    activations: list[str] = []
    other: list[str] = []
    for entry in unresolved:
        text = str(entry)
        if ":profile_activation:" in text:
            activations.append(text.rsplit(":profile_activation:", 1)[1] or "unknown")
        elif ":profile_compiler_arguments:" in text:
            other.append(
                f"the {_quoted_list([text.rsplit(':profile_compiler_arguments:', 1)[1]])} "
                "profile's compiler arguments could not be evaluated"
            )
        elif text.endswith(":xml_unreadable"):
            other.append(f"{text[: -len(':xml_unreadable')]} could not be read")
        elif text.endswith(":multiple_values"):
            setting = text[: -len(":multiple_values")].rsplit(":", 1)[-1]
            other.append(f"the '{setting}' setting has more than one value")
        else:
            parts = text.rsplit(":", 2)
            if len(parts) == 3:
                other.append(f"the '{parts[1]}' setting '{parts[2]}' names no Java version")
            else:
                other.append(f"'{text}' could not be evaluated")
    sentences = []
    if len(activations) == 1:
        sentences.append(
            f"the {_quoted_list(activations)} profile's activation could not be evaluated"
        )
    elif activations:
        sentences.append(
            f"the {_quoted_list(activations)} profiles' activations could not be evaluated"
        )
    sentences.extend(other)
    if not sentences:
        return "the installed Java version could not be compared with the project's requirement"
    return "; ".join(sentences)


_MODULES_GAP = (
    "no successful build of the whole project listed its modules, "
    "and the project's own module list could not be read in full"
)
_REPORTS_GAP = (
    "the project directory the test reports belong to could not be worked out "
    "from the commands' records"
)
_UNGRADED = "the run recorded no test outcome that could be graded"


def sag_checks(
    *,
    conflicts: Iterable[str],
    test_judgment: str,
    test_reason: str | None,
    java_unresolved: Sequence[str] | None,
) -> tuple[SagCheck, ...]:
    """The four checks, in order. ``java_unresolved`` None means the record was unreadable."""

    tags = set(str(conflict) for conflict in conflicts)
    java_ok = "build_requirements_unavailable" not in tags
    modules_ok = "maven_reactor_unverified" not in tags
    graded = str(test_judgment) != "unknown"
    tied = "test_primary_coordinate_unresolved" not in tags
    return (
        SagCheck(
            check=CHECK_NAMES[0],
            confirmed=java_ok,
            reason=None if java_ok else _java_gap(java_unresolved),
        ),
        SagCheck(
            check=CHECK_NAMES[1], confirmed=modules_ok, reason=None if modules_ok else _MODULES_GAP
        ),
        SagCheck(
            check=CHECK_NAMES[2],
            confirmed=graded,
            reason=None if graded else (_text(test_reason) or _UNGRADED),
        ),
        SagCheck(check=CHECK_NAMES[3], confirmed=tied, reason=None if tied else _REPORTS_GAP),
    )


# --- how the run ended ------------------------------------------------------

_LIVE_PREFIX = "job_live_at_close:"
_CLEANUP_FAILED = "cleanup_failed"


def _word(value: Any) -> str:
    return _text(getattr(value, "value", value)).lower()


def _field(record: Any, name: str) -> Any:
    if isinstance(record, Mapping):
        return record.get(name)
    return getattr(record, name, None)


def _steps_matched(task: Any, project_root: str | None, obligations: Iterable[Mapping[str, Any]]):
    if task is None or not project_root:
        return 0
    matched = set()
    for step in task.steps:
        tool = "bash" if step.runner == "native" else step.runner
        cwd = posixpath.normpath(posixpath.join(project_root, step.cwd))
        for record in obligations:
            if _text(record.get("tool")) != tool:
                continue
            directory = _text(record.get("working_directory") or record.get("actual_cwd"))
            if not directory or posixpath.normpath(directory) != cwd:
                continue
            try:
                argv = tuple(shlex.split(_text(record.get("argv"))))
            except ValueError:
                continue
            if (argv if tool == "bash" else argv[1:]) == (
                step.argv if tool == "bash" else step.argv[1:]
            ):
                matched.add(step.id)
    return len(matched)


def run_control(
    *,
    conflicts: Iterable[str],
    close_reason: str | None,
    phase_records: Iterable[Any],
    live_job_reasons: Mapping[str, str | None],
    obligations: Mapping[str, Mapping[str, Any]],
    task: Any = None,
    project_root: str | None = None,
) -> RunControl:
    """normally / time_limit / job_control_failure / aborted, and what was still running."""

    tags = tuple(str(conflict) for conflict in conflicts)
    live = [tag[len(_LIVE_PREFIX) :] for tag in tags if tag.startswith(_LIVE_PREFIX)]
    still_running = _steps_matched(
        task,
        project_root,
        [obligations[job] for job in live if isinstance(obligations.get(job), Mapping)],
    )
    cleanup_failed = any(_text(live_job_reasons.get(job)) == _CLEANUP_FAILED for job in live)
    if "job_barrier_integrity_failure" in tags or cleanup_failed:
        return RunControl(ended="job_control_failure", commands_still_running=still_running)
    if live:
        noun = "command was" if len(live) == 1 else "commands were"
        return RunControl(
            ended="time_limit",
            commands_still_running=still_running,
            detail=f"{len(live)} background {noun} still running when the run stopped",
        )
    aborted = next(
        (
            record
            for record in phase_records
            if _word(_field(record, "termination")) == "aborted"
            and _word(_field(record, "outcome")) == "failed"
        ),
        None,
    )
    if aborted is not None:
        reason = _text(_field(aborted, "reason"))
        detail = f"the {_text(_field(aborted, 'phase'))} phase was aborted"
        return RunControl(ended="aborted", detail=f"{detail}: {reason}" if reason else detail)
    if _word(close_reason) == "cancelled":
        return RunControl(ended="aborted", detail="the run was cancelled")
    if _word(close_reason) == "aborted":
        return RunControl(ended="aborted", detail="the run was aborted")
    return RunControl(ended="normally")


__all__ = [
    "CHECK_NAMES",
    "OFFICIAL_CI_BASIS",
    "CICommand",
    "FirstUnpassedStep",
    "OfficialCI",
    "RunControl",
    "SagCheck",
    "ci_command",
    "ci_command_result",
    "official_ci",
    "run_control",
    "sag_checks",
    "validate_sag_checks",
    "verdict_for_ci_command",
]
