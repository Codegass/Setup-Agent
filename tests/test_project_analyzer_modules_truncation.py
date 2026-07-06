"""Analyzer parses the pom from an UNTRUNCATED read.

The analyzer parses the pom internally by regex (java version, <modules>,
dependencies) and that content never reaches the model, so it must read the pom with
truncate_output=False. XML-aware truncation preserves <properties>/<dependency> but
not <modules> or enforcer blocks, so on a large pom those get dropped -- which
mis-scoped the build (httpcomponents-client: <modules> at line 260) and could hide
the required JDK. This is a general fix, not per-project: modules AND the enforcer
Java version both survive because the whole pom is read.
"""

from sag.tools.internal.project_analyzer import ProjectAnalyzerTool

_CAP = 150


def _big_pom():
    """A pom whose <modules> and enforcer block sit well past the truncation cap."""
    filler = "\n".join(f"    <!-- pad line {i} -->" for i in range(300))
    return (
        "<project>\n"
        "  <packaging>pom</packaging>\n"
        f"{filler}\n"
        "  <modules>\n"
        "    <module>mod-a</module>\n"
        "    <module>mod-b</module>\n"
        "  </modules>\n"
        "  <build><plugins><plugin><configuration><rules>\n"
        "    <requireJavaVersion><version>[17,)</version></requireJavaVersion>\n"
        "  </rules></configuration></plugin></plugins></build>\n"
        "</project>\n"
    )


def _truncate(text):
    return "\n".join(text.splitlines()[:_CAP])


class RecordingOrch:
    """Records the truncate_output kwarg on cat; simulates truncation when it is on."""

    def __init__(self, full_pom):
        self.full_pom = full_pom
        self.truncate_flags = []

    def execute_command(self, command, **kwargs):
        if command.startswith("cat ") and command.rstrip().endswith("/pom.xml"):
            truncate = kwargs.get("truncate_output", True)
            self.truncate_flags.append(truncate)
            output = self.full_pom if truncate is False else _truncate(self.full_pom)
            return {"success": True, "output": output, "exit_code": 0}
        return {"success": True, "output": "", "exit_code": 0}


def test_analyzer_reads_pom_untruncated_and_parses_modules_and_jdk():
    orch = RecordingOrch(_big_pom())
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orch)
    config = {}
    analyzer._analyze_maven_configuration("/workspace/p", config)

    assert False in orch.truncate_flags, "analyzer must read the pom untruncated"
    assert config["maven_modules"] == ["mod-a", "mod-b"]
    assert config["is_multi_module"] is True
    # The enforcer requireJavaVersion lives past the truncation cap too — it only
    # survives because the whole pom is read (the general win beyond <modules>).
    assert config.get("java_version") == "17"


def test_truncated_read_would_lose_the_modules_block():
    # Guard: proves the cap drops <modules>, i.e. the untruncated read is load-bearing.
    assert "<modules>" not in _truncate(_big_pom())
