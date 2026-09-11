"""One real facade receipt can prove both lanes of a host-pinned task.

These tests use the strict publication/contract/report fixtures, not a manually
green CertificateView. Real five-project executions are recorded separately.
"""

import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_ci_comparison import ROOT, REPORT, Runner, compare, setup_run
from test_analyze_terminal_exit import _analyze, _deliver

from sag.agent.action_intents import action_fingerprint
from sag.agent.attempt_policy import (
    TestAttemptRequirement as AttemptRequirement,
    TestCandidateResolution as CandidateResolution,
    required_test_attempt,
)
from sag.agent.evidence_state import RunEvidenceState
from sag.agent.invocation_contracts import action_context
from sag.agent.react_engine import ReActEngine
from sag.tools.build.backends import parse_runner_command
from sag.tools.build.build_tool import BuildTool
from sag.tools.bash import BashTool

PROJECTS = json.loads(
    (Path(__file__).resolve().parents[1] / "benchmarks/small-ci-10-v1/manifest.json").read_text()
)["projects"][:5]


def dispatch(run, command, *, suffix="one", **kwargs):
    params = {"command": command, "working_directory": ROOT, **kwargs}
    domain = "test:" + ROOT
    with action_context(
        envelope_id="envelope-fixed-" + suffix,
        intent_source="model",
        intent_id="intent-fixed-" + suffix,
        intent_domain_id=domain,
        intent_exact_params=params,
        action_fingerprint=action_fingerprint(domain_id=domain, tool="build", params=params),
    ):
        runner = Runner(run.fs)
        # Each physical invocation has a distinct ID, even across retries.
        runner.sequence = int(suffix) if suffix.isdigit() else 20
        return BuildTool(run.fs, maven_tool=runner).execute(**params)


def fixed_run(command="mvn -B clean install"):
    run = setup_run(execute_tests=0)
    run.state = RunEvidenceState(run_id=run.state.run_id)
    run.target = type(run.target).model_validate(
        {
            **run.target.model_dump(),
            "execution_command": command,
        }
    )
    return run


def bash_dispatch(run, command, monkeypatch, *, intent_tool="bash", intent_changes=None):
    params = {"command": command, "working_directory": ROOT, "timeout": 300}
    intent = {**params, **(intent_changes or {})}
    domain = "test:" + ROOT
    tool = BashTool(run.fs, build_tool=BuildTool(run.fs, maven_tool=Runner(run.fs)))
    monkeypatch.setattr(tool, "_working_directory_available", lambda _: (True, ""))
    with action_context(
        envelope_id="envelope-bash-fixed",
        intent_source="model",
        intent_id="intent-bash-fixed",
        intent_domain_id=domain,
        intent_exact_params=intent,
        action_fingerprint=action_fingerprint(domain_id=domain, tool=intent_tool, params=intent),
    ):
        return tool.execute(**params)


@pytest.mark.parametrize("project", PROJECTS, ids=lambda p: p["seat"])
def test_public_bash_command_closes_both_lanes_with_its_original_intent(project, monkeypatch):
    command = project["local_command"]
    run = fixed_run(command)
    result = bash_dispatch(run, command, monkeypatch)
    assert result.succeeded, result
    comparison = compare(run)
    assert comparison.attainment.verdict == "met", comparison
    assert comparison.certificate.build_steps.closure_fraction == "1/1"
    assert comparison.certificate.test_steps.closure_fraction == "1/1"
    assert len(comparison.receipt_ids) == 1
    contracts = [
        json.loads(raw) for path, raw in run.fs.files.items() if "/invocation_contracts/" in path
    ]
    contract = contracts[-1]
    assert contract["requested_call"] == {
        "tool": "bash",
        "params": {"command": command, "working_directory": ROOT, "timeout": 300},
    }
    assert contract["effective_tool"] == "maven"
    assert shlex.split(contract["expected_argv"]) == shlex.split(command)[1:]


@pytest.mark.parametrize("change", ["tool", "command", "cwd"])
def test_bash_adapter_cannot_borrow_a_nearby_action_intent(change, monkeypatch):
    run = fixed_run()
    before = {p: raw for p, raw in run.fs.files.items() if "/invocation_receipts/" in p}
    result = bash_dispatch(
        run,
        run.target.execution_command,
        monkeypatch,
        intent_tool="build" if change == "tool" else "bash",
        intent_changes=(
            {"command": "mvn test"}
            if change == "command"
            else ({"working_directory": "/workspace/other"} if change == "cwd" else {})
        ),
    )
    assert not result.succeeded
    assert {p: raw for p, raw in run.fs.files.items() if "/invocation_receipts/" in p} == before


@pytest.mark.parametrize("remove", ["receipt", "assessment", "pin", "report", "contract"])
def test_bash_entrypoint_still_requires_current_evidence(remove, monkeypatch):
    run = fixed_run()
    assert bash_dispatch(run, run.target.execution_command, monkeypatch).succeeded
    assert compare(run).attainment.verdict == "met"
    marker = {
        "receipt": "/invocation_receipts/",
        "assessment": "/evidence_assessments/",
        "pin": "run-pin.json",
        "report": REPORT,
        "contract": "/invocation_contracts/",
    }[remove]
    for path in list(run.fs.files):
        if marker in path:
            del run.fs.files[path]
    comparison = compare(run)
    assert comparison.attainment is None or comparison.attainment.alpha is None


@pytest.mark.parametrize("extra", [" -DskipTests", " -Dtest=OneTest"])
def test_bash_changed_scope_cannot_discharge_ci_target(extra, monkeypatch):
    run = fixed_run()
    bash_dispatch(run, run.target.execution_command + extra, monkeypatch)
    comparison = compare(run)
    assert comparison.attainment is None or comparison.attainment.alpha is None


@pytest.mark.parametrize("project", PROJECTS, ids=lambda p: p["seat"])
def test_five_commands_round_trip_without_skip_injection_and_close_both_lanes(project):
    command = project["local_command"]
    run = fixed_run(command)
    result = dispatch(run, command, maven_version_requirement="[3.9,)")
    assert result.succeeded, result
    comparison = compare(run)
    assert comparison.attainment.verdict == "met", comparison
    assert comparison.certificate.build_steps.closure_fraction == "1/1"
    assert comparison.certificate.test_steps.closure_fraction == "1/1"
    assert comparison.certificate.test_counts.reported == 1
    assert len(comparison.receipt_ids) == 1
    assert shlex.split(comparison.commands[0])[1:] == shlex.split(command)[1:]
    assert comparison.acceptance_command == command


def test_repeat_does_not_multiply_test_counts():
    run = fixed_run()
    for n in range(2):
        assert dispatch(run, run.target.execution_command, suffix=str(n + 30)).succeeded
    result = compare(run)
    assert result.attainment.verdict == "met", result
    assert result.certificate.test_counts.reported == 1
    assert len(result.receipt_ids) == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"system": "maven"},
        {"source_command": "mvn -B clean install"},
        {"system": "maven", "source_command": "mvn  -B clean install"},
    ],
)
def test_identical_runner_declarations_dispatch_once_without_rewriting_intent(extra):
    run = fixed_run()
    result = dispatch(run, run.target.execution_command, **extra)
    assert result.succeeded, result
    comparison = compare(run)
    assert comparison.attainment.verdict == "met"
    assert len(comparison.receipt_ids) == 1
    assert shlex.split(comparison.commands[0])[1:] == ["-B", "clean", "install"]


@pytest.mark.parametrize(
    "extra",
    [
        {"system": "gradle"},
        {"source_command": "mvn clean test"},
        {"source_command": "./mvnw -B clean install"},
        {"source_command": "mvn -B clean install -DskipTests"},
    ],
)
def test_conflicting_redundant_runner_declarations_do_not_dispatch(extra):
    run = fixed_run()
    before = set(run.fs.files)
    result = dispatch(run, run.target.execution_command, **extra)
    assert result.error_code == "BUILD_PARAMETER_INVALID"
    assert not any("/invocation_receipts/" in path for path in set(run.fs.files) - before)


@pytest.mark.parametrize("change", ["skip", "narrow", "goal", "flag"])
def test_changed_command_cannot_discharge_fixed_task(change):
    run = fixed_run()
    command = {
        "skip": "mvn -B clean install -DskipTests",
        "narrow": "mvn -B clean install -Dtest=OneTest",
        "goal": "mvn -B clean test",
        "flag": "mvn clean install",
    }[change]
    dispatch(run, command)
    result = compare(run)
    assert result.attainment is None or result.attainment.alpha is None, result


@pytest.mark.parametrize("remove", ["report", "receipt", "assessment", "pin"])
def test_missing_proof_cannot_be_replaced_by_a_fixed_command(remove):
    run = fixed_run()
    assert dispatch(run, run.target.execution_command).succeeded
    assert compare(run).attainment.verdict == "met"
    marker = {
        "receipt": "/invocation_receipts/",
        "assessment": "/evidence_assessments/",
        "pin": "run-pin.json",
        "report": REPORT,
    }[remove]
    for path in list(run.fs.files):
        if marker in path:
            del run.fs.files[path]
    result = compare(run)
    assert result.attainment is None or result.attainment.alpha is None, result


def test_single_command_still_requires_exact_engine_authority():
    run = fixed_run()
    result = BuildTool(run.fs, maven_tool=Runner(run.fs)).execute(
        command=run.target.execution_command,
        working_directory=ROOT,
    )
    assert not result.succeeded
    assert result.metadata["runner_dispatched"] is False


def test_build_receipt_satisfies_attempt_floor_only_after_strict_test_completion():
    run = fixed_run()
    run.state.set_fact("build.test_entry_ready", True, evidence_ref="validator:build")
    candidate = AttemptRequirement(
        ROOT,
        "maven",
        {
            "tool": "build",
            "params": {"action": "test", "working_directory": ROOT},
        },
    )
    resolution = CandidateResolution(
        "available",
        (candidate,),
        ROOT,
        "/workspace",
        primary=candidate,
    )

    def requirement():
        return required_test_attempt(
            run.state,
            run.fs,
            phase="test",
            attempt_id="test-1",
            resolution=resolution,
            validator=run.validator,
        )

    assert requirement() is not None
    assert dispatch(run, run.target.execution_command).succeeded
    assert requirement() is None
    del run.fs.files[REPORT]
    # An untrusted report cannot be used for final certification, even when
    # the attempt floor correctly remembers that an invocation happened.
    result = compare(run)
    assert result.attainment is None or result.attainment.alpha is None


@pytest.mark.parametrize("command", ["mvn", "mvn -B custom:check"])
def test_unknown_maven_semantics_can_reach_the_runner_without_a_success_claim(command):
    run = fixed_run()
    result = dispatch(run, command)
    assert result.succeeded, result
    result = compare(run)
    assert result.attainment is None or result.attainment.alpha is None


@pytest.mark.parametrize(
    "command", ["mvn test;id", "mvn test && id", "mvn $(id) test", "mvn test\nid"]
)
def test_runner_does_not_interpret_shell(command):
    with pytest.raises(ValueError):
        parse_runner_command(command)


@pytest.mark.parametrize("command", ["mvn -B", "mvn -B custom:check", "./gradlew customCheck"])
def test_unknown_semantics_are_not_invalid_syntax(command):
    system, verb, argv = parse_runner_command(command)
    assert system in {"maven", "gradle"}
    assert argv == shlex.split(command)[1:]
    assert verb == "run"


@pytest.mark.parametrize("plan", [None, {"summary": "incomplete hypothesis"}])
def test_strategy_does_not_gate_attempt_or_create_completion_proof(plan):
    engine, tool, _ = _analyze()
    result = tool.execute(
        action="done", outcome="success", key_results="Use CI command", execution_plan=plan
    )
    assert result.succeeded, result
    assert "execution_plan_candidate" not in result.metadata
    if plan:
        assert result.metadata["plan_warning"]
    _deliver(engine, result)
    assert engine.phase_machine.current_phase == "build"
    assert not engine.run_evidence_state.sealed


def test_short_runs_keep_execution_time_for_controller_owned_jobs():
    engine = ReActEngine.__new__(ReActEngine)
    engine._run_started_at = 100
    engine.config = SimpleNamespace(max_wall_clock_seconds=1200)
    assert engine._hold_deadline() == 1180
    assert engine._report_reserve_seconds() == 120
    # Long runs retain the previous maximum, and the explicit zero-reserve
    # controller harness remains available for its boundary tests.
    assert engine._report_reserve_seconds(7200) == 600
    engine._REPORT_RESERVE_SECONDS = 0
    assert engine._hold_deadline() == 1300
