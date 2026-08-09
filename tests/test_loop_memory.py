import pytest

from sag.agent.loop_memory import CompletionClaimEvent, LoopEvent, LoopMemory
from sag.agent.phase_transitions import RepairRequest


@pytest.fixture
def loop_memory():
    return LoopMemory()


def event(
    *,
    phase="build",
    attempt_id="build-1",
    tool="run_command",
    args=None,
    failure="",
    state=None,
    outcome=None,
    error_code="",
    evidence_ref="",
    invocation_status="completed",
    job_id="",
    output_cursor="",
):
    if args is None:
        args = {"command": "mvn test"}
    if state is None:
        state = {
            "environment": 1,
            "dependencies": 1,
            "artifacts": 1,
            "test_runtime": 1,
            "project_analysis": 1,
        }
    return LoopEvent(
        phase=phase,
        attempt_id=attempt_id,
        tool_name=tool,
        args=args,
        operation_outcome=outcome or ("failed" if failure else "success"),
        error_code=error_code or ("ACTION_FAILED" if failure else ""),
        failure_signature=failure,
        relevant_state=state,
        evidence_ref=evidence_ref,
        invocation_status=invocation_status,
        job_id=job_id,
        output_cursor=output_cursor,
    )


def test_same_repair_across_attempts_is_one_recurrence_key(loop_memory):
    first = event(
        phase="build",
        attempt_id="build-1",
        args={"command": "mvn install -DskipTests"},
        failure="missing:sibling-a",
        state={"dependencies": 4, "artifacts": 2},
    )
    second = event(
        phase="build",
        attempt_id="build-2",
        args={"command": "mvn   install -DskipTests"},
        failure="missing:sibling-a",
        state={"dependencies": 4, "artifacts": 2},
    )

    assert loop_memory.observe(first).decision == "continue"
    decision = loop_memory.observe(second)
    assert decision.decision == "guide"
    assert decision.prior_attempt_ids == ("build-1",)


def test_different_invalid_pytest_paths_share_one_recurrence(loop_memory):
    first = event(
        phase="test",
        attempt_id="test-1",
        tool="build",
        args={
            "action": "test",
            "working_directory": "/workspace/tvm",
            "args": "tests/python/unittest/test_a.py",
        },
        failure="pytest_args_rejected:invalid_selector",
        error_code="PYTEST_ARGS_REJECTED",
    )
    second = event(
        phase="test",
        attempt_id="test-1",
        tool="build",
        args={
            "action": "test",
            "working_directory": "/workspace/tvm",
            "args": "tests/python/unittest/test_b.py",
        },
        failure="pytest_args_rejected:invalid_selector",
        error_code="PYTEST_ARGS_REJECTED",
    )

    assert loop_memory.observe(first).decision == "continue"
    decision = loop_memory.observe(second)
    assert decision.decision == "guide"
    assert decision.recurrence_count == 2


def test_same_action_after_relevant_progress_is_not_a_loop(loop_memory):
    action = event(
        tool="poll_command",
        args={"job_id": "42"},
        job_id="42",
        state={"artifacts": 2, "test_runtime": 0},
    )
    loop_memory.observe(action)

    progressed = action.with_state({"artifacts": 3, "test_runtime": 0})

    assert loop_memory.observe(progressed).decision == "continue"


def test_unrelated_state_change_does_not_hide_recurrence(loop_memory):
    first = event(
        args={"command": "mvn test"},
        failure="tests:red",
        state={"test_runtime": 3, "artifacts": 1, "project_analysis": 1},
    )
    second = event(
        args={"command": "mvn test"},
        failure="tests:red",
        state={"test_runtime": 3, "artifacts": 1, "project_analysis": 9},
    )

    assert loop_memory.observe(first).decision == "continue"
    decision = loop_memory.observe(second)
    assert decision.decision == "guide"
    assert "project_analysis" not in decision.relevant_state_vector.scopes


def test_phase_is_metadata_not_recurrence_identity(loop_memory):
    first = event(
        phase="build",
        attempt_id="build-1",
        args={"command": "mvn test"},
        failure="same",
    )
    second = event(
        phase="test",
        attempt_id="test-1",
        args={"command": "mvn test"},
        failure="same",
    )

    loop_memory.observe(first)

    assert loop_memory.observe(second).decision == "guide"


def test_fourth_unchanged_recurrence_forces_break(loop_memory):
    decisions = [
        loop_memory.observe(event(failure="fixed", evidence_ref=f"log://{index}")).decision
        for index in range(4)
    ]

    assert decisions == ["continue", "guide", "guide", "force_break"]


def test_repeat_immediately_after_force_break_closes_phase(loop_memory):
    for _ in range(4):
        loop_memory.observe(event(failure="fixed"))

    decision = loop_memory.observe(event(failure="fixed"))

    assert decision.decision == "close_phase"
    assert decision.close_phase is True


def test_different_next_action_disarms_force_break(loop_memory):
    for _ in range(4):
        loop_memory.observe(event(failure="fixed"))
    loop_memory.observe(event(args={"command": "read pom.xml"}, failure="different"))

    decision = loop_memory.observe(event(failure="fixed"))

    assert decision.decision == "force_break"
    assert decision.close_phase is False


def test_relevant_progress_resolves_the_armed_loop_blocker(loop_memory):
    decision = None
    for _ in range(4):
        decision = loop_memory.observe(event(failure="fixed"))
    progressed = event(failure="fixed").with_state(
        {
            "environment": 1,
            "dependencies": 1,
            "artifacts": 2,
            "test_runtime": 1,
            "project_analysis": 1,
        }
    )

    after_progress = loop_memory.observe(progressed)

    assert after_progress.decision == "continue"
    assert after_progress.resolved_blocker_signatures == (decision.blocker_signature,)


def test_later_progress_resolves_blocker_after_alternative_action(loop_memory):
    decision = None
    for _ in range(4):
        decision = loop_memory.observe(event(failure="fixed"))
    loop_memory.observe(event(args={"command": "inspect pom.xml"}, failure="different"))

    progressed = loop_memory.observe(
        event(
            args={"command": "inspect dependency tree"},
            failure="still different",
            state={
                "environment": 1,
                "dependencies": 2,
                "artifacts": 1,
                "test_runtime": 1,
                "project_analysis": 1,
            },
        )
    )

    assert progressed.resolved_blocker_signatures == (decision.blocker_signature,)


def test_sixteen_distinct_searches_are_advisory_only(loop_memory):
    decisions = [
        loop_memory.observe(
            event(
                phase="analyze",
                attempt_id="analyze-1",
                tool="search_files",
                args={"pattern": f"symbol-{index}"},
                outcome="success",
                state={"project_analysis": 1},
            )
        ).decision
        for index in range(16)
    ]

    assert "force_break" not in decisions
    assert decisions[-1] == "diversity_advisory"


def test_diversity_budget_is_isolated_per_tool_and_phase(loop_memory):
    decisions = []
    for index in range(8):
        decisions.append(
            loop_memory.observe(
                event(
                    phase="analyze",
                    tool="search_files",
                    args={"pattern": f"symbol-{index}"},
                    outcome="success",
                    state={"project_analysis": 1},
                )
            ).decision
        )
        decisions.append(
            loop_memory.observe(
                event(
                    phase="analyze",
                    tool="file_io",
                    args={"action": "read", "file_path": f"file-{index}.txt"},
                    outcome="success",
                    state={"project_analysis": 1},
                )
            ).decision
        )

    assert "diversity_advisory" not in decisions


def test_repeated_success_is_not_a_loop_candidate(loop_memory):
    decisions = [
        loop_memory.observe(
            event(
                tool="search_files",
                args={"pattern": "same-symbol"},
                outcome="success",
                state={"project_analysis": 1},
            )
        ).decision
        for _ in range(8)
    ]

    assert decisions == ["continue"] * 8


def test_changed_pending_poll_cursor_is_exempt(loop_memory):
    first = event(
        tool="poll_command",
        args={"job_id": "42"},
        outcome="unknown",
        invocation_status="pending",
        job_id="42",
        output_cursor="100",
    )

    assert loop_memory.observe(first).decision == "continue"
    assert loop_memory.observe(first.with_output_cursor("200")).decision == "continue"
    assert loop_memory.observe(first.with_output_cursor("200")).decision == "guide"


def test_pending_to_terminal_poll_transition_is_progress(loop_memory):
    pending = event(
        tool="poll_command",
        args={"job_id": "42"},
        outcome="unknown",
        invocation_status="pending",
        job_id="42",
        output_cursor="100",
    )
    terminal = event(
        tool="poll_command",
        args={"job_id": "42"},
        outcome="failed",
        failure="job:failed",
        invocation_status="completed",
        job_id="42",
        output_cursor="100",
    )

    assert loop_memory.observe(pending).decision == "continue"
    assert loop_memory.observe(terminal).decision == "continue"
    assert loop_memory.observe(terminal).decision == "guide"


def test_changed_poll_cursor_disarms_force_break_and_resolves_blocker(loop_memory):
    pending = event(
        tool="poll_command",
        args={"job_id": "42"},
        outcome="unknown",
        invocation_status="pending",
        job_id="42",
        output_cursor="100",
    )
    decision = None
    for _ in range(4):
        decision = loop_memory.observe(pending)

    progressed = loop_memory.observe(pending.with_output_cursor("200"))

    assert decision.decision == "force_break"
    assert progressed.decision == "continue"
    assert progressed.resolved_blocker_signatures == (decision.blocker_signature,)


def test_identical_terminal_poll_result_is_treated_normally(loop_memory):
    terminal = event(
        tool="poll_command",
        args={"job_id": "42"},
        outcome="failed",
        failure="job:failed",
        job_id="42",
        output_cursor="complete",
    )

    assert loop_memory.observe(terminal).decision == "continue"
    assert loop_memory.observe(terminal).decision == "guide"


def test_command_normalization_removes_only_volatile_execution_tokens(loop_memory):
    first = event(
        args={
            "command": (
                "tail /workspace/.setup_agent/jobs/job-42/output.log "
                "--pid 1234 --module module-a"
            )
        },
        failure="job:42 failed at 2026-07-17T15:00:00Z",
    )
    second = event(
        args={
            "command": (
                "tail   /workspace/.setup_agent/jobs/job-99/output.log "
                "--pid 9876 --module module-a"
            )
        },
        failure="job:99 failed at 2026-07-17T16:00:00Z",
    )

    first_decision = loop_memory.observe(first)
    second_decision = loop_memory.observe(second)

    assert first_decision.action_key == second_decision.action_key
    assert first_decision.outcome_key == second_decision.outcome_key
    assert second_decision.decision == "guide"


def test_semantic_argument_order_remains_part_of_action_identity(loop_memory):
    first = event(args={"command": "mvn -pl module-a test"}, failure="same")
    reordered = event(args={"command": "mvn test -pl module-a"}, failure="same")

    loop_memory.observe(first)

    assert loop_memory.observe(reordered).decision == "continue"


def test_repair_guard_rejects_same_request_without_progress(loop_memory):
    request = RepairRequest(
        from_phase="test",
        target_phase="build",
        source_attempt_id="test-1",
        reason_code="missing_artifact",
        failure_signature="missing:sibling-a",
        hypothesis="publish sibling-a",
        evidence_refs=("log://test-1",),
    )
    vector = {"dependencies": 4, "artifacts": 2}

    assert loop_memory.check(request, vector) is None
    assert loop_memory.check(request, vector) == "repair_without_progress"


def completion_claim(**overrides):
    body = {
        "phase_attempt_id": "test-1",
        "claim_kind": "done",
        "judge_disposition": "repair_required",
        "blocker_id": "tests_failed",
        "mechanical_evidence_digest": "evidence-v1",
        "assessment_fingerprints": ("assessment-b", "assessment-a"),
        "open_job_fingerprints": (),
        "target_fingerprint": "sha-1",
        "config_fingerprint": "config-1",
        "fact_fingerprint": "facts-1",
        "evidence_refs": ("ref-b", "ref-a"),
        "prose": "I think this is done",
    }
    body.update(overrides)
    return CompletionClaimEvent(**body)


def test_third_no_op_completion_claim_converges_as_agent_no_progress(loop_memory):
    decisions = [loop_memory.observe_completion_claim(completion_claim()) for _ in range(3)]

    assert [decision.decision for decision in decisions] == [
        "continue",
        "continue",
        "agent_no_progress",
    ]
    assert decisions[-1].recurrence_count == 3
    assert decisions[-1].close_phase is True
    assert loop_memory.observe_completion_claim(completion_claim()).recurrence_count == 3


def test_done_blocked_prose_and_ref_order_cannot_evade_completion_chain(loop_memory):
    first = completion_claim()
    second = completion_claim(
        claim_kind="blocked",
        prose="a wholly different rationale",
        evidence_refs=("ref-a", "ref-b"),
        assessment_fingerprints=("assessment-a", "assessment-b"),
    )

    assert loop_memory.observe_completion_claim(first).recurrence_count == 1
    decision = loop_memory.observe_completion_claim(second)

    assert decision.recurrence_count == 2
    assert decision.key.canonical_claim == "completion"


def test_invalid_or_identical_action_does_not_reset_completion_chain(loop_memory):
    assert loop_memory.observe_completion_claim(completion_claim()).recurrence_count == 1
    assert loop_memory.observe_material_action("act-a", schema_valid=False) is False
    assert loop_memory.observe_completion_claim(completion_claim()).recurrence_count == 2

    assert loop_memory.observe_material_action("act-a") is True
    assert loop_memory.observe_completion_claim(completion_claim()).recurrence_count == 1
    assert loop_memory.observe_material_action("act-a") is False
    assert loop_memory.observe_completion_claim(completion_claim()).recurrence_count == 2


def test_new_material_action_resets_even_when_the_later_execution_failed(loop_memory):
    loop_memory.observe_completion_claim(completion_claim())
    loop_memory.observe_completion_claim(completion_claim())

    # LoopMemory needs dispatch identity, not a success claim.  A failed run is
    # still a real experiment from which the model can revise its hypothesis.
    assert loop_memory.observe_material_action({"action_fingerprint": "act-repair-b"}) is True

    decision = loop_memory.observe_completion_claim(completion_claim())
    assert decision.recurrence_count == 1
    assert loop_memory.epochs.material_action == 1


def test_new_fact_resets_but_duplicate_fact_fingerprint_does_not(loop_memory):
    loop_memory.observe_completion_claim(completion_claim())
    assert loop_memory.observe_evidence("fact-content-a") is True
    assert loop_memory.observe_completion_claim(completion_claim()).recurrence_count == 1
    assert loop_memory.observe_evidence("fact-content-a") is False
    assert loop_memory.observe_completion_claim(completion_claim()).recurrence_count == 2


def test_job_transition_resets_but_unchanged_poll_does_not(loop_memory):
    loop_memory.observe_completion_claim(completion_claim())
    assert loop_memory.observe_job_transition("job-1", "running", progress_fingerprint="10")
    assert loop_memory.observe_completion_claim(completion_claim()).recurrence_count == 1
    assert not loop_memory.observe_job_transition("job-1", "running", progress_fingerprint="10")
    assert loop_memory.observe_completion_claim(completion_claim()).recurrence_count == 2
    assert loop_memory.observe_job_transition("job-1", "settled", progress_fingerprint="20")
    assert loop_memory.observe_completion_claim(completion_claim()).recurrence_count == 1


@pytest.mark.parametrize(
    "disposition",
    [
        "wait_required",
        "harness_recovery_required",
        "terminal_claimable",
        "terminal_blocked",
    ],
)
def test_non_repair_dispositions_never_spend_completion_budget(loop_memory, disposition):
    first = loop_memory.observe_completion_claim(completion_claim())
    ignored = loop_memory.observe_completion_claim(completion_claim(judge_disposition=disposition))
    after = loop_memory.observe_completion_claim(completion_claim())

    assert first.recurrence_count == 1
    assert ignored.decision == "not_counted"
    assert ignored.recurrence_count == 0
    assert after.recurrence_count == 2


def test_completion_claim_cap_must_be_positive():
    with pytest.raises(ValueError, match="completion claim cap"):
        LoopMemory(completion_claim_cap=0)
