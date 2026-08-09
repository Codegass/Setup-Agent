# tests/test_survey_fingerprint_pin.py
"""The survey_fingerprint v2 pin is producible, self-verifying, and reachable.

Before this seam existed, `evidence_assessments.FINGERPRINT_KEYS` demanded a
`survey_fingerprint` pin on every model-sourced build contract while
`_validate_survey`'s exact-keys rejected the field and no producer stamped it
— so every live build dispatch assessed `contract_binding_unknown` and the
falsifier/expectation taxonomy was unreachable (plan Task 1.5: "Complete the
accepted survey_fingerprint v2 pin before D0").

The pin answers ONE question: which derived survey facts authorized this
dispatch. Inputs are already pinned by config_fingerprint / target_sha /
document_map_fingerprint; the fingerprint therefore covers the manifest body
MINUS the survey stamp (self-reference) and MINUS `module_structure`
(receipt-owned; a structure merge is not a survey change and must never flip
a contract binding).
"""

import json

import pytest

from build_requirements_fakes import complete_build_requirements_v1

from sag.tools.internal.build_preflight import (
    read_live_build_requirements,
    survey_facts_fingerprint,
    validate_build_requirements_v1,
    write_build_requirements,
)

RUN_ID = "run-20260809-fp"
SHA = "a" * 40


def valid_manifest(**overrides):
    return complete_build_requirements_v1(target_sha=SHA, **overrides)


# ---------------------------------------------------------------------------
# The fingerprint function itself
# ---------------------------------------------------------------------------


def test_fingerprint_is_deterministic_and_sha256_shaped():
    manifest = valid_manifest()
    first = survey_facts_fingerprint(manifest)
    second = survey_facts_fingerprint(json.loads(json.dumps(manifest)))
    assert first == second
    assert len(first) == 64 and set(first) <= set("0123456789abcdef")


def test_fingerprint_ignores_the_survey_stamp_itself():
    manifest = valid_manifest()
    restamped = dict(manifest, survey=dict(manifest["survey"], config_fingerprint="zz"))
    assert survey_facts_fingerprint(manifest) == survey_facts_fingerprint(restamped)


def test_fingerprint_ignores_a_receipt_proven_structure_merge():
    """Only a receipt may replace a receipt — and doing so is not a re-survey."""
    manifest = valid_manifest()
    merged = dict(manifest, module_structure={"modules": ["core"], "source": "receipt"})
    assert survey_facts_fingerprint(manifest) == survey_facts_fingerprint(merged)


def test_fingerprint_changes_when_a_derived_fact_changes():
    base = valid_manifest()
    moved = valid_manifest(build_root="/workspace/project/submodule",
                           root_shape="pathological_aggregator",
                           test_root="/workspace/project/submodule",
                           build_islands=[{"root": "/workspace/project/submodule",
                                          "system": "maven"}],
                           test_islands=[{"root": "/workspace/project/submodule",
                                         "system": "maven"}])
    assert survey_facts_fingerprint(base) != survey_facts_fingerprint(moved)


# ---------------------------------------------------------------------------
# The schema admits, requires, and VERIFIES the pin
# ---------------------------------------------------------------------------


def test_the_valid_manifest_fixture_carries_a_verified_pin():
    manifest = valid_manifest()
    validated = validate_build_requirements_v1(manifest)
    assert validated["survey"]["survey_fingerprint"] == survey_facts_fingerprint(manifest)


def test_a_missing_pin_is_refused_by_the_schema():
    manifest = valid_manifest()
    survey = dict(manifest["survey"])
    survey.pop("survey_fingerprint")
    with pytest.raises((TypeError, ValueError)):
        validate_build_requirements_v1(dict(manifest, survey=survey))


def test_a_forged_or_stale_pin_is_refused_not_republished():
    manifest = valid_manifest()
    forged = dict(manifest, survey=dict(manifest["survey"], survey_fingerprint="f" * 64))
    with pytest.raises((TypeError, ValueError)):
        validate_build_requirements_v1(forged)


# ---------------------------------------------------------------------------
# The one producer stamps it; the live reader returns it
# ---------------------------------------------------------------------------


def test_write_build_requirements_stamps_and_the_live_reader_returns_the_pin():
    from test_receipt_proven_structure import ManifestOrchestrator

    manifest = valid_manifest()
    survey = dict(manifest["survey"])
    survey.pop("survey_fingerprint")  # the producer, not the caller, owns the stamp
    unstamped = dict(manifest, survey=survey)

    orch = ManifestOrchestrator(None)
    assert write_build_requirements(orch, unstamped) is True

    live = read_live_build_requirements(orch)
    assert live.complete and live.payload is not None
    assert live.payload["survey"]["survey_fingerprint"] == survey_facts_fingerprint(
        live.payload
    )


# ---------------------------------------------------------------------------
# The assessor taxonomy is reachable end to end
# ---------------------------------------------------------------------------


def test_a_frozen_contract_carries_the_pin_and_binding_is_not_unknown():
    """The exact defect line: 'invocation contract has no current-authority
    survey_fingerprint pin'. With the producer complete, a contract frozen
    from a live manifest must carry the pin and the current side must state
    it, so the binding check proceeds past the availability guards."""
    from sag.agent.evidence_assessments import _current_contract_binding_problem

    manifest = validate_build_requirements_v1(valid_manifest())
    pin = manifest["survey"]["survey_fingerprint"]

    contract = {
        "intent_source": "model",
        "effective_tool": "build",
        "requested_call": {"tool": "build"},
        "target_sha": SHA,
        "survey_fingerprint": pin,
        "config_fingerprint": manifest["survey"]["config_fingerprint"] or "cfg",
        "document_map_fingerprint": "d" * 64,
        "domain_id": "/workspace/project",
        "fact_epoch": 3,
    }
    current = {
        "target_sha": SHA,
        "survey_fingerprint": pin,
        "config_fingerprint": contract["config_fingerprint"],
        "document_map_fingerprint": contract["document_map_fingerprint"],
        "domain_id": "/workspace/project",
        "fact_epoch": 3,
    }
    assert _current_contract_binding_problem(contract, current) == ""
    absent = dict(current)
    absent.pop("survey_fingerprint")
    assert "survey_fingerprint" in _current_contract_binding_problem(contract, absent)
