# tests/test_verdict_kernel_unification.py
"""Bug #11 (live probes 2026-07-10): the blocked-build evidence-rescue must
live INSIDE the shared verdict kernel, not only in the run finalization.

Live evidence, pyyaml run 7: the report banner printed
"🎯 SETUP COMPLETED: ❌ FAILED" while the finalization logged "Phase machine
recorded a blocked build phase, but physical build evidence shows a real
build; capping verdict to partial instead of failed" and ended
"Project setup completed: success=True, verdict=partial". libcloud run 2:
the same FAILED-banner/partial-final split.

Root cause: SetupAgent._get_verified_final_status applied the rescue
(agent-blocked build phase + physical build evidence success=True -> the
machine outcome caps at PARTIAL, not FAILED) inline, while the report's
_snapshot_kernel_verdict — which stamps the stored snapshot verdict and feeds
every rendered banner — never heard of it and kept saying failed.

Fix under test: sag.verdict.rescue_blocked_build is ONE function consumed by
BOTH the finalization and the snapshot kernel, and the finalization keeps
consuming the snapshot verdict (the bug #4 mirror), so all three surfaces —
report banner, stored snapshot, CLI final — emit the SAME verdict.

Secondary incoherence under test (pyyaml run 6 shape, same blocked-build
path): the agent's report call carried zeroed test_stats (it believed the run
was blocked), so the rendered output said "Tests: no tests executed" directly
under a banner showing "🧪 Tests: 1287 detected, 1287 executed". When the
rescue fires, physical evidence outranks agent belief for the stats line too.

Regressions pinned here: an evidence-absent blocked build stays FAILED on ALL
surfaces (no false-green path), and a Java artifact-backed blocked build
behaves identically to the python one on all surfaces.
"""

from test_python_phase_verdict import _pytest_all_green_test_status, _python_partial_build_status
from test_report_contract import PhaseTrunkContextManager

from sag.tools.report_tool import ReportTool
from sag.verdict import rescue_blocked_build

PYYAML_TOTAL = 1287


# ---------------------------------------------------------------------------
# fixtures: pyyaml run 7's physical shape, fed through the REAL snapshot
# builder (no hand-written snapshot dicts for the reproduction).
# ---------------------------------------------------------------------------


def _pyyaml7_build_status():
    """Python evidence ladder: success=True / build_complete=False (real
    build, declared C-extensions have no built .so artifact)."""
    status = _python_partial_build_status()
    status["evidence"] = {
        "build_system": "python",
        "fingerprint_details": {
            "venv_exists": True,
            "pip_check_clean": True,
            "imports_ok": True,
            "ext_modules_ok": False,
        },
    }
    status["test_stats"] = None
    status["evidence_refs"] = ["/workspace/pyyaml"]
    return status


def _pyyaml7_test_status(total=PYYAML_TOTAL):
    status = _pytest_all_green_test_status(total)
    status["evidence_status"] = "success"
    status["test_stats"] = {
        "discovered": total,
        "executed": total,
        "passed": total,
        "failed": 0,
        "skipped": 0,
        "pass_rate": 100.0,
    }
    status["static_test_count"] = total
    status["evidence_refs"] = ["/workspace/.setup_agent/pytest-reports/pytest-1783686362.xml"]
    return status


def _pyyaml7_accomplishments(total=PYYAML_TOTAL):
    return {
        "repository_cloned": True,
        "build_success": True,
        "test_success": True,
        "physical_validation": {
            "build_status": _pyyaml7_build_status(),
            "test_status": _pyyaml7_test_status(total),
            "test_analysis": {
                "total_tests": total,
                "passed_tests": total,
                "failed_tests": 0,
                "error_tests": 0,
                "skipped_tests": 0,
                "pass_rate": 100.0,
                "report_files": ["/workspace/.setup_agent/pytest-reports/pytest-1783686362.xml"],
            },
        },
    }


_PYYAML_PROJECT_INFO = {
    "directory": "/workspace/pyyaml",
    "type": "Python Project",
    "build_system": "pip/poetry",
}


def _build_real_snapshot(
    tool,
    accomplishments,
    project_info,
    verified_status="success",
    evidence_status="success",
    model_test_stats=None,
    evidence_refs=("/workspace/pyyaml",),
):
    """Drive the REAL evidence resolver + snapshot builder, mirroring the
    report(action='generate') internals for the live run's report call."""
    evidence_result = tool._resolve_report_evidence_result(
        evidence_status,
        model_test_stats,
        None,
        list(evidence_refs),
        verified_status,
        verified_status,
        accomplishments,
    )
    return tool._build_legacy_report_snapshot(
        verified_status,
        "setup-report-test.md",
        project_info,
        accomplishments,
        {},
        evidence_result,
    )


# ---------------------------------------------------------------------------
# 0. the shared rescue function's contract
# ---------------------------------------------------------------------------


def test_rescue_blocked_build_contract():
    # evidence-backed blocked build -> partial, never failed, never success
    assert rescue_blocked_build("failed", True) == "partial"
    # evidence-absent blocked build passes through untouched
    assert rescue_blocked_build("failed", False) == "failed"
    # never promotes anything else
    assert rescue_blocked_build("partial", True) == "partial"
    assert rescue_blocked_build("success", True) == "success"
    assert rescue_blocked_build(None, True) is None


# ---------------------------------------------------------------------------
# 1. LIVE-RUN REPRODUCTION (pyyaml run 7) through the REAL snapshot builder:
#    the stored snapshot verdict, the rendered banners, and the CLI final all
#    say PARTIAL — not a FAILED banner over a partial final.
# ---------------------------------------------------------------------------


def test_pyyaml7_snapshot_verdict_is_partial_not_failed():
    tool = ReportTool(context_manager=PhaseTrunkContextManager(blocked={"build"}))
    snapshot = _build_real_snapshot(
        tool,
        _pyyaml7_accomplishments(),
        _PYYAML_PROJECT_INFO,
        model_test_stats={
            "discovered": 49,
            "executed": PYYAML_TOTAL,
            "passed": PYYAML_TOTAL,
            "failed": 0,
            "skipped": 0,
        },
    )

    assert snapshot["status"]["verdict"] == "partial", (
        "the snapshot kernel must apply the SAME blocked-build evidence-rescue "
        "as the finalization: agent-blocked build phase + physical build "
        "evidence success=True is PARTIAL, not FAILED"
    )


def test_pyyaml7_condensed_banner_says_partial_with_real_test_stats():
    tool = ReportTool(context_manager=PhaseTrunkContextManager(blocked={"build"}))
    snapshot = _build_real_snapshot(tool, _pyyaml7_accomplishments(), _PYYAML_PROJECT_INFO)

    banner = tool._generate_condensed_log_output(
        "success", "setup-report-test.md", _pyyaml7_accomplishments(), snapshot
    )

    first_line = banner.splitlines()[0]
    assert "PARTIAL" in first_line.upper(), first_line
    assert "FAILED" not in first_line.upper(), first_line
    assert "1287" in banner, banner
    assert "no tests executed" not in banner, banner


def test_pyyaml7_markdown_header_says_partial_with_real_test_stats():
    tool = ReportTool(context_manager=PhaseTrunkContextManager(blocked={"build"}))
    snapshot = _build_real_snapshot(tool, _pyyaml7_accomplishments(), _PYYAML_PROJECT_INFO)

    lines = tool._render_enhanced_header(
        "2026-07-10 08:26:16", "success", _PYYAML_PROJECT_INFO, snapshot=snapshot
    )

    result_lines = [l for l in lines if l.startswith("**Result:**")]
    assert result_lines and "PARTIAL" in result_lines[0].upper(), result_lines
    tests_lines = [l for l in lines if l.startswith("**Tests:**")]
    assert tests_lines and "1287" in tests_lines[0], tests_lines
    assert "no tests executed" not in tests_lines[0], tests_lines


def test_pyyaml7_legacy_report_surfaces_agree_on_partial():
    """The retained legacy report adapter keeps its own surfaces aligned."""
    tool = ReportTool(context_manager=PhaseTrunkContextManager(blocked={"build"}))
    snapshot = _build_real_snapshot(tool, _pyyaml7_accomplishments(), _PYYAML_PROJECT_INFO)
    banner = tool._generate_condensed_log_output(
        "success", "setup-report-test.md", _pyyaml7_accomplishments(), snapshot
    )

    assert snapshot["status"]["verdict"] == "partial"
    assert "PARTIAL" in banner.splitlines()[0].upper()


# ---------------------------------------------------------------------------
# 2. secondary incoherence (pyyaml run 6 shape): the agent's report call
#    zeroed test_stats while the rescue fired — the rendered output must show
#    the physically-validated stats, not "no tests executed".
# ---------------------------------------------------------------------------


def test_rescued_report_output_renders_real_stats_over_model_zeroed_stats(monkeypatch):
    tool = ReportTool(context_manager=PhaseTrunkContextManager(blocked={"build"}))
    accomplishments = _pyyaml7_accomplishments()
    zeroed = {"discovered": 0, "executed": 0, "passed": 0, "failed": 0, "skipped": 0}
    snapshot = _build_real_snapshot(
        tool, accomplishments, _PYYAML_PROJECT_INFO, model_test_stats=zeroed
    )

    monkeypatch.setattr(tool, "_validate_context_prerequisites", lambda: {"valid": True})
    monkeypatch.setattr(
        tool,
        "_generate_comprehensive_report",
        lambda summary, status, details, **kwargs: (
            "# Full Report",
            "success",
            "setup-report-test.md",
            accomplishments,
            snapshot,
        ),
    )

    result = tool.execute(action="generate", summary="done", status="success", test_stats=zeroed)

    assert result.succeeded is True
    assert "no tests executed" not in result.output, result.output
    assert "1287 / 1287 passed" in result.output, result.output
    assert result.test_stats is not None and result.test_stats.executed == PYYAML_TOTAL
    assert "PARTIAL" in result.output.splitlines()[0].upper(), result.output


def test_rescue_does_not_invent_stats_when_nothing_ran(monkeypatch):
    """libcloud run 2 shape: rescue fires (compileall evidence) but genuinely
    0 tests executed — 'no tests executed' stays, honestly."""
    accomplishments = {
        "repository_cloned": True,
        "build_success": True,
        "test_success": False,
        "physical_validation": {
            "build_status": {
                "success": True,
                "build_complete": True,
                "reason": "python evidence ladder: compileall 100%",
                "evidence": {
                    "build_system": "python",
                    "fingerprint_details": {
                        "venv_exists": True,
                        "pip_check_clean": True,
                        "imports_ok": True,
                        "compileall_coverage": 1.0,
                    },
                },
                "evidence_status": "success",
                "test_stats": None,
                "conflicts": [],
                "evidence_refs": ["/workspace/libcloud"],
            },
            "test_status": {
                "has_test_reports": False,
                "status": "WARNING",
                "reason": "No test reports found",
                "pass_rate": 0.0,
                "total_tests": 0,
                "passed_tests": 0,
                "failed_tests": 0,
                "error_tests": 0,
                "skipped_tests": 0,
                "test_exclusions": [],
                "modules_without_tests": [],
                "evidence_status": "unknown",
                "test_stats": None,
                "static_test_count": None,
                "conflicts": [],
                "evidence_refs": ["/workspace/libcloud"],
            },
            "test_analysis": {},
        },
    }
    project_info = {
        "directory": "/workspace/libcloud",
        "type": "Python Project",
        "build_system": "pip/poetry",
    }
    tool = ReportTool(context_manager=PhaseTrunkContextManager(blocked={"build", "test"}))
    zeroed = {"discovered": 0, "executed": 0, "passed": 0, "failed": 0, "skipped": 0}
    snapshot = _build_real_snapshot(
        tool,
        accomplishments,
        project_info,
        verified_status="fail",
        evidence_status="blocked",
        model_test_stats=zeroed,
        evidence_refs=("/workspace/libcloud",),
    )

    monkeypatch.setattr(tool, "_validate_context_prerequisites", lambda: {"valid": True})
    monkeypatch.setattr(
        tool,
        "_generate_comprehensive_report",
        lambda summary, status, details, **kwargs: (
            "# Full Report",
            "fail",
            "setup-report-test.md",
            accomplishments,
            snapshot,
        ),
    )

    result = tool.execute(
        action="generate",
        summary="blocked",
        status="partial",
        evidence_status="blocked",
        test_stats=zeroed,
    )

    assert "no tests executed" in result.output, result.output


def test_libcloud2_banner_and_snapshot_say_partial_like_the_final():
    """libcloud run 2: rescue fires on compileall evidence; the model's
    evidence_status='blocked' restates the same agent belief and must not keep
    the banner at FAILED while the CLI says partial."""
    accomplishments = {
        "repository_cloned": True,
        "build_success": True,
        "test_success": False,
        "physical_validation": {
            "build_status": {
                "success": True,
                "build_complete": True,
                "reason": "python evidence ladder: compileall 100%",
                "evidence": {"build_system": "python", "fingerprint_details": {}},
                "evidence_status": "success",
                "test_stats": None,
                "conflicts": [],
                "evidence_refs": ["/workspace/libcloud"],
            },
            "test_status": {
                "has_test_reports": False,
                "status": "WARNING",
                "reason": "No test reports found",
                "pass_rate": 0.0,
                "total_tests": 0,
                "passed_tests": 0,
                "failed_tests": 0,
                "error_tests": 0,
                "skipped_tests": 0,
                "test_exclusions": [],
                "modules_without_tests": [],
                "evidence_status": "unknown",
                "test_stats": None,
                "static_test_count": None,
                "conflicts": [],
                "evidence_refs": ["/workspace/libcloud"],
            },
            "test_analysis": {},
        },
    }
    project_info = {
        "directory": "/workspace/libcloud",
        "type": "Python Project",
        "build_system": "pip/poetry",
    }
    tool = ReportTool(context_manager=PhaseTrunkContextManager(blocked={"build", "test"}))
    snapshot = _build_real_snapshot(
        tool,
        accomplishments,
        project_info,
        verified_status="fail",
        evidence_status="blocked",
        evidence_refs=("/workspace/libcloud",),
    )

    assert snapshot["status"]["verdict"] == "partial", snapshot["status"]

    banner = tool._generate_condensed_log_output(
        "fail", "setup-report-test.md", accomplishments, snapshot
    )
    assert "PARTIAL" in banner.splitlines()[0].upper(), banner


# ---------------------------------------------------------------------------
# 3. regression: evidence-ABSENT blocked build stays FAILED on ALL surfaces —
#    the rescue never creates a false-green (or false-yellow) path.
# ---------------------------------------------------------------------------


def _no_evidence_accomplishments():
    return {
        "repository_cloned": True,
        "build_success": False,
        "test_success": False,
        "physical_validation": {
            "build_status": {
                "success": False,
                "build_complete": False,
                "reason": "No build evidence found (no artifacts or build fingerprints)",
                "evidence": {},
                "evidence_status": "blocked",
                "test_stats": None,
                "conflicts": ["build_validation_failed"],
                "evidence_refs": ["/workspace/demo"],
            },
            "test_status": {
                "has_test_reports": False,
                "status": "WARNING",
                "reason": "No test reports found",
                "pass_rate": 0.0,
                "total_tests": 0,
                "passed_tests": 0,
                "failed_tests": 0,
                "error_tests": 0,
                "skipped_tests": 0,
                "test_exclusions": [],
                "modules_without_tests": [],
                "evidence_status": "unknown",
                "test_stats": None,
                "static_test_count": None,
                "conflicts": [],
                "evidence_refs": ["/workspace/demo"],
            },
            "test_analysis": {},
        },
    }


def test_evidence_absent_blocked_build_stays_failed_on_all_surfaces():
    project_info = {
        "directory": "/workspace/demo",
        "type": "Maven Java Project",
        "build_system": "Maven",
    }
    tool = ReportTool(context_manager=PhaseTrunkContextManager(blocked={"build"}))
    snapshot = _build_real_snapshot(
        tool,
        _no_evidence_accomplishments(),
        project_info,
        verified_status="fail",
        evidence_status="blocked",
        evidence_refs=("/workspace/demo",),
    )

    assert snapshot["status"]["verdict"] == "failed"

    banner = tool._generate_condensed_log_output(
        "fail", "setup-report-test.md", _no_evidence_accomplishments(), snapshot
    )
    assert "FAILED" in banner.splitlines()[0].upper(), banner


# ---------------------------------------------------------------------------
# 4. regression: Java artifact-backed blocked build behaves IDENTICALLY on all
#    surfaces — the rescue's scope is the evidence, not the language.
# ---------------------------------------------------------------------------


def _java_artifact_accomplishments(total=100):
    return {
        "repository_cloned": True,
        "build_success": True,
        "test_success": True,
        "physical_validation": {
            "build_status": {
                "success": True,
                "build_complete": True,
                "reason": "Found 120 compiled classes (build appears successful)",
                "evidence": {"build_system": "maven"},
                "evidence_status": "success",
                "test_stats": None,
                "conflicts": [],
                "evidence_refs": ["/workspace/demo"],
            },
            "test_status": {
                "has_test_reports": True,
                "status": "SUCCESS",
                "reason": "All tests passed",
                "pass_rate": 100.0,
                "total_tests": total,
                "passed_tests": total,
                "failed_tests": 0,
                "error_tests": 0,
                "skipped_tests": 0,
                "test_exclusions": [],
                "modules_without_tests": [],
                "evidence_status": "success",
                "test_stats": {
                    "discovered": total,
                    "executed": total,
                    "passed": total,
                    "failed": 0,
                    "skipped": 0,
                    "pass_rate": 100.0,
                },
                "static_test_count": total,
                "conflicts": [],
                "evidence_refs": ["/workspace/demo/target/surefire-reports"],
            },
            "test_analysis": {
                "total_tests": total,
                "passed_tests": total,
                "failed_tests": 0,
                "error_tests": 0,
                "skipped_tests": 0,
                "pass_rate": 100.0,
                "catalog_test_count": total,
                "report_files": ["/workspace/demo/target/surefire-reports/TEST-demo.xml"],
            },
            "class_files": 120,
            "jar_files": 2,
        },
    }


def test_java_artifact_backed_verdict_ignores_phase_termination_on_all_surfaces():
    project_info = {
        "directory": "/workspace/demo",
        "type": "Maven Java Project",
        "build_system": "Maven",
    }
    tool = ReportTool(context_manager=PhaseTrunkContextManager(blocked={"build"}))
    snapshot = _build_real_snapshot(tool, _java_artifact_accomplishments(), project_info)

    assert snapshot["status"]["verdict"] == "success"

    banner = tool._generate_condensed_log_output(
        "success", "setup-report-test.md", _java_artifact_accomplishments(), snapshot
    )
    assert "SUCCESS" in banner.splitlines()[0].upper(), banner
    assert "100" in banner, banner
