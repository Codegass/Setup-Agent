# tests/test_job_settlement.py
"""Plan 8 Stage 1 — settling the books a detached job left open (spec §3.2).

p7d polaris (`logs/session_20260729_111737_22356`) dispatched its Gradle test
job, polled it patiently, watched 321 tests run and pass — and counted none of
them, because the job produced no terminal receipt inside the call that
started it. The exit code was written atomically to
`/tmp/sag_jobs/<job_id>.log.exit` by the launcher
(docker_orch/orch.py:979-984), the complete log was on disk, and the
before-snapshot had been taken at dispatch. Nothing returned to look.

Settlement returns to look. It reads the exit code, snapshots the same roots
again, runs the SAME parsers the synchronous path runs, and writes an ORDINARY
receipt through `record_invocation`. One schema, one writer, no second
bookkeeping system — the only thing that moved is WHEN (acceptance §6.5).

Attribution does not move at all. The settling receipt's window is its own
`before` against its own `after`, and receipts are ordered: a path claimed by a
receipt written INSIDE that window — between the dispatch and its settlement —
is EXCLUDED here and the exclusion is counted on the receipt. A receipt from
BEFORE the dispatch is outside the window and takes nothing: the whole point of
Stage 1 is that a detached retry finally gets to claim its own rewrite of a
report an earlier, already-closed attempt had produced. An untouched file
nobody vouched for stays unclaimed — the Bigtop rule is not negotiable.
"""

import json

from test_repair_contracts import ContainerFS, ScriptedOrchestrator, ok

from sag.agent.invocation_receipts import RECEIPT_DIR, next_sequence
from sag.agent.job_obligations import (
    OBLIGATION_DIR,
    build_obligation,
    open_job_ids,
    settle_open_obligations,
    write_obligation,
)
from sag.tools.internal.gradle_tool import (
    GradleTool,
    _gradle_cached_report_dirs,
    _gradle_module_outcomes,
)

ROOT = "/workspace/polaris"
JOB = "373f63e5a0a4"
LOG_PATH = f"/tmp/sag_jobs/{JOB}.log"
EXIT_PATH = f"{LOG_PATH}.exit"

# The tail of a Gradle run that served one module's tests from the build cache
# and ran the other's. Both parsers read THIS text, on both paths.
POLARIS_LOG = """> Task :polaris-core:compileJava
> Task :polaris-core:test
> Task :polaris-api:test FROM-CACHE
BUILD SUCCESSFUL in 41m 3s
"""

CORE_REPORT = f"{ROOT}/polaris-core/build/test-results/test/TEST-Core.xml"
API_REPORT = f"{ROOT}/polaris-api/build/test-results/test/TEST-Api.xml"
STRANGER_REPORT = f"{ROOT}/other/build/test-results/test/TEST-Stranger.xml"

# `sha256sum`-shaped digests: the snapshot parser keeps a line only when the
# digest is 64 hex characters, exactly as the container prints it.
SHA_CORE = "cc" * 32
SHA_API = "bb" * 32
SHA_STRANGER = "ab" * 32

# What was on disk when the job was dispatched: the api report from an earlier
# attempt, and a report from a module this job never touches.
BEFORE = {API_REPORT: SHA_API, STRANGER_REPORT: SHA_STRANGER}
# What is on disk when the job finishes: core wrote one, api's is byte-identical
# (Gradle served it FROM-CACHE), the stranger's never moved.
AFTER = {CORE_REPORT: SHA_CORE, API_REPORT: SHA_API, STRANGER_REPORT: SHA_STRANGER}

# The survey manifest the dispatch was decided on. Settlement must state the
# same pins and the same domain the synchronous receipt would have stated.
REQUIREMENTS = {
    "survey_fingerprint": "sf-8f21",
    "config_fingerprint": "cf-04ab",
    "build_domains": [{"root": ROOT}],
}


class JobContainer(ContainerFS):
    """`ContainerFS` plus the probes an invocation receipt makes.

    The report snapshot (`find … -exec sha256sum`), the tree's sha, the
    runner's toolchain line and the per-testcase tag read are the four round
    trips `record_invocation` owns; everything else is the ordinary evidence
    file layer.
    """

    def __init__(self, files=None, reports=None):
        super().__init__(files=files)
        self.reports = dict(reports or {})

    def __call__(self, command, **kwargs):
        if command.startswith("find "):
            self.commands.append(command)
            return ok(
                "\n".join(f"{digest}  {path}" for path, digest in sorted(self.reports.items()))
            )
        if command.startswith("git -C "):
            self.commands.append(command)
            return ok("9f1a2b3c4d5e6f708192a3b4c5d6e7f809111213")
        if "SAGTOOLCHAIN" in command:
            self.commands.append(command)
            return ok(f"{ROOT}/gradlew\nSAGTOOLCHAIN\nGradle 8.7")
        if command.startswith("grep -oE") or command.startswith("ls "):
            self.commands.append(command)
            return ok("")
        return super().__call__(command, **kwargs)


def _orchestrator(*, exit_code="0", reports=None, files=None):
    orchestrator = ScriptedOrchestrator()
    orchestrator.filesystem = JobContainer(
        files={
            LOG_PATH: POLARIS_LOG,
            **({EXIT_PATH: f"{exit_code}\n"} if exit_code is not None else {}),
            **(files or {}),
        },
        reports=AFTER if reports is None else reports,
    )
    return orchestrator


def _obligation(**overrides):
    """One obligation in the shape the real detach seam writes it.

    `dispatch_sequence` is part of that shape: it is the receipt ordinal in
    hand at dispatch, and it is the only thing that lets settlement tell a
    receipt written inside its window from one written before it. Tests that
    need two obligations, or that rewrite one body twice, keep the body they
    built rather than calling this again — a second call is a second dispatch.
    """
    body = build_obligation(
        job_id=JOB,
        tool="gradle",
        attempt=1,
        requested_action="test",
        effective_action="test",
        argv=f"{ROOT}/gradlew --continue test",
        working_directory=ROOT,
        before=BEFORE,
        log_path=LOG_PATH,
        exit_code_path=EXIT_PATH,
        requirements_pins={
            "survey_fingerprint": "sf-8f21",
            "config_fingerprint": "cf-04ab",
        },
        domain_id=ROOT,
        dispatch_sequence=next_sequence(),
    )
    body.update(overrides)
    return body


def _settled_receipt(orchestrator, job_id=JOB):
    """The receipt THIS obligation settled into, named by the ledger itself.

    Picking it out by id would guess; the ledger states it.
    """
    receipt_id = _ledger(orchestrator, job_id)["settled_receipt_id"]
    return json.loads(orchestrator.filesystem.files[f"{RECEIPT_DIR}/{receipt_id}.json"])


def _with_obligation(orchestrator, **overrides):
    write_obligation(orchestrator.execute_command, _obligation(**overrides))
    return orchestrator


def _receipts(orchestrator):
    return [
        json.loads(body)
        for path, body in sorted(orchestrator.filesystem.files.items())
        if path.startswith(f"{RECEIPT_DIR}/")
    ]


def _ledger(orchestrator, job_id=JOB):
    return json.loads(orchestrator.filesystem.files[f"{OBLIGATION_DIR}/{job_id}.json"])


# ---------------------------------------------------------------------------
# the receipt polaris never got
# ---------------------------------------------------------------------------


def test_a_terminated_job_settles_into_an_ordinary_receipt():
    orchestrator = _with_obligation(_orchestrator())

    settlements = settle_open_obligations(orchestrator)

    (receipt,) = _receipts(orchestrator)
    assert receipt["tool"] == "gradle"
    assert receipt["exit_code"] == 0
    assert receipt["outcome"] == "completed"
    assert receipt["argv"] == f"{ROOT}/gradlew --continue test"
    assert receipt["working_directory"] == ROOT
    assert [entry["path"] for entry in receipt["report_delta"]["new"]] == [CORE_REPORT]
    assert [settlement.job_id for settlement in settlements] == [JOB]
    assert settlements[0].receipt_id == receipt["receipt_id"]


def test_settlement_marks_the_obligation_and_nothing_else():
    orchestrator = _with_obligation(_orchestrator())
    before_body = _ledger(orchestrator)

    settle_open_obligations(orchestrator)

    after_body = _ledger(orchestrator)
    assert after_body["settled_receipt_id"] == _receipts(orchestrator)[0]["receipt_id"]
    assert {key: value for key, value in after_body.items() if key != "settled_receipt_id"} == {
        key: value for key, value in before_body.items() if key != "settled_receipt_id"
    }
    assert open_job_ids(orchestrator) == ()


def test_a_job_that_has_not_terminated_stays_open():
    """Nothing may be guessed from a partial log. No exit file, no receipt."""
    orchestrator = _with_obligation(_orchestrator(exit_code=None))

    assert settle_open_obligations(orchestrator) == []
    assert _receipts(orchestrator) == []
    assert open_job_ids(orchestrator) == (JOB,)


def test_settlement_is_idempotent():
    """Every trigger sweeps the whole ledger; a settled obligation is skipped."""
    orchestrator = _with_obligation(_orchestrator())

    settle_open_obligations(orchestrator)
    again = settle_open_obligations(orchestrator)

    assert again == []
    assert len(_receipts(orchestrator)) == 1


def test_a_failing_job_settles_as_a_failed_receipt():
    """A settled failure is an ordinary failure: same field, same meaning."""
    orchestrator = _with_obligation(_orchestrator(exit_code="1"))

    settle_open_obligations(orchestrator)

    (receipt,) = _receipts(orchestrator)
    assert receipt["exit_code"] == 1
    assert receipt["outcome"] == "failed"


def test_the_module_outcomes_are_the_synchronous_parsers_output():
    """`_gradle_module_outcomes` over the job's own complete log — the same
    call the synchronous path makes at gradle_tool.py:404."""
    orchestrator = _with_obligation(_orchestrator())

    settle_open_obligations(orchestrator)

    (receipt,) = _receipts(orchestrator)
    assert receipt["module_outcomes"] == _gradle_module_outcomes(POLARIS_LOG)


def test_a_cache_hit_the_job_vouched_for_is_claimed():
    """`_gradle_cached_report_dirs` runs on the settled path too, so kafka's
    4,686-test lesson survives detachment."""
    orchestrator = _with_obligation(_orchestrator())

    settle_open_obligations(orchestrator)

    (receipt,) = _receipts(orchestrator)
    assert [entry["path"] for entry in receipt["report_delta"]["cached"]] == [API_REPORT]


def test_an_untouched_report_nobody_vouched_for_stays_unclaimed():
    """The Bigtop rule. Settlement lets completed work claim its OWN write
    window, and not one file more."""
    orchestrator = _with_obligation(_orchestrator())

    settle_open_obligations(orchestrator)

    (receipt,) = _receipts(orchestrator)
    claimed = {
        entry["path"]
        for bucket in ("new", "changed", "cached")
        for entry in receipt["report_delta"].get(bucket, ())
    }
    assert STRANGER_REPORT not in claimed


# ---------------------------------------------------------------------------
# attribution: an intervening receipt claimed it first
# ---------------------------------------------------------------------------


def _other_receipt(receipt_id, paths, digest=SHA_CORE, exit_code=0):
    """One receipt some OTHER dispatch wrote, claiming `paths` at `digest`."""
    return json.dumps(
        {
            "schema_version": 2,
            "receipt_id": receipt_id,
            "tool": "gradle",
            "requested_action": "test",
            "effective_action": "test",
            "argv": f"{ROOT}/gradlew :polaris-core:test",
            "working_directory": ROOT,
            "exit_code": exit_code,
            "outcome": "completed" if exit_code == 0 else "failed",
            "report_delta": {
                "new": [{"path": path, "sha256": digest} for path in paths],
                "changed": [],
            },
        },
        sort_keys=True,
    )


def _with_intervening(*paths):
    """This job's obligation, plus a receipt written AFTER its dispatch.

    The order is the fact under test: the obligation's `dispatch_sequence` is
    taken first, the other receipt's id second, so the receipt is provably
    inside the window between dispatch and settlement.
    """
    orchestrator = _with_obligation(_orchestrator())
    receipt_id = f"inv-gradle-3-{next_sequence():04d}"
    orchestrator.filesystem.files[f"{RECEIPT_DIR}/{receipt_id}.json"] = _other_receipt(
        receipt_id, paths
    )
    return orchestrator


def test_a_path_an_intervening_receipt_already_claimed_is_excluded():
    """First claim wins inside the window. Two receipts counting one report
    file is how a passing suite gets counted twice."""
    orchestrator = _with_intervening(CORE_REPORT)

    settle_open_obligations(orchestrator)

    settled = _settled_receipt(orchestrator)
    assert settled["report_delta"]["new"] == []
    assert settled["excluded_claimed_paths"] == 1


def test_the_exclusion_never_touches_what_nobody_claimed():
    """Only the intervening receipt's own paths move; this job keeps the rest
    of its window."""
    orchestrator = _with_intervening(CORE_REPORT)

    settle_open_obligations(orchestrator)

    settled = _settled_receipt(orchestrator)
    assert [entry["path"] for entry in settled["report_delta"]["cached"]] == [API_REPORT]


def test_a_settlement_whose_mark_never_landed_cannot_double_count():
    """Risk §7, two writers to one job: if the ledger mark fails after the
    receipt was written, the next sweep settles again — and the FIRST receipt
    now owns every path, so the second one claims nothing. The exclusion rule
    is what makes that harmless.

    The re-read body is the SAME dispatch, ordinal included: a mark that never
    reached disk did not re-dispatch the job.
    """
    body = _obligation()
    orchestrator = _orchestrator()
    write_obligation(orchestrator.execute_command, body)
    settle_open_obligations(orchestrator)
    # The mark that never reached disk.
    orchestrator.filesystem.files[f"{OBLIGATION_DIR}/{JOB}.json"] = json.dumps(
        body, sort_keys=True
    )

    settle_open_obligations(orchestrator)

    first, second = sorted(_receipts(orchestrator), key=lambda receipt: receipt["receipt_id"])
    assert len(_delta_paths(first)) == 2
    assert _delta_paths(second) == []
    assert second["excluded_claimed_paths"] == 2


# ---------------------------------------------------------------------------
# attribution: a receipt from BEFORE the dispatch is outside the window
# ---------------------------------------------------------------------------

SHA_CORE_ATTEMPT_1 = "dd" * 32


def test_a_receipt_from_before_this_dispatch_does_not_take_the_rewrite():
    """The camel case Stage 1 exists to retire, in miniature.

    Attempt 1 ran synchronously, exited 1, and wrote a receipt claiming
    TEST-Core.xml at sha A. Attempt 2 dispatched the same command and detached,
    holding A in its `before`. The job finished and REWROTE the report at sha B.

    B is attempt 2's own evidence. The earlier receipt vouched for a version
    that is no longer on disk, and it was closed before this dispatch existed —
    first claim applies to the window between dispatch and settlement (spec
    §3.2), not to the whole history of the run. Excluding it here is how 11,492
    passing tests stayed unclaimable: the path leaves `after`, the settled
    receipt claims nothing, and the file on disk hashes to a sha only the stale
    earlier claim names.
    """
    earlier = f"inv-gradle-1-{next_sequence():04d}"
    orchestrator = _orchestrator(
        files={
            f"{RECEIPT_DIR}/{earlier}.json": _other_receipt(
                earlier, [CORE_REPORT], digest=SHA_CORE_ATTEMPT_1, exit_code=1
            )
        }
    )
    _with_obligation(orchestrator, before={CORE_REPORT: SHA_CORE_ATTEMPT_1, **BEFORE})

    settle_open_obligations(orchestrator)

    settled = _settled_receipt(orchestrator)
    assert settled["report_delta"]["changed"] == [{"path": CORE_REPORT, "sha256": SHA_CORE}]
    assert "excluded_claimed_paths" not in settled


def test_the_pre_dispatch_receipt_is_still_the_only_claim_it_ever_made():
    """Scoping the exclusion adds no claim to anyone else's books."""
    earlier = f"inv-gradle-1-{next_sequence():04d}"
    orchestrator = _orchestrator(
        files={
            f"{RECEIPT_DIR}/{earlier}.json": _other_receipt(
                earlier, [CORE_REPORT], digest=SHA_CORE_ATTEMPT_1, exit_code=1
            )
        }
    )
    _with_obligation(orchestrator, before={CORE_REPORT: SHA_CORE_ATTEMPT_1, **BEFORE})

    settle_open_obligations(orchestrator)

    untouched = json.loads(orchestrator.filesystem.files[f"{RECEIPT_DIR}/{earlier}.json"])
    assert untouched["report_delta"]["new"] == [
        {"path": CORE_REPORT, "sha256": SHA_CORE_ATTEMPT_1}
    ]


def test_an_obligation_that_cannot_order_itself_excludes_every_claim():
    """A ledger entry with no dispatch ordinal cannot tell a window from a
    history, so it refuses to claim a path any receipt already vouched for.
    Conservative, and stated: the notice counts what it gave up."""
    receipt_id = f"inv-gradle-3-{next_sequence():04d}"
    orchestrator = _orchestrator(
        files={f"{RECEIPT_DIR}/{receipt_id}.json": _other_receipt(receipt_id, [CORE_REPORT])}
    )
    body = _obligation()
    body.pop("dispatch_sequence")
    write_obligation(orchestrator.execute_command, body)

    settle_open_obligations(orchestrator)

    assert _settled_receipt(orchestrator)["excluded_claimed_paths"] == 1


def _delta_paths(receipt):
    return [
        entry["path"]
        for bucket in ("new", "changed", "cached")
        for entry in receipt["report_delta"].get(bucket, ())
    ]


def test_no_interleaving_records_no_exclusion():
    """An absent fact stays an absent key: `excluded_claimed_paths` is not a
    zero every receipt carries."""
    orchestrator = _with_obligation(_orchestrator())

    settle_open_obligations(orchestrator)

    assert "excluded_claimed_paths" not in _receipts(orchestrator)[0]


# ---------------------------------------------------------------------------
# acceptance §6.5: the settled path IS the synchronous path
# ---------------------------------------------------------------------------


def _synchronous_receipt():
    """What the runner writes when the same job finishes inside the call.

    Mirrors gradle_tool.execute()'s own call site (the parsers at
    gradle_tool.py:404-413), because that is the receipt settlement claims to
    reproduce.
    """
    orchestrator = _orchestrator()
    tool = GradleTool.__new__(GradleTool)
    tool.orchestrator = orchestrator
    tool._pending_invocation_receipt = None
    tool._record_invocation_receipt(
        requested_action="test",
        argv=f"{ROOT}/gradlew --continue test",
        working_directory=ROOT,
        attempt=1,
        result={"exit_code": 0, "output": POLARIS_LOG, "full_output": POLARIS_LOG},
        before=BEFORE,
        requirements=REQUIREMENTS,
        module_outcomes=_gradle_module_outcomes(POLARIS_LOG),
        cached_report_roots=_gradle_cached_report_dirs(POLARIS_LOG, ROOT),
    )
    return _receipts(orchestrator)[0]


def test_the_settled_receipt_is_field_for_field_the_synchronous_one():
    """One schema, one writer. Only the id — which is a sequence, not a fact —
    is allowed to differ."""
    orchestrator = _with_obligation(_orchestrator())

    settle_open_obligations(orchestrator)

    settled = _receipts(orchestrator)[0]
    synchronous = _synchronous_receipt()
    assert {key: value for key, value in settled.items() if key != "receipt_id"} == {
        key: value for key, value in synchronous.items() if key != "receipt_id"
    }


def test_the_settled_receipt_states_the_pins_the_dispatch_was_decided_on():
    """Settlement happens turns later; the survey manifest it would re-read is
    not the one this dispatch was decided on. The obligation carries them."""
    orchestrator = _with_obligation(_orchestrator())

    settle_open_obligations(orchestrator)

    receipt = _receipts(orchestrator)[0]
    assert receipt["survey_fingerprint"] == "sf-8f21"
    assert receipt["config_fingerprint"] == "cf-04ab"
    assert receipt["domain_id"] == ROOT


# ---------------------------------------------------------------------------
# the bounded notice
# ---------------------------------------------------------------------------


def test_the_notice_is_one_bounded_line():
    """Risk answer (spec §7): settlement must not surprise the model with a
    receipt from nowhere, and must not fabricate a tool result either."""
    orchestrator = _with_obligation(_orchestrator())

    (settlement,) = settle_open_obligations(orchestrator)

    receipt_id = settlement.receipt_id
    expected = f"[settled] job {JOB}: exit 0 — receipt {receipt_id}, 2 report paths claimed"
    assert settlement.notice() == expected
    assert "\n" not in settlement.notice()


def test_the_notice_states_the_exclusion_when_there_was_one():
    """"Earlier" would be false: the receipt that took the path was written
    AFTER this dispatch, which is precisely why it took it."""
    orchestrator = _with_intervening(CORE_REPORT)

    (settlement,) = settle_open_obligations(orchestrator)

    assert settlement.notice().endswith("1 already claimed by an intervening receipt)")
