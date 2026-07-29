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
`before` against its own `after`, and receipts are ordered: a path an
intervening receipt already claimed is EXCLUDED here and the exclusion is
counted on the receipt. An untouched file nobody vouched for stays unclaimed —
the Bigtop rule is not negotiable.
"""

import json

from test_repair_contracts import ContainerFS, ScriptedOrchestrator, ok

from sag.agent.invocation_receipts import RECEIPT_DIR
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
    )
    body.update(overrides)
    return body


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


def _intervening_receipt(*paths):
    """A receipt written between this job's dispatch and its settlement."""
    return json.dumps(
        {
            "schema_version": 2,
            "receipt_id": "inv-gradle-3-0042",
            "tool": "gradle",
            "requested_action": "test",
            "effective_action": "test",
            "argv": f"{ROOT}/gradlew :polaris-core:test",
            "working_directory": ROOT,
            "exit_code": 0,
            "outcome": "completed",
            "report_delta": {
                "new": [{"path": path, "sha256": SHA_CORE} for path in paths],
                "changed": [],
            },
        },
        sort_keys=True,
    )


def test_a_path_an_intervening_receipt_already_claimed_is_excluded():
    """First claim wins. Two receipts counting one report file is how a
    passing suite gets counted twice."""
    orchestrator = _with_obligation(
        _orchestrator(files={f"{RECEIPT_DIR}/inv-gradle-3-0042.json": _intervening_receipt(CORE_REPORT)})
    )

    settle_open_obligations(orchestrator)

    settled = next(
        receipt for receipt in _receipts(orchestrator) if receipt["receipt_id"] != "inv-gradle-3-0042"
    )
    assert settled["report_delta"]["new"] == []
    assert settled["excluded_claimed_paths"] == 1


def test_the_exclusion_never_touches_what_nobody_claimed():
    """Only the intervening receipt's own paths move; this job keeps the rest
    of its window."""
    orchestrator = _with_obligation(
        _orchestrator(files={f"{RECEIPT_DIR}/inv-gradle-3-0042.json": _intervening_receipt(CORE_REPORT)})
    )

    settle_open_obligations(orchestrator)

    settled = next(
        receipt for receipt in _receipts(orchestrator) if receipt["receipt_id"] != "inv-gradle-3-0042"
    )
    assert [entry["path"] for entry in settled["report_delta"]["cached"]] == [API_REPORT]


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

    assert settlement.notice() == (
        f"[settled] job {JOB}: exit 0 — receipt {settlement.receipt_id}, "
        "2 report paths claimed"
    )
    assert "\n" not in settlement.notice()


def test_the_notice_states_the_exclusion_when_there_was_one():
    orchestrator = _with_obligation(
        _orchestrator(files={f"{RECEIPT_DIR}/inv-gradle-3-0042.json": _intervening_receipt(CORE_REPORT)})
    )

    (settlement,) = settle_open_obligations(orchestrator)

    assert settlement.notice().endswith("1 already claimed by an earlier receipt)")
