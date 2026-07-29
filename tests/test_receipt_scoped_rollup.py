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
from pathlib import Path

import pytest

from sag.agent.attempt_policy import TestAttemptRequirement as AttemptRequirement
from sag.agent.attempt_policy import TestCandidateResolution as CandidateResolution
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
    """One schema-v1 invocation receipt (exact cross-lane contract)."""
    report_delta = {}
    if new:
        report_delta["new"] = [{"path": str(p), "sha256": _sha256(Path(p))} for p in new]
    if changed:
        report_delta["changed"] = [{"path": str(p), "sha256": _sha256(Path(p))} for p in changed]
    if cached:
        report_delta["cached"] = [{"path": str(p), "sha256": _sha256(Path(p))} for p in cached]
    return {
        "schema_version": 1,
        "receipt_id": receipt_id,
        "tool": "maven",
        "requested_action": "test",
        "effective_action": "test",
        "argv": "mvn -B test",
        "working_directory": str(working_directory),
        "exit_code": 0,
        "outcome": "completed",
        "report_delta": report_delta,
    }


class ReceiptWorkspace:
    """A Bigtop-shaped workspace: a primary coordinate plus auxiliary reports."""

    def __init__(self, tmp_path: Path):
        self.workspace = tmp_path / "workspace"
        self.project = self.workspace / "bigtop"
        self.primary_root = self.project / "bigtop-data-generators"
        self.auxiliary_root = self.project / "bigtop-tests" / "test-framework"
        self.receipts_dir = self.workspace / ".setup_agent" / "invocation_receipts"
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


class ReceiptOrchestrator:
    """Runs the emitted compact parser locally; every other probe is silent."""

    def __init__(self, workspace: ReceiptWorkspace):
        self.workspace = workspace
        self.commands: list[str] = []

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        text = command.strip()
        if "SAG_COMPACT_TEST_REPORT_PARSER" in text:
            return self._run_compact_parser(command)
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
    assert str(corrupt) in result["receipt_error"]
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
        assert "inv-test-1-0001.json" in gate.reason


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
    assert "inv-test-1-0003.json" in result["receipt_error"]


# ---------------------------------------------------------------------------
# Legacy fallback: no receipts means byte-identical behaviour
# ---------------------------------------------------------------------------
def test_no_receipts_directory_keeps_the_global_scan(bigtop, monkeypatch):
    """Recorded sessions have no receipts: the legacy rollup must not move."""
    _bind_primary_coordinate(monkeypatch, bigtop)
    validator, _ = _validator(bigtop)

    result = validator.parse_test_reports(str(bigtop.project))

    assert result["total_tests"] == 54
    assert "receipt_scoped" not in result
    assert "auxiliary_test_stats" not in result
    assert "stale_test_reports" not in result


def test_empty_receipts_directory_keeps_the_global_scan(bigtop, monkeypatch):
    _bind_primary_coordinate(monkeypatch, bigtop)
    bigtop.receipts_dir.mkdir(parents=True, exist_ok=True)
    validator, _ = _validator(bigtop)

    result = validator.parse_test_reports(str(bigtop.project))

    assert result["total_tests"] == 54
    assert "receipt_scoped" not in result


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


def test_legacy_rollup_shape_is_unchanged(bigtop, monkeypatch):
    """Byte-compat with recorded replay fixtures: absent facts stay absent keys."""
    _bind_primary_coordinate(monkeypatch, bigtop)
    validator, orchestrator = _validator(bigtop)

    facts = check_phase_done("test", validator, orchestrator, "bigtop")["validated_facts"]
    rollup = facts["test.stats"]

    assert set(rollup) == {
        "discovered",
        "unique",
        "raw",
        "flaky_count",
        "conflicts",
        "collection_errors",
        "collection_errors_skipped",
    }
    assert rollup["unique"]["executed"] == 54


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
