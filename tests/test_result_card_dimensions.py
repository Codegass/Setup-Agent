"""The result card states the measured lines first and the word last (Change 3).

Rows, in order: CI command, Official CI, Not confirmed by SAG, Run, Summary,
then Coverage and Report. The terminal block, the report's markdown section and
the Workbench all print the same card.
"""

import pytest
from rich.console import Console

from result_card_fakes import RUN_ID, module_metrics, snapshot_dict
from sag.agent.verdict_finalizer import (
    ReportDeliveryStatus,
    RunTermination,
    RunTerminationStatus,
)
from sag.console.result_block import render_result_block
from sag.result_card.build import build_result_card
from sag.result_card.markdown import render_result_card_markdown
from sag.result_card.models import ROW_LABELS, ROW_ORDER
from sag.web.models import WorkspaceResult
from sag.web.session_registry import _workspace_result

CONFIRMED = [
    {"check": "java requirement decided", "confirmed": True, "reason": None},
    {"check": "built modules confirmed", "confirmed": True, "reason": None},
    {"check": "test outcome graded", "confirmed": True, "reason": None},
    {"check": "test reports tied to the command", "confirmed": True, "reason": None},
]


def _command(**overrides):
    payload = {
        "result": "passed",
        "steps_passed": 1,
        "steps_total": 1,
        "first_unpassed_step": None,
        "toolchain_as_pinned": "yes",
        "pinned_java": [17],
        "pinned_maven": ["3.9.16"],
        "toolchain_reason": None,
        "source_unchanged": "yes",
        "source_detail": None,
    }
    payload.update(overrides)
    return payload


def _official(**overrides):
    payload = {
        "basis": "ci-target cell summary",
        "reason": None,
        "cell": "Build on JDK 21 (jdk)",
        "ci_tests_executed": 933,
        "ci_tests_red": 0,
        "ci_modules": ["."],
        "agent_tests_executed": 933,
        "agent_modules_built": ["."],
        "agent_failures": 0,
        "agent_errors": 0,
    }
    payload.update(overrides)
    return payload


def _task(*steps, status):
    return {
        "run_id": RUN_ID,
        "task_sha256": "a" * 64,
        "status": status,
        "steps": list(steps),
        "reasons": [],
    }


def _step(step_id, status, command="mvn verify", exit_code=None):
    row = {"id": step_id, "command": command, "status": status}
    if status in {"complete", "failed"}:
        row.update(receipt_id=f"inv-maven-1-aaaaaaaaaaaa-000{len(step_id)}", exit_code=exit_code)
    return row


def passed_all_confirmed():
    return snapshot_dict(
        ci_command=_command(),
        official_ci=_official(),
        sag_checks=CONFIRMED,
        run_control={"ended": "normally", "commands_still_running": 0, "detail": None},
    )


def failed_step_two_of_three():
    return snapshot_dict(
        verdict="failed",
        conflicts=["acceptance_task_incomplete", "test_errors_detected"],
        task_completion=_task(
            _step("compile", "complete", "mvn install -DskipTests", 0),
            _step("test", "failed", "mvn test", 1),
            _step("site", "missing", "mvn site"),
            status="incomplete",
        ),
        ci_command=_command(
            result="failed",
            steps_passed=1,
            steps_total=3,
            first_unpassed_step={"id": "test", "status": "failed", "exit_code": 1},
        ),
        official_ci=_official(
            ci_modules=["core", "extras", "docs"],
            agent_modules_built=["core", "extras"],
            ci_tests_executed=2762,
            agent_tests_executed=2679,
            agent_errors=4,
        ),
        sag_checks=[
            CONFIRMED[0],
            {
                "check": "built modules confirmed",
                "confirmed": False,
                "reason": "no successful build of the whole project listed its modules, "
                "and the project's own module list could not be read in full",
            },
            {
                "check": "test outcome graded",
                "confirmed": False,
                "reason": "test execution completion lacks bound task, assessment or outcome evidence",
            },
            CONFIRMED[3],
        ],
        run_control={"ended": "normally", "commands_still_running": 0, "detail": None},
    )


def unfinished_at_the_time_limit():
    return snapshot_dict(
        verdict="partial",
        conflicts=["job_live_at_close:be6ae3c023c2", "acceptance_task_incomplete"],
        task_completion=_task(
            _step("ci-step-1", "complete", "mvn clean install", 0),
            _step("ci-step-2", "missing", "mvn -q -fae test"),
            status="incomplete",
        ),
        ci_command=_command(
            result="not_finished",
            steps_passed=1,
            steps_total=2,
            first_unpassed_step={"id": "ci-step-2", "status": "missing", "exit_code": None},
            toolchain_as_pinned="unavailable",
            pinned_java=[11],
            pinned_maven=[],
            toolchain_reason="step ci-step-2 did not finish, so its Java version was not recorded",
        ),
        official_ci=_official(
            cell="Merge_PR_CI", ci_tests_executed=None, ci_tests_red=None, ci_modules=None
        ),
        sag_checks=[
            *CONFIRMED[:2],
            {
                "check": "test outcome graded",
                "confirmed": False,
                "reason": "no current test receipt",
            },
            CONFIRMED[3],
        ],
        run_control={
            "ended": "time_limit",
            "commands_still_running": 1,
            "detail": "2 background commands were still running when the run stopped",
        },
    )


def no_task():
    payload = snapshot_dict(
        official_ci={
            "basis": "unavailable",
            "reason": "no official CI record was supplied for this run",
        },
        sag_checks=CONFIRMED,
        run_control={"ended": "normally", "commands_still_running": 0, "detail": None},
    )
    payload.pop("task_completion")
    return payload


def _card(payload):
    return build_result_card(
        payload,
        module_metrics=module_metrics(),
        termination=RunTermination(
            termination=RunTerminationStatus.COMPLETED,
            report_delivery_status=ReportDeliveryStatus.DELIVERED,
        ),
        project="commons-cli",
        container="sag-commons-cli",
        turn_count=12,
        tool_calls=20,
        trajectory_session={"wall_clock_seconds": 390.2},
        report_path="/workspace/setup-report.md",
    )


def _console(card, width=78):
    console = Console(width=width, force_terminal=False, no_color=True, soft_wrap=False)
    with console.capture() as capture:
        console.print(render_result_block(card, width=width), markup=True, highlight=False)
    return capture.get()


def _flat(text):
    return " ".join(text.split())


def test_rows_read_in_the_order_the_spec_sets():
    assert ROW_ORDER == (
        "ci_command",
        "official_ci",
        "unconfirmed",
        "run",
        "summary",
        "coverage",
        "report",
    )
    assert [ROW_LABELS[key] for key in ROW_ORDER] == [
        "CI command",
        "Official CI",
        "Not confirmed by SAG",
        "Run",
        "Summary",
        "Coverage",
        "Report",
    ]
    card = _card(passed_all_confirmed())
    assert tuple(row.key for row in card.rows) == ROW_ORDER
    # The card's colour is the derived word's.
    assert card.tone == card.row("summary").tone == "success"


def test_a_passed_command_with_everything_confirmed():
    card = _card(passed_all_confirmed())
    command = card.row("ci_command")
    assert (command.status, command.tone) == ("passed", "success")
    assert command.headline == "1 of 1 steps passed"
    assert command.detail == "Java 17 and Maven 3.9.16 as pinned · sources unchanged"
    assert command.items == ("smoke-build-test: complete — mvn clean verify → exit 0",)
    official = card.row("official_ci")
    assert (official.status, official.tone) == ("compared", "success")
    assert official.headline == (
        "built 1 of 1 modules, ran 933 of 933 tests (CI cell 'Build on JDK 21 (jdk)')"
    )
    assert official.detail == "0 failures, 0 errors in this run · 119 class files · 4 jars"
    unconfirmed = card.row("unconfirmed")
    assert unconfirmed.status == "none"
    assert unconfirmed.headline == "SAG confirmed each of the above from its own records."
    run = card.row("run")
    assert (run.status, run.headline) == ("ended normally", "ended normally")
    assert run.detail == "12 turns · 20 tool calls · 6m 30s"
    summary = card.row("summary")
    assert (summary.status, summary.headline) == ("success", "derived from the lines above")


def test_a_failed_second_step_of_three():
    card = _card(failed_step_two_of_three())
    command = card.row("ci_command")
    assert (command.status, command.tone) == ("failed", "failed")
    assert command.headline == "1 of 3 steps passed · step 2 of 3 exited 1"
    official = card.row("official_ci")
    assert official.headline == (
        "built 2 of 3 modules, ran 2,679 of 2,762 tests (CI cell 'Build on JDK 21 (jdk)')"
    )
    assert official.detail.startswith("0 failures, 4 errors in this run")
    assert official.tone == "attention"
    unconfirmed = card.row("unconfirmed")
    assert unconfirmed.status == "2 of 4"
    assert unconfirmed.items == (
        "which modules the build produced: no successful build of the whole project "
        "listed its modules, and the project's own module list could not be read in full",
        "whether the tests passed: test execution completion lacks bound task, assessment "
        "or outcome evidence",
    )
    assert card.row("summary").status == "failed"


def test_a_command_that_did_not_finish_at_the_time_limit():
    card = _card(unfinished_at_the_time_limit())
    command = card.row("ci_command")
    assert (command.status, command.tone) == ("did not finish", "attention")
    assert command.headline == "1 of 2 steps passed · step 2 of 2 did not finish"
    assert command.detail == (
        "toolchain not checked: step ci-step-2 did not finish, so its Java version was not "
        "recorded · sources unchanged"
    )
    official = card.row("official_ci")
    assert official.status == "not compared"
    assert official.headline == (
        "official CI numbers not available: the CI record for cell 'Merge_PR_CI' states no "
        "module list or test count"
    )
    assert official.detail.startswith("this run: 1/1 modules built · 994 executed · 933 passed")
    run = card.row("run")
    assert run.status == "time limit"
    assert run.headline == "stopped at the time limit with 1 command still running"
    assert run.detail.startswith("2 background commands were still running when the run stopped")
    assert card.row("summary").status == "partial"


def test_a_run_with_no_pinned_task():
    card = _card(no_task())
    command = card.row("ci_command")
    assert (command.status, command.tone) == ("not supplied", "neutral")
    assert command.headline == (
        "no pinned CI command; the build and tests below are what the run did"
    )
    official = card.row("official_ci")
    assert official.headline == (
        "official CI numbers not available: no official CI record was supplied for this run"
    )
    assert official.detail == (
        "this run: 1/1 modules built · 994 executed · 933 passed · 0 failed · 0 errors · "
        "61 skipped · 119 class files · 4 jars"
    )


def test_the_other_endings_of_a_run():
    stopped = snapshot_dict(
        run_control={"ended": "job_control_failure", "commands_still_running": 1, "detail": None}
    )
    assert _card(stopped).row("run").headline == (
        "stopped after a command could not be stopped cleanly"
    )
    aborted = snapshot_dict(
        run_control={
            "ended": "aborted",
            "commands_still_running": 0,
            "detail": "the build phase was aborted: docker went away",
        }
    )
    assert _card(aborted).row("run").headline == (
        "aborted: the build phase was aborted: docker went away"
    )


def test_an_older_record_is_read_through_the_same_rows():
    payload = snapshot_dict(
        schema_version=5, verdict="partial", conflicts=["build_requirements_unavailable"]
    )
    card = _card(payload)
    assert card.verdict_source == "legacy"
    assert tuple(row.key for row in card.rows) == ROW_ORDER
    assert card.row("ci_command").status == "passed"
    assert card.row("official_ci").headline == (
        "official CI numbers not available: this run record does not carry them"
    )
    assert card.row("unconfirmed").status == "1 of 4"
    # The word stays the one the record sealed.
    assert card.row("summary").status == "partial"


@pytest.mark.parametrize(
    "payload",
    [passed_all_confirmed, failed_step_two_of_three, unfinished_at_the_time_limit, no_task],
)
def test_markdown_and_console_say_the_same_lines(payload):
    card = _card(payload())
    console = _flat(_console(card))
    markdown = _flat("\n".join(render_result_card_markdown(card)))
    for row in card.rows:
        for text in (row.label, row.status, row.headline, row.detail, row.reason, *row.items):
            if not text:
                continue
            assert _flat(text) in markdown, (row.key, text)
            if row.key == "ci_command" and text in row.items:
                continue  # long step lines may be cut in the terminal; checked below
            assert _flat(text) in console, (row.key, text)
    for line in _console(card).splitlines():
        assert len(line) <= 78


def test_the_workbench_rail_carries_the_first_line_and_the_summary():
    card = _card(failed_step_two_of_three()).model_dump(mode="json", by_alias=True)
    result = _workspace_result(card, None)
    assert result.verdict == "failed"
    assert result.ci_command.status == "failed"
    assert result.ci_command.headline == "1 of 3 steps passed · step 2 of 3 exited 1"
    assert result.ci.status == "compared"
    wire = result.model_dump(mode="json", by_alias=True)
    assert wire["ciCommand"] == {
        "status": "failed",
        "headline": "1 of 3 steps passed · step 2 of 3 exited 1",
    }
    assert WorkspaceResult.model_validate(wire) == result
