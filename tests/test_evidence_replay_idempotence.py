# tests/test_evidence_replay_idempotence.py
"""Assessment-aware evidence consumers and replay idempotence (Plan 6 Stage 0).

Spec §C4: a receipt is finalized once and then immutable, so semantic
classification is an append-only ``ReceiptAssessment`` instead of the
``mark_semantic_failure`` rewrite. The consumers therefore have to answer
"is this invocation semantically failed?" from TWO immutable sources — the
receipt's own exit fact and any failure-class assessment naming it — and they
have to answer it the same way every time they replay the same directories.

Lane z2 owns the readers only: lane z1 owns the writers, so every record here
is hand-written against the documented cross-lane shapes

    /workspace/.setup_agent/invocation_receipts/<receipt_id>.json     (v1, v2)
    /workspace/.setup_agent/evidence_assessments/<assessment_id>.json
        {assessment_id, receipt_id, typed_code, detail}

and the fake orchestrator serves them through the same ``cat <dir>/*.json``
glob production uses — which is what makes the ``.tmp`` (partially written)
case decidable rather than asserted.
"""

import contextlib
import glob
import hashlib
import importlib.util
import io
import json
import os
import shlex
from pathlib import Path

import pytest
from build_requirements_fakes import complete_build_requirements_v1
from container_evidence_fakes import (
    add_published_mutable_json,
    canonical_json,
    complete_receipt,
    strict_published_evidence,
)

from sag.agent.attempt_policy import CurrentBuildReceiptScope
from sag.agent.evidence_assessments import ReceiptAssessment, validate_assessment_v2
from sag.agent.evidence_publications import (
    BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
    evidence_publication_authority_for,
)
from sag.agent.evidence_records import (
    frame_json_record_stream,
    frame_named_json_record_stream,
)
from sag.agent.invocation_receipts import RECEIPT_DIR, build_receipt, validate_receipt_v2
from sag.agent.phase_gates import (
    ASSESSMENT_DIR,
    _domain_states,
    _gate_domain_states,
    check_phase_claim,
)
from sag.agent.phase_machine import PhaseClaim, PhaseOutcome
from sag.agent.physical_validator import PhysicalValidator
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from test_container_io import FakeContainer

WORKSPACE = "/workspace/bigtop"
PRODUCER = f"{WORKSPACE}/bigtop-data-generators"
CONSUMER = f"{WORKSPACE}/bigtop-bigpetstore/bigpetstore-spark"

BUILD_DOMAINS = [
    {"root": PRODUCER, "system": "gradle", "languages": ["java", "groovy"]},
    {"root": CONSUMER, "system": "gradle", "languages": ["scala"]},
]

NO_SOURCE_DETAIL = "compileJava reported NO-SOURCE — the compile did not cover the sources"


# ---------------------------------------------------------------------------
# Hand-written records (the cross-lane shapes, not writer output)
# ---------------------------------------------------------------------------
def receipt_v1(receipt_id, working_directory, outcome="completed", *, schema_version=1):
    """One schema-v1 receipt. ``schema_version=None`` omits the key entirely."""
    payload = {
        "receipt_id": receipt_id,
        "tool": "gradle",
        "requested_action": "compile",
        "effective_action": "build",
        "argv": "./gradlew build",
        "working_directory": working_directory,
        "exit_code": 0 if outcome == "completed" else 1,
        "outcome": outcome,
        "report_delta": {"new": [], "changed": []},
    }
    if schema_version is not None:
        payload["schema_version"] = schema_version
    return payload


def receipt_v2(receipt_id, working_directory, outcome="completed"):
    """A v2 receipt: every v1 key byte-identical, plus the Stage 0 additions."""
    payload = receipt_v1(receipt_id, working_directory, outcome, schema_version=2)
    payload.update(
        {
            "run_id": "run-current",
            "target_sha": "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c",
            "survey_fingerprint": "survey-7f3a",
            "config_fingerprint": "config-11b2",
            "domain_id": working_directory,
            "actual_cwd": working_directory,
            "compliance": "exact",
            "toolchain_fingerprint": "/usr/bin/gradle 8.5",
            "output_content_hash": "e3b0c44298fc1c149afbf4c8996fb924",
            "testcase_outcomes": [],
        }
    )
    return complete_receipt(payload)


def assessment(assessment_id, receipt_id, typed_code, detail=NO_SOURCE_DETAIL):
    del assessment_id
    return ReceiptAssessment(
        receipt_id=receipt_id,
        typed_code=typed_code,
        detail=detail,
    ).payload()


# ---------------------------------------------------------------------------
# Fake orchestrator: real directories, real glob, production command strings
# ---------------------------------------------------------------------------
class EvidenceOrch:
    """Serves the manifest, the receipt dir and the assessment dir.

    ``cat <dir>/*.json`` is answered by globbing a real temp directory, so a
    ``<id>.json.tmp`` file is excluded by the SAME rule that excludes it in the
    container instead of by a hand-written special case.
    """

    def __init__(self, tmp_path, *, domains=BUILD_DOMAINS, edges=(), run_pin=None):
        # Live derivation reads only the strict host-published v1 record, so
        # the fixture states the complete schema: the aggregate root builds
        # from PRODUCER (a pathological aggregator) and islands mirror domains.
        overrides = {}
        if domains is not None:
            overrides["build_islands"] = [
                {"root": domain["root"], "system": domain["system"]} for domain in domains
            ]
            overrides["build_domains"] = [dict(domain) for domain in domains]
            if edges is not None:
                overrides["domain_edges"] = list(edges)
        self.manifest = complete_build_requirements_v1(
            project_root=WORKSPACE,
            build_system="gradle",
            root_shape="pathological_aggregator",
            build_root=PRODUCER,
            test_root=PRODUCER,
            **overrides,
        )
        self.receipts_dir = Path(tmp_path) / "invocation_receipts"
        self.assessments_dir = Path(tmp_path) / "evidence_assessments"
        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        self.assessments_dir.mkdir(parents=True, exist_ok=True)
        self.commands = []
        self.evidence = strict_published_evidence(
            self,
            run_id="run-current",
            target_sha="a" * 40,
            run_pin=False if run_pin is None else run_pin,
        )
        self.manifest_raw = add_published_mutable_json(
            self,
            self.evidence,
            path=REQUIREMENTS_PATH,
            record_kind="build_requirements",
            record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            payload=self.manifest,
        )

    # -- records ---------------------------------------------------------
    def write_receipt(self, payload):
        candidate = dict(payload)
        publish = candidate.get("schema_version") == 2
        if publish:
            candidate = complete_receipt(candidate)
        path = self._write(
            self.receipts_dir,
            f"{candidate['receipt_id']}.json",
            candidate,
        )
        raw = path.read_bytes()
        if publish and candidate.get("run_id") == "run-current":
            evidence_publication_authority_for(self).publish_bytes(
                record_kind="invocation_receipt",
                record_id=str(candidate["receipt_id"]),
                raw=raw,
            )
        return path

    def write_assessment(self, payload):
        try:
            candidate = validate_assessment_v2(payload)
        except (TypeError, ValueError):
            return self._write(
                self.assessments_dir,
                f"{payload['assessment_id']}.json",
                payload,
            )
        path = self._write(
            self.assessments_dir,
            f"{candidate['assessment_id']}.json",
            candidate,
        )
        evidence_publication_authority_for(self).publish_bytes(
            record_kind="receipt_assessment",
            record_id=str(candidate["assessment_id"]),
            raw=path.read_bytes(),
        )
        return path

    def write_partial_assessment(self, payload):
        """The atomic writer's temp file, before the final ``mv``."""
        return self._write(self.assessments_dir, f"{payload['assessment_id']}.json.tmp", payload)

    @staticmethod
    def _write(directory, name, payload):
        path = directory / name
        # write_receipt persists ONE line per file; the readers depend on it.
        path.write_text(canonical_json(payload), encoding="utf-8")
        return path

    # -- transport -------------------------------------------------------
    def execute_command(self, command, workdir=None, timeout=None, truncate_output=None):
        self.commands.append(command)
        if "job_obligations" in command:
            return {
                "success": True,
                "exit_code": 0,
                "output": (
                    frame_named_json_record_stream([])
                    if "SAG_NAMED_JSON_RECORD_V1" in command
                    else frame_json_record_stream([])
                ),
            }
        if "run-pin.json" in command:
            return self.evidence(command)
        if REQUIREMENTS_PATH in command:
            if "SAG_NAMED_JSON_RECORD_V1" in command:
                return self.evidence(command)
            return {"success": True, "exit_code": 0, "output": self.manifest_raw}
        if ASSESSMENT_DIR in command:
            return self._cat(self.assessments_dir, command=command)
        if RECEIPT_DIR in command:
            return self._cat(self.receipts_dir, command=command)
        return {"success": True, "exit_code": 0, "output": ""}

    @staticmethod
    def _cat(directory, *, command=""):
        records = [
            (Path(path).name, Path(path).read_text(encoding="utf-8"))
            for path in sorted(glob.glob(os.path.join(str(directory), "*.json")))
        ]
        return {
            "success": True,
            "exit_code": 0,
            "output": (
                frame_named_json_record_stream(records)
                if "SAG_NAMED_JSON_RECORD_V1" in command
                else frame_json_record_stream(body for _name, body in records)
            ),
        }


class GreenValidator:
    """The physical oracle says green — the pressure the derivation must resist."""

    def validate_build_status(self, project_name=None):
        return {
            "success": True,
            "build_complete": True,
            "evidence_status": "success",
            "evidence": {"build_system": "gradle", "has_artifacts": True, "class_count": 12},
            "reason": "artifacts present under the project root",
        }

    def validate_test_status(self, project_name=None):
        return {
            "has_test_reports": True,
            "evidence_status": "success",
            "reason": "test reports present",
            "test_stats": {"executed": 3, "passed": 3, "failed": 0, "errors": 0, "skipped": 0},
            "total_tests": 3,
            "unique_tests": 3,
            "unique_passed_tests": 3,
        }


def states_of(orch):
    derived = _gate_domain_states(orch)
    return {root: entry["state"] for root, entry in (derived.states or {}).items()}


def current_scope(*, run_id="run-current", target_sha="a" * 40):
    return CurrentBuildReceiptScope(
        status="available",
        run_id=run_id,
        target_sha=target_sha,
        project_root=WORKSPACE,
    )


def scoped_receipt(receipt_id, working_directory, outcome="completed", **pins):
    payload = receipt_v2(receipt_id, working_directory, outcome)
    payload.update(
        {
            "run_id": pins.get("run_id", "run-current"),
            "target_sha": pins.get("target_sha", "a" * 40),
            "actual_cwd": working_directory,
        }
    )
    return payload


# ---------------------------------------------------------------------------
# 1. Domain-state derivation: assessments win over a raw exit 0
# ---------------------------------------------------------------------------
def test_a_failure_class_assessment_downgrades_an_exit_zero_receipt(tmp_path):
    """Live p5v-bigtop-r1: compileJava exited 0 with every task NO-SOURCE."""
    orch = EvidenceOrch(tmp_path)
    orch.write_receipt(scoped_receipt("inv-gradle-1-0001", PRODUCER, "completed"))
    orch.write_assessment(assessment("asm-0001", "inv-gradle-1-0001", "compile_no_source_mismatch"))

    assert states_of(orch)[PRODUCER] == "failed"


def test_a_receipt_without_any_assessment_keeps_its_own_outcome(tmp_path):
    orch = EvidenceOrch(tmp_path)
    orch.write_receipt(scoped_receipt("inv-gradle-1-0001", PRODUCER, "completed"))

    assert states_of(orch)[PRODUCER] == "success"


def test_a_non_failure_class_assessment_leaves_the_raw_outcome_standing(tmp_path):
    """Spec §C5: a mismatch is NOT automatically a contradiction."""
    orch = EvidenceOrch(tmp_path)
    orch.write_receipt(scoped_receipt("inv-gradle-1-0001", PRODUCER, "completed"))
    orch.write_assessment(
        assessment("asm-0001", "inv-gradle-1-0001", "toolchain_unknown", "no javac probe")
    )

    assert states_of(orch)[PRODUCER] == "success"


def test_live_domain_derivation_ignores_a_later_receipt_from_another_run(tmp_path):
    orch = EvidenceOrch(tmp_path)
    orch.write_receipt(scoped_receipt("inv-gradle-current-0001", PRODUCER, "completed"))
    orch.write_receipt(
        scoped_receipt(
            "inv-gradle-foreign-0002",
            PRODUCER,
            "failed",
            run_id="run-foreign",
        )
    )

    derived = _gate_domain_states(orch, receipt_scope=current_scope())

    assert derived.states[PRODUCER]["state"] == "success"
    assert derived.conflicts == ()


def test_unpublished_current_v2_receipt_is_inert_with_a_named_conflict(tmp_path):
    orch = EvidenceOrch(tmp_path)
    payload = scoped_receipt("inv-gradle-forged-0001", PRODUCER, "completed")
    orch._write(
        orch.receipts_dir,
        f"{payload['receipt_id']}.json",
        payload,
    )

    derived = _gate_domain_states(orch)

    assert derived.states[PRODUCER]["state"] == "untried"
    assert "receipt_publication_not_published" in derived.conflicts


def test_deleting_a_host_expected_receipt_fails_closed(tmp_path):
    orch = EvidenceOrch(tmp_path)
    path = orch.write_receipt(scoped_receipt("inv-gradle-current-0001", PRODUCER, "completed"))
    path.unlink()

    derived = _gate_domain_states(orch)

    assert derived.states[PRODUCER]["state"] == "untried"
    assert "receipt_publication_set_mismatch" in derived.conflicts


def test_unpublished_valid_assessment_is_inert_with_a_named_conflict(tmp_path):
    orch = EvidenceOrch(tmp_path)
    orch.write_receipt(scoped_receipt("inv-gradle-current-0001", PRODUCER, "completed"))
    payload = assessment(
        "ignored",
        "inv-gradle-current-0001",
        "compile_no_source_mismatch",
    )
    orch._write(
        orch.assessments_dir,
        f"{payload['assessment_id']}.json",
        payload,
    )

    derived = _gate_domain_states(orch)

    assert derived.states[PRODUCER]["state"] == "success"
    assert "assessment_publication_not_published" in derived.conflicts


def test_same_run_wrong_target_and_its_assessment_are_historical_not_current_conflicts(
    tmp_path,
):
    orch = EvidenceOrch(tmp_path)
    foreign = scoped_receipt(
        "inv-gradle-wrong-target-0001",
        PRODUCER,
        "completed",
        target_sha="b" * 40,
    )
    orch.write_receipt(foreign)
    orch.write_assessment(
        assessment(
            "asm-foreign-target",
            foreign["receipt_id"],
            "compile_no_source_mismatch",
        )
    )

    derived = _gate_domain_states(orch, receipt_scope=current_scope())

    assert derived.states[PRODUCER]["state"] == "untried"
    assert "assessment_receipt_missing" not in derived.conflicts


def test_domain_credit_uses_the_receipts_actual_cwd_not_the_requested_directory(tmp_path):
    orch = EvidenceOrch(tmp_path)
    payload = scoped_receipt("inv-gradle-current-0001", PRODUCER, "completed")
    payload["actual_cwd"] = CONSUMER
    payload["domain_id"] = CONSUMER
    orch.write_receipt(payload)

    derived = _gate_domain_states(orch, receipt_scope=current_scope())

    assert derived.states[PRODUCER]["state"] == "untried"
    assert derived.states[CONSUMER]["state"] == "success"


def test_unavailable_live_receipt_scope_cannot_promote_a_domain(tmp_path):
    orch = EvidenceOrch(tmp_path)
    orch.write_receipt(scoped_receipt("inv-gradle-current-0001", PRODUCER))
    unavailable = CurrentBuildReceiptScope(
        status="run_pin_unreadable",
        run_id="run-current",
        project_root=WORKSPACE,
    )

    derived = _gate_domain_states(orch, receipt_scope=unavailable)

    assert derived.states[PRODUCER]["state"] == "untried"
    assert "receipt_binding_run_pin_unreadable" in derived.conflicts


def test_live_build_gate_binds_domain_receipts_to_the_validators_run_pin(tmp_path):
    orch = EvidenceOrch(
        tmp_path,
        run_pin={"run_id": "run-current", "target_repo_sha": "a" * 40},
    )
    orch.write_receipt(
        scoped_receipt(
            "inv-gradle-old-run-0001",
            PRODUCER,
            "completed",
            run_id="run-old",
        )
    )
    validator = GreenValidator()
    validator.receipt_run_id = "run-current"
    validator.project_path = "/workspace"

    gate = check_phase_claim(
        "build",
        PhaseClaim(phase="build", claimed_outcome=PhaseOutcome.PARTIAL),
        validator=validator,
        orchestrator=orch,
        project_name=None,
    )

    assert gate.validated_facts["build.domain_states"][PRODUCER]["state"] == "untried"


def test_live_test_gate_resolves_the_same_manifest_root_before_binding_receipts(tmp_path):
    orch = EvidenceOrch(
        tmp_path,
        run_pin={"run_id": "run-current", "target_repo_sha": "a" * 40},
    )
    orch.write_receipt(scoped_receipt("inv-gradle-current-0001", PRODUCER, "completed"))
    validator = GreenValidator()
    validator.receipt_run_id = "run-current"
    validator.project_path = "/workspace"

    gate = check_phase_claim(
        "test",
        PhaseClaim(phase="test", claimed_outcome=PhaseOutcome.PARTIAL),
        validator=validator,
        orchestrator=orch,
        project_name=None,
    )

    states = gate.validated_facts["test.stats"]["domain_states"]
    assert states[PRODUCER]["state"] == "success"
    assert states[CONSUMER]["state"] == "untried"
    assert (
        "receipt_binding_project_root_missing"
        not in gate.validated_facts["test.stats"]["conflicts"]
    )


def test_an_assessment_can_never_promote_a_failed_receipt(tmp_path):
    orch = EvidenceOrch(tmp_path)
    orch.write_receipt(scoped_receipt("inv-gradle-1-0001", PRODUCER, "failed"))
    orch.write_assessment(
        assessment("asm-0001", "inv-gradle-1-0001", "toolchain_unknown", "no javac probe")
    )

    assert states_of(orch)[PRODUCER] == "failed"


def test_the_latest_receipt_still_decides_and_carries_its_own_assessment(tmp_path):
    """Retry semantics are unchanged: the assessment binds to ITS receipt."""
    orch = EvidenceOrch(tmp_path)
    orch.write_receipt(scoped_receipt("inv-gradle-1-0001", PRODUCER, "failed"))
    orch.write_receipt(scoped_receipt("inv-gradle-2-0002", PRODUCER, "completed"))
    orch.write_assessment(assessment("asm-0001", "inv-gradle-1-0001", "compile_no_source_mismatch"))

    # The condemned receipt is the SUPERSEDED one; attempt 2 stands.
    assert states_of(orch)[PRODUCER] == "success"

    orch.write_assessment(assessment("asm-0002", "inv-gradle-2-0002", "compile_no_source_mismatch"))
    assert states_of(orch)[PRODUCER] == "failed"


def test_an_assessment_binds_to_one_receipt_not_to_every_receipt_at_the_root(tmp_path):
    orch = EvidenceOrch(tmp_path)
    orch.write_receipt(scoped_receipt("inv-gradle-1-0001", PRODUCER, "completed"))
    orch.write_receipt(scoped_receipt("inv-gradle-1-0002", CONSUMER, "completed"))
    orch.write_assessment(assessment("asm-0001", "inv-gradle-1-0001", "compile_no_source_mismatch"))

    assert states_of(orch) == {PRODUCER: "failed", CONSUMER: "success"}


def test_the_build_gate_publishes_the_assessment_aware_states(tmp_path):
    """End to end: exit 0 plus a green oracle cannot close an assessed domain."""
    orch = EvidenceOrch(tmp_path)
    orch.write_receipt(scoped_receipt("inv-gradle-1-0001", PRODUCER, "completed"))
    orch.write_receipt(scoped_receipt("inv-gradle-1-0002", CONSUMER, "completed"))
    orch.write_assessment(assessment("asm-0001", "inv-gradle-1-0001", "compile_no_source_mismatch"))

    gate = check_phase_claim(
        "build",
        PhaseClaim(phase="build", claimed_outcome=PhaseOutcome.PARTIAL),
        validator=GreenValidator(),
        orchestrator=orch,
        project_name=None,
    )

    assert gate.validated_facts["build.domain_states"][PRODUCER]["state"] == "failed"
    assert gate.validated_outcome is PhaseOutcome.PARTIAL


# ---------------------------------------------------------------------------
# 2. Version-gated receipt reading: v1 and v2, nothing coerced
# ---------------------------------------------------------------------------
def test_a_v1_receipt_is_forensic_while_current_v2_is_live(tmp_path):
    v1 = EvidenceOrch(tmp_path / "v1")
    v1.write_receipt(receipt_v1("inv-gradle-1-0001", PRODUCER, "completed"))
    v2 = EvidenceOrch(tmp_path / "v2")
    v2.write_receipt(receipt_v2("inv-gradle-1-0001", PRODUCER, "completed"))

    assert states_of(v1)[PRODUCER] == "untried"
    assert states_of(v2)[PRODUCER] == "success"


def test_a_receipt_without_a_schema_version_key_is_forensic(tmp_path):
    orch = EvidenceOrch(tmp_path)
    orch.write_receipt(receipt_v1("inv-gradle-1-0001", PRODUCER, schema_version=None))

    assert states_of(orch)[PRODUCER] == "untried"


def test_an_unknown_future_schema_version_is_skipped_with_a_named_conflict(tmp_path):
    orch = EvidenceOrch(tmp_path)
    orch.write_receipt(receipt_v1("inv-gradle-1-0001", PRODUCER, schema_version=3))
    orch.write_receipt(receipt_v1("inv-gradle-1-0002", CONSUMER, "completed"))

    derived = _gate_domain_states(orch)

    # A future receipt may be the later failure for any root.  Until its schema
    # is understood, no readable prefix may retain a green domain.
    assert derived.states[PRODUCER]["state"] == "untried"
    assert derived.states[CONSUMER]["state"] == "untried"
    assert "receipt_record_schema_invalid" in derived.conflicts


def test_a_future_receipt_that_renamed_its_keys_is_still_named(tmp_path):
    """The version is read BEFORE the payload's keys, so a v3 that renamed
    ``working_directory`` conflicts loudly instead of vanishing."""
    orch = EvidenceOrch(tmp_path)
    payload = receipt_v1("inv-gradle-1-0001", PRODUCER, schema_version=3)
    payload["actual_working_directory"] = payload.pop("working_directory")
    orch.write_receipt(payload)

    assert "receipt_record_schema_invalid" in _gate_domain_states(orch).conflicts


def test_a_v1_receipt_without_a_working_directory_is_absent_not_a_conflict(tmp_path):
    """Unchanged pre-Plan-6 behaviour: an unattributable v1 receipt is silent."""
    orch = EvidenceOrch(tmp_path)
    payload = receipt_v1("inv-gradle-1-0001", PRODUCER)
    payload.pop("working_directory")
    orch.write_receipt(payload)

    derived = _gate_domain_states(orch)

    assert derived.states[PRODUCER]["state"] == "untried"
    assert derived.conflicts == ()


def test_an_unpublished_future_assessment_is_inert_with_a_named_conflict(tmp_path):
    orch = EvidenceOrch(tmp_path)
    orch.write_receipt(scoped_receipt("inv-gradle-1-0001", PRODUCER, "completed"))
    payload = assessment("asm-0001", "inv-gradle-1-0001", "compile_no_source_mismatch")
    payload["schema_version"] = 9
    orch.write_assessment(payload)

    derived = _gate_domain_states(orch)

    assert derived.states[PRODUCER]["state"] == "success"
    assert "assessment_record_schema_invalid" in derived.conflicts


def test_an_assessment_for_a_missing_receipt_is_a_named_conflict_not_a_crash(tmp_path):
    orch = EvidenceOrch(tmp_path)
    orch.write_receipt(scoped_receipt("inv-gradle-1-0001", PRODUCER, "completed"))
    orch.write_assessment(assessment("asm-0001", "inv-gradle-9-9999", "compile_no_source_mismatch"))

    derived = _gate_domain_states(orch)

    assert derived.states[PRODUCER]["state"] == "success"
    assert "assessment_receipt_missing" in derived.conflicts


def test_derivation_conflicts_reach_the_build_gate_as_a_named_fact(tmp_path):
    orch = EvidenceOrch(tmp_path)
    orch.write_receipt(receipt_v1("inv-gradle-1-0001", PRODUCER, schema_version=3))

    gate = check_phase_claim(
        "build",
        PhaseClaim(phase="build", claimed_outcome=PhaseOutcome.PARTIAL),
        validator=GreenValidator(),
        orchestrator=orch,
        project_name=None,
    )

    assert gate.validated_facts["build.evidence_conflicts"] == ["receipt_record_schema_invalid"]


def test_a_clean_run_publishes_no_evidence_conflicts_key(tmp_path):
    """Absent facts stay absent keys — recorded fixtures serialize unchanged."""
    orch = EvidenceOrch(tmp_path)
    orch.write_receipt(scoped_receipt("inv-gradle-1-0001", PRODUCER, "completed"))

    gate = check_phase_claim(
        "build",
        PhaseClaim(phase="build", claimed_outcome=PhaseOutcome.PARTIAL),
        validator=GreenValidator(),
        orchestrator=orch,
        project_name=None,
    )

    assert "build.evidence_conflicts" not in gate.validated_facts


OMISSION = {
    "field": "testcase_execution_rows",
    "status": "unavailable",
    "reasons": ["receipt testcase_execution_rows.rows is invalid"],
}


def test_a_receipt_that_dropped_an_evidence_field_says_so_at_the_gate(tmp_path):
    """The receipt's own declaration finally reaches a reader.

    `build_receipt` drops an unrepresentable observability field alone and
    records WHY on the receipt — and nothing read it back, so a rollup built
    from a receipt with no testcase rows looked exactly like a rollup built
    from a dispatch that ran no tests.
    """
    orch = EvidenceOrch(tmp_path)
    orch.write_receipt(
        {
            **scoped_receipt("inv-gradle-1-0001", PRODUCER, "completed"),
            "evidence_omissions": [OMISSION],
        }
    )

    gate = check_phase_claim(
        "build",
        PhaseClaim(phase="build", claimed_outcome=PhaseOutcome.PARTIAL),
        validator=GreenValidator(),
        orchestrator=orch,
        project_name=None,
    )

    assert gate.validated_facts["build.evidence_omissions"] == [
        "inv-gradle-1-0001:testcase_execution_rows"
    ]
    # An omission is not a conflict: the receipt is intact and readable, and it
    # states the hole itself.
    assert "build.evidence_conflicts" not in gate.validated_facts


def test_a_run_whose_receipts_omitted_nothing_publishes_no_omission_key(tmp_path):
    orch = EvidenceOrch(tmp_path)
    orch.write_receipt(scoped_receipt("inv-gradle-1-0001", PRODUCER, "completed"))

    gate = check_phase_claim(
        "build",
        PhaseClaim(phase="build", claimed_outcome=PhaseOutcome.PARTIAL),
        validator=GreenValidator(),
        orchestrator=orch,
        project_name=None,
    )

    assert "build.evidence_omissions" not in gate.validated_facts


def test_a_single_domain_project_still_names_its_evidence_omissions(tmp_path):
    """cli/tvm survey no domains; a dropped evidence field is still a fact."""
    orch = EvidenceOrch(tmp_path, domains=None)
    orch.write_receipt(
        {
            **scoped_receipt("inv-python-1-0001", PRODUCER, "completed"),
            "evidence_omissions": [OMISSION],
        }
    )

    derived = _gate_domain_states(orch)

    assert derived.states is None
    assert derived.omissions == ("inv-python-1-0001:testcase_execution_rows",)


def test_a_single_domain_project_still_names_its_evidence_conflicts(tmp_path):
    """cli/tvm survey no domains, but an unreadable receipt is still a fact."""
    orch = EvidenceOrch(tmp_path, domains=None)
    orch.write_receipt(receipt_v1("inv-python-1-0001", PRODUCER, schema_version=4))

    derived = _gate_domain_states(orch)

    assert derived.states is None
    assert "receipt_record_schema_invalid" in derived.conflicts


# ---------------------------------------------------------------------------
# 3. Replay and idempotence
# ---------------------------------------------------------------------------
def test_two_reads_of_the_same_directories_are_byte_identical(tmp_path):
    orch = EvidenceOrch(tmp_path)
    orch.write_receipt(scoped_receipt("inv-gradle-1-0001", PRODUCER, "completed"))
    orch.write_receipt(receipt_v2("inv-gradle-1-0002", CONSUMER, "completed"))
    orch.write_assessment(assessment("asm-0001", "inv-gradle-1-0001", "compile_no_source_mismatch"))

    first = _gate_domain_states(orch)
    second = _gate_domain_states(orch)

    assert json.dumps(first.states, sort_keys=True) == json.dumps(second.states, sort_keys=True)
    assert first.conflicts == second.conflicts


def test_a_partially_written_assessment_is_treated_as_absent(tmp_path):
    """Temp file present, final absent: the transition has not happened yet."""
    orch = EvidenceOrch(tmp_path)
    orch.write_receipt(scoped_receipt("inv-gradle-1-0001", PRODUCER, "completed"))
    orch.write_partial_assessment(
        assessment("asm-0001", "inv-gradle-1-0001", "compile_no_source_mismatch")
    )

    derived = _gate_domain_states(orch)

    assert derived.states[PRODUCER]["state"] == "success"
    assert derived.conflicts == ()


def test_the_final_assessment_landing_completes_the_transition(tmp_path):
    """The same temp file, once renamed, IS the transition (boundary check)."""
    orch = EvidenceOrch(tmp_path)
    orch.write_receipt(scoped_receipt("inv-gradle-1-0001", PRODUCER, "completed"))
    payload = assessment("asm-0001", "inv-gradle-1-0001", "compile_no_source_mismatch")
    orch.write_partial_assessment(payload)
    assert states_of(orch)[PRODUCER] == "success"

    orch.write_assessment(payload)

    assert states_of(orch)[PRODUCER] == "failed"


def test_double_ingesting_the_same_assessment_is_one_transition(tmp_path):
    """Append-only storage may hold the same verdict twice; state is a set."""
    orch = EvidenceOrch(tmp_path)
    orch.write_receipt(scoped_receipt("inv-gradle-1-0001", PRODUCER, "completed"))
    once = assessment("asm-0001", "inv-gradle-1-0001", "compile_no_source_mismatch")
    twice = assessment("asm-0002", "inv-gradle-1-0001", "compile_no_source_mismatch")

    orch.write_assessment(once)
    single = _gate_domain_states(orch)
    orch.write_assessment(twice)
    double = _gate_domain_states(orch)

    assert json.dumps(single.states, sort_keys=True) == json.dumps(double.states, sort_keys=True)
    assert single.conflicts == double.conflicts


def test_a_repeated_missing_receipt_reference_conflicts_once(tmp_path):
    orch = EvidenceOrch(tmp_path)
    orch.write_receipt(scoped_receipt("inv-gradle-1-0001", PRODUCER, "completed"))
    orch.write_assessment(assessment("asm-0001", "inv-gradle-9-9999", "compile_no_source_mismatch"))
    orch.write_assessment(assessment("asm-0002", "inv-gradle-9-9999", "compile_no_source_mismatch"))

    conflicts = _gate_domain_states(orch).conflicts

    assert list(conflicts).count("assessment_receipt_missing") == 1


def test_a_malformed_durable_receipt_prevents_a_readable_prefix_from_turning_green(
    tmp_path,
):
    orch = EvidenceOrch(tmp_path)
    orch.write_receipt(scoped_receipt("inv-gradle-1-0001", PRODUCER, "completed"))
    (orch.receipts_dir / "inv-gradle-1-0002.json").write_text(
        '{"receipt_id":"inv-gradle-1-0002","outcome":"failed",',
        encoding="utf-8",
    )

    derived = _gate_domain_states(orch)

    assert derived.states[PRODUCER]["state"] == "untried"
    assert "receipt_record_malformed" in derived.conflicts


def test_a_duplicate_key_receipt_prevents_a_readable_prefix_from_turning_green(tmp_path):
    orch = EvidenceOrch(tmp_path)
    orch.write_receipt(receipt_v1("inv-gradle-1-0001", PRODUCER, "completed"))
    (orch.receipts_dir / "inv-gradle-1-0002.json").write_text(
        (
            '{"schema_version":1,"receipt_id":"inv-gradle-1-0002",'
            f'"working_directory":"{PRODUCER}","outcome":"completed",'
            '"outcome":"failed"}'
        ),
        encoding="utf-8",
    )

    derived = _gate_domain_states(orch)

    assert derived.states[PRODUCER]["state"] == "untried"
    assert "receipt_record_malformed" in derived.conflicts


def test_reformatting_a_published_receipt_fails_exact_byte_authority(tmp_path):
    orch = EvidenceOrch(tmp_path)
    payload = scoped_receipt("inv-gradle-1-0001", PRODUCER, "completed")
    orch.write_receipt(payload)
    (orch.receipts_dir / "inv-gradle-1-0001.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )

    derived = _gate_domain_states(orch)

    assert "receipt_publication_mismatch" in derived.conflicts
    assert derived.states[PRODUCER]["state"] == "untried"


def test_an_unreadable_assessment_stream_cannot_leave_a_receipt_green(tmp_path):
    class AssessmentReadFails(EvidenceOrch):
        def execute_command(self, command, **kwargs):
            if ASSESSMENT_DIR in command:
                return {"success": False, "exit_code": 70, "output": "partial transport"}
            return super().execute_command(command, **kwargs)

    orch = AssessmentReadFails(tmp_path)
    orch.write_receipt(receipt_v1("inv-gradle-1-0001", PRODUCER, "completed"))

    derived = _gate_domain_states(orch)

    assert derived.states[PRODUCER]["state"] == "untried"
    assert "assessment_stream_unreadable" in derived.conflicts


def test_the_pure_derivation_is_order_independent(tmp_path):
    """Directory listing order is transport, never evidence."""
    receipts = [
        receipt_v1("inv-gradle-1-0001", PRODUCER, "completed"),
        receipt_v2("inv-gradle-1-0002", CONSUMER, "completed"),
    ]
    assessments = [
        assessment("asm-0001", "inv-gradle-1-0001", "compile_no_source_mismatch"),
    ]
    manifest = {"build_domains": BUILD_DOMAINS}

    forward = _domain_states(manifest, receipts, assessments)
    backward = _domain_states(manifest, list(reversed(receipts)), assessments)

    assert forward.states == backward.states


# ---------------------------------------------------------------------------
# 4. Fail-closed: a corrupt assessment blocks closure like a corrupt receipt
# ---------------------------------------------------------------------------
def _surefire_xml(classname, names):
    cases = "".join(
        f'<testcase classname="{classname}" name="{name}" time="0.01"/>' for name in names
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<testsuite name="{classname}" tests="{len(list(names))}" failures="0" '
        f'errors="0" skipped="0">{cases}</testsuite>'
    )


class ParserWorkspace:
    """A workspace the in-container compact parser can be run against locally."""

    def __init__(self, tmp_path):
        self.workspace = Path(tmp_path) / "workspace"
        self.project = self.workspace / "project"
        self.primary_root = self.project / "core"
        self.receipts_dir = self.workspace / ".setup_agent" / "invocation_receipts"
        self.assessments_dir = self.workspace / ".setup_agent" / "evidence_assessments"
        self.project.mkdir(parents=True, exist_ok=True)
        self.receipts_dir.mkdir(parents=True, exist_ok=True)

    def report(self, name, classname, cases):
        path = self.primary_root / "target" / "surefire-reports" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_surefire_xml(classname, cases), encoding="utf-8")
        return path

    def write_receipt(self, payload):
        path = self.receipts_dir / f"{payload['receipt_id']}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def write_raw_assessment(self, name, text):
        self.assessments_dir.mkdir(parents=True, exist_ok=True)
        path = self.assessments_dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def write_assessment(self, payload):
        return self.write_raw_assessment(f"{payload['assessment_id']}.json", json.dumps(payload))


class ParserOrchestrator:
    """Runs the emitted compact parser locally; every other probe is silent."""

    def __init__(self, workspace):
        self.commands = []
        self.workspace = workspace
        self.atomic = FakeContainer()
        self.evidence = strict_published_evidence(
            self,
            run_id="run-pytest",
            target_sha="a" * 40,
            run_pin=False,
        )
        authority = evidence_publication_authority_for(self)
        for path in sorted(workspace.receipts_dir.glob("*.json")):
            try:
                payload = validate_receipt_v2(
                    json.loads(path.read_text(encoding="utf-8")),
                    expected_id=path.stem,
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            authority.publish_bytes(
                record_kind="invocation_receipt",
                record_id=payload["receipt_id"],
                raw=path.read_bytes(),
                contract_id=payload.get("contract_id"),
                contract_hash=payload.get("contract_hash"),
            )
        for path in sorted(workspace.assessments_dir.glob("*.json")):
            try:
                payload = validate_assessment_v2(
                    json.loads(path.read_text(encoding="utf-8")),
                    expected_id=path.stem,
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            authority.publish_bytes(
                record_kind="receipt_assessment",
                record_id=payload["assessment_id"],
                raw=path.read_bytes(),
            )

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        text = command.strip()
        if text.startswith(
            (
                "mkdir -p -- ",
                ": > ",
                "printf '%s' ",
                "base64 --decode ",
                "python3 -c ",
                "rm -f -- ",
                "mv -f -- ",
            )
        ):
            result = self.atomic.execute_command(command, **kwargs)
            if text.startswith("mv -f -- ") and result.get("exit_code") == 0:
                target = shlex.split(text)[-1]
                payload = self.atomic.files.get(target)
                if payload is not None:
                    path = Path(target)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(payload, encoding="utf-8")
            return result
        if "SAG_COMPACT_TEST_REPORT_PARSER" in text:
            body = command.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exec(compile(body, "<compact-parser>", "exec"), {})
            return {"exit_code": 0, "success": True, "output": buffer.getvalue()}
        if "SAG_NAMED_JSON_RECORD_V1" in text and "for file in " in text:
            quoted_glob = text.partition(" in ")[2].partition("; do")[0]
            target = shlex.split(quoted_glob)[0]
            directory = Path(target[: -len("/*.json")])
            records = [
                (path.name, path.read_bytes()) for path in sorted(directory.glob("*.json"))
            ]
            return {
                "exit_code": 0,
                "success": True,
                "output": frame_named_json_record_stream(records),
            }
        if "job_obligations" in text:
            # A successful empty atomic-record stream means this local parser
            # fixture has no detached controller work.
            return {
                "exit_code": 0,
                "success": True,
                "output": (
                    frame_named_json_record_stream([])
                    if "SAG_NAMED_JSON_RECORD_V1" in text
                    else frame_json_record_stream([])
                ),
            }
        if text.startswith("test -d "):
            path = text[len("test -d ") :].split()[0].strip("'\"")
            exists = os.path.isdir(path)
            return {
                "exit_code": 0 if exists else 1,
                "success": exists,
                "output": "EXISTS" if exists else "",
            }
        return {"exit_code": 1, "success": False, "output": ""}


@pytest.fixture
def parser_workspace(tmp_path, monkeypatch):
    """One receipted primary coordinate with three passing tests."""
    workspace = ParserWorkspace(tmp_path)
    report = workspace.report("TEST-core.AlphaTest.xml", "core.AlphaTest", ["a", "b", "c"])
    receipt = build_receipt(
        receipt_id="inv-maven-1-0001",
        run_id="run-pytest",
        tool="maven",
        requested_action="test",
        effective_action="test",
        argv="mvn -B test",
        working_directory=str(workspace.primary_root),
        exit_code=0,
        before={},
        after={},
    )
    receipt["report_delta"] = {
        "new": [
            {
                "path": str(report),
                "sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
            }
        ],
        "changed": [],
    }
    workspace.write_receipt(
        validate_receipt_v2(receipt, expected_id=receipt["receipt_id"])
    )

    import sag.agent.attempt_policy as attempt_policy
    from sag.agent.attempt_policy import TestAttemptRequirement, TestCandidateResolution

    root = str(workspace.primary_root)
    requirement = TestAttemptRequirement(
        root=root,
        system="maven",
        required_action={"tool": "build", "params": {"working_directory": root}},
    )
    monkeypatch.setattr(
        attempt_policy,
        "resolve_survey_test_candidates",
        lambda orchestrator: TestCandidateResolution(
            status="available",
            candidates=(requirement,),
            project_root=str(workspace.project),
            workspace_root=str(workspace.workspace),
            primary=requirement,
        ),
    )
    return workspace


def _parse(workspace):
    orchestrator = ParserOrchestrator(workspace)
    validator = PhysicalValidator(
        docker_orchestrator=orchestrator, project_path=str(workspace.workspace)
    )
    return validator, validator.parse_test_reports(str(workspace.project))


def test_a_receipted_rollup_with_no_assessments_is_unchanged(parser_workspace):
    _validator, result = _parse(parser_workspace)

    assert result["receipt_scoped"] is True
    assert result["total_tests"] == 3
    assert "receipt_error" not in result


def test_a_well_formed_assessment_does_not_disturb_the_rollup(parser_workspace):
    parser_workspace.write_assessment(
        assessment("asm-0001", "inv-maven-1-0001", "compile_no_source_mismatch")
    )

    _validator, result = _parse(parser_workspace)

    assert result["total_tests"] == 3
    assert "receipt_error" not in result


def test_a_corrupt_assessment_blocks_evidence_closure_and_names_the_file(parser_workspace):
    parser_workspace.write_raw_assessment(
        "asm-0001.json", '{"assessment_id": "asm-0001", "receipt'
    )

    _validator, result = _parse(parser_workspace)

    assert result["valid"] is False
    assert result["receipt_error"] == (
        "evidence assessment ledger is not host-authorized and complete"
    )
    assert result["total_tests"] == 0


def test_an_assessment_missing_its_required_keys_is_corrupt(parser_workspace):
    parser_workspace.write_raw_assessment(
        "asm-0001.json", json.dumps({"assessment_id": "asm-0001"})
    )

    _validator, result = _parse(parser_workspace)

    assert result["valid"] is False
    assert result["receipt_error"] == (
        "evidence assessment ledger is not host-authorized and complete"
    )


def test_a_corrupt_assessment_blocks_the_phase_gate(parser_workspace):
    parser_workspace.write_raw_assessment("asm-0001.json", "not json at all")
    orchestrator = ParserOrchestrator(parser_workspace)
    validator = PhysicalValidator(
        docker_orchestrator=orchestrator,
        project_path=str(parser_workspace.workspace),
    )

    status = validator.validate_test_status("project")
    assert status["evidence_status"] == "conflict"
    assert "test_receipt_unreadable" in status["conflicts"]

    gate = check_phase_claim(
        "test",
        PhaseClaim(phase="test", claimed_outcome=PhaseOutcome.SUCCESS),
        validator,
        orchestrator,
        "project",
    )
    assert gate.accepted is False
    assert "assessment ledger" in gate.reason


def test_a_partially_written_assessment_never_blocks_closure(parser_workspace):
    parser_workspace.write_raw_assessment(
        "asm-0001.json.tmp", '{"assessment_id": "asm-0001", "receipt'
    )

    _validator, result = _parse(parser_workspace)

    assert result["total_tests"] == 3
    assert "receipt_error" not in result


def test_the_compact_parser_accepts_a_v2_receipt(parser_workspace):
    """v2 adds keys; the v1 scoping contract it inherits must still hold."""
    report = next((parser_workspace.primary_root / "target" / "surefire-reports").glob("*.xml"))
    payload = build_receipt(
        receipt_id="inv-maven-2-0002",
        run_id="run-pytest",
        tool="maven",
        requested_action="test",
        effective_action="test",
        argv="mvn -B test",
        working_directory=str(parser_workspace.primary_root),
        exit_code=0,
        before={},
        after={},
    )
    payload["report_delta"] = {
        "new": [],
        "changed": [
            {"path": str(report), "sha256": hashlib.sha256(report.read_bytes()).hexdigest()}
        ]
    }
    parser_workspace.write_receipt(
        validate_receipt_v2(payload, expected_id=payload["receipt_id"])
    )

    _validator, result = _parse(parser_workspace)

    assert result["receipt_scoped"] is True
    assert result["total_tests"] == 3
    assert "receipt_error" not in result


def test_an_unsupported_receipt_version_still_fails_closed_in_the_rollup(parser_workspace):
    payload = receipt_v1("inv-maven-3-0003", str(parser_workspace.primary_root))
    payload["schema_version"] = 3
    parser_workspace.write_receipt(payload)

    _validator, result = _parse(parser_workspace)

    assert result["valid"] is False
    assert result["receipt_error"] == (
        "invocation receipt ledger is not host-authorized and complete"
    )


def test_an_empty_assessment_directory_changes_nothing(parser_workspace):
    parser_workspace.assessments_dir.mkdir(parents=True, exist_ok=True)

    _validator, result = _parse(parser_workspace)

    assert result["total_tests"] == 3
    assert "receipt_error" not in result


# ---------------------------------------------------------------------------
# 5. Verifier: receipts.immutable
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_verifier_module():
    spec = importlib.util.spec_from_file_location(
        "verify_native_test_policy",
        os.path.join(REPO_ROOT, "scripts", "verify_native_test_policy.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_verifier_session(tmp_path, receipts=(), *, recorded_hashes=None, raw=()):
    session = Path(tmp_path) / "session"
    control = session / ".setup_agent"
    control.mkdir(parents=True, exist_ok=True)
    receipts_dir = control / "invocation_receipts"

    events = []
    for payload in receipts:
        receipts_dir.mkdir(parents=True, exist_ok=True)
        (receipts_dir / f"{payload['receipt_id']}.json").write_text(
            json.dumps(payload, sort_keys=True), encoding="utf-8"
        )
        metadata = {"receipt_id": payload["receipt_id"]}
        if recorded_hashes and payload["receipt_id"] in recorded_hashes:
            metadata["receipt_sha256"] = recorded_hashes[payload["receipt_id"]]
        events.append(
            {
                "kind": "tool_result",
                "payload": {"envelope_id": payload["receipt_id"], "result": {"metadata": metadata}},
            }
        )
    for name, text in raw:
        receipts_dir.mkdir(parents=True, exist_ok=True)
        (receipts_dir / name).write_text(text, encoding="utf-8")
    (control / "control_events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    (control / "verdict.json").write_text(json.dumps({"test_stats": {}}), encoding="utf-8")
    return session


def run_receipts_immutable(session):
    verifier = load_verifier_module().Verifier(str(session), forensic=True)
    verifier.assert_receipts_immutable()
    return verifier


def test_no_receipt_directory_asserts_nothing(tmp_path):
    verifier = run_receipts_immutable(write_verifier_session(tmp_path))

    assert verifier.passes == []
    assert verifier.failures == []


def test_parseable_receipt_files_without_recorded_hashes_pass(tmp_path):
    session = write_verifier_session(
        tmp_path,
        [
            receipt_v1("inv-maven-1-0001", "/workspace/project"),
            receipt_v2("inv-gradle-1-0002", "/workspace/project/core"),
        ],
    )

    verifier = run_receipts_immutable(session)

    assert verifier.passes == ["receipts.immutable"]
    assert verifier.failures == []


def test_a_truncated_receipt_file_fails_the_immutability_assertion(tmp_path):
    session = write_verifier_session(
        tmp_path,
        [receipt_v1("inv-maven-1-0001", "/workspace/project")],
        raw=[("inv-maven-1-0002.json", '{"schema_version": 1, "recei')],
    )

    verifier = run_receipts_immutable(session)

    assert verifier.passes == []
    assert any("inv-maven-1-0002.json" in failure for failure in verifier.failures)


def test_an_empty_receipt_file_fails_the_immutability_assertion(tmp_path):
    session = write_verifier_session(tmp_path, raw=[("inv-maven-1-0001.json", "")])

    verifier = run_receipts_immutable(session)

    assert any("inv-maven-1-0001.json" in failure for failure in verifier.failures)


def test_a_recorded_hash_that_still_matches_passes(tmp_path):
    payload = receipt_v2("inv-gradle-1-0001", "/workspace/project")
    digest = hashlib.sha256((json.dumps(payload, sort_keys=True)).encode("utf-8")).hexdigest()
    session = write_verifier_session(
        tmp_path, [payload], recorded_hashes={"inv-gradle-1-0001": digest}
    )

    verifier = run_receipts_immutable(session)

    assert verifier.passes == ["receipts.immutable"]


def test_a_receipt_rewritten_after_its_recorded_hash_fails(tmp_path):
    """The mark_semantic_failure rewrite this stage deletes is now a FAILURE."""
    payload = receipt_v1("inv-gradle-1-0001", "/workspace/project")
    stale = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    rewritten = dict(payload, outcome="failed", semantic_failure=NO_SOURCE_DETAIL)
    session = write_verifier_session(
        tmp_path, [rewritten], recorded_hashes={"inv-gradle-1-0001": stale}
    )

    verifier = run_receipts_immutable(session)

    assert verifier.passes == []
    assert any("inv-gradle-1-0001" in failure for failure in verifier.failures)


# ---------------------------------------------------------------------------
# 6. Plan 5 regression: the recorded battery keeps grading clean
# ---------------------------------------------------------------------------
RECORDED_SESSIONS = {
    "cli": ("session_20260726_192837_88194",),
    "bigtop": ("session_20260726_195220_99607",),
    "tvm": ("session_20260726_192841_88267", "session_20260726_200021_3936"),
}


@pytest.mark.parametrize(
    "profile,name",
    [(profile, name) for profile, names in RECORDED_SESSIONS.items() for name in names],
)
def test_recorded_plan5_sessions_grade_without_failures(profile, name):
    session = os.path.join(REPO_ROOT, "logs", name)
    if not os.path.isdir(session):
        pytest.skip(f"recorded session {name} not present")
    module = load_verifier_module()
    verifier = module.Verifier(session, forensic=True)
    verifier.assert_pairing_and_hashes()
    verifier.assert_receipts_immutable()
    getattr(verifier, f"assert_{profile}")()

    assert verifier.failures == []
    # The archived receipts are v1 and parse, so the new assertion registers
    # and passes rather than staying silent on the only live evidence we have.
    assert "receipts.immutable" in verifier.passes


def test_control_assessments_share_the_directory_without_corrupting_it():
    """Live p6v-cli-r1: a ControlAssessment (event_or_intent_id, no
    receipt_id) in evidence_assessments/ was judged a corrupt
    ReceiptAssessment and the fail-closed path blocked test closure over a
    green suite. The two record shapes share one directory by design."""
    from sag.agent.phase_gates import _condemned_receipt_ids

    control = {
        "schema_version": 1,
        "assessment_id": "asm-ctl_build_retry_0001-retry_without_delta-bffda016",
        "event_or_intent_id": "ctl-build_retry-0001",
        "stage": "precondition",
        "typed_code": "retry_without_delta",
        "detail": "already failed as expectation_unmet x1",
    }
    receipts = [{"receipt_id": "inv-maven-1-0001", "outcome": "completed"}]
    condemning = {
        "schema_version": 1,
        "assessment_id": "asm-x",
        "receipt_id": "inv-maven-1-0001",
        "typed_code": "compile_no_source_mismatch",
    }

    condemned, conflicts = _condemned_receipt_ids(receipts, [control, condemning])

    assert condemned == frozenset({"inv-maven-1-0001"})
    assert conflicts == ()  # the control record raises no missing-receipt noise
