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

from test_repair_contracts import ContainerFS, ScriptedOrchestrator

from sag.agent.invocation_receipts import next_sequence
from sag.agent.job_obligations import (
    OBLIGATION_DIR,
    OBLIGATION_SCHEMA_VERSION,
    build_obligation,
    open_job_ids,
    read_obligations,
    record_dispatch_obligation,
    write_obligation,
)
from sag.tools.internal.gradle_tool import GradleTool
from sag.tools.internal.maven_tool import MavenTool

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
        "pid_path": "/tmp/sag_jobs/373f63e5a0a4.pid",
        "log_path": "/tmp/sag_jobs/373f63e5a0a4.log",
        "exit_code_path": "/tmp/sag_jobs/373f63e5a0a4.log.exit",
    },
}

BEFORE = {"/workspace/polaris/service/build/test-results/test/TEST-a.xml": "aa"}


def _obligation(filesystem, job_id="373f63e5a0a4"):
    return json.loads(filesystem.files[f"{OBLIGATION_DIR}/{job_id}.json"])


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
    _gradle(orchestrator)._record_invocation_receipt(
        requested_action="test",
        argv="/workspace/polaris/gradlew --continue test",
        working_directory="/workspace/polaris",
        attempt=2,
        result=POLARIS_HANDOFF,
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
    assert obligation["settled_receipt_id"] is None


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
    )
    write_obligation(orchestrator.execute_command, first)

    refused = write_obligation(
        orchestrator.execute_command,
        {**first, "argv": "gradlew build"},
    )

    assert refused is False
    assert _obligation(orchestrator.filesystem)["argv"] == "gradlew test"


def test_settling_writes_the_receipt_id_and_nothing_else():
    """The one permitted rewrite: null -> a receipt id, on an otherwise
    byte-identical body."""
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
    )
    write_obligation(orchestrator.execute_command, obligation)

    settled = write_obligation(
        orchestrator.execute_command,
        {**obligation, "settled_receipt_id": "inv-gradle-1-0002"},
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
    )
    write_obligation(orchestrator.execute_command, obligation)
    write_obligation(
        orchestrator.execute_command,
        {**obligation, "settled_receipt_id": "inv-gradle-1-0002"},
    )

    assert write_obligation(orchestrator.execute_command, obligation) is False
    assert (
        write_obligation(
            orchestrator.execute_command,
            {**obligation, "settled_receipt_id": "inv-gradle-1-0003"},
        )
        is False
    )
    assert _obligation(orchestrator.filesystem)["settled_receipt_id"] == "inv-gradle-1-0002"


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


def test_a_line_that_does_not_parse_is_skipped_never_fatal():
    """Same read discipline as every other evidence directory: a corrupt
    neighbour must not hide the obligations we do understand."""
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

    assert [record["job_id"] for record in records] == ["373f63e5a0a4"]


def test_open_job_ids_names_only_the_books_still_out():
    orchestrator = ScriptedOrchestrator(
        files={
            f"{OBLIGATION_DIR}/aaaaaaaaaaaa.json": json.dumps(
                {"job_id": "aaaaaaaaaaaa", "settled_receipt_id": "inv-gradle-1-0002"},
                sort_keys=True,
            ),
            f"{OBLIGATION_DIR}/bbbbbbbbbbbb.json": json.dumps(
                {"job_id": "bbbbbbbbbbbb", "settled_receipt_id": None},
                sort_keys=True,
            ),
        }
    )

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


def test_an_absent_ledger_directory_states_no_obligations():
    """The normal case for every run that never detached anything: the glob
    matches nothing, the read RAN, and the answer is genuinely empty."""
    orchestrator = ScriptedOrchestrator()

    assert read_obligations(orchestrator) == []
    assert open_job_ids(orchestrator) == ()
