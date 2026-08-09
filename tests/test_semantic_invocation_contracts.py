"""Schema-v2 invocation authority and semantic-assessment regressions."""

import json
import shlex

import pytest
from test_container_io import FakeContainer

from sag.agent.action_intents import action_fingerprint
from sag.agent.claim_graph import commit_assessment_transitions
from sag.agent.control_events import canonical_json
from sag.agent.control_ownership import BlockerOwner
from sag.agent.evidence_assessments import (
    CONTRACT_BINDING_UNKNOWN,
    EXPECTATION_MET,
    EXPECTATION_UNOBSERVED,
    assess_receipt,
    read_receipt,
)
from sag.agent.invocation_contracts import (
    ARGV_EXECUTION_BINDING,
    CONTRACT_DIR,
    PYTHON_FACADE_EXECUTION_BINDING,
    build_contract,
    contract_hash,
    contract_identity,
    contract_identity_v2,
    current_contract,
    dispatch_contract,
    live_contract_valid,
    python_facade_dispatch_matches,
    read_frozen_contract,
    write_contract,
)
from sag.agent.invocation_receipts import (
    RECEIPT_MAX_RAW_BYTES,
    normalize_producer_observations,
    producer_observations_sha256,
    validate_receipt_v2,
    write_receipt_result,
)
from sag.agent.evidence_publications import (
    install_evidence_publication_authority,
    reset_evidence_publication_authority,
    unavailable_evidence_publication_authority,
)

ROOT = "/workspace/proj"
RUN = "run-pytest"
SHA = "a" * 64
CURRENT = {
    "target_sha": SHA,
    "survey_fingerprint": "survey-1",
    "config_fingerprint": "cfg-1",
    "document_map_fingerprint": "map-1",
    "domain_id": ROOT,
    "fact_epoch": 1,
    "python_import_targets": ["demo"],
    "python_import_targets_sha256": producer_observations_sha256(["demo"]),
}


def contract_for(
    *,
    action="test",
    effective_tool="maven",
    effective_action=None,
    binding=ARGV_EXECUTION_BINDING,
    expected_argv="--fail-at-end verify",
    intent_id="intent-semantic-1",
    params=None,
    **overrides,
):
    exact = dict(params or {"action": action, "working_directory": ROOT})
    domain = "test:" + ROOT
    values = {
        "run_id": RUN,
        "envelope_id": "envelope-semantic-1",
        "tool": "build",
        "params": exact,
        "effective_tool": effective_tool,
        "effective_action": effective_action or action,
        "expected_cwd": ROOT,
        "execution_binding": binding,
        "expected_argv": expected_argv,
        "intent_source": "model",
        "intent_id": intent_id,
        "intent_domain_id": domain,
        "intent_exact_params": exact,
        "action_fingerprint": action_fingerprint(
            domain_id=domain,
            tool="build",
            params=exact,
        ),
        "target_sha": SHA,
        "survey_fingerprint": "survey-1",
        "config_fingerprint": "cfg-1",
        "document_map_fingerprint": "map-1",
        "domain_id": ROOT,
        "fact_epoch": 1,
    }
    values.update(overrides)
    return build_contract(**values)


def receipt_for(contract, **overrides):
    receipt = {
        "schema_version": 2,
        "receipt_id": "inv-maven-semantic-0001",
        "run_id": contract["run_id"],
        "tool": contract["effective_tool"],
        "requested_action": contract["effective_action"],
        "effective_action": contract["effective_action"],
        "argv": "mvn --fail-at-end verify",
        "working_directory": ROOT,
        "actual_cwd": ROOT,
        "exit_code": 0,
        "outcome": "completed",
        "report_delta": {"new": [], "changed": []},
        "contract_id": contract["contract_id"],
        "contract_hash": contract["contract_hash"],
        "execution_binding": contract["execution_binding"],
        "target_sha": SHA,
        "survey_fingerprint": "survey-1",
        "config_fingerprint": "cfg-1",
        "document_map_fingerprint": "map-1",
        "domain_id": ROOT,
        "fact_epoch": 1,
    }
    if contract["execution_binding"] == ARGV_EXECUTION_BINDING:
        receipt["compliance"] = "exact"
    receipt.update(overrides)
    return receipt


def test_strict_receipt_v2_rejects_unknown_and_malformed_nested_shapes():
    contract = contract_for()
    valid = receipt_for(contract)
    assert validate_receipt_v2(valid, expected_id=valid["receipt_id"]) == valid

    malformed = [
        {**valid, "future_semantics": True},
        {**valid, "report_delta": {"new": "truthy", "changed": []}},
        {
            **valid,
            "testcase_execution_rows": {
                "schema_version": 2,
                "status": "complete",
                "report_count": 1,
                "rows": [{"outcome": "garbage"}],
            },
        },
    ]
    for candidate in malformed:
        with pytest.raises(ValueError):
            validate_receipt_v2(candidate, expected_id=valid["receipt_id"])


def test_receipt_reader_rejects_path_traversal_duplicate_keys_and_filename_mismatch():
    calls = []

    def execute(command):
        calls.append(command)
        return {"success": False, "exit_code": 1, "output": ""}

    assert read_receipt(execute, "../../run-pin") is None
    assert calls == []

    valid = receipt_for(contract_for())
    encoded = json.dumps(valid, separators=(",", ":"))
    duplicate = encoded[:-1] + ',"receipt_id":"inv-other-0001"}'
    assert read_receipt(
        lambda _command: {"success": True, "exit_code": 0, "output": duplicate},
        valid["receipt_id"],
    ) is None
    assert read_receipt(
        lambda _command: {"success": True, "exit_code": 0, "output": encoded},
        "inv-different-0001",
    ) is None


def test_receipt_reader_and_writer_enforce_transport_bounds_and_same_validator():
    valid = receipt_for(contract_for())
    oversized = "{" + "x" * RECEIPT_MAX_RAW_BYTES + "}"
    assert read_receipt(
        lambda _command: {"success": True, "exit_code": 0, "output": oversized},
        valid["receipt_id"],
    ) is None

    calls = []
    malformed = {**valid, "report_delta": {"new": "truthy", "changed": []}}
    result = write_receipt_result(lambda command: calls.append(command), malformed)
    assert result.persisted is False
    assert result.code == "invalid_arguments"
    assert calls == []


def test_v2_identity_binds_intent_run_call_executor_and_runtime():
    first = contract_for()
    assert contract_for(intent_id="intent-semantic-2")["contract_id"] != first["contract_id"]
    assert contract_for(run_id="run-other")["contract_id"] != first["contract_id"]
    assert contract_for(effective_tool="gradle")["contract_id"] != first["contract_id"]
    changed = {"action": "test", "working_directory": ROOT, "args": "-Dslow=true"}
    assert contract_for(params=changed)["contract_id"] != first["contract_id"]
    assert contract_for(effective_jdk={"major": "17"})["contract_id"] != first["contract_id"]
    assert contract_for(survey_fingerprint="survey-2")["contract_id"] != first["contract_id"]


def test_schema_v2_unknown_field_cannot_become_live_after_rehashing():
    contract = contract_for()
    tampered = {**contract, "semantic_effects": ["trust-me"]}
    tampered["contract_hash"] = contract_hash(tampered)

    assert live_contract_valid(tampered) is False
    with dispatch_contract(tampered):
        assert current_contract() is None


def _rehash_live_candidate(contract, **changes):
    candidate = {**contract, **changes}
    candidate["contract_id"] = contract_identity_v2(candidate)
    candidate["contract_hash"] = contract_hash(candidate)
    return candidate


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", 7),
        ("target_sha", 7),
        ("target_sha", ""),
        ("fact_epoch", True),
        ("fact_epoch", -1),
        ("domain_id", [ROOT]),
        ("effective_tool", "MAVEN"),
        ("effective_action", 7),
        ("expected_cwd", "workspace/proj"),
        ("expected_argv", 7),
        ("expected_observations", "report_delta"),
        ("expected_observations", ["not_a_registered_observation"]),
        ("direct_falsifiers", {"kind": "delta_empty_on_exit0"}),
        ("supporting_claim_ids", "claim-1"),
        ("supporting_claim_ids", ["claim-1", "claim-1"]),
        ("blocking_conflict_ids", {"edge": "blocked"}),
    ],
)
def test_rehashed_v2_contract_rejects_noncanonical_field_shapes(field, value):
    candidate = _rehash_live_candidate(contract_for(), **{field: value})

    assert live_contract_valid(candidate) is False


def test_rehashed_v2_contract_rejects_noncanonical_requested_tool():
    contract = contract_for()
    requested = {**contract["requested_call"], "tool": "BUILD"}
    candidate = _rehash_live_candidate(contract, requested_call=requested)

    assert live_contract_valid(candidate) is False


def test_contract_derives_registered_expectations_and_rejects_caller_substitution():
    contract = contract_for()
    assert contract["expected_observations"] == ["report_delta"]
    assert contract["direct_falsifiers"] == [
        {"predicate_id": "empty_delta_despite_success", "kind": "delta_empty_on_exit0"}
    ]

    with pytest.raises(ValueError, match="expected_observations"):
        contract_for(expected_observations=["artifact_or_report_delta"])
    with pytest.raises(ValueError, match="direct_falsifiers"):
        contract_for(
            direct_falsifiers=[
                {"predicate_id": "model_selected", "kind": "delta_empty_on_exit0"}
            ]
        )


def test_projection_pins_change_identity_while_nonprojection_pins_hit_cas_collision():
    first = contract_for()
    changed_survey = contract_for(survey_fingerprint="survey-2")
    changed_config = contract_for(config_fingerprint="cfg-2")

    assert changed_survey["contract_id"] != first["contract_id"]
    assert changed_config["contract_id"] == first["contract_id"]
    assert changed_config["contract_hash"] != first["contract_hash"]

    store = ContractStore()
    assert write_contract(store.execute_command, first) is True
    assert write_contract(store.execute_command, changed_config) is False


@pytest.mark.parametrize("executor", ["shell", "unknown"])
def test_argv_binding_rejects_non_allowlisted_executor(executor):
    with pytest.raises(ValueError, match="allowlisted executor"):
        contract_for(effective_tool=executor)


def test_python_cannot_bypass_its_semantic_binding_with_argv_v1():
    with pytest.raises(ValueError, match="python_facade_v1"):
        contract_for(effective_tool="python")


def test_python_binding_is_explicit_and_forbids_argv_or_ignored_params():
    params = {"action": "compile", "working_directory": ROOT}
    contract = contract_for(
        action="compile",
        effective_tool="python",
        effective_action="compile",
        binding=PYTHON_FACADE_EXECUTION_BINDING,
        expected_argv=None,
        params=params,
    )
    assert contract["execution_binding"] == PYTHON_FACADE_EXECUTION_BINDING
    assert "expected_argv" not in contract

    with pytest.raises(ValueError, match="forbids"):
        contract_for(
            action="compile",
            effective_tool="python",
            effective_action="compile",
            binding=PYTHON_FACADE_EXECUTION_BINDING,
            expected_argv="python -m compileall",
            params=params,
        )
    with pytest.raises(ValueError, match="does not consume"):
        contract_for(
            action="compile",
            effective_tool="python",
            effective_action="compile",
            binding=PYTHON_FACADE_EXECUTION_BINDING,
            expected_argv=None,
            params={**params, "args": "ignored"},
        )


def test_python_deps_target_is_disabled_even_with_an_opaque_supporting_claim_id():
    params = {
        "action": "deps",
        "working_directory": ROOT,
        "args": "numpy==1.26.4",
    }
    with pytest.raises(ValueError, match="typed claim authority"):
        contract_for(
            action="deps",
            effective_tool="python",
            effective_action="setup_env",
            binding=PYTHON_FACADE_EXECUTION_BINDING,
            expected_argv=None,
            params=params,
            supporting_claim_ids=["dependency-forged0001"],
        )


def test_python_null_timeout_materializes_to_default_but_explicit_timeout_stays_exact():
    base = {"action": "test", "working_directory": ROOT, "timeout": None}
    defaulted = contract_for(
        action="test",
        effective_tool="python",
        effective_action="test",
        binding=PYTHON_FACADE_EXECUTION_BINDING,
        expected_argv=None,
        params=base,
    )
    internal = {
        "operation": "test",
        "working_directory": ROOT,
        "timeout": 600,
    }
    assert python_facade_dispatch_matches(
        defaulted,
        operation="test",
        working_directory=ROOT,
        internal_params=internal,
    )
    assert not python_facade_dispatch_matches(
        defaulted,
        operation="test",
        working_directory=ROOT,
        internal_params={**internal, "timeout": 601},
    )

    explicit_params = {"action": "test", "working_directory": ROOT, "timeout": 37}
    explicit = contract_for(
        action="test",
        effective_tool="python",
        effective_action="test",
        binding=PYTHON_FACADE_EXECUTION_BINDING,
        expected_argv=None,
        params=explicit_params,
    )
    assert python_facade_dispatch_matches(
        explicit,
        operation="test",
        working_directory=ROOT,
        internal_params={**internal, "timeout": 37},
    )
    assert not python_facade_dispatch_matches(
        explicit,
        operation="test",
        working_directory=ROOT,
        internal_params=internal,
    )


def test_v1_is_readable_for_history_but_never_live_authority():
    legacy = {
        "schema_version": 1,
        "contract_id": contract_identity("legacy-envelope", "verify"),
        "envelope_id": "legacy-envelope",
        "intent_source": "controller",
        "requested_call": {"tool": "build", "params": {"action": "test"}},
        "effective_action": "verify",
        "expected_cwd": ROOT,
        "expected_argv": "verify",
    }
    legacy["contract_hash"] = contract_hash(legacy)

    def read(_command):
        return {"exit_code": 0, "output": canonical_json(legacy)}

    assert read_frozen_contract(read, legacy["contract_id"]) == legacy
    with dispatch_contract(legacy):
        assert current_contract() is None


class ContractStore:
    def __init__(self):
        self.fs = FakeContainer()

    def execute_control_command(self, command, **kwargs):
        # Production orchestrators expose the clean host-control channel; the
        # strict evidence transport refuses a bare bound normal executor.
        return self.execute_command(command, **kwargs)

    def execute_command(self, command, **_kwargs):
        tokens = shlex.split(command)
        if tokens[:1] == ["cat"]:
            path = tokens[1]
            if path not in self.fs.files:
                return {"exit_code": 1, "output": ""}
            return {"exit_code": 0, "output": self.fs.files[path]}
        if tokens[:3] == ["test", "!", "-e"]:
            return {"exit_code": 0 if tokens[3] not in self.fs.files else 1, "output": ""}
        return self.fs.execute_command(command)


def test_contract_writer_is_absent_or_identical_and_preserves_collision_bytes():
    store = ContractStore()
    contract = contract_for()
    path = f"{CONTRACT_DIR}/{contract['contract_id']}.json"
    assert write_contract(store.execute_command, contract) is True
    assert write_contract(store.execute_command, contract) is True
    original = store.fs.files[path]
    store.fs.files[path] = '{"forged":true}'
    assert write_contract(store.execute_command, contract) is False
    assert store.fs.files[path] == '{"forged":true}'
    assert original == canonical_json(contract)


def test_contract_writer_requires_host_publication_and_identical_replay_repairs_it(
    bind_host_evidence_publication_authority,
):
    store = ContractStore()
    contract = contract_for()
    token = install_evidence_publication_authority(
        unavailable_evidence_publication_authority("publication probe")
    )
    try:
        assert write_contract(store.execute_command, contract) is False
    finally:
        reset_evidence_publication_authority(token)

    path = f"{CONTRACT_DIR}/{contract['contract_id']}.json"
    assert store.fs.files[path] == canonical_json(contract)
    assert write_contract(store.execute_command, contract) is True
    assert bind_host_evidence_publication_authority.verify_bytes(
        record_kind="invocation_contract",
        record_id=contract["contract_id"],
        raw=canonical_json(contract).encode("utf-8"),
        contract_id=contract["contract_id"],
        contract_hash=contract["contract_hash"],
    ).authorized


def test_binding_pin_conflict_is_harness_owned_and_generic_exit_zero_is_not_green():
    contract = contract_for(
        expected_observations=["report_delta"],
        direct_falsifiers=[
            {"predicate_id": "empty_delta_despite_success", "kind": "delta_empty_on_exit0"}
        ],
    )
    mismatch = assess_receipt(
        contract,
        receipt_for(contract, config_fingerprint="cfg-other"),
        current_fingerprints=CURRENT,
    )
    assert mismatch.typed_code == CONTRACT_BINDING_UNKNOWN
    assert mismatch.owner is BlockerOwner.HARNESS

    empty = assess_receipt(contract, receipt_for(contract), current_fingerprints=CURRENT)
    assert empty.typed_code != EXPECTATION_MET

    survey_mismatch = assess_receipt(
        contract,
        receipt_for(contract, survey_fingerprint="survey-other"),
        current_fingerprints=CURRENT,
    )
    assert survey_mismatch.typed_code == CONTRACT_BINDING_UNKNOWN
    assert survey_mismatch.owner is BlockerOwner.HARNESS


def _setup_observations(*, complete_postconditions):
    def step(ordinal, role):
        return {
            "ordinal": ordinal,
            "role": role,
            "argv_sha256": f"{ordinal:x}" * 64,
            "outcome": "completed",
            "exit_code": 0,
            "output_sha256": f"{ordinal + 8:x}" * 64,
        }

    setup = {
        "venv": f"{ROOT}/.venv",
        "installer": "pip",
        "install_outcome": "success",
    }
    steps = [step(1, "dependency_install")]
    if complete_postconditions:
        targets = ["demo"]
        setup.update(
            {
                "pip_check": {
                    "status": "clean",
                    "exit_code": 0,
                    "output_sha256": "a" * 64,
                },
                "imports": {
                    "status": "complete",
                    "targets": targets,
                    "targets_sha256": producer_observations_sha256(targets),
                    "target_count": 1,
                    "importable_count": 1,
                    "failed_count": 0,
                    "failures": [],
                    "output_sha256": "b" * 64,
                },
            }
        )
        steps.extend((step(2, "pip_check"), step(3, "import_probe")))
    return normalize_producer_observations(
        {
            "schema_version": 1,
            "ecosystem": "python",
            "operation": "setup_env",
            "recorded_project_steps": steps,
            "setup": setup,
        }
    )


def test_python_setup_requires_explicit_clean_pip_and_complete_imports():
    params = {"action": "deps", "working_directory": ROOT}
    contract = contract_for(
        action="deps",
        effective_tool="python",
        effective_action="setup_env",
        binding=PYTHON_FACADE_EXECUTION_BINDING,
        expected_argv=None,
        params=params,
        expected_observations=["python_setup_observation"],
    )
    base = receipt_for(
        contract,
        receipt_id="inv-python-setup-0001",
        tool="python",
        argv=f"{ROOT}/.venv/bin/python -m pip install -e .",
    )
    partial = _setup_observations(complete_postconditions=False)
    partial_receipt = {
        **base,
        "producer_observations": partial,
        "producer_observations_sha256": producer_observations_sha256(partial),
    }
    assert (
        assess_receipt(contract, partial_receipt, current_fingerprints=CURRENT).typed_code
        == EXPECTATION_UNOBSERVED
    )

    complete = _setup_observations(complete_postconditions=True)
    complete_receipt = {
        **base,
        "producer_observations": complete,
        "producer_observations_sha256": producer_observations_sha256(complete),
    }
    assert (
        assess_receipt(contract, complete_receipt, current_fingerprints=CURRENT).typed_code
        == EXPECTATION_MET
    )
    wrong_targets = {
        **CURRENT,
        "python_import_targets": ["other"],
        "python_import_targets_sha256": producer_observations_sha256(["other"]),
    }
    assert (
        assess_receipt(
            contract,
            complete_receipt,
            current_fingerprints=wrong_targets,
        ).typed_code
        == EXPECTATION_UNOBSERVED
    )
    no_targets = dict(CURRENT)
    no_targets.pop("python_import_targets")
    no_targets.pop("python_import_targets_sha256")
    assert (
        assess_receipt(
            contract,
            complete_receipt,
            current_fingerprints=no_targets,
        ).typed_code
        == EXPECTATION_UNOBSERVED
    )


def test_python_test_requires_complete_non_all_skipped_current_report_rows():
    params = {"action": "test", "working_directory": ROOT}
    contract = contract_for(
        action="test",
        effective_tool="python",
        effective_action="test",
        binding=PYTHON_FACADE_EXECUTION_BINDING,
        expected_argv=None,
        params=params,
        expected_observations=["python_test_report"],
    )
    base = receipt_for(
        contract,
        receipt_id="inv-python-test-0001",
        tool="python",
        argv=f"{ROOT}/.venv/bin/python -m pytest --junitxml=/tmp/r.xml",
    )
    assert (
        assess_receipt(contract, base, current_fingerprints=CURRENT).typed_code
        == EXPECTATION_UNOBSERVED
    )
    report = {"new": [{"path": "/tmp/r.xml", "sha256": "b" * 64}], "changed": []}
    all_skipped = {
        "status": "complete",
        "rows": [{"outcome": "skipped"}],
    }
    assert assess_receipt(
        contract,
        {**base, "report_delta": report, "testcase_execution_rows": all_skipped},
        current_fingerprints=CURRENT,
    ).typed_code == EXPECTATION_UNOBSERVED
    one_passed = {"status": "complete", "rows": [{"outcome": "passed"}]}
    assert assess_receipt(
        contract,
        {**base, "report_delta": report, "testcase_execution_rows": one_passed},
        current_fingerprints=CURRENT,
    ).typed_code == EXPECTATION_UNOBSERVED


def test_supporting_claim_ids_are_authorization_only_and_move_nothing():
    emitted = []
    moved = commit_assessment_transitions(
        lambda *_args, **_kwargs: {"exit_code": 0, "output": ""},
        assessment_id="asm-1",
        typed_code="expectation_met",
        claim_ids=["claim-authorization-only"],
        emit=lambda kind, payload: emitted.append((kind, payload)),
    )
    assert moved == ()
    assert emitted == []
