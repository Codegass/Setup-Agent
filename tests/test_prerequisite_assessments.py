"""Structured prerequisite observations remain evidence, never prescriptions."""

from types import SimpleNamespace

import sag.agent.evidence_assessments as assessments_module
import sag.tools.build.build_tool as build_tool_module
from sag.agent.action_intents import action_fingerprint
from sag.agent.evidence_assessments import (
    PREREQUISITE_EXECUTABLE_MISSING,
    PREREQUISITE_FINDING_CAP,
    PREREQUISITE_SERVICE_UNAVAILABLE,
    assess_dispatch,
    prerequisite_assessments,
)
from sag.agent.invocation_contracts import ARGV_EXECUTION_BINDING, build_contract
from sag.tools.build.build_tool import BuildTool


def receipt(**overrides):
    value = {
        "schema_version": 2,
        "receipt_id": "inv-gradle-test-0001",
        "tool": "gradle",
        "requested_action": "test",
        "effective_action": "test",
        "argv": "./gradlew test",
        "working_directory": "/workspace/project/integration",
        "actual_cwd": "/workspace/project/integration",
        "domain_id": "/workspace/project",
        "exit_code": 1,
        "outcome": "failed",
        "report_delta": {"new": [], "changed": []},
        "compliance": "exact",
    }
    value.update(overrides)
    return value


# A live build dispatch is assessable only when the frozen contract carries
# the complete current-authority pin tuple; the assessor otherwise reports
# `contract_binding_unknown` instead of grading the receipt.
CONTRACT_PINS = {
    "target_sha": "a" * 40,
    "survey_fingerprint": "sf-prereq",
    "config_fingerprint": "cf-prereq",
    "document_map_fingerprint": "dm-prereq",
    "domain_id": "/workspace/project",
    "fact_epoch": 1,
}


def contract():
    params = {
        "action": "test",
        "working_directory": "/workspace/project/integration",
    }
    domain = "test:/workspace/project/integration"
    return build_contract(
        run_id="run-prerequisite-assessment",
        envelope_id="envelope-prerequisite-assessment",
        tool="build",
        params=params,
        effective_tool="gradle",
        effective_action="test",
        expected_cwd="/workspace/project/integration",
        execution_binding=ARGV_EXECUTION_BINDING,
        expected_argv="test",
        intent_source="model",
        intent_id="intent-prerequisite-assessment",
        intent_domain_id=domain,
        intent_exact_params=params,
        action_fingerprint=action_fingerprint(
            domain_id=domain,
            tool="build",
            params=params,
        ),
        **CONTRACT_PINS,
        expected_observations=("report_delta",),
        direct_falsifiers=(
            {"predicate_id": "empty_delta_despite_success", "kind": "delta_empty_on_exit0"},
        ),
    )


def failed(reason, node_id="integration.CassandraTest#startsCluster"):
    return {
        "testcase_outcomes": {"nodes": [{"node_id": node_id, "status": "failed", "reason": reason}]}
    }


def test_ccm_command_not_found_becomes_a_structured_unknown_owner_rider():
    (finding,) = prerequisite_assessments(receipt(**failed("ccm: command not found")))

    assert finding.typed_code == PREREQUISITE_EXECUTABLE_MISSING
    assert finding.owner.value == "unknown"
    assert finding.payload() == {
        "schema_version": 2,
        "assessment_id": finding.assessment_id,
        "receipt_id": "inv-gradle-test-0001",
        "typed_code": PREREQUISITE_EXECUTABLE_MISSING,
        "blocker_owner": "unknown",
        "detail": "testcase integration.CassandraTest#startsCluster reported missing executable ccm",
        "name": "ccm",
        "scope": "/workspace/project",
        "evidence_ref": "inv-gradle-test-0001",
    }


def test_posix_shell_not_found_names_the_inner_command():
    (finding,) = prerequisite_assessments(receipt(**failed("/bin/sh: 1: ccm: not found")))

    assert finding.name == "ccm"
    assert finding.typed_code == PREREQUISITE_EXECUTABLE_MISSING


def test_generic_no_such_executable_forms_name_the_binary_not_the_stacktrace():
    after = prerequisite_assessments(
        receipt(),
        'RuntimeError: No such executable: "protoc"',
        evidence_ref="output_deadbeef",
    )
    before = prerequisite_assessments(
        receipt(),
        "different adapter wording\nprotoc: No such executable",
        evidence_ref="output_deadbeef",
    )

    assert [finding.name for finding in after] == ["protoc"]
    assert [finding.name for finding in before] == ["protoc"]
    assert after[0].assessment_id == before[0].assessment_id
    assert after[0].evidence_ref == "output_deadbeef"


def test_java_cannot_run_program_is_an_executable_observation_not_a_file_guess():
    (finding,) = prerequisite_assessments(
        receipt(),
        'java.io.IOException: Cannot run program "/opt/tools/ccm": error=2, '
        "No such file or directory",
    )

    assert finding.name == "ccm"
    assert finding.typed_code == PREREQUISITE_EXECUTABLE_MISSING


def test_connection_refused_records_endpoint_and_only_an_explicit_service_hint():
    (finding,) = prerequisite_assessments(
        receipt(),
        "Cassandra service at localhost:9042: Connection refused",
        evidence_ref="output_cafe1234",
    )

    payload = finding.payload()
    assert payload["typed_code"] == PREREQUISITE_SERVICE_UNAVAILABLE
    assert payload["blocker_owner"] == "unknown"
    assert payload["endpoint"] == "localhost:9042"
    assert payload["service_hint"] == "cassandra"
    assert payload["scope"] == "/workspace/project"
    assert payload["evidence_ref"] == "output_cafe1234"


def test_cassandra_battery_refusal_shape_normalizes_the_slashed_ipv4_endpoint():
    (finding,) = prerequisite_assessments(
        receipt(),
        "Suppressed: io.netty.channel.AbstractChannel$AnnotatedConnectException: "
        "Connection refused: /127.0.0.1:9042",
    )

    assert finding.endpoint == "127.0.0.1:9042"
    assert finding.service_hint is None


def test_port_and_project_name_never_invent_a_service_hint():
    cassandra_named_receipt = receipt(
        domain_id="/workspace/cassandra-java-driver",
        working_directory="/workspace/cassandra-java-driver",
        actual_cwd="/workspace/cassandra-java-driver",
    )

    (finding,) = prerequisite_assessments(
        cassandra_named_receipt,
        "connect localhost:9042 failed: Connection refused",
    )

    assert finding.endpoint == "localhost:9042"
    assert finding.service_hint is None
    assert "service_hint" not in finding.payload()


def test_complete_output_can_enrich_a_receipt_endpoint_only_with_an_explicit_hint():
    observed = receipt(
        **failed(
            "localhost:9042: Connection refused",
            node_id="integration.ClusterTest#connects",
        )
    )

    (finding,) = prerequisite_assessments(
        observed,
        "service=cassandra endpoint=localhost:9042 Connection refused",
        evidence_ref="output_1234abcd",
    )

    assert finding.endpoint == "localhost:9042"
    assert finding.service_hint == "cassandra"
    assert finding.evidence_ref == "output_1234abcd"


def test_executable_and_service_riders_coexist_without_waiving_the_red_result(monkeypatch):
    recorded = []

    def land(_execute, assessment):
        recorded.append(assessment.payload())
        return True

    monkeypatch.setattr(assessments_module, "write_assessment", land)
    frozen = contract()
    observed = receipt(
        **failed("setup failed: ccm: command not found"),
        run_id=frozen["run_id"],
        contract_id=frozen["contract_id"],
        contract_hash=frozen["contract_hash"],
        execution_binding=frozen["execution_binding"],
        # The receipt restates the contract's frozen pin tuple exactly.
        **{key: value for key, value in CONTRACT_PINS.items() if key != "domain_id"},
    )

    landed = assess_dispatch(
        lambda _command: {},
        contract=frozen,
        receipt=observed,
        current_fingerprints=dict(CONTRACT_PINS),
        output="localhost:9042 refused the connection: Connection refused",
        evidence_ref="output_c001d00d",
    )

    assert [finding.typed_code for finding in landed] == [
        "expectation_unmet",
        PREREQUISITE_EXECUTABLE_MISSING,
        PREREQUISITE_SERVICE_UNAVAILABLE,
    ]
    assert [payload["typed_code"] for payload in recorded] == [
        "expectation_unmet",
        PREREQUISITE_EXECUTABLE_MISSING,
        PREREQUISITE_SERVICE_UNAVAILABLE,
    ]
    assert recorded[2]["evidence_ref"] == "output_c001d00d"


def test_build_facade_binds_output_derived_findings_to_the_durable_output(monkeypatch):
    observed_receipt = receipt(receipt_id="inv-gradle-test-0099")
    captured = {}

    monkeypatch.setattr(
        build_tool_module,
        "read_receipt",
        lambda _execute, _receipt_id: observed_receipt,
    )

    def capture(_execute, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(build_tool_module, "assess_dispatch", capture)
    orchestrator = SimpleNamespace(execute_command=lambda *_args, **_kwargs: {})
    tool = object.__new__(BuildTool)
    tool.docker_orchestrator = orchestrator
    result = SimpleNamespace(
        output="ccm: command not found",
        raw_output="ccm: command not found",
        output_ref="output_c001d00d",
        error_code="BUILD_FAILED",
        invocation_status=SimpleNamespace(value="completed"),
        metadata={"receipt_id": "inv-gradle-test-0099"},
    )

    tool._assess_receipts(
        {},
        [SimpleNamespace(result=result)],
        [{"contract_id": "ic-000000000001"}],
    )

    assert captured["output"] == "ccm: command not found"
    assert captured["evidence_ref"] == "output_c001d00d"


def test_stacktrace_prose_and_service_hint_do_not_change_endpoint_identity():
    first = prerequisite_assessments(
        receipt(), "Cassandra service localhost:9042 Connection refused"
    )[0]
    second = prerequisite_assessments(
        receipt(), "adapter.ConnectionRefusedError at localhost:9042"
    )[0]

    assert first.endpoint == second.endpoint == "localhost:9042"
    assert first.assessment_id == second.assessment_id
    assert first.service_hint == "cassandra"
    assert second.service_hint is None


def test_only_failed_or_error_testcases_can_emit_prerequisite_riders():
    observed = receipt(
        testcase_outcomes={
            "nodes": [
                {"node_id": "a", "status": "passed", "reason": "ccm: command not found"},
                {"node_id": "b", "status": "skipped", "reason": "ccm: command not found"},
            ]
        }
    )

    assert prerequisite_assessments(observed) == []


def test_refusal_without_an_endpoint_states_no_service_fact():
    assert prerequisite_assessments(receipt(), "Connection refused") == []


def test_complete_output_scan_is_bounded_and_still_reaches_a_late_failure():
    output = ("ordinary build output\n" * 100_000) + ("x" * 10_000) + " ccm: command not found\n"

    (finding,) = prerequisite_assessments(receipt(), output)

    assert finding.name == "ccm"
    assert len(finding.detail) <= 200


def test_findings_are_bounded_and_never_contain_an_action_prescription():
    output = "\n".join(f"tool{index}: command not found" for index in range(100))

    findings = prerequisite_assessments(receipt(), output)

    assert len(findings) == PREREQUISITE_FINDING_CAP
    assert len({finding.assessment_id for finding in findings}) == len(findings)
    for finding in findings:
        payload = finding.payload()
        assert not ({"command", "args", "suggestions", "install", "start"} & payload.keys())
