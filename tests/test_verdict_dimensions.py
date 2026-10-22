"""The four measured dimensions a sealed verdict carries (spec 2026-10-22, Change 1).

Each dimension is derived from facts the finalizer already holds: the pinned
task's step results, the receipts those steps were closed with, the official CI
record the run was handed, the conflicts SAG recorded, and how the run closed.
"""

import pytest

from sag.agent.acceptance_task import AcceptanceTask, TaskCompletionSnapshot
from sag.agent.ci_comparison import PinnedCITarget
from sag.agent.verdict_dimensions import (
    CHECK_NAMES,
    ci_command,
    ci_command_result,
    official_ci,
    run_control,
    sag_checks,
    verdict_for_ci_command,
)
from sag.metrics.target_record import TargetRecord

RUN = "run-dimensions"


def _task(*steps):
    return AcceptanceTask.model_validate({"repo": "acme/widget", "sha": "a" * 40, "steps": steps})


def _step(step_id, argv=("mvn", "verify"), **pins):
    return {"id": step_id, "runner": "maven", "argv": list(argv), **pins}


def _completion(*steps, status=None, reasons=(), source_changes=()):
    rows = [dict(step) for step in steps]
    if status is None:
        complete = rows and all(row["status"] == "complete" for row in rows)
        status = (
            "complete"
            if complete
            else "incomplete"
            if any(row["status"] in {"failed", "missing", "out_of_order"} for row in rows)
            else "unavailable"
        )
    return TaskCompletionSnapshot(
        run_id=RUN,
        task_sha256="b" * 64,
        status=status,
        steps=tuple(rows),
        reasons=tuple(reasons),
        source_changes_by_task=tuple(source_changes),
    )


def _done(step_id, receipt_id, exit_code=0, status=None, reason=None):
    if status is None:
        status = "complete" if exit_code == 0 else "failed"
    return {
        "id": step_id,
        "command": "mvn verify",
        "status": status,
        "receipt_id": receipt_id,
        "exit_code": exit_code,
        "reason": reason,
    }


def _missing(step_id):
    return {"id": step_id, "command": "mvn test", "status": "missing", "reason": "No retained invocation."}


def _receipt(receipt_id, *, java="17", maven="3.9.16", exit_code=0, modules=None, totals=None):
    receipt = {
        "receipt_id": receipt_id,
        "tool": "maven",
        "exit_code": exit_code,
        "effective_jdk": {"major": java, "runtime_authority": "dispatch_probe"},
        "toolchain_fingerprint": {
            "executable": "/opt/apache-maven/bin/mvn",
            "version": f"Apache Maven {maven} (abc)",
        },
    }
    if modules is not None:
        receipt["module_outcomes"] = [
            {"module": name, "status": status} for name, status in modules.items()
        ]
    if totals is not None:
        receipt["testcase_execution_totals"] = totals
    return receipt


# --- ci_command ------------------------------------------------------------


@pytest.mark.parametrize(
    ("steps", "expected", "word"),
    [
        ([_done("a", "r1"), _done("b", "r2")], "passed", "success"),
        ([_done("a", "r1"), _done("b", "r2", exit_code=1)], "failed", "failed"),
        ([_done("a", "r1"), _missing("b")], "not_finished", "partial"),
        # A failure anywhere outranks a step that never finished.
        ([_done("a", "r1", exit_code=2), _missing("b")], "failed", "failed"),
        # A step that ran but could not be proven is not a passing step.
        (
            [_done("a", "r1", status="unavailable", reason="Invocation contract binding is unavailable.")],
            "not_finished",
            "partial",
        ),
    ],
)
def test_step_statuses_map_to_the_command_result_and_its_word(steps, expected, word):
    completion = _completion(*steps)
    assert ci_command_result(completion) == expected
    assert verdict_for_ci_command(expected) == word


def test_a_task_record_without_steps_is_unavailable_and_reads_unknown():
    completion = _completion(status="unavailable", reasons=("task_run_pin_unavailable",))
    assert ci_command_result(completion) == "unavailable"
    assert verdict_for_ci_command("unavailable") == "unknown"
    dimension = ci_command(completion, task=None, receipts={})
    assert dimension.result == "unavailable"
    assert (dimension.steps_passed, dimension.steps_total) == (0, 0)
    assert dimension.first_unpassed_step is None
    assert dimension.source_unchanged == "unavailable"
    assert dimension.source_detail == "task_run_pin_unavailable"


def test_no_task_has_no_ci_command():
    assert ci_command(None, task=None, receipts={}) is None


def test_passed_command_at_the_pinned_toolchain_with_sources_unchanged():
    task = _task(_step("build", java_major=17, maven_version="3.9.16"))
    dimension = ci_command(
        _completion(_done("build", "r1")), task=task, receipts={"r1": _receipt("r1")}
    )
    assert dimension.result == "passed"
    assert (dimension.steps_passed, dimension.steps_total) == (1, 1)
    assert dimension.first_unpassed_step is None
    assert dimension.toolchain_as_pinned == "yes"
    assert dimension.pinned_java == (17,)
    assert dimension.pinned_maven == ("3.9.16",)
    assert dimension.toolchain_reason is None
    assert dimension.source_unchanged == "yes"
    assert dimension.source_detail is None


def test_first_unpassed_step_names_its_status_and_exit_code():
    task = _task(_step("one"), _step("two", argv=("mvn", "test")), _step("three", argv=("mvn", "install")))
    completion = _completion(_done("one", "r1"), _done("two", "r2", exit_code=1), _missing("three"))
    dimension = ci_command(completion, task=task, receipts={})
    assert dimension.result == "failed"
    assert (dimension.steps_passed, dimension.steps_total) == (1, 3)
    assert dimension.first_unpassed_step.model_dump() == {"id": "two", "status": "failed", "exit_code": 1}


def test_toolchain_not_as_pinned_says_what_ran():
    task = _task(_step("build", java_major=17, maven_version="3.9.16"))
    dimension = ci_command(
        _completion(_done("build", "r1")),
        task=task,
        receipts={"r1": _receipt("r1", java="21")},
    )
    assert dimension.toolchain_as_pinned == "no"
    assert dimension.toolchain_reason == "step build ran Java 21, not Java 17"

    dimension = ci_command(
        _completion(_done("build", "r1")),
        task=task,
        receipts={"r1": _receipt("r1", maven="3.8.1")},
    )
    assert dimension.toolchain_as_pinned == "no"
    assert dimension.toolchain_reason == "step build ran Maven 3.8.1, not Maven 3.9.16"


def test_toolchain_unchecked_when_a_pinned_step_did_not_finish_or_nothing_is_pinned():
    task = _task(_step("one", java_major=11), _step("two", argv=("mvn", "test"), java_major=11))
    dimension = ci_command(
        _completion(_done("one", "r1"), _missing("two")),
        task=task,
        receipts={"r1": _receipt("r1", java="11")},
    )
    assert dimension.toolchain_as_pinned == "unavailable"
    assert dimension.toolchain_reason == "step two did not finish, so its Java version was not recorded"

    unpinned = ci_command(
        _completion(_done("build", "r1")), task=_task(_step("build")), receipts={"r1": _receipt("r1")}
    )
    assert unpinned.toolchain_as_pinned == "unavailable"
    assert unpinned.toolchain_reason == "the task pins no Java or Maven version"


def test_sources_changed_only_by_the_command_still_count_as_unchanged():
    completion = _completion(
        _done("build", "r1"), source_changes=("examples/a.csv", "examples/b.csv")
    )
    dimension = ci_command(completion, task=None, receipts={})
    assert dimension.source_unchanged == "yes"
    assert dimension.source_detail == "examples/a.csv, examples/b.csv"


def test_sources_changed_by_something_else_are_named():
    completion = _completion(
        status="unavailable",
        reasons=(
            "task_source_changed_or_unverified",
            "Tracked files differ from the task commit and no required command was "
            "observed changing them: pom.xml, src/Main.java",
        ),
    )
    dimension = ci_command(completion, task=None, receipts={})
    assert dimension.result == "unavailable"
    assert dimension.source_unchanged == "no"
    assert dimension.source_detail == "pom.xml, src/Main.java"


# --- official_ci -----------------------------------------------------------


def _target(*, matched=True, executed=4906, skipped=22, red=0, modules=("core", "extras")):
    cell = {
        "cell_id": "Build on JDK 21 (jdk)",
        "build": "ok",
        "executed_count": executed,
        "red_count": red,
        "skipped": skipped,
        "modules": list(modules),
        "modules_basis": "log" if modules else None,
        "command": "mvn verify",
        "grade": "B",
    }
    record = TargetRecord.model_validate(
        {
            "repo": "acme/widget",
            "sha": "a" * 40,
            "harvested_at": "2026-10-21T00:00:00Z",
            "cells": [cell],
            "matched_cell": "Build on JDK 21 (jdk)" if matched else None,
        }
    )
    return PinnedCITarget(record=record, raw_sha256="c" * 64)


def test_official_ci_from_a_matched_cell_and_the_pinned_receipts():
    receipts = {
        "r1": _receipt(
            "r1",
            modules={"core": "success", "extras": "success", "docs": "skipped"},
            totals={"reported": 4906, "skipped": 22, "passed": 4884, "failed": 0, "errors": 0},
        )
    }
    dimension = official_ci(_target(), completion=_completion(_done("build", "r1")), receipts=receipts)
    assert dimension.basis == "ci-target cell summary"
    assert dimension.reason is None
    assert dimension.cell == "Build on JDK 21 (jdk)"
    # Both sides count tests that ran, skipped ones left out.
    assert dimension.ci_tests_executed == 4884
    assert dimension.ci_tests_red == 0
    assert dimension.ci_modules == ("core", "extras")
    assert dimension.agent_tests_executed == 4884
    assert dimension.agent_modules_built == ("core", "extras")
    assert (dimension.agent_failures, dimension.agent_errors) == (0, 0)


def test_official_ci_without_a_record_or_a_matched_cell_states_why():
    completion = _completion(_done("build", "r1"))
    receipts = {"r1": _receipt("r1", totals={"reported": 5, "skipped": 0, "passed": 4, "failed": 1, "errors": 0})}
    absent = official_ci(None, completion=completion, receipts=receipts)
    assert absent.basis == "unavailable"
    assert absent.reason == "no official CI record was supplied for this run"
    assert absent.cell is None and absent.ci_tests_executed is None and absent.ci_modules is None
    # This run's own numbers are still stated; only the ratio is refused.
    assert (absent.agent_tests_executed, absent.agent_failures) == (5, 1)

    unmatched = official_ci(_target(matched=False), completion=completion, receipts=receipts)
    assert unmatched.basis == "unavailable"
    assert unmatched.reason == "none of the official CI jobs in the record was matched to this run"


def test_official_ci_with_a_cell_but_no_receipt_totals_leaves_this_runs_side_empty():
    dimension = official_ci(
        _target(), completion=_completion(_done("build", "r1")), receipts={"r1": _receipt("r1")}
    )
    assert dimension.basis == "ci-target cell summary"
    assert dimension.ci_tests_executed == 4884
    assert dimension.agent_tests_executed is None
    assert dimension.agent_modules_built is None
    assert dimension.agent_failures is None and dimension.agent_errors is None

    missing = official_ci(_target(), completion=_completion(_missing("build")), receipts={})
    assert missing.agent_tests_executed is None and missing.agent_modules_built is None


def test_a_cell_with_no_test_count_states_none_rather_than_zero():
    dimension = official_ci(
        _target(executed=0, skipped=0, modules=()),
        completion=_completion(_done("build", "r1")),
        receipts={"r1": _receipt("r1")},
    )
    assert dimension.ci_tests_executed is None
    assert dimension.ci_tests_red is None
    assert dimension.ci_modules is None


def test_a_one_module_build_counts_its_exit_code_as_the_module():
    target = _target(modules=(".",))
    built = official_ci(target, completion=_completion(_done("build", "r1")), receipts={"r1": _receipt("r1")})
    assert built.agent_modules_built == (".",)
    failed = official_ci(
        target,
        completion=_completion(_done("build", "r1", exit_code=1)),
        receipts={"r1": _receipt("r1", exit_code=1)},
    )
    assert failed.agent_modules_built == ()


# --- sag_checks ------------------------------------------------------------


def test_every_check_confirmed_reads_four_confirmed_checks_in_order():
    checks = sag_checks(conflicts=(), test_judgment="success", test_reason=None, java_unresolved=())
    assert tuple(check.check for check in checks) == CHECK_NAMES
    assert CHECK_NAMES == (
        "java requirement decided",
        "built modules confirmed",
        "test outcome graded",
        "test reports tied to the command",
    )
    assert all(check.confirmed and check.reason is None for check in checks)


def test_java_requirement_undecided_names_the_profile_it_could_not_evaluate():
    checks = sag_checks(
        conflicts=("build_requirements_unavailable",),
        test_judgment="success",
        test_reason=None,
        java_unresolved=("/workspace/mug/pom.xml:profile_activation:playground",),
    )
    assert not checks[0].confirmed
    assert checks[0].reason == "the 'playground' profile's activation could not be evaluated"

    two = sag_checks(
        conflicts=("build_requirements_unavailable",),
        test_judgment="success",
        test_reason=None,
        java_unresolved=(
            "/w/pom.xml:profile_activation:java17",
            "/w/pom.xml:profile_activation:jspecify",
        ),
    )
    assert two[0].reason == "the 'java17' and 'jspecify' profiles' activations could not be evaluated"

    unread = sag_checks(
        conflicts=("build_requirements_unavailable",),
        test_judgment="success",
        test_reason=None,
        java_unresolved=None,
    )
    assert unread[0].reason == "the record of what the project requires could not be read"


def test_built_modules_unconfirmed_on_an_unverified_reactor():
    checks = sag_checks(
        conflicts=("maven_reactor_unverified", "build_modules_incomplete"),
        test_judgment="success",
        test_reason=None,
        java_unresolved=(),
    )
    assert [check.confirmed for check in checks] == [True, False, True, True]
    assert checks[1].reason == (
        "no successful build of the whole project recorded its module list, "
        "and the project's own module list could not be read in full"
    )
    # A disk scan that found modules without output is a fact about the build,
    # not a gap in what SAG knows, so on its own it leaves the check confirmed.
    alone = sag_checks(
        conflicts=("build_modules_incomplete",), test_judgment="success", test_reason=None, java_unresolved=()
    )
    assert alone[1].confirmed


def test_ungraded_test_outcome_carries_the_recorded_reason():
    checks = sag_checks(
        conflicts=(),
        test_judgment="unknown",
        test_reason="test execution completion lacks bound task, assessment or outcome evidence",
        java_unresolved=(),
    )
    assert not checks[2].confirmed
    assert checks[2].reason == "test execution completion lacks bound task, assessment or outcome evidence"
    fallback = sag_checks(conflicts=(), test_judgment="unknown", test_reason=None, java_unresolved=())
    assert fallback[2].reason == "the run recorded no test outcome that could be graded"
    graded_red = sag_checks(conflicts=(), test_judgment="failed", test_reason=None, java_unresolved=())
    assert graded_red[2].confirmed


def test_reports_not_tied_to_the_command():
    checks = sag_checks(
        conflicts=("metrics_conflict", "test_primary_coordinate_unresolved"),
        test_judgment="success",
        test_reason=None,
        java_unresolved=(),
    )
    assert [check.confirmed for check in checks] == [True, True, True, False]
    assert checks[3].reason == (
        "the project directory the test reports belong to could not be worked out "
        "from the commands' records"
    )


# --- run_control -----------------------------------------------------------


def _record(phase, termination="completed", outcome="success", reason=""):
    return {"phase": phase, "termination": termination, "outcome": outcome, "reason": reason}


def test_a_run_that_closed_cleanly_ended_normally():
    control = run_control(
        conflicts=(),
        close_reason="test_terminated",
        phase_records=(_record("build"), _record("test")),
        live_job_reasons={},
        obligations={},
        task=None,
        project_root=None,
    )
    assert control.model_dump() == {"ended": "normally", "commands_still_running": 0, "detail": None}


def test_live_jobs_at_the_deadline_stop_at_the_time_limit_counting_pinned_steps():
    task = _task(
        _step("ci-step-1", argv=("mvn", "clean", "install")),
        _step("ci-step-2", argv=("mvn", "-q", "-fae", "test")),
    )
    control = run_control(
        conflicts=("job_live_at_close:job-a", "job_live_at_close:job-b", "acceptance_task_incomplete"),
        close_reason="test_terminated",
        phase_records=(_record("build"),),
        live_job_reasons={"job-a": "deadline", "job-b": "deadline"},
        obligations={
            "job-a": {"tool": "maven", "argv": "/usr/local/bin/mvn -q -fae test", "working_directory": "/workspace/helix"},
            "job-b": {
                "tool": "maven",
                "argv": "/usr/local/bin/mvn --fail-at-end test",
                "working_directory": "/workspace/helix",
            },
        },
        task=task,
        project_root="/workspace/helix",
    )
    assert control.ended == "time_limit"
    assert control.commands_still_running == 1
    assert control.detail == "2 background commands were still running when the run stopped"


def test_a_job_that_could_not_be_stopped_is_a_job_control_failure():
    control = run_control(
        conflicts=("job_live_at_close:job-a", "job_barrier_integrity_failure"),
        close_reason="aborted",
        phase_records=(_record("build", termination="aborted", outcome="failed", reason="stalled"),),
        live_job_reasons={"job-a": "cleanup_failed"},
        obligations={"job-a": {"tool": "maven", "argv": "/opt/mvn verify", "working_directory": "/workspace/w"}},
        task=_task(_step("build")),
        project_root="/workspace/w",
    )
    assert control.ended == "job_control_failure"
    assert control.commands_still_running == 1
    assert control.detail is None


def test_an_aborted_run_says_which_phase_and_why():
    control = run_control(
        conflicts=(),
        close_reason="aborted",
        phase_records=(_record("build", termination="aborted", outcome="failed", reason="docker went away"),),
        live_job_reasons={},
        obligations={},
        task=None,
        project_root=None,
    )
    assert control.ended == "aborted"
    assert control.detail == "the build phase was aborted: docker went away"

    cancelled = run_control(
        conflicts=(),
        close_reason="cancelled",
        phase_records=(),
        live_job_reasons={},
        obligations={},
        task=None,
        project_root=None,
    )
    assert cancelled.ended == "aborted"
    assert cancelled.detail == "the run was cancelled"
