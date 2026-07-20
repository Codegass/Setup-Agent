"""Tests for ProjectAnalyzer's main-path build-system detection.

Regression coverage for beam 06-10, where the primary analysis path reported
'unknown' x853 for a project whose root build script is build.gradle.kts (the
legacy build.gradle check missed it). Detection must recognize the Kotlin DSL
root and wrapper-only Gradle projects, and an 'unknown' verdict must surface
the evidence it checked.
"""

import re

from sag.tools.internal.project_analyzer import ProjectAnalyzerTool


class FakeOrchestrator:
    """Minimal orchestrator that answers `test -f <path>` build-file probes."""

    def __init__(self, existing_paths):
        self.existing = set(existing_paths)

    def execute_command(self, command, **kwargs):
        match = re.search(r"test -f (\S+)", command)
        if match:
            path = match.group(1)
            output = "exists" if path in self.existing else "missing"
            return {"success": True, "output": output, "exit_code": 0}
        return {"success": True, "output": "", "exit_code": 0}


# --- main-path detection (beam 06-10: 'unknown' x853 from the PRIMARY path) --


def test_main_detection_recognizes_kotlin_dsl_root():
    orch = FakeOrchestrator(
        {
            "/workspace/beam/build.gradle.kts",
            "/workspace/beam/settings.gradle.kts",
            "/workspace/beam/gradlew",
            "/workspace/beam/README.md",
        }
    )
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orch)

    structure = analyzer._analyze_project_structure("/workspace/beam")

    assert structure["build_system"] == "Gradle"
    assert structure["project_type"] == "Java"


def test_main_detection_recognizes_wrapper_only_gradle():
    orch = FakeOrchestrator({"/workspace/p/gradlew", "/workspace/p/README.md"})
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orch)

    structure = analyzer._analyze_project_structure("/workspace/p")

    assert structure["build_system"] == "Gradle"


def test_unknown_detection_reports_its_evidence():
    """An unknown verdict must show what was checked (and the root listing)
    so the model can judge it instead of trusting a bare 'unknown'."""
    orch = FakeOrchestrator({"/workspace/x/README.md"})
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orch)

    structure = analyzer._analyze_project_structure("/workspace/x")

    assert structure["project_type"] == "unknown"
    assert "pom.xml" in structure.get("detection_checked", [])
    assert "build.gradle.kts" in structure.get("detection_checked", [])
