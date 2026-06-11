import json

from sag.runtime.env_overlay import DEFAULT_OVERLAY_JSON
from sag.tools.internal.toolchain_manager import (
    ToolchainManager,
    ToolchainSpec,
    ToolExecutableCandidate,
    ToolVersionRequirement,
)


class FakeToolchainOrchestrator:
    def __init__(self, executables=None, path_executable=None):
        self.executables = executables or {}
        self.path_executable = path_executable
        self.files = {}
        self.commands = []
        self.reads = []

    def read_file(self, path):
        self.reads.append(path)
        if path not in self.files:
            return {"success": False, "content": "", "exit_code": 1}
        return {"success": True, "content": self.files[path], "exit_code": 0}

    def read_count(self, path):
        return sum(1 for read_path in self.reads if read_path == path)

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append((command, workdir, timeout))

        if command.startswith("test -x "):
            path = command.split("test -x ", 1)[1].split(" && ", 1)[0].strip("'")
            exists = path in self.executables
            return {
                "success": True,
                "output": "EXISTS" if exists else "MISSING",
                "exit_code": 0,
            }

        if command.endswith(" -version"):
            path = command[: -len(" -version")].strip("'")
            output = self.executables.get(path)
            if output:
                return {"success": True, "output": output, "exit_code": 0}
            return {"success": False, "output": "", "exit_code": 1}

        if "apache-maven-*/bin/mvn" in command:
            paths = [
                path
                for path in self.executables
                if "/apache-maven-" in path and path.endswith("/bin/mvn")
            ]
            return {"success": True, "output": "\n".join(paths), "exit_code": 0}

        if command == "command -v mvn":
            if self.path_executable:
                return {"success": True, "output": self.path_executable, "exit_code": 0}
            return {"success": False, "output": "", "exit_code": 1}

        if command.startswith("cat /workspace/.setup_agent/toolchains.json"):
            output = self.files.get("/workspace/.setup_agent/toolchains.json", "{}")
            return {"success": True, "output": output, "exit_code": 0}

        if command.startswith("mkdir -p /workspace/.setup_agent"):
            return {"success": True, "output": "", "exit_code": 0}

        if command.startswith("cat > /workspace/.setup_agent/toolchains.json"):
            content = command.split("\n", 1)[1].rsplit("\nSAG_TOOLCHAINS_EOF", 1)[0]
            self.files["/workspace/.setup_agent/toolchains.json"] = content
            return {"success": True, "output": "", "exit_code": 0}

        return {"success": True, "output": "", "exit_code": 0}


def test_resolve_exact_requirement_does_not_upgrade_to_newer_version():
    manager = ToolchainManager(
        FakeToolchainOrchestrator(
            {
                "/tmp/apache-maven-3.8.8/bin/mvn": "Apache Maven 3.8.8",
                "/tmp/apache-maven-3.9.6/bin/mvn": "Apache Maven 3.9.6",
            }
        )
    )

    resolved = manager.resolve(
        ToolchainSpec(
            name="maven",
            executable="mvn",
            version_requirement=ToolVersionRequirement(
                raw="3.8.8",
                source="tool_parameter",
                kind="exact",
            ),
        )
    )

    assert resolved is not None
    assert resolved.candidate.version == "3.8.8"
    assert resolved.candidate.path == "/tmp/apache-maven-3.8.8/bin/mvn"


def test_resolve_range_requirement_excludes_newer_major_version():
    manager = ToolchainManager(
        FakeToolchainOrchestrator(
            {
                "/tmp/apache-maven-3.9.6/bin/mvn": "Apache Maven 3.9.6",
                "/tmp/apache-maven-4.0.0/bin/mvn": "Apache Maven 4.0.0",
            }
        )
    )

    resolved = manager.resolve(
        ToolchainSpec(
            name="maven",
            executable="mvn",
            version_requirement=ToolVersionRequirement(
                raw="[3.9,4.0)",
                source="tool_parameter",
                kind="range",
            ),
        )
    )

    assert resolved is not None
    assert resolved.candidate.version == "3.9.6"


def test_resolve_compound_requirement_respects_upper_bound():
    manager = ToolchainManager(
        FakeToolchainOrchestrator(
            {
                "/tmp/apache-maven-3.9.6/bin/mvn": "Apache Maven 3.9.6",
                "/tmp/apache-maven-4.0.0/bin/mvn": "Apache Maven 4.0.0",
            }
        )
    )

    requirement = ToolVersionRequirement.from_raw(
        ">=3.9,<4.0",
        source="tool_parameter",
    )

    resolved = manager.resolve(
        ToolchainSpec(
            name="maven",
            executable="mvn",
            version_requirement=requirement,
        )
    )

    assert requirement is not None
    assert requirement.kind == "range"
    assert resolved is not None
    assert resolved.candidate.version == "3.9.6"


def test_resolve_without_requirement_prefers_path_over_unregistered_standalone():
    manager = ToolchainManager(
        FakeToolchainOrchestrator(
            {
                "/tmp/apache-maven-3.9.6/bin/mvn": "Apache Maven 3.9.6",
                "/usr/local/bin/mvn": "Apache Maven 3.6.3",
            },
            path_executable="/usr/local/bin/mvn",
        )
    )

    resolved = manager.resolve(ToolchainSpec(name="maven", executable="mvn"))

    assert resolved is not None
    assert resolved.candidate.path == "/usr/local/bin/mvn"


def test_env_overlay_candidate_wins_over_system_path():
    orchestrator = FakeToolchainOrchestrator(
        {
            "/opt/apache-maven-3.9.9/bin/mvn": "Apache Maven 3.9.9",
            "/usr/bin/mvn": "Apache Maven 3.6.3",
        },
        path_executable="/usr/bin/mvn",
    )
    orchestrator.files[DEFAULT_OVERLAY_JSON] = json.dumps(
        {
            "version": 1,
            "tools": {
                "maven": {
                    "active": "/opt/apache-maven-3.9.9/bin/mvn",
                    "candidates": {
                        "/opt/apache-maven-3.9.9/bin/mvn": {
                            "version": "3.9.9",
                            "source": "agent_registered",
                        }
                    },
                    "blocked": [],
                }
            },
        }
    )
    manager = ToolchainManager(orchestrator)

    resolved = manager.resolve(
        ToolchainSpec(
            name="maven",
            executable="mvn",
            version_requirement=ToolVersionRequirement(
                raw="[3.9,)",
                source="tool_parameter",
                kind="range",
            ),
        )
    )

    assert resolved is not None
    assert resolved.candidate.path == "/opt/apache-maven-3.9.9/bin/mvn"
    assert resolved.candidate.version == "3.9.9"
    assert resolved.candidate.source == "env_overlay"


def test_env_overlay_blocker_excludes_exact_path_only():
    orchestrator = FakeToolchainOrchestrator(
        {
            "/opt/apache-maven-3.9.9/bin/mvn": "Apache Maven 3.9.9",
            "/usr/bin/mvn": "Apache Maven 3.6.3",
        },
        path_executable="/usr/bin/mvn",
    )
    orchestrator.files[DEFAULT_OVERLAY_JSON] = json.dumps(
        {
            "version": 1,
            "tools": {
                "maven": {
                    "active": "/opt/apache-maven-3.9.9/bin/mvn",
                    "candidates": {
                        "/opt/apache-maven-3.9.9/bin/mvn": {
                            "version": "3.9.9",
                            "source": "agent_registered",
                        }
                    },
                    "blocked": [
                        {
                            "executable": "/usr/bin/mvn",
                            "version": "3.6.3",
                            "requirement": "[3.9,)",
                            "reason": "Project requires Maven 3.9+",
                            "source": "build_error",
                        }
                    ],
                }
            },
        }
    )
    manager = ToolchainManager(orchestrator)
    spec = ToolchainSpec(name="maven", executable="mvn")

    discovered_paths = [candidate.path for candidate in manager.discover(spec)]
    resolved = manager.resolve(spec)

    assert "/usr/bin/mvn" not in discovered_paths
    assert "/opt/apache-maven-3.9.9/bin/mvn" in discovered_paths
    assert resolved is not None
    assert resolved.candidate.path == "/opt/apache-maven-3.9.9/bin/mvn"


def test_env_overlay_resolution_reads_overlay_json_once_for_multiple_candidates():
    orchestrator = FakeToolchainOrchestrator(
        {
            "/opt/apache-maven-3.9.9/bin/mvn": "Apache Maven 3.9.9",
            "/tmp/apache-maven-3.8.8/bin/mvn": "Apache Maven 3.8.8",
            "/tmp/apache-maven-3.9.6/bin/mvn": "Apache Maven 3.9.6",
            "/usr/bin/mvn": "Apache Maven 3.6.3",
        },
        path_executable="/usr/bin/mvn",
    )
    orchestrator.files[DEFAULT_OVERLAY_JSON] = json.dumps(
        {
            "version": 1,
            "tools": {
                "maven": {
                    "active": "/opt/apache-maven-3.9.9/bin/mvn",
                    "candidates": {
                        "/opt/apache-maven-3.9.9/bin/mvn": {
                            "version": "3.9.9",
                            "source": "agent_registered",
                        }
                    },
                    "blocked": [
                        {
                            "executable": "/tmp/apache-maven-3.8.8/bin/mvn",
                            "version": "3.8.8",
                            "requirement": None,
                            "reason": "Prefer Maven 3.9+",
                            "source": "build_error",
                        },
                        {
                            "executable": "/usr/bin/mvn",
                            "version": "3.6.3",
                            "requirement": None,
                            "reason": "System Maven is too old",
                            "source": "build_error",
                        },
                    ],
                }
            },
        }
    )
    manager = ToolchainManager(orchestrator)

    resolved = manager.resolve(ToolchainSpec(name="maven", executable="mvn"))

    assert resolved is not None
    assert resolved.candidate.path == "/opt/apache-maven-3.9.9/bin/mvn"
    assert orchestrator.read_count(DEFAULT_OVERLAY_JSON) <= 1


def test_registered_candidate_persists_and_is_loaded_for_resolution():
    orchestrator = FakeToolchainOrchestrator(
        {"/opt/apache-maven-3.9.6/bin/mvn": "Apache Maven 3.9.6"}
    )
    manager = ToolchainManager(orchestrator)
    manager.register(
        ToolExecutableCandidate(
            name="maven",
            executable="mvn",
            path="/opt/apache-maven-3.9.6/bin/mvn",
            version="3.9.6",
            source="registered",
        )
    )

    stored = json.loads(orchestrator.files["/workspace/.setup_agent/toolchains.json"])
    assert stored["maven"]["mvn"][0]["path"] == "/opt/apache-maven-3.9.6/bin/mvn"

    reloaded = ToolchainManager(orchestrator)
    resolved = reloaded.resolve(ToolchainSpec(name="maven", executable="mvn"))

    assert resolved is not None
    assert resolved.candidate.path == "/opt/apache-maven-3.9.6/bin/mvn"
