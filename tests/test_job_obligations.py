# tests/test_job_obligations.py
"""Plan 8 Stage 1 — the evidence a detached dispatch was already holding.

p7d polaris (`logs/session_20260729_111737_22356`) produced exactly ONE
invocation receipt for the whole run: `inv-gradle-1-0001`, the FAILED Java-17
compile, exit 1. The successful Java-21 retry detached. The test job detached.
321 tests ran, 321 passed, and no receipt could claim any of them, so they sat
in auxiliary. p7d camel (`logs/session_20260729_111740_22389`) is the same
shape at 11,492 tests.

Nothing was missing at the seam. Both runners hold the `before` snapshot, the
frozen contract and the detach handle when they reach

    # gradle_tool.py:682, maven_tool.py:1061
    if result.get("dispatch_status") in DETACHED_HANDOFF_STATUSES:
        return

and drop all of it. The obligation is that evidence, written down: one atomic
file per detached dispatch, so the work that outlives the call can still be
accounted for when it finishes (spec §3.1).

The ledger is append-only. `settled_receipt_id` is the ONE field settlement
may ever write, and it may only move from null to a receipt id — everything
else about a dispatch was true when the dispatch happened and stays true.
"""

import json

from container_evidence_fakes import ContainerFS, ScriptedOrchestrator

from sag.agent.evidence_publications import (
    install_evidence_publication_authority,
    reset_evidence_publication_authority,
    unavailable_evidence_publication_authority,
)
from sag.agent.invocation_receipts import next_sequence
from sag.agent.job_obligations import (
    OBLIGATION_DIR,
    OBLIGATION_SCHEMA_VERSION,
    blocks_model,
    build_obligation,
    is_terminal_unpersisted,
    open_job_ids,
    process_is_live,
    read_obligations,
    record_dispatch_obligation,
    record_dispatch_obligation_result,
    settlement_is_pending,
    validate_obligation_v3,
    write_obligation,
)
from sag.tools.internal.gradle_tool import GradleTool
from sag.tools.internal.maven_tool import MavenTool

TERMINAL_AUTHORITY = "docker_exec_inspect_v1"
DOCKER_EXEC_ID = "e" * 64
CONTAINER_ID = "c" * 64
TERMINAL_IDENTITY = {
    "terminal_authority": TERMINAL_AUTHORITY,
    "docker_exec_id": DOCKER_EXEC_ID,
    "container_id": CONTAINER_ID,
    "start_accepted": True,
    "startup_identity_verified": True,
    "runner_dispatch_state": "accepted",
    "pid": 4711,
    "pgid": 4711,
    "pid_path": "/tmp/sag_jobs/fixture.pid",
    "pgid_path": "/tmp/sag_jobs/fixture.pgid",
    "identity_path": "/tmp/sag_jobs/fixture.identity",
    "process_identity_token": "a" * 64,
}

# The polaris test job's handoff, in the exact shape
# `execute_command_with_soft_timeout` returns when the 900s window closes on a
# job that is still running (docker_orch/orch.py:1201-1216).
POLARIS_HANDOFF = {
    "success": True,
    "exit_code": None,
    "output": "⏳ Command still running after the 900s soft window",
    "termination_reason": None,
    "dispatch_status": "running_detached",
    "runner_dispatched": True,
    "lifecycle_state": "pending",
    "liveness_state": "running",
    "dispatch": {
        "started": True,
        "job_id": "373f63e5a0a4",
        "pid": 4711,
        "pgid": 4711,
        "process_identity_token": "a" * 64,
        "pid_path": "/tmp/sag_jobs/373f63e5a0a4.pid",
        "pgid_path": "/tmp/sag_jobs/373f63e5a0a4.pgid",
        "identity_path": "/tmp/sag_jobs/373f63e5a0a4.identity",
        "log_path": "/tmp/sag_jobs/373f63e5a0a4.log",
        "exit_code_path": "/tmp/sag_jobs/373f63e5a0a4.log.exit",
        **TERMINAL_IDENTITY,
    },
}

BEFORE = {"/workspace/polaris/service/build/test-results/test/TEST-a.xml": "a" * 64}


def _obligation(filesystem, job_id="373f63e5a0a4"):
    return json.loads(filesystem.files[f"{OBLIGATION_DIR}/{job_id}.json"])


def _running_obligation(job_id="373f63e5a0a4"):
    return build_obligation(
        job_id=job_id,
        tool="gradle",
        attempt=1,
        requested_action="test",
        effective_action="test",
        argv="gradlew test",
        working_directory="/workspace/polaris",
        before=BEFORE,
        log_path=f"/tmp/sag_jobs/{job_id}.log",
        exit_code_path=f"/tmp/sag_jobs/{job_id}.log.exit",
        **TERMINAL_IDENTITY,
    )


def _terminal_pending(obligation, *, exit_code=0):
    return {
        **obligation,
        "process_state": "terminal",
        "settlement_state": "pending",
        "terminal_exit_code": exit_code,
        "terminal_marker_ref": f"docker-exec:{obligation['docker_exec_id']}",
        "terminal_observed_at": "2026-08-08T00:00:00Z",
    }


def _gradle(orchestrator):
    tool = GradleTool.__new__(GradleTool)
    tool.orchestrator = orchestrator
    tool._pending_invocation_receipt = None
    return tool


def _maven(orchestrator):
    tool = MavenTool.__new__(MavenTool)
    tool.orchestrator = orchestrator
    tool._pending_invocation_receipt = None
    return tool


def test_the_gradle_detach_seam_records_what_it_was_about_to_drop():
    """The line that cost polaris its receipts now writes the obligation."""
    orchestrator = ScriptedOrchestrator()
    handoff = {**POLARIS_HANDOFF, "dispatch": dict(POLARIS_HANDOFF["dispatch"])}
    _gradle(orchestrator)._record_invocation_receipt(
        requested_action="test",
        argv="/workspace/polaris/gradlew --continue test",
        working_directory="/workspace/polaris",
        attempt=2,
        result=handoff,
        before=BEFORE,
    )

    obligation = _obligation(orchestrator.filesystem)
    assert obligation["schema_version"] == OBLIGATION_SCHEMA_VERSION
    assert obligation["tool"] == "gradle"
    assert obligation["attempt"] == 2
    assert obligation["argv"] == "/workspace/polaris/gradlew --continue test"
    assert obligation["working_directory"] == "/workspace/polaris"
    assert obligation["before"] == BEFORE
    assert obligation["log_path"] == "/tmp/sag_jobs/373f63e5a0a4.log"
    assert obligation["exit_code_path"] == "/tmp/sag_jobs/373f63e5a0a4.log.exit"
    assert obligation["pid"] == obligation["pgid"] == 4711
    assert obligation["pid_path"].endswith(".pid")
    assert obligation["pgid_path"].endswith(".pgid")
    assert obligation["identity_path"].endswith(".identity")
    assert obligation["process_identity_token"] == "a" * 64
    assert obligation["settled_receipt_id"] is None
    assert handoff["job_obligation_persisted"] is True
    assert handoff["job_obligation_persistence_code"] == "persisted"


def test_a_new_obligation_states_process_and_settlement_truth_independently():
    obligation = build_obligation(
        job_id="373f63e5a0a4",
        tool="gradle",
        attempt=1,
        requested_action="test",
        effective_action="test",
        argv="gradlew test",
        working_directory="/workspace/polaris",
        before={},
        log_path="/tmp/sag_jobs/373f63e5a0a4.log",
        exit_code_path="/tmp/sag_jobs/373f63e5a0a4.log.exit",
        **TERMINAL_IDENTITY,
    )

    assert OBLIGATION_SCHEMA_VERSION == 3
    assert obligation["process_state"] == "running"
    assert obligation["settlement_state"] == "none"
    assert obligation["terminal_exit_code"] is None
    assert obligation["settlement_attempts"] == 0
    assert process_is_live(obligation) is True
    assert settlement_is_pending(obligation) is False
    assert blocks_model(obligation) is True


def test_terminal_unpersisted_is_not_misreported_as_a_running_job():
    record = {
        "schema_version": 3,
        "job_id": "373f63e5a0a4",
        "process_state": "terminal",
        "settlement_state": "unpersisted",
        "terminal_exit_code": 137,
        "settled_receipt_id": None,
    }

    assert process_is_live(record) is False
    assert settlement_is_pending(record) is False
    assert blocks_model(record) is False
    assert is_terminal_unpersisted(record) is True


def test_a_v1_unsettled_obligation_remains_conservatively_live_until_reconciled():
    legacy = {"schema_version": 1, "job_id": "legacy", "settled_receipt_id": None}

    assert process_is_live(legacy) is True
    assert blocks_model(legacy) is True


def test_the_detach_seam_records_where_the_dispatch_sits_in_receipt_order():
    """Settlement runs turns later and has to tell a receipt written INSIDE its
    window from one written before it. Without an ordinal recorded here it
    cannot: the reviewer's camel case is a detached retry losing its own rewrite
    to the receipt its own earlier attempt wrote. Receipts are already ordered
    by the process-global counter, so the dispatch takes a number from it.
    """
    orchestrator = ScriptedOrchestrator()
    before_dispatch = next_sequence()

    _gradle(orchestrator)._record_invocation_receipt(
        requested_action="test",
        argv="/workspace/polaris/gradlew --continue test",
        working_directory="/workspace/polaris",
        attempt=2,
        result=POLARIS_HANDOFF,
        before=BEFORE,
    )

    recorded = _obligation(orchestrator.filesystem)["dispatch_sequence"]
    assert isinstance(recorded, int)
    assert recorded > before_dispatch
    assert recorded < next_sequence()


def test_the_maven_detach_seam_records_its_ordinal_too():
    orchestrator = ScriptedOrchestrator()

    _maven(orchestrator)._record_invocation_receipt(
        requested_action="verify",
        effective_action="verify",
        argv="/usr/local/bin/mvn -B verify",
        working_directory="/workspace/camel",
        attempt=1,
        result=POLARIS_HANDOFF,
        before={},
    )

    assert isinstance(_obligation(orchestrator.filesystem)["dispatch_sequence"], int)


def test_detached_obligation_preserves_effective_jdk_for_settlement_receipt():
    effective_jdk = {
        "major": "17",
        "requirement_major": "17",
        "requirement_authority": "persisted_dynamic",
        "provenance": {"source_ref": "inv-maven-compile-0001"},
    }

    obligation = build_obligation(
        job_id="job-jdk-17",
        tool="maven",
        attempt=2,
        requested_action="compile",
        effective_action="compile",
        argv="mvn compile",
        working_directory="/workspace/project",
        before={},
        log_path="/tmp/sag_jobs/job-jdk-17.log",
        exit_code_path="/tmp/sag_jobs/job-jdk-17.exit",
        effective_jdk=effective_jdk,
        **TERMINAL_IDENTITY,
    )

    assert obligation["effective_jdk"] == effective_jdk


def test_a_dispatch_that_never_started_burns_no_ordinal():
    """An obligation nobody can settle is a leak; an ordinal spent on one is a
    hole in receipt order for no fact."""
    orchestrator = ScriptedOrchestrator()
    before_dispatch = next_sequence()

    record_dispatch_obligation(
        orchestrator.execute_command,
        result={"dispatch_status": "running_detached", "dispatch": {"started": False}},
        tool="gradle",
        attempt=1,
        requested_action="test",
        effective_action="test",
        argv="gradlew test",
        working_directory="/workspace/polaris",
        before={},
    )

    assert next_sequence() == before_dispatch + 1


def test_an_ambiguous_daemon_start_stays_ephemeral_and_burns_no_ordinal():
    orchestrator = ScriptedOrchestrator()
    before_dispatch = next_sequence()
    result = {
        "dispatch_status": "dispatch_unknown",
        "runner_dispatched": None,
        "runner_dispatch_state": "unknown",
        "dispatch": {
            "started": False,
            "job_id": "ambiguous-start",
            "pid": None,
            "pgid": None,
            "process_identity_token": "",
            "pid_path": "/tmp/sag_jobs/ambiguous-start.pid",
            "pgid_path": "/tmp/sag_jobs/ambiguous-start.pgid",
            "identity_path": "/tmp/sag_jobs/ambiguous-start.identity",
            "log_path": "/tmp/sag_jobs/ambiguous-start.log",
            "exit_code_path": "/tmp/sag_jobs/ambiguous-start.log.exit",
            "terminal_authority": TERMINAL_AUTHORITY,
            "docker_exec_id": DOCKER_EXEC_ID,
            "container_id": CONTAINER_ID,
            "start_accepted": False,
            "startup_identity_verified": False,
            "runner_dispatched": None,
            "runner_dispatch_state": "unknown",
        },
    }

    outcome = record_dispatch_obligation_result(
        orchestrator.execute_command,
        result=result,
        tool="gradle",
        attempt=1,
        requested_action="test",
        effective_action="test",
        argv="gradlew test",
        working_directory="/workspace/polaris",
        before={},
    )

    assert outcome.persisted is False
    assert outcome.code == "invalid_dispatch_handle"
    assert outcome.start_accepted is False
    assert outcome.startup_identity_verified is False
    assert outcome.runner_dispatch_state == "unknown"
    assert orchestrator.filesystem.files == {}
    assert next_sequence() == before_dispatch + 1


def test_the_detached_dispatch_still_mints_no_receipt():
    """A job with no terminal exit code has no outcome to record. The
    obligation is the promise of one, not the thing itself."""
    orchestrator = ScriptedOrchestrator()
    tool = _gradle(orchestrator)

    tool._record_invocation_receipt(
        requested_action="test",
        argv="/workspace/polaris/gradlew test",
        working_directory="/workspace/polaris",
        attempt=1,
        result=POLARIS_HANDOFF,
        before=BEFORE,
    )

    assert tool._pending_invocation_receipt is None
    assert not [path for path in orchestrator.filesystem.files if "invocation_receipts" in path]


def test_the_maven_detach_seam_records_the_same_obligation():
    """camel's `mvn verify` detached at maven_tool.py:1061 and left nothing."""
    orchestrator = ScriptedOrchestrator()
    handoff = {
        **POLARIS_HANDOFF,
        "dispatch": {**POLARIS_HANDOFF["dispatch"], "job_id": "9f21c0be55aa"},
    }

    _maven(orchestrator)._record_invocation_receipt(
        requested_action="verify",
        effective_action="verify",
        argv="/usr/local/bin/mvn -B verify",
        working_directory="/workspace/camel",
        attempt=1,
        result=handoff,
        before={},
    )

    obligation = _obligation(orchestrator.filesystem, "9f21c0be55aa")
    assert obligation["tool"] == "maven"
    assert obligation["requested_action"] == "verify"
    assert obligation["effective_action"] == "verify"
    assert obligation["argv"] == "/usr/local/bin/mvn -B verify"


def test_a_dispatch_that_never_started_records_no_obligation():
    """`dispatch_failed` carries no job id, no log and no exit path. An
    obligation nobody can ever settle is not a fact, it is a leak."""
    orchestrator = ScriptedOrchestrator()

    written = record_dispatch_obligation(
        orchestrator.execute_command,
        result={"dispatch_status": "running_detached", "dispatch": {"started": False}},
        tool="gradle",
        attempt=1,
        requested_action="test",
        effective_action="test",
        argv="gradlew test",
        working_directory="/workspace/polaris",
        before={},
    )

    assert written is None
    assert orchestrator.filesystem.files == {}


def test_the_same_body_under_an_existing_id_is_a_no_op_success():
    """Same convention as receipts, repairs and claims: a re-derivation must
    not double-write."""
    orchestrator = ScriptedOrchestrator()
    obligation = build_obligation(
        job_id="373f63e5a0a4",
        tool="gradle",
        attempt=1,
        requested_action="test",
        effective_action="test",
        argv="gradlew test",
        working_directory="/workspace/polaris",
        before=BEFORE,
        log_path="/tmp/sag_jobs/373f63e5a0a4.log",
        exit_code_path="/tmp/sag_jobs/373f63e5a0a4.log.exit",
        **TERMINAL_IDENTITY,
    )

    assert write_obligation(orchestrator.execute_command, obligation) is True
    assert write_obligation(orchestrator.execute_command, obligation) is True
    assert len(orchestrator.filesystem.writes()) == 1


def test_a_different_body_under_an_existing_id_is_refused():
    """An id is a claim about identity. A collision is a defect to see, not
    one to merge away."""
    orchestrator = ScriptedOrchestrator()
    first = build_obligation(
        job_id="373f63e5a0a4",
        tool="gradle",
        attempt=1,
        requested_action="test",
        effective_action="test",
        argv="gradlew test",
        working_directory="/workspace/polaris",
        before=BEFORE,
        log_path="/tmp/sag_jobs/373f63e5a0a4.log",
        exit_code_path="/tmp/sag_jobs/373f63e5a0a4.log.exit",
        **TERMINAL_IDENTITY,
    )
    write_obligation(orchestrator.execute_command, first)

    refused = write_obligation(
        orchestrator.execute_command,
        {**first, "argv": "gradlew build"},
    )

    assert refused is False
    assert _obligation(orchestrator.filesystem)["argv"] == "gradlew test"


def test_settling_writes_the_receipt_id_and_nothing_else():
    """Settlement is the terminal/pending -> terminal/settled edge."""
    orchestrator = ScriptedOrchestrator()
    obligation = build_obligation(
        job_id="373f63e5a0a4",
        tool="gradle",
        attempt=1,
        requested_action="test",
        effective_action="test",
        argv="gradlew test",
        working_directory="/workspace/polaris",
        before=BEFORE,
        log_path="/tmp/sag_jobs/373f63e5a0a4.log",
        exit_code_path="/tmp/sag_jobs/373f63e5a0a4.log.exit",
        **TERMINAL_IDENTITY,
    )
    write_obligation(orchestrator.execute_command, obligation)

    pending = {
        **obligation,
        "process_state": "terminal",
        "settlement_state": "pending",
        "terminal_exit_code": 0,
        "terminal_marker_ref": f"docker-exec:{obligation['docker_exec_id']}",
        "terminal_observed_at": "2026-08-08T00:00:00Z",
        "settlement_attempts": 1,
        "attempted_receipt_id": "inv-gradle-1-0002",
    }
    assert write_obligation(orchestrator.execute_command, pending) is True

    settled = write_obligation(
        orchestrator.execute_command,
        {
            **pending,
            "settlement_state": "settled",
            "settled_receipt_id": "inv-gradle-1-0002",
        },
    )

    assert settled is True
    assert _obligation(orchestrator.filesystem)["settled_receipt_id"] == "inv-gradle-1-0002"


def test_a_settled_obligation_is_never_reopened():
    """Append-only in the other direction too: a settled book does not
    un-settle, and a second settlement under a different receipt is refused."""
    orchestrator = ScriptedOrchestrator()
    obligation = build_obligation(
        job_id="373f63e5a0a4",
        tool="gradle",
        attempt=1,
        requested_action="test",
        effective_action="test",
        argv="gradlew test",
        working_directory="/workspace/polaris",
        before=BEFORE,
        log_path="/tmp/sag_jobs/373f63e5a0a4.log",
        exit_code_path="/tmp/sag_jobs/373f63e5a0a4.log.exit",
        **TERMINAL_IDENTITY,
    )
    write_obligation(orchestrator.execute_command, obligation)
    pending = {
        **obligation,
        "process_state": "terminal",
        "settlement_state": "pending",
        "terminal_exit_code": 0,
        "terminal_marker_ref": f"docker-exec:{obligation['docker_exec_id']}",
        "terminal_observed_at": "2026-08-08T00:00:00Z",
        "settlement_attempts": 1,
        "attempted_receipt_id": "inv-gradle-1-0002",
    }
    write_obligation(orchestrator.execute_command, pending)
    settled = {
        **pending,
        "settlement_state": "settled",
        "settled_receipt_id": "inv-gradle-1-0002",
    }
    write_obligation(
        orchestrator.execute_command,
        settled,
    )

    assert write_obligation(orchestrator.execute_command, obligation) is False
    assert (
        write_obligation(
            orchestrator.execute_command,
            {
                **settled,
                "attempted_receipt_id": "inv-gradle-1-0003",
                "settled_receipt_id": "inv-gradle-1-0003",
            },
        )
        is False
    )
    assert _obligation(orchestrator.filesystem)["settled_receipt_id"] == "inv-gradle-1-0002"


def test_job_obligation_strict_schema_recomputes_filename_and_lifecycle():
    running = _running_obligation("strict-job")
    assert validate_obligation_v3(running, expected_id="strict-job") == running

    for mutation in (
        lambda body: body.update(extra="future"),
        lambda body: body.update(run_id=" run-pytest "),
        lambda body: body.update(working_directory="workspace/polaris"),
        lambda body: body.update(process_state="terminal"),
        lambda body: body.update(settlement_attempts=True),
        lambda body: body.pop("start_accepted"),
        lambda body: body.update(start_accepted=False),
        lambda body: body.update(startup_identity_verified=False),
        lambda body: body.update(runner_dispatch_state="unknown"),
        lambda body: body.update(terminal_authority="container_marker_v1"),
        lambda body: body.update(docker_exec_id="0" * 63),
        lambda body: body.update(container_id="f" * 63),
        lambda body: body.update(process_identity_token="0" * 63),
        lambda body: body.update(pid=running["pid"] + 1),
    ):
        forged = json.loads(json.dumps(running))
        mutation(forged)
        try:
            validate_obligation_v3(forged)
        except (TypeError, ValueError):
            pass
        else:  # pragma: no cover - each mutation is one security regression
            raise AssertionError(f"forged obligation passed: {forged}")

    try:
        validate_obligation_v3(running, expected_id="other-job")
    except ValueError as exc:
        assert "filename" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("filename mismatch passed")


def test_live_job_ledger_rejects_deletion_rollback_and_forged_current_bytes():
    orchestrator = ScriptedOrchestrator()
    running = _running_obligation("revision-job")
    pending = _terminal_pending(running)
    path = f"{OBLIGATION_DIR}/revision-job.json"

    assert write_obligation(orchestrator.execute_command, running)
    initial_raw = orchestrator.filesystem.files[path]
    assert write_obligation(orchestrator.execute_command, pending)
    latest_raw = orchestrator.filesystem.files[path]
    assert read_obligations(orchestrator) == [pending]

    del orchestrator.filesystem.files[path]
    assert read_obligations(orchestrator) is None

    orchestrator.filesystem.files[path] = initial_raw
    assert read_obligations(orchestrator) is None

    forged = {**pending, "argv": "gradlew forged"}
    orchestrator.filesystem.files[path] = json.dumps(forged, sort_keys=True)
    assert read_obligations(orchestrator) is None

    orchestrator.filesystem.files[path] = latest_raw
    assert read_obligations(orchestrator) == [pending]


def test_host_publication_failure_leaves_job_revision_forensic_only():
    orchestrator = ScriptedOrchestrator()
    obligation = _running_obligation("unpublished-job")
    token = install_evidence_publication_authority(
        unavailable_evidence_publication_authority("host sink failed", run_id="run-pytest")
    )
    try:
        assert write_obligation(orchestrator.execute_command, obligation) is False
    finally:
        reset_evidence_publication_authority(token)

    path = f"{OBLIGATION_DIR}/unpublished-job.json"
    assert path in orchestrator.filesystem.files
    assert read_obligations(orchestrator) is None
    # Mutable evidence cannot be replay-laundered after the host lost the
    # predecessor edge; the container artifact remains forensic.
    assert write_obligation(orchestrator.execute_command, obligation) is False


def test_same_job_concurrent_lifecycle_writers_cannot_lose_the_winner():
    running = _running_obligation("racing-job")
    winner = _terminal_pending(running, exit_code=1)
    stale = _terminal_pending(running, exit_code=0)

    class RacingContainer(ContainerFS):
        def __init__(self):
            super().__init__()
            self.inject = False
            self.inside = False
            self.orchestrator = None

        def __call__(self, command, **kwargs):
            if self.inject and not self.inside and "fcntl.flock" in command:
                self.inside = True
                try:
                    assert self.orchestrator is not None
                    assert write_obligation(self.orchestrator.execute_command, winner) is True
                finally:
                    self.inside = False
                self.inject = False
            return super().__call__(command, **kwargs)

    execute = RacingContainer()
    orchestrator = ScriptedOrchestrator()
    orchestrator.filesystem = execute
    execute.orchestrator = orchestrator
    assert write_obligation(orchestrator.execute_command, running) is True
    execute.inject = True

    assert write_obligation(orchestrator.execute_command, stale) is False
    assert json.loads(execute.files[f"{OBLIGATION_DIR}/racing-job.json"]) == winner
    assert read_obligations(orchestrator) == [winner]


def test_a_failed_write_is_reported_and_never_raised():
    """Persistence is best effort HERE. The model is waiting on a build."""
    orchestrator = ScriptedOrchestrator()
    orchestrator.filesystem.writable = False

    assert (
        record_dispatch_obligation(
            orchestrator.execute_command,
            result=POLARIS_HANDOFF,
            tool="gradle",
            attempt=1,
            requested_action="test",
            effective_action="test",
            argv="gradlew test",
            working_directory="/workspace/polaris",
            before=BEFORE,
        )
        is None
    )

    outcome = record_dispatch_obligation_result(
        orchestrator.execute_command,
        result=POLARIS_HANDOFF,
        tool="gradle",
        attempt=1,
        requested_action="test",
        effective_action="test",
        argv="gradlew test",
        working_directory="/workspace/polaris",
        before=BEFORE,
    )
    assert outcome.job_id == "373f63e5a0a4"
    assert outcome.persisted is False
    assert outcome.code == "transport_write_failed"
    assert outcome.exit_code_path == "/tmp/sag_jobs/373f63e5a0a4.log.exit"


def test_large_obligation_round_trips_after_a_transport_retry():
    """Detached jobs carry the unbounded pre-dispatch report snapshot too.

    Fixing only terminal receipts would leave the same argv-limit regression
    at the detach seam, before the controller could ever wait for the job.
    """

    class ArgLimitContainer(ContainerFS):
        def __init__(self):
            super().__init__()
            self.fail_one_chunk = True

        def __call__(self, command, **kwargs):
            if len(command) >= 128 * 1024:
                self.commands.append(command)
                return {"exit_code": 255, "output": "argument list too long"}
            if self.fail_one_chunk and command.startswith("printf '%s' "):
                self.fail_one_chunk = False
                self.commands.append(command)
                return {"exit_code": 1, "output": "transient transport failure"}
            return super().__call__(command, **kwargs)

    orchestrator = ScriptedOrchestrator()
    orchestrator.filesystem = ArgLimitContainer()
    before = {
        f"/workspace/project/module-{index}/{'x' * 1550}/TEST-{index}.xml": (f"{index:064x}"[-64:])
        for index in range(2_000)
    }
    obligation = build_obligation(
        job_id="large-obligation",
        tool="maven",
        attempt=1,
        requested_action="verify",
        effective_action="verify",
        argv="./mvnw verify",
        working_directory="/workspace/project",
        before=before,
        log_path="/tmp/sag_jobs/large-obligation.log",
        exit_code_path="/tmp/sag_jobs/large-obligation.log.exit",
        **TERMINAL_IDENTITY,
    )
    canonical = json.dumps(obligation, sort_keys=True)
    assert len(canonical.encode("utf-8")) >= 3 * 1024 * 1024

    assert write_obligation(orchestrator.execute_command, obligation) is False
    assert f"{OBLIGATION_DIR}/large-obligation.json" not in orchestrator.filesystem.files
    assert not any(path.endswith(".tmp") for path in orchestrator.filesystem.files)

    assert write_obligation(orchestrator.execute_command, obligation) is True
    assert orchestrator.filesystem.files[f"{OBLIGATION_DIR}/large-obligation.json"] == canonical
    assert max(map(len, orchestrator.filesystem.commands)) <= 60_200
    assert not any(path.endswith(".tmp") for path in orchestrator.filesystem.files)


def test_a_malformed_record_holds_the_entire_job_barrier_fail_closed():
    """A corrupt neighbour may be the only record for another live job.

    Treating it as absent would let the model run behind an unknown process,
    so the controller must see an unreadable ledger rather than a safe prefix.
    """
    orchestrator = ScriptedOrchestrator(
        files={
            f"{OBLIGATION_DIR}/373f63e5a0a4.json": json.dumps(
                {
                    "schema_version": 1,
                    "job_id": "373f63e5a0a4",
                    "settled_receipt_id": None,
                },
                sort_keys=True,
            ),
            f"{OBLIGATION_DIR}/broken.json": "{not json",
        }
    )

    records = read_obligations(orchestrator)

    assert records is None
    command = orchestrator.filesystem.commands[-1]
    assert "for file in " in command
    assert "base64" in command
    assert "SAG_NAMED_JSON_RECORD_END_V1" in command
    assert f"cat {OBLIGATION_DIR}/*.json" not in command


def test_a_duplicate_key_record_holds_the_entire_job_barrier_fail_closed():
    orchestrator = ScriptedOrchestrator(
        files={
            f"{OBLIGATION_DIR}/good.json": json.dumps(
                {"job_id": "good", "settled_receipt_id": None}
            ),
            f"{OBLIGATION_DIR}/duplicate.json": (
                '{"job_id":"first","job_id":"second","settled_receipt_id":null}'
            ),
        }
    )

    assert read_obligations(orchestrator) is None


def test_open_job_ids_names_only_the_books_still_out():
    orchestrator = ScriptedOrchestrator()

    def obligation(job_id):
        return build_obligation(
            job_id=job_id,
            tool="gradle",
            attempt=1,
            requested_action="test",
            effective_action="test",
            argv="gradlew test",
            working_directory="/workspace/polaris",
            before={},
            log_path=f"/tmp/sag_jobs/{job_id}.log",
            exit_code_path=f"/tmp/sag_jobs/{job_id}.log.exit",
            **TERMINAL_IDENTITY,
        )

    settled = obligation("aaaaaaaaaaaa")
    running = obligation("bbbbbbbbbbbb")
    assert write_obligation(orchestrator.execute_command, settled)
    assert write_obligation(
        orchestrator.execute_command,
        {
            **settled,
            "process_state": "terminal",
            "settlement_state": "pending",
            "terminal_exit_code": 0,
            "terminal_marker_ref": f"docker-exec:{settled['docker_exec_id']}",
            "terminal_observed_at": "2026-08-08T00:00:00Z",
            "settlement_attempts": 1,
            "attempted_receipt_id": "inv-gradle-1-0002",
        },
    )
    assert write_obligation(
        orchestrator.execute_command,
        {
            **settled,
            "process_state": "terminal",
            "settlement_state": "settled",
            "terminal_exit_code": 0,
            "terminal_marker_ref": f"docker-exec:{settled['docker_exec_id']}",
            "terminal_observed_at": "2026-08-08T00:00:00Z",
            "settlement_attempts": 1,
            "attempted_receipt_id": "inv-gradle-1-0002",
            "settled_receipt_id": "inv-gradle-1-0002",
        },
    )
    assert write_obligation(orchestrator.execute_command, running)

    assert open_job_ids(orchestrator) == ("bbbbbbbbbbbb",)


def test_an_unreadable_ledger_reads_as_could_not_read():
    """Premise corrected (§6.8 fence 1 / P4). This test used to assert that a
    RAISING container reads as "no obligations" — but "container gone" is a
    failed read, not an empty directory, and reading it as empty is exactly
    what let the round-four review lift the §3.3 cap by deleting evidence
    (delete/corrupt/failed-cat/failed-settle each upgraded a contradicted
    success to a confirmed one). Could-not-read is now its own answer: None.
    The announcement surface still invents no job (open_job_ids stays ()) —
    the GATE is where an unreadable ledger has teeth."""

    class Broken(ContainerFS):
        def __call__(self, command, **kwargs):
            raise RuntimeError("container gone")

    orchestrator = ScriptedOrchestrator()
    orchestrator.filesystem = Broken()

    assert read_obligations(orchestrator) is None
    assert open_job_ids(orchestrator) == ()


def test_a_partial_multi_file_ledger_read_is_not_used_as_complete_state():
    class Partial(ContainerFS):
        def __call__(self, command, **kwargs):
            if "for file in " in command:
                return {
                    "exit_code": 70,
                    "output": json.dumps({"job_id": "first-only"}) + "\n",
                }
            return super().__call__(command, **kwargs)

    orchestrator = ScriptedOrchestrator()
    orchestrator.filesystem = Partial()

    assert read_obligations(orchestrator) is None


def test_an_absent_ledger_directory_states_no_obligations():
    """The normal case for every run that never detached anything: the glob
    matches nothing, the read RAN, and the answer is genuinely empty."""
    orchestrator = ScriptedOrchestrator()

    assert read_obligations(orchestrator) == []
    assert open_job_ids(orchestrator) == ()


def test_unreadable_settlement_output_is_not_an_observed_empty_log():
    from sag.agent.job_obligations import _read_complete_log

    assert (
        _read_complete_log(
            lambda *_a, **_k: {"success": False, "exit_code": 1, "output": ""}, "/tmp/job.log"
        )
        is None
    )
    assert (
        _read_complete_log(
            lambda *_a, **_k: {"success": True, "exit_code": 0, "output": ""}, "/tmp/job.log"
        )
        == ""
    )
