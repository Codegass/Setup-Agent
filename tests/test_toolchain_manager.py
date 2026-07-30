import json
import shlex

from sag.runtime.env_overlay import DEFAULT_OVERLAY_JSON
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.toolchain_manager import (
    ToolchainManager,
    ToolchainSpec,
    ToolExecutableCandidate,
    ToolVersionRequirement,
)


class FakeToolchainOrchestrator:
    def __init__(
        self,
        executables=None,
        path_executable=None,
        *,
        realpaths=None,
        regular_files=None,
    ):
        self.executables = executables or {}
        self.path_executable = path_executable
        self.realpaths = realpaths or {}
        self.regular_files = set(regular_files or ())
        self.files = {}
        self.commands = []
        self.reads = []

    def read_file(self, path):
        self.reads.append(path)
        if path not in self.files:
            # §3.9 absence protocol: absence is STATED (None), never implied
            # by an ordinary failure — a failed read now raises on the exact
            # path, because "could not look" is not "looked and found nothing".
            return None
        return {"success": True, "content": self.files[path], "exit_code": 0}

    def read_count(self, path):
        return sum(1 for read_path in self.reads if read_path == path)

    def execute_command(self, command, workdir=None, timeout=None):
        self.commands.append((command, workdir, timeout))

        if command.startswith("realpath -e -- "):
            path = shlex.split(command)[3]
            resolved = self.realpaths.get(path)
            if resolved:
                return {"success": True, "output": resolved, "exit_code": 0}
            return {"success": False, "output": "", "exit_code": 1}

        if command.startswith("test -x "):
            path = command.split("test -x ", 1)[1].split(" && ", 1)[0].strip("'")
            exists = path in self.executables
            return {
                "success": True,
                "output": "EXISTS" if exists else "MISSING",
                "exit_code": 0,
            }

        if command.startswith("test -f "):
            path = command.split("test -f ", 1)[1].split(" && ", 1)[0].strip("'")
            exists = path in self.regular_files or path in self.files
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

        if command in ("command -v mvn", "command -v gradle"):
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


def test_nested_gradle_island_prefers_checkout_ancestor_wrapper():
    root = "/workspace/repo"
    island = f"{root}/islands/data"
    wrapper = f"{root}/gradlew"
    orchestrator = FakeToolchainOrchestrator(
        {
            wrapper: "Gradle 8.7",
            "/usr/bin/gradle": "Gradle 4.4.1",
        },
        path_executable="/usr/bin/gradle",
        realpaths={
            root: root,
            island: island,
            f"{root}/islands": f"{root}/islands",
            wrapper: wrapper,
        },
        regular_files={wrapper},
    )
    orchestrator.files[REQUIREMENTS_PATH] = json.dumps(
        {"survey": {"project_path": root}}
    )

    resolved = ToolchainManager(orchestrator).resolve(
        ToolchainSpec(name="gradle", executable="gradle"),
        working_directory=island,
    )

    assert resolved is not None
    assert resolved.candidate.path == wrapper
    assert resolved.candidate.source == "wrapper"


def test_gradle_wrapper_discovery_rejects_escape_and_stops_at_survey_root():
    root = "/workspace/repo"
    island = f"{root}/island"
    system_gradle = "/usr/bin/gradle"
    cases = (
        # The working directory itself resolves outside the surveyed checkout.
        {
            "realpaths": {
                root: root,
                island: "/outside/island",
                "/outside/island": "/outside/island",
            },
            "wrappers": {"/outside/gradlew"},
        },
        # The wrapper is a symlink whose target escapes the checkout.
        {
            "realpaths": {
                root: root,
                island: island,
                f"{root}/gradlew": "/outside/gradlew",
            },
            "wrappers": {f"{root}/gradlew"},
        },
        # A wrapper above the surveyed checkout must never be considered.
        {
            "realpaths": {
                root: root,
                island: island,
                "/workspace/gradlew": "/workspace/gradlew",
            },
            "wrappers": {"/workspace/gradlew"},
        },
    )

    for case in cases:
        executables = {
            system_gradle: "Gradle 8.5",
            **{path: "Gradle 8.7" for path in case["wrappers"]},
        }
        orchestrator = FakeToolchainOrchestrator(
            executables,
            path_executable=system_gradle,
            realpaths=case["realpaths"],
            regular_files=case["wrappers"],
        )
        orchestrator.files[REQUIREMENTS_PATH] = json.dumps(
            {"survey": {"project_path": root}}
        )

        resolved = ToolchainManager(orchestrator).resolve(
            ToolchainSpec(name="gradle", executable="gradle"),
            working_directory=island,
        )

        assert resolved is not None
        assert resolved.candidate.path == system_gradle
        assert resolved.candidate.source == "system"


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


def test_public_requirement_matcher_uses_same_range_semantics_as_resolution():
    manager = ToolchainManager(FakeToolchainOrchestrator())
    requirement = ToolVersionRequirement.from_raw("[3.9,4.0)")

    assert manager.matches_requirement("3.9.9", requirement) is True
    assert manager.matches_requirement("4.0.0", requirement) is False


def test_bare_resolution_inherits_observed_overlay_requirement():
    orchestrator = FakeToolchainOrchestrator(
        {
            "/opt/apache-maven-3.9.9/bin/mvn": "Apache Maven 3.9.9",
            "/usr/bin/mvn": "Apache Maven 3.8.7",
        },
        path_executable="/usr/bin/mvn",
    )
    orchestrator.files[DEFAULT_OVERLAY_JSON] = json.dumps(
        {
            "version": 1,
            "tools": {
                "maven": {
                    "requirements": [
                        {
                            "raw": "[3.9,)",
                            "source": "build_error",
                            "working_directory": "/workspace/project",
                        }
                    ],
                    "candidates": {},
                    "blocked": [],
                }
            },
        }
    )
    manager = ToolchainManager(orchestrator)

    resolved = manager.resolve(ToolchainSpec(name="maven", executable="mvn"))

    assert resolved is not None
    assert resolved.candidate.path == "/opt/apache-maven-3.9.9/bin/mvn"
    assert manager.observed_requirement("maven").raw == "[3.9,)"


def test_bare_resolution_intersects_same_root_history_without_polluting_sibling():
    orchestrator = FakeToolchainOrchestrator(
        {
            "/opt/apache-maven-3.8.8/bin/mvn": "Apache Maven 3.8.8",
            "/opt/apache-maven-3.9.9/bin/mvn": "Apache Maven 3.9.9",
        }
    )
    orchestrator.files[DEFAULT_OVERLAY_JSON] = json.dumps(
        {
            "version": 1,
            "tools": {
                "maven": {
                    "requirements": [
                        {
                            "raw": ">=3.9",
                            "source": "build_error",
                            "working_directory": "/workspace/project/island-a",
                        },
                        {
                            "raw": ">=3.8",
                            "source": "build_error",
                            "working_directory": "/workspace/project/island-a",
                        },
                        {
                            "raw": "[3.8,3.9)",
                            "source": "build_error",
                            "working_directory": "/workspace/project/island-b",
                        },
                    ],
                    "candidates": {},
                    "blocked": [],
                }
            },
        }
    )
    manager = ToolchainManager(orchestrator)
    spec = ToolchainSpec(name="maven", executable="mvn")

    island_a = manager.resolve(spec, working_directory="/workspace/project/island-a")
    island_b = manager.resolve(spec, working_directory="/workspace/project/island-b")

    assert island_a is not None
    assert island_a.candidate.version == "3.9.9"
    assert island_b is not None
    assert island_b.candidate.version == "3.8.8"


def test_parent_reactor_inherits_constraint_observed_in_child_module():
    orchestrator = FakeToolchainOrchestrator(
        {
            "/opt/apache-maven-3.8.8/bin/mvn": "Apache Maven 3.8.8",
            "/opt/apache-maven-3.9.9/bin/mvn": "Apache Maven 3.9.9",
        }
    )
    orchestrator.files[DEFAULT_OVERLAY_JSON] = json.dumps(
        {
            "version": 1,
            "tools": {
                "maven": {
                    "requirements": [
                        {
                            "raw": ">=3.9",
                            "source": "build_error",
                            "working_directory": "/workspace/project/module",
                        }
                    ],
                    "candidates": {},
                    "blocked": [],
                }
            },
        }
    )
    manager = ToolchainManager(orchestrator)

    resolved = manager.resolve(
        ToolchainSpec(name="maven", executable="mvn"),
        working_directory="/workspace/project",
    )

    assert resolved is not None
    assert resolved.candidate.version == "3.9.9"
    assert [
        item.raw
        for item in manager.observed_requirements(
            "maven",
            working_directory="/workspace/project",
        )
    ] == [">=3.9"]


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
