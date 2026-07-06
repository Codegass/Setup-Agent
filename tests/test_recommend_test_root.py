"""_recommend_test_approach — a Maven reactor is tested at its root, not a leaf.

httpcomponents-client has 5 sibling modules each with one src/test dir, so the
dominant-cluster heuristic tied them all at 1 and picked an arbitrary leaf
(httpclient5-fluent) -> `mvn test` ran 16 of 1856 tests. When the build is already
the reactor root and the tests are the same build system, tests must run at the
root. The Bigtop case (tests in a foreign Gradle subtree beside a leaf Maven build)
must still target that cluster.
"""

import re

from sag.tools.internal.project_analyzer import ProjectAnalyzerTool


class FakeOrch:
    def __init__(self, test_dirs, existing_paths):
        self.test_dirs = list(test_dirs)
        self.existing = set(existing_paths)

    def execute_command(self, command, **kwargs):
        if command.startswith("find ") and "src/test" in command:
            return {"success": True, "output": "\n".join(self.test_dirs), "exit_code": 0}
        m = re.search(r"test -e (\S+)", command)
        if m:
            return {"success": True, "output": "yes" if m.group(1) in self.existing else "no", "exit_code": 0}
        return {"success": True, "output": "", "exit_code": 0}


def _test_rec(project_path, build_rec, test_dirs, existing):
    analyzer = ProjectAnalyzerTool(docker_orchestrator=FakeOrch(test_dirs, existing))
    analyzer._recommend_test_approach(project_path, build_rec)
    return build_rec


def test_maven_reactor_tests_at_root_not_leaf():
    p = "/workspace/p"
    mods = ["httpclient5", "httpclient5-observation", "httpclient5-fluent", "httpclient5-cache"]
    rec = _test_rec(
        p,
        {"build_system": "maven", "build_root": p},
        test_dirs=[f"{p}/{m}/src/test/java" for m in mods],
        existing={f"{p}/{m}/pom.xml" for m in mods},  # each module maven, no gradle
    )
    assert rec["test_root"] == p
    assert rec["test_system"] == "maven"


def test_gradle_reactor_tests_at_root():
    # Generality across build systems: a Gradle multi-project built at its root is
    # also tested at the root (build_system == test_system == gradle).
    p = "/workspace/g"
    rec = _test_rec(
        p,
        {"build_system": "gradle", "build_root": p},
        test_dirs=[f"{p}/{m}/src/test/java" for m in ["core", "web", "cli"]],
        existing={f"{p}/{m}/build.gradle" for m in ["core", "web", "cli"]},
    )
    assert rec["test_root"] == p
    assert rec["test_system"] == "gradle"


def test_foreign_gradle_cluster_still_wins_when_build_is_a_leaf():
    p = "/workspace/bigtop"
    rec = _test_rec(
        p,
        {"build_system": "maven", "build_root": f"{p}/bigtop-test-framework"},  # leaf != root
        test_dirs=[
            f"{p}/bigtop-data-generators/g1/src/test/groovy",
            f"{p}/bigtop-data-generators/g2/src/test/groovy",
        ],
        existing={f"{p}/bigtop-data-generators/build.gradle"},
    )
    assert rec["test_root"] == f"{p}/bigtop-data-generators"
    assert rec["test_system"] == "gradle"
