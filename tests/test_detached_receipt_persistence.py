# tests/test_detached_receipt_persistence.py
"""Task #54: a detached test/build receipt persists its terminal facts.

Live camel and kafka both dispatched detached, both COMPLETED in band
(`dispatch_status: "completed_detached"`, which is deliberately NOT a member
of `DETACHED_HANDOFF_STATUSES`), and both lost their receipt entirely — a
bare `invalid_arguments` from `write_receipt_result`.

What is different about the detached shape is one key. `collect_detached_result`
returns the COMPLETE container log as `full_output` next to the bounded inline
`output`, while the synchronous monitor returns only a clipped `output`. The
producers read `full_output or output`, so the same parser sees a 9 KB log in
band and a 2.3 MB log on completion:

* camel: 652 reactor rows instead of 31, over a 256-item receipt bound;
* kafka: a JUnit display name whose `&#10;` the report parser unescapes, so the
  diagnostic `node_id` carried raw newlines.

Neither is an integrity fact. An exit code, an argv, a contract binding and a
report delta are; an over-cap or unclean OBSERVABILITY field must never take
them down with it, and when a field is dropped the receipt must say which one
and why.
"""

import pytest
from test_container_io import FakeContainer
from test_invocation_receipts import (
    FakeExecute,
    HASH_A,
    ReceiptOrchestrator,
    SUREFIRE,
    argv_contract_authority,
    maven_tool,
    minimal_valid_receipt,
    receipts_written,
    sha256sum_output,
)

from sag.agent.invocation_receipts import (
    RECEIPT_MODULE_OUTCOMES_MAX_ITEMS,
    build_receipt,
    validate_receipt_v2,
    write_receipt_result,
)
from sag.agent.physical_validator import PhysicalValidator
from sag.agent.receipt_test_rows import diagnostic_testcase_outcomes
from sag.agent.output_storage import OutputStorageManager

# camel's own reactor, measured on the stored detached log `output_3dff08155101`:
# 652 rows through the full-output parser, 31 through the truncated one.
CAMEL_REACTOR_ROWS = 652


def _receipt(**overrides):
    payload = {
        "receipt_id": "inv-maven-1-detached-0001",
        "run_id": "run-pytest",
        "tool": "maven",
        "requested_action": "test",
        "effective_action": "test",
        "argv": "mvn test",
        "working_directory": "/workspace/proj",
        "exit_code": 0,
        "before": {},
        "after": {SUREFIRE: HASH_A},
    }
    payload.update(overrides)
    return build_receipt(**payload)


def _reactor(count, *, prefix="camel"):
    return [{"module": f"{prefix}-{index:04d}", "status": "success"} for index in range(count)]


# ---------------------------------------------------------------------------
# camel: the detached log's own reactor is representable
# ---------------------------------------------------------------------------


def test_a_full_reactor_module_list_is_representable_on_a_receipt():
    """652 rows is a real reactor, not an attack. 256 was never that bound."""

    modules = _reactor(CAMEL_REACTOR_ROWS)

    receipt = _receipt(module_outcomes=modules)

    assert receipt["module_outcomes"] == modules
    assert "evidence_omissions" not in receipt
    assert validate_receipt_v2(receipt)["module_outcomes"] == modules
    assert write_receipt_result(FakeContainer(), receipt).persisted is True


def test_the_module_bound_is_a_reactor_bound_not_the_generic_sequence_bound():
    assert RECEIPT_MODULE_OUTCOMES_MAX_ITEMS > CAMEL_REACTOR_ROWS


# ---------------------------------------------------------------------------
# kafka: the diagnostic node id is sanitized where it is built
# ---------------------------------------------------------------------------


def test_a_diagnostic_node_id_is_sanitized_exactly_like_its_reason():
    """The in-container parser unescapes `&#10;`; the row builder must not
    carry that into an identifier the receipt validator refuses."""

    parsed = {
        "status": "complete",
        "rows": [
            {
                "classname": "kafka.api\r\nPlaintextConsumerTest",
                "name": "testCoordinatorFailover\nwhen the broker dies",
                "outcome": "failed",
                "reason": "  timed   out  ",
            }
        ],
    }

    diagnostic = diagnostic_testcase_outcomes(parsed)

    assert diagnostic["nodes"] == [
        {
            "node_id": "kafka.api PlaintextConsumerTest#testCoordinatorFailover when the broker dies",
            "status": "failed",
            "reason": "timed out",
        }
    ]
    receipt = _receipt(testcase_outcomes=diagnostic)
    assert receipt["testcase_outcomes"] == diagnostic
    assert "evidence_omissions" not in receipt
    assert validate_receipt_v2(receipt)["testcase_outcomes"] == diagnostic


# ---------------------------------------------------------------------------
# the family fix: an unrepresentable OPTIONAL field states why it is absent
# ---------------------------------------------------------------------------


def test_unrepresentable_optional_evidence_never_voids_the_terminal_facts():
    """Both live payloads, pushed past every widened bound at once."""

    receipt = _receipt(
        module_outcomes=_reactor(RECEIPT_MODULE_OUTCOMES_MAX_ITEMS + 1),
        testcase_outcomes={"nodes": [{"node_id": "kafka.T#dies\nhere", "status": "failed"}]},
        capability_observations=[{"feature": "toolchains", "probe": "mvn -v\nsecond line"}],
    )

    assert receipt["exit_code"] == 0
    assert receipt["outcome"] == "completed"
    assert receipt["argv"] == "mvn test"
    assert receipt["report_delta"]["new"] == [{"path": SUREFIRE, "sha256": HASH_A}]
    for field in ("module_outcomes", "testcase_outcomes", "capability_observations"):
        assert field not in receipt

    omissions = {entry["field"]: entry for entry in receipt["evidence_omissions"]}
    assert set(omissions) == {
        "module_outcomes",
        "testcase_outcomes",
        "capability_observations",
    }
    for field, entry in omissions.items():
        assert entry["status"] == "unavailable"
        assert entry["reasons"] and all(reason.strip() for reason in entry["reasons"])

    container = FakeContainer()
    assert write_receipt_result(container, receipt).persisted is True
    (persisted,) = receipts_written(container.commands)
    assert persisted["evidence_omissions"] == receipt["evidence_omissions"]


def test_an_omission_may_not_contradict_a_field_the_receipt_carries():
    receipt = _receipt(module_outcomes=_reactor(3))
    forged = {
        **receipt,
        "evidence_omissions": [
            {
                "field": "module_outcomes",
                "status": "unavailable",
                "reasons": ["receipt module_outcomes is invalid"],
            }
        ],
    }

    with pytest.raises(ValueError):
        validate_receipt_v2(forged)


def test_an_omission_must_state_a_reason():
    receipt = _receipt()
    forged = {
        **receipt,
        "evidence_omissions": [
            {"field": "module_outcomes", "status": "unavailable", "reasons": []}
        ],
    }

    with pytest.raises(ValueError):
        validate_receipt_v2(forged)


# ---------------------------------------------------------------------------
# a refusal names the argument
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,broken",
    (
        ("report_delta", {"report_delta": {"new": "truthy", "changed": []}}),
        ("module_outcomes", {"module_outcomes": [{"module": "camel-core"}]}),
        (
            "testcase_outcomes",
            {"testcase_outcomes": {"nodes": [{"node_id": "a\nb", "status": "failed"}]}},
        ),
        ("capability_observations", {"capability_observations": [{"feature": "x"}]}),
    ),
)
def test_a_receipt_refusal_names_the_argument_that_caused_it(field, broken):
    """`invalid_arguments` alone cannot be acted on: camel and kafka failed for
    two different reasons and reported the same word."""

    execute = FakeExecute()

    result = write_receipt_result(execute, {**minimal_valid_receipt("inv-maven-x-0001"), **broken})

    assert result.persisted is False
    assert result.code == f"invalid_arguments:{field}"
    assert execute.commands == []


# ---------------------------------------------------------------------------
# the round trip: dispatch -> detached completion -> receipt -> counting
# ---------------------------------------------------------------------------


class DetachedReceiptOrchestrator(ReceiptOrchestrator):
    """A dispatch that COMPLETES in band, the way orch does after a poll.

    `collect_detached_result` hands back the complete container log as
    `full_output` next to the bounded inline `output`; the producers read
    `full_output or output`, which is exactly what widened camel's reactor
    parse from 31 rows to 652.
    """

    def __init__(self, *args, full_output="", **kwargs):
        super().__init__(*args, **kwargs)
        self.full_output = full_output
        self.soft_timeout_calls = []

    def execute_command_with_soft_timeout(self, command, workdir=None, **kwargs):
        del kwargs
        self.soft_timeout_calls.append((command, workdir))
        return {
            "success": True,
            "runner_dispatched": True,
            "final_runner_dispatched": True,
            "exit_code": 0,
            "output": self.full_output[:10000],
            "full_output": self.full_output,
            "termination_reason": None,
            "lifecycle_state": "finished",
            "dispatch_status": "completed_detached",
            "dispatch": {
                "job_id": "camel1",
                "log_path": "/tmp/sag_jobs/camel1.log",
                "exit_code_path": "/tmp/sag_jobs/camel1.log.exit",
            },
        }


def _camel_detached_log(rows):
    lines = [
        "[INFO] Reactor Summary:",
        *(
            f"[INFO] camel-{index:04d} ......................... SUCCESS [  0.1 s]"
            for index in range(rows)
        ),
        "[INFO] Tests run: 11492, Failures: 0, Errors: 0, Skipped: 0",
        "[INFO] BUILD SUCCESS",
    ]
    return "\n".join(lines)


def test_a_completed_detached_maven_dispatch_persists_a_countable_receipt(tmp_path):
    orchestrator = DetachedReceiptOrchestrator(
        snapshots=["", sha256sum_output((SUREFIRE, HASH_A))],
        full_output=_camel_detached_log(CAMEL_REACTOR_ROWS),
    )
    tool = maven_tool(orchestrator)
    tool.output_storage = OutputStorageManager(tmp_path)

    with argv_contract_authority(
        executor="maven", action="test", expected_argv="--fail-at-end test"
    ):
        result = tool.execute(
            command="test",
            fail_at_end=True,
            working_directory="/workspace/proj",
        )

    assert orchestrator.soft_timeout_calls, "the fence must exercise the dispatch path"
    assert result.succeeded is True

    (receipt,) = receipts_written(orchestrator.receipt_commands)
    assert receipt["exit_code"] == 0
    assert receipt["outcome"] == "completed"
    assert receipt["report_delta"]["new"] == [{"path": SUREFIRE, "sha256": HASH_A}]
    assert len(receipt["module_outcomes"]) == CAMEL_REACTOR_ROWS
    assert "evidence_omissions" not in receipt
    assert result.metadata["receipt_id"] == receipt["receipt_id"]
    assert "receipt_persisted" not in result.metadata

    # Counting admits the reports this detached dispatch claimed.
    claims = PhysicalValidator._verified_report_claims([receipt], "/workspace/proj")
    assert claims == {SUREFIRE: [HASH_A]}


def test_a_completed_detached_dispatch_keeps_its_receipt_when_evidence_is_unclean(tmp_path):
    """The whole task in one shape: the terminal facts survive, and the
    receipt states which observability field it could not carry."""

    log = _camel_detached_log(RECEIPT_MODULE_OUTCOMES_MAX_ITEMS + 1)
    orchestrator = DetachedReceiptOrchestrator(
        snapshots=["", sha256sum_output((SUREFIRE, HASH_A))],
        full_output=log,
    )
    tool = maven_tool(orchestrator)
    tool.output_storage = OutputStorageManager(tmp_path)

    with argv_contract_authority(
        executor="maven", action="test", expected_argv="--fail-at-end test"
    ):
        tool.execute(command="test", fail_at_end=True, working_directory="/workspace/proj")

    (receipt,) = receipts_written(orchestrator.receipt_commands)
    assert receipt["exit_code"] == 0
    assert receipt["report_delta"]["new"] == [{"path": SUREFIRE, "sha256": HASH_A}]
    assert "module_outcomes" not in receipt
    (omission,) = receipt["evidence_omissions"]
    assert omission["field"] == "module_outcomes"
    assert omission["status"] == "unavailable"
    assert omission["reasons"]
