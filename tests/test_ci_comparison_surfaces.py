"""Reader and command-entry agreement; producer tests live in test_ci_comparison."""

import hashlib
import json

import pytest
from test_cli_project_exit_codes import RecordingSetupAgent, invoke_project
from test_control_layer_replay import _pin_template_agent
from test_java_success_certificates import _payload, _scope
from test_snapshot_surface_agreement import (
    VERDICT_PATH,
    SnapshotOrchestrator,
    SurfaceHarness,
    _phase_trunk,
    snapshot_factory,
)

from sag.agent.ci_comparison import CIComparisonSnapshot, load_ci_target, render_ci_comparison_lines
from sag.agent.control_events import canonical_sha256
from sag.agent.evidence_publications import (
    VERDICT_LOGICAL_ARTIFACT_ID,
    evidence_publication_authority_for,
)
from sag.agent.java_success_certificates import evaluate_java_success_certificate
from sag.agent.verdict_finalizer import RunVerdictSnapshot
from sag.metrics.attainment import evaluate_attainment, view_from_certificate
from sag.metrics.target_record import CellTarget, TargetRecord
from sag.web.session_registry import _session_detail, _setup_artifact_item


def comparison_fixture(*, missing_scope=False):
    """A controlled display input, never evidence of a real official run."""
    payload = _payload(scope=_scope(target_sha="a" * 40))
    certificate = evaluate_java_success_certificate(payload)
    cell = CellTarget(
        cell_id="controlled-maven-17",
        grade="A",
        build="ok",
        executed_count=12,
        red_count=0,
        modules=() if missing_scope else ("root",),
        modules_basis=None if missing_scope else "log",
    )
    target = TargetRecord(
        repo="apache/commons-cli",
        sha="a" * 40,
        harvested_at="2026-09-09T00:00:00Z",
        cells=(cell,),
        matched_cell=cell.cell_id,
    )
    result = evaluate_attainment(view_from_certificate(certificate, repo=target.repo), target)
    return CIComparisonSnapshot(
        status="evaluated",
        run_id=certificate.run_id,
        repo=target.repo,
        target_sha=certificate.target_sha,
        target_record_sha256=canonical_sha256(target.model_dump(mode="json")),
        certificate_input_sha256=canonical_sha256(payload.model_dump(mode="json")),
        certificate=certificate,
        attainment=result,
        receipt_ids=("display-fixture",),
        reasons=result.reason_codes,
    )


def _block_ci_fragments(comparison):
    """What the result block must state about a sealed comparison."""

    result = comparison.attainment
    if result is None:
        return ["not compared", *comparison.reasons]
    score = (
        f"{result.alpha.numerator:,}/{result.alpha.denominator:,}"
        if result.alpha is not None
        else "scope score unavailable"
    )
    return [str(result.verdict).replace("_", " "), score, result.cell_id]


@pytest.mark.parametrize("status", ["met", "missing_scope", "no_target", "unavailable"])
def test_published_comparison_is_identical_across_every_surface(snapshot_factory, status):
    comparison = (
        comparison_fixture(missing_scope=status == "missing_scope")
        if status in {"met", "missing_scope"}
        else CIComparisonSnapshot(status=status, run_id="run-1", reasons=("fixture_missing",))
    )
    snapshot = snapshot_factory(
        verdict="success", unique_total=12, unique_passed=12, unique_errors=0, raw_executions=12
    )
    snapshot = RunVerdictSnapshot.model_validate(
        {**snapshot.model_dump(mode="json"), "run_id": "run-1", "ci_comparison": comparison}
    )
    surfaces = SurfaceHarness().render_all(snapshot)
    expected = render_ci_comparison_lines(comparison)
    for surface in (surfaces.markdown, surfaces.condensed):
        assert surface.verdict == "success"
        for line in expected:
            assert line in surface.text
    # The block says the same result in its own words, so the agreement is
    # checked against the sealed comparison itself rather than the report's
    # phrasing.
    assert surfaces.cli.verdict == "success"
    for fragment in _block_ci_fragments(comparison):
        assert fragment in surfaces.cli.text
    files = {
        VERDICT_PATH: snapshot.model_dump_json(),
        "/workspace/.setup_agent/contexts/trunk_tvm.json": _phase_trunk(),
    }
    orchestrator = SnapshotOrchestrator(files)
    item = _setup_artifact_item(orchestrator, "sag-tvm")
    first = _session_detail(item, "sag-tvm", None).model_dump(mode="json", by_alias=True)
    second = _session_detail(item, "sag-tvm", None).model_dump(mode="json", by_alias=True)
    assert first == second
    assert first["ciComparison"] == comparison.model_dump(mode="json")
    assert first["ciComparisonLines"] == expected
    assert first["canonicalVerdict"] == "success"
    if status == "met":
        assert comparison.attainment.verdict == "met"
        assert comparison.attainment.alpha is not None
    if status == "missing_scope":
        assert comparison.attainment.alpha is None
        assert comparison.attainment.verdict != "met"

    # A surviving mirror cannot preserve comparison authority after revocation.
    authority = evidence_publication_authority_for(orchestrator)
    head = authority.latest_head(VERDICT_LOGICAL_ARTIFACT_ID)
    authority.revoke_latest(
        record_kind="verdict",
        record_id=VERDICT_LOGICAL_ARTIFACT_ID,
        logical_artifact_id=VERDICT_LOGICAL_ARTIFACT_ID,
        expected_previous_raw_sha256=head.raw_sha256,
    )
    withdrawn = _setup_artifact_item(orchestrator, "sag-tvm")
    assert withdrawn["ci_comparison"] is None
    assert "unavailable" in withdrawn["ci_comparison_lines"][0]


def test_cli_loads_explicit_target_bytes_before_starting_agent(monkeypatch, tmp_path):
    target = TargetRecord(
        repo="apache/commons-cli",
        sha="a" * 40,
        harvested_at="2026-09-09T00:00:00Z",
        cells=(
            CellTarget(
                cell_id="unknown", grade="B", build="unknown", executed_count=0, red_count=0
            ),
        ),
    )
    raw = target.model_dump_json().encode()
    path = tmp_path / "target.json"
    path.write_bytes(raw)
    RecordingSetupAgent.calls = []
    result = invoke_project(
        monkeypatch, tmp_path, RecordingSetupAgent, "--ci-target-file", str(path)
    )
    assert result.exit_code == 0, result.output
    pinned = RecordingSetupAgent.calls[0]["ci_target"]
    assert pinned.record == target
    assert pinned.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert "Official CI" in result.output
    assert "not compared" in result.output
    assert "[fully]" not in result.output


def test_cli_rejects_invalid_target_before_agent_execution(monkeypatch, tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps({"repo": "apache/commons-cli"}))
    RecordingSetupAgent.calls = []
    result = invoke_project(
        monkeypatch, tmp_path, RecordingSetupAgent, "--ci-target-file", str(path)
    )
    assert result.exit_code != 0
    assert RecordingSetupAgent.calls == []


def test_selected_target_is_pinned_before_checkout_and_survives_source_file_edits(tmp_path):
    target = TargetRecord(
        repo="apache/commons-cli",
        sha="a" * 40,
        harvested_at="2026-09-09T00:00:00Z",
        cells=(
            CellTarget(
                cell_id="jdk17",
                grade="A",
                build="ok",
                executed_count=12,
                red_count=0,
                modules=(".",),
                modules_basis="log",
            ),
        ),
        matched_cell="jdk17",
    )
    path = tmp_path / "target.json"
    path.write_text(target.model_dump_json())
    selected = load_ci_target(path)
    agent = _pin_template_agent(tmp_path)
    agent._setup_ci_target = selected
    path.write_text("{}")
    agent._initialize_run_pin_template()
    pin = json.loads((tmp_path / "run-pin.json").read_text())
    assert pin["target_repo_sha"] is None
    assert pin["sanitized_config"]["ci_target"] == {
        "record_sha256": selected.raw_sha256,
        "repo": target.repo,
        "sha": target.sha,
        "matched_cell": "jdk17",
    }
