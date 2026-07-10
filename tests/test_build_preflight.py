# tests/test_build_preflight.py
"""JDK pre-flight + build-requirements manifest (spec §1).

The manifest is the phase-1 -> build-tool handoff: tools only hold an
orchestrator, so requirements persist in the container next to the env
overlay (/workspace/.setup_agent/).
"""

import json

from sag.tools.internal.build_preflight import (
    REQUIREMENTS_PATH,
    read_build_requirements,
    write_build_requirements,
)


class FakeOrch:
    """In-memory container FS: supports the cat/mkdir/heredoc commands used."""

    def __init__(self):
        self.files = {}

    def execute_command(self, cmd, workdir=None):
        if cmd.startswith("mkdir -p"):
            return {"success": True, "exit_code": 0, "output": ""}
        if "<<" in cmd and REQUIREMENTS_PATH in cmd:  # heredoc write ("cat > ... <<'SAGEOF'")
            body = cmd.split("<<'SAGEOF'\n", 1)[1].rsplit("\nSAGEOF", 1)[0]
            self.files[REQUIREMENTS_PATH] = body
            return {"success": True, "exit_code": 0, "output": ""}
        if cmd.startswith("cat "):
            path = cmd.split("cat ", 1)[1].strip()
            if path in self.files:
                return {"success": True, "exit_code": 0, "output": self.files[path]}
            return {"success": False, "exit_code": 1, "output": "No such file"}
        return {"success": True, "exit_code": 0, "output": ""}


def test_write_then_read_round_trips():
    orch = FakeOrch()
    data = {"java_version": "17", "root_shape": "healthy_reactor", "build_root": "/workspace/p"}
    assert write_build_requirements(orch, data) is True
    assert read_build_requirements(orch) == data


def test_read_missing_manifest_returns_empty_dict():
    assert read_build_requirements(FakeOrch()) == {}


def test_read_corrupt_manifest_returns_empty_dict():
    orch = FakeOrch()
    orch.files[REQUIREMENTS_PATH] = "{not json"
    assert read_build_requirements(orch) == {}


from sag.tools.internal.build_preflight import JdkPreflight, active_java_major


class ProvisionOrch(FakeOrch):
    """Scriptable orchestrator: maps command substrings to canned results."""

    def __init__(self, java_version_output, apt_ok=True, temurin_ok=True):
        super().__init__()
        self.java_output = java_version_output
        self.apt_ok = apt_ok
        self.temurin_ok = temurin_ok
        self.commands = []

    def execute_command(self, cmd, workdir=None):
        self.commands.append(cmd)
        if "java -version" in cmd:
            return {"success": True, "exit_code": 0, "output": self.java_output}
        if "apt-get install -y openjdk" in cmd:
            return {"success": self.apt_ok, "exit_code": 0 if self.apt_ok else 100,
                    "output": "" if self.apt_ok else "E: Unable to locate package"}
        # Must precede the "temurin" check: the real JAVA_HOME lookup globs
        # both /usr/lib/jvm/java-N-openjdk-* and /usr/lib/jvm/temurin-N-jdk*.
        if cmd.startswith("ls -d /usr/lib/jvm"):
            return {"success": True, "exit_code": 0,
                    "output": "/usr/lib/jvm/java-17-openjdk-arm64"}
        if "temurin" in cmd:
            return {"success": self.temurin_ok, "exit_code": 0 if self.temurin_ok else 1,
                    "output": ""}
        return super().execute_command(cmd, workdir)


def test_matching_jdk_is_a_noop():
    orch = ProvisionOrch('openjdk version "17.0.9" 2023-10-17')
    outcome = JdkPreflight(orch).run("17", source="maven-enforcer")
    assert outcome.matched is True
    assert outcome.provisioned is False
    assert outcome.narration == ""
    assert not any("apt-get" in c for c in orch.commands)


def test_active_java_major_parses_legacy_and_modern():
    assert active_java_major(ProvisionOrch('openjdk version "17.0.9"')) == "17"
    assert active_java_major(ProvisionOrch('java version "1.8.0_392"')) == "8"


def test_mismatch_provisions_and_narrates(monkeypatch):
    orch = ProvisionOrch('openjdk version "11.0.2"')
    # Overlay registration talks to the container too; stub it out.
    import sag.tools.internal.build_preflight as bp
    monkeypatch.setattr(bp, "_register_overlay", lambda *a, **k: True)
    outcome = JdkPreflight(orch).run("17", source="maven-enforcer")
    assert outcome.provisioned is True
    assert outcome.mismatch is False
    assert "[pre-flight] Required: Java 17" in outcome.narration
    assert "Active: Java 11" in outcome.narration


def test_unprovisionable_degrades_to_mismatch_note_never_raises(monkeypatch):
    orch = ProvisionOrch('openjdk version "11.0.2"', apt_ok=False, temurin_ok=False)
    import sag.tools.internal.build_preflight as bp
    monkeypatch.setattr(bp, "_register_overlay", lambda *a, **k: True)
    outcome = JdkPreflight(orch).run("8", source="maven-compiler")
    assert outcome.provisioned is False
    assert outcome.mismatch is True          # verifier picks this up (Task 8)
    assert "could not provision" in outcome.narration


def test_no_requirement_is_a_noop():
    orch = ProvisionOrch('openjdk version "21.0.1"')
    outcome = JdkPreflight(orch).run(None)
    assert outcome.matched is True and outcome.narration == ""


from sag.tools.internal.build_preflight import classify_version_error


def test_enforcer_message_yields_version():
    out = ("[ERROR] Rule 0: org.apache.maven.plugins.enforcer.RequireJavaVersion failed "
           "with message:\nDetected JDK Version: 11.0.2 is not in the allowed range [17,).")
    assert classify_version_error(out) == "17"


def test_unsupported_class_version_maps_bytecode_to_jdk():
    # class file version 61.0 = JDK 17 (44 + major)
    out = ("java.lang.UnsupportedClassVersionError: com/foo/Bar has been compiled by a "
           "more recent version of the Java Runtime (class file version 61.0)")
    assert classify_version_error(out) == "17"


def test_invalid_target_release():
    assert classify_version_error("[ERROR] Fatal error compiling: error: invalid target release: 21") == "21"
    assert classify_version_error("error: release version 17 not supported") == "17"


def test_non_version_failures_return_none():
    assert classify_version_error("[ERROR] Failed to execute goal ... test failures") is None
    assert classify_version_error("") is None
    assert classify_version_error("BUILD SUCCESS") is None
