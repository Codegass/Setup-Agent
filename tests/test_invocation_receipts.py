# tests/test_invocation_receipts.py
"""Plan 5 Stage B Task B1 (P0-A): every runner call leaves a scoped receipt.

Ground-truth review 2026-07-26 (§"Evidence is snapshot-global instead of
receipt-scoped"): the validator scanned the whole filesystem after several
invocations and could not answer which invocation produced which report —
the direct cause of Bigtop's 54/54. A receipt makes that answerable: one
atomic JSON file per physical runner invocation carrying the requested and
effective action, the exact argv, the exit status, and the before/after
content-hash delta of the test reports the invocation actually touched.

Persistence is best effort at the tool layer: a failed write NEVER raises
and never blocks the command result — it only flips `receipt_persisted` to
false in the ToolResult metadata (the phase gate is lane b2's business).

Scripted-orchestrator style (house pattern, shared with
tests/test_python_tool.py and tests/test_maven_gradle_tool_contracts.py).
"""

import copy
import json
import re
import threading

import pytest
from build_requirements_fakes import (
    complete_build_requirements_v1,
    complete_python_build_requirements_v1,
)
from container_evidence_fakes import ContainerFS, add_published_mutable_json
from test_container_io import FakeContainer
from test_maven_gradle_tool_contracts import FakeBuildToolOrchestrator
from test_python_tool import MANIFEST, Orch, ok

from sag.agent.action_intents import action_fingerprint
from sag.agent.invocation_contracts import (
    ARGV_EXECUTION_BINDING,
    PYTHON_FACADE_EXECUTION_BINDING,
    build_contract,
    dispatch_contract,
)
from sag.agent.invocation_receipts import (
    HOST_PUBLICATION_FAILED,
    PRODUCER_OBSERVATIONS_MAX_CANONICAL_BYTES,
    ReportSnapshot,
    RECEIPT_DIR,
    RECEIPT_SCHEMA_VERSION,
    build_receipt,
    next_receipt_id,
    normalize_producer_observations,
    producer_observations_sha256,
    read_producer_observations,
    record_invocation,
    report_delta,
    report_snapshot_complete,
    snapshot_reports,
    write_receipt,
    write_receipt_result,
)
from sag.agent.evidence_publications import (
    BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
    current_evidence_publication_authority,
    install_evidence_publication_authority,
    reset_evidence_publication_authority,
    unavailable_evidence_publication_authority,
)
from sag.tools.internal.gradle_tool import GradleTool
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.maven_tool import MavenTool
from sag.tools.internal.python_tool import PYTEST_REPORT_DIR, PythonTool

SUREFIRE = "/workspace/proj/target/surefire-reports/TEST-a.xml"
FAILSAFE = "/workspace/proj/target/failsafe-reports/TEST-b.xml"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def minimal_valid_receipt(
    receipt_id: str,
    *,
    tool: str = "maven",
    after=None,
):
    """One strict v2 receipt for transport/CAS tests.

    Writer tests exercise persistence mechanics, but the production writer is
    also the live schema boundary.  Keep their payloads valid so a transport
    failure cannot be confused with a schema refusal.
    """

    argv = {
        "maven": "mvn test",
        "gradle": "gradle test",
        "python": "python -m pytest",
    }[tool]
    return build_receipt(
        receipt_id=receipt_id,
        run_id="run-pytest",
        tool=tool,
        requested_action="test",
        effective_action="test",
        argv=argv,
        working_directory="/workspace/proj",
        exit_code=0,
        before={},
        after=after or {},
    )


def argv_contract_authority(*, executor, action, expected_argv, cwd="/workspace/proj"):
    lowered = action.lower()
    public_action = (
        "test"
        if "test" in lowered or "verify" in lowered
        else (
            "compile"
            if "compile" in lowered
            else (
                "deps"
                if "depend" in lowered
                else (
                    "install"
                    if "install" in lowered or "publishtomavenlocal" in lowered
                    else "package"
                )
            )
        )
    )
    params = {"action": public_action, "working_directory": cwd}
    domain_id = f"test:{cwd}"
    contract = build_contract(
        run_id=f"run-invocation-receipts-{executor}",
        envelope_id=f"envelope-invocation-receipts-{executor}-{action}",
        tool="build",
        params=params,
        effective_tool=executor,
        effective_action=action,
        expected_cwd=cwd,
        expected_argv=expected_argv,
        execution_binding=ARGV_EXECUTION_BINDING,
        intent_source="controller",
        intent_id=f"intent-invocation-receipts-{executor}-{action}",
        intent_domain_id=domain_id,
        intent_exact_params=params,
        action_fingerprint=action_fingerprint(
            domain_id=domain_id,
            tool="build",
            params=params,
        ),
    )
    return dispatch_contract(contract)


def python_test_authority(cwd="/workspace/proj"):
    params = {"action": "test", "working_directory": cwd}
    domain_id = f"test:{cwd}"
    contract = build_contract(
        run_id="run-invocation-receipts-python",
        envelope_id="envelope-invocation-receipts-python-test",
        tool="build",
        params=params,
        effective_tool="python",
        effective_action="test",
        expected_cwd=cwd,
        expected_argv=None,
        execution_binding=PYTHON_FACADE_EXECUTION_BINDING,
        intent_source="controller",
        intent_id="intent-invocation-receipts-python-test",
        intent_domain_id=domain_id,
        intent_exact_params=params,
        action_fingerprint=action_fingerprint(
            domain_id=domain_id,
            tool="build",
            params=params,
        ),
    )
    return dispatch_contract(contract)


def bind_current_host_store(source):
    """Bind one fake's stable store identity to this test's host authority."""

    token = install_evidence_publication_authority(
        current_evidence_publication_authority(),
        orchestrator=source,
    )
    reset_evidence_publication_authority(token)


def python_receipt_requirements():
    """Complete live v1 authority retaining this fixture's Python facts."""

    return complete_python_build_requirements_v1(
        project_root="/workspace/proj",
        **dict(MANIFEST),
    )


def python_compile_observations():
    roots = ["src/proj"]
    return {
        "schema_version": 1,
        "ecosystem": "python",
        "operation": "compile",
        "recorded_project_steps": [
            {
                "ordinal": 1,
                "role": "compileall",
                "argv_sha256": "1" * 64,
                "exit_code": 0,
                "outcome": "completed",
                "output_sha256": "2" * 64,
            },
            {
                "ordinal": 2,
                "role": "compile_metrics",
                "argv_sha256": "3" * 64,
                "exit_code": 0,
                "outcome": "completed",
                "output_sha256": "4" * 64,
            },
        ],
        "compile": {
            "status": "valid",
            "roots": roots,
            "roots_sha256": producer_observations_sha256(roots),
            "source_count": 2,
            "compiled_source_count": 2,
            "missing_source_count": 0,
            "foreign_pyc_count": 0,
            "coverage": 1.0,
            "cache_tag": "cpython-312",
            "source_basis_sha256": "5" * 64,
            "pyc_basis_sha256": "6" * 64,
            "source_basis_entry_count": 2,
            "pyc_basis_entry_count": 2,
            "missing_sources": [],
            "foreign_pycs": [],
            "conflicts": [],
        },
    }


def python_setup_observations():
    return {
        "schema_version": 1,
        "ecosystem": "python",
        "operation": "setup_env",
        "recorded_project_steps": [
            {
                "ordinal": 1,
                "role": "dependency_install",
                "argv_sha256": "1" * 64,
                "exit_code": 0,
                "outcome": "completed",
                "output_sha256": "2" * 64,
            },
            {
                "ordinal": 2,
                "role": "pip_check",
                "argv_sha256": "3" * 64,
                "exit_code": 0,
                "outcome": "completed",
                "output_sha256": "4" * 64,
            },
            {
                "ordinal": 3,
                "role": "import_probe",
                "argv_sha256": "5" * 64,
                "exit_code": 0,
                "outcome": "completed",
                "output_sha256": "6" * 64,
            },
        ],
        "setup": {
            "venv": "/workspace/proj/.venv",
            "installer": "pip",
            "install_outcome": "success",
            "pip_check": {
                "status": "clean",
                "exit_code": 0,
                "output_sha256": "4" * 64,
            },
            "imports": {
                "status": "complete",
                "targets": ["proj"],
                "targets_sha256": producer_observations_sha256(["proj"]),
                "target_count": 1,
                "importable_count": 1,
                "failed_count": 0,
                "failures": [],
                "output_sha256": "6" * 64,
            },
        },
    }


def python_build_observations():
    return {
        "schema_version": 1,
        "ecosystem": "python",
        "operation": "build",
        "recorded_project_steps": [
            {
                "ordinal": 1,
                "role": "wheel_build",
                "argv_sha256": "1" * 64,
                "exit_code": 0,
                "outcome": "completed",
                "output_sha256": "2" * 64,
            }
        ],
        "build": {
            "artifact_status": "produced",
            "artifacts": [
                {
                    "path": "dist/proj-1-py3-none-any.whl",
                    "sha256": "3" * 64,
                    "size_bytes": 7,
                    "change": "new",
                }
            ],
        },
    }


def sha256sum_output(*pairs):
    """The container's `sha256sum` transport format: `<hash>  <path>`."""
    return "".join(f"{digest}  {path}\n" for path, digest in pairs)


class FakeExecute:
    """Scriptable stand-in for orchestrator.execute_command."""

    def __init__(self, rules=None, default=None, raises=False):
        self.rules = list(rules or [])
        self.default = default or ok("")
        self.raises = raises
        self.commands = []
        bind_current_host_store(self)

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        if self.raises:
            raise RuntimeError("container is gone")
        for substring, result in self.rules:
            if substring in command:
                return result(command) if callable(result) else dict(result)
        return dict(self.default)

    def __call__(self, command, **kwargs):
        return self.execute_command(command, **kwargs)


def receipts_written(commands):
    """Replay bounded transport commands and return the persisted receipts."""
    filesystem = FakeContainer()
    for command in commands:
        filesystem.execute_command(command)
    return [
        json.loads(body)
        for path, body in sorted(filesystem.files.items())
        if path.startswith(f"{RECEIPT_DIR}/") and path.endswith(".json")
    ]


# ---------------------------------------------------------------------------
# snapshot_reports
# ---------------------------------------------------------------------------


def test_snapshot_reports_parses_one_shell_round_trip_into_path_hashes():
    execute = FakeExecute(
        rules=[("sha256sum", ok(sha256sum_output((SUREFIRE, HASH_A), (FAILSAFE, HASH_B))))]
    )
    snapshot = snapshot_reports(execute, ["/workspace/proj"])

    assert snapshot == {SUREFIRE: HASH_A, FAILSAFE: HASH_B}
    assert len(execute.commands) == 1


def test_receipt_preserves_contract_effective_jdk_provenance():
    effective_jdk = {
        "major": "17",
        "requirement_major": "17",
        "requirement_authority": "runner_observed",
        "provenance": {
            "target_sha": "a" * 40,
            "domain_root": "/workspace/proj",
            "source_ref": "inv-maven-compile-0001",
        },
    }

    receipt = build_receipt(
        receipt_id="inv-maven-2-0002",
        tool="maven",
        requested_action="compile",
        effective_action="compile",
        argv="mvn compile",
        working_directory="/workspace/proj",
        exit_code=0,
        before={},
        after={},
        effective_jdk=effective_jdk,
    )

    assert receipt["effective_jdk"] == effective_jdk


def test_receipt_binds_strict_python_producer_observations_by_canonical_hash():
    observations = python_compile_observations()

    receipt = build_receipt(
        receipt_id="inv-python-compile-0001",
        tool="python",
        requested_action="compile",
        effective_action="compile",
        argv="/workspace/proj/.venv/bin/python -m compileall -q src/proj",
        working_directory="/workspace/proj",
        exit_code=0,
        before={},
        after={},
        producer_observations=observations,
    )

    assert receipt["producer_observations"] == observations
    assert receipt["producer_observations_sha256"] == producer_observations_sha256(observations)
    assert read_producer_observations(receipt) == observations


def test_tampered_python_producer_observations_are_not_read_as_evidence():
    observations = python_compile_observations()
    receipt = build_receipt(
        receipt_id="inv-python-compile-0002",
        tool="python",
        requested_action="compile",
        effective_action="compile",
        argv="python -m compileall src/proj",
        working_directory="/workspace/proj",
        exit_code=0,
        before={},
        after={},
        producer_observations=observations,
    )

    receipt["producer_observations"]["compile"]["source_count"] = 3

    assert read_producer_observations(receipt) is None


@pytest.mark.parametrize(
    "mutate",
    [
        # A setup result cannot float free of the command that produced it.
        lambda value: value["recorded_project_steps"].pop(0),
        lambda value: value["recorded_project_steps"][1].update(role="dependency_install"),
        lambda value: value["recorded_project_steps"][1].update(output_sha256="9" * 64),
        lambda value: value["recorded_project_steps"][2].update(outcome="failed", exit_code=1),
        lambda value: value["setup"]["imports"].update(output_sha256="9" * 64),
    ],
)
def test_setup_observation_is_bound_to_exact_recorded_roles_and_results(mutate):
    observations = python_setup_observations()
    mutate(observations)

    with pytest.raises(ValueError):
        normalize_producer_observations(observations)


@pytest.mark.parametrize(
    ("reason_code", "probe_outcome", "probe_exit", "include_probe", "valid"),
    [
        ("no_survey_targets", None, None, False, True),
        ("invalid_survey_targets", None, None, False, True),
        ("probe_failed", "failed", 7, True, True),
        ("invalid_probe_output", "completed", 0, True, True),
        ("probe_failed", "completed", 0, True, False),
        ("invalid_probe_output", "failed", 7, True, False),
        ("no_survey_targets", "completed", 0, True, False),
    ],
)
def test_unavailable_import_reason_binds_whether_and_how_probe_ran(
    reason_code, probe_outcome, probe_exit, include_probe, valid
):
    observations = python_setup_observations()
    observations["setup"]["imports"] = {
        "status": "unavailable",
        "reason_code": reason_code,
    }
    observations["recorded_project_steps"] = observations["recorded_project_steps"][:2]
    if include_probe:
        observations["recorded_project_steps"].append(
            {
                "ordinal": 3,
                "role": "import_probe",
                "argv_sha256": "5" * 64,
                "exit_code": probe_exit,
                "outcome": probe_outcome,
                "output_sha256": "6" * 64,
            }
        )

    if valid:
        assert normalize_producer_observations(observations)["setup"]["imports"] == {
            "status": "unavailable",
            "reason_code": reason_code,
        }
    else:
        with pytest.raises(ValueError):
            normalize_producer_observations(observations)


def test_failed_install_forbids_post_install_roles_even_when_detail_keys_are_removed():
    observations = python_setup_observations()
    observations["setup"] = {
        "venv": "/workspace/proj/.venv",
        "installer": "pip",
        "install_outcome": "failed",
    }
    observations["recorded_project_steps"][0].update(outcome="failed", exit_code=5)

    with pytest.raises(ValueError):
        normalize_producer_observations(observations)


def test_semantic_install_failure_may_preserve_the_real_zero_exit_only_on_dependency_step():
    observations = python_setup_observations()
    observations["setup"] = {
        "venv": "/workspace/proj/.venv",
        "installer": "pip",
        "install_outcome": "failed",
    }
    observations["recorded_project_steps"] = [
        {
            **observations["recorded_project_steps"][0],
            "outcome": "failed",
            "exit_code": 0,
            "semantic_failure": "install_error_signature",
        }
    ]

    normalized = normalize_producer_observations(observations)

    assert normalized["recorded_project_steps"][0]["outcome"] == "failed"
    assert normalized["recorded_project_steps"][0]["exit_code"] == 0
    assert normalized["recorded_project_steps"][0]["semantic_failure"] == (
        "install_error_signature"
    )


def test_semantic_install_failure_marker_is_rejected_when_the_process_really_failed():
    observations = python_setup_observations()
    observations["setup"] = {
        "venv": "/workspace/proj/.venv",
        "installer": "pip",
        "install_outcome": "failed",
    }
    observations["recorded_project_steps"] = [
        {
            **observations["recorded_project_steps"][0],
            "outcome": "failed",
            "exit_code": 17,
            "semantic_failure": "install_error_signature",
        }
    ]

    with pytest.raises(ValueError):
        normalize_producer_observations(observations)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(recorded_project_steps=[]),
        lambda value: value["recorded_project_steps"][0].update(role="compileall"),
        lambda value: value["recorded_project_steps"][0].update(outcome="failed", exit_code=1),
        lambda value: value["build"]["artifacts"].append(
            copy.deepcopy(value["build"]["artifacts"][0])
        ),
    ],
)
def test_wheel_artifact_observation_requires_its_unique_successful_wheel_step(mutate):
    observations = python_build_observations()
    mutate(observations)

    with pytest.raises(ValueError):
        normalize_producer_observations(observations)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["recorded_project_steps"].pop(0),
        lambda value: value["recorded_project_steps"].pop(),
        lambda value: value["recorded_project_steps"][1].update(outcome="failed", exit_code=2),
        lambda value: value["recorded_project_steps"][1].pop("output_sha256"),
    ],
)
def test_valid_compile_metrics_require_both_roles_and_a_successful_metrics_result(mutate):
    observations = python_compile_observations()
    mutate(observations)

    with pytest.raises(ValueError):
        normalize_producer_observations(observations)


@pytest.mark.parametrize(
    ("reason_code", "metrics_outcome", "metrics_exit", "valid"),
    [
        ("metrics_command_failed", "failed", 2, True),
        ("metrics_command_failed", "completed", 0, False),
        ("metrics_output_invalid", "completed", 0, True),
        ("metrics_output_invalid", "failed", 2, False),
        ("basis_escaped", "completed", 0, True),
        ("no_sources", "completed", 0, True),
    ],
)
def test_unavailable_compile_reason_is_bound_to_compile_metrics_step(
    reason_code, metrics_outcome, metrics_exit, valid
):
    observations = python_compile_observations()
    roots = observations["compile"]["roots"]
    observations["compile"] = {
        "status": "unavailable",
        "roots": roots,
        "roots_sha256": producer_observations_sha256(roots),
        "reason_code": reason_code,
    }
    observations["recorded_project_steps"][1].update(
        outcome=metrics_outcome,
        exit_code=metrics_exit,
    )

    if valid:
        normalize_producer_observations(observations)
    else:
        with pytest.raises(ValueError):
            normalize_producer_observations(observations)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(operation="test"),
        lambda value: value["compile"].update(source_basis_sha256="not-a-digest"),
        lambda value: value["compile"].update(compiled_source_count=3),
        lambda value: value["recorded_project_steps"][0].update(role="pip_check"),
        lambda value: value.update(untyped_prose="trust me"),
    ],
)
def test_invalid_python_producer_observations_fail_closed_before_receipt_io(mutate):
    observations = python_compile_observations()
    mutate(observations)
    execute = FakeExecute()

    metadata = record_invocation(
        execute,
        tool="python",
        attempt="compile",
        requested_action="compile",
        effective_action="compile",
        argv="python -m compileall src/proj",
        working_directory="/workspace/proj",
        exit_code=0,
        before={},
        after={},
        producer_observations=observations,
    )

    assert metadata == {
        "receipt_persisted": False,
        "receipt_persistence_code": "producer_observations_invalid",
    }
    assert execute.commands == []


def test_oversize_python_producer_observations_fail_closed_without_truncation():
    observations = python_compile_observations()
    observations["compile"]["cache_tag"] = "x" * (PRODUCER_OBSERVATIONS_MAX_CANONICAL_BYTES + 1)
    execute = FakeExecute()

    metadata = record_invocation(
        execute,
        tool="python",
        attempt="compile",
        requested_action="compile",
        effective_action="compile",
        argv="python -m compileall src/proj",
        working_directory="/workspace/proj",
        exit_code=0,
        before={},
        after={},
        producer_observations=observations,
    )

    assert metadata["receipt_persisted"] is False
    assert metadata["receipt_persistence_code"] == "producer_observations_invalid"
    assert execute.commands == []


def test_snapshot_reports_scans_every_root_for_the_validators_report_shapes():
    """The scan must find exactly what physical_validator.is_report_file
    accepts: surefire, failsafe, gradle test-results and pytest junit XML."""
    execute = FakeExecute()

    snapshot_reports(execute, ["/workspace/proj", PYTEST_REPORT_DIR])

    command = execute.commands[0]
    assert "/workspace/proj" in command
    assert PYTEST_REPORT_DIR in command
    for marker in (
        "/target/surefire-reports/",
        "/target/failsafe-reports/",
        "/build/test-results/",
        "/.setup_agent/pytest-reports/",
    ):
        assert marker in command
    assert "*.xml" in command


def test_snapshot_reports_ignores_output_lines_that_are_not_hash_path_pairs():
    execute = FakeExecute(
        rules=[
            (
                "sha256sum",
                ok(
                    "sha256sum: /workspace/proj/gone.xml: No such file or directory\n"
                    "\n"
                    f"{HASH_A}  {SUREFIRE}\n"
                ),
            )
        ]
    )

    assert snapshot_reports(execute, ["/workspace/proj"]) == {SUREFIRE: HASH_A}


def test_snapshot_reports_returns_nothing_and_never_raises_when_the_call_fails():
    snapshot = snapshot_reports(FakeExecute(raises=True), ["/workspace/proj"])

    assert snapshot == {}
    assert report_snapshot_complete(snapshot) is False


def test_snapshot_reports_without_scan_roots_does_not_touch_the_container():
    execute = FakeExecute()

    assert snapshot_reports(execute, []) == {}
    assert execute.commands == []


@pytest.mark.parametrize(
    "result",
    (
        {"success": False, "exit_code": -1, "output": ""},
        {"success": True, "exit_code": 1, "output": ""},
        {
            "success": False,
            "exit_code": -1,
            "dispatch_status": "control_transport_unavailable",
            "output": f"{HASH_A}  {SUREFIRE}",
        },
    ),
)
def test_snapshot_transport_failure_is_not_a_proven_empty_directory(result):
    snapshot = snapshot_reports(lambda *_args, **_kwargs: result, ["/workspace/proj"])

    assert snapshot == {}
    assert report_snapshot_complete(snapshot) is False


def test_unreadable_before_snapshot_cannot_launder_unchanged_report_as_new():
    before = ReportSnapshot(complete=False)
    after = ReportSnapshot({SUREFIRE: HASH_A}, complete=True)

    assert report_delta(before, after) == {"new": [], "changed": []}


# ---------------------------------------------------------------------------
# report_delta
# ---------------------------------------------------------------------------


def test_report_delta_separates_new_from_changed_and_drops_unchanged():
    before = {SUREFIRE: HASH_A, FAILSAFE: HASH_B}
    after = {SUREFIRE: HASH_A, FAILSAFE: HASH_C, "/workspace/proj/build/test-results/x.xml": HASH_B}

    delta = report_delta(before, after)

    assert delta == {
        "new": [{"path": "/workspace/proj/build/test-results/x.xml", "sha256": HASH_B}],
        "changed": [{"path": FAILSAFE, "sha256": HASH_C}],
    }


def test_report_delta_of_an_untouched_tree_claims_no_reports():
    """A same-path overwrite is 'changed'; a byte-identical file is neither.
    An empty delta is a stated fact, not a missing one."""
    snapshot = {SUREFIRE: HASH_A}

    assert report_delta(snapshot, dict(snapshot)) == {"new": [], "changed": []}


def test_report_delta_ignores_reports_that_vanished():
    assert report_delta({SUREFIRE: HASH_A}, {}) == {"new": [], "changed": []}


# ---------------------------------------------------------------------------
# write_receipt
# ---------------------------------------------------------------------------


def test_write_receipt_persists_atomically_under_the_session_receipt_dir():
    execute = FakeExecute()
    receipt = minimal_valid_receipt("inv-maven-1-0001")

    assert write_receipt(execute, receipt) is True

    final = f"{RECEIPT_DIR}/inv-maven-1-0001.json"
    assert all(len(command) <= 60_200 for command in execute.commands)
    assert any("mv -f --" in command and final in command for command in execute.commands)
    assert receipts_written(execute.commands) == [receipt]


def test_write_receipt_returns_false_when_the_atomic_write_fails():
    execute = FakeExecute(default={"success": False, "exit_code": 1, "output": "Read-only"})
    receipt = minimal_valid_receipt("inv-maven-1-0002")

    assert write_receipt(execute, receipt) is False
    assert write_receipt_result(execute, receipt).code == ("transport_write_failed")


def test_write_receipt_never_raises_when_the_container_call_raises():
    assert (
        write_receipt(FakeExecute(raises=True), minimal_valid_receipt("inv-maven-1-0003")) is False
    )


def test_write_receipt_refuses_a_receipt_without_an_id():
    execute = FakeExecute()

    assert write_receipt(execute, {"schema_version": RECEIPT_SCHEMA_VERSION}) is False
    assert execute.commands == []


@pytest.mark.parametrize(
    "receipt_id",
    ("../escape", "inv/python/0001", " inv-python-0001", "inv-python-0001\nnext"),
)
def test_write_receipt_refuses_unsafe_ids_before_any_container_io(receipt_id):
    execute = FakeExecute()
    # An otherwise complete receipt, so the refusal is the ID and the typed
    # code names the ID — `invalid_arguments` alone cannot be repaired.
    payload = {**minimal_valid_receipt("inv-python-0001", tool="python"), "receipt_id": receipt_id}

    result = write_receipt_result(execute, payload)

    assert result.persisted is False
    assert result.code == "invalid_arguments:receipt_id"
    assert execute.commands == []


def test_receipt_id_is_immutable_but_byte_identical_replay_is_idempotent():
    fake = FakeContainer()
    receipt = minimal_valid_receipt("inv-python-compile-0042", tool="python")

    first = write_receipt_result(fake, receipt)
    replay = write_receipt_result(fake, dict(reversed(list(receipt.items()))))
    collision = write_receipt_result(
        fake, minimal_valid_receipt("inv-python-compile-0042", tool="maven")
    )

    assert first.persisted is True
    assert replay.persisted is True
    assert collision.persisted is False
    assert collision.code == "receipt_id_collision"
    assert json.loads(fake.files[f"{RECEIPT_DIR}/{receipt['receipt_id']}.json"]) == receipt


def test_receipt_writer_requires_host_publication_and_identical_replay_repairs_it(
    bind_host_evidence_publication_authority,
):
    fake = FakeContainer()
    receipt = minimal_valid_receipt("inv-maven-host-publication-0001")
    token = install_evidence_publication_authority(
        unavailable_evidence_publication_authority("publication probe")
    )
    try:
        first = write_receipt_result(fake, receipt)
    finally:
        reset_evidence_publication_authority(token)

    assert first.persisted is False
    assert first.code == HOST_PUBLICATION_FAILED
    body = json.dumps(receipt, sort_keys=True)
    assert fake.files[f"{RECEIPT_DIR}/{receipt['receipt_id']}.json"] == body
    replay = write_receipt_result(fake, receipt)
    assert replay.persisted is True
    assert bind_host_evidence_publication_authority.verify_bytes(
        record_kind="invocation_receipt",
        record_id=receipt["receipt_id"],
        raw=body.encode("utf-8"),
    ).authorized


def test_concurrent_same_id_publish_is_identical_or_collision_never_last_writer_wins(
    bind_host_evidence_publication_authority,
):
    class ConcurrentContainer(FakeContainer):
        def __init__(self):
            super().__init__()
            self.cas_barrier = threading.Barrier(2)

        def execute_command(self, command, **kwargs):
            if "fcntl.flock" in command:
                self.cas_barrier.wait(timeout=5)
            return super().execute_command(command, **kwargs)

    fake = ConcurrentContainer()
    install_evidence_publication_authority(
        bind_host_evidence_publication_authority,
        orchestrator=fake,
    )
    receipts = [
        minimal_valid_receipt("inv-python-compile-0043", tool=tool) for tool in ("python", "maven")
    ]
    results = []

    threads = [
        threading.Thread(
            target=lambda value=value: results.append(write_receipt_result(fake, value))
        )
        for value in receipts
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(result.code for result in results) == ["persisted", "receipt_id_collision"]
    persisted = json.loads(fake.files[f"{RECEIPT_DIR}/inv-python-compile-0043.json"])
    assert persisted in receipts


def test_generated_receipt_ids_include_a_startup_nonce_and_monotonic_suffix():
    first = next_receipt_id("maven", "test")
    second = next_receipt_id("maven", "test")

    pattern = r"^inv-maven-test-([0-9a-f]{12})-(\d+)$"
    first_match = re.fullmatch(pattern, first)
    second_match = re.fullmatch(pattern, second)
    assert first_match is not None and second_match is not None
    assert first_match.group(1) == second_match.group(1)
    assert int(second_match.group(2)) == int(first_match.group(2)) + 1


def test_record_invocation_accepts_a_prefrozen_receipt_id_for_settlement_replay():
    execute = FakeExecute()

    metadata = record_invocation(
        execute,
        receipt_id="inv-maven-settlement-fixed",
        run_id="run-pytest",
        tool="maven",
        attempt=1,
        requested_action="verify",
        effective_action="verify",
        argv="./mvnw verify",
        working_directory="/workspace/project",
        exit_code=0,
        before={},
        after={},
    )

    assert metadata == {"receipt_id": "inv-maven-settlement-fixed"}
    assert receipts_written(execute.commands)[0]["receipt_id"] == ("inv-maven-settlement-fixed")
    assert receipts_written(execute.commands)[0]["run_id"] == "run-pytest"


def test_large_receipt_round_trips_without_crossing_the_exec_argument_limit():
    """The Lucene/Geode/Cayenne failure shape: an unbounded report delta.

    The old writer embedded this canonical JSON in one shell argv element and
    failed before the receipt reached the container. This fake rejects the
    kernel-risk shape and proves every transport command stays bounded.
    """

    class ArgLimitContainer(FakeContainer):
        def execute_command(self, command, **kwargs):
            if len(command) >= 128 * 1024:
                self.commands.append(command)
                return {"exit_code": 255, "output": "argument list too long"}
            return super().execute_command(command, **kwargs)

    fake = ArgLimitContainer()
    reports = {
        f"/workspace/project/module-{index}/{'x' * 1550}/TEST-{index}.xml": (f"{index:064x}"[-64:])
        for index in range(2_000)
    }
    receipt = minimal_valid_receipt(
        "inv-maven-1-large",
        after=reports,
    )
    canonical = json.dumps(receipt, sort_keys=True)
    assert len(canonical.encode("utf-8")) >= 3 * 1024 * 1024

    result = write_receipt_result(fake, receipt)

    final = f"{RECEIPT_DIR}/{receipt['receipt_id']}.json"
    assert result.persisted is True
    assert fake.files[final] == canonical
    assert json.loads(fake.files[final]) == receipt
    assert max(map(len, fake.commands)) <= 60_200
    assert not any(path.endswith(".tmp") for path in fake.files)


# ---------------------------------------------------------------------------
# maven integration
# ---------------------------------------------------------------------------


class ReceiptOrchestrator(FakeBuildToolOrchestrator):
    """Build-tool orchestrator double that scripts the report snapshots."""

    def __init__(
        self,
        snapshots=(),
        monitored_result=None,
        receipt_write=None,
        *,
        build_system="maven",
    ):
        # Do not call the legacy base initializer: it publishes a raw `{}`
        # build-requirements artifact before this strict fixture can replace
        # it. A live authority sees only the complete v1 record below.
        self.monitored_result = monitored_result or {
            "output": "[INFO] BUILD SUCCESS",
            "exit_code": 0,
        }
        self.commands = []
        self.monitored_commands = []
        self.project_name = None
        self.snapshots = list(snapshots)
        self.receipt_write = receipt_write
        self.receipt_commands = []
        self.evidence = ContainerFS()
        self.evidence_store = self.evidence
        add_published_mutable_json(
            self,
            self.evidence_store,
            path=REQUIREMENTS_PATH,
            record_kind="build_requirements",
            record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            payload=complete_build_requirements_v1(
                project_root="/workspace/proj",
                build_system=build_system,
            ),
        )

    def execute_command(self, command, workdir=None, timeout=None, **kwargs):
        del kwargs
        if "SAG_NAMED_JSON_RECORD_V1" in command and REQUIREMENTS_PATH in command:
            self.commands.append((command, workdir, timeout))
            return self.evidence_store(command)
        if "sha256sum" in command:
            self.commands.append((command, workdir, timeout))
            return ok(self.snapshots.pop(0) if self.snapshots else "")
        if RECEIPT_DIR in command:
            self.commands.append((command, workdir, timeout))
            self.receipt_commands.append(command)
            return dict(self.receipt_write or ok(""))
        return super().execute_command(command, workdir=workdir, timeout=timeout)

    def execute_control_command(self, command, workdir=None, timeout=None, **kwargs):
        return self.execute_command(command, workdir=workdir, timeout=timeout, **kwargs)


MAVEN_TEST_OUTPUT = "\n".join(
    [
        "[INFO] --- maven-surefire-plugin:3.5.5:test (default-test) @ demo ---",
        "[INFO] Tests run: 4, Failures: 0, Errors: 0, Skipped: 0",
        "[INFO] BUILD SUCCESS",
    ]
)


def maven_tool(orchestrator):
    tool = MavenTool(orchestrator)
    tool._record_test_summary = lambda *args, **kwargs: None
    return tool


def test_maven_test_run_lands_a_schema_v1_receipt_for_its_own_reports(
    facade_contract_authority,
):
    orchestrator = ReceiptOrchestrator(
        snapshots=["", sha256sum_output((SUREFIRE, HASH_A))],
        monitored_result={"output": MAVEN_TEST_OUTPUT, "exit_code": 0},
    )

    with argv_contract_authority(
        executor="maven", action="verify", expected_argv="--fail-at-end verify"
    ):
        result = maven_tool(orchestrator).execute(
            command="verify",
            fail_at_end=True,
            working_directory="/workspace/proj",
        )

    (receipt,) = receipts_written(orchestrator.receipt_commands)
    assert receipt["schema_version"] == RECEIPT_SCHEMA_VERSION
    assert receipt["tool"] == "maven"
    assert receipt["requested_action"] == "verify"
    assert receipt["effective_action"] == "verify"
    assert receipt["argv"] == orchestrator.monitored_commands[0][0]
    assert receipt["working_directory"] == "/workspace/proj"
    assert receipt["exit_code"] == 0
    assert receipt["outcome"] == "completed"
    assert receipt["report_delta"] == {
        "new": [{"path": SUREFIRE, "sha256": HASH_A}],
        "changed": [],
    }
    assert result.metadata["receipt_id"] == receipt["receipt_id"]
    assert "receipt_persisted" not in result.metadata


def test_maven_receipt_brackets_the_run_so_pre_existing_reports_stay_out(
    facade_contract_authority,
):
    """The Bigtop case: auxiliary XML already on disk is NOT this
    invocation's evidence — only the same-path overwrite is."""
    orchestrator = ReceiptOrchestrator(
        snapshots=[
            sha256sum_output((SUREFIRE, HASH_A), (FAILSAFE, HASH_B)),
            sha256sum_output((SUREFIRE, HASH_C), (FAILSAFE, HASH_B)),
        ],
        monitored_result={"output": MAVEN_TEST_OUTPUT, "exit_code": 0},
    )

    with argv_contract_authority(executor="maven", action="verify", expected_argv="verify"):
        maven_tool(orchestrator).execute(command="verify", working_directory="/workspace/proj")

    (receipt,) = receipts_written(orchestrator.receipt_commands)
    assert receipt["report_delta"] == {
        "new": [],
        "changed": [{"path": SUREFIRE, "sha256": HASH_C}],
    }


def test_maven_failed_build_still_leaves_a_receipt_recording_the_failure(
    facade_contract_authority,
):
    orchestrator = ReceiptOrchestrator(
        monitored_result={"output": "[ERROR] BUILD FAILURE", "exit_code": 1},
    )

    with argv_contract_authority(executor="maven", action="verify", expected_argv="verify"):
        result = maven_tool(orchestrator).execute(
            command="verify", working_directory="/workspace/proj"
        )

    (receipt,) = receipts_written(orchestrator.receipt_commands)
    assert receipt["outcome"] == "failed"
    assert receipt["exit_code"] == 1
    assert result.succeeded is False
    assert result.metadata["receipt_id"] == receipt["receipt_id"]


def test_maven_receipt_persistence_failure_is_visible_and_never_blocks(
    facade_contract_authority,
):
    orchestrator = ReceiptOrchestrator(
        monitored_result={"output": MAVEN_TEST_OUTPUT, "exit_code": 0},
        receipt_write={"success": False, "exit_code": 1, "output": "Read-only file system"},
    )

    with argv_contract_authority(executor="maven", action="verify", expected_argv="verify"):
        result = maven_tool(orchestrator).execute(
            command="verify", working_directory="/workspace/proj"
        )

    assert result.succeeded is True
    assert result.metadata["receipt_persisted"] is False
    assert result.metadata["receipt_persistence_code"] == "transport_write_failed"
    assert "receipt_id" not in result.metadata


# ---------------------------------------------------------------------------
# gradle integration
# ---------------------------------------------------------------------------


def test_gradle_test_run_lands_a_schema_v1_receipt(facade_contract_authority):
    gradle_report = "/workspace/proj/build/test-results/test/TEST-a.xml"
    orchestrator = ReceiptOrchestrator(
        snapshots=["", sha256sum_output((gradle_report, HASH_A))],
        monitored_result={"output": "BUILD SUCCESSFUL in 3s", "exit_code": 0},
        build_system="gradle",
    )

    with argv_contract_authority(
        executor="gradle", action="test", expected_argv="--build-cache test"
    ):
        result = GradleTool(orchestrator).execute(tasks="test", working_directory="/workspace/proj")

    (receipt,) = receipts_written(orchestrator.receipt_commands)
    assert receipt["tool"] == "gradle"
    assert receipt["requested_action"] == "test"
    assert receipt["effective_action"] == "test"
    assert receipt["argv"] == orchestrator.monitored_commands[0][0]
    assert receipt["working_directory"] == "/workspace/proj"
    assert receipt["outcome"] == "completed"
    assert receipt["report_delta"]["new"] == [{"path": gradle_report, "sha256": HASH_A}]
    assert result.metadata["receipt_id"] == receipt["receipt_id"]


def test_gradle_receipt_records_the_substituted_default_task(
    facade_contract_authority,
):
    """No task named means gradle_tool runs `build` — a requested/effective
    divergence the receipt must state rather than smooth over."""
    orchestrator = ReceiptOrchestrator(
        monitored_result={"output": "BUILD SUCCESSFUL in 3s", "exit_code": 0},
        build_system="gradle",
    )

    with argv_contract_authority(
        executor="gradle", action="build", expected_argv="--build-cache build"
    ):
        GradleTool(orchestrator).execute(working_directory="/workspace/proj")

    (receipt,) = receipts_written(orchestrator.receipt_commands)
    assert receipt["requested_action"] == ""
    assert receipt["effective_action"] == "build"


def test_maven_receipt_records_extra_goals_as_the_effective_action(
    facade_contract_authority,
):
    orchestrator = ReceiptOrchestrator(
        monitored_result={"output": MAVEN_TEST_OUTPUT, "exit_code": 0},
    )

    with argv_contract_authority(
        executor="maven",
        action="clean verify jacoco:report",
        expected_argv="clean verify jacoco:report",
    ):
        maven_tool(orchestrator).execute(
            command="clean verify",
            goals="jacoco:report",
            working_directory="/workspace/proj",
        )

    (receipt,) = receipts_written(orchestrator.receipt_commands)
    assert receipt["requested_action"] == "clean verify"
    assert receipt["effective_action"] == "clean verify jacoco:report"


# ---------------------------------------------------------------------------
# python (pytest) integration
# ---------------------------------------------------------------------------


class PytestReceiptOrch(Orch):
    """Scripted python orchestrator that also answers the report snapshots."""

    def __init__(self, snapshots=(), **kwargs):
        super().__init__(**kwargs)
        self.snapshots = list(snapshots)

    def execute_command(self, cmd, workdir=None, **kwargs):
        if "SAG_NAMED_JSON_RECORD_V1" in cmd:
            return super().execute_command(cmd, workdir=workdir, **kwargs)
        if "sha256sum" in cmd:
            self.commands.append(cmd)
            return ok(self.snapshots.pop(0) if self.snapshots else "")
        return super().execute_command(cmd, workdir=workdir, **kwargs)

    def execute_control_command(self, cmd, workdir=None, **kwargs):
        return self.execute_command(cmd, workdir=workdir, **kwargs)


def test_pytest_run_lands_a_schema_v1_receipt_for_its_junit_report(
    facade_contract_authority,
):
    junit = f"{PYTEST_REPORT_DIR}/pytest-attempt-000001.xml"
    orch = PytestReceiptOrch(
        snapshots=["", sha256sum_output((junit, HASH_A))],
        manifest=python_receipt_requirements(),
        rules=[("--collect-only", ok("42 tests collected in 0.12s"))],
    )

    with python_test_authority():
        result = PythonTool(orch).execute("test", working_directory="/workspace/proj")

    (receipt,) = receipts_written(orch.commands)
    assert receipt["schema_version"] == RECEIPT_SCHEMA_VERSION
    assert receipt["tool"] == "python"
    assert receipt["requested_action"] == "test"
    assert receipt["effective_action"] == "test"
    assert receipt["argv"] == result.metadata["command"]
    assert receipt["working_directory"] == "/workspace/proj"
    assert receipt["exit_code"] == 0
    assert receipt["outcome"] == "completed"
    assert receipt["report_delta"] == {
        "new": [{"path": junit, "sha256": HASH_A}],
        "changed": [],
    }
    assert result.metadata["receipt_id"] == receipt["receipt_id"]
    assert "receipt_persisted" not in result.metadata


def test_pytest_receipt_hashes_the_report_after_the_attempt_tagger_rewrote_it(
    facade_contract_authority,
):
    """The tagger rewrites the JUnit XML in place to persist sag.attempt_id.
    Hashing before it would bind the receipt to bytes that no longer exist,
    and every consumer would read this attempt's own report as stale."""
    orch = PytestReceiptOrch(
        manifest=python_receipt_requirements(),
        rules=[("--collect-only", ok("42 tests collected in 0.12s"))],
    )

    with python_test_authority():
        PythonTool(orch).execute("test", working_directory="/workspace/proj")

    snapshots = [
        index
        for index, command in enumerate(orch.commands)
        if "sha256sum" in command and "SAG_NAMED_JSON_RECORD_V1" not in command
    ]
    run = next(
        index
        for index, command in enumerate(orch.commands)
        if "--junitxml" in command
        and "-m pytest" in command
        and "invocation_contracts" not in command
    )
    tagged = next(
        index for index, command in enumerate(orch.commands) if "SAG_ATTEMPT_TAGGED" in command
    )
    assert len(snapshots) == 2
    assert snapshots[0] < run < tagged < snapshots[1]


def test_pytest_receipt_ids_are_unique_across_invocations_of_one_process(
    facade_contract_authority,
):
    orch = PytestReceiptOrch(
        manifest=python_receipt_requirements(),
        rules=[("--collect-only", ok("42 tests collected in 0.12s"))],
    )
    tool = PythonTool(orch)

    with python_test_authority():
        first = tool.execute("test", working_directory="/workspace/proj")
        second = tool.execute("test", working_directory="/workspace/proj")

    ids = [receipt["receipt_id"] for receipt in receipts_written(orch.commands)]
    assert len(ids) == 2
    assert len(set(ids)) == 2
    assert first.metadata["receipt_id"] != second.metadata["receipt_id"]


def test_pytest_receipt_persistence_failure_never_masks_the_test_result(
    facade_contract_authority,
):
    orch = PytestReceiptOrch(
        manifest=python_receipt_requirements(),
        rules=[
            ("--collect-only", ok("42 tests collected in 0.12s")),
            (RECEIPT_DIR, {"success": False, "exit_code": 1, "output": "Read-only"}),
        ],
    )

    with python_test_authority():
        result = PythonTool(orch).execute("test", working_directory="/workspace/proj")

    assert result.succeeded is True
    assert result.metadata["receipt_persisted"] is False
    assert result.metadata["receipt_persistence_code"] == "transport_write_failed"
    assert "receipt_id" not in result.metadata
