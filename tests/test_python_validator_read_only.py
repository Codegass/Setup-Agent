"""Python physical judgment is an observer, never an evidence producer."""

import hashlib
import json
import shlex

import pytest
from build_requirements_fakes import complete_python_build_requirements_v1
from container_evidence_fakes import complete_run_pin
from test_container_io import FakeContainer

from sag.agent.action_intents import action_fingerprint
from sag.agent.control_events import canonical_json
from sag.agent.evidence_publications import (
    BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
    EVIDENCE_PUBLICATION_GENESIS_SHA256,
    RUN_PIN_LOGICAL_ARTIFACT_ID,
    latest_publication_raw_sha256,
    publish_evidence_bytes,
    publish_evidence_revision,
)
from sag.agent.evidence_records import frame_json_record_stream, frame_named_json_record_stream
from sag.agent.evidence_state import RunEvidenceState
from sag.agent.invocation_contracts import (
    CONTRACT_DIR,
    PYTHON_FACADE_EXECUTION_BINDING,
    build_contract,
    contract_hash,
)
from sag.agent.invocation_receipts import (
    build_receipt,
    producer_observations_sha256,
)
from sag.agent.phase_gates import check_phase_claim
from sag.agent.phase_machine import PhaseClaim, PhaseOutcome
from sag.agent.physical_validator import PhysicalValidator
from sag.agent.verdict_finalizer import EvidenceCloseReason, VerdictFinalizer
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH


# The current-manifest fingerprint tuple every fixture pins. A valid v1 survey
# carries target-sha + survey + config + document-map fingerprints; a python
# project carries no build_domains/domain_facts, so contracts and receipts pin
# exactly this tuple and leave domain/epoch pins absent on BOTH sides, which
# the pairwise pin semantics read as agreement rather than a binding hole.
_CONFIG_FINGERPRINT = "config-current"
_DOCUMENT_MAP_FINGERPRINT = "d" * 64
_SITE = "/workspace/proj/.venv/lib/python3.12/site-packages"


def _python_manifest():
    """One complete, self-stamped v1 manifest — the current-authority side."""

    return complete_python_build_requirements_v1(
        project_root="/workspace/proj",
        target_sha="a" * 40,
        config_fingerprint=_CONFIG_FINGERPRINT,
        document_map_fingerprint=_DOCUMENT_MAP_FINGERPRINT,
        python_version="3.12",
        python_constraint=">=3.9",
        python_constraint_source=None,
        python_install_source=None,
        python_build_backend=None,
        python_packages=["side_effect_pkg"],
        python_distribution_name="side_effect_pkg",
    )


# Every live manifest now states a survey_fingerprint, so a contract/receipt
# that omitted the pin would be one-sided — a binding hole, not agreement.
# Fixtures therefore pin the manifest's OWN stamp.
_SURVEY_FINGERPRINT = _python_manifest()["survey"]["survey_fingerprint"]


class ProjectExecutionTrap:
    """A Python project whose import would be an observable side effect.

    The fake raises before executing any project/venv binary.  Safe filesystem
    probes receive enough answers for validation, the phase gate, and the final
    evidence close to finish normally.
    """

    def __init__(self):
        self.commands = []
        self.project_executions = []
        self._atomic_container = FakeContainer()
        self.files = self._atomic_container.files
        # Live-authority migration: the published head must be a complete v1
        # manifest, and exact distribution ownership requires the surveyed
        # distribution name (its record probes are answered read-only below).
        # A fresh copy per fixture: tests move the survey's own pins on it.
        self.manifest = _python_manifest()
        self.publish_manifest_update(initial=True)

    def publish_manifest_update(self, *, initial=False):
        previous = (
            EVIDENCE_PUBLICATION_GENESIS_SHA256
            if initial
            else latest_publication_raw_sha256(
                self,
                BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            )
        )
        publication = publish_evidence_revision(
            self,
            record_kind="build_requirements",
            record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            raw=canonical_json(self.manifest).encode("utf-8"),
            expected_previous_raw_sha256=previous,
        )
        if not publication.published:
            raise AssertionError(f"test manifest publication failed: {publication.status}")

    def read_file(self, path):
        if path == REQUIREMENTS_PATH:
            return {
                "success": True,
                "exit_code": 0,
                "content": canonical_json(self.manifest),
            }
        if path in self.files:
            return {"success": True, "exit_code": 0, "content": self.files[path]}
        return None

    def execute_command(self, command, **_kwargs):
        self.commands.append(command)
        if (
            "/workspace/proj/.venv/bin/" in command
            or "import side_effect_pkg" in command
            or "-m compileall" in command
        ):
            self.project_executions.append(command)
            raise AssertionError(f"physical judge executed project code: {command}")

        def result(ok=True, output=""):
            return {"success": ok, "exit_code": 0 if ok else 1, "output": output}

        stripped = command.strip()
        tokens = shlex.split(command)
        if (
            tokens[:3] == ["mkdir", "-p", "--"]
            or tokens[:2] == ["rm", "-f"]
            or tokens[:2] == [":", ">"]
            or tokens[:2] == ["printf", "%s"]
            or tokens[:2] == ["base64", "--decode"]
            or tokens[:3] == ["mv", "-f", "--"]
            or (
                tokens[:2] == ["python3", "-c"]
                and any(
                    marker in tokens[2] for marker in ("hashlib.sha256", "json.load", "fcntl.flock")
                )
            )
        ):
            return self._atomic_container.execute_command(command)
        if "SAG_NAMED_JSON_RECORD_V1" in stripped and REQUIREMENTS_PATH in stripped:
            return result(
                True,
                frame_named_json_record_stream(
                    [("build-requirements.json", canonical_json(self.manifest))]
                ),
            )
        snapshot_path = "/workspace/.setup_agent/verdict.json"
        if "SAG_NAMED_JSON_RECORD_V1" in stripped and snapshot_path in stripped:
            records = (
                [("verdict.json", self.files[snapshot_path])] if snapshot_path in self.files else []
            )
            return result(True, frame_named_json_record_stream(records))
        if "SAG_NAMED_JSON_RECORD_V1" in stripped:
            return result(True, frame_named_json_record_stream([]))
        if "SAG_JSON_RECORD_V1" in stripped and "/workspace/.setup_agent/" in stripped:
            return result(True, frame_json_record_stream([]))
        if stripped == f"test -f {snapshot_path} && cat {snapshot_path}":
            if snapshot_path in self.files:
                return result(True, self.files[snapshot_path])
            return result(False, "")
        if command.startswith("cat > "):
            target = command.split()[2]
            payload = command.split("\n", 1)[1].rsplit("\n", 1)[0]
            self.files[target] = payload + "\n"
            return result(True, "")
        if stripped.startswith("truncate -s -1 "):
            target = stripped.split()[-1]
            if target not in self.files:
                return result(False, "")
            self.files[target] = self.files[target][:-1]
            return result(True, "")
        if stripped.startswith("mv "):
            _, source, target = stripped.split()
            if source not in self.files:
                return result(False, "")
            self.files[target] = self.files.pop(source)
            return result(True, "")
        if stripped in (f"cat {REQUIREMENTS_PATH}", f"cat -- {REQUIREMENTS_PATH}"):
            return result(True, canonical_json(self.manifest))
        if stripped.startswith("test -f "):
            return result(stripped.endswith("/pyproject.toml"))
        if stripped.startswith("test -d "):
            path = stripped.split()[2]
            return result(path in {"/workspace/proj/.venv", "/workspace/proj/side_effect_pkg"})
        # Exact-distribution ownership probes (read-only; never /.venv/bin/).
        if stripped.startswith("find") and "site-packages" in stripped and "dist-info" in stripped:
            return result(True, f"{_SITE}/side_effect_pkg-1.0.dist-info")
        if stripped.startswith("cat") and stripped.split()[1].endswith("/direct_url.json"):
            return result(True, '{"url": "file:///workspace/proj", "dir_info": {"editable": true}}')
        if stripped.startswith("cat") and stripped.split()[1].endswith("/top_level.txt"):
            return result(True, "side_effect_pkg\n")
        if stripped.startswith("realpath -m -- "):
            return result(True, "\n".join(shlex.split(stripped)[3:]))
        if "site-packages" in stripped and ("dist-info" in stripped or "egg-info" in stripped):
            return result(True, "")
        if "'*.jar'" in stripped or "'*.class'" in stripped:
            return result(True, "0")
        if "'*.so'" in stripped or "'*.dylib'" in stripped:
            return result(True, "")
        if "python3 --version" in stripped:
            # This is intentionally available to catch whether the judge still
            # executes an ambient interpreter instead of reading overlay facts.
            self.project_executions.append(command)
            raise AssertionError(f"physical judge executed an interpreter: {command}")
        return result(True, "")

    def execute_control_command(self, command, **kwargs):
        """Host-owned control channel (production surface for evidence I/O)."""
        return self.execute_command(command, **kwargs)


def _validator(orch):
    return PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")


def _assert_unknown_runtime_rungs(status):
    details = status["evidence"]["fingerprint_details"]
    assert details["venv_exists"] is True
    assert details["pip_check_clean"] is None
    assert details["imports_ok"] is None
    assert details["import_failures"] == []
    assert details["compileall_coverage"] is None
    assert details["compileall_metric_status"] == "unavailable"
    assert status["success"] is True
    assert status["build_complete"] is False
    assert status["evidence_status"] == "partial"
    assert "producer receipt" in status["reason"]


def test_python_validator_never_imports_compiles_or_runs_project_owned_python():
    orch = ProjectExecutionTrap()

    status = _validator(orch).validate_build_status("proj")

    _assert_unknown_runtime_rungs(status)
    assert orch.project_executions == []
    assert not any("__pycache__" in command for command in orch.commands)


def test_phase_gate_and_finalizer_do_not_manufacture_python_evidence():
    orch = ProjectExecutionTrap()
    validator = _validator(orch)

    gate = check_phase_claim(
        "build",
        PhaseClaim(phase="build", claimed_outcome=PhaseOutcome.PARTIAL),
        validator=validator,
        orchestrator=orch,
        project_name="proj",
    )
    snapshot = VerdictFinalizer(
        orch,
        validator=validator,
        project_name="proj",
    ).finalize(RunEvidenceState(run_id="run-pytest"), EvidenceCloseReason.ABORTED)

    assert gate.accepted is True
    assert snapshot.build_evidence.judgment == "partial"
    assert snapshot.build_evidence.source == "physical"
    assert orch.project_executions == []
    assert not any("-m compileall" in command for command in orch.commands)
    assert not any("import side_effect_pkg" in command for command in orch.commands)


_RUN_ID = "run-pytest"
_TARGET_SHA = "a" * 40
_SOURCE = "/workspace/proj/side_effect_pkg/module.py"
_PYC = "/workspace/proj/side_effect_pkg/__pycache__/module.cpython-312.pyc"
_FOREIGN_PYC = "/workspace/proj/side_effect_pkg/__pycache__/legacy.cpython-311.pyc"
_WHEEL = "/workspace/proj/dist/side_effect_pkg-1.0-py3-none-any.whl"
_SOURCE_SHA = hashlib.sha256(b"source-v1").hexdigest()
_PYC_SHA = hashlib.sha256(b"pyc-v1").hexdigest()
_FOREIGN_PYC_SHA = hashlib.sha256(b"foreign-pyc").hexdigest()
_WHEEL_SHA = hashlib.sha256(b"wheel-v1").hexdigest()


def _step(ordinal, role, *, outcome="completed", exit_code=0):
    return {
        "ordinal": ordinal,
        "role": role,
        "argv_sha256": f"{ordinal:x}" * 64,
        "outcome": outcome,
        "exit_code": exit_code,
        "output_sha256": f"{ordinal + 8:x}" * 64,
    }


def _setup_observations(*, include_postconditions=True):
    setup = {
        "venv": "/workspace/proj/.venv",
        "installer": "pip",
        "install_outcome": "success",
    }
    steps = [_step(1, "dependency_install")]
    if include_postconditions:
        setup.update(
            {
                "pip_check": {
                    "status": "clean",
                    "exit_code": 0,
                    "output_sha256": "a" * 64,
                },
                "imports": {
                    "status": "complete",
                    "targets": ["side_effect_pkg"],
                    "targets_sha256": producer_observations_sha256(["side_effect_pkg"]),
                    "target_count": 1,
                    "importable_count": 1,
                    "failed_count": 0,
                    "failures": [],
                    "output_sha256": "b" * 64,
                },
            }
        )
        steps.extend((_step(2, "pip_check"), _step(3, "import_probe")))
    return {
        "schema_version": 1,
        "ecosystem": "python",
        "operation": "setup_env",
        "recorded_project_steps": steps,
        "setup": setup,
    }


def _compile_observations():
    roots = ["side_effect_pkg"]
    source_basis = [{"path": _SOURCE, "sha256": _SOURCE_SHA}]
    pyc_basis = [{"path": _PYC, "sha256": _PYC_SHA, "source": _SOURCE}]
    return {
        "schema_version": 1,
        "ecosystem": "python",
        "operation": "compile",
        "recorded_project_steps": [
            _step(1, "compileall"),
            _step(2, "compile_metrics"),
        ],
        "compile": {
            "status": "valid",
            "roots": roots,
            "roots_sha256": producer_observations_sha256(roots),
            "source_count": 1,
            "compiled_source_count": 1,
            "missing_source_count": 0,
            "foreign_pyc_count": 0,
            "coverage": 1.0,
            "cache_tag": "cpython-312",
            "source_basis_sha256": producer_observations_sha256(source_basis),
            "pyc_basis_sha256": producer_observations_sha256(pyc_basis),
            "source_basis_entry_count": 1,
            "pyc_basis_entry_count": 1,
            "missing_sources": [],
            "foreign_pycs": [],
            "conflicts": [],
        },
    }


def _invalid_compile_observations():
    observations = _compile_observations()
    compile_observation = observations["compile"]
    compile_observation.pop("coverage")
    compile_observation.update(
        {
            "status": "invalid",
            "foreign_pyc_count": 1,
            "pyc_basis_entry_count": 1,
            "foreign_pycs": ["side_effect_pkg/__pycache__/legacy.cpython-311.pyc"],
            "conflicts": ["metrics_conflict"],
        }
    )
    return observations


def _build_observations():
    return {
        "schema_version": 1,
        "ecosystem": "python",
        "operation": "build",
        "recorded_project_steps": [_step(1, "wheel_build")],
        "recorded_mechanical_steps": [_step(2, "build_prerequisite")],
        "build": {
            "artifact_status": "produced",
            "artifacts": [
                {
                    "path": "dist/side_effect_pkg-1.0-py3-none-any.whl",
                    "sha256": _WHEEL_SHA,
                    "size_bytes": len(b"wheel-v1"),
                    "change": "new",
                }
            ],
        },
    }


def _receipt(
    operation,
    observations,
    *,
    suffix,
    run_id=_RUN_ID,
    target_sha=_TARGET_SHA,
    actual_cwd="/workspace/proj",
    # A python v1 manifest carries no build_domains, so receipts state no
    # domain_id and bind through the exact actual_cwd (absent-preserving).
    domain_id=None,
    exit_code=0,
    survey_fingerprint=_SURVEY_FINGERPRINT,
    config_fingerprint=_CONFIG_FINGERPRINT,
    document_map_fingerprint=_DOCUMENT_MAP_FINGERPRINT,
    fact_epoch=None,
):
    return build_receipt(
        receipt_id=f"inv-python-{operation}-{suffix}",
        run_id=run_id,
        tool="python",
        requested_action=operation,
        effective_action=operation,
        argv=f"python {operation}",
        working_directory=actual_cwd,
        actual_cwd=actual_cwd,
        domain_id=domain_id,
        target_sha=target_sha,
        survey_fingerprint=survey_fingerprint,
        config_fingerprint=config_fingerprint,
        document_map_fingerprint=document_map_fingerprint,
        fact_epoch=fact_epoch,
        exit_code=exit_code,
        before={},
        after={},
        producer_observations=observations,
    )


class ProducerReceiptOrch(ProjectExecutionTrap):
    def __init__(
        self,
        receipts,
        *,
        source_sha=_SOURCE_SHA,
        pyc_sha=_PYC_SHA,
        wheel_sha=_WHEEL_SHA,
        foreign_pyc=False,
        malformed=False,
        stream_fails=False,
        auto_contracts=True,
        auto_publish=True,
    ):
        super().__init__()
        self.receipts = receipts
        self.contracts = {}
        if auto_contracts:
            for receipt in self.receipts:
                self.bind_semantic_contract(receipt)
        if auto_publish:
            self.publish_receipts()
        self.run_pin = complete_run_pin(_RUN_ID, _TARGET_SHA)
        run_pin_publication = publish_evidence_revision(
            self,
            record_kind="run_pin",
            record_id=RUN_PIN_LOGICAL_ARTIFACT_ID,
            logical_artifact_id=RUN_PIN_LOGICAL_ARTIFACT_ID,
            raw=canonical_json(self.run_pin).encode("utf-8"),
            expected_previous_raw_sha256=EVIDENCE_PUBLICATION_GENESIS_SHA256,
        )
        if not run_pin_publication.published:
            raise AssertionError(f"test run-pin publication failed: {run_pin_publication.status}")
        self.source_sha = source_sha
        self.pyc_sha = pyc_sha
        self.wheel_sha = wheel_sha
        self.foreign_pyc = foreign_pyc
        self.malformed = malformed
        self.stream_fails = stream_fails

    def publish_receipts(self):
        published = set()
        for receipt in self.receipts:
            receipt_id = str(receipt.get("receipt_id") or "")
            if not receipt_id or receipt_id in published:
                continue
            try:
                raw = canonical_json(receipt).encode("utf-8")
            except (TypeError, ValueError):
                continue
            publication = publish_evidence_bytes(
                self,
                record_kind="invocation_receipt",
                record_id=receipt_id,
                raw=raw,
                contract_id=receipt.get("contract_id"),
                contract_hash=receipt.get("contract_hash"),
            )
            if publication.published:
                published.add(receipt_id)

    def bind_semantic_contract(self, receipt, *, compliance=None):
        operation = str(receipt.get("effective_action") or "")
        public_action = {
            "setup_env": "deps",
            "build": "package",
            "compile": "compile",
        }[operation]
        cwd = str(receipt.get("actual_cwd") or receipt.get("working_directory") or "")
        params = {
            "action": public_action,
            "args": None,
            "working_directory": cwd,
            "timeout": None,
            "maven_version_requirement": None,
            "features": None,
            "definitions": None,
        }
        domain_id = f"test:{cwd}"
        contract = build_contract(
            run_id=str(receipt.get("run_id") or ""),
            envelope_id=f"envelope-{receipt['receipt_id']}",
            tool="build",
            params=params,
            effective_tool="python",
            effective_action=operation,
            expected_cwd=cwd,
            expected_argv=None,
            execution_binding=PYTHON_FACADE_EXECUTION_BINDING,
            intent_source="controller",
            intent_id=f"intent-{receipt['receipt_id']}",
            intent_domain_id=domain_id,
            intent_exact_params=params,
            action_fingerprint=action_fingerprint(
                domain_id=domain_id,
                tool="build",
                params=params,
            ),
            target_sha=receipt.get("target_sha"),
            survey_fingerprint=receipt.get("survey_fingerprint"),
            config_fingerprint=receipt.get("config_fingerprint"),
            document_map_fingerprint=receipt.get("document_map_fingerprint"),
            domain_id=receipt.get("domain_id"),
            fact_epoch=receipt.get("fact_epoch"),
        )
        receipt["contract_id"] = contract["contract_id"]
        receipt["contract_hash"] = contract["contract_hash"]
        receipt["execution_binding"] = contract["execution_binding"]
        if compliance is None:
            receipt.pop("compliance", None)
        else:
            receipt["compliance"] = compliance
        self.contracts[contract["contract_id"]] = contract
        body = canonical_json(contract).encode("utf-8")
        publication = publish_evidence_bytes(
            self,
            record_kind="invocation_contract",
            record_id=contract["contract_id"],
            raw=body,
            contract_id=contract["contract_id"],
            contract_hash=contract["contract_hash"],
        )
        if not publication.published:
            raise AssertionError(f"test contract publication failed: {publication.status}")
        return contract

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        if (
            "/workspace/proj/.venv/bin/" in command
            or "import side_effect_pkg" in command
            or "-m compileall" in command
        ):
            self.project_executions.append(command)
            raise AssertionError(f"physical judge executed project code: {command}")

        def result(ok=True, output="", *, exit_code=None):
            return {
                "success": ok,
                "exit_code": (0 if ok else 1) if exit_code is None else exit_code,
                "output": output,
            }

        stripped = command.strip()
        if (
            stripped.startswith("file=")
            and "SAG_NAMED_JSON_RECORD_V1" in stripped
            and "run-pin.json" in stripped
        ):
            return result(
                True,
                frame_named_json_record_stream([("run-pin.json", canonical_json(self.run_pin))]),
            )
        if stripped == "test -d /workspace/.setup_agent/invocation_receipts && echo EXISTS":
            return result(True, "EXISTS\n")
        if (
            "SAG_NAMED_JSON_RECORD_V1" in stripped
            and "/workspace/.setup_agent/invocation_receipts" in stripped
        ):
            if self.stream_fails:
                return result(False, "transport failed", exit_code=70)
            records = []
            bodies_by_name = {}
            for receipt in self.receipts:
                name = f"{receipt.get('receipt_id', 'receipt')}.json"
                body = canonical_json(receipt)
                if bodies_by_name.get(name) == body:
                    # An absent-or-identical replay leaves one physical file.
                    continue
                bodies_by_name[name] = body
                records.append((name, body))
            if self.malformed:
                records.insert(0, ("malformed.json", '{"broken":'))
            return result(True, frame_named_json_record_stream(records))
        if "SAG_NAMED_JSON_RECORD_V1" in stripped and CONTRACT_DIR in stripped:
            return result(
                True,
                frame_named_json_record_stream(
                    (f"{contract_id}.json", contract)
                    for contract_id, contract in sorted(self.contracts.items())
                ),
            )
        if stripped == "cat /workspace/.setup_agent/run-pin.json":
            return result(True, canonical_json(self.run_pin))
        if stripped.startswith(f"cat {CONTRACT_DIR}/"):
            contract_id = shlex.split(stripped)[1].rsplit("/", 1)[-1].removesuffix(".json")
            contract = self.contracts.get(contract_id)
            return result(contract is not None, json.dumps(contract) if contract else "")
        if "find -P" in stripped and "-type l" in stripped:
            return result(True, "")
        if "find -P" in stripped and "sha256sum" in stripped:
            foreign = f"{_FOREIGN_PYC_SHA}  {_FOREIGN_PYC}\n" if self.foreign_pyc else ""
            return result(
                True,
                f"{self.source_sha}  {_SOURCE}\n{self.pyc_sha}  {_PYC}\n{foreign}",
            )
        if stripped.startswith("sha256sum -- ") and _WHEEL in stripped:
            return result(True, f"{self.wheel_sha}  {_WHEEL}\n")
        if stripped.startswith("wc -c < ") and _WHEEL in stripped:
            return result(True, f"{len(b'wheel-v1')}\n")
        return super().execute_command(command, **kwargs)


def _producer_status(orch):
    return PhysicalValidator(
        docker_orchestrator=orch,
        project_path="/workspace",
        receipt_run_id=_RUN_ID,
    )._verify_python_build("/workspace/proj")


def test_current_bound_producer_receipts_make_python_runtime_rungs_readable_only():
    receipts = [
        _receipt("setup_env", _setup_observations(), suffix="1"),
        _receipt("compile", _compile_observations(), suffix="2"),
        _receipt("build", _build_observations(), suffix="3"),
    ]
    orch = ProducerReceiptOrch(receipts)

    status = _producer_status(orch)

    assert status["pip_check_clean"] is True
    assert status["imports_ok"] is True
    assert status["compileall_metric_status"] == "valid"
    assert status["compileall_coverage"] == 1.0
    assert status["wheel_artifact_status"] == "produced"
    assert status["wheel_artifacts_verified"] is True
    assert status["complete"] is True
    assert orch.project_executions == []


def test_single_domain_receipt_without_domain_id_uses_exact_actual_cwd_binding():
    # Premise update (live authority): a python v1 manifest can never carry
    # build_domains, so the single-domain survey writes no domain_id anywhere;
    # this pins that the exact actual_cwd binding alone grades completely.
    setup = _receipt("setup_env", _setup_observations(), suffix="1")
    compile_receipt = _receipt("compile", _compile_observations(), suffix="2")
    assert "domain_id" not in setup
    assert "domain_id" not in compile_receipt
    orch = ProducerReceiptOrch([setup, compile_receipt])
    assert "build_domains" not in orch.manifest

    status = _producer_status(orch)

    assert status["pip_check_clean"] is True
    assert status["imports_ok"] is True
    assert status["compileall_coverage"] == 1.0
    assert status["complete"] is True


def test_public_python_build_judgment_projects_receipt_ids_and_verified_basis():
    receipts = [
        _receipt("setup_env", _setup_observations(), suffix="1"),
        _receipt("compile", _compile_observations(), suffix="2"),
    ]
    orch = ProducerReceiptOrch(receipts)
    validator = PhysicalValidator(
        docker_orchestrator=orch,
        project_path="/workspace",
        receipt_run_id=_RUN_ID,
    )

    status = validator.validate_build_status("proj")

    details = status["evidence"]["fingerprint_details"]
    assert status["success"] is True
    assert status["build_complete"] is True
    assert status["evidence_status"] == "success"
    assert details["producer_receipt_ids"] == {
        "setup_env": "inv-python-setup_env-1",
        "compile": "inv-python-compile-2",
    }
    assert details["compileall_source_basis_sha256"] is not None
    assert details["compileall_pyc_basis_sha256"] is not None
    assert orch.project_executions == []


def test_strongest_complete_current_receipt_outranks_an_incomplete_peer():
    incomplete = _receipt(
        "setup_env", _setup_observations(include_postconditions=False), suffix="1"
    )
    complete = _receipt("setup_env", _setup_observations(), suffix="2")
    orch = ProducerReceiptOrch(
        [incomplete, complete, _receipt("compile", _compile_observations(), suffix="3")]
    )

    status = _producer_status(orch)

    assert status["pip_check_clean"] is True
    assert status["imports_ok"] is True
    assert status["complete"] is True


def test_failed_runner_cannot_launder_positive_producer_observations():
    failed = _receipt("setup_env", _setup_observations(), suffix="1", exit_code=1)
    orch = ProducerReceiptOrch([failed, _receipt("compile", _compile_observations(), suffix="2")])

    status = _producer_status(orch)

    assert status["pip_check_clean"] is None
    assert status["imports_ok"] is None
    assert status["complete"] is False
    assert any("runner" in warning for warning in status["warnings"])


def test_failed_runner_preserves_explicit_negative_setup_facts_only():
    observations = _setup_observations()
    observations["setup"]["pip_check"].update({"status": "broken", "exit_code": 1})
    observations["setup"]["imports"].update(
        {
            "importable_count": 0,
            "failed_count": 1,
            "failures": [{"target": "side_effect_pkg", "error_type": "ModuleNotFoundError"}],
        }
    )
    observations["recorded_project_steps"][1].update({"outcome": "failed", "exit_code": 1})
    orch = ProducerReceiptOrch([_receipt("setup_env", observations, suffix="1", exit_code=1)])

    status = _producer_status(orch)

    assert status["pip_check_clean"] is False
    assert status["imports_ok"] is False
    assert status["import_failures"] == ["side_effect_pkg"]
    assert status["success"] is False
    assert status["complete"] is False
    assert orch.project_executions == []


def test_complete_successful_retry_outranks_a_failed_setup_receipt_in_same_run():
    failed_observations = _setup_observations()
    failed_observations["setup"] = {
        "venv": "/workspace/proj/.venv",
        "installer": "pip",
        "install_outcome": "failed",
    }
    failed_observations["recorded_project_steps"] = [
        _step(1, "dependency_install", outcome="failed", exit_code=1)
    ]
    orch = ProducerReceiptOrch(
        [
            _receipt("setup_env", failed_observations, suffix="1", exit_code=1),
            _receipt("setup_env", _setup_observations(), suffix="2"),
            _receipt("compile", _compile_observations(), suffix="3"),
        ]
    )

    status = _producer_status(orch)

    assert status["install_outcome"] == "success"
    assert status["pip_check_clean"] is True
    assert status["imports_ok"] is True
    assert status["complete"] is True


def test_stale_foreign_or_wrong_target_receipts_have_no_python_judge_authority():
    stale = _receipt("setup_env", _setup_observations(), suffix="1", run_id="old-run")
    wrong_target = _receipt("setup_env", _setup_observations(), suffix="2", target_sha="b" * 40)
    foreign = _receipt(
        "setup_env",
        _setup_observations(),
        suffix="3",
        actual_cwd="/workspace/proj/foreign",
        domain_id="/workspace/proj/foreign",
    )
    orch = ProducerReceiptOrch([stale, wrong_target, foreign])

    status = _producer_status(orch)

    assert status["pip_check_clean"] is None
    assert status["imports_ok"] is None
    assert status["compileall_coverage"] is None
    assert status["complete"] is False


def test_semantic_python_contract_is_read_back_and_bound_without_inventing_argv_compliance():
    receipt = _receipt("setup_env", _setup_observations(), suffix="1")
    orch = ProducerReceiptOrch([receipt])

    status = _producer_status(orch)

    assert "expected_argv" not in next(iter(orch.contracts.values()))
    assert "compliance" not in receipt
    assert status["pip_check_clean"] is True
    assert status["imports_ok"] is True


def test_setup_receipt_cannot_substitute_a_different_import_target_set():
    observations = _setup_observations()
    imports = observations["setup"]["imports"]
    imports.update(
        {
            "targets": ["different_pkg"],
            "targets_sha256": producer_observations_sha256(["different_pkg"]),
        }
    )
    receipt = _receipt("setup_env", observations, suffix="1")
    orch = ProducerReceiptOrch([receipt])

    status = _producer_status(orch)

    assert status["pip_check_clean"] is None
    assert status["imports_ok"] is None
    assert any("import target" in warning for warning in status["warnings"])


@pytest.mark.parametrize(
    ("fault", "expected_conflict"),
    (
        ("missing_binding", "python_producer_receipt_invalid"),
        ("unreadable", "python_producer_receipts_unreadable"),
        ("forged_hash", "python_producer_receipts_unreadable"),
    ),
)
def test_missing_unreadable_or_forged_contract_cannot_authorize_python_facts(
    fault, expected_conflict
):
    receipt = _receipt("setup_env", _setup_observations(), suffix="1")
    orch = ProducerReceiptOrch([receipt], auto_contracts=False)
    if fault != "missing_binding":
        contract = orch.bind_semantic_contract(receipt)
        if fault == "unreadable":
            orch.contracts.pop(contract["contract_id"])
        else:
            receipt["contract_hash"] = "f" * 64

    status = _producer_status(orch)

    assert status["pip_check_clean"] is None
    assert status["imports_ok"] is None
    assert expected_conflict in status["metrics_conflicts"]


def test_semantic_contract_rejects_a_receipt_that_invents_argv_compliance():
    receipt = _receipt("setup_env", _setup_observations(), suffix="1")
    orch = ProducerReceiptOrch([receipt])
    receipt["compliance"] = "exact"

    status = _producer_status(orch)

    assert status["pip_check_clean"] is None
    assert "python_producer_receipts_unreadable" in status["metrics_conflicts"]


def test_argv_contract_requires_recomputed_exact_or_equivalent_compliance():
    receipt = _receipt("setup_env", _setup_observations(), suffix="1")
    orch = ProducerReceiptOrch([receipt])
    receipt["compliance"] = "deviated"

    status = _producer_status(orch)

    assert status["pip_check_clean"] is None
    assert "python_producer_receipts_unreadable" in status["metrics_conflicts"]


def test_python_semantic_contract_rejects_even_exact_argv_compliance():
    receipt = _receipt("setup_env", _setup_observations(), suffix="1")
    orch = ProducerReceiptOrch([receipt])
    receipt["compliance"] = "exact"

    status = _producer_status(orch)

    assert status["pip_check_clean"] is None
    assert "python_producer_receipts_unreadable" in status["metrics_conflicts"]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("effective_action", "compile"),
        ("expected_cwd", "/workspace/other"),
        ("target_sha", "b" * 40),
        ("config_fingerprint", "wrong-config"),
        ("domain_id", "/workspace/other"),
    ),
)
def test_contract_scope_fields_must_match_receipt_and_current_scope(field, value):
    receipt = _receipt("setup_env", _setup_observations(), suffix="1")
    orch = ProducerReceiptOrch([receipt])
    contract = next(iter(orch.contracts.values()))
    contract[field] = value
    contract["contract_hash"] = contract_hash(contract)
    receipt["contract_hash"] = contract["contract_hash"]

    status = _producer_status(orch)

    assert status["pip_check_clean"] is None
    assert "python_producer_receipts_unreadable" in status["metrics_conflicts"]


def test_frozen_build_call_must_map_to_the_exact_python_producer_operation():
    receipt = _receipt("setup_env", _setup_observations(), suffix="1")
    orch = ProducerReceiptOrch([receipt])
    contract = next(iter(orch.contracts.values()))
    contract["requested_call"]["params"]["action"] = "compile"
    contract["contract_hash"] = contract_hash(contract)
    receipt["contract_hash"] = contract["contract_hash"]

    status = _producer_status(orch)

    assert status["pip_check_clean"] is None
    assert "python_producer_receipts_unreadable" in status["metrics_conflicts"]


def test_current_manifest_config_mismatch_invalidates_receipt_and_contract_tuple():
    receipt = _receipt(
        "setup_env",
        _setup_observations(),
        suffix="1",
        config_fingerprint="old-config",
    )
    orch = ProducerReceiptOrch([receipt])
    # The survey stays a complete v1 record; only its config pin moves on.
    orch.manifest["survey"]["config_fingerprint"] = "current-config"
    orch.publish_manifest_update()

    status = _producer_status(orch)

    assert status["pip_check_clean"] is None
    assert "python_producer_receipt_invalid" in status["metrics_conflicts"]


def test_current_fact_epoch_must_match_contract_and_receipt():
    # Premise update (live authority): a python v1 manifest cannot carry
    # domain_facts, so the current fact epoch is ABSENT — a contract/receipt
    # that pins one anyway is a one-sided epoch pin and stays invalid, the
    # same must-match contract enforced from the absent side.
    receipt = _receipt(
        "setup_env",
        _setup_observations(),
        suffix="1",
        fact_epoch=1,
    )
    orch = ProducerReceiptOrch([receipt])

    status = _producer_status(orch)

    assert status["pip_check_clean"] is None
    assert status["imports_ok"] is None
    assert "python_producer_receipt_invalid" in status["metrics_conflicts"]


@pytest.mark.parametrize(
    ("key", "receipt_keyword"),
    (
        ("survey_fingerprint", "survey_fingerprint"),
        ("document_map_fingerprint", "document_map_fingerprint"),
    ),
)
def test_current_manifest_survey_tuple_must_match_contract_and_receipt(key, receipt_keyword):
    # Premise update (stamped surveys): the manifest side of the tuple is what
    # a v1 survey actually carries — its own recomputed survey_fingerprint and
    # the current document-map fingerprint. A contract/receipt pinning a
    # different value is a stale pin and stays invalid either way.
    receipt = _receipt(
        "setup_env",
        _setup_observations(),
        suffix="1",
        **{receipt_keyword: "frozen-fingerprint"},
    )
    orch = ProducerReceiptOrch([receipt])

    status = _producer_status(orch)

    assert status["pip_check_clean"] is None
    assert "python_producer_receipt_invalid" in status["metrics_conflicts"]


@pytest.mark.parametrize("key", ("survey_fingerprint", "document_map_fingerprint"))
def test_one_sided_contract_receipt_survey_pin_is_invalid(key):
    # Premise update (live authority): the pin is stripped from the RECEIPT
    # after publication while the frozen contract keeps it — the one-sided
    # tuple (and the tampered published bytes) must fail closed.
    receipt = _receipt(
        "setup_env",
        _setup_observations(),
        suffix="1",
        **{key: "same-fingerprint"},
    )
    orch = ProducerReceiptOrch([receipt])
    receipt.pop(key)

    status = _producer_status(orch)

    assert status["pip_check_clean"] is None
    assert "python_producer_receipts_unreadable" in status["metrics_conflicts"]


def test_current_future_schema_producer_receipt_is_invalid_not_silently_historical():
    receipt = _receipt("setup_env", _setup_observations(), suffix="1")
    receipt["schema_version"] = 999
    orch = ProducerReceiptOrch([receipt])

    status = _producer_status(orch)

    assert status["pip_check_clean"] is None
    assert "python_producer_receipts_unreadable" in status["metrics_conflicts"]


def test_new_invalid_receipt_cannot_be_ignored_in_favor_of_old_positive_evidence():
    old = _receipt("setup_env", _setup_observations(), suffix="1")
    new = _receipt("setup_env", _setup_observations(), suffix="2")
    new["schema_version"] = 999
    orch = ProducerReceiptOrch([old, new])

    status = _producer_status(orch)

    assert status["pip_check_clean"] is None
    assert "python_producer_receipts_unreadable" in status["metrics_conflicts"]


def test_identical_duplicate_receipt_id_is_idempotently_coalesced():
    receipt = _receipt("setup_env", _setup_observations(), suffix="1")
    orch = ProducerReceiptOrch([receipt, json.loads(json.dumps(receipt))])

    status = _producer_status(orch)

    assert status["pip_check_clean"] is True
    assert "python_producer_receipt_id_collision" not in status["metrics_conflicts"]


def test_same_receipt_id_with_different_body_is_an_integrity_conflict():
    first = _receipt("setup_env", _setup_observations(), suffix="1")
    second = _receipt("setup_env", _setup_observations(), suffix="1", exit_code=1)
    orch = ProducerReceiptOrch([first, second])

    status = _producer_status(orch)

    assert status["pip_check_clean"] is None
    assert "python_producer_receipts_unreadable" in status["metrics_conflicts"]


def test_same_producer_sequence_on_different_receipts_is_an_integrity_conflict():
    setup = _receipt("setup_env", _setup_observations(), suffix="42")
    compile_receipt = _receipt("compile", _compile_observations(), suffix="42")
    orch = ProducerReceiptOrch([setup, compile_receipt])

    status = _producer_status(orch)

    assert status["pip_check_clean"] is None
    assert "python_producer_sequence_conflict" in status["metrics_conflicts"]


@pytest.mark.parametrize("mutation", ("missing", "mismatched"))
def test_missing_or_mismatched_explicit_producer_sequence_fails_closed(mutation):
    receipt = _receipt("setup_env", _setup_observations(), suffix="1")
    if mutation == "missing":
        receipt.pop("producer_sequence")
    else:
        receipt["producer_sequence"] = 99
    orch = ProducerReceiptOrch([receipt])

    status = _producer_status(orch)

    assert status["pip_check_clean"] is None
    assert "python_producer_receipts_unreadable" in status["metrics_conflicts"]


def test_latest_producer_receipt_is_selected_before_authority_is_classified():
    old = _receipt("setup_env", _setup_observations(), suffix="1")
    negative_observations = _setup_observations()
    negative_observations["setup"]["pip_check"].update(status="broken", exit_code=1)
    negative_observations["recorded_project_steps"][1].update(outcome="failed", exit_code=1)
    new = _receipt("setup_env", negative_observations, suffix="2", exit_code=1)
    orch = ProducerReceiptOrch([old, new])

    status = _producer_status(orch)

    assert status["pip_check_clean"] is False
    assert status["complete"] is False


def test_tampered_current_producer_receipt_fails_closed_instead_of_using_its_claims():
    receipt = _receipt("setup_env", _setup_observations(), suffix="1")
    receipt["producer_observations"]["setup"]["pip_check"]["status"] = "broken"
    orch = ProducerReceiptOrch([receipt])

    status = _producer_status(orch)

    assert status["pip_check_clean"] is None
    assert status["imports_ok"] is None
    assert status["complete"] is False
    assert "python_producer_receipts_unreadable" in status["metrics_conflicts"]


@pytest.mark.parametrize("failure_mode", ("malformed", "stream_fails"))
def test_malformed_or_unreadable_receipt_stream_cannot_refine_python_unknown_upward(
    failure_mode,
):
    valid = _receipt("setup_env", _setup_observations(), suffix="1")
    orch = ProducerReceiptOrch(
        [valid],
        malformed=failure_mode == "malformed",
        stream_fails=failure_mode == "stream_fails",
    )

    status = _producer_status(orch)

    assert status["pip_check_clean"] is None
    assert status["imports_ok"] is None
    assert status["complete"] is False
    assert "python_producer_receipts_unreadable" in status["metrics_conflicts"]


def test_compile_basis_changed_after_receipt_is_unknown_not_historical_green():
    orch = ProducerReceiptOrch(
        [
            _receipt("setup_env", _setup_observations(), suffix="1"),
            _receipt("compile", _compile_observations(), suffix="2"),
        ],
        source_sha="d" * 64,
    )

    status = _producer_status(orch)

    assert status["pip_check_clean"] is True
    assert status["imports_ok"] is True
    assert status["compileall_coverage"] is None
    assert status["compileall_metric_status"] == "unavailable"
    assert status["complete"] is False
    assert "python_compile_basis_changed" in status["metrics_conflicts"]


def test_current_invalid_compile_basis_preserves_counts_samples_and_conflict():
    orch = ProducerReceiptOrch(
        [
            _receipt("setup_env", _setup_observations(), suffix="1"),
            _receipt(
                "compile",
                _invalid_compile_observations(),
                suffix="2",
                exit_code=1,
            ),
        ],
        foreign_pyc=True,
    )

    status = _producer_status(orch)

    assert status["compileall_metric_status"] == "invalid"
    assert status["compileall_source_count"] == 1
    assert status["compileall_compiled_source_count"] == 1
    assert status["compileall_foreign_pyc_count"] == 1
    assert status["compileall_cache_tag"] == "cpython-312"
    assert status["compileall_foreign_pycs"] == [
        "side_effect_pkg/__pycache__/legacy.cpython-311.pyc"
    ]
    assert "metrics_conflict" in status["metrics_conflicts"]
    assert status["complete"] is False


def test_wheel_delta_is_accepted_only_while_path_hash_and_size_still_match():
    orch = ProducerReceiptOrch(
        [_receipt("build", _build_observations(), suffix="1")],
        wheel_sha="e" * 64,
    )

    status = _producer_status(orch)

    assert status["wheel_artifact_status"] == "produced"
    assert status["wheel_artifacts_verified"] is False
    assert status["wheel_artifacts"] == []
    assert "python_wheel_artifact_changed" not in status["metrics_conflicts"]
    assert any("wheel artifact delta changed" in warning for warning in status["warnings"])
