"""Focused contracts for model-authored and engine-sealed execution plans."""

import hashlib
import json

import pytest
from test_container_io import FakeContainer

from sag.agent.document_map import DocumentMapEntry, document_map_fingerprint, entry_id
from sag.agent.control_events import canonical_sha256
from sag.agent.project_execution_plan import (
    MAX_INVENTORY_PROMPT_CHARS,
    MAX_SYSTEM_PROMPT_CHARS,
    PROJECT_EXECUTION_PLAN_PATH,
    ExecutionStep,
    ProjectExecutionPlan,
    ProjectExecutionPlanPersistenceError,
    ProjectExecutionPlanReadError,
    ProjectExecutionPlanValidationError,
    SealedProjectExecutionPlan,
    bind_project_execution_plan_inventory,
    canonical_authored_plan_sha256,
    read_sealed_project_execution_plan,
    render_document_inventory_guidance,
    render_plan_system_prompt,
    seal_and_write_project_execution_plan,
    seal_project_execution_plan,
    validate_authored_plan,
    validate_reviewed_document_evidence,
    write_sealed_project_execution_plan,
)

ROOT = "/workspace/ignite"
ATTEMPT_ID = "analyze-1"
CLAIM_SHA = "c" * 64

DEVNOTES_PATH = f"{ROOT}/DEVNOTES.txt"
DEVNOTES_TEXT = "Build with ./mvnw clean install -DskipTests\n"
DEVNOTES_HASH = hashlib.sha256(DEVNOTES_TEXT.encode()).hexdigest()
DEVNOTES = DocumentMapEntry(
    entry_id=entry_id(DEVNOTES_PATH),
    path=DEVNOTES_PATH,
    realpath=DEVNOTES_PATH,
    source_hash=DEVNOTES_HASH,
    kind="text",
)

README_PATH = f"{ROOT}/README.md"
README_TEXT = "# Ignite\n"
README_HASH = hashlib.sha256(README_TEXT.encode()).hexdigest()
README = DocumentMapEntry(
    entry_id=entry_id(README_PATH),
    path=README_PATH,
    realpath=README_PATH,
    source_hash=README_HASH,
    kind="markdown",
)


def document_map(*entries, partial=()):
    selected = list(entries or (DEVNOTES,))
    return {
        "entries": selected,
        "document_map_fingerprint": document_map_fingerprint(selected),
        "partial_map": list(partial),
    }


def candidate():
    return {
        "summary": "Build without tests first; run the documented focused test lane afterward.",
        "documents_reviewed": [
            {
                "path": DEVNOTES_PATH,
                "reason": "Defines the project's supported build and test entry points.",
                "evidence_refs": [DEVNOTES.entry_id],
            }
        ],
        "build_steps": [
            {
                "tool": "build",
                "params": {
                    "action": "install",
                    "args": "-DskipTests",
                    "working_directory": ROOT,
                },
                "purpose": "Produce the full reactor artifacts without entering the test lane.",
                "evidence_refs": [DEVNOTES.entry_id],
            }
        ],
        "build_success_criteria": [
            "The terminal Maven receipt reports every selected reactor module successful."
        ],
        "test_steps": [
            {
                "tool": "build",
                "params": {
                    "action": "test",
                    "args": "-Dtest=IgniteBasicTestSuite",
                    "working_directory": ROOT,
                },
                "purpose": "Run the documented bounded suite instead of an unfiltered root verify.",
                "evidence_refs": [DEVNOTES.entry_id],
            }
        ],
        "test_success_criteria": [
            "The selected suite has a terminal receipt and no failed or errored test cases."
        ],
        "environment_constraints": ["Use the repository Maven wrapper."],
        "risks": ["An unfiltered verify includes manual or long-running tests."],
        "unresolved_questions": ["Whether integration tests need a separate environment."],
    }


def planned_candidate():
    value = candidate()
    value["test_disposition"] = {
        "status": "planned",
        "reason": "DEVNOTES defines IgniteBasicTestSuite as an unattended product test lane.",
        "execution_mechanism": "Maven Surefire executes IgniteBasicTestSuite.",
        "verdict_scope": "product_test_cases",
        "readiness": "ready",
        "evidence_refs": [DEVNOTES.entry_id],
        "definition_evidence_refs": [DEVNOTES.entry_id],
    }
    return value


def blocked_candidate():
    value = candidate()
    value["test_steps"] = []
    value["test_disposition"] = {
        "status": "blocked",
        "reason": "The documented test lane requires manual network coordination.",
        "execution_mechanism": "A server and client must be launched separately.",
        "verdict_scope": "benchmark_or_manual",
        "readiness": "blocked",
        "evidence_refs": [DEVNOTES.entry_id],
        "definition_evidence_refs": [DEVNOTES.entry_id],
    }
    return value


def test_validate_authored_plan_normalizes_to_frozen_bounded_models():
    plan = validate_authored_plan(candidate())

    assert isinstance(plan, ProjectExecutionPlan)
    assert isinstance(plan.build_steps[0], ExecutionStep)
    assert plan.documents_reviewed[0].path == DEVNOTES_PATH
    assert plan.build_steps[0].params["action"] == "install"
    with pytest.raises(Exception):
        plan.summary = "changed"


def test_new_analyze_plans_require_an_explicit_test_disposition():
    with pytest.raises(ProjectExecutionPlanValidationError, match="test_disposition"):
        validate_authored_plan(candidate(), require_test_disposition=True)

    planned = validate_authored_plan(planned_candidate(), require_test_disposition=True)
    blocked = validate_authored_plan(blocked_candidate(), require_test_disposition=True)

    assert planned.test_disposition.status == "planned"
    assert blocked.test_disposition.status == "blocked"
    assert blocked.test_steps == ()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("readiness", "unknown", "readiness=ready"),
        ("verdict_scope", "quality_only", "verdict_scope=product_test_cases"),
    ],
)
def test_planned_test_disposition_requires_ready_product_cases(field, value, message):
    plan = planned_candidate()
    plan["test_disposition"][field] = value

    with pytest.raises(ProjectExecutionPlanValidationError, match=message):
        validate_authored_plan(plan, require_test_disposition=True)


def test_legacy_plan_digest_does_not_gain_a_null_disposition_field():
    plan = validate_authored_plan(candidate())
    legacy_payload = plan.model_dump(mode="json", exclude={"test_disposition"})

    assert canonical_authored_plan_sha256(plan) == canonical_sha256(legacy_payload)


def test_execution_steps_do_not_require_repeating_document_refs():
    value = candidate()
    value["build_steps"][0].pop("evidence_refs")
    value["test_steps"][0].pop("evidence_refs")

    plan = validate_authored_plan(value)

    assert plan.build_steps[0].evidence_refs == ()
    assert plan.test_steps[0].evidence_refs == ()


def test_test_steps_may_be_empty_when_analysis_cannot_establish_a_safe_entry():
    value = candidate()
    value["test_steps"] = []
    value["risks"] = ["No project-declared unattended test entry was established."]

    plan = validate_authored_plan(value)

    assert plan.test_steps == ()


@pytest.mark.parametrize(
    "missing",
    [
        "documents_reviewed",
        "build_steps",
        "build_success_criteria",
        "test_success_criteria",
    ],
)
def test_authored_plan_requires_documents_build_test_and_both_criteria(missing):
    value = candidate()
    value[missing] = []

    with pytest.raises(ProjectExecutionPlanValidationError):
        validate_authored_plan(value)


def test_authored_plan_rejects_extra_fields_and_unbounded_params():
    extra = candidate()
    extra["model_commentary"] = "not part of the plan contract"
    with pytest.raises(ProjectExecutionPlanValidationError):
        validate_authored_plan(extra)

    deep = candidate()
    deep["build_steps"][0]["params"] = {"a": {"b": {"c": {"d": {"e": 1}}}}}
    with pytest.raises(ProjectExecutionPlanValidationError):
        validate_authored_plan(deep)


def test_canonical_authored_hash_is_stable_across_mapping_order():
    original = candidate()
    reordered = dict(reversed(list(original.items())))
    reordered["build_steps"][0]["params"] = dict(
        reversed(list(reordered["build_steps"][0]["params"].items()))
    )

    assert canonical_authored_plan_sha256(original) == canonical_authored_plan_sha256(reordered)
    assert canonical_authored_plan_sha256(original) == canonical_authored_plan_sha256(
        validate_authored_plan(original)
    )


def test_inventory_binding_hints_do_not_change_the_model_strategy_hash():
    without_hints = candidate()
    with_hints = candidate()
    with_hints["documents_reviewed"][0].update(
        {"entry_id": "model-hint", "source_hash": "not-a-digest-hint"}
    )

    mapped = document_map(DEVNOTES)
    bound_without_hints = bind_project_execution_plan_inventory(without_hints, mapped)
    bound_with_hints = bind_project_execution_plan_inventory(with_hints, mapped)

    assert canonical_authored_plan_sha256(bound_with_hints) == canonical_authored_plan_sha256(
        bound_without_hints
    )


def test_seal_binds_attempt_claim_authored_plan_and_document_map():
    mapped = document_map(
        DEVNOTES,
        README,
        partial=[{"path": f"{ROOT}/target/README.md", "reason": "generated_tree"}],
    )

    artifact = seal_project_execution_plan(
        candidate(),
        source_attempt_id=ATTEMPT_ID,
        claim_sha256=CLAIM_SHA,
        document_map=mapped,
    )

    assert isinstance(artifact, SealedProjectExecutionPlan)
    assert artifact.source_attempt_id == ATTEMPT_ID
    assert artifact.claim_sha256 == CLAIM_SHA
    assert artifact.authored_plan_sha256 == canonical_authored_plan_sha256(artifact.plan)
    assert artifact.document_map_fingerprint == mapped["document_map_fingerprint"]
    assert artifact.plan.documents_reviewed[0].entry_id == DEVNOTES.entry_id
    assert artifact.plan.documents_reviewed[0].source_hash == DEVNOTES.source_hash
    assert artifact.inventory_coverage.indexed_documents == 2
    assert artifact.inventory_coverage.reviewed_indexed_documents == 1
    assert artifact.inventory_coverage.unreviewed_indexed_documents == 1
    assert artifact.inventory_coverage.partial_inventory_records == 1
    assert any("inventory_review_gap" in warning for warning in artifact.inventory_warnings)
    assert any("inventory_discovery_gap" in warning for warning in artifact.inventory_warnings)


@pytest.mark.parametrize("field", ["entry_id", "source_hash"])
def test_seal_corrects_optional_document_map_binding_hints(field):
    value = candidate()
    value["documents_reviewed"][0][field] = "f" * 64 if field == "source_hash" else "doc-wrong"

    artifact = seal_project_execution_plan(
        value,
        source_attempt_id=ATTEMPT_ID,
        claim_sha256=CLAIM_SHA,
        document_map=document_map(DEVNOTES),
    )

    reviewed = artifact.plan.documents_reviewed[0]
    assert reviewed.entry_id == DEVNOTES.entry_id
    assert reviewed.source_hash == DEVNOTES.source_hash
    assert any(
        "document_binding_hint_corrected" in warning and field in warning
        for warning in artifact.inventory_warnings
    )


def test_seal_rejects_a_tampered_document_map_fingerprint():
    mapped = document_map(DEVNOTES)
    mapped["document_map_fingerprint"] = "f" * 64

    with pytest.raises(ProjectExecutionPlanValidationError, match="fingerprint mismatch"):
        seal_project_execution_plan(
            candidate(),
            source_attempt_id=ATTEMPT_ID,
            claim_sha256=CLAIM_SHA,
            document_map=mapped,
        )


def test_map_external_document_with_direct_output_evidence_is_a_warning():
    value = candidate()
    value["documents_reviewed"] = [
        {
            "path": f"{ROOT}/PRIVATE-NOTES",
            "reason": "A targeted repository search found project-specific instructions.",
            "evidence_refs": ["output_abc123"],
            "entry_id": "stale-model-hint",
        }
    ]

    artifact = seal_project_execution_plan(
        value,
        source_attempt_id=ATTEMPT_ID,
        claim_sha256=CLAIM_SHA,
        document_map=document_map(DEVNOTES),
    )

    assert artifact.inventory_coverage.external_documents_reviewed == 1
    assert artifact.inventory_coverage.unreviewed_indexed_documents == 1
    assert artifact.plan.documents_reviewed[0].entry_id is None
    assert artifact.plan.documents_reviewed[0].source_hash is None
    assert any("external_document_reviewed" in warning for warning in artifact.inventory_warnings)
    assert any(
        "external_document_binding_hint_ignored" in warning
        for warning in artifact.inventory_warnings
    )


@pytest.mark.parametrize("evidence_ref", ["claim-123", f"file:{ROOT}/PRIVATE-NOTES"])
def test_map_external_document_without_output_evidence_is_rejected(evidence_ref):
    value = candidate()
    value["documents_reviewed"] = [
        {
            "path": f"{ROOT}/PRIVATE-NOTES",
            "reason": "Unbound prose.",
            "evidence_refs": [evidence_ref],
        }
    ]

    with pytest.raises(ProjectExecutionPlanValidationError, match="output evidence"):
        seal_project_execution_plan(
            value,
            source_attempt_id=ATTEMPT_ID,
            claim_sha256=CLAIM_SHA,
            document_map=document_map(DEVNOTES),
        )


def test_absent_document_map_allows_direct_evidence_and_records_the_gap():
    value = candidate()
    review = value["documents_reviewed"][0]
    review.pop("entry_id", None)
    review.pop("source_hash", None)
    review["evidence_refs"] = ["output_direct_read"]

    artifact = seal_project_execution_plan(
        value,
        source_attempt_id=ATTEMPT_ID,
        claim_sha256=CLAIM_SHA,
        document_map=None,
    )

    assert artifact.document_map_fingerprint is None
    assert artifact.inventory_coverage.external_documents_reviewed == 1
    assert any("document_map_unavailable" in warning for warning in artifact.inventory_warnings)


def _document_read_observation(
    ref,
    *,
    path=DEVNOTES_PATH,
    attempt_id=ATTEMPT_ID,
    tool_name="search",
    params=None,
    succeeded=True,
):
    return {
        "tool_name": tool_name,
        "params": params or {"target": f"file:{path}", "pattern": "."},
        "source_phase": "analyze",
        "source_attempt_id": attempt_id,
        "result": {"succeeded": succeeded, "output_ref": ref},
    }


def test_document_review_evidence_resolves_current_analyze_output_and_path():
    value = candidate()
    value["documents_reviewed"][0]["evidence_refs"] = ["output_real_read"]

    plan = validate_reviewed_document_evidence(
        value,
        observations=[_document_read_observation("output_real_read")],
        output_reader={"output_real_read": "document excerpt"}.get,
        source_attempt_id=ATTEMPT_ID,
    )

    assert plan.documents_reviewed[0].evidence_refs == ("output_real_read",)


def test_document_review_does_not_depend_on_tool_or_action_names():
    value = candidate()
    value["documents_reviewed"][0]["evidence_refs"] = ["output_future_reader"]
    observation = _document_read_observation(
        "output_future_reader",
        tool_name="future_document_facade",
        params={"operation": "open", "request": {"source": DEVNOTES_PATH}},
    )

    plan = validate_reviewed_document_evidence(
        value,
        observations=[observation],
        output_reader={"output_future_reader": DEVNOTES_TEXT}.get,
        source_attempt_id=ATTEMPT_ID,
    )

    assert plan.documents_reviewed[0].evidence_refs == ("output_future_reader",)


def test_document_review_rejects_a_search_observation_that_explicitly_did_not_match():
    value = candidate()
    value["documents_reviewed"][0]["evidence_refs"] = ["output_empty_search"]
    observation = _document_read_observation("output_empty_search")
    observation["result"]["facts"] = {"matched": False}

    with pytest.raises(ProjectExecutionPlanValidationError, match="current Analyze attempt"):
        validate_reviewed_document_evidence(
            value,
            observations=[observation],
            output_reader={"output_empty_search": "No matches found"}.get,
            source_attempt_id=ATTEMPT_ID,
        )


@pytest.mark.parametrize(
    ("evidence_refs", "observations", "outputs"),
    [
        ([f"file:{DEVNOTES_PATH}"], [], {}),
        (["output_missing"], [_document_read_observation("output_missing")], {}),
        (
            ["output_old_attempt"],
            [_document_read_observation("output_old_attempt", attempt_id="analyze-previous")],
            {"output_old_attempt": DEVNOTES_TEXT},
        ),
        (
            ["output_other_path"],
            [_document_read_observation("output_other_path", path=README_PATH)],
            {"output_other_path": "# Ignite"},
        ),
        (
            ["output_failed_read"],
            [_document_read_observation("output_failed_read", succeeded=False)],
            {"output_failed_read": "read failed"},
        ),
    ],
)
def test_document_review_rejects_unreadable_unbound_or_previous_attempt_refs(
    evidence_refs,
    observations,
    outputs,
):
    value = candidate()
    value["documents_reviewed"][0]["evidence_refs"] = evidence_refs

    with pytest.raises(ProjectExecutionPlanValidationError, match="current Analyze attempt"):
        validate_reviewed_document_evidence(
            value,
            observations=observations,
            output_reader=outputs.get,
            source_attempt_id=ATTEMPT_ID,
        )


def test_document_review_rejects_hash_only_output_without_a_path_bound_read():
    value = candidate()
    value["documents_reviewed"][0]["evidence_refs"] = ["output_exact_document"]
    value["documents_reviewed"][0]["source_hash"] = DEVNOTES_HASH
    observation = _document_read_observation(
        "output_exact_document",
        tool_name="advisor",
        params={"action": "consult"},
    )

    with pytest.raises(ProjectExecutionPlanValidationError, match="Exact valid.*none"):
        validate_reviewed_document_evidence(
            value,
            observations=[observation],
            output_reader={"output_exact_document": DEVNOTES_TEXT}.get,
            source_attempt_id=ATTEMPT_ID,
        )


def test_document_review_error_lists_exact_valid_refs_for_the_claimed_path():
    value = candidate()
    value["documents_reviewed"][0]["evidence_refs"] = ["output_guessed"]
    observations = [
        _document_read_observation("output_valid_devnotes"),
        _document_read_observation("output_other_document", path=README_PATH),
    ]

    with pytest.raises(ProjectExecutionPlanValidationError) as raised:
        validate_reviewed_document_evidence(
            value,
            observations=observations,
            output_reader={
                "output_valid_devnotes": DEVNOTES_TEXT,
                "output_other_document": README_TEXT,
            }.get,
            source_attempt_id=ATTEMPT_ID,
        )

    message = str(raised.value)
    assert "output_valid_devnotes" in message
    assert "output_guessed" in message
    assert "output_other_document" not in message
    assert len(message) <= 4_096


def test_document_review_binding_does_not_make_the_inventory_an_allowlist():
    external_path = f"{ROOT}/PRIVATE-NOTES"
    value = candidate()
    value["documents_reviewed"] = [
        {
            "path": external_path,
            "reason": "A targeted checkout read found project-specific instructions.",
            "evidence_refs": ["output_external_read"],
        }
    ]

    plan = validate_reviewed_document_evidence(
        value,
        observations=[_document_read_observation("output_external_read", path=external_path)],
        output_reader={"output_external_read": "Use ./projectw verify"}.get,
        source_attempt_id=ATTEMPT_ID,
    )

    assert plan.documents_reviewed[0].path == external_path


def test_atomic_write_and_strict_read_round_trip_the_sealed_artifact():
    fake = FakeContainer()
    artifact = seal_project_execution_plan(
        candidate(),
        source_attempt_id=ATTEMPT_ID,
        claim_sha256=CLAIM_SHA,
        document_map=document_map(DEVNOTES),
    )

    result = write_sealed_project_execution_plan(fake, artifact)
    loaded = read_sealed_project_execution_plan(fake)

    assert result.persisted
    assert loaded == artifact
    assert json.loads(fake.files[PROJECT_EXECUTION_PLAN_PATH])["artifact_sha256"] == (
        artifact.artifact_sha256
    )
    assert "test_disposition" not in json.loads(fake.files[PROJECT_EXECUTION_PLAN_PATH])["plan"]
    assert any("json.load" in command for command in fake.commands)
    assert not any(path.endswith(".tmp") for path in fake.files)


def test_strict_read_rejects_authored_or_artifact_tampering_and_extra_fields():
    fake = FakeContainer()
    artifact = seal_project_execution_plan(
        candidate(),
        source_attempt_id=ATTEMPT_ID,
        claim_sha256=CLAIM_SHA,
        document_map=document_map(DEVNOTES),
    )
    body = artifact.model_dump(mode="json")
    body["plan"]["summary"] = "changed after sealing"
    fake.files[PROJECT_EXECUTION_PLAN_PATH] = json.dumps(body)
    with pytest.raises(ProjectExecutionPlanReadError, match="hash mismatch"):
        read_sealed_project_execution_plan(fake)

    body = artifact.model_dump(mode="json")
    body["plan"]["documents_reviewed"][0]["entry_id"] = "doc-tampered"
    fake.files[PROJECT_EXECUTION_PLAN_PATH] = json.dumps(body)
    with pytest.raises(ProjectExecutionPlanReadError, match="hash mismatch"):
        read_sealed_project_execution_plan(fake)

    body = artifact.model_dump(mode="json")
    body["unexpected"] = True
    fake.files[PROJECT_EXECUTION_PLAN_PATH] = json.dumps(body)
    with pytest.raises(ProjectExecutionPlanReadError, match="extra"):
        read_sealed_project_execution_plan(fake)


def test_missing_artifact_reads_as_none():
    assert read_sealed_project_execution_plan(FakeContainer()) is None


def test_seal_and_write_is_engine_convenience_and_reports_transport_failure():
    fake = FakeContainer()

    artifact = seal_and_write_project_execution_plan(
        candidate(),
        fake,
        source_attempt_id=ATTEMPT_ID,
        claim_sha256=CLAIM_SHA,
        document_map=document_map(DEVNOTES),
    )

    assert read_sealed_project_execution_plan(fake) == artifact

    failing = FakeContainer(fail_on="mv -f --")
    with pytest.raises(ProjectExecutionPlanPersistenceError, match="transport_publish_failed"):
        seal_and_write_project_execution_plan(
            candidate(),
            failing,
            source_attempt_id=ATTEMPT_ID,
            claim_sha256=CLAIM_SHA,
            document_map=document_map(DEVNOTES),
        )


def test_forged_model_copy_is_revalidated_before_write():
    fake = FakeContainer()
    artifact = seal_project_execution_plan(
        candidate(),
        source_attempt_id=ATTEMPT_ID,
        claim_sha256=CLAIM_SHA,
        document_map=document_map(DEVNOTES),
    )
    forged = artifact.model_copy(update={"claim_sha256": "f" * 64})

    result = write_sealed_project_execution_plan(fake, forged)

    assert not result.persisted
    assert result.code == "invalid_arguments"
    assert PROJECT_EXECUTION_PLAN_PATH not in fake.files


def test_system_prompt_is_complete_bounded_and_carries_seal_and_plan():
    artifact = seal_project_execution_plan(
        candidate(),
        source_attempt_id=ATTEMPT_ID,
        claim_sha256=CLAIM_SHA,
        document_map=document_map(
            DEVNOTES,
            README,
            partial=[{"path": f"{ROOT}/deep/guide", "reason": "over_budget"}],
        ),
    )

    prompt = render_plan_system_prompt(artifact)

    assert len(prompt) <= MAX_SYSTEM_PROMPT_CHARS
    assert "SEALED PROJECT EXECUTION PLAN" in prompt
    assert artifact.authored_plan_sha256 in prompt
    assert '"build_steps"' in prompt
    assert '"test_success_criteria"' in prompt
    assert "inventory_review_gap" in prompt


def test_document_inventory_guidance_is_bounded_and_labels_hints_and_gaps():
    entries = [DEVNOTES]
    for index in range(100):
        path = f"{ROOT}/module-{index:03d}/unusual-{index}.zzz"
        entries.append(
            {
                "entry_id": entry_id(path),
                "path": path,
                "source_hash": hashlib.sha256(path.encode()).hexdigest(),
                "kind": "unknown",
            }
        )
    mapped = document_map(
        *entries,
        partial=[{"path": f"{ROOT}/beyond-depth/guide", "reason": "over_budget"}],
    )

    guidance = render_document_inventory_guidance(mapped, max_chars=900)

    assert len(guidance) <= 900
    assert "guidance, not a document allowlist" in guidance
    assert DEVNOTES_PATH in guidance
    assert "entry_id=" not in guidance
    assert "source_hash=" not in guidance
    assert "inventory lines omitted" in guidance
    assert len(render_document_inventory_guidance(mapped)) <= MAX_INVENTORY_PROMPT_CHARS


def test_invalid_artifact_mapping_returns_a_typed_write_failure():
    result = write_sealed_project_execution_plan(FakeContainer(), {"not": "an artifact"})

    assert not result.persisted
    assert result.code == "invalid_arguments"
