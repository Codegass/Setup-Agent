# tests/test_root_shape_policy.py
"""Root-shape derivation + manifest persistence (spec §2).

The analyzer DERIVES the root shape from the recommendation it already
computed (it does not re-classify) and persists the phase-1 -> build-tool
handoff manifest:

healthy_reactor          -> build_root == project root AND reactor modules
                            declared; install/test fail-at-end at root
pathological_aggregator  -> build_root is a subdirectory (leaf targeting)
single_module            -> everything else (root build, no reactor modules)
"""

import json

from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.project_analyzer import ProjectAnalyzerTool


class FakeOrch:
    """Answers the analyzer's shell probes from a canned filesystem set and
    captures the manifest heredoc write."""

    def __init__(self, existing_paths, find_output="", test_find_output=""):
        self.existing = set(existing_paths)
        self.find_output = find_output
        self.test_find_output = test_find_output
        self.pom = ""
        self.files = {}

    def execute_command(self, cmd, workdir=None):
        if cmd.startswith("mkdir -p"):
            return {"success": True, "exit_code": 0, "output": ""}
        if "<<" in cmd and REQUIREMENTS_PATH in cmd:  # heredoc manifest write
            body = cmd.split("<<'SAGEOF'\n", 1)[1].rsplit("\nSAGEOF", 1)[0]
            self.files[REQUIREMENTS_PATH] = body
            return {"success": True, "exit_code": 0, "output": ""}
        if cmd.startswith("test -e"):
            path = cmd.split("test -e ", 1)[1].split(" ", 1)[0]
            return {"success": True, "exit_code": 0,
                    "output": "yes" if path in self.existing else "no"}
        if cmd.startswith("find"):
            output = self.test_find_output if "src/test" in cmd else self.find_output
            return {"success": True, "exit_code": 0, "output": output}
        if "pom.xml" in cmd and ("grep" in cmd or "cat" in cmd):
            return {"success": True, "exit_code": 0, "output": self.pom}
        return {"success": True, "exit_code": 0, "output": ""}


def _manifest(analysis, orch, project_path="/workspace/proj"):
    """Run the recommendation flow the analyzer runs, then the manifest hook."""
    tool = ProjectAnalyzerTool.__new__(ProjectAnalyzerTool)  # skip __init__ wiring
    tool.docker_orchestrator = orch
    analysis["build_recommendation"] = tool._recommend_build_approach(project_path, analysis)
    tool._recommend_test_approach(project_path, analysis["build_recommendation"])
    tool._persist_build_requirements(project_path, analysis)
    return json.loads(orch.files[REQUIREMENTS_PATH])


def test_healthy_reactor_manifest_targets_root_install_fail_at_end():
    orch = FakeOrch(
        existing_paths={"/workspace/proj/pom.xml"},
        # one source-bearing module reachable from root
        find_output="/workspace/proj/core/src/main/java\n",
    )
    orch.pom = "<project><packaging>pom</packaging><modules><module>core</module></modules></project>"
    analysis = {
        "maven_modules": ["core"],
        "build_config": {"packaging": "pom"},
        "java_version": "11",
        "java_version_source": "maven-compiler",
        "java_version_enforced": False,
    }
    manifest = _manifest(analysis, orch)
    assert manifest["root_shape"] == "healthy_reactor"
    assert manifest["build_root"] == "/workspace/proj"
    assert manifest["build_goal"] == "install"
    assert manifest["fail_at_end"] is True
    # java requirements ride along on the same handoff
    assert manifest["java_version"] == "11"
    assert manifest["java_version_source"] == "maven-compiler"
    assert manifest["java_version_enforced"] is False


def test_pathological_aggregator_manifest_keeps_leaf_targeting():
    orch = FakeOrch(
        existing_paths={"/workspace/proj/pom.xml"},
        find_output="/workspace/proj/vendor-tools/src/main/groovy\n",
    )
    orch.pom = "<project><packaging>pom</packaging></project>"  # no <modules> (profile-gated)
    analysis = {"maven_modules": [], "build_config": {"packaging": "pom"}}
    manifest = _manifest(analysis, orch)
    assert manifest["root_shape"] == "pathological_aggregator"
    assert manifest["build_root"] == "/workspace/proj/vendor-tools"  # PR #9 leaf path preserved
    assert manifest["fail_at_end"] is False


def test_single_module_manifest_shape():
    orch = FakeOrch(
        existing_paths={"/workspace/proj/pom.xml", "/workspace/proj/src/main/java"},
    )
    orch.pom = "<project><packaging>jar</packaging></project>"
    analysis = {"maven_modules": [], "build_config": {"packaging": "jar"}}
    manifest = _manifest(analysis, orch)
    assert manifest["root_shape"] == "single_module"
    assert manifest["build_root"] == "/workspace/proj"
    assert manifest["fail_at_end"] is False
    assert manifest["test_fail_at_end"] is False
    # absent detection persists honestly as null, not a guess
    assert manifest["java_version"] is None


def test_healthy_reactor_test_targeting_is_root_fail_at_end():
    orch = FakeOrch(
        existing_paths={"/workspace/proj/pom.xml"},
        find_output="/workspace/proj/core/src/main/java\n",
        test_find_output="/workspace/proj/core/src/test/java\n",
    )
    orch.pom = "<project><packaging>pom</packaging><modules><module>core</module></modules></project>"
    analysis = {"maven_modules": ["core"], "build_config": {"packaging": "pom"}}
    manifest = _manifest(analysis, orch)
    assert manifest["test_root"] == "/workspace/proj"
    assert manifest["test_system"] == "maven"
    assert manifest["test_fail_at_end"] is True
