"""Tests for ProjectAnalyzer's fallback execution plan.

Regression coverage for the case that made apache/beam thrash: when the main
analysis fails to record any build files, the fallback used to treat the project
as "completely unknown" and emit a "manually explore" task (which the agent then
re-added in a loop). It now re-scans the project root for build files first.
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


def _descriptions(plan):
    return [str(step.get("description", "")).lower() for step in plan]


def test_fallback_redetects_gradle_kts_instead_of_unknown():
    # Mirrors apache/beam: analysis recorded only README (its root build script
    # is build.gradle.kts, which the legacy build.gradle check missed).
    orch = FakeOrchestrator(
        {
            "/workspace/beam/build.gradle.kts",
            "/workspace/beam/settings.gradle.kts",
            "/workspace/beam/README.md",
        }
    )
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orch)

    plan = analyzer._generate_three_step_fallback_plan(
        {"project_path": "/workspace/beam", "existing_files": ["README.md"]}
    )

    descriptions = _descriptions(plan)
    assert any("gradle" in d for d in descriptions), descriptions
    assert not any("manually explore" in d for d in descriptions), descriptions
    assert any(step.get("core_step") == "build" for step in plan)


def test_fallback_redetects_maven_when_analysis_missed_it():
    orch = FakeOrchestrator({"/workspace/proj/pom.xml"})
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orch)

    plan = analyzer._generate_three_step_fallback_plan(
        {"project_path": "/workspace/proj", "existing_files": []}
    )

    descriptions = _descriptions(plan)
    assert any("maven" in d for d in descriptions), descriptions
    assert not any("manually explore" in d for d in descriptions), descriptions


def test_fallback_stays_unknown_when_no_build_files_exist():
    # A genuinely unknown project must still get the explore fallback.
    orch = FakeOrchestrator(set())
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orch)

    plan = analyzer._generate_three_step_fallback_plan(
        {"project_path": "/workspace/foo", "existing_files": []}
    )

    descriptions = _descriptions(plan)
    assert any("manually explore" in d for d in descriptions), descriptions


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
