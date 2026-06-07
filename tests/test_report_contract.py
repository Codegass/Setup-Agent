import json

from sag.agent.context_manager import Task, TaskStatus, TrunkContext
from sag.tools.command_tracker import CommandTracker
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


def test_report_tool_returns_full_report_in_raw_data(monkeypatch):
    tool = ReportTool()

    monkeypatch.setattr(tool, "_validate_context_prerequisites", lambda: {"valid": True})
    monkeypatch.setattr(
        tool,
        "_generate_comprehensive_report",
        lambda summary, status, details: (
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


def test_report_tool_marks_final_report_task_completed(monkeypatch):
    context_manager = FakeReportContextManager()
    tool = ReportTool(context_manager=context_manager)

    monkeypatch.setattr(tool, "_validate_context_prerequisites", lambda: {"valid": True})
    monkeypatch.setattr(
        tool,
        "_generate_comprehensive_report",
        lambda summary, status, details: (
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
