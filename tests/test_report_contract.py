import json

from sag.evidence import EvidenceStatus, TestStats
from sag.agent.context_manager import Task, TaskStatus, TrunkContext
from sag.tools.internal.command_tracker import CommandTracker
from sag.tools.report_tool import ReportTool


class FakeReplayOrchestrator:
    def __init__(self):
        self.commands = []

    def execute_command(self, command):
        self.commands.append(command)
        return {
            "output": "BUILD SUCCESS\nTests run: 1, Failures: 0, Errors: 0, Skipped: 0",
            "exit_code": 0,
        }


class FakeReportOverlayOrchestrator:
    def __init__(self, files=None, unreadable_paths=None):
        self.files = files or {}
        self.unreadable_paths = set(unreadable_paths or [])

    def read_file(self, path):
        if path in self.unreadable_paths:
            return {"success": False, "content": "", "exit_code": 1}
        return {"success": True, "content": self.files.get(path, ""), "exit_code": 0}


class FakeReportContextManager:
    def __init__(self):
        self.current_task_id = None
        self.trunk = TrunkContext(
            context_id="trunk_test",
            goal="Set up demo",
            project_url="https://example.test/demo.git",
            project_name="demo",
            todo_list=[
                Task(id="task_1", description="Run tests", status=TaskStatus.COMPLETED),
                Task(
                    id="task_2",
                    description="Generate comprehensive setup completion report",
                    status=TaskStatus.PENDING,
                ),
            ],
        )
        self.saved_trunk = None

    def load_trunk_context(self):
        return self.trunk

    def _save_trunk_context(self, trunk_context):
        self.saved_trunk = trunk_context


def _generate_report_with_overlay(overlay_json=None, unreadable_paths=None):
    files = {}
    if overlay_json is not None:
        files["/workspace/.setup_agent/env_overlay.json"] = overlay_json
    tool = ReportTool(
        docker_orchestrator=FakeReportOverlayOrchestrator(
            files,
            unreadable_paths=unreadable_paths,
        )
    )

    return tool._generate_markdown_report(
        "done",
        "success",
        None,
        "2026-06-06 12:00:00",
        {"directory": "/workspace/demo", "type": "Maven Java Project", "build_system": "Maven"},
        {
            "repository_cloned": True,
            "build_success": True,
            "test_success": True,
            "physical_validation": {
                "test_analysis": {
                    "total_tests": 1,
                    "passed_tests": 1,
                    "pass_rate": 100,
                }
            },
        },
        {},
        {
            "status": {"overall": "success", "tests_total": 1, "tests_passed": 1},
            "phases": {"clone": True, "build": True, "test": True},
            "physical_evidence": {},
            "attention": {"raw": []},
        },
    )


def test_report_header_uses_package_version():
    lines = ReportTool()._render_enhanced_header(
        "2026-06-06 12:00:00",
        "success",
        {"directory": "/workspace/demo", "type": "Maven Java Project", "build_system": "Maven"},
    )

    assert lines[0] == "# 🎯 Project Setup Report v0.3.0"


def test_report_tool_returns_full_report_in_raw_data(monkeypatch):
    tool = ReportTool()

    monkeypatch.setattr(tool, "_validate_context_prerequisites", lambda: {"valid": True})
    monkeypatch.setattr(
        tool,
        "_generate_comprehensive_report",
        lambda summary, status, details, **kwargs: (
            "# Full Report",
            "success",
            "setup-report-test.md",
            {
                "build_success": True,
                "test_success": True,
                "physical_validation": {
                    "test_analysis": {
                        "pass_rate": 100,
                        "total_tests": 1,
                        "passed_tests": 1,
                    }
                },
            },
            {"status": "success"},
        ),
    )
    monkeypatch.setattr(
        tool,
        "_generate_condensed_log_output",
        lambda verified_status,
        report_filename,
        actual_accomplishments,
        report_snapshot: "condensed",
    )

    result = tool.execute(action="generate", summary="done", status="success")

    assert result.success is True
    assert result.output == "condensed"
    assert result.raw_data["full_report"] == "# Full Report"
    assert result.raw_data["report_snapshot"]["status"] == "success"
    assert result.metadata["verified_status"] == "success"


def test_report_tool_accepts_evidence_state_when_generation_is_monkeypatched(monkeypatch):
    tool = ReportTool()

    monkeypatch.setattr(tool, "_validate_context_prerequisites", lambda: {"valid": True})
    monkeypatch.setattr(
        tool,
        "_generate_comprehensive_report",
        lambda summary, status, details, **kwargs: (
            "# Full Report",
            "success",
            "setup-report-test.md",
            {
                "build_success": True,
                "test_success": False,
                "physical_validation": {
                    "test_analysis": {
                        "pass_rate": 96.3,
                        "total_tests": 214,
                        "passed_tests": 206,
                        "failed_tests": 3,
                        "skipped_tests": 5,
                    }
                },
            },
            {"status": "success"},
        ),
    )
    monkeypatch.setattr(
        tool,
        "_generate_condensed_log_output",
        lambda verified_status,
        report_filename,
        actual_accomplishments,
        report_snapshot: "condensed",
    )

    result = tool.execute(
        action="generate",
        summary="done",
        status="success",
        evidence_status="partial",
        test_stats={"executed": 214, "passed": 206, "failed": 3, "skipped": 5},
        conflicts=["3 tests failed"],
        evidence_refs=["/workspace/demo/target/surefire-reports/TEST-demo.xml"],
    )

    assert result.success is True
    assert result.status == EvidenceStatus.PARTIAL
    assert isinstance(result.test_stats, TestStats)
    assert result.test_stats.pass_rate == 96.3
    assert result.test_stats.failed == 3
    assert result.conflicts == ["3 tests failed"]
    assert result.evidence_refs == ["/workspace/demo/target/surefire-reports/TEST-demo.xml"]
    assert "Result: PARTIAL" in result.output
    assert "96.3% pass rate" in result.output
    assert "3 failed" in result.output
    assert "3 tests failed" in result.output
    assert result.metadata["status"] == "success"
    assert result.metadata["evidence_status"] == "partial"
    assert result.raw_data["evidence_status"] == "partial"
    assert result.raw_data["test_stats"]["pass_rate"] == 96.3


def test_real_report_renderer_includes_evidence_result(monkeypatch):
    tool = ReportTool()
    saved_markdown = {}

    actual_accomplishments = {
        "repository_cloned": True,
        "build_success": True,
        "test_success": False,
        "physical_validation": {
            "class_files": 18,
            "jar_files": 1,
            "test_analysis": {
                "pass_rate": 96.3,
                "total_tests": 214,
                "passed_tests": 206,
                "failed_tests": 3,
                "error_tests": 0,
                "skipped_tests": 5,
                "report_files_count": 1,
            },
        },
    }

    monkeypatch.setattr(tool, "_validate_context_prerequisites", lambda: {"valid": True})
    monkeypatch.setattr(
        tool,
        "_verify_execution_history",
        lambda status, summary: ("success", actual_accomplishments),
    )
    monkeypatch.setattr(
        tool,
        "_collect_execution_metrics",
        lambda: {
            "total_actions": 1,
            "successful_actions": 1,
            "failed_actions": 0,
            "success_rate": 100,
            "tools_used": {},
            "tool_failures": {},
            "phases": {
                "clone": {"status": True},
                "analyze": {"status": True},
                "build": {"status": True},
                "test": {"status": False},
            },
        },
    )
    monkeypatch.setattr(
        tool,
        "_get_project_info",
        lambda: {
            "directory": "/workspace/demo",
            "type": "Maven Java Project",
            "build_system": "Maven",
        },
    )
    monkeypatch.setattr(
        tool,
        "_save_markdown_report",
        lambda markdown, timestamp, filename: saved_markdown.setdefault("content", markdown),
    )

    result = tool.execute(
        action="generate",
        summary="done",
        status="success",
        evidence_status="partial",
        test_stats={"executed": 214, "passed": 206, "failed": 3, "skipped": 5},
        conflicts=["test_failures_detected"],
        evidence_refs=["/workspace/demo/target/surefire-reports/TEST-demo.xml"],
    )

    assert result.success is True
    assert result.status == EvidenceStatus.PARTIAL
    assert "Result: PARTIAL" in result.raw_data["full_report"]
    assert "96.3% pass rate" in result.raw_data["full_report"]
    assert "3 failed" in result.raw_data["full_report"]
    assert "test_failures_detected" in result.raw_data["full_report"]
    assert "/workspace/demo/target/surefire-reports/TEST-demo.xml" in result.raw_data["full_report"]
    assert "**Result:** ⚠️ PARTIAL" in saved_markdown["content"]
    assert "96.3% pass rate" in saved_markdown["content"]
    assert "3 failed" in saved_markdown["content"]
    assert "test_failures_detected" in saved_markdown["content"]
    assert "/workspace/demo/target/surefire-reports/TEST-demo.xml" in saved_markdown["content"]
    assert "**Result:** ✅ SUCCESS" not in saved_markdown["content"]


def test_report_failed_legacy_status_maps_to_blocked(monkeypatch):
    tool = ReportTool()

    monkeypatch.setattr(tool, "_validate_context_prerequisites", lambda: {"valid": True})
    monkeypatch.setattr(
        tool,
        "_generate_comprehensive_report",
        lambda summary, status, details, **kwargs: (
            "# Full Report",
            "fail",
            "setup-report-test.md",
            {"build_success": False, "test_success": False},
            {"status": "fail"},
        ),
    )
    monkeypatch.setattr(
        tool,
        "_generate_condensed_log_output",
        lambda verified_status,
        report_filename,
        actual_accomplishments,
        report_snapshot: "condensed",
    )

    result = tool.execute(action="generate", summary="blocked", status="fail")

    assert result.success is True
    assert result.status == EvidenceStatus.BLOCKED
    assert result.metadata["evidence_status"] == "blocked"
    assert result.raw_data["evidence_status"] == "blocked"


def test_report_uses_validator_evidence_defaults_when_kwargs_missing(monkeypatch):
    tool = ReportTool()
    saved_markdown = {}

    actual_accomplishments = {
        "repository_cloned": True,
        "build_success": True,
        "test_success": False,
        "physical_validation": {
            "build_status": {
                "evidence_status": "success",
                "conflicts": [],
                "evidence_refs": ["/workspace/demo/target/demo-1.0.jar"],
            },
            "test_status": {
                "evidence_status": "partial",
                "test_stats": {
                    "executed": 214,
                    "passed": 206,
                    "failed": 3,
                    "skipped": 5,
                },
                "conflicts": ["test_failures_detected"],
                "evidence_refs": ["/workspace/demo/target/surefire-reports/TEST-demo.xml"],
            },
            "test_analysis": {
                "pass_rate": 96.3,
                "total_tests": 214,
                "passed_tests": 206,
                "failed_tests": 3,
                "error_tests": 0,
                "skipped_tests": 5,
            },
        },
    }

    monkeypatch.setattr(tool, "_validate_context_prerequisites", lambda: {"valid": True})
    monkeypatch.setattr(
        tool,
        "_verify_execution_history",
        lambda status, summary: ("success", actual_accomplishments),
    )
    monkeypatch.setattr(
        tool,
        "_collect_execution_metrics",
        lambda: {
            "phases": {
                "clone": {"status": True},
                "analyze": {"status": True},
                "build": {"status": True},
                "test": {"status": False},
            }
        },
    )
    monkeypatch.setattr(
        tool,
        "_get_project_info",
        lambda: {
            "directory": "/workspace/demo",
            "type": "Maven Java Project",
            "build_system": "Maven",
        },
    )
    monkeypatch.setattr(
        tool,
        "_save_markdown_report",
        lambda markdown, timestamp, filename: saved_markdown.setdefault("content", markdown),
    )

    result = tool.execute(action="generate", summary="done", status="success")

    assert result.success is True
    assert result.status == EvidenceStatus.PARTIAL
    assert result.metadata["verified_status"] == "success"
    assert result.metadata["evidence_status"] == "partial"
    assert "Result: PARTIAL" in result.raw_data["full_report"]
    assert "96.3% pass rate" in result.raw_data["full_report"]
    assert "3 failed" in result.raw_data["full_report"]
    assert "test_failures_detected" in result.raw_data["full_report"]
    assert "/workspace/demo/target/surefire-reports/TEST-demo.xml" in result.raw_data["full_report"]
    assert "**Result:** ⚠️ PARTIAL" in saved_markdown["content"]


def test_ordinary_success_report_does_not_render_empty_test_stats(monkeypatch):
    tool = ReportTool()
    saved_markdown = {}

    actual_accomplishments = {
        "repository_cloned": True,
        "build_success": True,
        "test_success": True,
        "physical_validation": {},
    }

    monkeypatch.setattr(tool, "_validate_context_prerequisites", lambda: {"valid": True})
    monkeypatch.setattr(
        tool,
        "_verify_execution_history",
        lambda status, summary: ("success", actual_accomplishments),
    )
    monkeypatch.setattr(
        tool,
        "_collect_execution_metrics",
        lambda: {
            "phases": {
                "clone": {"status": True},
                "analyze": {"status": True},
                "build": {"status": True},
                "test": {"status": True},
            }
        },
    )
    monkeypatch.setattr(
        tool,
        "_get_project_info",
        lambda: {
            "directory": "/workspace/demo",
            "type": "Generic Project",
            "build_system": "Unknown",
        },
    )
    monkeypatch.setattr(
        tool,
        "_save_markdown_report",
        lambda markdown, timestamp, filename: saved_markdown.setdefault("content", markdown),
    )

    result = tool.execute(action="generate", summary="done", status="success")

    assert result.success is True
    assert result.status == EvidenceStatus.SUCCESS
    assert result.raw_data["test_stats"] is None
    assert "0 / 0 passed" not in result.output
    assert "0 / 0 passed" not in result.raw_data["full_report"]
    assert "0 / 0 passed" not in saved_markdown["content"]
    assert "Tests: 0 / 0 passed" not in result.raw_data["full_report"]
    assert "**Tests:** 0 / 0 passed" not in saved_markdown["content"]
    assert "Result: SUCCESS" not in result.raw_data["full_report"]


def test_invalid_explicit_evidence_status_is_unknown_without_success_fallback(monkeypatch):
    tool = ReportTool()

    actual_accomplishments = {
        "repository_cloned": True,
        "build_success": True,
        "test_success": True,
        "physical_validation": {},
    }

    monkeypatch.setattr(tool, "_validate_context_prerequisites", lambda: {"valid": True})
    monkeypatch.setattr(
        tool,
        "_verify_execution_history",
        lambda status, summary: ("success", actual_accomplishments),
    )
    monkeypatch.setattr(
        tool,
        "_collect_execution_metrics",
        lambda: {
            "phases": {
                "clone": {"status": True},
                "analyze": {"status": True},
                "build": {"status": True},
                "test": {"status": True},
            }
        },
    )
    monkeypatch.setattr(
        tool,
        "_get_project_info",
        lambda: {
            "directory": "/workspace/demo",
            "type": "Generic Project",
            "build_system": "Unknown",
        },
    )
    monkeypatch.setattr(tool, "_save_markdown_report", lambda markdown, timestamp, filename: None)

    result = tool.execute(
        action="generate",
        summary="done",
        status="success",
        evidence_status="bogus",
    )

    assert result.success is True
    assert result.status == EvidenceStatus.UNKNOWN
    assert result.metadata["evidence_status"] == "unknown"
    assert result.raw_data["evidence_status"] == "unknown"


def test_report_tool_marks_final_report_task_completed(monkeypatch):
    context_manager = FakeReportContextManager()
    tool = ReportTool(context_manager=context_manager)

    monkeypatch.setattr(tool, "_validate_context_prerequisites", lambda: {"valid": True})
    monkeypatch.setattr(
        tool,
        "_generate_comprehensive_report",
        lambda summary, status, details, **kwargs: (
            "# Full Report",
            "success",
            "setup-report-test.md",
            {
                "build_success": True,
                "test_success": True,
                "physical_validation": {
                    "test_analysis": {
                        "pass_rate": 100,
                        "total_tests": 1,
                        "passed_tests": 1,
                    }
                },
            },
            {"status": "success"},
        ),
    )
    monkeypatch.setattr(
        tool,
        "_generate_condensed_log_output",
        lambda verified_status,
        report_filename,
        actual_accomplishments,
        report_snapshot: "condensed",
    )

    result = tool.execute(action="generate", summary="done", status="success")

    final_task = context_manager.trunk.todo_list[1]
    assert result.success is True
    assert final_task.status == TaskStatus.COMPLETED
    assert final_task.completed_at is not None
    assert final_task.notes == "Final setup report generated."
    assert "setup-report-test.md" in final_task.key_results
    assert context_manager.saved_trunk is context_manager.trunk


def test_markdown_report_includes_runtime_env_overlay_evidence():
    overlay_json = """
    {
      "version": 1,
      "tools": {
        "maven": {
          "active": "/opt/apache-maven-3.9.9/bin/mvn",
          "candidates": {
            "/opt/apache-maven-3.9.9/bin/mvn": {
              "version": "3.9.9",
              "source": "agent_registered",
              "env": {},
              "path_prepend": ["/opt/apache-maven-3.9.9/bin"]
            }
          },
          "blocked": [
            {
              "executable": "/usr/bin/mvn",
              "version": "3.6.3",
              "requirement": "[3.9,)",
              "reason": "Project requires Maven 3.9+",
              "source": "build_error"
            }
          ]
        },
        "gradle": {
          "candidates": {},
          "blocked": [
            {
              "executable": "/usr/bin/gradle",
              "version": "7.4",
              "requirement": ">=8",
              "reason": "Wrapper requires Gradle 8+",
              "source": "build_error"
            }
          ]
        }
      }
    }
    """
    report = _generate_report_with_overlay(overlay_json)

    assert "## Runtime Environment Overlay Evidence" in report
    assert "runtime command evidence, not project source configuration" in report
    assert "| maven | `/opt/apache-maven-3.9.9/bin/mvn` | 3.9.9 | agent_registered |" in report
    assert "| maven | `/usr/bin/mvn` | 3.6.3 | [3.9,) | Project requires Maven 3.9+ | build_error |" in report
    assert "| gradle | `/usr/bin/gradle` | 7.4 | >=8 | Wrapper requires Gradle 8+ | build_error |" in report


def test_markdown_report_skips_inactive_only_runtime_env_overlay_evidence():
    overlay_json = """
    {
      "version": 1,
      "tools": {
        "maven": {
          "candidates": {
            "/opt/apache-maven-3.9.9/bin/mvn": {
              "version": "3.9.9",
              "source": "agent_registered",
              "env": {},
              "path_prepend": ["/opt/apache-maven-3.9.9/bin"]
            }
          },
          "blocked": []
        }
      }
    }
    """

    report = _generate_report_with_overlay(overlay_json)

    assert "## Runtime Environment Overlay Evidence" not in report
    assert "No active overlay executables recorded" not in report


def test_markdown_report_caps_blocked_runtime_env_overlay_candidates():
    blocked = []
    for index in range(7):
        blocked.append(
            {
                "executable": f"/usr/bin/mvn-{index}",
                "version": f"3.6.{index}",
                "requirement": "[3.9,)",
                "reason": f"Project requires Maven 3.9+ reason-{index}",
                "source": "build_error",
            }
        )
    overlay_json = {
        "version": 1,
        "tools": {
            "maven": {
                "candidates": {},
                "blocked": blocked,
            }
        },
    }

    report = _generate_report_with_overlay(json.dumps(overlay_json))

    assert "Project requires Maven 3.9+ reason-0" in report
    assert "Project requires Maven 3.9+ reason-4" in report
    assert "Project requires Maven 3.9+ reason-5" not in report
    assert "Project requires Maven 3.9+ reason-6" not in report
    assert "+2 more" in report


def test_markdown_report_truncates_long_blocked_runtime_env_overlay_reasons():
    long_reason = "Project requires Maven 3.9+ " + ("because " * 40)
    overlay_json = {
        "version": 1,
        "tools": {
            "maven": {
                "candidates": {},
                "blocked": [
                    {
                        "executable": "/usr/bin/mvn",
                        "version": "3.6.3",
                        "requirement": "[3.9,)",
                        "reason": long_reason,
                        "source": "build_error",
                    }
                ],
            }
        },
    }

    report = _generate_report_with_overlay(json.dumps(overlay_json))

    assert "Project requires Maven 3.9+" in report
    assert long_reason not in report
    assert "..." in report


def test_markdown_report_skips_unreadable_runtime_env_overlay():
    report = _generate_report_with_overlay(
        unreadable_paths={"/workspace/.setup_agent/env_overlay.json"}
    )

    assert "## Runtime Environment Overlay Evidence" not in report
    assert "**Task completed. Setup Agent has finished.**" in report


def test_replay_delegates_environment_handling_to_docker_orchestrator():
    orchestrator = FakeReplayOrchestrator()
    tracker = CommandTracker(docker_orchestrator=orchestrator, project_name="demo")
    tracker.track_test_command("mvn test", "maven", working_dir="/workspace/demo")

    result = tracker.replay_all_tests()

    assert result["success"] is True
    assert orchestrator.commands == ["cd /workspace/demo && mvn test"]


def test_report_header_tests_line_uses_snapshot_stats_not_evidence_stats():
    """The header must consume the same physically-validated numbers as the
    dashboard. 06-10 eval: every header contradicted its own dashboard
    (commons-cli: header '977/977 passed, 100%' vs dashboard 420/430)."""
    tool = ReportTool()
    snapshot = {
        "status": {
            "tests_total": 430,
            "tests_passed": 420,
            "tests_failed": 0,
            "tests_errors": 0,
            "tests_skipped": 10,
            "static_test_count": 460,
            "pass_pct": 97.7,
        },
        "evidence_result": {
            "status": "success",
            # Model-supplied stats disagree (raw surefire totals) — must lose.
            "test_stats": {"executed": 977, "passed": 977, "failed": 0, "skipped": 61},
            "conflicts": [],
            "evidence_refs": ["output_x"],
        },
    }

    lines = tool._render_enhanced_header(
        "2026-06-10 12:00:00",
        "success",
        {"directory": "/workspace/demo", "type": "Maven Java Project", "build_system": "Maven"},
        snapshot=snapshot,
    )

    tests_lines = [l for l in lines if l.startswith("**Tests:**")]
    assert tests_lines, lines
    assert "420" in tests_lines[0] and "430" in tests_lines[0], tests_lines[0]
    assert "977" not in tests_lines[0], tests_lines[0]


def test_report_result_header_matches_kernel_verdict():
    """Same inputs -> report Result line equals the kernel verdict (round-5
    iceberg: report PARTIAL vs CLI success can no longer happen)."""
    from sag.verdict import run_verdict

    tool = ReportTool()
    snapshot = {
        "status": {"overall": "success", "tests_total": 2913, "tests_passed": 2893,
                   "tests_failed": 15, "tests_errors": 0, "tests_skipped": 5,
                   "pass_pct": 99.3},
        "evidence_result": {"status": "success", "conflicts": ["test_report_parse_error"],
                            "test_stats": None, "evidence_refs": []},
    }
    lines = tool._render_enhanced_header(
        "2026-06-12 12:00:00", "success",
        {"directory": "/workspace/x", "type": "Gradle Java Project", "build_system": "Gradle"},
        snapshot=snapshot,
    )
    result_lines = [l for l in lines if l.startswith("**Result:**")]
    expected = run_verdict("success", "success", ["test_report_parse_error"])
    assert expected == "partial"
    assert "PARTIAL" in result_lines[0].upper()


class PhaseTrunkContextManager:
    """Context manager whose trunk mirrors a phase-mode run: one phase_<name>
    task per phase, FAILED where the machine recorded a block."""

    def __init__(self, blocked=()):
        tasks = [
            Task(
                id=f"phase_{name}",
                description=f"Phase: {name}",
                status=TaskStatus.FAILED if name in set(blocked) else TaskStatus.COMPLETED,
            )
            for name in ("provision", "analyze", "build", "test", "report")
        ]
        self.trunk = TrunkContext(
            context_id="trunk_phase",
            goal="Set up demo",
            project_url="https://example.test/demo.git",
            project_name="demo",
            todo_list=tasks,
        )

    def load_trunk_context(self):
        return self.trunk


def _all_green_kernel_snapshot():
    return {
        "status": {"overall": "success", "tests_total": 100, "tests_passed": 100,
                   "tests_failed": 0, "tests_errors": 0, "tests_skipped": 0,
                   "pass_pct": 100.0},
        "evidence_result": {"status": "success", "conflicts": [],
                            "test_stats": None, "evidence_refs": ["output_x"]},
    }


def test_report_result_header_caps_on_blocked_trunk_phase():
    """The header's kernel call must consume the phase-machine outcome too —
    a machine-capped run (blocked phase_* trunk task) with green physical
    evidence rendered '**Result:** ✅ SUCCESS' while the CLI banner said
    verdict=partial/failed (round-6 review)."""
    tool = ReportTool(context_manager=PhaseTrunkContextManager(blocked={"test"}))

    assert tool._snapshot_kernel_verdict(_all_green_kernel_snapshot()) == "partial"

    lines = tool._render_enhanced_header(
        "2026-06-12 12:00:00", "success",
        {"directory": "/workspace/demo", "type": "Maven Java Project", "build_system": "Maven"},
        snapshot=_all_green_kernel_snapshot(),
    )
    result_lines = [l for l in lines if l.startswith("**Result:**")]
    assert result_lines and "PARTIAL" in result_lines[0].upper(), result_lines


def test_report_result_header_blocked_build_phase_is_failed():
    tool = ReportTool(context_manager=PhaseTrunkContextManager(blocked={"build"}))

    assert tool._snapshot_kernel_verdict(_all_green_kernel_snapshot()) == "failed"


def test_report_kernel_verdict_abstains_without_phase_tasks():
    """Non-phase runs (sag run --task, legacy) have no phase_* trunk tasks:
    the machine input abstains and physical evidence still rules."""
    tool = ReportTool(context_manager=FakeReportContextManager())

    assert tool._snapshot_kernel_verdict(_all_green_kernel_snapshot()) == "success"


def test_condensed_log_output_matches_kernel_verdict():
    """Contract mirror of test_report_result_header_matches_kernel_verdict for
    the condensed log output (round-6 review): the SAME snapshot printed
    '🎯 SETUP COMPLETED: ✅ SUCCESS' and 'Project ready for development...🎉'
    while the report header said '**Result:** ⚠️ PARTIAL'. Banner and Next
    line must read the kernel verdict, never overall/verified_status."""
    tool = ReportTool()
    snapshot = {
        "status": {"overall": "success", "tests_total": 2913, "tests_passed": 2893,
                   "tests_failed": 15, "tests_errors": 0, "tests_skipped": 5,
                   "pass_pct": 99.3},
        "project": {"type": "Gradle Java Project", "build_system": "Gradle"},
        "phases": {"clone": True, "build": True, "test": True},
        "evidence_result": {"status": "success", "conflicts": ["test_report_parse_error"],
                            "test_stats": None, "evidence_refs": []},
    }

    output = tool._generate_condensed_log_output(
        "success", "setup-report-test.md", {"build_success": True}, snapshot
    )

    banner = output.splitlines()[0]
    assert "PARTIAL" in banner.upper(), banner
    assert "✅ SUCCESS" not in banner, banner
    assert "ready for development" not in output, "Next line must not announce 🎉 on partial"


def test_condensed_log_output_kernel_success_keeps_celebration():
    tool = ReportTool()
    snapshot = {
        "status": {"overall": "success", "tests_total": 100, "tests_passed": 100,
                   "tests_failed": 0, "tests_errors": 0, "tests_skipped": 0,
                   "pass_pct": 100.0},
        "project": {"type": "Maven Java Project", "build_system": "Maven"},
        "phases": {"clone": True, "build": True, "test": True},
        "evidence_result": {"status": "success", "conflicts": [],
                            "test_stats": None, "evidence_refs": []},
    }

    output = tool._generate_condensed_log_output(
        "success", "setup-report-test.md", {"build_success": True}, snapshot
    )

    assert "✅ SUCCESS" in output.splitlines()[0]
    assert "ready for development" in output


def test_report_header_falls_back_to_evidence_stats_without_snapshot_stats():
    tool = ReportTool()
    snapshot = {
        "status": {},
        "evidence_result": {
            "status": "partial",
            "test_stats": {"executed": 10, "passed": 8, "failed": 2, "skipped": 0},
            "conflicts": [],
            "evidence_refs": ["output_y"],
        },
    }

    lines = tool._render_enhanced_header(
        "2026-06-10 12:00:00",
        "partial",
        {"directory": "/workspace/demo", "type": "Maven Java Project", "build_system": "Maven"},
        snapshot=snapshot,
    )

    tests_lines = [l for l in lines if l.startswith("**Tests:**")]
    assert tests_lines and "8" in tests_lines[0]
