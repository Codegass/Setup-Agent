# tests/test_detached_death_harvest.py
"""Task #62: a detached dispatch that dies mid-run still harvests what it wrote.

Live kafka (d2r4 slice `logs/d2r4-20260815/slices/kafka.md`): the gradle
dispatch ran 13,132 + 987 tests — 27,177 `PASSED` lines in the 6.2 MB stored
log — and then the gradle build daemon disappeared. The run came back
`dispatch_status: "completed_detached"`, `exit_code: 1`, and the harness sealed
`DETACHED_OPERATION_FAILED` with `facts: {}` and `test_stats` all zeros, while
`metadata.analysis.test_results` sat right there in the same result object
holding `{"failed": 1, "skipped": 30, "total": 13132}`.

The reports the dying run wrote are physical, and its crash is a fact. Task #54
already kept the RECEIPT alive across the detached transport; what this task
adds is that the death does not swallow the harvest either:

* the receipt still snapshots `report_delta` for the reports the run wrote, so
  the physical counting path admits them exactly as for a clean run;
* the runner's own aggregate promotes into the result's `test_stats`, so the
  facade's facts carry `executed/failed/skipped` instead of nothing;
* the sealed error stays `DETACHED_OPERATION_FAILED` — red tests and the crash
  remain facts, and nothing here invents success;
* an observability field the receipt cannot carry is still dropped ALONE and
  named in `evidence_omissions` (#54), while the harvest above survives it.
"""

import pytest
from test_build_tool import FakeBackendTool
from test_invocation_receipts import (
    HASH_A,
    ReceiptOrchestrator,
    argv_contract_authority,
    receipts_written,
    sha256sum_output,
)

from sag.agent.invocation_receipts import RECEIPT_MODULE_OUTCOMES_MAX_ITEMS, build_receipt
from sag.agent.output_storage import OutputStorageManager
from sag.agent.physical_validator import PhysicalValidator
from sag.tools.build.build_tool import BuildTool
from sag.tools.internal.gradle_tool import GradleTool
from sag.tools.internal.maven_tool import MavenTool

GRADLE_REPORT = "/workspace/proj/build/test-results/test/TEST-a.xml"
SUREFIRE_REPORT = "/workspace/proj/target/surefire-reports/TEST-a.xml"

# kafka's own numbers, from the slice's `metadata.analysis.test_results`.
KAFKA_TOTAL = 13132
KAFKA_FAILED = 1
KAFKA_SKIPPED = 30


def _kafka_daemon_death_log(*, modules=1):
    """The shape of the stored 6.2 MB log: work, an aggregate, then the death."""
    lines = ["> Task :clients:compileJava"]
    for index in range(modules):
        lines.append(f"> Task :m{index:04d}:test")
    lines.extend(
        [
            "Gradle Test Run :clients:test > Executor 158 > ApiKeysTest > t() PASSED",
            f"{KAFKA_TOTAL} tests completed, {KAFKA_FAILED} failed, {KAFKA_SKIPPED} skipped",
            "",
            "FAILURE: Build failed with an exception.",
            "",
            "* What went wrong:",
            "Gradle build daemon disappeared unexpectedly "
            "(it may have been killed or may have crashed)",
            "",
            "> Task :stre",
        ]
    )
    return "\n".join(lines)


MAVEN_DEATH_LOG = "\n".join(
    [
        "[INFO] --- maven-surefire-plugin:3.5.5:test (default-test) @ demo ---",
        "[INFO] Tests run: 13132, Failures: 1, Errors: 0, Skipped: 30",
        "[ERROR] The forked VM terminated without properly saying goodbye",
    ]
)


class DetachedDeathOrchestrator(ReceiptOrchestrator):
    """A detached dispatch that COMPLETED in band and died doing it.

    `collect_detached_result` hands back the complete container log as
    `full_output` next to the bounded inline `output`, a non-zero exit code,
    and — when the process itself vanished — `lifecycle_state: "vanished"`.
    """

    def __init__(
        self,
        *args,
        full_output="",
        exit_code=1,
        lifecycle_state="finished",
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.full_output = full_output
        self.exit_code = exit_code
        self.lifecycle_state = lifecycle_state
        self.soft_timeout_calls = []

    def execute_command_with_soft_timeout(self, command, workdir=None, **kwargs):
        del kwargs
        self.soft_timeout_calls.append((command, workdir))
        return {
            "success": False,
            "runner_dispatched": True,
            "final_runner_dispatched": True,
            "exit_code": self.exit_code,
            "output": self.full_output[-2000:],
            "full_output": self.full_output,
            "termination_reason": None,
            "lifecycle_state": self.lifecycle_state,
            "dispatch_status": "completed_detached",
            "dispatch": {
                "job_id": "kafka1",
                "log_path": "/tmp/sag_jobs/kafka1.log",
                "exit_code_path": "/tmp/sag_jobs/kafka1.log.exit",
            },
        }


def _run_dying_gradle(tmp_path, orchestrator, storage=None):
    tool = GradleTool(orchestrator)
    tool.output_storage = storage or OutputStorageManager(tmp_path)
    with argv_contract_authority(
        executor="gradle", action="test", expected_argv="--build-cache test"
    ):
        return tool.execute(tasks="test", working_directory="/workspace/proj")


# ---------------------------------------------------------------------------
# the kafka shape, end to end
# ---------------------------------------------------------------------------


def test_a_detached_death_harvests_its_reports_and_keeps_its_crash(tmp_path):
    """The whole task in one shape: harvest, admit, and still seal the crash."""

    orchestrator = DetachedDeathOrchestrator(
        snapshots=["", sha256sum_output((GRADLE_REPORT, HASH_A))],
        build_system="gradle",
        full_output=_kafka_daemon_death_log(),
    )

    result = _run_dying_gradle(tmp_path, orchestrator)

    assert orchestrator.soft_timeout_calls, "the fence must exercise the dispatch path"

    # The crash stays sealed — nothing here invents success.
    assert result.succeeded is False
    assert result.error_code == "DETACHED_OPERATION_FAILED"
    assert result.metadata["exit_code"] == 1
    assert result.metadata["dispatch_status"] == "completed_detached"

    # The runner's own aggregate promotes out of `metadata.analysis` and into
    # the typed result the facade turns into facts.
    assert result.metadata["analysis"]["test_results"] == {
        "total": KAFKA_TOTAL,
        "failed": KAFKA_FAILED,
        "skipped": KAFKA_SKIPPED,
    }
    assert result.test_stats is not None
    assert result.test_stats.executed == KAFKA_TOTAL
    assert result.test_stats.failed == KAFKA_FAILED
    assert result.test_stats.skipped == KAFKA_SKIPPED
    assert result.test_stats.passed == KAFKA_TOTAL - KAFKA_FAILED - KAFKA_SKIPPED

    # The reports the dying run wrote are claimed by its receipt...
    (receipt,) = receipts_written(orchestrator.receipt_commands)
    assert receipt["exit_code"] == 1
    assert receipt["outcome"] == "failed"
    assert receipt["report_delta"]["new"] == [{"path": GRADLE_REPORT, "sha256": HASH_A}]
    assert "evidence_omissions" not in receipt
    assert result.metadata["receipt_id"] == receipt["receipt_id"]

    # ...and the physical counting path admits them exactly as for a clean run:
    # the projection reads the delta, never the outcome that sealed the death.
    claims = PhysicalValidator._verified_report_claims([receipt], "/workspace/proj")
    assert claims == {GRADLE_REPORT: [HASH_A]}
    clean_twin = build_receipt(
        **{
            key: receipt[key]
            for key in ("receipt_id", "run_id", "tool", "requested_action", "argv")
        },
        effective_action=receipt["effective_action"],
        working_directory=receipt["working_directory"],
        exit_code=0,
        before={},
        after={GRADLE_REPORT: HASH_A},
    )
    assert clean_twin["outcome"] == "completed"
    assert PhysicalValidator._verified_report_claims([clean_twin], "/workspace/proj") == claims


def test_the_facade_publishes_the_harvested_counts_as_facts(tmp_path, durable_tool_result_storage):
    """kafka's sealed result carried `facts: {}` next to a 13,132-test log.

    The facade turns the dispatch's typed `test_stats` into the facts the model
    and every downstream reader see; the harvest is what makes them non-empty.
    """

    orchestrator = DetachedDeathOrchestrator(
        snapshots=["", sha256sum_output((GRADLE_REPORT, HASH_A))],
        build_system="gradle",
        full_output=_kafka_daemon_death_log(),
    )
    dying = _run_dying_gradle(tmp_path, orchestrator, storage=durable_tool_result_storage)

    facade = BuildTool(orchestrator, gradle_tool=FakeBackendTool(result=dying))
    result = facade._envelope(
        dying,
        system="gradle",
        requested_verb="test",
        effective_verb="test",
        working_directory="/workspace/proj",
    )

    assert result.facts["executed"] == KAFKA_TOTAL
    assert result.facts["failed"] == KAFKA_FAILED
    assert result.facts["skipped"] == KAFKA_SKIPPED
    assert result.facts["passed"] == KAFKA_TOTAL - KAFKA_FAILED - KAFKA_SKIPPED
    # The counts adjudicate nothing: the dispatch's own word still stands.
    assert result.succeeded is False
    assert result.error_code == "DETACHED_OPERATION_FAILED"


def test_a_promoted_aggregate_never_rewrites_the_dispatch_outcome(tmp_path):
    """A harvested count is a fact about tests, not a verdict on the dispatch."""

    orchestrator = DetachedDeathOrchestrator(
        snapshots=["", sha256sum_output((GRADLE_REPORT, HASH_A))],
        build_system="gradle",
        full_output=_kafka_daemon_death_log(),
    )

    result = _run_dying_gradle(tmp_path, orchestrator)

    assert result.operation_outcome.value == "failed"
    assert result.error == "Detached operation failed"
    # The log's own aggregate is red; the classifier's conflict machinery is for
    # a runner that claimed SUCCESS while its tests failed, which this is not.
    assert result.conflicts == []


def test_the_stored_full_log_stays_referenced_from_the_dying_result(tmp_path):
    """The 6.2 MB log is the only place the 27,177 PASSED rows exist."""

    orchestrator = DetachedDeathOrchestrator(
        snapshots=["", sha256sum_output((GRADLE_REPORT, HASH_A))],
        build_system="gradle",
        full_output=_kafka_daemon_death_log(),
    )

    result = _run_dying_gradle(tmp_path, orchestrator)

    ref = result.metadata["output_ref_id"]
    assert ref
    assert result.output_ref == ref
    assert result.evidence_refs == [ref]


def test_a_dying_dispatch_that_wrote_no_reports_claims_none(tmp_path):
    """Harvesting is reading what is there, never manufacturing a claim."""

    orchestrator = DetachedDeathOrchestrator(
        snapshots=["", ""],
        build_system="gradle",
        full_output="\n".join(
            [
                "FAILURE: Build failed with an exception.",
                "* What went wrong:",
                "Gradle build daemon disappeared unexpectedly",
            ]
        ),
    )

    result = _run_dying_gradle(tmp_path, orchestrator)

    assert result.error_code == "DETACHED_OPERATION_FAILED"
    assert result.test_stats is None
    (receipt,) = receipts_written(orchestrator.receipt_commands)
    assert receipt["report_delta"] == {"new": [], "changed": []}
    assert PhysicalValidator._verified_report_claims([receipt], "/workspace/proj") == {}


# ---------------------------------------------------------------------------
# #54 interaction: a partial harvest says which field it could not carry
# ---------------------------------------------------------------------------


def test_a_partial_harvest_names_the_field_it_had_to_omit(tmp_path):
    """An over-cap module list is dropped ALONE: the delta, the exit code and
    the promoted aggregate all survive it, and the receipt says what is gone."""

    orchestrator = DetachedDeathOrchestrator(
        snapshots=["", sha256sum_output((GRADLE_REPORT, HASH_A))],
        build_system="gradle",
        full_output=_kafka_daemon_death_log(modules=RECEIPT_MODULE_OUTCOMES_MAX_ITEMS + 1),
    )

    result = _run_dying_gradle(tmp_path, orchestrator)

    assert result.error_code == "DETACHED_OPERATION_FAILED"
    assert result.test_stats.executed == KAFKA_TOTAL

    (receipt,) = receipts_written(orchestrator.receipt_commands)
    assert receipt["exit_code"] == 1
    assert receipt["report_delta"]["new"] == [{"path": GRADLE_REPORT, "sha256": HASH_A}]
    assert "module_outcomes" not in receipt
    (omission,) = receipt["evidence_omissions"]
    assert omission["field"] == "module_outcomes"
    assert omission["status"] == "unavailable"
    assert omission["reasons"]

    assert PhysicalValidator._verified_report_claims([receipt], "/workspace/proj") == {
        GRADLE_REPORT: [HASH_A]
    }


# ---------------------------------------------------------------------------
# the same death through the maven transport
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lifecycle_state", ("finished", "vanished"))
def test_a_dying_maven_dispatch_harvests_its_own_aggregate(tmp_path, lifecycle_state):
    """A vanished process takes the generic detached path; its surefire counts
    and its report delta are physical either way."""

    orchestrator = DetachedDeathOrchestrator(
        snapshots=["", sha256sum_output((SUREFIRE_REPORT, HASH_A))],
        full_output=MAVEN_DEATH_LOG,
        lifecycle_state=lifecycle_state,
    )
    tool = MavenTool(orchestrator)
    tool._record_test_summary = lambda *args, **kwargs: None
    tool.output_storage = OutputStorageManager(tmp_path)

    with argv_contract_authority(
        executor="maven", action="test", expected_argv="--fail-at-end test"
    ):
        result = tool.execute(
            command="test",
            fail_at_end=True,
            working_directory="/workspace/proj",
        )

    assert result.succeeded is False
    assert result.test_stats is not None
    assert result.test_stats.executed == KAFKA_TOTAL
    assert result.test_stats.failed == KAFKA_FAILED
    assert result.test_stats.skipped == KAFKA_SKIPPED

    (receipt,) = receipts_written(orchestrator.receipt_commands)
    assert receipt["exit_code"] == 1
    assert receipt["report_delta"]["new"] == [{"path": SUREFIRE_REPORT, "sha256": HASH_A}]
    assert PhysicalValidator._verified_report_claims([receipt], "/workspace/proj") == {
        SUREFIRE_REPORT: [HASH_A]
    }
