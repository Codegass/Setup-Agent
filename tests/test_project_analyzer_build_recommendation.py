"""ProjectAnalyzer._recommend_build_approach — pick the real build target.

Bigtop's root pom is packaging=pom aggregating Groovy/Gradle modules, and its
modules are declared inside a profile (so the parsed <modules> list is empty).
The analyzer must still find the Groovy source module and recommend building it,
not fall back to compiling the empty root.
"""

import re

from sag.tools.internal.project_analyzer import ProjectAnalyzerTool


class FakeOrchestrator:
    """Answers `test -e` existence probes, the packaging grep, and the source-dir
    `find` used to locate compilable modules."""

    def __init__(self, paths, packaging="jar", source_dirs=()):
        self.paths = set(paths)
        self.packaging = packaging
        self.source_dirs = list(source_dirs)

    def execute_command(self, command, **kwargs):
        if command.startswith("find ") and "src/main" in command:
            return {"success": True, "output": "\n".join(self.source_dirs), "exit_code": 0}
        m = re.search(r"test -e (\S+)", command)
        if m:
            return {
                "success": True,
                "output": "yes" if m.group(1) in self.paths else "no",
                "exit_code": 0,
            }
        if command.startswith("grep -m1 '<packaging>'"):
            return {
                "success": True,
                "output": f"<packaging>{self.packaging}</packaging>",
                "exit_code": 0,
            }
        return {"success": True, "output": "", "exit_code": 0}


def _rec(paths, analysis, packaging="jar", source_dirs=(), path="/workspace/p"):
    orch = FakeOrchestrator(paths, packaging, source_dirs)
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orch)
    return analyzer._recommend_build_approach(path, analysis)


def test_plain_maven_module_compiles_at_root():
    rec = _rec(
        {"/workspace/p/pom.xml", "/workspace/p/src/main/java"},
        {"build_system": "maven"},
        packaging="jar",
    )
    assert rec["build_system"] == "maven"
    assert rec["goal"] == "compile"
    assert rec["build_root"] == "/workspace/p"
    assert rec["is_aggregator_only"] is False


def test_aggregator_with_declared_modules_builds_reactor_at_root():
    rec = _rec(
        {"/workspace/bigtop/pom.xml"},
        {"build_system": "maven", "maven_modules": ["bigtop-test-framework", "bigtop-tests"]},
        packaging="pom",
        source_dirs=["/workspace/bigtop/bigtop-test-framework/src/main/groovy"],
        path="/workspace/bigtop",
    )
    assert rec["build_system"] == "maven"
    assert rec["goal"] == "install"  # Groovy -> install, not bare compile
    assert rec["build_root"] == "/workspace/bigtop"  # reactor declares the modules
    assert any(m["lang"] == "groovy" for m in rec["source_modules"])


def test_aggregator_with_profile_gated_modules_targets_source_module_directly():
    # The real Bigtop shape: root <modules> is empty (profile-gated), but a Groovy
    # source module exists. Building the root compiles nothing, so target the module.
    rec = _rec(
        {"/workspace/bigtop/pom.xml"},
        {"build_system": "maven"},  # no maven_modules parsed
        packaging="pom",
        source_dirs=["/workspace/bigtop/bigtop-test-framework/src/main/groovy"],
        path="/workspace/bigtop",
    )
    assert rec["build_system"] == "maven"
    assert rec["goal"] == "install"
    assert rec["build_root"] == "/workspace/bigtop/bigtop-test-framework"
    assert rec["is_aggregator_only"] is False


def test_aggregator_with_no_source_modules_but_gradle_recommends_gradle():
    rec = _rec(
        {"/workspace/p/pom.xml", "/workspace/p/gradlew", "/workspace/p/build.gradle"},
        {"build_system": "maven", "maven_modules": ["docs"]},
        packaging="pom",
        source_dirs=[],
    )
    assert rec["build_system"] == "gradle"
    assert rec["goal"] == "build"
    assert rec["has_gradle"] is True
    assert rec["is_aggregator_only"] is False


def test_pure_aggregator_meta_project_is_flagged_blocked():
    rec = _rec(
        {"/workspace/p/pom.xml"},
        {"build_system": "maven", "maven_modules": ["bom"]},
        packaging="pom",
        source_dirs=[],
    )
    assert rec["is_aggregator_only"] is True
    assert "meta-project" in rec["rationale"]


def test_gradle_only_project_recommends_gradle_build():
    rec = _rec(
        {"/workspace/p/gradlew", "/workspace/p/build.gradle"},
        {"build_system": "gradle"},
    )
    assert rec["build_system"] == "gradle"
    assert rec["goal"] == "build"
