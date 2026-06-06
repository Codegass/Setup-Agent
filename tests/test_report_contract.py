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
    def __init__(self, files=None):
        self.files = files or {}

    def read_file(self, path):
        return {"success": True, "content": self.files.get(path, ""), "exit_code": 0}


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
    tool = ReportTool(
        docker_orchestrator=FakeReportOverlayOrchestrator(
            {"/workspace/.setup_agent/env_overlay.json": overlay_json}
        )
    )

    report = tool._generate_markdown_report(
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

    assert "## Runtime Environment Overlay Evidence" in report
    assert "runtime command evidence, not project source configuration" in report
    assert "| maven | `/opt/apache-maven-3.9.9/bin/mvn` | 3.9.9 | agent_registered |" in report
    assert "| maven | `/usr/bin/mvn` | 3.6.3 | [3.9,) | Project requires Maven 3.9+ | build_error |" in report
    assert "| gradle | `/usr/bin/gradle` | 7.4 | >=8 | Wrapper requires Gradle 8+ | build_error |" in report


def test_replay_delegates_environment_handling_to_docker_orchestrator():
    orchestrator = FakeReplayOrchestrator()
    tracker = CommandTracker(docker_orchestrator=orchestrator, project_name="demo")
    tracker.track_test_command("mvn test", "maven", working_dir="/workspace/demo")

    result = tracker.replay_all_tests()

    assert result["success"] is True
    assert orchestrator.commands == ["cd /workspace/demo && mvn test"]
