import base64
import json
import shlex

import pytest
from build_requirements_fakes import complete_build_requirements_v1
from container_evidence_fakes import ContainerFS, add_published_mutable_json
from test_container_io import FakeContainer as AtomicContainerFS

from sag.agent.control_events import ControlEventSink
from sag.agent.evidence_publications import (
    BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
    ENV_OVERLAY_LOGICAL_ARTIFACT_ID,
    EvidencePublicationAuthority,
    install_evidence_publication_authority,
    reset_evidence_publication_authority,
)
from sag.runtime.env_overlay import (
    DEFAULT_OVERLAY_JSON,
    DEFAULT_OVERLAY_SCRIPT,
    RUNTIME_REQUIREMENT_CONFLICT,
    EnvOverlayStore,
    EnvOverlayUnavailableError,
)
from sag.tools.build.build_tool import BuildTool
from sag.tools.internal.env_tool import EnvTool
from sag.tools.internal.maven_tool import MavenTool
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.toolchain_manager import ToolchainManager, ToolchainSpec
from sag.tools.project_tool import ProjectTool


class FakeEnvOverlayOrchestrator:
    def __init__(self):
        self.files = {}
        self.commands = []

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append((command, workdir, timeout))
        if command.startswith("realpath -e -- "):
            return {
                "success": True,
                "output": shlex.split(command)[-1],
                "exit_code": 0,
            }
        if command.startswith("test -x "):
            return {"success": True, "output": "EXISTS\n", "exit_code": 0}
        if command.endswith(" -version"):
            if "apache-maven-3.9.9" in command:
                version = "3.9.9"
            elif "/usr/bin/mvn" in command:
                version = "3.8.7"
            else:
                version = "3.9.6"
            return {
                "success": True,
                "output": f"Apache Maven {version}\nMaven home: /opt/maven",
                "exit_code": 0,
            }
        return {"success": True, "output": "", "exit_code": 0}

    def write_file(self, path, content):
        self.files[path] = content
        return {"success": True, "output": "", "exit_code": 0}


def test_register_activate_writes_json_and_shell_script():
    orchestrator = FakeEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)

    store.register("maven", "/opt/apache-maven-3.9.9/bin/mvn", version="3.9.9")
    overlay = store.activate("maven", "/opt/apache-maven-3.9.9/bin/mvn")

    stored = json.loads(orchestrator.files[DEFAULT_OVERLAY_JSON])
    assert overlay["tools"]["maven"]["active"] == "/opt/apache-maven-3.9.9/bin/mvn"
    assert stored["tools"]["maven"]["active"] == "/opt/apache-maven-3.9.9/bin/mvn"
    assert (
        stored["tools"]["maven"]["candidates"]["/opt/apache-maven-3.9.9/bin/mvn"]["version"]
        == "3.9.9"
    )
    assert (
        "export PATH=/opt/apache-maven-3.9.9/bin:$PATH"
        in orchestrator.files[DEFAULT_OVERLAY_SCRIPT]
    )


def test_published_overlay_derives_environment_across_cwd_without_executing_shell(
    bind_host_evidence_publication_authority,
):
    orchestrator = FakeEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)
    store.register(
        "java",
        "/opt/jdk-21/bin/java",
        env={"JAVA_HOME": "/opt/jdk-21"},
        path_prepend=["/opt/shared/bin"],
        activate=True,
    )
    orchestrator.files[DEFAULT_OVERLAY_SCRIPT] = (
        "base64() { printf forged; }; find() { printf forged; }; "
        "sha256sum() { printf forged; }; printf() { command printf forged; };\n"
    )

    first = store.authorized_environment({"CUSTOM": "1"})
    second = EnvOverlayStore(orchestrator).authorized_environment({"CUSTOM": "1"})

    assert first == second
    assert first["JAVA_HOME"] == "/opt/jdk-21"
    assert first["PATH"].startswith("/opt/jdk-21/bin:/opt/shared/bin:")
    assert first["CUSTOM"] == "1"
    head = bind_host_evidence_publication_authority.latest_head(ENV_OVERLAY_LOGICAL_ARTIFACT_ID)
    assert head is not None
    assert head.record_kind == "env_overlay"


def test_tamper_delete_rollback_and_extra_fields_make_overlay_unavailable():
    orchestrator = FakeEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)
    store.register("java", "/opt/jdk-17/bin/java", activate=True)
    first = orchestrator.files[DEFAULT_OVERLAY_JSON]
    store.register("java", "/opt/jdk-21/bin/java", activate=True)
    latest = orchestrator.files[DEFAULT_OVERLAY_JSON]

    orchestrator.files[DEFAULT_OVERLAY_JSON] = latest.replace(
        '"version": 1', '"unexpected": true,\n  "version": 1'
    )
    with pytest.raises(EnvOverlayUnavailableError):
        EnvOverlayStore(orchestrator).authorized_environment()

    orchestrator.files[DEFAULT_OVERLAY_JSON] = first
    with pytest.raises(EnvOverlayUnavailableError):
        EnvOverlayStore(orchestrator).authorized_environment()

    del orchestrator.files[DEFAULT_OVERLAY_JSON]
    with pytest.raises(EnvOverlayUnavailableError):
        EnvOverlayStore(orchestrator).authorized_environment()


def test_mirror_only_overlay_never_affects_runner_environment():
    orchestrator = FakeEnvOverlayOrchestrator()
    orchestrator.files[DEFAULT_OVERLAY_JSON] = json.dumps(
        {"version": 1, "tools": {}}, indent=2, sort_keys=True
    )

    with pytest.raises(EnvOverlayUnavailableError):
        EnvOverlayStore(orchestrator).authorized_environment()


@pytest.mark.parametrize("key", ["PATH", "BASH_ENV", "ENV", "CDPATH", "SHELLOPTS"])
def test_overlay_rejects_shell_control_environment_keys(key):
    orchestrator = FakeEnvOverlayOrchestrator()

    with pytest.raises(ValueError, match="shell control state"):
        EnvOverlayStore(orchestrator).register(
            "java",
            "/opt/jdk-21/bin/java",
            env={key: "/workspace/attacker"},
            activate=True,
        )

    assert DEFAULT_OVERLAY_JSON not in orchestrator.files


def test_publication_failure_leaves_container_overlay_forensic_only(tmp_path):
    class FailingSink:
        path = tmp_path / "unwritten-control-events.jsonl"

        def emit(self, kind, *_args, **_kwargs):
            if kind == "evidence_store_bound":
                return None
            raise OSError("host sink unavailable")

    orchestrator = FakeEnvOverlayOrchestrator()
    authority = EvidencePublicationAuthority(run_id="run-overlay-failure", sink=FailingSink())
    token = install_evidence_publication_authority(authority, orchestrator=orchestrator)
    try:
        with pytest.raises(EnvOverlayUnavailableError, match="host publication failed"):
            EnvOverlayStore(orchestrator).register("java", "/opt/jdk-21/bin/java", activate=True)
        assert DEFAULT_OVERLAY_JSON in orchestrator.files
        with pytest.raises(EnvOverlayUnavailableError):
            EnvOverlayStore(orchestrator).authorized_environment()
    finally:
        reset_evidence_publication_authority(token)


def test_overlay_authority_recovers_from_host_stream_after_restart(tmp_path):
    orchestrator = FakeEnvOverlayOrchestrator()
    sink = ControlEventSink(tmp_path / "control-events.jsonl")
    first_authority = EvidencePublicationAuthority.for_live_run(
        run_id="run-overlay-restart",
        sink=sink,
    )
    first_token = install_evidence_publication_authority(
        first_authority,
        orchestrator=orchestrator,
    )
    try:
        EnvOverlayStore(orchestrator).register(
            "java",
            "/opt/jdk-21/bin/java",
            env={"JAVA_HOME": "/opt/jdk-21"},
            activate=True,
        )
    finally:
        reset_evidence_publication_authority(first_token)

    recovered = EvidencePublicationAuthority.for_live_run(
        run_id="run-overlay-restart",
        sink=sink,
    )
    second_token = install_evidence_publication_authority(
        recovered,
        orchestrator=orchestrator,
    )
    try:
        environment = EnvOverlayStore(orchestrator).authorized_environment()
    finally:
        reset_evidence_publication_authority(second_token)

    assert environment["JAVA_HOME"] == "/opt/jdk-21"
    assert environment["PATH"].startswith("/opt/jdk-21/bin:")


def test_block_records_exact_executable_without_blocking_other_versions():
    orchestrator = FakeEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)

    store.register("maven", "/usr/bin/mvn", version="3.6.3")
    store.block(
        "maven",
        "/usr/bin/mvn",
        version="3.6.3",
        requirement="[3.9,)",
        reason="Project requires Maven 3.9+",
    )
    store.register("maven", "/opt/apache-maven-3.9.9/bin/mvn", version="3.9.9")

    assert store.is_blocked("maven", "/usr/bin/mvn") is True
    assert store.is_blocked("maven", "/opt/apache-maven-3.9.9/bin/mvn") is False


def test_invalid_overlay_json_is_visible_but_cannot_become_runtime_authority():
    orchestrator = FakeEnvOverlayOrchestrator()
    orchestrator.files[DEFAULT_OVERLAY_JSON] = "{not valid json"
    store = EnvOverlayStore(orchestrator)

    inspected = store.inspect()

    assert inspected["tools"] == {}
    assert inspected["warnings"]

    with pytest.raises(EnvOverlayUnavailableError):
        store.register("maven", "/opt/apache-maven-3.9.9/bin/mvn", version="3.9.9")

    assert orchestrator.files[DEFAULT_OVERLAY_JSON] == "{not valid json"


def test_env_tool_register_activate_inspect():
    orchestrator = FakeEnvOverlayOrchestrator()
    tool = EnvTool(orchestrator)

    registered = tool.execute(
        {
            "action": "register",
            "tool": "maven",
            "executable": "/opt/apache-maven-3.9.9/bin/mvn",
            "version": "3.9.9",
        }
    )
    activated = tool.execute(
        {
            "action": "activate",
            "tool": "maven",
            "executable": "/opt/apache-maven-3.9.9/bin/mvn",
        }
    )
    inspected = tool.execute({"action": "inspect"})

    assert registered.succeeded is True
    assert activated.succeeded is True
    assert inspected.succeeded is True
    assert inspected.raw_data["overlay"]["tools"]["maven"]["active"] == (
        "/opt/apache-maven-3.9.9/bin/mvn"
    )


def test_env_tool_register_with_activate_confirms_exact_active_candidate():
    orchestrator = FakeEnvOverlayOrchestrator()
    tool = EnvTool(orchestrator)

    result = tool.execute(
        action="register",
        tool="maven",
        executable="/opt/apache-maven-3.9.9/bin/mvn",
        version="3.9.9",
        activate=True,
    )

    assert result.succeeded is True
    assert result.raw_data["active_candidate"] == {
        "executable": "/opt/apache-maven-3.9.9/bin/mvn",
        "version": "3.9.9",
        "source": "agent_registered",
        "env": {},
        "path_prepend": ["/opt/apache-maven-3.9.9/bin"],
    }
    assert result.raw_data["measured_version"] == "3.9.9"


class ReadLimitedEnvOverlayOrchestrator(FakeEnvOverlayOrchestrator):
    """Fail any read after the register transaction's exact snapshot."""

    def __init__(self):
        super().__init__()
        self.read_calls = 0

    def read_file(self, path):
        self.read_calls += 1
        if self.read_calls > 5:
            # The tripwire, deliberately a FAILURE and not absence: under the
            # §3.9 contract a sixth read raises out of the exact path, so a
            # transaction that re-reads past its own verified snapshot cannot
            # quietly see an empty overlay — it fails loudly.
            return {
                "success": False,
                "content": "",
                "exit_code": 1,
            }
        if path not in self.files:
            # §3.9 absence protocol: absence is STATED (None), never implied
            # by an ordinary failure.
            return None
        return {
            "success": True,
            "content": self.files[path],
            "exit_code": 0,
        }


def test_register_activation_uses_same_transaction_readback_snapshot():
    orchestrator = ReadLimitedEnvOverlayOrchestrator()
    tool = EnvTool(orchestrator)

    result = tool.execute(
        action="register",
        tool="maven",
        executable="/opt/apache-maven-3.9.9/bin/mvn",
        activate=True,
    )

    assert result.succeeded is True
    assert result.raw_data["active_candidate"]["executable"] == ("/opt/apache-maven-3.9.9/bin/mvn")
    assert orchestrator.read_calls == 5


def test_project_env_facade_replaces_stale_active_runtime_atomically():
    orchestrator = FakeEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)
    store.register("maven", "/usr/bin/mvn", version="3.8.7", activate=True)
    project = ProjectTool(env_tool=EnvTool(orchestrator, store=store))

    result = project.safe_execute(
        action="env",
        tool="maven",
        executable="/opt/apache-maven-3.9.9/bin/mvn",
        version="3.9.9",
    )

    assert result.succeeded is True
    assert result.raw_data["active_candidate"]["executable"] == ("/opt/apache-maven-3.9.9/bin/mvn")
    assert store.active_candidate("maven")["version"] == "3.9.9"
    assert orchestrator.files[DEFAULT_OVERLAY_SCRIPT].splitlines()[-1] == (
        "export PATH=/opt/apache-maven-3.9.9/bin:$PATH"
    )


def test_env_tool_block_defaults_to_build_error_without_source():
    orchestrator = FakeEnvOverlayOrchestrator()
    tool = EnvTool(orchestrator)

    block_from_dict = tool.execute(
        {
            "action": "block",
            "tool": "maven",
            "executable": "/usr/bin/mvn",
        }
    )
    block_from_kwargs = tool.execute("block", tool="gradle", executable="/usr/bin/gradle")

    assert block_from_dict.succeeded is True
    assert block_from_kwargs.succeeded is True
    assert (
        block_from_dict.raw_data["overlay"]["tools"]["maven"]["blocked"][0]["source"]
        == "build_error"
    )
    assert (
        block_from_kwargs.raw_data["overlay"]["tools"]["gradle"]["blocked"][0]["source"]
        == "build_error"
    )


def test_register_activate_rejects_blocked_executable():
    orchestrator = FakeEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)

    store.block("maven", "/usr/bin/mvn", reason="Project requires Maven 3.9+")

    with pytest.raises(ValueError, match="blocked"):
        store.register("maven", "/usr/bin/mvn", version="3.6.3", activate=True)

    inspected = store.inspect()
    assert "active" not in inspected["tools"]["maven"]


class FallbackWriteEnvOverlayOrchestrator:
    def __init__(self):
        self.files = {}
        self.commands = []

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append((command, workdir, timeout))
        if command.startswith("printf %s ") and " | base64 -d > " in command:
            tokens = shlex.split(command)
            self.files[tokens[-1]] = base64.b64decode(tokens[2]).decode("utf-8")
            return {"success": True, "output": "", "exit_code": 0}
        return {"success": True, "output": "", "exit_code": 0}


def test_fallback_writer_uses_base64_decode_not_raw_heredoc():
    orchestrator = FallbackWriteEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)

    store.register(
        "maven",
        "/opt/apache-maven-3.9.9/bin/mvn",
        env={"SAG_MARKER": "line one\nSAG_ENV_OVERLAY_EOF\nline three"},
    )

    commands = [command for command, _workdir, _timeout in orchestrator.commands]
    write_commands = [command for command in commands if DEFAULT_OVERLAY_JSON in command]
    assert write_commands
    assert all("SAG_ENV_OVERLAY_EOF" not in command for command in write_commands)
    assert any("base64 -d" in command for command in write_commands)


def test_inspect_rejects_entire_unpublished_malformed_overlay():
    orchestrator = FakeEnvOverlayOrchestrator()
    orchestrator.files[DEFAULT_OVERLAY_JSON] = json.dumps(
        {
            "version": 1,
            "tools": {
                "maven": {
                    "candidates": {
                        "/usr/bin/mvn": {
                            "version": "3.6.3",
                            "env": ["JAVA_HOME=/bad"],
                            "path_prepend": ["/usr/bin"],
                        },
                        "/opt/apache-maven-3.9.9/bin/mvn": {
                            "version": "3.9.9",
                            "env": {"JAVA_HOME": "/opt/jdk"},
                            "path_prepend": {"0": "/opt/apache-maven-3.9.9/bin"},
                        },
                    },
                }
            },
        }
    )
    store = EnvOverlayStore(orchestrator)

    inspected = store.inspect()

    assert inspected["tools"] == {}
    assert inspected["warnings"]


def test_env_tool_path_prepend_wire_is_uniform_but_a_string_still_normalizes():
    # Premise updated 2026-08-09: providers refuse union wire schemas (the
    # live 'project' facade failure was the top-level cousin), so the wire
    # states one array shape. The string-or-array CONTRACT moved whole to the
    # normalization seam: a plain string is still accepted and normalized.
    schema = EnvTool(FakeEnvOverlayOrchestrator()).get_parameter_schema()

    path_schema = schema["properties"]["path_prepend"]
    assert path_schema["type"] == "array"
    assert path_schema["items"] == {"type": "string"}
    assert "oneOf" not in path_schema

    from sag.runtime.env_overlay import EnvOverlayStore

    normalized = EnvOverlayStore._normalize_path_prepend(
        None, "/opt/maven/bin", "/opt/maven/bin/mvn"
    )
    assert normalized == ["/opt/maven/bin"]


def test_maven_registration_uses_measured_version_not_caller_claim():
    orchestrator = FakeEnvOverlayOrchestrator()
    tool = EnvTool(orchestrator)

    result = tool.execute(
        action="register",
        tool="maven",
        executable="/opt/apache-maven-3.9.9/bin/mvn",
        version="99.0-caller-claim",
        requirement="[3.9,4.0)",
        activate=True,
    )

    assert result.succeeded is True
    assert result.raw_data["measured_version"] == "3.9.9"
    assert result.raw_data["active_candidate"]["version"] == "3.9.9"
    stored = json.loads(orchestrator.files[DEFAULT_OVERLAY_JSON])
    assert (
        stored["tools"]["maven"]["candidates"]["/opt/apache-maven-3.9.9/bin/mvn"]["version"]
        == "3.9.9"
    )


def test_maven_registration_accepts_ansi_decorated_identity_line():
    orchestrator = ScriptedMavenProbeOrchestrator(
        {
            "success": True,
            "output": "\x1b[1mApache Maven 3.9.9\x1b[0m\nMaven home: /opt/maven",
            "exit_code": 0,
        }
    )
    tool = EnvTool(orchestrator)

    result = tool.execute(
        action="register",
        tool="maven",
        executable="/opt/apache-maven-3.9.9/bin/mvn",
        requirement="[3.9,4.0)",
        activate=True,
    )

    assert result.succeeded is True
    assert result.raw_data["measured_version"] == "3.9.9"


class ScriptedMavenProbeOrchestrator(FakeEnvOverlayOrchestrator):
    def __init__(self, probe):
        super().__init__()
        self.probe = probe

    def execute_command(self, command, workdir=None, timeout=None):
        if command.endswith(" -version"):
            self.commands.append((command, workdir, timeout))
            return dict(self.probe)
        return super().execute_command(command, workdir=workdir, timeout=timeout)


@pytest.mark.parametrize(
    ("probe", "requirement", "error_code"),
    [
        (
            {"success": True, "output": "not Maven 3.9.9", "exit_code": 0},
            None,
            "ENV_RUNTIME_IDENTITY_MISMATCH",
        ),
        (
            {"success": True, "output": "Apache Maven 3.8.7", "exit_code": 0},
            "[3.9,)",
            "ENV_RUNTIME_REQUIREMENT_MISMATCH",
        ),
        (
            {"success": False, "output": "missing libjansi", "exit_code": 1},
            None,
            "ENV_RUNTIME_PROBE_FAILED",
        ),
    ],
)
def test_maven_registration_rejects_unproven_runtime_without_mutating_overlay(
    probe,
    requirement,
    error_code,
):
    orchestrator = ScriptedMavenProbeOrchestrator(probe)
    tool = EnvTool(orchestrator)

    result = tool.execute(
        action="register",
        tool="maven",
        executable="/opt/candidate/bin/mvn",
        version="3.9.9",
        requirement=requirement,
        activate=True,
    )

    assert result.succeeded is False
    assert result.error_code == error_code
    assert DEFAULT_OVERLAY_JSON not in orchestrator.files
    assert DEFAULT_OVERLAY_SCRIPT not in orchestrator.files


class FailAfterMutatingWriteOrchestrator(FakeEnvOverlayOrchestrator):
    def __init__(self):
        super().__init__()
        self.fail_path = None
        self.failed = False

    def write_file(self, path, content):
        self.files[path] = content
        if path == self.fail_path and not self.failed:
            self.failed = True
            return {"success": False, "output": "injected write failure", "exit_code": 1}
        return {"success": True, "output": "", "exit_code": 0}


def test_overlay_mutating_write_failure_leaves_new_json_forensic_only():
    orchestrator = FailAfterMutatingWriteOrchestrator()
    store = EnvOverlayStore(orchestrator)
    store.register("maven", "/usr/bin/mvn", version="3.8.7", activate=True)
    before = dict(orchestrator.files)
    orchestrator.fail_path = DEFAULT_OVERLAY_JSON

    with pytest.raises(RuntimeError, match="forensic shell restored"):
        store.register(
            "maven",
            "/opt/apache-maven-3.9.9/bin/mvn",
            version="3.9.9",
            activate=True,
        )

    assert orchestrator.files[DEFAULT_OVERLAY_JSON] != before[DEFAULT_OVERLAY_JSON]
    assert orchestrator.files[DEFAULT_OVERLAY_SCRIPT] == before[DEFAULT_OVERLAY_SCRIPT]
    assert store.active_candidate("maven") is None
    assert EnvOverlayStore(orchestrator).inspect()["warnings"]


class CommandOnlyFailingOverlayOrchestrator:
    """Exercise the production fallback, which has no read_file/write_file API."""

    def __init__(self):
        self.storage = {}
        self.fail_path = DEFAULT_OVERLAY_JSON
        self.failed = False

    def execute_command(self, command, workdir=None, timeout=None):
        del workdir, timeout
        if command.startswith("mkdir -p "):
            return {"success": True, "output": "", "exit_code": 0}
        if command.startswith("if test -f ") and "base64 -w 0 --" in command:
            encoded_path = command.split("base64 -w 0 --", 1)[1].split(";", 1)[0].strip()
            path = shlex.split(encoded_path)[0]
            if path not in self.storage:
                return {
                    "success": False,
                    "output": "__SAG_FILE_MISSING__",
                    "exit_code": 44,
                }
            payload = base64.b64encode(self.storage[path].encode("utf-8")).decode("ascii")
            return {
                "success": True,
                "output": f"__SAG_FILE_BASE64__{payload}",
                "exit_code": 0,
            }
        if command.startswith("cat "):
            path = shlex.split(command)[1]
            if path not in self.storage:
                return {"success": False, "output": "", "exit_code": 1}
            return {
                "success": True,
                "output": self.storage[path],
                "exit_code": 0,
            }
        if command.startswith("printf %s ") and " | base64 -d > " in command:
            tokens = shlex.split(command)
            path = tokens[-1]
            self.storage[path] = base64.b64decode(tokens[2]).decode("utf-8")
            if path == self.fail_path and not self.failed:
                self.failed = True
                return {
                    "success": False,
                    "output": "injected initial write failure",
                    "exit_code": 1,
                }
            return {"success": True, "output": "", "exit_code": 0}
        if command.startswith("rm -f "):
            path = shlex.split(command)[2]
            self.storage.pop(path, None)
            return {"success": True, "output": "", "exit_code": 0}
        raise AssertionError(f"unexpected command: {command}")


def test_command_only_fake_without_atomic_cas_fails_closed():
    orchestrator = CommandOnlyFailingOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)

    with pytest.raises(RuntimeError, match="forensic shell restored"):
        store.register(
            "maven",
            "/opt/apache-maven-3.9.9/bin/mvn",
            version="3.9.9",
            activate=True,
        )

    assert orchestrator.storage == {}


class ProductionStrippingOverlayOrchestrator:
    """Mirror DockerOrchestrator's text `.strip()` while supporting raw transport."""

    def __init__(self):
        self.atomic = AtomicContainerFS()
        self.storage = self.atomic.files

    def execute_command(self, command, workdir=None, timeout=None, truncate_output=True):
        del workdir, timeout, truncate_output
        if command.startswith("cat "):
            path = shlex.split(command)[1]
            if path not in self.storage:
                return {"success": False, "output": "", "exit_code": 1}
            # This is the production defect: ordinary command output loses the
            # shell script's terminal LF.
            return {
                "success": True,
                "output": self.storage[path].strip(),
                "exit_code": 0,
            }
        if command.startswith("if test -f ") and "base64 -w 0 --" in command:
            encoded_path = command.split("base64 -w 0 --", 1)[1].split(";", 1)[0].strip()
            path = shlex.split(encoded_path)[0]
            if path not in self.storage:
                return {
                    "success": False,
                    "output": "__SAG_FILE_MISSING__",
                    "exit_code": 44,
                }
            payload = base64.b64encode(self.storage[path].encode("utf-8")).decode("ascii")
            return {
                "success": True,
                "output": f"__SAG_FILE_BASE64__{payload}",
                "exit_code": 0,
            }
        if command.startswith("printf %s ") and " | base64 -d > " in command:
            tokens = shlex.split(command)
            path = tokens[-1]
            self.storage[path] = base64.b64decode(tokens[2]).decode("utf-8")
            return {"success": True, "output": "", "exit_code": 0}
        return self.atomic.execute_command(command)


def test_command_fallback_readback_preserves_terminal_newline_byte_for_byte():
    orchestrator = ProductionStrippingOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)

    overlay = store.register(
        "java",
        "/usr/lib/jvm/java-8/bin/java",
        version="8",
        env={"JAVA_HOME": "/usr/lib/jvm/java-8"},
        activate=True,
    )

    assert overlay["tools"]["java"]["active"] == "/usr/lib/jvm/java-8/bin/java"
    assert orchestrator.storage[DEFAULT_OVERLAY_SCRIPT].endswith("\n")
    assert json.loads(orchestrator.storage[DEFAULT_OVERLAY_JSON])["tools"]["java"]["active"] == (
        "/usr/lib/jvm/java-8/bin/java"
    )


def test_requirement_failure_atomically_records_constraint_and_exact_block():
    orchestrator = FakeEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)
    store.register("maven", "/usr/bin/mvn", version="3.8.7", activate=True)

    overlay = store.record_requirement_failure(
        "maven",
        requirement="[3.9,)",
        executable="/usr/bin/mvn",
        version="3.8.7",
        reason="Maven Enforcer rejected this runtime",
        working_directory="/workspace/project",
    )

    entry = overlay["tools"]["maven"]
    assert entry["requirements"] == [
        {
            "raw": "[3.9,)",
            "source": "build_error",
            "working_directory": "/workspace/project",
        }
    ]
    assert "active" not in entry
    assert entry["blocked"][-1] == {
        "executable": "/usr/bin/mvn",
        "version": "3.8.7",
        "requirement": "[3.9,)",
        "reason": "Maven Enforcer rejected this runtime",
        "source": "build_error",
    }
    assert store.observed_requirement("maven")["raw"] == "[3.9,)"


def test_dynamic_java_requirement_is_persisted_with_exact_runtime_scope():
    orchestrator = FakeEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)

    overlay = store.record_runtime_requirement(
        "java",
        target_sha="a" * 40,
        domain_id="dom-camel-quarkus",
        domain_root="/workspace/camel-quarkus/integration-tests",
        required_major="17",
        source_ref="inv-maven-compile-0001",
        observed_at="2026-08-08T12:00:00Z",
        observed_runtime={"major": "11", "executable": "/usr/bin/java"},
    )

    assert overlay["tools"]["java"]["runtime_requirements"] == [
        {
            "target_sha": "a" * 40,
            "domain_id": "dom-camel-quarkus",
            "domain_root": "/workspace/camel-quarkus/integration-tests",
            "required_major": "17",
            "source_ref": "inv-maven-compile-0001",
            "observed_sequence": 1,
            "observed_at": "2026-08-08T12:00:00Z",
            "observed_runtime": {"major": "11", "executable": "/usr/bin/java"},
        }
    ]


def test_corrupt_overlay_is_unknown_not_absent_for_dynamic_runtime_authority():
    orchestrator = FakeEnvOverlayOrchestrator()
    orchestrator.files[DEFAULT_OVERLAY_JSON] = "{not valid json"
    store = EnvOverlayStore(orchestrator)

    with pytest.raises(RuntimeError, match="dynamic runtime state is not trustworthy"):
        store.resolve_runtime_requirement(
            "java",
            target_sha="a" * 40,
            domain_id="dom-a",
            domain_root="/workspace/project/a",
            static_major="11",
            static_source="survey",
        )

    assert orchestrator.files[DEFAULT_OVERLAY_JSON] == "{not valid json"


def test_persisted_dynamic_runtime_outranks_static_survey_for_same_scope():
    orchestrator = FakeEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)
    store.record_runtime_requirement(
        "java",
        target_sha="b" * 40,
        domain_id="dom-it",
        domain_root="/workspace/project/it",
        required_major="17",
        source_ref="output_abc",
        observed_at="2026-08-08T12:00:00Z",
    )

    resolved = store.resolve_runtime_requirement(
        "java",
        target_sha="b" * 40,
        domain_id="dom-it",
        domain_root="/workspace/project/it",
        static_major="11",
        static_source="maven-compiler",
    )

    assert resolved["required_major"] == "17"
    assert resolved["authority"] == "persisted_dynamic"
    assert resolved["provenance"]["source_ref"] == "output_abc"


def test_dynamic_runtime_requirement_does_not_leak_to_sibling_or_other_sha():
    orchestrator = FakeEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)
    store.record_runtime_requirement(
        "java",
        target_sha="c" * 40,
        domain_id="dom-a",
        domain_root="/workspace/project/a",
        required_major="17",
        source_ref="inv-a",
        observed_at="2026-08-08T12:00:00Z",
    )

    sibling = store.resolve_runtime_requirement(
        "java",
        target_sha="c" * 40,
        domain_id="dom-b",
        domain_root="/workspace/project/b",
        static_major="11",
        static_source="survey",
    )
    newer_tree = store.resolve_runtime_requirement(
        "java",
        target_sha="d" * 40,
        domain_id="dom-a",
        domain_root="/workspace/project/a",
        static_major="11",
        static_source="survey",
    )

    assert sibling == {
        "required_major": "11",
        "authority": "static_survey",
        "provenance": {"source": "survey"},
    }
    assert newer_tree == sibling


def test_same_scope_dynamic_runtime_disagreement_is_typed_not_latest_wins():
    orchestrator = FakeEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)
    scope = {
        "target_sha": "e" * 40,
        "domain_id": "dom-a",
        "domain_root": "/workspace/project/a",
    }
    store.record_runtime_requirement(
        "java",
        **scope,
        required_major="17",
        source_ref="inv-1",
        observed_at="2026-08-08T12:00:00Z",
    )
    store.record_runtime_requirement(
        "java",
        **scope,
        required_major="21",
        source_ref="inv-2",
        observed_at="2026-08-08T12:01:00Z",
    )

    resolved = store.resolve_runtime_requirement(
        "java",
        **scope,
        static_major="11",
        static_source="survey",
    )

    assert "required_major" not in resolved
    assert resolved["conflict"] == {
        "typed_code": RUNTIME_REQUIREMENT_CONFLICT,
        "required_majors": ["17", "21"],
        "source_refs": ["inv-1", "inv-2"],
    }


def test_confirmed_runtime_records_the_postcondition_without_rewriting_observation():
    orchestrator = FakeEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)
    scope = {
        "target_sha": "f" * 40,
        "domain_id": "dom-a",
        "domain_root": "/workspace/project/a",
    }
    store.record_runtime_requirement(
        "java",
        **scope,
        required_major="17",
        source_ref="inv-1",
        observed_at="2026-08-08T12:00:00Z",
        observed_runtime={"major": "11"},
    )

    overlay = store.confirm_runtime_requirement(
        "java",
        **scope,
        source_ref="inv-1",
        active_runtime={"major": "17", "executable": "/opt/jdk-17/bin/java"},
        activated_at="2026-08-08T12:00:30Z",
    )

    (record,) = overlay["tools"]["java"]["runtime_requirements"]
    assert record["observed_runtime"] == {"major": "11"}
    assert record["active_runtime"] == {
        "major": "17",
        "executable": "/opt/jdk-17/bin/java",
    }
    assert record["activated_at"] == "2026-08-08T12:00:30Z"


def test_scoped_requirement_history_is_append_only_and_cannot_be_weakened():
    orchestrator = FakeEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)

    store.record_requirement_failure(
        "maven",
        requirement=">=3.9",
        working_directory="/workspace/project/island-a",
    )
    store.record_requirement_failure(
        "maven",
        requirement=">=3.8",
        working_directory="/workspace/project/island-a",
    )
    store.record_requirement_failure(
        "maven",
        requirement="[3.8,3.9)",
        working_directory="/workspace/project/island-b",
    )

    assert [
        record["raw"]
        for record in store.observed_requirements(
            "maven",
            working_directory="/workspace/project/island-a",
        )
    ] == [">=3.9", ">=3.8"]
    assert [
        record["raw"]
        for record in store.observed_requirements(
            "maven",
            working_directory="/workspace/project/island-b",
        )
    ] == ["[3.8,3.9)"]
    assert [
        record["raw"]
        for record in store.observed_requirements(
            "maven",
            working_directory="/workspace/project",
        )
    ] == [">=3.9", ">=3.8", "[3.8,3.9)"]
    assert (
        store.observed_requirements(
            "maven",
            working_directory="/workspace/other-project",
        )
        == []
    )


def test_maven_registration_cannot_narrow_persisted_constraints_by_path():
    orchestrator = FakeEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)
    store.record_requirement_failure(
        "maven",
        requirement="[3.9,)",
        working_directory="/workspace/project/module",
    )
    tool = EnvTool(orchestrator, store=store)

    result = tool.execute(
        action="register",
        tool="maven",
        executable="/usr/bin/mvn",
        activate=True,
        working_directory="/workspace/unrelated",
    )

    assert result.succeeded is False
    assert result.error_code == "ENV_RUNTIME_REQUIREMENT_MISMATCH"
    assert result.raw_data["requirement"] == "[3.9,)"


def test_omitted_maven_requirement_inherits_harness_observation():
    orchestrator = FakeEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)
    store.record_requirement_failure(
        "maven",
        requirement="[3.9,)",
        executable="/usr/bin/mvn",
        version="3.8.7",
    )
    tool = EnvTool(orchestrator, store=store)

    rejected = tool.execute(
        action="register",
        tool="maven",
        executable="/usr/bin/mvn",
        activate=True,
    )
    accepted = tool.execute(
        action="register",
        tool="maven",
        executable="/opt/apache-maven-3.9.9/bin/mvn",
        activate=True,
    )

    assert rejected.succeeded is False
    assert rejected.error_code == "ENV_RUNTIME_REQUIREMENT_MISMATCH"
    assert rejected.raw_data["requirement"] == "[3.9,)"
    assert rejected.raw_data["requirement_source"] == "registered_state"
    assert accepted.succeeded is True
    assert store.active_candidate("maven")["version"] == "3.9.9"


class MavenRealpathOrchestrator(FakeEnvOverlayOrchestrator):
    def __init__(self, realpaths):
        super().__init__()
        self.realpaths = realpaths

    def execute_command(self, command, workdir=None, timeout=None):
        if command.startswith("realpath -e -- "):
            self.commands.append((command, workdir, timeout))
            requested = shlex.split(command)[-1]
            resolved = self.realpaths.get(requested)
            return {
                "success": resolved is not None,
                "exit_code": 0 if resolved is not None else 1,
                "output": resolved or "",
            }
        return super().execute_command(command, workdir=workdir, timeout=timeout)


def test_public_maven_registration_rejects_relative_executable_before_probe():
    orchestrator = FakeEnvOverlayOrchestrator()
    project = ProjectTool(env_tool=EnvTool(orchestrator))

    result = project.execute(
        action="env",
        tool="mvn",
        executable="downloads/apache-maven/bin/mvn",
    )

    assert result.succeeded is False
    assert result.error_code == "ENV_EXECUTABLE_PATH_NOT_ABSOLUTE"
    assert DEFAULT_OVERLAY_JSON not in orchestrator.files
    assert not any(
        command.startswith("realpath ") or command.endswith(" -version")
        for command, _workdir, _timeout in orchestrator.commands
    )


def test_public_maven_registration_fails_closed_when_realpath_cannot_be_proven():
    requested = "/opt/apache-maven-current/bin/mvn"
    orchestrator = MavenRealpathOrchestrator({requested: None})

    result = EnvTool(orchestrator).execute(
        action="register",
        tool="maven",
        executable=requested,
        activate=True,
    )

    assert result.succeeded is False
    assert result.error_code == "ENV_EXECUTABLE_REALPATH_FAILED"
    assert DEFAULT_OVERLAY_JSON not in orchestrator.files


def test_public_maven_registration_rejects_workspace_symlink_escape():
    requested = "/workspace/project/tools/mvn"
    orchestrator = MavenRealpathOrchestrator({requested: "/root/private/bin/mvn"})

    result = EnvTool(orchestrator).execute(
        action="register",
        tool="maven",
        executable=requested,
        activate=True,
    )

    assert result.succeeded is False
    assert result.error_code == "ENV_EXECUTABLE_REALPATH_ESCAPE"
    assert result.raw_data["resolved_executable"] == "/root/private/bin/mvn"
    assert DEFAULT_OVERLAY_JSON not in orchestrator.files


def test_public_maven_registration_rejects_canonical_target_not_named_mvn():
    requested = "/opt/apache-maven-current/bin/mvn"
    orchestrator = MavenRealpathOrchestrator({requested: "/opt/apache-maven-3.9.9/bin/mvn-real"})

    result = EnvTool(orchestrator).execute(
        action="register",
        tool="maven",
        executable=requested,
        activate=True,
    )

    assert result.succeeded is False
    assert result.error_code == "ENV_MAVEN_EXECUTABLE_NAME_MISMATCH"
    assert DEFAULT_OVERLAY_JSON not in orchestrator.files


def test_legacy_unpublished_mvn_overlay_is_forensic_only():
    executable = "/opt/apache-maven-3.9.9/bin/mvn"
    orchestrator = FakeEnvOverlayOrchestrator()
    orchestrator.files[DEFAULT_OVERLAY_JSON] = json.dumps(
        {
            "version": 1,
            "tools": {
                "mvn": {
                    "active": executable,
                    "candidates": {
                        executable: {
                            "version": "3.9.9",
                            "source": "agent_registered",
                            "env": {},
                            "path_prepend": ["/stale/bin"],
                        }
                    },
                    "requirements": [
                        {
                            "raw": "[3.9,)",
                            "source": "build_error",
                            "working_directory": None,
                        }
                    ],
                }
            },
        }
    )
    store = EnvOverlayStore(orchestrator)

    inspected = store.inspect()
    resolved = ToolchainManager(orchestrator).resolve(
        ToolchainSpec(name="maven", executable="mvn", prefer_wrapper=False),
        working_directory="/workspace/other-project",
    )

    assert inspected["tools"] == {}
    assert inspected["warnings"]
    assert store.observed_requirements("mvn") == []
    assert resolved is None


def test_mvn_alias_covers_negative_evidence_requirements_and_clear_without_double_key():
    orchestrator = FakeEnvOverlayOrchestrator()
    store = EnvOverlayStore(orchestrator)

    recorded = store.record_requirement_failure(
        "mvn",
        requirement="[3.9,)",
        executable="/usr/bin/mvn",
        version="3.8.7",
    )

    assert set(recorded["tools"]) == {"maven"}
    assert store.observed_requirements("mvn")[0]["raw"] == "[3.9,)"
    assert store.is_blocked("maven", "/usr/bin/mvn", version="3.8.7") is True
    cleared = store.clear("mvn")
    assert cleared["tools"] == {}


class MavenContractE2EOrchestrator:
    def __init__(self):
        self.files = {}
        self.commands = []
        self.monitored_commands = []
        self.project_name = "project"
        self.executables = {
            "/usr/bin/mvn": "Apache Maven 3.8.7",
        }
        self.build_results = [
            {
                "success": False,
                "exit_code": 1,
                "output": (
                    "[ERROR] BUILD FAILURE\n"
                    "Detected Maven Version: 3.8.7 is not in the allowed range [3.9,)."
                ),
            },
            {"success": True, "exit_code": 0, "output": "[INFO] BUILD SUCCESS"},
        ]
        self.requirements_store = ContainerFS()
        add_published_mutable_json(
            self,
            self.requirements_store,
            path=REQUIREMENTS_PATH,
            record_kind="build_requirements",
            record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            payload=complete_build_requirements_v1(project_root="/workspace/project"),
        )

    def read_file(self, path):
        if path not in self.files:
            # §3.9 absence protocol: absence is STATED (None), never implied
            # by an ordinary failure — a failed read now raises on the exact
            # path, because "could not look" is not "looked and found nothing".
            return None
        return {"success": True, "content": self.files[path], "exit_code": 0}

    def write_file(self, path, content):
        self.files[path] = content
        return {"success": True, "output": "", "exit_code": 0}

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append((command, workdir, timeout))
        if REQUIREMENTS_PATH in command and "SAG_NAMED_JSON_RECORD_V1" in command:
            return self.requirements_store(command)
        if command.startswith("realpath -e -- "):
            requested = shlex.split(command)[-1]
            return {
                "success": requested in self.executables,
                "exit_code": 0 if requested in self.executables else 1,
                "output": requested if requested in self.executables else "",
            }
        if "java -version" in command:
            return {"success": True, "exit_code": 0, "output": 'openjdk version "17.0.1"'}
        if "build_requirements.json" in command and command.startswith("cat "):
            return {"success": False, "exit_code": 1, "output": ""}
        if command.startswith("test -f ") and "pom.xml" in command:
            marker = "EXISTS" if "'EXISTS'" in command else "exists"
            return {"success": True, "exit_code": 0, "output": marker}
        if command.startswith("test -x "):
            path = shlex.split(command)[2]
            exists = path in self.executables
            return {
                "success": True,
                "exit_code": 0,
                "output": "EXISTS" if exists else "MISSING",
            }
        if command.endswith(" -version"):
            path = shlex.split(command)[0]
            output = self.executables.get(path)
            return {
                "success": bool(output),
                "exit_code": 0 if output else 1,
                "output": output or "",
            }
        if command == "command -v mvn":
            return {"success": True, "exit_code": 0, "output": "/usr/bin/mvn"}
        if "apache-maven-*/bin/mvn" in command:
            standalone = [
                path
                for path in self.executables
                if "/apache-maven-" in path and path.endswith("/bin/mvn")
            ]
            return {"success": True, "exit_code": 0, "output": "\n".join(standalone)}
        if command.startswith("find ") and "target/classes" in command:
            return {
                "success": True,
                "exit_code": 0,
                "output": "/workspace/project/target/classes/Example.class",
            }
        return {"success": True, "exit_code": 0, "output": ""}

    def execute_command_with_monitoring(self, command, **kwargs):
        self.monitored_commands.append((command, kwargs))
        result = self.build_results.pop(0)
        return dict(result)


class CanonicalMavenFacadeE2EOrchestrator(MavenContractE2EOrchestrator):
    alias = "/opt/apache-maven-current/bin/mvn"
    canonical = "/opt/apache-maven-3.9.9/bin/mvn"

    def __init__(self):
        super().__init__()
        self.executables[self.alias] = "Apache Maven 3.9.9"
        self.executables[self.canonical] = "Apache Maven 3.9.9"
        self.build_results = [{"success": True, "exit_code": 0, "output": "[INFO] BUILD SUCCESS"}]

    def execute_command(self, command, workdir=None, timeout=None):
        if command.startswith("realpath -e -- "):
            self.commands.append((command, workdir, timeout))
            requested = shlex.split(command)[-1]
            resolved = self.canonical if requested == self.alias else requested
            return {
                "success": requested in self.executables,
                "exit_code": 0 if requested in self.executables else 1,
                "output": resolved if requested in self.executables else "",
            }
        if command == "command -v mvn" and DEFAULT_OVERLAY_SCRIPT in self.files:
            self.commands.append((command, workdir, timeout))
            path_line = next(
                line
                for line in self.files[DEFAULT_OVERLAY_SCRIPT].splitlines()
                if line.startswith("export PATH=")
            )
            path_prefix = path_line.removeprefix("export PATH=").removesuffix(":$PATH")
            for directory in path_prefix.split(":"):
                candidate = f"{directory}/mvn"
                if candidate in self.executables:
                    return {"success": True, "exit_code": 0, "output": candidate}
        return super().execute_command(command, workdir=workdir, timeout=timeout)


@pytest.mark.usefixtures(
    "facade_contract_authority",
    "exact_build_facade_authority",
    "exact_internal_runner_authority",
)
def test_public_mvn_env_pins_canonical_maven_for_shell_and_cross_workdir_build():
    orchestrator = CanonicalMavenFacadeE2EOrchestrator()
    orchestrator.executables["/stale/bin/mvn"] = "Apache Maven 3.8.7"
    store = EnvOverlayStore(orchestrator)
    store.register(
        "aardvark",
        "/stale/bin/helper",
        path_prepend=["/stale/bin"],
        activate=True,
    )
    project = ProjectTool(env_tool=EnvTool(orchestrator, store=store))
    build = BuildTool(orchestrator, maven_tool=MavenTool(orchestrator))

    registered = project.safe_execute(
        action="env",
        tool="mvn",
        executable=orchestrator.alias,
        path_prepend=["/stale/bin"],
        working_directory="/workspace/bootstrap",
    )
    shell_resolution = orchestrator.execute_command(
        "command -v mvn",
        workdir="/workspace/unrelated/module",
    )
    built = build.execute(
        action="compile",
        working_directory="/workspace/unrelated/module",
    )

    assert registered.succeeded is True
    assert registered.raw_data["active_candidate"]["executable"] == orchestrator.canonical
    assert set(registered.raw_data["overlay"]["tools"]) == {"aardvark", "maven"}
    assert "mvn" not in registered.raw_data["overlay"]["tools"]
    candidate = registered.raw_data["active_candidate"]
    assert candidate["path_prepend"] == [
        "/opt/apache-maven-3.9.9/bin",
        "/stale/bin",
    ]
    assert shell_resolution["output"] == orchestrator.canonical
    assert built.succeeded is True
    assert orchestrator.monitored_commands[-1][0].startswith(f"{orchestrator.canonical} ")
    assert orchestrator.monitored_commands[-1][1]["workdir"] == ("/workspace/unrelated/module")


@pytest.mark.usefixtures(
    "facade_contract_authority",
    "exact_build_facade_authority",
    "exact_internal_runner_authority",
)
def test_maven_failure_contract_survives_weak_model_omissions_end_to_end():
    orchestrator = MavenContractE2EOrchestrator()
    maven = MavenTool(orchestrator)
    build = BuildTool(orchestrator, maven_tool=maven)
    project = ProjectTool(env_tool=EnvTool(orchestrator))

    first = build.execute(action="compile", working_directory="/workspace/project")

    assert first.succeeded is False
    assert first.error_code == "MAVEN_VERSION_ERROR"
    assert first.metadata["runtime_contract_persisted"] is True
    stored = json.loads(orchestrator.files[DEFAULT_OVERLAY_JSON])
    assert stored["tools"]["maven"]["requirements"][0]["raw"] == "[3.9,)"
    assert stored["tools"]["maven"]["blocked"][-1]["executable"] == "/usr/bin/mvn"
    assert stored["tools"]["maven"]["blocked"][-1]["version"] == "3.8.7"

    stale = project.execute(
        action="env",
        tool="maven",
        executable="/usr/bin/mvn",
    )
    assert stale.succeeded is False
    assert stale.error_code == "ENV_RUNTIME_REQUIREMENT_MISMATCH"

    compatible_path = "/opt/apache-maven-3.9.9/bin/mvn"
    orchestrator.executables[compatible_path] = "Apache Maven 3.9.9"
    registered = project.execute(
        action="env",
        tool="maven",
        executable=compatible_path,
    )
    assert registered.succeeded is True
    assert registered.raw_data["measured_version"] == "3.9.9"

    retried = build.execute(action="compile", working_directory="/workspace/project")

    assert retried.succeeded is True
    assert orchestrator.monitored_commands[-1][0].startswith(f"{compatible_path} ")
    assert retried.metadata["maven_version_requirement"] == {
        "raw": "[3.9,)",
        "source": "registered_state",
        "kind": "range",
    }


def test_env_not_found_with_no_candidates_routes_to_provision():
    """#19-class: a refusal must name a call that can succeed. With no
    registered candidate anywhere, re-registering other paths cannot help;
    the one productive move is installing the tool (live 2026-08-09: the
    model looped on an absent /usr/bin/mvn with no route out)."""
    class AbsentExecutableOrchestrator(FakeEnvOverlayOrchestrator):
        # The shared fake answers every probe optimistically; this one models
        # a container where the requested executable genuinely is not there.
        def execute_command(self, command, workdir=None, timeout=None):
            if command.startswith(("test -x ", "realpath -e -- ")) or command.endswith(
                " -version"
            ):
                return {"success": False, "output": "", "exit_code": 1}
            return super().execute_command(command, workdir=workdir, timeout=timeout)

    tool = EnvTool(AbsentExecutableOrchestrator())

    result = tool.execute(
        action="register", tool="maven", executable="/usr/bin/mvn", activate=True
    )

    assert not result.succeeded
    assert result.error_code == "ENV_EXECUTABLE_NOT_FOUND"
    assert any(
        "project(action='provision', packages=['maven'])" in s
        for s in (result.suggestions or [])
    )


# ---------------------------------------------------------------------------
# D2 2026-08-12: the toolchain acquisition loop (#42)
# ---------------------------------------------------------------------------


class _MissingExecutableOrchestrator(FakeEnvOverlayOrchestrator):
    """Nothing the model asks for exists; a gradle wrapper sits in the project."""

    def __init__(self, wrapper: str | None = "/workspace/tapestry-5/gradlew"):
        super().__init__()
        self.wrapper = wrapper

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append((command, workdir, timeout))
        if command.startswith(("test -x ", "realpath -e -- ")) or command.endswith(" -version"):
            return {"success": False, "output": "", "exit_code": 1}
        if "gradlew" in command or "mvnw" in command:
            if self.wrapper and self.wrapper.rsplit("/", 1)[-1] in command:
                return {"success": True, "output": f"{self.wrapper}\n", "exit_code": 0}
            return {"success": True, "output": "", "exit_code": 0}
        return {"success": True, "output": "", "exit_code": 0}


def test_a_missing_executable_names_the_projects_own_wrapper_first():
    """D2: tapestry-5 had gradlew on disk, and the model still burned 71 calls
    registering a nonexistent /usr/bin/gradle. The wrapper needs no network and
    is the move most likely to work, so the refusal names it first."""
    tool = EnvTool(_MissingExecutableOrchestrator())

    result = tool.execute(
        action="register", tool="gradle", executable="/usr/bin/gradle", activate=True
    )

    assert not result.succeeded and result.error_code == "ENV_EXECUTABLE_NOT_FOUND"
    assert "/workspace/tapestry-5/gradlew" in (result.suggestions or [None])[0], (
        "the wrapper is named, and named first"
    )


def test_without_a_wrapper_the_refusal_still_routes_to_provision():
    """Premise corrected against the D2 evidence: `register` requires `tool`,
    so the loops did not recur through bare calls. rocketmq-externals refused
    ~689 times across three maven paths and the provision route was rendered
    5 times — the guidance fires, and the model kept going anyway. Bounding
    that recurrence is task #42's remaining half; what is pinned here is that
    a project without a wrapper still gets the one move that can succeed."""
    tool = EnvTool(_MissingExecutableOrchestrator(wrapper=None))

    result = tool.execute(
        action="register", tool="maven", executable="/usr/bin/mvn", activate=True
    )

    assert result.error_code == "ENV_EXECUTABLE_NOT_FOUND"
    assert any(
        "provision" in s and "maven" in s for s in (result.suggestions or [])
    ), "a refusal must name a call that can succeed"


# ---------------------------------------------------------------------------
# The third rung: a material action may not recur without bound (#42)
# docs/superpowers/specs/2026-08-13-material-recurrence-bound-design.md §3
# ---------------------------------------------------------------------------

ROCKETMQ_WRAPPER = "/workspace/rocketmq-externals/mvnw"
ROCKETMQ_PATHS = (
    "/usr/share/maven/bin/mvn",
    "/opt/maven/bin/mvn",
    "/usr/bin/mvn",
)


class _OneRealMavenOrchestrator(FakeEnvOverlayOrchestrator):
    """Every path the model guesses is absent except one real installation."""

    real = "/opt/apache-maven-3.9.9/bin/mvn"

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append((command, workdir, timeout))
        if self.real in command:
            if command.startswith("realpath -e -- "):
                return {"success": True, "output": f"{self.real}\n", "exit_code": 0}
            if command.startswith("test -x "):
                return {"success": True, "output": "EXISTS\n", "exit_code": 0}
            if command.endswith(" -version"):
                return {
                    "success": True,
                    "output": "Apache Maven 3.9.9\nMaven home: /opt/apache-maven-3.9.9",
                    "exit_code": 0,
                }
        if command.startswith(("test -x ", "realpath -e -- ")) or command.endswith(" -version"):
            return {"success": False, "output": "", "exit_code": 1}
        if "mvnw" in command or "gradlew" in command:
            return {"success": True, "output": "", "exit_code": 0}
        if command.startswith("cat /workspace/.setup_agent/env_overlay.json"):
            return {
                "exit_code": 0,
                "success": True,
                "output": self.files.get("/workspace/.setup_agent/env_overlay.json", ""),
            }
        return {"success": True, "output": "", "exit_code": 0}


def _refuse(tool, executable, *, name="maven"):
    return tool.execute(action="register", tool=name, executable=executable, activate=True)


def _bound_marker(result):
    """The typed fact the tool states; the engine is what writes the ledger."""
    return (result.metadata or {}).get("material_recurrence_bound")


def test_the_third_identical_refusal_states_the_bound_naming_tool_paths_and_moves():
    """§3: the first two refusals are ordinary; the third converts a known
    hopeless repetition into a stated fact. rocketmq-externals refused ~689
    times while the provision route rendered 5 times — more guidance was never
    the missing piece. The tool states the fact; `RunEvidenceState` is written
    by the engine alone (see tests/test_material_recurrence_bound.py)."""
    tool = EnvTool(_MissingExecutableOrchestrator(wrapper=ROCKETMQ_WRAPPER))

    first = _refuse(tool, ROCKETMQ_PATHS[0])
    second = _refuse(tool, ROCKETMQ_PATHS[0])
    assert not first.succeeded and not second.succeeded
    assert _bound_marker(first) is None and _bound_marker(second) is None

    third = _refuse(tool, ROCKETMQ_PATHS[0])

    assert not third.succeeded, "the result stays a refusal, never a synthesized success"
    marker = _bound_marker(third)
    assert marker["tool"] == "maven", "the fact names the tool"
    assert marker["error_code"] == "ENV_EXECUTABLE_NOT_FOUND", "and the refused error code"
    assert marker["refused_executables"] == [ROCKETMQ_PATHS[0]], "and the paths already refused"
    assert marker["refusal_count"] == 3 and marker["bound"] == 3
    moves = " ".join(marker["remaining_moves"])
    assert ROCKETMQ_WRAPPER in moves, "and the wrapper move that remains"
    assert "provision" in moves, "and the provision route that remains"
    assert marker["evidence_refs"], "and the refusals it was computed from"


def test_three_paths_for_one_tool_trip_the_bound_because_identity_is_not_the_path():
    """The rocketmq shape: one tool, three Maven paths, one wall. A path-keyed
    bound would never have fired, so identity is (tool, error_code)."""
    tool = EnvTool(_MissingExecutableOrchestrator(wrapper=ROCKETMQ_WRAPPER))

    for path in ROCKETMQ_PATHS[:2]:
        refusal = _refuse(tool, path)
        assert not refusal.succeeded and _bound_marker(refusal) is None

    third = _refuse(tool, ROCKETMQ_PATHS[2])

    assert not third.succeeded
    assert _bound_marker(third)["refused_executables"] == list(ROCKETMQ_PATHS)


def test_three_refusals_across_three_different_tools_do_not_trip_the_bound():
    """One refusal each for three tools is three separate first attempts, not
    one wall; the bound is per (tool, error_code)."""
    tool = EnvTool(_MissingExecutableOrchestrator(wrapper=None))

    for name, path in (
        ("maven", "/usr/bin/mvn"),
        ("gradle", "/usr/bin/gradle"),
        ("java", "/usr/bin/java"),
    ):
        refusal = _refuse(tool, path, name=name)
        assert not refusal.succeeded
        assert _bound_marker(refusal) is None


def test_a_successful_registration_resets_the_counter_and_reports_the_release():
    """§3: a later failure after a real success is new information, not the
    same wall. A blocker left standing against a tool that now registers would
    be a false statement in the ledger, so the success reports the release."""
    orchestrator = _OneRealMavenOrchestrator()
    tool = EnvTool(orchestrator)

    for path in ROCKETMQ_PATHS:
        assert not _refuse(tool, path).succeeded

    registered = _refuse(tool, _OneRealMavenOrchestrator.real)

    assert registered.succeeded is True
    assert registered.metadata["material_recurrence_released"]["tool"] == "maven"
    for path in ROCKETMQ_PATHS[:2]:
        refusal = _refuse(tool, path)
        assert not refusal.succeeded
        assert _bound_marker(refusal) is None, "the successful registration restarted the count"

    assert _bound_marker(_refuse(tool, ROCKETMQ_PATHS[2])), "three fresh refusals trip it again"


def test_after_the_bound_a_path_already_refused_costs_no_further_container_probe():
    """§4's fourth unit, with one premise corrected. §3 drops the probe because
    "the probe cannot change its answer" — true of a path this identity already
    refused, false of one never tried. So the identity owns the bound and the
    count, and the paths it already refused are the ones that stop costing a
    round trip."""
    orchestrator = _MissingExecutableOrchestrator(wrapper=ROCKETMQ_WRAPPER)
    tool = EnvTool(orchestrator)

    for path in ROCKETMQ_PATHS:
        assert not _refuse(tool, path).succeeded
    probes = len(orchestrator.commands)

    bounded = _refuse(tool, ROCKETMQ_PATHS[0])

    assert len(orchestrator.commands) == probes, "a refused path is never re-probed"
    assert bounded.succeeded is False, "the model is never told an action succeeded"
    assert bounded.error_code == "ENV_REFUSAL_BOUND_REACHED"
    named = " ".join(bounded.suggestions or [])
    assert ROCKETMQ_WRAPPER in named and "provision" in named, "the moves that remain are named"
    assert _bound_marker(bounded)["refused_executables"] == list(ROCKETMQ_PATHS)


def test_a_trailing_slash_is_not_a_new_path():
    """The suppression is about the path, not the spelling of it; a trailing
    slash must not buy another round trip to the same answer."""
    orchestrator = _MissingExecutableOrchestrator(wrapper=None)
    tool = EnvTool(orchestrator)

    for path in ROCKETMQ_PATHS:
        assert not _refuse(tool, path).succeeded
    probes = len(orchestrator.commands)

    respelled = _refuse(tool, f"{ROCKETMQ_PATHS[0]}/")

    assert len(orchestrator.commands) == probes
    assert respelled.error_code == "ENV_REFUSAL_BOUND_REACHED"


def test_a_new_path_past_the_bound_is_probed_once_and_still_states_one_wall():
    """The complement of the rule above: a path never tried is probed, because
    the harness has no evidence about it. It restates the same identity, which
    is why the engine — not the tool — owns the record-once decision."""
    orchestrator = _MissingExecutableOrchestrator(wrapper=None)
    tool = EnvTool(orchestrator)

    for path in ROCKETMQ_PATHS:
        assert not _refuse(tool, path).succeeded
    probes = len(orchestrator.commands)

    refused = _refuse(tool, "/usr/local/bin/mvn")

    assert len(orchestrator.commands) > probes, "an untried path is never refused unseen"
    assert refused.error_code == "ENV_EXECUTABLE_NOT_FOUND"
    marker = _bound_marker(refused)
    assert marker["tool"] == "maven" and marker["error_code"] == "ENV_EXECUTABLE_NOT_FOUND"


class _GradleWrapperOrchestrator(FakeEnvOverlayOrchestrator):
    """No system gradle; the project's own wrapper is on disk and runs."""

    wrapper = "/workspace/tapestry-5/gradlew"

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append((command, workdir, timeout))
        if command.startswith("find /workspace") and "gradlew" in command:
            return {"success": True, "output": f"{self.wrapper}\n", "exit_code": 0}
        if self.wrapper in command:
            return {"success": True, "output": "EXISTS\n", "exit_code": 0}
        if command.startswith(("test -x ", "realpath -e -- ")) or command.endswith(" -version"):
            return {"success": False, "output": "", "exit_code": 1}
        if command.startswith("cat /workspace/.setup_agent/env_overlay.json"):
            return {
                "exit_code": 0,
                "success": True,
                "output": self.files.get("/workspace/.setup_agent/env_overlay.json", ""),
            }
        return {"success": True, "output": "", "exit_code": 0}


def test_the_bound_still_probes_the_wrapper_the_refusal_itself_recommends():
    """The anti-deadlock fence §3 needs to stay coherent: rung 1 promises every
    refusal names a productive move, and the bound may not then refuse that
    move unseen. A run that recovers by another route is never cut short."""
    orchestrator = _GradleWrapperOrchestrator()
    tool = EnvTool(orchestrator)

    for _ in range(3):
        assert not _refuse(tool, "/usr/bin/gradle", name="gradle").succeeded
    assert _bound_marker(_refuse(tool, "/usr/bin/gradle", name="gradle"))

    recovered = _refuse(tool, _GradleWrapperOrchestrator.wrapper, name="gradle")

    assert recovered.succeeded is True, "the recommended move is still available past the bound"
    assert recovered.metadata["material_recurrence_released"]["tool"] == "gradle"
