"""The report phase costs a model turn only when the record cannot speak for itself.

Measured on the 10-11 ledgers (14 runs): the report phase was one model request
per run (~6.1k input tokens, 5.5% of the total and 8.8% of the uncached input)
whose text only restated the record and was rendered nowhere a reader sees. In
setup mode the report tool renders the record itself and ignores the model's
summary and details for the markdown.

So a green run — success verdict, the task complete, each of provision, analyze,
build and test closed once as success, no blocker, conflict or repair, and the
evidence closed when the tests ended — gets its report written by the harness,
as a controller action on the record, and the flow closes in the same
iteration. Any departure keeps the model turn, and the brief lists each
departure from the record so the model writes what the record cannot say.
"""

from types import SimpleNamespace

import pytest
from test_native_loop_engine import _PHASE_FACTS, _engine, _phase_turn, _PhaseTool
from test_turn_records import _BillingClient, _events
from test_verdict_finalizer import _RateValidator, _set_rate_test_rollup

from sag.agent.control_events import ControlEventSink
from sag.agent.evidence_state import BlockerRecord, RepairRecord
from sag.agent.loop_memory import LoopMemory
from sag.agent.native_messages import render_messages
from sag.agent.output_storage import OutputStorageManager
from sag.agent.phase_gates import ClaimDisposition, GateResult, ValidatorState
from sag.agent.phase_handoff import PhaseHandoff
from sag.agent.phase_machine import PhaseAttemptRecord, PhaseClaim, PhaseMachine, PhaseOutcome
from sag.agent.react_engine import (
    KICKOFF_PHASE_OBJECTIVES,
    PHASE_OBJECTIVES,
    ReActEngine,
)
from sag.agent.react_llm import NativeToolCall, NativeTurn
from sag.agent.report_departures import (
    DEPARTURE_BLOCK_CAP,
    DEPARTURE_HEADER,
    DEPARTURE_LINE_CAP,
    departure_items,
    render_departure_block,
    report_turn_needed,
)
from sag.agent.token_tracker import TokenTracker
from sag.agent.tool_orchestration import ToolOrchestrator
from sag.agent.verdict_finalizer import ReportDeliveryStatus, RunTerminationStatus
from sag.tools.base import ToolResult
from sag.tools.report_tool import ReportTool
from sag.trajectory.builder import build_trajectory

GREEN_SUMMARY = (
    "Every phase closed as success and the record carries the result; the harness "
    "wrote the report without asking the agent for notes."
)
CUT = "… (cut; full text in the phase handoff)"
LONG_BUILD_NOTES = (
    "Compiled 14 of 15 modules with the registered JDK 17; the benchmark module "
    "needs a JMH annotation processor that the pinned task does not install, so "
    "its classes were not produced. "
) * 6


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def _report_tool(engine, monkeypatch, *, fail_first=False):
    """The real report tool; only its rendering decoration is replaced."""
    tool = ReportTool(docker_orchestrator=engine.orchestrator, workflow_mode="setup")
    monkeypatch.setattr(tool, "_get_project_info", lambda: {})
    monkeypatch.setattr(tool, "_collect_execution_metrics", lambda: {})
    monkeypatch.setattr(tool, "_assemble_report_metrics_artifact", lambda **kw: {})
    monkeypatch.setattr(tool, "_persist_report_metrics", lambda data: True)
    monkeypatch.setattr(tool, "_generate_console_report", lambda *args: "report")
    renders = []

    def markdown(*args):
        renders.append(args)
        if fail_first and len(renders) == 1:
            raise OSError("report store unavailable")
        return "# Result\n"

    monkeypatch.setattr(tool, "_generate_markdown_report", markdown)
    return tool


def _close_turn(index, outcome="success", key_results=""):
    arguments = {"action": "done", "outcome": outcome}
    if key_results:
        arguments["key_results"] = key_results
    return NativeTurn(
        text=f"Closing phase {index}.",
        tool_calls=(
            NativeToolCall(
                id=f"call_{index}",
                name="phase",
                arguments=arguments,
                raw_arguments="{}",
            ),
        ),
        model_used="scripted-model",
    )


def _model_report_turn(index=5):
    return NativeTurn(
        text="Writing the report.",
        tool_calls=(
            NativeToolCall(
                id=f"call_{index}",
                name="report",
                arguments={
                    "action": "generate",
                    "status": "partial",
                    "summary": "The build left one module behind.",
                    "details": "The benchmark module needs a JMH processor; install it next.",
                },
                raw_arguments="{}",
            ),
        ),
        model_used="scripted-model",
    )


def _run_engine(tmp_path, monkeypatch, turns, *, pre_dispatch=True, fail_first=False):
    """The scripted native loop with a real ledger, verdict, report tool and intro.

    Only the report phase uses the real `_phase_intro_step`; earlier phases keep
    the loop fixture's one-line intro because this file is about the report.
    """
    engine = _engine(turns)
    engine.orchestrator = engine.verdict_finalizer.orchestrator
    engine.verdict_finalizer.validator = _RateValidator()
    engine.verdict_finalizer.project_name = "demo"
    _set_rate_test_rollup(
        engine.run_evidence_state,
        passed=100,
        failed=0,
        errors=0,
        driven_modules=["core", "io"],
    )
    engine.control_event_sink = ControlEventSink(
        tmp_path / "control_events.jsonl", run_id=engine.run_evidence_state.run_id
    )
    engine.output_storage = OutputStorageManager(tmp_path / "contexts")
    engine.token_tracker = TokenTracker()
    engine.loop_memory = LoopMemory()
    engine.llm_client = _BillingClient(turns, engine.token_tracker)
    engine.tools["report"] = _report_tool(engine, monkeypatch, fail_first=fail_first)
    stub_intro = engine._phase_intro_step
    engine._phase_intro_step = lambda: (
        ReActEngine._phase_intro_step(engine)
        if engine.phase_machine.current_phase == "report"
        else stub_intro()
    )
    if pre_dispatch:
        # Production wires the intent-minting boundary in front of every tool.
        engine._pending_repair_context = None
        engine._last_invocation_contract_id = None
        orchestrator = ToolOrchestrator(
            tools=engine.tools,
            context_manager=engine.context_manager,
            recent_tool_executions=engine.recent_tool_executions,
            successful_states=engine.successful_states,
            repository_url=engine.repository_url,
            track_tool_execution=lambda *a, **k: None,
            update_successful_states=lambda *a, **k: None,
            add_system_guidance=lambda *a, **k: None,
            get_timestamp=lambda: "2026-10-15T00:00:00Z",
            output_storage=None,
            before_tool_execute=engine._prepare_control_action,
        )
        engine._get_tool_orchestrator = lambda: orchestrator
    return engine


def _green_turns():
    return [_phase_turn(index) for index in range(1, 5)]


def _report_envelopes(engine):
    return [
        row["payload"]
        for row in _events(engine, "action_envelope")
        if row["payload"]["tool"] == "report"
    ]


def _report_request(engine):
    """The text of every user message in the report phase's model request."""
    request = engine.llm_client.requests[4]
    return "\n".join(
        str(message.get("content") or "") for message in request if message["role"] == "user"
    )


# ---------------------------------------------------------------------------
# a green run: the harness writes the report, no model turn
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pre_dispatch", [True, False])
def test_a_green_run_writes_its_report_without_a_model_turn(tmp_path, monkeypatch, pre_dispatch):
    engine = _run_engine(tmp_path, monkeypatch, _green_turns(), pre_dispatch=pre_dispatch)

    result = engine.run_setup_loop("set up the project", max_iterations=12)

    assert result.termination is RunTerminationStatus.COMPLETED
    assert engine.phase_machine.is_complete
    # provision, analyze, build, test — and nothing in report.
    assert len(engine.llm_client.requests) == 4
    assert engine.current_iteration == 4
    assert engine._report_delivery_status() is ReportDeliveryStatus.DELIVERED

    needed, reason = engine._report_turn_needed()
    assert needed is False
    envelopes = _report_envelopes(engine)
    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert envelope["intent_source"] == "controller"
    assert envelope["exact_params"] == {
        "action": "generate",
        "status": "success",
        "summary": GREEN_SUMMARY,
        "details": reason,
    }

    records = [row["payload"] for row in _events(engine, "turn_record")]
    report_turns = [row for row in records if row["envelope_ref"] == envelope["envelope_id"]]
    assert [(row["actor"], row["phase"]) for row in report_turns] == [("controller", "report")]
    assert [row for row in records if row["actor"] == "model" and row["phase"] == "report"] == []
    assert [row["turn_id"] for row in records] == list(range(1, len(records) + 1))

    results = [
        row["payload"]
        for row in _events(engine, "tool_result")
        if row["payload"]["envelope_id"] == envelope["envelope_id"]
    ]
    assert len(results) == 1

    gates = [row["payload"] for row in _events(engine, "gate_decision")]
    assert gates[-1]["phase"] == "report"
    assert gates[-1]["expected_accepted"] is True
    assert gates[-1]["code"] == "report_present"
    assert "report.delivery" in gates[-1]["validated_facts"]
    transitions = [row["payload"] for row in _events(engine, "phase_transition")]
    assert transitions[-1]["expected_kind"] == "flow_close"

    # The derived view reads the report as the harness's turn, with no hole.
    trajectory = build_trajectory(tmp_path)
    harness_reports = [
        turn for turn in trajectory.turns if turn.call and turn.call.tool == "report"
    ]
    assert [(turn.actor, turn.phase) for turn in harness_reports] == [("controller", "report")]
    assert [w for w in trajectory.warnings if w.turn_id == harness_reports[0].turn_id] == []

    # The harness call is answered in the window like any call would be.
    messages = render_messages("SYSTEM PROMPT", engine.steps)
    calls = [call["id"] for message in messages for call in message.get("tool_calls") or ()]
    answers = [message["tool_call_id"] for message in messages if message["role"] == "tool"]
    assert any(call.startswith("harness-report-") for call in calls)
    assert calls == answers


def test_a_floor_close_into_a_green_report_makes_no_model_turn(tmp_path, monkeypatch):
    """The test phase can also be closed by the harness at the loop's top.

    That close lands in the report phase after the loop's first delivery check,
    so the delivered report must be closed there too, before a model request.
    """
    engine = _run_engine(tmp_path, monkeypatch, [_phase_turn(index) for index in range(1, 4)])

    def floor_close():
        machine = engine.phase_machine
        if machine.current_phase != "test":
            return False
        claim = PhaseClaim(
            phase="test",
            signal="done",
            claimed_outcome=PhaseOutcome.SUCCESS,
            key_results="the floor closed the test phase on its receipts",
        )
        gate = GateResult(
            accepted=True,
            validated_outcome=PhaseOutcome.SUCCESS,
            claim_disposition=ClaimDisposition.CONFIRMED,
            validator_state=ValidatorState.GREEN,
            reason="scripted floor close",
            claim=claim,
        )
        engine._seal_engine_gate(claim, gate)
        record = machine.close_attempt(gate)
        decision = engine.transition_policy.decide(
            record, state=engine.run_evidence_state, budgets=engine._repair_budgets()
        )
        engine._apply_phase_decision(record, decision)
        return True

    engine._enforce_phase_floors = floor_close

    result = engine.run_setup_loop("set up the project", max_iterations=12)

    assert result.termination is RunTerminationStatus.COMPLETED
    assert len(engine.llm_client.requests) == 3
    assert [envelope["intent_source"] for envelope in _report_envelopes(engine)] == ["controller"]
    assert engine._report_delivery_status() is ReportDeliveryStatus.DELIVERED


def test_the_green_reason_is_plain_english_and_names_what_was_checked(tmp_path, monkeypatch):
    engine = _run_engine(tmp_path, monkeypatch, _green_turns())
    engine.run_setup_loop("set up the project", max_iterations=12)

    needed, reason = engine._report_turn_needed()

    assert needed is False
    assert reason.startswith("verdict success; task none given;")
    assert "provision, analyze, build and test each closed once as success" in reason
    assert "no blockers, conflicts or repairs" in reason


# ---------------------------------------------------------------------------
# any departure keeps the model turn, and the brief lists the departures
# ---------------------------------------------------------------------------


class _GradingPhaseTool(_PhaseTool):
    """The scripted phase tool, grading a partial close as a partial gate."""

    def execute(self, action, outcome="success", key_results=""):
        if outcome == "success":
            return super().execute(action, outcome, key_results)
        phase = self.machine.current_phase
        claimed = PhaseOutcome(outcome)
        claim = PhaseClaim(
            phase=phase, signal=action, claimed_outcome=claimed, key_results=key_results
        )
        gate = GateResult(
            accepted=True,
            validated_outcome=claimed,
            claim_disposition=ClaimDisposition.CONFIRMED,
            validator_state=ValidatorState.PARTIAL,
            reason="scripted partial gate",
            validated_facts=dict(_PHASE_FACTS.get(phase, {})),
            claim=claim,
        )
        return ToolResult.completed_success(
            output=f"phase {phase} {action} {outcome}",
            metadata={
                "phase_signal": action,
                "phase_claim": claim.to_metadata(),
                "gate_result": gate.to_metadata(),
            },
        )


def _with_partial_build(engine):
    engine.tools["phase"] = _GradingPhaseTool(engine.phase_machine)
    engine.llm_client.turns[2] = _close_turn(3, outcome="partial", key_results=LONG_BUILD_NOTES)


def _with_blocker(engine):
    engine.run_evidence_state.record_blocker(
        "jmh-generator-annprocess:1.37 not resolvable",
        error_code="DEPENDENCY_UNRESOLVED",
        source_phase="build",
    )


def _with_repair(engine):
    engine.run_evidence_state.record_repair(
        SimpleNamespace(
            from_phase="test",
            target_phase="build",
            source_attempt_id="test-1",
            reason_code="missing_build_prerequisite",
            failure_signature="ClassNotFoundException: Generated_Bench",
            hypothesis="the benchmark classes were never generated",
            evidence_refs=("output_test",),
        ),
        state_vector={},
        decision_reason="the test runner needs the benchmark module built first",
    )


def _with_conflict(engine):
    engine.run_evidence_state.record_conflict("coverage_scan_incomplete")


def _with_partial_verdict(engine):
    _set_rate_test_rollup(
        engine.run_evidence_state,
        passed=90,
        failed=0,
        errors=0,
        driven_modules=["core", "io"],
        execution_state="partial",
    )


@pytest.mark.parametrize(
    ("departure", "expected"),
    [
        (
            _with_partial_build,
            "Departure: build attempt {build}: completed [partial] — "
            + " ".join(LONG_BUILD_NOTES.split()),
        ),
        (
            _with_blocker,
            "Departure: blocker blocker_1 in build: DEPENDENCY_UNRESOLVED "
            "jmh-generator-annprocess:1.37 not resolvable [active]",
        ),
        (
            _with_repair,
            "Departure: repair in test: missing_build_prerequisite — "
            "the test runner needs the benchmark module built first",
        ),
        (_with_conflict, "Departure: conflict: coverage_scan_incomplete"),
        (_with_partial_verdict, "Departure: verdict partial; task none given"),
    ],
    ids=["partial_phase", "blocker", "repair", "conflict", "verdict_partial"],
)
def test_a_departure_keeps_the_model_turn_and_the_brief_lists_it(
    tmp_path, monkeypatch, departure, expected
):
    engine = _run_engine(tmp_path, monkeypatch, [*_green_turns(), _model_report_turn()])
    departure(engine)

    result = engine.run_setup_loop("set up the project", max_iterations=12)

    assert result.termination is RunTerminationStatus.COMPLETED
    assert len(engine.llm_client.requests) == 5
    needed, reason = engine._report_turn_needed()
    assert needed is True and reason
    envelopes = _report_envelopes(engine)
    assert [envelope["intent_source"] for envelope in envelopes] == ["model"]
    records = [row["payload"] for row in _events(engine, "turn_record")]
    assert [
        row["actor"] for row in records if row["envelope_ref"] == envelopes[0]["envelope_id"]
    ] == ["model"]

    brief = _report_request(engine)
    lines = brief.splitlines()
    build = next(
        record.attempt_id for record in engine.phase_machine.records if record.phase == "build"
    )
    assert expected.format(build=build) in lines
    header = lines.index(DEPARTURE_HEADER)
    assert lines[header - 1] == f"Objective: {PHASE_OBJECTIVES['report']}"
    departures = []
    for line in lines[header + 1 :]:
        if not line.startswith("Departure: "):
            break
        departures.append(line)
    assert departures[-1].startswith("Departure: verdict ")
    assert departures[-1].endswith("; task none given")
    assert all(len(line) <= DEPARTURE_LINE_CAP for line in departures)


def test_a_harness_report_that_raises_falls_back_to_the_model_turn(tmp_path, monkeypatch):
    engine = _run_engine(tmp_path, monkeypatch, [*_green_turns(), _model_report_turn()])
    execute = engine._execute_tool_call
    raised = []

    def flaky(call):
        if call.name == "report" and not raised:
            raised.append(call.action_intent.source)
            raise RuntimeError("report dispatch lost")
        return execute(call)

    engine._execute_tool_call = flaky

    result = engine.run_setup_loop("set up the project", max_iterations=12)

    assert raised == ["controller"]
    assert result.termination is RunTerminationStatus.COMPLETED
    assert len(engine.llm_client.requests) == 5
    # The state was left as it was: the model saw the brief and nothing else.
    request = engine.llm_client.requests[4]
    assert [message["role"] for message in request] == ["system", "user"]
    assert DEPARTURE_HEADER in request[1]["content"]
    assert [envelope["intent_source"] for envelope in _report_envelopes(engine)] == ["model"]
    assert engine._report_delivery_status() is ReportDeliveryStatus.DELIVERED


def test_a_harness_report_that_fails_is_on_record_and_the_model_writes_it(tmp_path, monkeypatch):
    engine = _run_engine(
        tmp_path, monkeypatch, [*_green_turns(), _model_report_turn()], fail_first=True
    )

    result = engine.run_setup_loop("set up the project", max_iterations=12)

    assert result.termination is RunTerminationStatus.COMPLETED
    assert len(engine.llm_client.requests) == 5
    envelopes = _report_envelopes(engine)
    assert [envelope["intent_source"] for envelope in envelopes] == ["controller", "model"]
    records = [row["payload"] for row in _events(engine, "turn_record")]
    actors = {row["envelope_ref"]: row["actor"] for row in records if row["envelope_ref"]}
    assert [actors[envelope["envelope_id"]] for envelope in envelopes] == ["controller", "model"]
    # The model read the harness's failed call, answered, beside the brief.
    request = engine.llm_client.requests[4]
    harness_calls = [
        call["id"]
        for message in request
        for call in message.get("tool_calls") or ()
        if call["id"].startswith("harness-report-")
    ]
    answered = [message["tool_call_id"] for message in request if message["role"] == "tool"]
    assert len(harness_calls) == 1 and harness_calls == answered
    assert engine._report_delivery_status() is ReportDeliveryStatus.DELIVERED


def test_no_report_tool_means_no_harness_report(tmp_path, monkeypatch):
    engine = _run_engine(tmp_path, monkeypatch, [*_green_turns(), _model_report_turn()])
    del engine.tools["report"]
    engine.llm_client.turns[-1] = _phase_turn(5)

    result = engine.run_setup_loop("set up the project", max_iterations=12)

    assert result.termination is RunTerminationStatus.COMPLETED
    assert len(engine.llm_client.requests) == 5
    assert _report_envelopes(engine) == []


# ---------------------------------------------------------------------------
# the green predicate, on the record alone
# ---------------------------------------------------------------------------


def _record(phase, outcome="success", termination="completed", attempt=1, **extra):
    return PhaseAttemptRecord(
        phase=phase,
        attempt_id=f"{phase}-{attempt}",
        termination=termination,
        outcome=outcome,
        **extra,
    )


def _clean_records():
    return [_record(phase) for phase in ("provision", "analyze", "build", "test")]


def _predicate(**overrides):
    values = {
        "verdict": "success",
        "task_status": "complete",
        "records": _clean_records(),
        "blockers": (),
        "conflicts": (),
        "repairs": (),
        "close_reason": "test_terminated",
    }
    values.update(overrides)
    return report_turn_needed(**values)


def _blocker(status="resolved"):
    return BlockerRecord(
        blocker_id="blocker_1",
        category="runtime",
        error_code="BLOCKED",
        failure_signature="sig",
        status=status,
        source_phase="build",
    )


def _repair():
    return RepairRecord(
        repair_id="repair_1",
        from_phase="test",
        target_phase="build",
        source_attempt_id="test-1",
        reason_code="missing_build_prerequisite",
        failure_signature="sig",
        hypothesis="h",
        accepted=True,
        decision_reason="the tests need the module built first",
    )


def test_a_clean_record_needs_no_model_turn():
    needed, reason = _predicate()
    assert needed is False
    assert reason.startswith("verdict success; task complete;")


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"verdict": "partial"}, "verdict partial"),
        ({"task_status": "incomplete"}, "task incomplete"),
        ({"records": _clean_records()[:3]}, "test closed 0 times"),
        ({"records": [*_clean_records(), _record("build", attempt=2)]}, "build closed 2 times"),
        (
            {"records": [*_clean_records()[:2], _record("build", "partial"), _clean_records()[3]]},
            "build closed completed [partial]",
        ),
        (
            {"records": [*_clean_records()[:3], _record("test", "failed", "blocked")]},
            "test closed blocked [failed]",
        ),
        ({"blockers": (_blocker("resolved"),)}, "1 blocker on record"),
        ({"blockers": (_blocker("active"), _blocker("active"))}, "2 blockers on record"),
        ({"conflicts": ("coverage_scan_incomplete",)}, "1 conflict on record"),
        ({"repairs": (_repair(),)}, "1 repair on record"),
        ({"close_reason": "dependents_skipped"}, "evidence closed for dependents_skipped"),
    ],
)
def test_any_departure_needs_the_model_turn(overrides, fragment):
    needed, reason = _predicate(**overrides)
    assert needed is True
    assert fragment in reason


def test_a_task_that_was_never_given_is_not_a_departure():
    needed, reason = _predicate(task_status=None)
    assert needed is False
    assert reason.startswith("verdict success; task none given;")


# The 2026-10-15 httpcomponents-client run: the agent closed build as partial
# because the first Maven run failed on a timing-sensitive test, the gate graded
# the phase success on compilation evidence, and the harness wrote the report
# without the agent's notes. The agent's own word is a departure.
BUILD_FAILURE_NOTES = (
    "Compiled every module; Terminal result: BUILD FAILURE on a timing-sensitive test"
)


def _claimed(phase, claimed, key_results=""):
    """A gate-graded success over the agent's own claim, as `PhaseMachine` records it."""
    return _record(
        phase,
        key_results=key_results,
        claim=PhaseClaim(phase=phase, claimed_outcome=claimed, key_results=key_results),
    )


def _with_build(record):
    clean = _clean_records()
    return [*clean[:2], record, clean[3]]


def test_a_phase_the_agent_did_not_call_a_success_needs_the_model_turn():
    needed, reason = _predicate(
        records=_with_build(_claimed("build", "partial", BUILD_FAILURE_NOTES))
    )
    assert needed is True
    assert "build closed as success but the agent claimed partial" in reason


def test_the_departure_lines_name_what_the_agent_claimed_with_its_notes():
    items = departure_items(
        records=_with_build(_claimed("build", "partial", BUILD_FAILURE_NOTES)),
        blockers=(),
        conflicts=(),
        repairs=(),
    )
    assert items == [
        "Departure: build attempt build-1: completed [success], the agent claimed partial "
        f"— {BUILD_FAILURE_NOTES}"
    ]


def test_a_claimed_departure_with_no_notes_says_so():
    items = departure_items(
        records=[_claimed("build", "failed")], blockers=(), conflicts=(), repairs=()
    )
    assert items == [
        "Departure: build attempt build-1: completed [success], the agent claimed failed "
        "— no notes recorded"
    ]


def test_a_success_the_agent_also_claimed_stays_clean():
    records = _with_build(_claimed("build", "success", "every module compiled"))
    needed, reason = _predicate(records=records)
    assert needed is False
    assert reason.startswith("verdict success; task complete;")
    assert departure_items(records=records, blockers=(), conflicts=(), repairs=()) == []


def test_a_record_with_no_claim_is_judged_on_its_close_alone():
    records = _clean_records()
    assert all(record.claim is None for record in records)
    needed, _ = _predicate(records=records)
    assert needed is False
    assert departure_items(records=records, blockers=(), conflicts=(), repairs=()) == []


def test_a_graded_departure_is_not_reported_twice_for_its_claim():
    record = _record(
        "build",
        "partial",
        key_results="14 of 15 modules",
        claim=PhaseClaim(phase="build", claimed_outcome="partial", key_results="14 of 15 modules"),
    )
    needed, reason = _predicate(records=_with_build(record))
    assert needed is True
    assert "build closed completed [partial]" in reason
    assert "the agent claimed" not in reason
    assert departure_items(records=[record], blockers=(), conflicts=(), repairs=()) == [
        "Departure: build attempt build-1: completed [partial] — 14 of 15 modules"
    ]


def test_the_engine_predicate_needs_a_current_verdict(tmp_path, monkeypatch):
    engine = _run_engine(tmp_path, monkeypatch, _green_turns())
    engine.phase_machine = PhaseMachine(start_phase="report")
    # Nothing sealed: the verdict cannot be read, so the model turn is needed.
    needed, reason = engine._report_turn_needed()
    assert needed is True
    assert "verdict" in reason


# ---------------------------------------------------------------------------
# the departure block: one line per item, capped, the verdict line always last
# ---------------------------------------------------------------------------


def test_the_departure_block_lists_every_item_in_record_order():
    lines = render_departure_block(
        records=[
            *_clean_records()[:2],
            _record("build", "partial", key_results="14 of 15 modules"),
            _record("test", "failed", "blocked", reason="the runner never started"),
        ],
        blockers=(_blocker("active"),),
        conflicts=("coverage_scan_incomplete",),
        repairs=(_repair(),),
        verdict="partial",
        task_status="incomplete",
    )
    assert lines == [
        DEPARTURE_HEADER,
        "Departure: build attempt build-1: completed [partial] — 14 of 15 modules",
        "Departure: test attempt test-1: blocked [failed] — the runner never started",
        "Departure: blocker blocker_1 in build: BLOCKED sig [active]",
        "Departure: conflict: coverage_scan_incomplete",
        "Departure: repair in test: missing_build_prerequisite — the tests need the module built first",
        "Departure: verdict partial; task incomplete",
    ]


def test_a_long_departure_line_is_cut_and_says_so():
    notes = "word " * 600
    lines = render_departure_block(
        records=[_record("build", "partial", key_results=notes)],
        blockers=(),
        conflicts=(),
        repairs=(),
        verdict="partial",
        task_status=None,
    )
    build = lines[1]
    assert len(build) <= DEPARTURE_LINE_CAP
    assert build.endswith(CUT)
    assert build.startswith("Departure: build attempt build-1: completed [partial] — word word")
    assert lines[-1] == "Departure: verdict partial; task none given"


def test_the_block_is_capped_and_says_how_many_items_it_left_out():
    records = [
        _record("build", "partial", attempt=index, key_results=f"attempt {index} " + "x" * 900)
        for index in range(1, 8)
    ]
    lines = render_departure_block(
        records=records,
        blockers=(),
        conflicts=(),
        repairs=(),
        verdict="partial",
        task_status="incomplete",
    )
    assert len("\n".join(lines)) <= DEPARTURE_BLOCK_CAP
    assert lines[0] == DEPARTURE_HEADER
    assert all(line.startswith("Departure: ") for line in lines[1:])
    assert lines[-1] == "Departure: verdict partial; task incomplete"
    assert "more not shown (full text in the phase handoff)" in lines[-2]
    assert lines[1].startswith("Departure: build attempt build-1:")


def test_departure_text_is_one_line_per_item():
    lines = render_departure_block(
        records=[_record("build", "partial", key_results="first line\nsecond line\n\nthird")],
        blockers=(),
        conflicts=(),
        repairs=(),
        verdict="partial",
        task_status=None,
    )
    assert (
        lines[1]
        == "Departure: build attempt build-1: completed [partial] — first line second line third"
    )


# ---------------------------------------------------------------------------
# the report brief and its objective
# ---------------------------------------------------------------------------


def test_the_report_objective_asks_only_for_what_the_record_cannot_say():
    objective = PHASE_OBJECTIVES["report"]
    assert objective == (
        "Write what the record cannot say. This run departed from a clean path; the "
        "departures are listed below. Call report(action='generate', summary=<one "
        "sentence>, details=<notes>) where details say where the run got stuck, why, and "
        "what a person needs to do next. Do not repeat commands, versions, exit codes or "
        "test counts; the record already carries them. Completion requires a durable "
        "report artifact reference; report delivery status does not rewrite the verdict."
    )
    # The kickoff plan is written before the run has a record, so its report
    # task names no departures and selects no call.
    assert "departed" not in KICKOFF_PHASE_OBJECTIVES["report"]
    assert "report(action=" not in KICKOFF_PHASE_OBJECTIVES["report"]
    for name in ("provision", "analyze", "build", "test"):
        assert KICKOFF_PHASE_OBJECTIVES[name] == PHASE_OBJECTIVES[name]


class _RecordingStorage:
    def __init__(self):
        self.calls = []

    def store_output(self, **kwargs):
        self.calls.append(kwargs)
        return "output_test123"


@pytest.mark.parametrize("policy", ["facts", "relevant"])
def test_departure_lines_survive_the_whole_intro_cut_in_the_report_brief(policy):
    from test_retained_phase_context import fixture_state

    engine = ReActEngine.__new__(ReActEngine)
    engine.config = SimpleNamespace(phase_context_policy=policy, phase_handoff_char_budget=6000)
    engine.phase_machine = PhaseMachine(start_phase="report")
    engine.phase_handoff = PhaseHandoff(fixture_state(), retain_context=True, context_policy=policy)
    engine.output_storage = _RecordingStorage()
    engine._phase_budget_numbers = lambda phase: (150, 10, 100)
    engine._detected_build_system = lambda: "maven"
    # Current evidence far larger than the whole-intro budget.
    engine._current_phase_evidence_lines = lambda: [
        f"- receipt inv-{i:03d}: an observed runner line with a long description"
        for i in range(400)
    ]
    departures = render_departure_block(
        records=[_record("build", "partial", key_results="14 of 15 modules built")],
        blockers=(_blocker("active"),),
        conflicts=("coverage_scan_incomplete",),
        repairs=(),
        verdict="partial",
        task_status="incomplete",
    )
    engine._report_departure_lines = lambda: departures

    intro = engine._phase_intro_step().content

    assert len(intro) <= 6000
    assert engine._phase_handoff_audit["contract_omission_ref"] == "output_test123"
    lines = intro.splitlines()
    for line in departures:
        assert line in lines
    omitted = engine.output_storage.calls[0]["output"]
    assert "Departure" not in omitted and "inv-399" in omitted
