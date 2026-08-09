import json

import pytest
from pydantic import ValidationError

from sag.agent.action_intents import (
    ActionIntent,
    ActionIntentSubmission,
    EngineActionIntentFactory,
    action_fingerprint,
    canonical_params,
)


def submission(**overrides):
    body = {
        "domain_id": "module-a",
        "tool": "build",
        "params": {"working_directory": "/workspace/repo", "action": "test"},
        "blocking_fact_refs": ["fact-b", "fact-a"],
        "repair_hypothesis": "a narrower test will expose the blocker",
        "next_action_kind": "test",
        "expected_observation": ["report_delta"],
        "stop_condition": "stop after one terminal receipt",
    }
    body.update(overrides)
    return body


def test_engine_factory_owns_source_identity_and_fingerprint():
    model_factory = EngineActionIntentFactory.for_model()
    controller_factory = EngineActionIntentFactory.for_controller()

    model_intent = model_factory.from_submission(submission(), tool_call_id="call-1")
    controller_intent = controller_factory.from_submission(submission(), tool_call_id="call-2")

    assert model_intent.source == "model"
    assert controller_intent.source == "controller"
    assert model_intent.intent_id != controller_intent.intent_id
    assert model_intent.action_fingerprint == controller_intent.action_fingerprint
    assert model_intent.blocking_fact_refs == ("fact-a", "fact-b")


def test_submission_cannot_self_attest_engine_fields():
    factory = EngineActionIntentFactory.for_model()

    for key, value in (
        ("source", "controller"),
        ("intent_id", "intent-forged"),
        ("action_fingerprint", "act-forged"),
        ("predecessor_contract_id", "ic-forged"),
    ):
        with pytest.raises(ValidationError):
            factory.from_submission(submission(**{key: value}))


def test_factory_revalidates_an_existing_submission_instance():
    candidate = ActionIntentSubmission.model_validate(submission())
    object.__setattr__(candidate, "repair_hypothesis", "x" * 50_000)

    with pytest.raises(ValidationError, match="length|characters"):
        EngineActionIntentFactory.for_model().from_submission(candidate)


def test_action_fingerprint_ignores_prose_call_id_and_ref_order():
    first = action_fingerprint(
        domain_id="module-a",
        tool="build",
        params={
            "action": "test",
            "tool_call_id": "call-1",
            "reason": "first explanation",
            "evidence_refs": ["ref-b", "ref-a"],
        },
        tool_call_id="outer-1",
        prose="first prose",
        refs=("ref-b", "ref-a"),
    )
    second = action_fingerprint(
        domain_id="module-a",
        tool="build",
        params={
            "evidence_refs": ["ref-a", "ref-b"],
            "reason": "different explanation",
            "tool_call_id": "call-2",
            "action": "test",
        },
        tool_call_id="outer-2",
        prose="different prose",
        refs=("ref-a", "ref-b"),
    )

    assert first == second


@pytest.mark.parametrize(
    ("changed", "value"),
    [
        ("domain", {"domain_id": "module-b", "tool": "build", "params": {"action": "test"}}),
        ("tool", {"domain_id": "module-a", "tool": "search", "params": {"action": "test"}}),
        (
            "params",
            {"domain_id": "module-a", "tool": "build", "params": {"action": "compile"}},
        ),
    ],
)
def test_action_fingerprint_distinguishes_executable_identity(changed, value):
    del changed
    baseline = action_fingerprint(
        domain_id="module-a",
        tool="build",
        params={"action": "test"},
    )

    assert action_fingerprint(**value) != baseline


def test_canonical_params_preserves_semantic_list_order_but_sorts_ref_sets():
    canonical = canonical_params(
        {
            "args": ["test", "-pl", "module-a"],
            "evidence_refs": ["z", "a"],
            "nested": {"fact_refs": ["fact-2", "fact-1"]},
        }
    )

    assert canonical["args"] == ["test", "-pl", "module-a"]
    assert canonical["evidence_refs"] == ["a", "z"]
    assert canonical["nested"]["fact_refs"] == ["fact-1", "fact-2"]


def test_action_intent_rejects_a_forged_fingerprint_on_read():
    intent = EngineActionIntentFactory.for_model().from_submission(submission())
    payload = intent.model_dump(mode="json")
    payload["action_fingerprint"] = "act-forged"

    with pytest.raises(ValidationError):
        ActionIntent.model_validate(payload)


def test_action_intent_copy_cannot_reassign_engine_owned_provenance():
    intent = EngineActionIntentFactory.for_model().from_submission(submission())

    with pytest.raises(ValueError, match="engine-owned"):
        intent.model_copy(update={"source": "controller"})
    with pytest.raises(ValueError, match="engine-owned"):
        intent.model_copy(update={"action_fingerprint": "act-forged"})
    with pytest.raises(ValueError, match="engine-owned"):
        intent.model_copy(update={"predecessor_contract_id": "ic-forged"})


def test_action_intent_json_round_trip_rechecks_fingerprint():
    intent = EngineActionIntentFactory.for_model().from_submission(submission())

    assert ActionIntent.model_validate_json(intent.model_dump_json()) == intent


def test_repair_link_requires_the_model_owned_reasoning_fields():
    factory = EngineActionIntentFactory.for_model()

    with pytest.raises(ValueError, match="repair-linked"):
        factory.from_submission(
            {
                "domain_id": "module-a",
                "tool": "build",
                "params": {"action": "test"},
            },
            trigger_assessment_id="asm-1",
            repair_context_id="rcx-1",
            repair_context_sha256="a" * 64,
        )

    intent = factory.from_submission(
        submission(),
        trigger_assessment_id="asm-1",
        repair_context_id="rcx-1",
        repair_context_sha256="a" * 64,
        predecessor_contract_id="ic-000000000001",
    )
    assert intent.repair_context_id == "rcx-1"
    assert intent.predecessor_contract_id == "ic-000000000001"

    payload = intent.model_dump(mode="json")
    payload["predecessor_contract_id"] = "ic-forged"
    with pytest.raises(ValidationError, match="predecessor_contract_id"):
        ActionIntent.model_validate(payload)
    with pytest.raises(ValidationError, match="predecessor_contract_id"):
        ActionIntent.model_construct(**payload)
    with pytest.raises(ValidationError, match="predecessor_contract_id"):
        ActionIntent.model_validate_json(json.dumps(payload))


def test_controller_intent_cannot_consume_a_model_repair_context():
    with pytest.raises(ValidationError, match="model-owned"):
        EngineActionIntentFactory.for_controller().from_submission(
            submission(),
            trigger_assessment_id="asm-1",
            repair_context_id="rcx-1",
            repair_context_sha256="a" * 64,
        )
