"""Replay actual DBCP evidence; removing a premise must not improve completion.

The archived runtime/report bytes are real. Live publication readers and the
container transport are replaced only at their I/O boundaries; assessment,
scope evaluation, physical completion and phase projection remain real.
"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import zipfile

import pytest
from test_test_execution_completion import validator_for

from sag.agent import evidence_assessments as assessment
from sag.agent.phase_gates import _inspect_test, ValidatorState
from sag.benchmark.native_evidence import junit_counts
from sag.benchmark.requirements import bound_file, canonical_digest, evaluation_identity
from sag.benchmark.sag_observer import SAGRequirementObserver


@pytest.fixture
def archived(tmp_path):
    with zipfile.ZipFile(Path(__file__).parent / "fixtures/pilot_dbcp_test_completion.zip") as z:
        for name in z.namelist():
            assert (tmp_path / name).resolve().is_relative_to(tmp_path.resolve())
        z.extractall(tmp_path)
    manifest = json.loads((tmp_path / "fixture-manifest.json").read_text())
    for ref in manifest["files"]:
        raw = (tmp_path / ref["path"]).read_bytes()
        assert len(raw) == ref["bytes"] and hashlib.sha256(raw).hexdigest() == ref["sha256"]
    data = {
        name: json.loads((tmp_path / (name + ".json")).read_text())
        for name in ("task", "requirements", "receipt", "contract", "observation")
    }
    data["output"] = (tmp_path / "output.log").read_text()

    class NoContainer:
        def execute(self, *args, **kwargs):
            raise AssertionError("Offline replay must not call a container or build tool")

    owner = NoContainer()
    observer = SAGRequirementObserver(
        tmp_path / "session",
        data["receipt"]["run_id"],
        data["contract"]["expected_cwd"],
        data["task"],
        data["requirements"],
        owner.execute,
    )
    observer._exports[data["receipt"]["contract_id"]] = data["observation"]
    owner.requirement_observer = observer
    data.update(observer=observer, owner=owner)
    return data


def project(monkeypatch, case, *, completion_enabled=True):
    monkeypatch.setattr(assessment, "write_assessment", lambda *_: True)
    if not completion_enabled:
        case["owner"].requirement_observer = None
    records = assessment.assess_dispatch(
        case["owner"].execute,
        contract=case["contract"],
        receipt=case["receipt"],
        output=case["output"],
        current_fingerprints={
            k: case["receipt"][k] for k in assessment.FINGERPRINT_KEYS if k in case["receipt"]
        },
        require_bound_output=True,
    )
    validator = validator_for(
        monkeypatch, [(case["receipt"], case["contract"])], [r.payload() for r in records]
    )
    return validator, records


def test_completed_tests_survive_a_later_javadoc_failure(monkeypatch, archived):
    # Same actual record: only enable/disable the new scope-completion evidence.
    control, _ = project(monkeypatch, archived, completion_enabled=False)
    root = archived["contract"]["expected_cwd"]
    assert control._test_execution_receipt_summary(root)["state"] == "unknown"
    archived["owner"].requirement_observer = archived["observer"]
    fixed, records = project(monkeypatch, archived)
    assert fixed._test_execution_receipt_summary(root)["state"] == "completed"
    assert archived["receipt"]["exit_code"] == 1  # Original command still failed.
    proof = next(r for r in records if r.typed_code == assessment.TEST_SCOPE_COMPLETED)
    raw = (archived["observer"].base / proof.evidence_ref).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == proof.scope
    result = json.loads(raw)
    assert result["whole_command_success"] is False
    assert [r["status"] for r in result["requirements"]] == ["passed"]
    counts = junit_counts(archived["observer"].base, archived["observation"]["reports"])
    assert counts == dict(reported=1605, passed=1596, failed=0, errors=0, skipped=9, assessed=1596)
    monkeypatch.setattr(
        fixed,
        "parse_test_reports_with_catalog",
        lambda _: {
            "valid": True,
            "receipt_scoped": True,
            "total_tests": 1605,
            "passed_tests": 1596,
            "failed_tests": 0,
            "error_tests": 0,
            "skipped_tests": 9,
        },
    )
    monkeypatch.setattr(fixed, "_python_collected_count", lambda _: None)
    gate = _inspect_test(fixed, "commons-dbcp")
    assert gate.state is ValidatorState.GREEN
    assert gate.validated_facts["test.stats"]["execution_state"] == "completed"


@pytest.mark.parametrize(
    "removed",
    [
        "output_binding",
        "one_report",
        "collection",
        "receipt_identity",
        "scope_review",
        "native_completion",
        "normal_exit",
        "runtime",
        "contract_scope",
        "future_test_step",
    ],
)
def test_weaker_or_conflicting_evidence_cannot_complete(monkeypatch, archived, removed):
    c = archived
    if removed == "output_binding":
        c["output"] += "\nchanged bytes"
    elif removed == "one_report":
        bound_file(c["observer"].base, c["observation"]["reports"][0]).unlink()
    elif removed == "collection":
        c["observation"]["reports_collection_complete"] = False
    elif removed == "receipt_identity":
        c["observation"]["receipt_id"] = "a-different-receipt"
    elif removed == "scope_review":
        c["observer"].spec["annotation_completeness"]["status"] = "review_required"
    elif removed == "native_completion":
        # Preserve positive reports but stop output within the Surefire execution.
        text = c["output"]
        start = text.index("--- surefire:")
        c["output"] = text[: text.index("[INFO] ---", start + 4)]
        c["receipt"]["output_content_hash"] = hashlib.sha256(c["output"].encode()).hexdigest()
    elif removed == "normal_exit":
        c["receipt"]["termination_reason"] = "timeout"
        c["receipt"]["exit_code"] = 137
    elif removed == "runtime":
        c["observer"].task["steps"][0]["java_major"] = 17
    elif removed == "contract_scope":
        c["receipt"]["argv"] += " -Dtest=SmokeTest"
    elif removed == "future_test_step":
        second = {**c["observer"].task["steps"][0], "id": "integration"}
        c["observer"].task["steps"].append(second)
        row = deepcopy(next(x for x in c["observer"].spec["requirements"] if x["kind"] == "test"))
        row.update(id="later-test-pool", step_id="integration")
        c["observer"].spec["requirements"].append(row)
        c["observer"].spec["task_sha256"] = canonical_digest(c["observer"].task)
        c["observer"].identity = evaluation_identity(c["observer"].spec)
        c["observation"]["evaluation_identity"] = c["observer"].identity
    validator, records = project(monkeypatch, c)
    assert not any(r.typed_code == assessment.TEST_SCOPE_COMPLETED for r in records)
    assert (
        validator._test_execution_receipt_summary(c["contract"]["expected_cwd"])["state"]
        != "completed"
    )


def test_unknown_completion_keeps_its_reason(monkeypatch, archived):
    validator, _ = project(monkeypatch, archived, completion_enabled=False)
    monkeypatch.setattr(
        validator,
        "parse_test_reports_with_catalog",
        lambda _: {"valid": True, "receipt_scoped": True, "total_tests": 1, "passed_tests": 1},
    )
    monkeypatch.setattr(validator, "_python_collected_count", lambda _: None)
    status = validator.validate_test_status("commons-dbcp")
    assert status["test_execution_state"] == "unknown"
    assert (
        status["test_execution_reason"]
        == validator._test_execution_receipt_summary(archived["contract"]["expected_cwd"])["reason"]
    )
