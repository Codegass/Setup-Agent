import json

import pytest
from pydantic import ValidationError

import sag.agent.repair_contexts as repair_contexts_module
from container_evidence_fakes import ContainerFS
from sag.agent.control_events import ControlEventSink
from sag.agent.evidence_publications import (
    EvidencePublicationAuthority,
    PublicationAttempt,
    install_evidence_publication_authority,
    reset_evidence_publication_authority,
)
from sag.agent.evidence_records import frame_named_json_record_stream
from sag.agent.repair_contexts import (
    REPAIR_CONTEXT_INVALID,
    REPAIR_CONTEXT_INVALID_ID,
    REPAIR_CONTEXT_LIVE_UNAVAILABLE,
    REPAIR_CONTEXT_MISSING,
    REPAIR_CONTEXT_READ,
    REPAIR_CONTEXT_READ_FAILED,
    RepairContext,
    build_repair_context,
    read_live_repair_context,
    read_live_repair_context_ledger,
    read_repair_context,
    repair_context_canonical_json,
    repair_context_identity,
    repair_context_path,
    validate_repair_context_record,
    write_repair_context,
)
from sag.utils.container_io import ContainerWriteResult


def context_payload():
    return {
        "repair_context_id": repair_context_identity("asm-1"),
        "trigger_assessment_id": "asm-1",
        "trigger_receipt_id": "rcp-1",
        "domain_id": "module-a",
        "typed_failure_or_capability": "dependency_incompatible_numpy",
        "blocker_owner": "project",
        "observed_fact_refs": ["fact-b", "fact-a"],
        "constraint_set": {
            "constraints": [
                {
                    "constraint_id": "constraint-python",
                    "kind": "version",
                    "subject": "python",
                    "relation": ">=",
                    "value": "3.10",
                    "source_refs": ["claim-b", "claim-a"],
                }
            ],
            "source_refs": ["claim-a"],
        },
        "allowed_tool_affordances": [
            {
                "tool": "build",
                "action_parameter": "action",
                "action_kinds": ["test", "deps"],
                "constraint_refs": ["constraint-python"],
            }
        ],
        "admissible_observation_types": ["report_delta", "environment_delta"],
        "supporting_claim_ids": ["claim-b", "claim-a"],
        "open_conflict_refs": ["conflict-1"],
    }


def test_repair_context_is_facts_constraints_and_affordances_only():
    context = RepairContext.model_validate(context_payload())

    assert context.typed_blocker == "dependency_incompatible_numpy"
    assert context.observed_fact_refs == ("fact-a", "fact-b")
    assert context.supporting_claim_ids == ("claim-a", "claim-b")
    assert context.constraint_set.constraints[0].source_refs == ("claim-a", "claim-b")
    assert context.allowed_tool_affordances[0].action_kinds == ("deps", "test")


@pytest.mark.parametrize("forbidden", ["params", "argv", "steps", "proposed_public_call"])
def test_repair_context_rejects_prescriptive_top_level_fields(forbidden):
    payload = context_payload()
    payload[forbidden] = {} if forbidden != "steps" else []

    with pytest.raises(ValidationError):
        RepairContext.model_validate(payload)


@pytest.mark.parametrize("forbidden", ["params", "argv", "steps", "proposed_public_call"])
def test_repair_context_rejects_prescriptive_nested_affordance_fields(forbidden):
    payload = context_payload()
    payload["allowed_tool_affordances"][0][forbidden] = {}

    with pytest.raises(ValidationError):
        RepairContext.model_validate(payload)


def test_policy_factory_has_no_public_call_or_step_input():
    context = build_repair_context(
        trigger_assessment_id="asm-1",
        trigger_receipt_id="rcp-1",
        typed_blocker="timeout",
        blocker_owner="unknown",
        observed_fact_refs=("fact-1",),
        allowed_tool_affordances=(
            {
                "tool": "build",
                "action_parameter": "action",
                "action_kinds": ("test",),
                "constraint_refs": (),
            },
        ),
        admissible_observation_types=("report_delta",),
    )
    payload = context.model_dump(mode="json", exclude_none=True)

    assert context.repair_context_id == repair_context_identity("asm-1")
    assert not {"params", "argv", "steps", "proposed_public_call"} & payload.keys()


def test_repair_context_rejects_an_unknown_blocker_owner():
    payload = context_payload()
    payload["blocker_owner"] = "model"

    with pytest.raises(ValidationError):
        RepairContext.model_validate(payload)


def test_repair_context_copy_cannot_bypass_nested_schema():
    context = RepairContext.model_validate(context_payload())

    with pytest.raises(ValidationError):
        context.model_copy(
            update={"allowed_tool_affordances": ({"tool": "build", "params": {"action": "test"}},)}
        )


def test_repair_context_json_round_trip_preserves_strict_shape():
    context = RepairContext.model_validate(context_payload())

    assert RepairContext.model_validate_json(context.model_dump_json()) == context


def test_repair_context_identity_is_engine_owned():
    payload = context_payload()
    payload["repair_context_id"] = repair_context_identity("some-other-assessment")

    with pytest.raises(ValidationError):
        RepairContext.model_validate(payload)


def test_write_repair_context_uses_atomic_validated_engine_owned_path(monkeypatch):
    context = RepairContext.model_validate(context_payload())
    calls = []

    def atomic_write(execute, path, content, *, validate_json=False):
        calls.append((execute, path, content, validate_json))
        return ContainerWriteResult(True, "persisted", len(content.encode()), "a" * 64)

    monkeypatch.setattr(repair_contexts_module, "write_container_text_atomic", atomic_write)
    execute = object()

    result = write_repair_context(execute, context)

    assert result.persisted is True
    assert result.code == "persisted"
    assert calls == [
        (
            execute,
            repair_context_path(context.repair_context_id),
            calls[0][2],
            True,
        )
    ]
    assert RepairContext.model_validate_json(calls[0][2]) == context
    assert "<<" not in calls[0][2]


def test_write_repair_context_preserves_atomic_transport_failure_without_fallback(monkeypatch):
    context = RepairContext.model_validate(context_payload())
    calls = []

    def refuse_write(*args, **kwargs):
        calls.append((args, kwargs))
        return ContainerWriteResult(False, "transport_publish_failed")

    monkeypatch.setattr(repair_contexts_module, "write_container_text_atomic", refuse_write)

    result = write_repair_context(lambda command: {"exit_code": 0}, context)

    assert result == ContainerWriteResult(False, "transport_publish_failed")
    assert len(calls) == 1
    assert calls[0][1] == {"validate_json": True}


def test_write_repair_context_revalidates_constructed_instance_before_choosing_path(
    monkeypatch,
):
    payload = dict(RepairContext.model_validate(context_payload()).__dict__)
    payload["repair_context_id"] = repair_context_identity("unrelated-assessment")
    calls = []
    monkeypatch.setattr(
        repair_contexts_module,
        "write_container_text_atomic",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    with pytest.raises(ValidationError):
        RepairContext.model_construct(**payload)
    assert calls == []


class _ReadOrchestrator:
    def __init__(self, result):
        self.result = result
        self.commands = []

    def execute_command(self, command, *, workdir=None, timeout=None):
        self.commands.append((command, workdir, timeout))
        return self.result


def test_read_repair_context_strictly_validates_json_and_expected_identity():
    context = RepairContext.model_validate(context_payload())
    orchestrator = _ReadOrchestrator({"exit_code": 0, "output": context.model_dump_json()})

    result = read_repair_context(orchestrator, context.repair_context_id)

    assert result.code == REPAIR_CONTEXT_READ
    assert result.ok is True
    assert result.context == context
    assert orchestrator.commands == [
        (
            f"cat -- {repair_context_path(context.repair_context_id)} 2>/dev/null",
            None,
            30,
        )
    ]


@pytest.mark.parametrize(
    ("transport_result", "expected_code"),
    [
        ({"exit_code": 1, "output": ""}, REPAIR_CONTEXT_MISSING),
        ({"exit_code": 0, "output": "{not-json"}, REPAIR_CONTEXT_INVALID),
        ({"exit_code": -1, "output": "docker unavailable"}, REPAIR_CONTEXT_READ_FAILED),
    ],
)
def test_read_repair_context_distinguishes_missing_invalid_and_transport_failure(
    transport_result, expected_code
):
    identifier = repair_context_identity("asm-1")

    result = read_repair_context(_ReadOrchestrator(transport_result), identifier)

    assert result.context is None
    assert result.code == expected_code
    assert result.ok is False


def test_read_repair_context_accepts_callable_and_rejects_non_engine_id():
    context = RepairContext.model_validate(context_payload())
    calls = []

    def execute(command):
        calls.append(command)
        return {"success": True, "output": context.model_dump_json()}

    result = read_repair_context(execute, context.repair_context_id)
    invalid = read_repair_context(execute, "../../borrowed")

    assert result.context == context
    assert result.code == REPAIR_CONTEXT_READ
    assert invalid.code == REPAIR_CONTEXT_INVALID_ID
    assert len(calls) == 1


def _published_context_store(context, authority):
    raw = repair_context_canonical_json(context)
    store = ContainerFS({repair_context_path(context.repair_context_id): raw})
    authority.publish_bytes(
        record_kind="repair_context",
        record_id=context.repair_context_id,
        raw=raw.encode("utf-8"),
    )
    return store, raw


def test_validate_repair_context_record_requires_canonical_schema_id_and_filename():
    context = RepairContext.model_validate(context_payload())
    payload = json.loads(repair_context_canonical_json(context))

    assert validate_repair_context_record(payload, context.repair_context_id) == payload
    with pytest.raises(ValueError, match="filename"):
        validate_repair_context_record(payload, repair_context_identity("asm-other"))
    with pytest.raises(ValueError, match="canonical"):
        validate_repair_context_record(
            {**payload, "trigger_receipt_id": None, "extra": True}, context.repair_context_id
        )


def test_live_repair_context_requires_exact_host_published_ledger(
    bind_host_evidence_publication_authority,
):
    context = RepairContext.model_validate(context_payload())
    store, _raw = _published_context_store(context, bind_host_evidence_publication_authority)

    ledger = read_live_repair_context_ledger(store)
    selected = read_live_repair_context(store, context.repair_context_id)

    assert ledger.complete is True
    assert ledger.conflict is None
    assert [record.payload for record in ledger.records] == [context.model_dump(mode="json")]
    assert selected == repair_contexts_module.RepairContextReadResult(
        context,
        REPAIR_CONTEXT_READ,
    )


def test_live_repair_context_rejects_container_mirror_without_host_publication():
    context = RepairContext.model_validate(context_payload())
    raw = repair_context_canonical_json(context)
    store = ContainerFS({repair_context_path(context.repair_context_id): raw})

    ledger = read_live_repair_context_ledger(store)
    selected = read_live_repair_context(store, context.repair_context_id)

    assert ledger.complete is False
    assert ledger.conflict == "publication_not_published"
    assert selected.context is None
    assert selected.code == REPAIR_CONTEXT_LIVE_UNAVAILABLE


def test_live_repair_context_rejects_tamper_and_host_expected_deletion(
    bind_host_evidence_publication_authority,
):
    context = RepairContext.model_validate(context_payload())
    store, raw = _published_context_store(context, bind_host_evidence_publication_authority)
    path = repair_context_path(context.repair_context_id)

    store.files[path] = raw + " "
    tampered = read_live_repair_context_ledger(store)
    store.files.pop(path)
    deleted = read_live_repair_context_ledger(store)

    assert tampered.complete is False
    assert tampered.conflict == "publication_mismatch"
    assert deleted.complete is False
    assert deleted.conflict == "publication_set_mismatch"


def test_live_repair_context_rejects_unpublished_neighbor_and_duplicate_semantic_id(
    bind_host_evidence_publication_authority,
):
    context = RepairContext.model_validate(context_payload())
    store, raw = _published_context_store(context, bind_host_evidence_publication_authority)
    other_id = repair_context_identity("asm-other")
    store.files[repair_context_path(other_id)] = raw

    ledger = read_live_repair_context_ledger(store)

    assert ledger.complete is False
    assert ledger.conflict == "record_schema_invalid"
    assert other_id in ledger.detail


def test_live_repair_context_rejects_duplicate_atomic_filename():
    context = RepairContext.model_validate(context_payload())
    raw = repair_context_canonical_json(context)
    filename = f"{context.repair_context_id}.json"

    def duplicate_stream(_command, **_kwargs):
        return {
            "success": True,
            "output": frame_named_json_record_stream([(filename, raw), (filename, raw)]),
        }

    ledger = read_live_repair_context_ledger(duplicate_stream)

    assert ledger.complete is False
    assert ledger.conflict == "stream_malformed"


@pytest.mark.parametrize(
    ("raw", "expected_conflict"),
    [
        ("{not-json", "record_malformed"),
        ('{"schema_version":1,"schema_version":1}', "record_malformed"),
    ],
)
def test_live_repair_context_rejects_host_published_malformed_bytes(
    raw,
    expected_conflict,
    bind_host_evidence_publication_authority,
):
    identifier = repair_context_identity("asm-1")
    store = ContainerFS({repair_context_path(identifier): raw})
    bind_host_evidence_publication_authority.publish_bytes(
        record_kind="repair_context",
        record_id=identifier,
        raw=raw.encode("utf-8"),
    )

    ledger = read_live_repair_context_ledger(store)

    assert ledger.complete is False
    assert ledger.conflict == expected_conflict


def test_live_repair_context_rejects_host_published_future_schema(
    bind_host_evidence_publication_authority,
):
    context = RepairContext.model_validate(context_payload())
    payload = context.model_dump(mode="json")
    payload["schema_version"] = 2
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    store = ContainerFS({repair_context_path(context.repair_context_id): raw})
    bind_host_evidence_publication_authority.publish_bytes(
        record_kind="repair_context",
        record_id=context.repair_context_id,
        raw=raw.encode("utf-8"),
    )

    ledger = read_live_repair_context_ledger(store)

    assert ledger.complete is False
    assert ledger.conflict == "record_schema_invalid"


def test_publication_failure_leaves_only_a_forensic_repair_context(monkeypatch):
    context = RepairContext.model_validate(context_payload())
    persisted = {}

    def atomic_write(_execute, path, content, *, validate_json=False):
        persisted[path] = content
        return ContainerWriteResult(
            True,
            "persisted",
            len(content.encode("utf-8")),
            "a" * 64,
        )

    monkeypatch.setattr(repair_contexts_module, "write_container_text_atomic", atomic_write)
    monkeypatch.setattr(
        "sag.agent.evidence_publications.publish_evidence_bytes",
        lambda *_args, **_kwargs: PublicationAttempt(
            "publication_unavailable",
            detail="host disk unavailable",
        ),
    )

    written = write_repair_context(object(), context)
    raw = persisted[repair_context_path(context.repair_context_id)]
    forensic = read_repair_context(
        _ReadOrchestrator({"exit_code": 0, "output": raw}),
        context.repair_context_id,
    )
    live = read_live_repair_context(
        ContainerFS({repair_context_path(context.repair_context_id): raw}),
        context.repair_context_id,
    )

    assert written.persisted is False
    assert written.code == "publication_unavailable"
    assert forensic.context == context
    assert forensic.code == REPAIR_CONTEXT_READ
    assert live.context is None
    assert live.code == REPAIR_CONTEXT_LIVE_UNAVAILABLE


def test_restart_authority_recovers_exact_repair_context_bytes(tmp_path):
    context = RepairContext.model_validate(context_payload())
    raw = repair_context_canonical_json(context)
    store = ContainerFS({repair_context_path(context.repair_context_id): raw})
    sink = ControlEventSink(tmp_path / "host-control-events.jsonl")
    live = EvidencePublicationAuthority.for_live_run(run_id="run-repair-restart", sink=sink)
    live.publish_bytes(
        record_kind="repair_context",
        record_id=context.repair_context_id,
        raw=raw.encode("utf-8"),
    )
    restarted = EvidencePublicationAuthority.recover_from_host_jsonl(
        sink.path,
        run_id="run-repair-restart",
    )
    token = install_evidence_publication_authority(restarted)
    try:
        selected = read_live_repair_context(store, context.repair_context_id)
    finally:
        reset_evidence_publication_authority(token)

    assert selected.context == context
    assert selected.code == REPAIR_CONTEXT_READ


def test_live_repair_context_rejects_invalid_requested_identifier_without_transport():
    store = ContainerFS()

    selected = read_live_repair_context(store, "../../borrowed")

    assert selected.context is None
    assert selected.code == REPAIR_CONTEXT_INVALID_ID
    assert store.commands == []
