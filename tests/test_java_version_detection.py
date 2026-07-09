"""Detection hardening for Java-version detection (execution-strategy fixes).

Covers spec §1a: reject ${...} property indirection, normalize legacy 1.x
versions, take the lower bound of enforcer ranges — via ONE normalization
path (_normalize_java_version) applied at every capture site in both
project_analyzer and project_setup_tool, including the PR #12
compiler-plugin <source>/<target>/<release> patterns. A rejected capture
must fall through to the next pattern, never clear the search.
"""

import re

from sag.tools.internal.project_analyzer import (
    ENFORCER_JAVA_PATTERN,
    ProjectAnalyzerTool,
    _normalize_java_version,
)
from sag.tools.internal.project_setup_tool import ProjectSetupTool


def test_plain_major_version_passes_through():
    assert _normalize_java_version("17") == "17"
    assert _normalize_java_version(" 11 ") == "11"


def test_legacy_one_dot_versions_normalize_to_major():
    assert _normalize_java_version("1.8") == "8"
    assert _normalize_java_version("1.7") == "7"


def test_property_indirection_is_rejected():
    assert _normalize_java_version("${jdk.version}") is None
    assert _normalize_java_version("${maven.compiler.release}") is None
    # Compiler-plugin form indirection (e.g. <source>${javac.src.version}</source>)
    assert _normalize_java_version("${javac.src.version}") is None


def test_garbage_is_rejected():
    assert _normalize_java_version("") is None
    assert _normalize_java_version(None) is None
    assert _normalize_java_version("banana") is None


def test_enforcer_range_lower_bound_via_pattern():
    # The enforcer regex must capture a usable version from range syntax,
    # including legacy 1.x lower bounds ([1.8,) captured "1" before this fix).
    m = re.search(
        ENFORCER_JAVA_PATTERN,
        "<requireJavaVersion><version>[1.8,)</version></requireJavaVersion>",
        re.DOTALL | re.IGNORECASE,
    )
    assert m and _normalize_java_version(m.group(1)) == "8"
    m = re.search(
        ENFORCER_JAVA_PATTERN,
        "<requireJavaVersion><version>[11,17)</version></requireJavaVersion>",
        re.DOTALL | re.IGNORECASE,
    )
    assert m and _normalize_java_version(m.group(1)) == "11"


# --- Detection-loop behavior: rejected captures fall through, never clear ---


class _PomOrch:
    """Serves a fixed pom for any cat of pom.xml; empty output otherwise."""

    def __init__(self, pom):
        self.pom = pom

    def execute_command(self, command, **kwargs):
        if command.startswith("cat ") and "/pom.xml" in command:
            return {"success": True, "output": self.pom, "exit_code": 0}
        return {"success": True, "output": "", "exit_code": 0}


def _analyze(pom):
    analyzer = ProjectAnalyzerTool(docker_orchestrator=_PomOrch(pom))
    config = {}
    analyzer._analyze_maven_configuration("/workspace/p", config)
    return config


def test_analyzer_rejected_property_falls_through_to_compiler_plugin_form():
    # A ${...} capture from a higher-priority property pattern must not be
    # accepted AND must not end the search: the compiler-plugin <source>
    # form later in the pattern list still wins.
    pom = (
        "<project>"
        "<properties>"
        "<maven.compiler.source>${javac.src.version}</maven.compiler.source>"
        "</properties>"
        "<build><plugins><plugin>"
        "<artifactId>maven-compiler-plugin</artifactId>"
        "<configuration><source>1.8</source><target>1.8</target></configuration>"
        "</plugin></plugins></build></project>"
    )
    config = _analyze(pom)
    assert config.get("java_version") == "8"
    assert config.get("java_version_source") == "maven-compiler"


def test_analyzer_compiler_plugin_indirection_yields_no_version_not_garbage():
    # <source>${javac.src.version}</source> alone: rejected, detection reports
    # nothing rather than an unresolved property string.
    pom = (
        "<project><build><plugins><plugin>"
        "<artifactId>maven-compiler-plugin</artifactId>"
        "<configuration><source>${javac.src.version}</source></configuration>"
        "</plugin></plugins></build></project>"
    )
    config = _analyze(pom)
    assert config.get("java_version") is None


def test_analyzer_enforcer_legacy_range_normalizes():
    pom = (
        "<project><build><plugins><plugin>"
        "<artifactId>maven-enforcer-plugin</artifactId>"
        "<configuration><rules>"
        "<requireJavaVersion><version>[1.8,)</version></requireJavaVersion>"
        "</rules></configuration></plugin></plugins></build></project>"
    )
    config = _analyze(pom)
    assert config.get("java_version") == "8"
    assert config.get("java_version_source") == "maven-enforcer"
    assert config.get("java_version_enforced") is True


def test_analyzer_property_capture_normalizes_legacy_form():
    pom = (
        "<project><properties>"
        "<maven.compiler.target>1.7</maven.compiler.target>"
        "</properties></project>"
    )
    config = _analyze(pom)
    assert config.get("java_version") == "7"


def test_setup_tool_rejected_property_falls_through_to_compiler_plugin_form():
    pom = (
        "<project>"
        "<properties>"
        "<maven.compiler.source>${javac.src.version}</maven.compiler.source>"
        "</properties>"
        "<build><plugins><plugin>"
        "<artifactId>maven-compiler-plugin</artifactId>"
        "<configuration><source>1.8</source><target>1.8</target></configuration>"
        "</plugin></plugins></build></project>"
    )
    tool = ProjectSetupTool(_PomOrch(pom))
    assert tool._detect_maven_java_version("/workspace/p") == "8"


def test_setup_tool_enforcer_legacy_range_normalizes():
    pom = (
        "<project><build><plugins><plugin>"
        "<artifactId>maven-enforcer-plugin</artifactId>"
        "<configuration><rules>"
        "<requireJavaVersion><version>[1.8,)</version></requireJavaVersion>"
        "</rules></configuration></plugin></plugins></build></project>"
    )
    tool = ProjectSetupTool(_PomOrch(pom))
    assert tool._detect_maven_java_version("/workspace/p") == "8"


def test_setup_tool_compiler_plugin_indirection_yields_none():
    pom = (
        "<project><build><plugins><plugin>"
        "<artifactId>maven-compiler-plugin</artifactId>"
        "<configuration><source>${javac.src.version}</source></configuration>"
        "</plugin></plugins></build></project>"
    )
    tool = ProjectSetupTool(_PomOrch(pom))
    assert tool._detect_maven_java_version("/workspace/p") is None
