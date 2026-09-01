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
from sag.agent.evidence_publications import (
    BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
    current_evidence_publication_authority,
    install_evidence_publication_authority,
    reset_evidence_publication_authority,
    unavailable_evidence_publication_authority,
)
from sag.agent.invocation_contracts import (
    ARGV_EXECUTION_BINDING,
    PYTHON_FACADE_EXECUTION_BINDING,
    build_contract,
    dispatch_contract,
)
from sag.agent.invocation_receipts import (
    HOST_PUBLICATION_FAILED,
    PRODUCER_OBSERVATIONS_MAX_CANONICAL_BYTES,
    RECEIPT_DIR,
    RECEIPT_SCHEMA_VERSION,
    ReportSnapshot,
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
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.gradle_tool import GradleTool
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


# ---------------------------------------------------------------------------
# Task #38 item 1 — a `requested_action` fallback no admitted receipt can reach
# ---------------------------------------------------------------------------
def _canonical_receipt(**overrides):
    payload = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "receipt_id": "receipt-000001",
        "run_id": "run-20260815-000000-abcdef",
        "tool": "maven",
        "requested_action": "build",
        "effective_action": "test",
        "argv": "mvn test",
        "working_directory": "/workspace/proj",
        "actual_cwd": "/workspace/proj",
        "outcome": "completed",
        "exit_code": 0,
        "report_delta": {"new": [], "changed": []},
    }
    payload.update(overrides)
    return payload


def test_no_admitted_receipt_can_omit_its_effective_action():
    """The dead fail-closed fallback both action readers carried.

    ``attempt_policy._production_build_receipt`` and
    ``physical_validator._read_python_producer_receipts`` each read
    ``effective_action or requested_action``, a fallback the first one's own
    docstring justified as "legacy receipts that predate it". No such receipt
    exists at any door: BOTH schemas require ``effective_action`` and require
    it non-empty, while ``requested_action`` is the one of the pair that may be
    empty — so the fallback could only ever fire on a receipt no reader admits,
    and it would fall back TO the weaker field. Every consumer of both
    predicates loads through a validating reader (``read_receipt`` and
    ``PhysicalValidator._read_live_invocation_receipts``, both
    ``validate_receipt_v2``), so the branch is unreachable in the live loop.
    """
    from sag.agent.invocation_receipts import validate_receipt_v2

    live = _canonical_receipt()
    assert validate_receipt_v2(live)["effective_action"] == "test"
    historical = {
        field: value
        for field, value in _canonical_receipt(schema_version=1).items()
        if field not in {"run_id", "actual_cwd"}
    }
    assert validate_receipt_v2(historical, live=False)["effective_action"] == "test"

    for missing in ("", None):
        broken = dict(live)
        broken["effective_action"] = missing
        with pytest.raises(ValueError, match="effective_action"):
            validate_receipt_v2(broken)
        old = dict(historical)
        old["effective_action"] = missing
        with pytest.raises(ValueError, match="effective_action"):
            validate_receipt_v2(old, live=False)
    dropped = {k: v for k, v in live.items() if k != "effective_action"}
    with pytest.raises(ValueError, match="missing="):
        validate_receipt_v2(dropped)
    # …and the surviving read is the EFFECTIVE one, in both directions: what
    # physically ran decides, never what was asked for.
    from sag.agent.attempt_policy import _production_build_receipt

    assert _production_build_receipt({"effective_action": "test", "requested_action": "deps"})
    assert not _production_build_receipt({"effective_action": "deps", "requested_action": "test"})


# ---------------------------------------------------------------------------
# Gradle test evidence fields (evidence study 2026-08-30). Two ADDITIVE
# optional sections on the same schema v2: the complete per (project, task-dir)
# totals, and what the bounded identity harvest beside them had to drop. Absent
# stays absent, every cap that fires is stated, and neither section can be
# carried by a runner that never harvested it.
# ---------------------------------------------------------------------------

GRADLE_SUITE = {
    "module": ":clients",
    "task": "test",
    "xml_files": 231,
    "tests": 8123,
    "failures": 3,
    "errors": 0,
    "skipped": 12,
}
GRADLE_DISCLOSURE = {"rows_source": "gradle_xml", "red_rows_complete": True}


def _claimed_reports(count, root="/workspace/proj"):
    """`{path: sha}` for `count` claimed reports, as a snapshot states them.

    A disclosure counts files it dropped OUT OF this receipt's own claim set,
    so a receipt that states 900 dropped reports has to be a receipt that
    claimed at least 900 (plan r2 T3). The claims are what make the numbers
    below a bounded read rather than a number nobody could have measured.
    """

    return {
        f"{root}/m{index // 100}/build/test-results/test/TEST-{index:04d}.xml": f"{index:064x}"
        for index in range(count)
    }


def _claim_delta(count, root="/workspace/proj"):
    return {
        "new": [
            {"path": path, "sha256": digest}
            for path, digest in sorted(_claimed_reports(count, root).items())
        ],
        "changed": [],
    }


def _gradle_receipt(**overrides):
    payload = _canonical_receipt(
        tool="gradle",
        argv="./gradlew test",
        receipt_id="inv-gradle-test-0001",
    )
    payload.update(overrides)
    return payload


def test_gradle_evidence_sections_stay_absent_when_the_harvest_stated_nothing():
    """Additive means additive: an untouched receipt is byte-identical."""
    from sag.agent.invocation_receipts import validate_receipt_v2

    receipt = build_receipt(
        receipt_id="inv-gradle-test-0001",
        run_id="run-gradle",
        tool="gradle",
        requested_action="test",
        effective_action="test",
        argv="./gradlew test",
        working_directory="/workspace/proj",
        exit_code=0,
        before={},
        after={},
        gradle_suite_summaries=None,
        gradle_row_disclosure=None,
    )

    assert "gradle_suite_summaries" not in receipt
    assert "gradle_row_disclosure" not in receipt
    assert "evidence_omissions" not in receipt
    assert validate_receipt_v2(receipt)["schema_version"] == RECEIPT_SCHEMA_VERSION
    # The schema version does NOT move for an additive optional field. It moved
    # to 3 in r2 because the MEANING of the gradle sections changed — totals
    # became load-bearing and identity rows a disclosed sample — and there is
    # no compatibility machinery: 3 is the only version a live reader admits.
    assert RECEIPT_SCHEMA_VERSION == 3


def test_a_full_gradle_evidence_receipt_validates_on_the_live_schema():
    from sag.agent.invocation_receipts import validate_receipt_v2

    receipt = build_receipt(
        receipt_id="inv-gradle-test-0002",
        run_id="run-gradle",
        tool="gradle",
        requested_action="test",
        effective_action="test",
        argv="./gradlew test",
        working_directory="/workspace/proj",
        exit_code=0,
        before={},
        after=_claimed_reports(900),
        gradle_suite_summaries={
            "suites": [GRADLE_SUITE],
            "truncated": True,
            "dropped_suites": 4,
            "unreadable_suites": 1,
        },
        gradle_row_disclosure={
            "rows_source": "gradle_xml",
            "red_rows_complete": False,
            "rows_truncated": {"dropped_green": 27000, "dropped_files": 900, "dropped_red": 2},
        },
    )

    validated = validate_receipt_v2(receipt)
    assert validated["schema_version"] == 3
    assert validated["gradle_suite_summaries"]["suites"] == [GRADLE_SUITE]
    assert validated["gradle_row_disclosure"]["rows_truncated"]["dropped_red"] == 2
    assert "evidence_omissions" not in validated


@pytest.mark.parametrize("field", ["gradle_suite_summaries", "gradle_row_disclosure"])
def test_a_runner_that_never_harvested_gradle_reports_cannot_carry_its_sections(field):
    """A maven or pytest receipt stating Gradle evidence is stating nothing."""
    from sag.agent.invocation_receipts import validate_receipt_v2

    value = {"suites": [GRADLE_SUITE]} if field.endswith("summaries") else dict(GRADLE_DISCLOSURE)
    for tool, argv in (("maven", "mvn test"), ("python", "python -m pytest")):
        with pytest.raises(ValueError, match=field):
            validate_receipt_v2(_gradle_receipt(tool=tool, argv=argv, **{field: value}))
    assert validate_receipt_v2(_gradle_receipt(**{field: value}))[field] == value


@pytest.mark.parametrize(
    "section, refusal",
    [
        ({"suites": []}, "suites"),
        ({}, "suites"),
        ({"suites": [GRADLE_SUITE], "unknown": 1}, "shape"),
        ({"suites": [{**GRADLE_SUITE, "extra": 1}]}, "entry shape"),
        (
            {"suites": [{key: value for key, value in GRADLE_SUITE.items() if key != "task"}]},
            "shape",
        ),
        ({"suites": [{**GRADLE_SUITE, "module": ""}]}, "module"),
        ({"suites": [{**GRADLE_SUITE, "task": "test\nrun"}]}, "task"),
        ({"suites": [{**GRADLE_SUITE, "xml_files": 0}]}, "xml_files"),
        ({"suites": [{**GRADLE_SUITE, "tests": -1}]}, "tests"),
        ({"suites": [{**GRADLE_SUITE, "failures": True}]}, "failures"),
        ({"suites": [{**GRADLE_SUITE, "skipped": "12"}]}, "skipped"),
        ({"suites": [GRADLE_SUITE, dict(GRADLE_SUITE)]}, "repeats a module task pair"),
        ({"suites": [GRADLE_SUITE], "truncated": True}, "state what it dropped"),
        ({"suites": [GRADLE_SUITE], "dropped_suites": 3}, "state what it dropped"),
        ({"suites": [GRADLE_SUITE], "truncated": False, "dropped_suites": 3}, "truncated"),
        ({"suites": [GRADLE_SUITE], "truncated": True, "dropped_suites": 0}, "dropped_suites"),
        ({"suites": [GRADLE_SUITE], "unreadable_suites": 0}, "unreadable_suites"),
    ],
)
def test_the_gradle_summary_section_refuses_a_shape_it_cannot_stand_behind(section, refusal):
    from sag.agent.invocation_receipts import validate_receipt_v2

    with pytest.raises(ValueError, match=refusal):
        validate_receipt_v2(_gradle_receipt(gradle_suite_summaries=section))


def test_the_gradle_summary_section_is_bounded_by_its_own_declared_cap():
    from sag.agent.invocation_receipts import GRADLE_SUITE_SUMMARY_CAP, validate_receipt_v2

    def suites(count):
        return [dict(GRADLE_SUITE, module=f":m{index:04d}") for index in range(count)]

    at_cap = _gradle_receipt(gradle_suite_summaries={"suites": suites(GRADLE_SUITE_SUMMARY_CAP)})
    assert len(validate_receipt_v2(at_cap)["gradle_suite_summaries"]["suites"]) == 256
    over = _gradle_receipt(gradle_suite_summaries={"suites": suites(GRADLE_SUITE_SUMMARY_CAP + 1)})
    with pytest.raises(ValueError, match="gradle_suite_summaries.suites"):
        validate_receipt_v2(over)


@pytest.mark.parametrize(
    "disclosure, refusal",
    [
        ({"rows_source": "gradle_xml"}, "red completeness"),
        ({"red_rows_complete": True}, "red completeness"),
        ({**GRADLE_DISCLOSURE, "unknown": 1}, "shape"),
        ({**GRADLE_DISCLOSURE, "rows_source": "model_summary"}, "rows_source"),
        ({**GRADLE_DISCLOSURE, "rows_source": ""}, "rows_source"),
        ({**GRADLE_DISCLOSURE, "red_rows_complete": 1}, "red_rows_complete"),
        ({**GRADLE_DISCLOSURE, "red_rows_complete": "true"}, "red_rows_complete"),
        ({**GRADLE_DISCLOSURE, "rows_truncated": {"dropped_green": 1}}, "incomplete"),
        (
            {**GRADLE_DISCLOSURE, "rows_truncated": {"dropped_green": 0, "dropped_files": 0}},
            "dropped nothing",
        ),
        (
            {**GRADLE_DISCLOSURE, "rows_truncated": {"dropped_green": -1, "dropped_files": 0}},
            "dropped_green",
        ),
        (
            {**GRADLE_DISCLOSURE, "rows_truncated": {"dropped_green": 1, "dropped_files": True}},
            "dropped_files",
        ),
        (
            {
                **GRADLE_DISCLOSURE,
                "rows_truncated": {"dropped_green": 1, "dropped_files": 1, "surprise": 1},
            },
            "rows_truncated shape",
        ),
        # A stated in-file loss states a number; zero of them is not a loss and
        # a truncation record that says so is describing nothing.
        (
            {
                **GRADLE_DISCLOSURE,
                "rows_truncated": {
                    "dropped_green": 0,
                    "dropped_files": 0,
                    "unread_rows": 0,
                },
            },
            "unread_rows",
        ),
        (
            {
                **GRADLE_DISCLOSURE,
                "rows_truncated": {
                    "dropped_green": 0,
                    "dropped_files": 0,
                    "unread_rows": True,
                },
            },
            "unread_rows",
        ),
    ],
)
def test_the_gradle_row_disclosure_refuses_a_shape_it_cannot_stand_behind(disclosure, refusal):
    from sag.agent.invocation_receipts import validate_receipt_v2

    with pytest.raises(ValueError, match=refusal):
        validate_receipt_v2(_gradle_receipt(gradle_row_disclosure=disclosure))


def test_a_disclosure_cannot_drop_a_red_and_still_claim_the_reds_are_complete():
    """The one claim a sampled row list offers is the one that is checked."""
    from sag.agent.invocation_receipts import validate_receipt_v2

    claims = _claim_delta(900)
    dropped_red = {"dropped_green": 4, "dropped_files": 1, "dropped_red": 2}
    with pytest.raises(ValueError, match="cannot drop a red and claim completeness"):
        validate_receipt_v2(
            _gradle_receipt(
                report_delta=claims,
                gradle_row_disclosure={
                    "rows_source": "gradle_xml",
                    "red_rows_complete": True,
                    "rows_truncated": dropped_red,
                },
            )
        )
    honest = _gradle_receipt(
        report_delta=claims,
        gradle_row_disclosure={
            "rows_source": "gradle_xml",
            "red_rows_complete": False,
            "rows_truncated": dropped_red,
        },
    )
    assert validate_receipt_v2(honest)["gradle_row_disclosure"]["rows_truncated"] == dropped_red
    # Dropping only greens keeps the claim available — that is the whole point
    # of ordering reds first.
    greens_only = _gradle_receipt(
        report_delta=claims,
        gradle_row_disclosure={
            "rows_source": "gradle_xml",
            "red_rows_complete": True,
            "rows_truncated": {"dropped_green": 27000, "dropped_files": 900},
        },
    )
    assert validate_receipt_v2(greens_only)["gradle_row_disclosure"]["red_rows_complete"] is True


@pytest.mark.parametrize(
    "field, value",
    [
        ("gradle_suite_summaries", {"suites": [{**GRADLE_SUITE, "module": "a\nb"}]}),
        ("gradle_row_disclosure", {"rows_source": "hand_counted", "red_rows_complete": True}),
        # Not a mapping at all: the drop still costs one field, never the copy
        # that assembles it.
        ("gradle_suite_summaries", [GRADLE_SUITE]),
        ("gradle_row_disclosure", "red_rows_complete"),
    ],
)
def test_unrepresentable_gradle_evidence_is_dropped_alone_and_stated_as_an_omission(field, value):
    """Live camel and live kafka: one bad evidence field voided whole receipts.

    A Gradle section the schema refuses must cost that section and nothing
    else — the exit code, the argv and the report delta survive, and the drop
    is a stated omission rather than a silent absence.
    """
    from sag.agent.invocation_receipts import receipt_refusal_code, validate_receipt_v2

    receipt = build_receipt(
        receipt_id="inv-gradle-test-0003",
        run_id="run-gradle",
        tool="gradle",
        requested_action="test",
        effective_action="test",
        argv="./gradlew test",
        working_directory="/workspace/proj",
        exit_code=1,
        before={},
        after={},
        module_outcomes=[{"module": ":clients", "status": "attempted"}],
        **{field: value},
    )

    assert field not in receipt
    assert receipt["exit_code"] == 1
    assert receipt["module_outcomes"] == [{"module": ":clients", "status": "attempted"}]
    assert [entry["field"] for entry in receipt["evidence_omissions"]] == [field]
    assert receipt["evidence_omissions"][0]["status"] == "unavailable"
    assert receipt["evidence_omissions"][0]["reasons"]
    assert validate_receipt_v2(receipt)["evidence_omissions"][0]["field"] == field
    # And the refusal names the argument, so a caller can repair it.
    assert receipt_refusal_code(ValueError(f"receipt {field} shape is invalid")).endswith(field)


def test_record_invocation_carries_the_gradle_sections_to_the_one_assembly_point():
    """Settlement parity: the detached path calls the SAME `record_invocation`.

    A Gradle section only the synchronous caller could pass would be missing
    from every receipt settled from a job obligation — which is exactly how the
    measured kafka run lost its evidence.
    """
    execute = FakeExecute()
    metadata = record_invocation(
        execute,
        receipt_id="inv-gradle-test-0004",
        run_id="run-pytest",
        tool="gradle",
        attempt=1,
        requested_action="test",
        effective_action="test",
        argv="./gradlew test",
        working_directory="/workspace/proj",
        exit_code=0,
        before={},
        after={},
        gradle_suite_summaries={"suites": [GRADLE_SUITE]},
        gradle_row_disclosure=dict(GRADLE_DISCLOSURE),
    )

    assert metadata == {"receipt_id": "inv-gradle-test-0004"}
    written = receipts_written(execute.commands)
    assert len(written) == 1
    assert written[0]["gradle_suite_summaries"]["suites"] == [GRADLE_SUITE]
    assert written[0]["gradle_row_disclosure"] == GRADLE_DISCLOSURE


# --- receipt-level reconciliation (plan r2 T3; blocker 3b) ------------------
#
# The owner probed the r1 branch live and persisted `tests=1, failures=2`. Every
# validator up to here checks ONE field's shape, so a receipt could state suite
# totals, a module witness and an identity sample that contradicted each other
# freely and still validate. Each test below carries the impossible payload
# itself and proves the receipt cannot be constructed around it — and, where a
# writer produces one, that the contradiction costs that one field and never the
# exit code, the argv, the contract binding or the report delta.


def _reconciling_receipt(**overrides):
    """One gradle receipt whose totals, witness and sample already agree."""

    payload = _gradle_receipt(
        report_delta=_claim_delta(4),
        gradle_suite_summaries={
            "suites": [
                {
                    "module": ":clients",
                    "task": "test",
                    "xml_files": 4,
                    "tests": 20,
                    "failures": 1,
                    "errors": 0,
                    "skipped": 2,
                }
            ]
        },
        module_outcomes=[{"module": "clients", "status": "attempted", "tests_reported": 20}],
    )
    payload.update(overrides)
    return payload


def test_a_suite_that_failed_more_tests_than_it_ran_is_unconstructible():
    """THE probe: `tests=1, failures=2`, accepted live on the r1 branch.

    `tests` counts the testcases the reports declared and the other three count
    dispositions OF those testcases, so their sum cannot exceed it. Conservation
    is a property of one suite entry, so it is checked where that entry is —
    and the receipt that would have carried it does not exist.
    """
    from sag.agent.invocation_receipts import validate_receipt_v2

    impossible = {
        "module": ":clients",
        "task": "test",
        "xml_files": 1,
        "tests": 1,
        "failures": 2,
        "errors": 0,
        "skipped": 0,
    }
    with pytest.raises(ValueError, match="more outcomes than it ran tests"):
        validate_receipt_v2(_gradle_receipt(gradle_suite_summaries={"suites": [impossible]}))
    # Every disposition counted and the total exactly spent: legal, because a
    # suite may end with no test left ungraded.
    exact = {**impossible, "tests": 4, "failures": 2, "errors": 1, "skipped": 1}
    assert validate_receipt_v2(_gradle_receipt(gradle_suite_summaries={"suites": [exact]}))
    # And the writer degrades rather than voiding: the totals section is dropped
    # alone, with the refusal stated, while the receipt keeps everything else.
    receipt = build_receipt(
        receipt_id="inv-gradle-test-0100",
        run_id="run-gradle",
        tool="gradle",
        requested_action="test",
        effective_action="test",
        argv="./gradlew test",
        working_directory="/workspace/proj",
        exit_code=1,
        before={},
        after={},
        gradle_suite_summaries={"suites": [impossible]},
    )
    assert "gradle_suite_summaries" not in receipt
    assert receipt["exit_code"] == 1
    assert receipt["evidence_omissions"][0]["field"] == "gradle_suite_summaries"


def test_a_module_witness_that_contradicts_its_own_suite_totals_is_unconstructible():
    """`tests_reported` is summed from the same report roots as the totals.

    Where both speak, they state one number. The r1 receipt let them disagree
    by any margin — a module witnessing 27,219 tests beside totals declaring
    20 — because nothing ever compared them.
    """
    from sag.agent.invocation_receipts import validate_receipt_v2

    assert validate_receipt_v2(_reconciling_receipt())["module_outcomes"][0]["tests_reported"] == 20
    for reported in (19, 21, 27_219):
        with pytest.raises(ValueError, match="contradicts its gradle_suite_summaries"):
            validate_receipt_v2(
                _reconciling_receipt(
                    module_outcomes=[
                        {"module": "clients", "status": "attempted", "tests_reported": reported}
                    ]
                )
            )
    # A witness for a module the complete totals never summed is a count from
    # nowhere.
    with pytest.raises(ValueError, match="never summed"):
        validate_receipt_v2(
            _reconciling_receipt(
                module_outcomes=[{"module": "streams", "status": "attempted", "tests_reported": 9}]
            )
        )


def test_a_bounded_summary_section_lets_the_witness_exceed_it_but_never_fall_short():
    """A pair cap that fired removes totals, not tests.

    The witness sums every report the harvest read; the section shows the pairs
    that survived its own bound. So the witness may be LARGER than what the
    section still displays — and smaller is impossible, because the displayed
    pairs are part of what the witness summed.
    """
    from sag.agent.invocation_receipts import validate_receipt_v2

    bounded = {
        "suites": [
            {
                "module": ":clients",
                "task": "test",
                "xml_files": 4,
                "tests": 20,
                "failures": 1,
                "errors": 0,
                "skipped": 2,
            }
        ],
        "truncated": True,
        "dropped_suites": 3,
    }
    larger = _reconciling_receipt(
        gradle_suite_summaries=bounded,
        module_outcomes=[{"module": "clients", "status": "attempted", "tests_reported": 900}],
    )
    assert validate_receipt_v2(larger)["module_outcomes"][0]["tests_reported"] == 900
    smaller = _reconciling_receipt(
        gradle_suite_summaries=bounded,
        module_outcomes=[{"module": "clients", "status": "attempted", "tests_reported": 19}],
    )
    with pytest.raises(ValueError, match="smaller than the suite totals"):
        validate_receipt_v2(smaller)


def test_a_module_that_ran_and_found_nothing_is_not_an_execution_witness():
    """`tests_reported = 0` is a disclosed state, never proof of execution.

    It says the reports were read and declared no test — "ran, found none" —
    which is a different fact from a module with no count at all, and a
    different fact again from a module that executed tests. It is carried only
    where the reports that found none were actually summed; a bare zero over a
    receipt that summed nothing is a claim about files nobody read.
    """
    from sag.agent.invocation_receipts import module_execution_witnessed, validate_receipt_v2

    ran_found_none = _reconciling_receipt(
        gradle_suite_summaries={
            "suites": [
                {
                    "module": ":clients",
                    "task": "test",
                    "xml_files": 1,
                    "tests": 0,
                    "failures": 0,
                    "errors": 0,
                    "skipped": 0,
                }
            ]
        },
        module_outcomes=[{"module": "clients", "status": "attempted", "tests_reported": 0}],
    )
    validated = validate_receipt_v2(ran_found_none)
    assert validated["module_outcomes"][0]["tests_reported"] == 0
    # Stated, and still not a witness — the one reading every consumer takes.
    assert module_execution_witnessed(validated["module_outcomes"][0]) is False
    assert module_execution_witnessed({"module": "clients", "status": "attempted"}) is False
    assert module_execution_witnessed({"module": "clients", "tests_reported": 1}) is True
    with pytest.raises(ValueError, match="states zero for a module"):
        validate_receipt_v2(
            _gradle_receipt(
                module_outcomes=[{"module": "clients", "status": "attempted", "tests_reported": 0}]
            )
        )


def test_an_identity_sample_cannot_carry_more_reds_than_the_totals_declare():
    """The rows are a sample of the executions the totals counted.

    A sample holds fewer identities than the run had — never more, and never a
    failure the reports never declared. This is `tests=1, failures=2` one tier
    down, between the totals and the identities beside them.
    """
    from sag.agent.invocation_receipts import validate_receipt_v2

    def outcomes(reds):
        return {
            "nodes": [
                {"node_id": f"com.acme.Suite#red{index}", "status": "failed"}
                for index in range(reds)
            ]
        }

    assert validate_receipt_v2(_reconciling_receipt(testcase_outcomes=outcomes(1)))
    with pytest.raises(ValueError, match="more red identities"):
        validate_receipt_v2(_reconciling_receipt(testcase_outcomes=outcomes(2)))


def test_a_sample_cannot_account_for_more_executions_than_the_run_ran():
    """kept + dropped == observed, and `observed` is what the totals counted.

    A disclosure stating 27,000 dropped rows beside totals declaring 20 tests
    is not a bound that fired hard; it is arithmetic that cannot close.
    """
    from sag.agent.invocation_receipts import validate_receipt_v2

    def disclosure(dropped_green):
        return {
            "rows_source": "gradle_xml",
            "red_rows_complete": False,
            "rows_truncated": {"dropped_green": dropped_green, "dropped_files": 1},
        }

    assert validate_receipt_v2(_reconciling_receipt(gradle_row_disclosure=disclosure(20)))
    with pytest.raises(ValueError, match="more executions than its gradle_suite_summaries"):
        validate_receipt_v2(_reconciling_receipt(gradle_row_disclosure=disclosure(27_000)))


def test_a_disclosure_cannot_drop_more_reports_than_the_receipt_claims():
    """`dropped_files` counts reports out of THIS receipt's claim set.

    The identity pass reads the delta's own claims and nothing else, so a
    sample that dropped 900 files on a four-report delta is describing a read
    that never happened.
    """
    from sag.agent.invocation_receipts import validate_receipt_v2

    def disclosure(files):
        return {
            "rows_source": "gradle_xml",
            "red_rows_complete": False,
            "rows_truncated": {"dropped_green": 0, "dropped_files": files},
        }

    assert validate_receipt_v2(_reconciling_receipt(gradle_row_disclosure=disclosure(4)))
    with pytest.raises(ValueError, match="drops more reports than the report_delta claims"):
        validate_receipt_v2(_reconciling_receipt(gradle_row_disclosure=disclosure(900)))


def test_one_identity_list_may_not_carry_two_disclosures():
    """The r1 repair, hardened from a caller's care into a schema rule.

    `gradle_row_disclosure` describes the harvest's diagnostic list;
    `testcase_row_disclosure` describes the exact delta parse's rows, or — with
    none carried — the diagnostic list it produced instead. Both over one list
    means one of them states drops from a sample the surviving list never came
    from, which is the contradiction the r1 fix made unreachable by convention.
    """
    from sag.agent.invocation_receipts import validate_receipt_v2

    exact = {
        "rows_source": "delta_xml",
        "red_rows_complete": False,
        "rows_truncated": {"dropped_green": 3, "dropped_files": 1},
    }
    contested = _reconciling_receipt(
        testcase_outcomes={"nodes": [{"node_id": "com.acme.Suite#green", "status": "passed"}]},
        gradle_row_disclosure={"rows_source": "gradle_xml", "red_rows_complete": False},
        testcase_row_disclosure=exact,
    )
    with pytest.raises(ValueError, match="same list as gradle_row_disclosure"):
        validate_receipt_v2(contested)
    # A disclosure with no list at all describes nothing.
    with pytest.raises(ValueError, match="a list the receipt does not carry"):
        validate_receipt_v2(_reconciling_receipt(testcase_row_disclosure=exact))
    # And the writer never assembles the contested pair in the first place: the
    # exact record has no rows of its own to describe, so it never rides, and
    # the harvest's list and disclosure stand.
    receipt = build_receipt(
        receipt_id="inv-gradle-test-0101",
        run_id="run-gradle",
        tool="gradle",
        requested_action="test",
        effective_action="test",
        argv="./gradlew test",
        working_directory="/workspace/proj",
        exit_code=0,
        before={},
        after={},
        testcase_outcomes={"nodes": [{"node_id": "com.acme.Suite#green", "status": "passed"}]},
        gradle_row_disclosure={"rows_source": "gradle_xml", "red_rows_complete": False},
        testcase_row_disclosure=exact,
    )
    assert "testcase_row_disclosure" not in receipt
    assert receipt["gradle_row_disclosure"] == {
        "rows_source": "gradle_xml",
        "red_rows_complete": False,
    }
    assert receipt["testcase_outcomes"]["nodes"]


def test_complete_reds_over_rows_the_read_never_built_needs_a_witness():
    """An unread row has no stated outcome, so it could be a red.

    `unread_rows` is the per-file bound's own loss — identities a report
    declared and the bounded read never delivered. Claiming every red is
    present over them is only true where the totals account for every red and
    the sample carries them all.
    """
    from sag.agent.invocation_receipts import validate_receipt_v2

    unread = {
        "rows_source": "gradle_xml",
        "red_rows_complete": True,
        "rows_truncated": {"dropped_green": 0, "dropped_files": 0, "unread_rows": 3},
    }
    with pytest.raises(ValueError, match="complete reds over rows it never read"):
        validate_receipt_v2(_reconciling_receipt(gradle_row_disclosure=unread))
    # With the declared red carried, the witness stands behind the claim.
    witnessed = _reconciling_receipt(
        gradle_row_disclosure=unread,
        testcase_outcomes={"nodes": [{"node_id": "com.acme.Suite#red", "status": "failed"}]},
    )
    assert validate_receipt_v2(witnessed)["gradle_row_disclosure"]["red_rows_complete"] is True
    # Withdrawing the claim is always available, and states the same loss.
    honest = _reconciling_receipt(gradle_row_disclosure={**unread, "red_rows_complete": False})
    validated = validate_receipt_v2(honest)
    assert validated["gradle_row_disclosure"]["rows_truncated"]["unread_rows"] == 3


@pytest.mark.parametrize(
    "loss",
    [
        {"unreadable_suites": 2},
        {"unsummarized_files": 900},
        {"post_snapshot_rewrite": {"files": 1, "paths": ["/workspace/proj/a.xml"]}},
    ],
)
def test_the_totals_own_losses_withdraw_the_harvests_completeness_claim(loss):
    """One read produced the totals and the sample beside them.

    So a claimed report those totals never summed is a report this sample never
    saw either — bounded out, unparsable, or excluded because its bytes moved —
    and any of them can hold a failure. `red_rows_complete` is the one claim a
    sample offers, and it does not survive a bound that could hide a red.

    A dropped PAIR is different and stays allowed: the pair cap keeps
    red-bearing pairs first, so what it sheds is green.
    """
    from sag.agent.invocation_receipts import validate_receipt_v2

    section = validate_receipt_v2(_reconciling_receipt())["gradle_suite_summaries"]
    with pytest.raises(ValueError, match="left a claimed report unread"):
        validate_receipt_v2(
            _reconciling_receipt(
                gradle_suite_summaries={**section, **loss},
                gradle_row_disclosure={"rows_source": "gradle_xml", "red_rows_complete": True},
            )
        )
    stated = _reconciling_receipt(
        gradle_suite_summaries={**section, **loss},
        gradle_row_disclosure={"rows_source": "gradle_xml", "red_rows_complete": False},
    )
    assert validate_receipt_v2(stated)["gradle_row_disclosure"]["red_rows_complete"] is False
    capped_pairs = _reconciling_receipt(
        gradle_suite_summaries={**section, "truncated": True, "dropped_suites": 3},
        gradle_row_disclosure={"rows_source": "gradle_xml", "red_rows_complete": True},
    )
    assert validate_receipt_v2(capped_pairs)["gradle_row_disclosure"]["red_rows_complete"] is True
