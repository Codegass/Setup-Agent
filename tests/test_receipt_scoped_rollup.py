"""Receipt-scoped primary test rollup (Plan 5 Stage B, Task B2).

The 2026-07-26 ground-truth review (§"Evidence is snapshot-global instead of
receipt-scoped") traced Bigtop's ``54/54`` to a validator that answers "which
XML files exist under the project root?" instead of "which reports did the
primary coordinate's runner invocation actually write?".  Fifty of those
reports came from ``bigtop-data-generators``; the other four came from the
test-framework build and silently joined the primary numerator.

These tests drive the partition from hand-written schema-v1 invocation
receipts (the cross-lane contract in
``docs/superpowers/plans/2026-07-26-sagv2-plan5-p0-ground-truth.md``):

* PRIMARY  — scanned reports claimed by ``report_delta`` of a receipt whose
  ``working_directory`` is at/under the primary test coordinate root AND whose
  recorded ``sha256`` still matches the file's current content.
* STALE    — claimed but superseded (no receipt hash matches the current
  content): excluded from primary and flagged, never re-attributed.
* AUXILIARY— every other scanned report: visible as ``auxiliary_test_stats``,
  never in the primary numerator or denominator.

The compact in-container parser is executed locally against real temp files
(same source string the container runs), so the assertions cover the exact
script the validator emits, including its prepended coordinates.
"""

import contextlib
import hashlib
import io
import json
import os
import shlex
from pathlib import Path

import pytest
from test_container_io import FakeContainer

from sag.agent.attempt_policy import TestAttemptRequirement as AttemptRequirement
from sag.agent.attempt_policy import TestCandidateResolution as CandidateResolution
from sag.agent.evidence_assessments import ReceiptAssessment, validate_assessment_v2
from sag.agent.evidence_publications import publish_evidence_bytes
from sag.agent.evidence_records import (
    frame_json_record_stream,
    frame_named_json_record_stream,
)
from sag.agent.invocation_receipts import build_receipt, validate_receipt_v2
from sag.agent.phase_gates import check_phase_claim, check_phase_done
from sag.agent.phase_machine import PhaseClaim, PhaseOutcome
from sag.agent.physical_validator import PhysicalValidator


# ---------------------------------------------------------------------------
# Fixtures: real files on disk, hand-written schema-v1 receipts
# ---------------------------------------------------------------------------
def _surefire_xml(classname: str, names) -> str:
    cases = "".join(
        f'<testcase classname="{classname}" name="{name}" time="0.01"/>' for name in names
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<testsuite name="{classname}" tests="{len(list(names))}" failures="0" '
        f'errors="0" skipped="0">{cases}</testsuite>'
    )


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _receipt(receipt_id: str, working_directory: Path, new=(), changed=(), cached=()) -> dict:
    """One strict current v2 invocation receipt."""
    report_delta = {}
    if new:
        report_delta["new"] = [{"path": str(p), "sha256": _sha256(Path(p))} for p in new]
    if changed:
        report_delta["changed"] = [{"path": str(p), "sha256": _sha256(Path(p))} for p in changed]
    if cached:
        report_delta["cached"] = [{"path": str(p), "sha256": _sha256(Path(p))} for p in cached]
    receipt = build_receipt(
        receipt_id=receipt_id,
        tool="maven",
        requested_action="test",
        effective_action="test",
        argv="mvn -B test",
        working_directory=str(working_directory),
        exit_code=0,
        before={},
        after={},
    )
    receipt["report_delta"] = {"new": [], "changed": [], **report_delta}
    return validate_receipt_v2(receipt, expected_id=receipt_id)


class ReceiptWorkspace:
    """A Bigtop-shaped workspace: a primary coordinate plus auxiliary reports."""

    def __init__(self, tmp_path: Path):
        self.workspace = tmp_path / "workspace"
        self.project = self.workspace / "bigtop"
        self.primary_root = self.project / "bigtop-data-generators"
        self.auxiliary_root = self.project / "bigtop-tests" / "test-framework"
        self.receipts_dir = self.workspace / ".setup_agent" / "invocation_receipts"
        self.assessments_dir = self.workspace / ".setup_agent" / "evidence_assessments"
        self.project.mkdir(parents=True, exist_ok=True)

    # -- reports ---------------------------------------------------------
    def primary_report(self, name: str, classname: str, cases) -> Path:
        return _write(
            self.primary_root / "target" / "surefire-reports" / name,
            _surefire_xml(classname, cases),
        )

    def auxiliary_report(self, name: str, classname: str, cases) -> Path:
        return _write(
            self.auxiliary_root / "target" / "surefire-reports" / name,
            _surefire_xml(classname, cases),
        )

    # -- receipts --------------------------------------------------------
    def write_receipt(self, payload: dict) -> Path:
        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        path = self.receipts_dir / f"{payload.get('receipt_id', 'receipt')}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def write_raw_receipt(self, name: str, text: str) -> Path:
        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        path = self.receipts_dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def write_assessment(self, payload: dict) -> Path:
        self.assessments_dir.mkdir(parents=True, exist_ok=True)
        path = self.assessments_dir / f"{payload['assessment_id']}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path


class ReceiptOrchestrator:
    """Runs the emitted compact parser locally; every other probe is silent."""

    def __init__(self, workspace: ReceiptWorkspace):
        self.workspace = workspace
        self.commands: list[str] = []
        self.atomic = FakeContainer()
        if workspace.receipts_dir.is_dir():
            for path in sorted(workspace.receipts_dir.glob("*.json")):
                try:
                    payload = validate_receipt_v2(
                        json.loads(path.read_text(encoding="utf-8")),
                        expected_id=path.stem,
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                publication = publish_evidence_bytes(
                    self,
                    record_kind="invocation_receipt",
                    record_id=payload["receipt_id"],
                    raw=path.read_bytes(),
                    contract_id=payload.get("contract_id"),
                    contract_hash=payload.get("contract_hash"),
                )
                assert publication.published
        if workspace.assessments_dir.is_dir():
            for path in sorted(workspace.assessments_dir.glob("*.json")):
                try:
                    payload = validate_assessment_v2(
                        json.loads(path.read_text(encoding="utf-8")),
                        expected_id=path.stem,
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                publication = publish_evidence_bytes(
                    self,
                    record_kind="receipt_assessment",
                    record_id=payload["assessment_id"],
                    raw=path.read_bytes(),
                )
                assert publication.published

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
            return self._run_compact_parser(command)
        if "SAG_NAMED_JSON_RECORD_V1" in text and "for file in " in text:
            target = shlex.split(text.partition(" in ")[2].partition("; do")[0])[0]
            directory = Path(target[: -len("/*.json")])
            records = (
                [(path.name, path.read_bytes()) for path in sorted(directory.glob("*.json"))]
                if directory.is_dir()
                else []
            )
            return {
                "exit_code": 0,
                "success": True,
                "output": frame_named_json_record_stream(records),
            }
        if "job_obligations" in text and "SAG_JSON_RECORD_END_V1" in text:
            return {
                "exit_code": 0,
                "success": True,
                "output": frame_json_record_stream([]),
            }
        if text.startswith("test -d "):
            path = text[len("test -d ") :].split()[0].strip("'\"")
            exists = os.path.isdir(path)
            marker = "EXISTS" if "EXISTS" in text else ""
            return {
                "exit_code": 0 if exists else 1,
                "success": exists,
                "output": marker if exists else "",
            }
        return {"exit_code": 1, "success": False, "output": ""}

    @staticmethod
    def _run_compact_parser(command: str) -> dict:
        body = command.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exec(compile(body, "<compact-parser>", "exec"), {})
        return {"exit_code": 0, "success": True, "output": buffer.getvalue()}


@pytest.fixture
def bigtop(tmp_path, monkeypatch):
    """Bigtop's matrix row: 50 primary tests plus 4 auxiliary test-framework tests."""
    workspace = ReceiptWorkspace(tmp_path)
    workspace.primary_report(
        "TEST-org.apache.bigtop.datagen.AlphaTest.xml",
        "org.apache.bigtop.datagen.AlphaTest",
        [f"alpha{i}" for i in range(25)],
    )
    workspace.primary_report(
        "TEST-org.apache.bigtop.datagen.BetaTest.xml",
        "org.apache.bigtop.datagen.BetaTest",
        [f"beta{i}" for i in range(25)],
    )
    for index in range(4):
        workspace.auxiliary_report(
            f"TEST-org.apache.bigtop.itest.Framework{index}Test.xml",
            f"org.apache.bigtop.itest.Framework{index}Test",
            [f"framework{index}"],
        )
    return workspace


def _validator(workspace: ReceiptWorkspace) -> tuple[PhysicalValidator, ReceiptOrchestrator]:
    orchestrator = ReceiptOrchestrator(workspace)
    validator = PhysicalValidator(
        docker_orchestrator=orchestrator,
        project_path=str(workspace.workspace),
    )
    return validator, orchestrator


def _bind_primary_coordinate(monkeypatch, workspace: ReceiptWorkspace, root=None) -> None:
    """Bind attempt_policy's primary test coordinate (Plan 4) without probing."""
    import sag.agent.attempt_policy as attempt_policy

    resolved = str(root if root is not None else workspace.primary_root)
    requirement = AttemptRequirement(
        root=resolved,
        system="maven",
        required_action={"tool": "build", "params": {"working_directory": resolved}},
    )
    resolution = CandidateResolution(
        status="available",
        candidates=(requirement,),
        project_root=str(workspace.project),
        workspace_root=str(workspace.workspace),
        primary=requirement,
    )
    monkeypatch.setattr(
        attempt_policy, "resolve_survey_test_candidates", lambda orchestrator: resolution
    )


def _unbound_primary_coordinate(monkeypatch) -> None:
    import sag.agent.attempt_policy as attempt_policy

    monkeypatch.setattr(
        attempt_policy,
        "resolve_survey_test_candidates",
        lambda orchestrator: CandidateResolution(status="manifest_unreadable"),
    )


# ---------------------------------------------------------------------------
# Bigtop's acceptance row: primary exactly 50, auxiliary exactly 4
# ---------------------------------------------------------------------------
def test_primary_and_auxiliary_reports_coexist_at_fifty_and_four(bigtop, monkeypatch):
    """Auxiliary reports stay visible but never enter the primary numerator."""
    _bind_primary_coordinate(monkeypatch, bigtop)
    primary_reports = sorted((bigtop.primary_root / "target" / "surefire-reports").glob("*.xml"))
    auxiliary_reports = sorted(
        (bigtop.auxiliary_root / "target" / "surefire-reports").glob("*.xml")
    )
    bigtop.write_receipt(_receipt("inv-test-1-0001", bigtop.primary_root, new=primary_reports))
    # The auxiliary reports are ALSO a real runner invocation — provenance, not
    # absence, is what keeps them out of the primary rollup.
    bigtop.write_receipt(_receipt("inv-build-1-0002", bigtop.auxiliary_root, new=auxiliary_reports))
    validator, _ = _validator(bigtop)

    result = validator.parse_test_reports(str(bigtop.project))

    assert result["receipt_scoped"] is True
    assert result["total_tests"] == 50
    assert result["passed_tests"] == 50
    assert result["unique_tests"] == 50
    assert result["raw_total_tests"] == 50
    assert result["auxiliary_test_stats"] == {
        "executed": 4,
        "passed": 4,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
    }
    assert len(result["auxiliary_report_files"]) == 4
    assert "stale_test_reports" not in result


def test_receipt_scoped_rollup_quarantines_auxiliary(bigtop, monkeypatch):
    """The sealed rollup shows 50 primary and carries the auxiliary block apart."""
    _bind_primary_coordinate(monkeypatch, bigtop)
    bigtop.write_receipt(
        _receipt(
            "inv-test-1-0001",
            bigtop.primary_root,
            new=sorted((bigtop.primary_root / "target" / "surefire-reports").glob("*.xml")),
        )
    )
    validator, orchestrator = _validator(bigtop)

    facts = check_phase_done("test", validator, orchestrator, "bigtop")["validated_facts"]
    rollup = facts["test.stats"]

    assert rollup["receipt_scoped"] is True
    assert rollup["unique"]["executed"] == 50
    assert rollup["unique"]["passed"] == 50
    assert rollup["raw"]["executed"] == 50
    assert rollup["auxiliary_test_stats"]["executed"] == 4


# ---------------------------------------------------------------------------
# Retry overwrite: the same path, two receipts, one current content
# ---------------------------------------------------------------------------
def test_retry_overwrite_cannot_double_count_the_superseded_attempt(tmp_path, monkeypatch):
    """Attempt 2 rewrote the report in place; attempt 1's hash no longer matches."""
    workspace = ReceiptWorkspace(tmp_path)
    report = workspace.primary_report(
        "TEST-org.apache.bigtop.datagen.RetryTest.xml",
        "org.apache.bigtop.datagen.RetryTest",
        [f"retry{i}" for i in range(3)],
    )
    stale_receipt = _receipt("inv-test-1-0001", workspace.primary_root, new=[report])
    # Attempt 2 overwrites the SAME path with a larger, different report.
    _write(
        report,
        _surefire_xml("org.apache.bigtop.datagen.RetryTest", [f"retry{i}" for i in range(25)]),
    )
    fresh_receipt = _receipt("inv-test-2-0002", workspace.primary_root, changed=[report])
    workspace.write_receipt(stale_receipt)
    workspace.write_receipt(fresh_receipt)
    _bind_primary_coordinate(monkeypatch, workspace)
    validator, _ = _validator(workspace)

    result = validator.parse_test_reports(str(workspace.project))

    assert result["receipt_scoped"] is True
    assert result["total_tests"] == 25
    assert result["raw_total_tests"] == 25
    # The path is verified by the NEWEST receipt, so the superseded claim can
    # neither double-count it nor flag the live report as stale.
    assert "stale_test_reports" not in result


# ---------------------------------------------------------------------------
# Stale hash: claimed, superseded by nothing we can attribute
# ---------------------------------------------------------------------------
def test_stale_report_is_excluded_from_primary_and_flagged(tmp_path, monkeypatch):
    """A claimed report whose content no receipt vouches for is quarantined."""
    workspace = ReceiptWorkspace(tmp_path)
    kept = workspace.primary_report(
        "TEST-org.apache.bigtop.datagen.KeptTest.xml",
        "org.apache.bigtop.datagen.KeptTest",
        [f"kept{i}" for i in range(25)],
    )
    superseded = workspace.primary_report(
        "TEST-org.apache.bigtop.datagen.StaleTest.xml",
        "org.apache.bigtop.datagen.StaleTest",
        [f"stale{i}" for i in range(10)],
    )
    payload = _receipt("inv-test-1-0001", workspace.primary_root, new=[kept, superseded])
    # Something outside the receipted invocation rewrote the second report.
    _write(
        superseded,
        _surefire_xml("org.apache.bigtop.datagen.StaleTest", [f"stale{i}" for i in range(7)]),
    )
    workspace.write_receipt(payload)
    _bind_primary_coordinate(monkeypatch, workspace)
    validator, _ = _validator(workspace)

    result = validator.parse_test_reports(str(workspace.project))

    assert result["receipt_scoped"] is True
    assert result["total_tests"] == 25
    assert result["stale_test_reports"] == [str(superseded)]
    # Superseded primary evidence is NOT laundered into the auxiliary block.
    assert "auxiliary_test_stats" not in result


# ---------------------------------------------------------------------------
# Corrupt receipt: evidence-closure failure, the gate refuses to close
# ---------------------------------------------------------------------------
def test_corrupt_receipt_fails_closed_with_the_file_named(bigtop, monkeypatch):
    _bind_primary_coordinate(monkeypatch, bigtop)
    bigtop.write_receipt(
        _receipt(
            "inv-test-1-0001",
            bigtop.primary_root,
            new=sorted((bigtop.primary_root / "target" / "surefire-reports").glob("*.xml")),
        )
    )
    corrupt = bigtop.write_raw_receipt("inv-test-1-0002.json", '{"schema_version": 1, "recei')
    validator, _ = _validator(bigtop)

    result = validator.parse_test_reports(str(bigtop.project))

    assert result["valid"] is False
    assert result["receipt_error"] == (
        "invocation receipt ledger is not host-authorized and complete"
    )
    assert result["total_tests"] == 0


@pytest.mark.parametrize("mutation", ("tamper", "delete"))
def test_host_published_receipt_must_still_exist_with_exact_bytes(bigtop, monkeypatch, mutation):
    _bind_primary_coordinate(monkeypatch, bigtop)
    path = bigtop.write_receipt(
        _receipt(
            "inv-test-1-0001",
            bigtop.primary_root,
            new=sorted((bigtop.primary_root / "target" / "surefire-reports").glob("*.xml")),
        )
    )
    validator, _ = _validator(bigtop)
    initial = validator.parse_test_reports(str(bigtop.project))
    assert initial["valid"] is True
    assert initial["total_tests"] == 50
    if mutation == "tamper":
        # Same JSON meaning, different physical bytes: exact host publication
        # is the live authority, not reparsing a container-controlled body.
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        path.unlink()

    result = validator.parse_test_reports(str(bigtop.project))

    assert result["valid"] is False
    assert result["receipt_error"] == (
        "invocation receipt ledger is not host-authorized and complete"
    )
    assert result["total_tests"] == 0


@pytest.mark.parametrize("mutation", ("tamper", "delete"))
def test_host_published_assessment_ledger_is_complete_and_exact(bigtop, monkeypatch, mutation):
    _bind_primary_coordinate(monkeypatch, bigtop)
    receipt = _receipt(
        "inv-test-1-0001",
        bigtop.primary_root,
        new=sorted((bigtop.primary_root / "target" / "surefire-reports").glob("*.xml")),
    )
    bigtop.write_receipt(receipt)
    assessment = ReceiptAssessment(
        receipt_id=receipt["receipt_id"],
        typed_code="expectation_met",
    ).payload()
    path = bigtop.write_assessment(assessment)
    validator, _ = _validator(bigtop)
    initial = validator.parse_test_reports(str(bigtop.project))
    assert initial["valid"] is True
    assert initial["total_tests"] == 50
    if mutation == "tamper":
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        path.unlink()

    result = validator.parse_test_reports(str(bigtop.project))

    assert result["valid"] is False
    assert result["receipt_error"] == (
        "evidence assessment ledger is not host-authorized and complete"
    )
    assert result["total_tests"] == 0


def test_corrupt_receipt_blocks_phase_closure(bigtop, monkeypatch):
    _bind_primary_coordinate(monkeypatch, bigtop)
    bigtop.write_raw_receipt("inv-test-1-0001.json", "not json at all")
    validator, orchestrator = _validator(bigtop)

    status = validator.validate_test_status("bigtop")
    assert status["evidence_status"] == "conflict"
    assert "test_receipt_unreadable" in status["conflicts"]

    for claimed in (PhaseOutcome.SUCCESS, PhaseOutcome.PARTIAL):
        gate = check_phase_claim(
            "test",
            PhaseClaim(phase="test", claimed_outcome=claimed),
            validator,
            orchestrator,
            "bigtop",
        )
        assert gate.accepted is False, claimed
        assert "receipt ledger" in gate.reason


def test_unavailable_container_parser_refuses_an_unscoped_rollup(bigtop, monkeypatch):
    """No receipt-aware parser + receipts on disk = no rollup, not a global one."""
    _bind_primary_coordinate(monkeypatch, bigtop)
    bigtop.write_receipt(
        _receipt(
            "inv-test-1-0001",
            bigtop.primary_root,
            new=sorted((bigtop.primary_root / "target" / "surefire-reports").glob("*.xml")),
        )
    )
    validator, orchestrator = _validator(bigtop)
    monkeypatch.setattr(
        ReceiptOrchestrator,
        "_run_compact_parser",
        staticmethod(lambda command: {"exit_code": 127, "success": False, "output": ""}),
    )

    result = validator.parse_test_reports(str(bigtop.project))

    assert result["valid"] is False
    assert "invocation receipts exist" in result["receipt_error"]
    assert result["total_tests"] == 0


def test_receipted_run_without_reports_never_falls_back_to_the_global_scan(tmp_path, monkeypatch):
    """Zero reports is "no reports" — not an excuse for a provenance-free scan."""
    workspace = ReceiptWorkspace(tmp_path)
    workspace.write_receipt(_receipt("inv-test-1-0001", workspace.primary_root))
    _bind_primary_coordinate(monkeypatch, workspace)
    validator, orchestrator = _validator(workspace)

    result = validator.parse_test_reports(str(workspace.project))

    assert result["valid"] is False
    assert result["error"] == "No test report files found"
    assert "receipt_error" not in result
    assert not any("surefire-reports' -o" in command for command in orchestrator.commands)


def test_receipt_directory_follows_the_cross_lane_storage_contract():
    """schema-v1 receipts live at /workspace/.setup_agent/invocation_receipts."""
    validator = PhysicalValidator(docker_orchestrator=None)

    assert validator._invocation_receipts_dir() == ("/workspace/.setup_agent/invocation_receipts")


def test_receipt_missing_required_schema_fields_is_corrupt(bigtop, monkeypatch):
    _bind_primary_coordinate(monkeypatch, bigtop)
    bigtop.write_raw_receipt(
        "inv-test-1-0003.json", json.dumps({"schema_version": 2, "receipt_id": "x"})
    )
    validator, _ = _validator(bigtop)

    result = validator.parse_test_reports(str(bigtop.project))

    assert result["valid"] is False
    assert "receipt ledger" in result["receipt_error"]


# ---------------------------------------------------------------------------
# Universal claim scoping: the partition does not wait for a receipt to exist
# ---------------------------------------------------------------------------
def test_no_receipts_directory_still_partitions_the_corpus(bigtop, monkeypatch):
    """Zero receipts is zero authority, not a licence to count everything.

    The partition used to be ARMED on receipt presence while exclusion ran
    against report CLAIMS, so a run with no receipt directory at all took the
    unscoped branch and handed the headline to 54 executions nothing vouched
    for — while geode's run, whose only receipts were two `compileJava`
    receipts claiming nothing, took the full partition and sealed 0. Deleting
    attributed evidence therefore IMPROVED the sealed number. The arming is
    gone: the partition is a property of the scan, not of the ledger's size.
    """
    _bind_primary_coordinate(monkeypatch, bigtop)
    validator, _ = _validator(bigtop)

    result = validator.parse_test_reports(str(bigtop.project))

    assert result["receipt_scoped"] is True
    assert result["total_tests"] == 0
    assert result["auxiliary_test_stats"]["executed"] == 54
    assert "stale_test_reports" not in result


def test_empty_receipts_directory_still_partitions_the_corpus(bigtop, monkeypatch):
    _bind_primary_coordinate(monkeypatch, bigtop)
    bigtop.receipts_dir.mkdir(parents=True, exist_ok=True)
    validator, _ = _validator(bigtop)

    result = validator.parse_test_reports(str(bigtop.project))

    assert result["receipt_scoped"] is True
    assert result["total_tests"] == 0
    assert result["auxiliary_test_stats"]["executed"] == 54


def test_a_receipt_can_only_move_reports_from_auxiliary_to_the_headline(bigtop, monkeypatch):
    """The evidence gradient is monotone in both directions.

    Adding a receipt may only move reports from auxiliary to headline; a run
    without it may never seal a HIGHER number. The same corpus is read twice —
    once with no ledger at all, then with the primary coordinate's receipt.
    """
    _bind_primary_coordinate(monkeypatch, bigtop)
    primary_reports = sorted((bigtop.primary_root / "target" / "surefire-reports").glob("*.xml"))
    receipt = _receipt("inv-test-1-0001", bigtop.primary_root, new=primary_reports)
    validator, _ = _validator(bigtop)

    def scan(records):
        return validator._parse_test_reports_compact_in_container(
            str(bigtop.project),
            primary_root=str(bigtop.primary_root),
            receipt_records=records,
        )

    without_receipt = scan([])
    with_receipt = scan([receipt])

    assert without_receipt["total_tests"] == 0
    assert without_receipt["auxiliary_test_stats"]["executed"] == 54
    assert with_receipt["total_tests"] == 50
    assert with_receipt["auxiliary_test_stats"]["executed"] == 4
    assert without_receipt["total_tests"] <= with_receipt["total_tests"]


def test_an_unresolved_primary_coordinate_still_counts_only_claimed_reports(bigtop, monkeypatch):
    """The coordinate NARROWS the claim set; it never authorizes a whole-tree scan.

    Live p7b-camel (`logs/session_20260728_020936_55719`): the coordinate could
    not be resolved, scoping fell back to the legacy scan, and 17,798 tests
    entered the MAIN count with no receipt behind any of them — the same
    unscoped number this machinery exists to remove. Not knowing which subset
    is primary is a reason to count every claimed report, never a reason to
    count everything on disk.

    Here the one receipt claims the primary module's 50 reports, so 50 is the
    main count and the auxiliary 4 stay out of it, exactly as they would with
    the coordinate resolved. The conflict is still recorded: the run should
    say that it could not narrow further.
    """
    _unbound_primary_coordinate(monkeypatch)
    bigtop.write_receipt(
        _receipt(
            "inv-test-1-0001",
            bigtop.primary_root,
            new=sorted((bigtop.primary_root / "target" / "surefire-reports").glob("*.xml")),
        )
    )
    validator, _ = _validator(bigtop)

    result = validator.parse_test_reports(str(bigtop.project))

    assert result["total_tests"] == 50
    assert result["receipt_scoped"] is True
    assert "test_primary_coordinate_unresolved" in result["metrics_conflicts"]


def test_a_receipt_claiming_nothing_leaves_the_main_count_empty(bigtop, monkeypatch):
    """The camel shape: one compile receipt, zero reports claimed.

    Every report on disk was produced by something the harness never
    dispatched, so the main count is zero and the reports are auxiliary. The
    alternative — counting them because we cannot attribute them — is how a
    number nobody can vouch for becomes the headline.
    """
    _unbound_primary_coordinate(monkeypatch)
    bigtop.write_receipt(_receipt("inv-compile-1-0001", bigtop.primary_root, new=[]))
    validator, _ = _validator(bigtop)

    result = validator.parse_test_reports(str(bigtop.project))

    assert result["total_tests"] == 0
    assert result["receipt_scoped"] is True
    assert (result.get("auxiliary_test_stats") or {}).get("executed") == 54


def test_an_unreceipted_rollup_states_the_partition_it_came_from(bigtop, monkeypatch):
    """The sealed shape of a receipt-free run, after universal scoping.

    This was `test_legacy_rollup_shape_is_unchanged`, which pinned the OTHER
    shape: no scoping keys and `unique.executed == 54`, i.e. the whole corpus
    as the headline because the ledger was empty. Under universal scoping the
    rollup states its basis (`receipt_scoped`) and carries the excluded volume
    beside a zero headline. Archived replay fixtures are unaffected: they
    replay the rollups their own runs recorded, and every optional key here is
    still absent-when-unobserved.

    `denominator_basis` is NOT one of the optional keys: every run states what
    its denominator covers, and `none` — no census exists — is a statement,
    not an absence (#39 §2.2). Bigtop's fixture has no module census, so the
    module arithmetic behind the word stays absent beside it.
    """
    _bind_primary_coordinate(monkeypatch, bigtop)
    validator, orchestrator = _validator(bigtop)

    facts = check_phase_done("test", validator, orchestrator, "bigtop")["validated_facts"]
    rollup = facts["test.stats"]

    assert set(rollup) == {
        "discovered",
        "denominator_basis",
        "unique",
        "raw",
        "flaky_count",
        "conflicts",
        "collection_errors",
        "collection_errors_skipped",
        "receipt_scoped",
        "auxiliary_test_stats",
    }
    assert rollup["denominator_basis"] == "none"
    assert rollup["receipt_scoped"] is True
    assert rollup["unique"]["executed"] == 0
    assert rollup["auxiliary_test_stats"]["executed"] == 54


def test_a_cached_claim_counts_toward_the_primary_rollup(bigtop, monkeypatch):
    """kafka's shape: the dispatch rewrote nothing, the build vouched for the
    reports it served from cache, and those reports are this run's evidence.

    Before this, a `--build-cache` run could claim nothing it did not rewrite:
    kafka observed 5,232 tests and reported 546.
    """
    _bind_primary_coordinate(monkeypatch, bigtop)
    bigtop.write_receipt(
        _receipt(
            "inv-test-1-0001",
            bigtop.primary_root,
            cached=sorted((bigtop.primary_root / "target" / "surefire-reports").glob("*.xml")),
        )
    )
    validator, _ = _validator(bigtop)

    result = validator.parse_test_reports(str(bigtop.project))

    assert result["total_tests"] == 50
    assert result["receipt_scoped"] is True


def test_second_validation_admits_newly_published_receipts_without_waiting_for_ttl(
    bigtop, monkeypatch
):
    _bind_primary_coordinate(monkeypatch, bigtop)
    reports = sorted((bigtop.primary_root / "target" / "surefire-reports").glob("*.xml"))
    bigtop.write_receipt(_receipt("inv-test-1-0001", bigtop.primary_root, new=reports[:1]))
    validator, orchestrator = _validator(bigtop)
    first = validator.parse_test_reports(str(bigtop.project))
    assert first["total_tests"] == 25

    receipt = _receipt("inv-test-1-0002", bigtop.primary_root, new=reports[1:])
    path = bigtop.write_receipt(receipt)
    publication = publish_evidence_bytes(
        orchestrator,
        record_kind="invocation_receipt",
        record_id=receipt["receipt_id"],
        raw=path.read_bytes(),
        contract_id=receipt.get("contract_id"),
        contract_hash=receipt.get("contract_hash"),
    )
    assert publication.published
    second = validator.parse_test_reports(str(bigtop.project))
    assert second["total_tests"] == 50
    assert first["total_tests"] == 25


def test_second_validation_rechecks_report_bytes_against_the_same_receipt(bigtop, monkeypatch):
    _bind_primary_coordinate(monkeypatch, bigtop)
    reports = sorted((bigtop.primary_root / "target" / "surefire-reports").glob("*.xml"))
    bigtop.write_receipt(_receipt("inv-test-1-0001", bigtop.primary_root, new=reports))
    validator, _ = _validator(bigtop)
    assert validator.parse_test_reports(str(bigtop.project))["total_tests"] == 50
    for path in reports:
        path.write_bytes(path.read_bytes() + b"\n")
    second = validator.parse_test_reports(str(bigtop.project))
    assert second["total_tests"] == 0
    assert sorted(second["stale_test_reports"]) == sorted(map(str, reports))


def test_second_validation_rebinds_the_primary_coordinate_with_the_same_ledger(bigtop, monkeypatch):
    for index, root in enumerate((bigtop.primary_root, bigtop.auxiliary_root), start=1):
        reports = sorted((root / "target" / "surefire-reports").glob("*.xml"))
        bigtop.write_receipt(_receipt(f"inv-test-1-{index:04}", root, new=reports))
    _bind_primary_coordinate(monkeypatch, bigtop)
    validator, _ = _validator(bigtop)
    first = validator.parse_test_reports(str(bigtop.project))
    assert first["total_tests"] == 50
    _bind_primary_coordinate(monkeypatch, bigtop, root=bigtop.auxiliary_root)
    second = validator.parse_test_reports(str(bigtop.project))
    assert second["total_tests"] == 4
    assert second["test_modules"] == [str(bigtop.auxiliary_root)]
    assert first["total_tests"] == 50
