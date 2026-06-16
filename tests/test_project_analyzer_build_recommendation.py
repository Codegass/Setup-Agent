"""ProjectAnalyzer._recommend_build_approach — pick the real build target.

Bigtop's root pom is packaging=pom aggregating Groovy/Gradle modules, so
`mvn compile` at the root is BUILD SUCCESS with zero classes. The analyzer must
recommend the reactor/module + goal the build phase should actually run.
"""

import re

from sag.tools.internal.project_analyzer import ProjectAnalyzerTool


class FakeOrchestrator:
    """Answers `test -e <path>` existence probes and the root packaging grep."""

    def __init__(self, paths, packaging="jar"):
        self.paths = set(paths)
        self.packaging = packaging

    def execute_command(self, command, **kwargs):
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


def _rec(paths, analysis, packaging="jar", path="/workspace/p"):
    analyzer = ProjectAnalyzerTool(docker_orchestrator=FakeOrchestrator(paths, packaging))
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


def test_aggregator_over_groovy_modules_recommends_install():
    # Bigtop shape: packaging=pom root, a Groovy source module in the reactor.
    rec = _rec(
        {
            "/workspace/bigtop/pom.xml",
            "/workspace/bigtop/bigtop-test-framework/src/main/groovy",
        },
        {"build_system": "maven", "maven_modules": ["bigtop-test-framework", "bigtop-tests"]},
        packaging="pom",
        path="/workspace/bigtop",
    )
    assert rec["build_system"] == "maven"
    # Groovy compiles via a plugin bound to a later phase -> install, not compile.
    assert rec["goal"] == "install"
    assert any(m["lang"] == "groovy" for m in rec["source_modules"])
    assert rec["is_aggregator_only"] is False


def test_aggregator_with_no_source_modules_but_gradle_recommends_gradle():
    rec = _rec(
        {"/workspace/p/pom.xml", "/workspace/p/gradlew", "/workspace/p/build.gradle"},
        {"build_system": "maven", "maven_modules": ["docs"]},
        packaging="pom",
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
