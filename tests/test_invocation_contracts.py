# tests/test_invocation_contracts.py
"""Plan 6 Stage B Task B1 — a frozen contract precedes every runner dispatch.

Spec §C3: the model submits one canonical public tool call; the facade
materializes it into an effective action and an exact cwd/argv, freezes that
materialization as an immutable `InvocationContract`, and dispatches ONLY if
the contract is on disk. Nothing about the physical call may be decided after
the fact, and a run whose contract could not be persisted has no authority to
touch the project at all.

The binding decision (plan §Stage B): the engine emits the action envelope
BEFORE tool execution and the materialized argv exists only inside the build
facade, so the freeze happens INSIDE `build_tool` — after the backend
materializes, strictly before the runner runs — and records the envelope id
that the verifier walks (envelope -> contract -> receipt).

Scripted-orchestrator style (house pattern, shared with
tests/test_invocation_receipts.py and tests/test_build_tool.py).
"""

import json
import shlex
from types import SimpleNamespace

import pytest
from test_container_io import FakeContainer
from container_evidence_fakes import ContainerFS
from test_forced_attempt_native import forced_engine  # noqa: F401  (shared fixture)
from test_invocation_receipts import receipts_written as atomic_receipts_written

from sag.agent.control_events import canonical_json, canonical_sha256
from sag.agent.action_intents import action_fingerprint as compute_action_fingerprint
from sag.agent.evidence_assessments import ASSESSMENT_DIR
from sag.agent.evidence_publications import (
    BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
    EVIDENCE_PUBLICATION_GENESIS_SHA256,
    evidence_publication_authority_for,
    install_evidence_publication_authority,
    reset_evidence_publication_authority,
    unavailable_evidence_publication_authority,
)
from sag.agent.evidence_records import frame_named_json_record_stream
from sag.agent.invocation_contracts import (
    CONTRACT_AUTHORITY_MISSING,
    CONTRACT_DIR,
    CONTRACT_MAX_RAW_BYTES,
    CONTRACT_PERSIST_FAILED,
    CONTRACT_SCHEMA_VERSION,
    ActionContext,
    action_context,
    authorized_facade_envelope_id,
    build_contract as _build_contract_v2,
    clear_action_context,
    compliance_class,
    contract_hash,
    contract_identity,
    contract_identity_v2,
    contract_receipt_fields,
    current_action_context,
    current_contract,
    dispatch_contract,
    ensure_dispatch_contract,
    freeze_contract as _freeze_contract_v2,
    read_frozen_contract,
    write_contract,
)
from sag.agent.invocation_receipts import RECEIPT_DIR, RECEIPT_HEREDOC, record_invocation
from sag.tools.base import ToolResult
from sag.tools.build.build_tool import BuildTool
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH

DISPATCH = "<<runner dispatch>>"
SHA = "9f2b1c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b"
FREEZE_DOMAIN = "test:/workspace/proj/core"

# One surveyed manifest carrying the Stage A/§C2 projection the freeze pins.
MANIFEST = {
    "survey": {
        "survey_fingerprint": "survey-7",
        "config_fingerprint": "cfg-7",
        "project_path": "/workspace/proj",
    },
    "build_domains": [{"root": "/workspace/proj/core", "system": "maven"}],
    "domain_facts": [
        {
            "domain_id": "dom-abc123456789",
            "root": "/workspace/proj/core",
            "fact_epoch": 3,
            "open_conflicts": [
                {"kind": "version_incompatible", "edge_id": "edge-deadbeef0001"},
                {"kind": "partial_map", "path": "/workspace/proj/core/vendor"},
            ],
        }
    ],
}


class RecordingOrchestrator:
    """Records every container command in dispatch order.

    Answers the facade's marker probes, the freeze's target-sha probe and the
    atomic writes. `fail_contract_write` simulates the one failure the plan
    makes fatal: the contract cannot be persisted.
    """

    def __init__(self, markers=(), fail_contract_write=False, sha=SHA):
        self.markers = set(markers)
        self.fail_contract_write = fail_contract_write
        self.sha = sha
        self.commands = []
        self.manifest_raw = None

    def publish_manifest(self, payload=MANIFEST):
        if self.manifest_raw is not None:
            return
        self.manifest_raw = canonical_json(payload)
        authority = evidence_publication_authority_for(self)
        prior = authority.latest_head(BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID)
        authority.publish_revision(
            record_kind="build_requirements",
            record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            raw=self.manifest_raw.encode("utf-8"),
            expected_previous_raw_sha256=(
                prior.raw_sha256 if prior else EVIDENCE_PUBLICATION_GENESIS_SHA256
            ),
        )

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        if "SAG_NAMED_JSON_RECORD_V1" in command and REQUIREMENTS_PATH in command:
            records = (
                [("build_requirements.json", self.manifest_raw)]
                if self.manifest_raw is not None
                else []
            )
            return {
                "success": True,
                "exit_code": 0,
                "output": frame_named_json_record_stream(records),
            }
        if "__SAG_FILE_MISSING__" in command:
            return {"success": False, "exit_code": 44, "output": "__SAG_FILE_MISSING__"}
        tokens = shlex.split(command)
        if tokens[:1] == ["cat"]:
            return {"success": False, "exit_code": 1, "output": ""}
        if tokens[:3] == ["test", "!", "-e"]:
            return {"success": True, "exit_code": 0, "output": ""}
        if "test -f" in command:
            probed = tokens[tokens.index("-f") + 1] if "-f" in tokens else ""
            exists = any(marker in probed for marker in self.markers)
            return {"success": True, "output": "exists" if exists else "missing"}
        if "rev-parse HEAD" in command:
            return {"success": True, "output": f"{self.sha}\n"}
        if self.fail_contract_write and CONTRACT_DIR in command:
            return {"success": False, "output": "read-only file system"}
        return {"success": True, "output": ""}


class DispatchingBackendTool:
    """MavenTool/GradleTool/PythonTool stand-in that logs its own dispatch."""

    def __init__(self, orchestrator, result=None):
        self.orchestrator = orchestrator
        self.calls = []
        self.result = result or ToolResult.completed_success(output="BUILD SUCCESS")

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        self.orchestrator.commands.append(DISPATCH)
        return self.result


def _atomic_written(commands, directory):
    """Replay bounded transport commands and return finalized JSON bodies."""
    filesystem = FakeContainer()
    for command in commands:
        filesystem.execute_command(command)
    return [
        json.loads(body)
        for path, body in sorted(filesystem.files.items())
        if path.startswith(f"{directory}/") and path.endswith(".json")
    ]


def contracts_written(commands):
    return _atomic_written(commands, CONTRACT_DIR)


def assessments_written(commands):
    return _atomic_written(commands, ASSESSMENT_DIR)


def receipts_written(commands):
    return atomic_receipts_written(commands)


def _build_tool(markers, orchestrator=None, **tools):
    orchestrator = orchestrator or RecordingOrchestrator(markers)
    orchestrator.publish_manifest()
    backends = {
        name: tools.get(name) or DispatchingBackendTool(orchestrator)
        for name in ("maven_tool", "gradle_tool", "python_tool")
    }
    return BuildTool(orchestrator, **backends), orchestrator, backends


@pytest.fixture(autouse=True)
def _no_leaked_action_context():
    """Every test starts with an empty request scope and leaves none behind."""
    clear_action_context()
    yield
    clear_action_context()


FREEZE_ARGS = {
    "envelope_id": "envelope-000012",
    "tool": "build",
    "params": {"action": "test", "working_directory": "/workspace/proj/core"},
    "effective_action": "verify",
    "expected_cwd": "/workspace/proj/core",
    "expected_argv": "--fail-at-end verify",
    "intent_source": "model",
    "requirements": MANIFEST,
}


def _v2_kwargs(values):
    enriched = dict(values)
    params = dict(enriched.get("params") or {})
    tool = str(enriched.get("tool") or "")
    expected_argv = enriched.get("expected_argv")
    effective_tool = enriched.setdefault(
        "effective_tool",
        tool if tool in {"maven", "gradle", "bash", "python"} else (
            "python" if expected_argv is None else "maven"
        ),
    )
    enriched.setdefault(
        "execution_binding",
        "python_facade_v1" if effective_tool == "python" else "argv_v1",
    )
    enriched.setdefault("run_id", "run-pytest")
    domain = enriched.setdefault(
        "intent_domain_id", f"test:{enriched.get('expected_cwd') or '/workspace'}"
    )
    enriched.setdefault("intent_id", f"intent-{enriched.get('envelope_id', 'test')}")
    enriched.setdefault("intent_exact_params", params)
    if "action_fingerprint" not in enriched:
        try:
            enriched["action_fingerprint"] = compute_action_fingerprint(
                domain_id=domain, tool=tool, params=params
            )
        except (TypeError, ValueError):
            # Let the production builder own the canonical-shape refusal.
            enriched["action_fingerprint"] = "act-invalid-test-fixture"
    return enriched


def build_contract(**kwargs):
    return _build_contract_v2(**_v2_kwargs(kwargs))


def freeze_contract(execute, **kwargs):
    return _freeze_contract_v2(execute, **_v2_kwargs(kwargs))


def build_action_context(
    envelope_id,
    *,
    action,
    working_directory="/workspace",
    intent_source="model",
    args=None,
    timeout=None,
    maven_version_requirement=None,
    features=None,
    definitions=None,
):
    """Exact engine authority for one BuildTool call in this test module."""

    params = {"action": action, "working_directory": working_directory}
    for key, value in (
        ("args", args),
        ("timeout", timeout),
        ("maven_version_requirement", maven_version_requirement),
        ("features", list(features) if features is not None else None),
        ("definitions", dict(definitions) if definitions is not None else None),
    ):
        if value is not None:
            params[key] = value
    domain = f"test:{working_directory}"
    return action_context(
        envelope_id=envelope_id,
        intent_source=intent_source,
        intent_id=f"intent-{envelope_id or 'sinkless'}",
        intent_domain_id=domain,
        intent_exact_params=params,
        action_fingerprint=compute_action_fingerprint(
            domain_id=domain,
            tool="build",
            params=params,
        ),
    )


# ---------------------------------------------------------------------------
# freeze_contract: shape, identity, persistence
# ---------------------------------------------------------------------------


def test_freeze_contract_records_the_v2_fields_and_persists_them_atomically():
    orchestrator = RecordingOrchestrator()

    contract = freeze_contract(orchestrator.execute_command, **FREEZE_ARGS)

    assert contract["schema_version"] == CONTRACT_SCHEMA_VERSION
    assert contract["envelope_id"] == "envelope-000012"
    assert contract["intent_source"] == "model"
    assert contract["requested_call"] == {
        "tool": "build",
        "params": {"action": "test", "working_directory": "/workspace/proj/core"},
    }
    assert contract["effective_action"] == "verify"
    assert contract["expected_cwd"] == "/workspace/proj/core"
    assert contract["expected_argv"] == "--fail-at-end verify"
    assert contract["target_sha"] == SHA
    assert contract["survey_fingerprint"] == "survey-7"
    assert contract["config_fingerprint"] == "cfg-7"
    assert contract["domain_id"] == "/workspace/proj/core"
    assert contract["fact_epoch"] == 3
    # Only an edge that BLOCKS is a blocking conflict; an incomplete document
    # map is an unknown, not a block.
    assert contract["blocking_conflict_ids"] == ["edge-deadbeef0001"]

    (persisted,) = contracts_written(orchestrator.commands)
    assert persisted == contract
    final = f"{CONTRACT_DIR}/{contract['contract_id']}.json"
    write = next(c for c in orchestrator.commands if "fcntl.flock" in c and final in c)
    assert "os.replace(candidate,target)" in write
    assert max(map(len, orchestrator.commands)) <= 60200


def test_contract_id_and_hash_recompute_from_the_persisted_payload():
    """Identity is derived, never assigned: a reader recomputes both."""
    orchestrator = RecordingOrchestrator()

    contract = freeze_contract(orchestrator.execute_command, **FREEZE_ARGS)

    assert contract["contract_id"] == contract_identity_v2(contract)
    assert contract["contract_id"].startswith("ic-")
    body = {key: value for key, value in contract.items() if key != "contract_hash"}
    assert contract["contract_hash"] == canonical_sha256(body)


def test_contract_requested_call_preserves_null_and_rejects_non_exact_keys():
    args = {
        key: value
        for key, value in FREEZE_ARGS.items()
        if key not in {"requirements", "params"}
    }
    contract = build_contract(
        **args,
        params={"action": "test", "optional": None},
    )
    assert contract["requested_call"]["params"] == {
        "action": "test",
        "optional": None,
    }

    with pytest.raises((TypeError, ValueError), match="keys|whitespace-canonical"):
        build_contract(**args, params={"a": 1, " a": 2})
    with pytest.raises((TypeError, ValueError), match="keys"):
        build_contract(**args, params={1: "not-json"})


def test_contract_io_and_active_scope_reject_tampered_authority():
    valid = build_contract(
        envelope_id="envelope-contract-validation",
        tool="maven",
        params={"command": "validate"},
        effective_action="validate",
        expected_cwd="/workspace/proj",
        expected_argv="validate",
        intent_source="controller",
    )
    tampered = dict(valid)
    tampered["expected_cwd"] = "/workspace/other"

    orchestrator = RecordingOrchestrator()
    assert write_contract(orchestrator.execute_command, tampered) is False
    assert orchestrator.commands == []
    with dispatch_contract(tampered):
        assert current_contract() is None
        existing, created = ensure_dispatch_contract(
            orchestrator.execute_command,
            tool="maven",
            effective_action="validate",
            expected_cwd="/workspace/proj",
            expected_argv="mvn validate",
        )
        assert existing is None
        assert created is False

    forged_identity = dict(valid)
    forged_identity["contract_id"] = "ic-000000000000"
    forged_identity["contract_hash"] = contract_hash(forged_identity)
    with dispatch_contract(forged_identity):
        assert current_contract() is None

    forged_schema = dict(valid)
    forged_schema["schema_version"] = 1.0
    forged_schema["contract_hash"] = contract_hash(forged_schema)
    with dispatch_contract(forged_schema):
        assert current_contract() is None


def test_read_contract_rejects_duplicate_json_oversize_and_hash_tamper():
    valid = build_contract(
        envelope_id="envelope-contract-read",
        tool="maven",
        params={"command": "validate"},
        effective_action="validate",
        expected_cwd="/workspace/proj",
        expected_argv="validate",
        intent_source="controller",
    )

    container = ContainerFS()
    assert write_contract(container, valid) is True
    path = f"{CONTRACT_DIR}/{valid['contract_id']}.json"
    assert read_frozen_contract(container, valid["contract_id"]) == valid

    tampered = dict(valid)
    tampered["expected_cwd"] = "/workspace/other"
    container.files[path] = canonical_json(tampered)
    assert read_frozen_contract(container, valid["contract_id"]) is None

    duplicate = canonical_json(valid)[:-1] + ',"intent_source":"controller"}'
    container.files[path] = duplicate
    assert read_frozen_contract(container, valid["contract_id"]) is None

    oversized = canonical_json(valid) + " " * (
        CONTRACT_MAX_RAW_BYTES - len(canonical_json(valid).encode("utf-8")) + 1
    )
    container.files[path] = oversized
    assert read_frozen_contract(container, valid["contract_id"]) is None


def test_live_contract_reader_ignores_only_strict_foreign_and_historical_siblings():
    current = build_contract(
        run_id="run-pytest",
        envelope_id="envelope-current-contract-ledger",
        tool="build",
        params={"action": "test", "working_directory": "/workspace/proj"},
        effective_tool="maven",
        effective_action="test",
        expected_cwd="/workspace/proj",
        expected_argv="test",
        execution_binding="argv_v1",
        intent_source="controller",
    )
    foreign = build_contract(
        run_id="run-previous",
        envelope_id="envelope-foreign-contract-ledger",
        tool="build",
        params={"action": "test", "working_directory": "/workspace/proj"},
        effective_tool="maven",
        effective_action="test",
        expected_cwd="/workspace/proj",
        expected_argv="test",
        execution_binding="argv_v1",
        intent_source="controller",
    )
    historical = {
        "schema_version": 1,
        "contract_id": contract_identity("envelope-historical-ledger", "test"),
        "envelope_id": "envelope-historical-ledger",
        "intent_source": "controller",
        "requested_call": {
            "tool": "build",
            "params": {"action": "test", "working_directory": "/workspace/proj"},
        },
        "effective_action": "test",
        "expected_cwd": "/workspace/proj",
        "expected_argv": "test",
    }
    historical["contract_hash"] = contract_hash(historical)
    container = ContainerFS()
    assert write_contract(container, current) is True
    container.files[f"{CONTRACT_DIR}/{foreign['contract_id']}.json"] = canonical_json(foreign)
    container.files[f"{CONTRACT_DIR}/{historical['contract_id']}.json"] = canonical_json(
        historical
    )

    assert read_frozen_contract(container, current["contract_id"]) == current
    assert read_frozen_contract(container, foreign["contract_id"]) is None

    unknown = dict(foreign)
    unknown["schema_version"] = 3
    container.files[f"{CONTRACT_DIR}/{foreign['contract_id']}.json"] = canonical_json(unknown)
    assert read_frozen_contract(container, current["contract_id"]) is None


def test_contract_writer_requires_host_publication_and_replay_repairs_it(
    bind_host_evidence_publication_authority,
):
    contract = build_contract(
        envelope_id="envelope-contract-host-publication",
        tool="build",
        params={"action": "test", "working_directory": "/workspace/proj"},
        effective_tool="maven",
        effective_action="test",
        expected_cwd="/workspace/proj",
        expected_argv="test",
        execution_binding="argv_v1",
        intent_source="controller",
    )
    container = ContainerFS()
    token = install_evidence_publication_authority(
        unavailable_evidence_publication_authority("publication probe")
    )
    try:
        assert write_contract(container, contract) is False
    finally:
        reset_evidence_publication_authority(token)

    body = canonical_json(contract)
    path = f"{CONTRACT_DIR}/{contract['contract_id']}.json"
    assert container.files[path] == body
    assert write_contract(container, contract) is True
    assert bind_host_evidence_publication_authority.verify_bytes(
        record_kind="invocation_contract",
        record_id=contract["contract_id"],
        raw=body.encode("utf-8"),
        contract_id=contract["contract_id"],
        contract_hash=contract["contract_hash"],
    ).authorized


def test_runtime_bound_contract_records_effective_jdk_and_has_distinct_retry_identity():
    orchestrator = RecordingOrchestrator()
    first_runtime = {
        "major": "11",
        "requirement_major": "11",
        "requirement_authority": "static_survey",
        "provenance": {"source": "maven-compiler"},
    }
    retry_runtime = {
        "major": "17",
        "requirement_major": "17",
        "requirement_authority": "runner_observed",
        "provenance": {
            "target_sha": SHA,
            "domain_id": "dom-abc123456789",
            "domain_root": "/workspace/proj/core",
            "source_ref": "inv-maven-1-0001",
            "observed_sequence": 1,
        },
    }

    first = freeze_contract(
        orchestrator.execute_command,
        **FREEZE_ARGS,
        effective_jdk=first_runtime,
    )
    retry = freeze_contract(
        orchestrator.execute_command,
        **FREEZE_ARGS,
        effective_jdk=retry_runtime,
        predecessor_contract_id=first["contract_id"],
    )

    assert first["effective_jdk"] == first_runtime
    assert retry["effective_jdk"] == retry_runtime
    assert retry["predecessor_contract_id"] == first["contract_id"]
    assert first["contract_id"] != retry["contract_id"]


@pytest.mark.parametrize(
    "forged_runtime",
    [
        "17",
        {},
        {"major": "17", "unowned": "forged"},
        {"major": "17", "provenance": {"source": "runner", "blank": ""}},
    ],
)
def test_effective_jdk_must_be_the_exact_canonical_engine_mapping_at_every_boundary(
    forged_runtime,
):
    valid = build_contract(
        envelope_id="envelope-runtime-canonical",
        tool="maven",
        params={"command": "test"},
        effective_action="test",
        expected_cwd="/workspace/proj",
        expected_argv="test",
        intent_source="controller",
        effective_jdk={"major": "17", "provenance": {"source": "runner"}},
    )
    forged = dict(valid)
    forged["effective_jdk"] = forged_runtime
    runtime_for_identity = forged_runtime if isinstance(forged_runtime, dict) else None
    forged["contract_id"] = contract_identity(
        forged["envelope_id"], forged["expected_argv"], runtime_for_identity
    )
    forged["contract_hash"] = contract_hash(forged)
    orchestrator = RecordingOrchestrator()

    assert write_contract(orchestrator.execute_command, forged) is False
    assert orchestrator.commands == []
    with dispatch_contract(forged):
        assert current_contract() is None
    body = canonical_json(forged)
    reader = lambda command: {"success": True, "exit_code": 0, "output": body}
    assert read_frozen_contract(reader, forged["contract_id"]) is None


def test_predecessor_contract_id_is_strict_and_cannot_name_the_contract_itself():
    common = {
        "envelope_id": "envelope-predecessor-strict",
        "tool": "maven",
        "params": {"command": "test"},
        "effective_action": "test",
        "expected_cwd": "/workspace/proj",
        "expected_argv": "test",
        "intent_source": "controller",
    }
    with pytest.raises(ValueError, match="predecessor"):
        build_contract(**common, predecessor_contract_id="ic-forged")

    identifier = build_contract(**common)["contract_id"]
    with pytest.raises(ValueError, match="predecessor"):
        build_contract(**common, predecessor_contract_id=identifier)

    valid = build_contract(**common, predecessor_contract_id="ic-000000000001")
    assert valid["predecessor_contract_id"] == "ic-000000000001"


def test_receipt_binding_copies_effective_jdk_from_the_active_contract():
    effective_jdk = {
        "major": "17",
        "requirement_major": "17",
        "requirement_authority": "persisted_dynamic",
        "provenance": {"source_ref": "output_abc"},
    }
    contract = freeze_contract(
        RecordingOrchestrator().execute_command,
        **FREEZE_ARGS,
        effective_jdk=effective_jdk,
    )

    with dispatch_contract(contract):
        fields = contract_receipt_fields("mvn --fail-at-end verify")

    assert fields["effective_jdk"] == effective_jdk


def test_frozen_contract_is_persisted_as_the_bytes_it_hashed():
    orchestrator = RecordingOrchestrator()

    contract = freeze_contract(orchestrator.execute_command, **FREEZE_ARGS)

    (persisted,) = contracts_written(orchestrator.commands)
    assert canonical_json(persisted) == canonical_json(contract)
    assert not any(canonical_json(contract) in command for command in orchestrator.commands)


def test_oversized_exact_call_is_refused_before_contract_persistence():
    orchestrator = RecordingOrchestrator()
    args = dict(FREEZE_ARGS, params={"action": "test", "args": "x" * 200_000})

    contract = freeze_contract(orchestrator.execute_command, **args)

    assert contract is None
    assert contracts_written(orchestrator.commands) == []


def test_freeze_contract_omits_every_pin_it_does_not_know():
    """Absent facts are absent keys — never null, never a guessed default."""
    orchestrator = RecordingOrchestrator(sha="build log line, not a sha")

    contract = freeze_contract(
        orchestrator.execute_command,
        envelope_id="envelope-000001",
        tool="build",
        params={"action": "compile"},
        effective_action="compile",
        expected_cwd="/workspace/proj",
        expected_argv=None,
        intent_source="model",
        requirements={},
    )

    assert set(contract) == {
        "schema_version",
        "run_id",
        "contract_id",
        "contract_hash",
        "envelope_id",
        "intent_id",
        "intent_domain_id",
        "action_fingerprint",
        "intent_source",
        "requested_call",
        "effective_tool",
        "effective_action",
        "expected_cwd",
        "execution_binding",
        # Not pins: the Stage C typed expectations are derived from the public
        # verb the caller submitted, so `compile` always states them.
        "expected_observations",
    }


def test_freeze_contract_records_a_predecessor_and_a_document_map_pin():
    orchestrator = RecordingOrchestrator()

    contract = freeze_contract(
        orchestrator.execute_command,
        document_map_fingerprint="map-9",
        predecessor_contract_id="ic-000000000001",
        **FREEZE_ARGS,
    )

    assert contract["document_map_fingerprint"] == "map-9"
    assert contract["predecessor_contract_id"] == "ic-000000000001"


def test_freeze_contract_binds_engine_owned_action_intent_lineage():
    orchestrator = RecordingOrchestrator()
    fingerprint = compute_action_fingerprint(
        domain_id=FREEZE_DOMAIN,
        tool="build",
        params=FREEZE_ARGS["params"],
    )

    contract = freeze_contract(
        orchestrator.execute_command,
        intent_id="intent-123456789abc",
        intent_domain_id=FREEZE_DOMAIN,
        action_fingerprint=fingerprint,
        trigger_assessment_id="asm-gate-compile-failed-12345678",
        repair_context_id="rcx-123456789abc",
        repair_context_sha256="b" * 64,
        **FREEZE_ARGS,
    )

    assert contract["intent_id"] == "intent-123456789abc"
    assert contract["action_fingerprint"] == fingerprint
    assert contract["trigger_assessment_id"] == "asm-gate-compile-failed-12345678"
    assert contract["repair_context_id"] == "rcx-123456789abc"
    assert contract["repair_context_sha256"] == "b" * 64
    assert contracts_written(orchestrator.commands) == [contract]


@pytest.mark.parametrize(
    "overrides",
    [
        {"repair_context_id": "rcx-123456789abc"},
        {
            "trigger_assessment_id": "asm-gate-compile-failed-12345678",
            "repair_context_id": "rcx-123456789abc",
            "repair_context_sha256": "not-a-digest",
            "intent_id": "intent-123456789abc",
            "action_fingerprint": "act-" + "a" * 64,
        },
        {
            "trigger_assessment_id": "asm-gate-compile-failed-12345678",
            "repair_context_id": "rcx-123456789abc",
            "repair_context_sha256": "b" * 64,
            "intent_source": "controller",
            "intent_id": "intent-123456789abc",
            "action_fingerprint": "act-" + "a" * 64,
        },
    ],
)
def test_contract_refuses_partial_or_non_model_repair_lineage(overrides):
    args = {key: value for key, value in FREEZE_ARGS.items() if key != "requirements"}
    domain = FREEZE_DOMAIN
    fingerprint = compute_action_fingerprint(
        domain_id=domain,
        tool=args["tool"],
        params=args["params"],
    )
    overrides = {
        **overrides,
        "intent_domain_id": domain,
        "intent_exact_params": args["params"],
        "action_fingerprint": fingerprint,
    }
    with pytest.raises(ValueError, match="repair"):
        build_contract(**{**args, **overrides})


def test_action_context_binds_the_repair_digest_as_one_strict_tuple():
    with pytest.raises(ValueError, match="repair"):
        ActionContext(repair_context_id="rcx-123456789abc")
    with pytest.raises(ValueError, match="predecessor"):
        ActionContext(predecessor_contract_id="ic-forged")
    with action_context(
        envelope_id="envelope-repair-1",
        intent_source="model",
        intent_id="intent-123456789abc",
        intent_domain_id="domain:test",
        intent_exact_params={"action": "test"},
        action_fingerprint="act-" + "a" * 64,
        trigger_assessment_id="asm-gate-compile-failed-12345678",
        repair_context_id="rcx-123456789abc",
        repair_context_sha256="B" * 64,
    ):
        assert current_action_context().repair_context_sha256 == "b" * 64


def test_unrecorded_envelope_is_only_for_sinkless_nonrepair_engine_intent():
    assert authorized_facade_envelope_id(ActionContext()) is None
    with pytest.raises(ValueError, match="complete tuple"):
        ActionContext(intent_id="intent-partial")

    sinkless = ActionContext(
        intent_source="model",
        intent_id="intent-sinkless",
        intent_domain_id="domain:sinkless",
        intent_exact_params={"action": "test"},
        action_fingerprint="act-" + "a" * 64,
    )
    first = authorized_facade_envelope_id(sinkless)
    second = authorized_facade_envelope_id(sinkless)
    assert first and first.startswith("envelope-unrecorded-")
    assert second and second.startswith("envelope-unrecorded-")
    assert first != second

    assert authorized_facade_envelope_id(
        ActionContext(
            intent_source="model",
            intent_id="intent-recording",
            intent_domain_id="domain:recording",
            intent_exact_params={"action": "test"},
            action_fingerprint="act-" + "b" * 64,
            control_recording_active=True,
        )
    ) is None
    assert authorized_facade_envelope_id(
        ActionContext(
            intent_source="model",
            intent_id="intent-repair",
            intent_domain_id="domain:repair",
            intent_exact_params={"action": "test"},
            action_fingerprint="act-" + "c" * 64,
            trigger_assessment_id="asm-repair",
            repair_context_id="rcx-123456789abc",
            repair_context_sha256="d" * 64,
        )
    ) is None
    assert authorized_facade_envelope_id(
        ActionContext(
            envelope_id="envelope-recorded",
            intent_source="model",
            intent_id="intent-recorded",
            intent_domain_id="domain:recorded",
            intent_exact_params={"action": "test"},
            action_fingerprint="act-" + "e" * 64,
            control_recording_active=True,
        )
    ) == "envelope-recorded"


def test_internal_contract_lookup_never_mints_a_second_producer():
    orchestrator = RecordingOrchestrator()
    contract, created = ensure_dispatch_contract(
        orchestrator.execute_command,
        tool="maven",
        effective_action="validate",
        expected_cwd="/workspace/proj",
        expected_argv="mvn validate",
        requirements=MANIFEST,
    )
    assert contract is None
    assert created is False
    assert orchestrator.commands == []


def test_freeze_contract_returns_none_when_the_write_fails():
    """A contract that is not on disk does not exist; the caller must refuse."""
    orchestrator = RecordingOrchestrator(fail_contract_write=True)

    assert freeze_contract(orchestrator.execute_command, **FREEZE_ARGS) is None


def test_freeze_contract_returns_none_when_the_container_is_gone():
    def execute(command, **kwargs):
        raise RuntimeError("container is gone")

    assert freeze_contract(execute, **FREEZE_ARGS) is None


def test_freeze_contract_refuses_a_call_the_canonical_form_cannot_hold():
    """An uncommittable call is not dispatched: the freeze fails closed."""
    orchestrator = RecordingOrchestrator()
    args = dict(FREEZE_ARGS, params={"action": "test", "sink": object()})

    assert freeze_contract(orchestrator.execute_command, **args) is None
    assert contracts_written(orchestrator.commands) == []


# ---------------------------------------------------------------------------
# compliance: the frozen argument vector versus the physical one
# ---------------------------------------------------------------------------


def test_compliance_is_exact_when_the_dispatch_ran_the_frozen_vector():
    assert compliance_class("--fail-at-end verify", "mvn --fail-at-end verify") == "exact"


def test_compliance_is_exact_across_pure_shell_quoting():
    assert compliance_class("--tests 'Foo Bar'", 'gradle --tests "Foo Bar"') == "exact"


def test_compliance_is_equivalent_when_the_runner_added_its_own_flags():
    """Every frozen token ran, in the frozen order; the runner added its own."""
    assert (
        compliance_class(
            "--fail-at-end verify",
            "/opt/maven/bin/mvn --fail-at-end -Dmaven.test.failure.ignore=true verify",
        )
        == "equivalent"
    )


def test_compliance_is_deviated_when_a_frozen_token_never_ran():
    assert compliance_class("test", "gradle --continue build") == "deviated"


def test_compliance_is_deviated_when_the_frozen_order_was_not_kept():
    assert compliance_class("-pl core compile", "mvn compile -pl core") == "deviated"


def test_compliance_is_deviated_when_the_dispatch_dropped_the_whole_vector():
    assert compliance_class("--fail-at-end verify", "mvn") == "deviated"


def test_compliance_is_unknown_when_either_side_states_no_argv():
    assert compliance_class(None, "python -m pytest") is None
    assert compliance_class("test", "") is None


# ---------------------------------------------------------------------------
# the request scope the facade reads the envelope identity from
# ---------------------------------------------------------------------------


def test_action_context_defaults_to_a_model_sourced_unbound_scope():
    assert current_action_context() == ActionContext(envelope_id=None, intent_source="model")


def test_action_context_is_restored_when_the_scope_closes():
    with action_context(envelope_id="forced-000003", intent_source="controller"):
        assert current_action_context().envelope_id == "forced-000003"
        assert current_action_context().intent_source == "controller"
    assert current_action_context().envelope_id is None


def test_contract_receipt_fields_are_absent_without_a_frozen_contract():
    assert contract_receipt_fields("mvn verify") == {}


def test_contract_receipt_fields_bind_the_receipt_to_the_contract():
    contract = build_contract(
        envelope_id="envelope-receipt-fields",
        tool="maven",
        params={"command": "verify"},
        effective_action="verify",
        expected_cwd="/workspace/proj",
        expected_argv="--fail-at-end verify",
        intent_source="controller",
    )

    with dispatch_contract(contract):
        assert current_contract() == contract
        assert contract_receipt_fields("mvn --fail-at-end verify") == {
                "contract_id": contract["contract_id"],
                "contract_hash": contract["contract_hash"],
                "execution_binding": "argv_v1",
                "compliance": "exact",
        }
    assert current_contract() is None


# ---------------------------------------------------------------------------
# build facade: the freeze precedes the dispatch, and gates it
# ---------------------------------------------------------------------------


def test_contract_is_frozen_before_the_runner_is_dispatched():
    tool, orchestrator, _ = _build_tool({"pom.xml"})

    with build_action_context(
        "envelope-000042", action="test", working_directory="/workspace/proj"
    ):
        result = tool.execute(action="test", working_directory="/workspace/proj")

    assert result.succeeded
    froze = orchestrator.commands.index(
        next(
            command
            for command in orchestrator.commands
            if "fcntl.flock" in command and CONTRACT_DIR in command
        )
    )
    assert froze < orchestrator.commands.index(DISPATCH)
    (contract,) = contracts_written(orchestrator.commands)
    assert contract["envelope_id"] == "envelope-000042"


def test_build_refuses_default_root_without_retargeting_or_freezing_a_contract():
    """A project name cannot mutate frozen cwd or authorize runner dispatch."""
    orchestrator = RecordingOrchestrator({"/workspace/proj/pom.xml"})
    orchestrator.project_name = "proj"
    tool, orchestrator, backends = _build_tool({"pom.xml"}, orchestrator=orchestrator)

    with build_action_context("envelope-000044", action="compile"):
        result = tool.execute(action="compile")

    assert result.operation_outcome.value == "unknown"
    assert result.error_code == "BUILD_SYSTEM_NOT_DETECTED"
    assert contracts_written(orchestrator.commands) == []
    assert all(backend.calls == [] for backend in backends.values())
    assert not any("/workspace/proj/" in command for command in orchestrator.commands)


def test_the_result_carries_the_contract_identity_to_the_control_event():
    """The control event only ever sees the ToolResult metadata, and that is
    where the verifier picks the chain up (plan §Stage B)."""
    tool, orchestrator, _ = _build_tool({"pom.xml"})

    with build_action_context(
        "envelope-000043", action="compile", working_directory="/workspace/proj"
    ):
        result = tool.execute(action="compile", working_directory="/workspace/proj")

    (contract,) = contracts_written(orchestrator.commands)
    assert result.metadata["contract_id"] == contract["contract_id"]
    assert result.metadata["contract_hash"] == contract["contract_hash"]


def test_maven_contract_pins_the_materialized_lifecycle_and_cwd():
    tool, orchestrator, _ = _build_tool({"pom.xml"})

    with build_action_context(
        "envelope-000001", action="package", working_directory="/workspace/proj"
    ):
        tool.execute(action="package", working_directory="/workspace/proj")

    (contract,) = contracts_written(orchestrator.commands)
    assert contract["effective_action"] == "package"
    assert contract["expected_argv"] == "--fail-at-end package -DskipTests"
    assert contract["expected_cwd"] == "/workspace/proj"
    assert contract["requested_call"] == {
        "tool": "build",
        "params": {"action": "package", "working_directory": "/workspace/proj"},
    }


def test_gradle_contract_pins_the_materialized_tasks():
    tool, orchestrator, _ = _build_tool({"build.gradle"})

    with build_action_context(
        "envelope-000002", action="package", working_directory="/workspace/proj"
    ):
        tool.execute(action="package", working_directory="/workspace/proj")

    (contract,) = contracts_written(orchestrator.commands)
    assert contract["effective_action"] == "assemble"
    assert contract["expected_argv"] == "--continue -x test assemble"


def test_python_contract_states_the_action_and_no_argv_it_cannot_know():
    """The venv interpreter and the junit path are resolved inside python_tool;
    the facade never guesses an argv it did not materialize."""
    tool, orchestrator, _ = _build_tool({"pyproject.toml"})

    with build_action_context(
        "envelope-000003", action="test", working_directory="/workspace/proj"
    ):
        tool.execute(action="test", working_directory="/workspace/proj")

    (contract,) = contracts_written(orchestrator.commands)
    assert contract["effective_action"] == "test"
    assert "expected_argv" not in contract


def test_contract_persistence_failure_refuses_the_dispatch():
    orchestrator = RecordingOrchestrator({"pom.xml"}, fail_contract_write=True)
    tool, orchestrator, backends = _build_tool({"pom.xml"}, orchestrator=orchestrator)

    with build_action_context(
        "envelope-000009", action="test", working_directory="/workspace/proj"
    ):
        result = tool.execute(action="test", working_directory="/workspace/proj")

    assert not result.succeeded
    assert result.error_code == CONTRACT_PERSIST_FAILED
    assert backends["maven_tool"].calls == []
    assert DISPATCH not in orchestrator.commands
    (assessment,) = assessments_written(orchestrator.commands)
    assert assessment["stage"] == "materialization"
    assert assessment["typed_code"] == CONTRACT_PERSIST_FAILED
    assert assessment["event_or_intent_id"] == "envelope-000009"


def test_python_contract_persistence_failure_refuses_before_the_internal_runner():
    orchestrator = RecordingOrchestrator({"pyproject.toml"}, fail_contract_write=True)
    tool, orchestrator, backends = _build_tool({"pyproject.toml"}, orchestrator=orchestrator)

    with build_action_context(
        "envelope-python-unpersisted", action="deps", working_directory="/workspace/proj"
    ):
        result = tool.execute(action="deps", working_directory="/workspace/proj")

    assert result.error_code == CONTRACT_PERSIST_FAILED
    assert backends["python_tool"].calls == []
    assert DISPATCH not in orchestrator.commands


def test_refused_dispatch_leaves_no_contract_bound_to_the_request():
    orchestrator = RecordingOrchestrator({"pom.xml"}, fail_contract_write=True)
    tool, _, _ = _build_tool({"pom.xml"}, orchestrator=orchestrator)

    with build_action_context(
        "envelope-000010", action="test", working_directory="/workspace/proj"
    ):
        tool.execute(action="test", working_directory="/workspace/proj")

    assert current_contract() is None


def test_dispatch_unbinds_the_contract_when_the_facade_returns():
    tool, _, _ = _build_tool({"pom.xml"})

    with build_action_context(
        "envelope-000011", action="compile", working_directory="/workspace/proj"
    ):
        tool.execute(action="compile", working_directory="/workspace/proj")

    assert current_contract() is None


def test_forced_dispatch_freezes_a_controller_sourced_contract():
    tool, orchestrator, _ = _build_tool({"pom.xml"})

    with build_action_context(
        "forced-000007",
        action="test",
        working_directory="/workspace/proj",
        intent_source="controller",
    ):
        tool.execute(action="test", working_directory="/workspace/proj")

    (contract,) = contracts_written(orchestrator.commands)
    assert contract["intent_source"] == "controller"
    assert contract["envelope_id"] == "forced-000007"


def test_a_dispatch_without_a_recorded_envelope_states_that_absence():
    """A complete sinkless engine intent gets one unique absence per dispatch."""
    tool, orchestrator, _ = _build_tool({"pom.xml"})

    with build_action_context(
        None, action="compile", working_directory="/workspace/proj"
    ):
        tool.execute(action="compile", working_directory="/workspace/proj")
        tool.execute(action="compile", working_directory="/workspace/proj")

    first, second = contracts_written(orchestrator.commands)
    assert first["envelope_id"].startswith("envelope-unrecorded-")
    assert first["intent_source"] == "model"
    # Two dispatches are two contracts even when nothing else differs.
    assert first["envelope_id"] != second["envelope_id"]
    assert first["contract_id"] != second["contract_id"]


def test_a_dispatch_without_engine_intent_authority_refuses_before_any_probe():
    tool, orchestrator, backends = _build_tool({"pom.xml"})

    result = tool.execute(action="compile", working_directory="/workspace/proj")

    assert result.error_code == CONTRACT_AUTHORITY_MISSING
    assert result.metadata["runner_dispatched"] is False
    assert orchestrator.commands == []
    assert backends["maven_tool"].calls == []


# ---------------------------------------------------------------------------
# receipts carry the binding back
# ---------------------------------------------------------------------------


def _record(execute, argv):
    return record_invocation(
        execute,
        tool="maven",
        attempt=1,
        requested_action="verify",
        effective_action="verify",
        argv=argv,
        working_directory="/workspace/proj",
        exit_code=0,
        before={},
        after={},
        **contract_receipt_fields(argv),
    )


def test_receipt_carries_the_contract_identity_and_an_exact_compliance():
    orchestrator = RecordingOrchestrator()
    contract = build_contract(
        envelope_id="envelope-receipt-exact",
        tool="maven",
        params={"command": "verify"},
        effective_action="verify",
        expected_cwd="/workspace/proj",
        expected_argv="--fail-at-end verify",
        intent_source="controller",
    )

    with dispatch_contract(contract):
        _record(orchestrator.execute_command, "mvn --fail-at-end verify")

    (receipt,) = receipts_written(orchestrator.commands)
    assert receipt["contract_id"] == contract["contract_id"]
    assert receipt["contract_hash"] == contract["contract_hash"]
    assert receipt["compliance"] == "exact"


def test_receipt_reports_a_dispatch_that_left_the_frozen_action():
    orchestrator = RecordingOrchestrator()
    contract = build_contract(
        envelope_id="envelope-receipt-deviated",
        tool="maven",
        params={"command": "verify"},
        effective_action="verify",
        expected_cwd="/workspace/proj",
        expected_argv="--fail-at-end verify",
        intent_source="controller",
    )

    with dispatch_contract(contract):
        _record(orchestrator.execute_command, "mvn --fail-at-end package")

    (receipt,) = receipts_written(orchestrator.commands)
    assert receipt["compliance"] == "deviated"


def test_receipt_states_no_compliance_without_a_frozen_contract():
    orchestrator = RecordingOrchestrator()

    _record(orchestrator.execute_command, "mvn verify")

    (receipt,) = receipts_written(orchestrator.commands)
    assert "compliance" not in receipt
    assert "contract_id" not in receipt


def test_facade_dispatch_binds_its_receipt_to_the_contract_it_froze():
    """End to end through the facade: the receipt the runner writes carries the
    identity of the contract the facade froze for that same dispatch."""
    orchestrator = RecordingOrchestrator({"pom.xml"})
    seen = {}

    class ReceiptWritingTool:
        def __init__(self):
            self.orchestrator = orchestrator
            self.calls = []

        def execute(self, **kwargs):
            self.calls.append(kwargs)
            seen.update(contract_receipt_fields("mvn --fail-at-end verify"))
            return ToolResult.completed_success(output="BUILD SUCCESS")

    orchestrator.publish_manifest()
    tool = BuildTool(orchestrator, maven_tool=ReceiptWritingTool())

    with build_action_context(
        "envelope-000021", action="test", working_directory="/workspace/proj"
    ):
        tool.execute(action="test", working_directory="/workspace/proj")

    (contract,) = contracts_written(orchestrator.commands)
    assert seen == {
        "contract_id": contract["contract_id"],
        "contract_hash": contract["contract_hash"],
        "execution_binding": "argv_v1",
        "compliance": "exact",
    }


def test_the_real_maven_dispatch_runs_the_vector_its_contract_froze():
    """Drift guard over the WHOLE chain with the real MavenTool: the facade
    predicts an argument vector, the runner builds the physical command, and
    the receipt states how the two relate. If either side moves, the
    compliance class moves with it and this fails loudly."""
    from test_build_tool_preflight_integration import EndToEndOrch, _e2e_build_tool

    orch = EndToEndOrch([(True, "BUILD SUCCESS")], java="17", manifest={"java_version": "17"})

    with build_action_context(
        "envelope-000099", action="compile", working_directory="/workspace/proj"
    ):
        result = _e2e_build_tool(orch).execute(
            action="compile",
            working_directory="/workspace/proj",
        )

    assert result.succeeded
    (contract,) = contracts_written(orch.commands)
    (receipt,) = receipts_written(orch.commands)
    assert contract["expected_argv"] == "--fail-at-end compile"
    assert receipt["argv"] == "mvn --fail-at-end compile"
    assert receipt["contract_id"] == contract["contract_id"]
    assert receipt["contract_hash"] == contract["contract_hash"]
    assert receipt["compliance"] == "exact"


def test_the_gradle_vector_the_facade_freezes_is_the_one_the_runner_builds():
    """Same drift guard for gradle, at the seam where the two token orders
    have to agree (`GradleBackend.expected_argv` mirrors
    `GradleTool._build_gradle_command`)."""
    from sag.tools.build.backends import GradleBackend
    from sag.tools.internal.gradle_tool import GradleTool

    backend = GradleBackend(SimpleNamespace(orchestrator=None))
    params = backend.materialize("package", "--info", "/workspace/proj", None)
    physical = GradleTool._build_gradle_command(
        None,
        "gradle",
        params["tasks"],
        "",
        params.get("gradle_args"),
        "",
        False,
        False,
        False,
        params.get("fail_at_end", False),
    )

    assert params["gradle_args"] == "--info -x test"
    assert compliance_class(backend.expected_argv(params), physical) == "exact"


# ---------------------------------------------------------------------------
# the engine publishes the identity the facade reads
# ---------------------------------------------------------------------------


def test_action_envelope_emission_opens_the_request_scope(tmp_path):
    from sag.agent.control_events import ControlEventSink
    from sag.agent.react_engine import ReActEngine
    from sag.agent.react_types import StepType

    engine = object.__new__(ReActEngine)
    engine.control_event_sink = ControlEventSink(tmp_path / "control_events.jsonl")
    engine._active_control_envelope_id = None
    engine.steps = [SimpleNamespace(step_type=StepType.ACTION, tool_call_id="call-1")]

    envelope_id = engine._emit_control_action_envelope("build", {"action": "test"})

    assert current_action_context() == ActionContext(
        envelope_id=envelope_id,
        intent_source="model",
        control_recording_active=True,
    )


def test_a_sinkless_engine_opens_no_scope_for_the_next_dispatch(tmp_path):
    from sag.agent.react_engine import ReActEngine

    engine = object.__new__(ReActEngine)
    engine.control_event_sink = None
    engine._active_control_envelope_id = None
    engine.steps = []

    with action_context(envelope_id="envelope-000030"):
        assert engine._emit_control_action_envelope("build", {}) is None
        assert current_action_context().envelope_id is None


def test_forced_test_attempt_runs_under_a_controller_scope(forced_engine):
    """A harness-forced attempt is the controller's intent, and its contract
    must say so — the model never authored that call."""
    engine, requirement = forced_engine()
    dispatched = engine._execute_tool_call
    seen = {}

    def capture(call):
        seen["context"] = current_action_context()
        return dispatched(call)

    engine._execute_tool_call = capture

    assert engine._force_required_test_attempt(requirement, trigger="phase_floor") is True

    context = seen["context"]
    assert context.envelope_id == "forced-000001"
    assert context.intent_source == "controller"
    assert context.intent_id and context.intent_id.startswith("intent-")
    assert context.action_fingerprint and context.action_fingerprint.startswith("act-")
    assert context.repair_context_id is None
    assert current_action_context().envelope_id is None
