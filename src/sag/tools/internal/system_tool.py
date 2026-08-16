"""System management tool for package installation and system operations."""

import re
from typing import Any, Dict, List, Optional

from loguru import logger

from sag.runtime import EnvOverlayStore

from ..base import BaseTool, ToolError, ToolResult
from .java_versions import JAVA_VERIFICATION_SEPARATOR, java_major, parse_java_verification
from .maven_versions import (
    maven_distribution_for_floor,
    maven_installable_floors,
    normalize_maven_floor,
    parse_maven_version,
    satisfies_maven_floor,
)
from .toolchain_manager import record_registered_runtime

# The verification a provision must pass is the one a dispatch would run: bare
# `java` and `javac`, resolved through the same environment DockerOrchestrator
# builds for every project command. `command -v` states WHICH binary answered,
# so the stored block carries the resolution itself and not just a version.
JAVA_DOMAIN_VERIFICATION = (
    "command -v java 2>/dev/null; "
    f"java -version 2>&1 && echo '{JAVA_VERIFICATION_SEPARATOR}' && javac -version 2>&1"
)
# The same law for Maven: bare `mvn`, resolved exactly as a dispatch resolves
# it, plus the path that answered.
MAVEN_DOMAIN_VERIFICATION = "command -v mvn 2>/dev/null; mvn -version 2>&1"
# Apache publishes every Maven 3 binary distribution here. The containers this
# harness runs clone repositories over the network from the same place a build
# downloads its plugins, so a distribution is as reachable as a POM.
MAVEN_ARCHIVE_BASE = "https://archive.apache.org/dist/maven"
MAVEN_INSTALL_ROOT = "/opt"


class SystemTool(BaseTool):
    """Tool for system management operations like package installation."""

    def __init__(self, docker_orchestrator):
        super().__init__(
            name="system",
            description="Install system packages, manage dependencies, and perform system operations. "
            "Automatically detects missing dependencies and installs them.",
        )
        self.docker_orchestrator = docker_orchestrator

    def execute(
        self,
        action: str,
        packages: Optional[List[str]] = None,
        java_version: Optional[str] = None,
        maven_version: Optional[str] = None,
    ) -> ToolResult:
        """Execute system management operations."""
        # The base class now handles parameter validation automatically

        if action not in [
            "install",
            "update",
            "detect_missing",
            "install_missing",
            "install_java",
            "verify_java",
            "install_maven",
        ]:
            raise ToolError(
                message=f"Invalid action '{action}'. Must be 'install', 'update', 'detect_missing', 'install_missing', 'install_java', 'install_maven', or 'verify_java'",
                category="validation",
                error_code="INVALID_ACTION",
                suggestions=[
                    "Use 'install' to install specific packages",
                    "Use 'update' to update package lists",
                    "Use 'detect_missing' to check for missing dependencies",
                    "Use 'install_missing' to automatically install missing dependencies",
                    "Use 'install_java' to install and configure a specific Java version",
                    "Use 'install_maven' to install and activate a specific Maven version",
                    "Use 'verify_java' to check the current Java version",
                ],
                details={
                    "provided_action": action,
                    "valid_actions": [
                        "install",
                        "update",
                        "detect_missing",
                        "install_missing",
                        "install_java",
                        "install_maven",
                        "verify_java",
                    ],
                },
                retryable=True,
            )

        try:
            if action == "install":
                if not packages:
                    raise ToolError(
                        message="Packages list is required for 'install' action",
                        category="validation",
                        error_code="MISSING_PACKAGES",
                        suggestions=["Provide a list of packages to install"],
                        retryable=True,
                    )
                return self._install_packages(packages)

            elif action == "update":
                return self._update_packages()

            elif action == "detect_missing":
                return self._detect_missing_tools()

            elif action == "install_missing":
                if packages:
                    # Smart install: search for packages that provide the specified commands
                    return self._smart_install_commands(packages)
                else:
                    return self._install_missing_dependencies()

            elif action == "install_java":
                if not java_version:
                    raise ToolError(
                        message="Java version is required for 'install_java' action",
                        category="validation",
                        error_code="MISSING_VERSION",
                        suggestions=[
                            "Constraint: Java provisioning requires an explicit java_version",
                        ],
                        retryable=True,
                    )
                return self._install_and_configure_java(java_version)

            elif action == "install_maven":
                if not maven_version:
                    raise ToolError(
                        message="Maven version is required for 'install_maven' action",
                        category="validation",
                        error_code="MISSING_VERSION",
                        suggestions=[
                            "Constraint: Maven provisioning requires an explicit maven_version",
                            f"Observed installable Maven lines: {maven_installable_floors()}",
                        ],
                        retryable=True,
                    )
                return self._install_and_configure_maven(maven_version)

            elif action == "verify_java":
                if not java_version:
                    # Just check what Java version is installed
                    java_check = self.docker_orchestrator.execute_command("java -version 2>&1")
                    return ToolResult.completed_success(
                        output=java_check.get("output", "Java not installed"),
                        metadata={"exit_code": java_check.get("exit_code", -1)},
                    )
                else:
                    # Verify against a specific version
                    verification = self._verify_java_version(java_version)
                    if verification["matches"]:
                        return ToolResult.completed_success(
                            output=f"✅ Java {java_version} is installed and active",
                            metadata=verification,
                        )
                    elif verification["installed"]:
                        return ToolResult.completed_failure(
                            output=f"❌ Java version mismatch: Required {java_version}, but found {verification['current_version']}",
                            error=f"Java version mismatch",
                            error_code="JAVA_VERSION_MISMATCH",
                            suggestions=[
                                f"Observed active Java major version: {verification['current_version']}",
                                f"Constraint: requested Java major version is {java_version}",
                            ],
                            metadata=verification,
                        )
                    else:
                        return ToolResult.completed_failure(
                            output="Java is not installed",
                            error="Java not found",
                            error_code="JAVA_NOT_INSTALLED",
                            suggestions=[
                                "Observed capability: no active Java executable",
                                f"Constraint: requested Java major version is {java_version}",
                            ],
                            metadata=verification,
                        )

        except ToolError:
            # Re-raise ToolErrors without wrapping them
            raise
        except Exception as e:
            error_msg = f"System operation failed: {str(e)}"
            logger.error(f"System tool error for action '{action}': {error_msg}")
            return ToolResult.completed_failure(
                output="",
                error=error_msg,
                error_code="SYSTEM_ERROR",
                suggestions=[
                    "Observed category: system provision operation raised an exception",
                    "Relevant constraints: container availability, network, and package index state",
                ],
            )

    def _install_packages(self, packages: List[str]) -> ToolResult:
        """Install system packages using apt-get."""
        if not packages:
            return ToolResult.completed_success(
                output="No packages to install", metadata={"packages": []}
            )

        # Update package lists first
        update_result = self._update_packages()
        if not update_result.succeeded:
            logger.warning(f"Failed to update package lists: {update_result.error}")

        # Install packages
        packages_str = " ".join(packages)
        command = f"apt-get install -y {packages_str}"

        logger.info(f"Installing packages: {packages_str}")

        result = self.docker_orchestrator.execute_command(command=command)

        if result["exit_code"] == 0:
            return ToolResult.completed_success(
                output=f"Successfully installed packages: {packages_str}\n\n{result['output']}",
                metadata={
                    "packages": packages,
                    "exit_code": result["exit_code"],
                    "command": command,
                },
            )
        else:
            # Try to provide helpful error analysis
            error_analysis = self._analyze_install_error(result["output"])

            return ToolResult.completed_failure(
                output=result["output"],
                error=f"Failed to install packages: {packages_str}",
                error_code="INSTALL_FAILED",
                suggestions=error_analysis.get("suggestions", []),
                documentation_links=error_analysis.get("docs", []),
                metadata={
                    "packages": packages,
                    "exit_code": result["exit_code"],
                    "command": command,
                },
            )

    def _update_packages(self) -> ToolResult:
        """Update package lists."""
        command = "apt-get update"

        logger.info("Updating package lists")

        result = self.docker_orchestrator.execute_command(command=command)

        if result["exit_code"] == 0:
            return ToolResult.completed_success(
                output=f"Successfully updated package lists\n\n{result['output']}",
                metadata={"exit_code": result["exit_code"], "command": command},
            )
        else:
            return ToolResult.completed_failure(
                output=result["output"],
                error="Failed to update package lists",
                error_code="UPDATE_FAILED",
                suggestions=[
                    "Check network connectivity",
                    "Try running the command again",
                    "Verify repository URLs are accessible",
                ],
                metadata={"exit_code": result["exit_code"], "command": command},
            )

    def _detect_missing_tools(self) -> ToolResult:
        """Detect missing development tools and dependencies."""

        # Common development tools to check
        required_tools = [
            "git",
            "curl",
            "wget",
            "python3",
            "pip",
            "node",
            "npm",
            "java",
            "javac",
            "mvn",
            "make",
            "gcc",
            "g++",
        ]

        missing_tools = []

        for tool in required_tools:
            check_result = self.docker_orchestrator.execute_command(command=f"which {tool}")

            if check_result["exit_code"] != 0:
                missing_tools.append(
                    {
                        "tool": tool,
                        "packages": [tool],  # Assuming a package name is the tool itself
                        "check_command": f"which {tool}",
                    }
                )

        if missing_tools:
            output = "Missing development tools detected:\n\n"
            for tool_info in missing_tools:
                output += f"• {tool_info['tool']}: Install with 'apt-get install {' '.join(tool_info['packages'])}'\n"

            return ToolResult.completed_success(
                output=output,
                metadata={"missing_tools": missing_tools, "total_missing": len(missing_tools)},
            )
        else:
            return ToolResult.completed_success(
                output="All common development tools are available",
                metadata={"missing_tools": [], "total_missing": 0},
            )

    def _install_missing_dependencies(self) -> ToolResult:
        """Detect and install missing dependencies automatically."""

        # First detect what's missing
        detect_result = self._detect_missing_tools()
        if not detect_result.succeeded:
            return detect_result

        missing_tools = detect_result.metadata.get("missing_tools", [])

        if not missing_tools:
            return ToolResult.completed_success(
                output="No missing dependencies detected",
                metadata={"installed_packages": []},
            )

        # Collect all packages to install
        packages_to_install = []
        for tool_info in missing_tools:
            packages_to_install.extend(tool_info["packages"])

        # Remove duplicates
        packages_to_install = list(set(packages_to_install))

        # Install all missing packages
        install_result = self._install_packages(packages_to_install)

        if install_result.succeeded:
            return ToolResult.completed_success(
                output=f"Successfully installed missing dependencies: {', '.join(packages_to_install)}\n\n{install_result.output}",
                metadata={
                    "missing_tools": missing_tools,
                    "installed_packages": packages_to_install,
                    "total_installed": len(packages_to_install),
                },
            )
        else:
            return ToolResult.completed_failure(
                output=install_result.output,
                error=f"Failed to install some dependencies: {install_result.error}",
                error_code="INSTALL_MISSING_FAILED",
                suggestions=install_result.suggestions,
                documentation_links=install_result.documentation_links,
                metadata={"missing_tools": missing_tools, "failed_packages": packages_to_install},
            )

    def _verify_java_version(self, required_version: str) -> Dict[str, Any]:
        """
        Verify the current Java version against the required version.

        Returns:
            Dict with keys:
            - installed: bool (whether Java is installed)
            - current_version: str (current Java version, e.g., "11", "17")
            - required_version: str (required Java version)
            - matches: bool (whether current matches required)
            - raw_output: str (raw java -version output)
        """
        result = {
            "installed": False,
            "current_version": None,
            "required_version": required_version,
            "matches": False,
            "raw_output": "",
        }

        # Execute java -version command
        java_check = self.docker_orchestrator.execute_command("java -version 2>&1")
        result["raw_output"] = java_check.get("output", "")

        if java_check.get("exit_code") != 0 or "command not found" in result["raw_output"]:
            logger.info("Java is not installed")
            return result

        # One reader for every quoted JVM version string this tool meets:
        # `openjdk version "17.0.8"`, `java version "1.8.0_361"` (major 8, not 1).
        reported = parse_java_verification(result["raw_output"])["java_version"]
        version = java_major(reported)
        if version:
            result["installed"] = True
            result["current_version"] = version
            result["matches"] = version == java_major(required_version)
            logger.info(f"Detected Java version: {version} (required: {required_version})")
        elif reported:
            # Java answered with something this reader cannot major-ize.
            logger.warning(f"Could not parse Java version from: {result['raw_output']}")
            result["installed"] = True
            result["current_version"] = "unknown"

        return result

    def _install_and_configure_java(self, java_version: str) -> ToolResult:
        """Install and configure a specific Java version."""
        logger.info(f"Installing and configuring Java {java_version}")

        # Step 1: Check current Java version using the new verification method
        version_check = self._verify_java_version(java_version)
        logger.info(f"Current Java status: {version_check}")

        # If the correct version is already installed, just configure it
        if version_check["matches"]:
            logger.info(f"Java {java_version} is already installed and active")
            return ToolResult.completed_success(
                output=f"Java {java_version} is already installed and configured\n\n{version_check['raw_output']}",
                metadata=version_check,
            )

        # Step 2: Update package lists
        update_result = self._update_packages()
        if not update_result.succeeded:
            logger.warning("Failed to update package lists, continuing anyway")

        # Step 3: Install OpenJDK
        java_package = f"openjdk-{java_version}-jdk"
        install_cmd = f"apt-get install -y {java_package}"
        logger.info(f"Installing {java_package}")

        install_result = self.docker_orchestrator.execute_command(install_cmd)

        if install_result["exit_code"] != 0:
            # Try alternative package names
            alt_packages = [
                f"openjdk-{java_version}-jdk-headless",
                f"java-{java_version}-openjdk",
                f"java-{java_version}-openjdk-devel",
            ]

            for alt_package in alt_packages:
                logger.info(f"Trying alternative package: {alt_package}")
                alt_result = self.docker_orchestrator.execute_command(
                    f"apt-get install -y {alt_package}"
                )
                if alt_result["exit_code"] == 0:
                    install_result = alt_result
                    java_package = alt_package
                    break
            else:
                return ToolResult.completed_failure(
                    output=install_result["output"],
                    error=f"Failed to install Java {java_version}",
                    error_code="JAVA_INSTALL_FAILED",
                    suggestions=[
                        f"Observed fact: packages for Java {java_version} were not installable",
                        "Constraint: a compatible JDK package must exist in the configured repositories",
                    ],
                )

        # Step 4: Get architecture for Java home path - ENHANCED VERSION
        arch_result = self.docker_orchestrator.execute_command("dpkg --print-architecture")
        arch = arch_result.get("output", "").strip()

        # Don't fallback to amd64 - detect properly
        if not arch:
            # Try alternative detection methods
            uname_result = self.docker_orchestrator.execute_command("uname -m")
            machine = uname_result.get("output", "").strip()

            # Map machine architecture to dpkg architecture
            arch_mapping = {
                "x86_64": "amd64",
                "aarch64": "arm64",
                "armv7l": "armhf",
                "ppc64le": "ppc64el",
                "s390x": "s390x",
            }
            arch = arch_mapping.get(machine, "")

            if not arch:
                logger.warning(f"Could not detect architecture, machine type: {machine}")
                # Last resort: scan what's actually installed
                scan_result = self.docker_orchestrator.execute_command(
                    f"ls -d /usr/lib/jvm/java-{java_version}-openjdk-* 2>/dev/null | head -1"
                )
                if scan_result.get("output"):
                    installed_path = scan_result["output"].strip()
                    # Extract architecture from path
                    import re

                    match = re.search(r"openjdk-([^/]+)$", installed_path)
                    if match:
                        arch = match.group(1)
                        logger.info(f"Detected architecture from installed path: {arch}")

        logger.info(f"Detected system architecture: {arch}")

        # Step 5: Set JAVA_HOME and update alternatives with better verification
        java_home = f"/usr/lib/jvm/java-{java_version}-openjdk-{arch}"
        java_bin = f"{java_home}/bin/java"
        javac_bin = f"{java_home}/bin/javac"

        # Enhanced verification: Check if the Java installation exists
        check_java = self.docker_orchestrator.execute_command(
            f"test -f {java_bin} && echo 'exists'"
        )
        if "exists" not in check_java.get("output", ""):
            logger.info(f"Java binary not found at {java_bin}, searching for actual installation")

            # Try to find the actual Java installation with more specific search
            find_cmds = [
                # First try: exact version match
                f"find /usr/lib/jvm -name 'java-{java_version}-openjdk*' -type d | head -1",
                # Second try: look for any java installation of this version
                f"ls -d /usr/lib/jvm/*{java_version}* 2>/dev/null | grep -v '.jinfo' | head -1",
                # Third try: check default-java symlink
                f"readlink -f /usr/lib/jvm/default-java | grep -q {java_version} && readlink -f /usr/lib/jvm/default-java",
            ]

            for find_cmd in find_cmds:
                find_java = self.docker_orchestrator.execute_command(find_cmd)
                if find_java.get("output") and find_java["output"].strip():
                    java_home = find_java["output"].strip()
                    java_bin = f"{java_home}/bin/java"
                    javac_bin = f"{java_home}/bin/javac"

                    # Verify the binaries actually exist
                    verify_result = self.docker_orchestrator.execute_command(
                        f"test -f {java_bin} && test -f {javac_bin} && echo 'verified'"
                    )
                    if "verified" in verify_result.get("output", ""):
                        logger.info(f"Found Java installation at: {java_home}")
                        break
            else:
                # Could not find Java installation
                return ToolResult.completed_failure(
                    output=f"Java {java_version} was installed but cannot find the binaries",
                    error="Java binaries not found",
                    error_code="JAVA_BINARIES_NOT_FOUND",
                    suggestions=[
                        "Observed fact: installation completed without discoverable java and javac binaries",
                        f"Constraint: Java {java_version} must expose both executables before activation",
                    ],
                )

        # Step 6: What the exact binaries say about themselves, asked by path,
        # before anything is switched. A refusal here leaves the container
        # exactly as it was found.
        probe_result = self.docker_orchestrator.execute_command(
            f"{java_bin} -version 2>&1 && echo '{JAVA_VERIFICATION_SEPARATOR}' "
            f"&& {javac_bin} -version 2>&1"
        )
        if probe_result["exit_code"] != 0:
            return ToolResult.completed_failure(
                output=probe_result["output"],
                error=f"Java {java_version} installed but verification failed",
                error_code="JAVA_CONFIG_FAILED",
                suggestions=[
                    "Observed fact: the installed Java runtime failed post-install verification",
                    f"Observed candidate JAVA_HOME: {java_home}",
                    "Constraint: java and javac must both execute under the activated environment",
                ],
            )
        # The verification block is evidence, not decoration. Live
        # camel-quarkus d2r3 (seq 127) sealed `java_version: "17"` over a block
        # reading `openjdk version "11.0.31"` / `javac 11.0.31`, because only
        # the exit code was read. A provision may not seal the version its own
        # verification disproves.
        contradiction = self._verification_contradiction(
            java_version,
            java_home,
            probe_result["output"],
        )
        if contradiction is not None:
            return contradiction

        # Step 7: Land the switch in the domain that resolves `java` — both
        # halves of it. The /usr/bin links answer for anything that resolves
        # through PATH's system directories; the persisted overlay is what
        # DockerOrchestrator builds every project command's environment from,
        # so it fronts both the verification shell and every later dispatch.
        self._persist_java_home_profile(java_home)
        alternatives = self._set_java_alternatives(java_bin, javac_bin)
        activation_failure = self._activate_java_runtime(java_home, java_version)
        if activation_failure is not None:
            return activation_failure

        # Step 8: Verify in THAT domain. Live lucene d2r4 (seq 93/94) asked for
        # Java 21 with Java 17 active: the JDK 21 install was honest, JAVA_HOME
        # named it, and the verification shell still resolved the java-17 bin
        # directory the previous provision had made active — so an honest
        # switch was refused and the build stayed on 17. What the provision
        # claims and what the next dispatch runs are now one observation.
        verify_result = self.docker_orchestrator.execute_command(JAVA_DOMAIN_VERIFICATION)
        contradiction = self._verification_contradiction(
            java_version,
            java_home,
            verify_result["output"],
        )
        if contradiction is not None:
            return contradiction

        observed = parse_java_verification(verify_result["output"])
        return ToolResult.completed_success(
            output=f"Successfully installed and configured Java {java_version}\n\n"
            f"JAVA_HOME: {java_home}\n"
            f"Verification:\n{verify_result['output']}",
            metadata={
                "java_version": java_version,
                "java_home": java_home,
                "package": java_package,
                "architecture": arch,
                "verified_java_version": observed["java_version"],
                "verified_javac_version": observed["javac_version"],
                "resolved_java_executable": self._resolved_executable(verify_result["output"]),
                "alternatives_set": alternatives,
            },
        )

    def _install_and_configure_maven(self, maven_version: str) -> ToolResult:
        """Install an Apache Maven distribution and make it the resolved runtime.

        The wall this answers, twice over in d2r4. camel
        (`logs/d2r4-20260815/slices/camel.md`): the repo pins the
        `eu.maveniverse.maven.nisse` 0.8.4 build extensions, the container ran
        apt's Maven 3.8.7, and the reactor printed BUILD SUCCESS and then died
        with `NoSuchMethodError: org.eclipse.aether.SessionData.computeIfAbsent`
        — exit 1, no reports, `compiled_classes 0`. jackrabbit and gora asked
        for `[3.9,)` against the same 3.8.7. No call existed that could install
        another Maven, so `maven_version` appears zero times in camel's entire
        control-events file.

        The discipline is task #59's, unchanged: probe the exact binary by path
        before anything moves, land the switch in the persisted overlay —
        DockerOrchestrator derives every project command's environment from it
        — and verify with a bare `mvn` resolved exactly as a dispatch resolves
        it. An activation the domain does not answer for is a refusal.
        """
        floor = normalize_maven_floor(maven_version)
        if floor is None:
            return ToolResult.completed_failure(
                output="",
                error=f"Maven version is not a version floor: {maven_version!r}",
                error_code="MAVEN_VERSION_INVALID",
                suggestions=[
                    "Constraint: maven_version is a floor spelled major[.minor[.patch]], "
                    "for example '3.9'",
                    f"Observed installable Maven lines: {maven_installable_floors()}",
                ],
                metadata={"requested_maven_version": maven_version},
            )
        distribution = maven_distribution_for_floor(floor)
        if distribution is None:
            return ToolResult.completed_failure(
                output="",
                error=f"No Apache Maven distribution is known for floor {floor}",
                error_code="MAVEN_DISTRIBUTION_UNKNOWN",
                suggestions=[
                    f"Observed installable Maven lines: {maven_installable_floors()}",
                    "Constraint: a floor naming an exact patch (for example '3.9.11') "
                    "installs exactly that distribution",
                ],
                metadata={"maven_version_floor": floor},
            )

        # What the container already resolves, asked the way a dispatch asks.
        # A Maven that already satisfies the floor is the provision's answer;
        # downloading over it would swap a working runtime for no reason.
        current = self.docker_orchestrator.execute_command(MAVEN_DOMAIN_VERIFICATION)
        active_version = parse_maven_version(current.get("output"))
        if active_version and satisfies_maven_floor(active_version, floor):
            return ToolResult.completed_success(
                output=f"Maven {active_version} already satisfies {floor}\n\n"
                f"Verification:\n{current.get('output', '')}",
                metadata={
                    "maven_version": active_version,
                    "maven_version_floor": floor,
                    "already_active": True,
                    "verified_maven_version": active_version,
                    "resolved_maven_executable": self._resolved_executable(
                        current.get("output", "")
                    ),
                },
            )

        maven_home = f"{MAVEN_INSTALL_ROOT}/apache-maven-{distribution}"
        maven_bin = f"{maven_home}/bin/mvn"
        archive = f"/tmp/apache-maven-{distribution}-bin.tar.gz"
        major = distribution.split(".")[0]
        url = (
            f"{MAVEN_ARCHIVE_BASE}/maven-{major}/{distribution}/binaries/"
            f"apache-maven-{distribution}-bin.tar.gz"
        )
        install_result = self.docker_orchestrator.execute_command(
            f"mkdir -p {MAVEN_INSTALL_ROOT} && "
            f"(curl -fsSL {url} -o {archive} || wget -qO {archive} {url}) && "
            f"tar -xzf {archive} -C {MAVEN_INSTALL_ROOT}"
        )
        if install_result.get("exit_code") != 0:
            return ToolResult.completed_failure(
                output=install_result.get("output", ""),
                error=f"Failed to install Apache Maven {distribution}",
                error_code="MAVEN_INSTALL_FAILED",
                suggestions=[
                    f"Observed fact: the distribution at {url} was not downloaded and extracted",
                    "Relevant constraints: container network reachability and writable /opt",
                ],
                metadata={
                    "maven_version_floor": floor,
                    "requested_distribution": distribution,
                    "distribution_url": url,
                },
            )

        present = self.docker_orchestrator.execute_command(f"test -x {maven_bin} && echo 'present'")
        if "present" not in present.get("output", ""):
            return ToolResult.completed_failure(
                output=present.get("output", ""),
                error=f"Apache Maven {distribution} was installed but exposes no {maven_bin}",
                error_code="MAVEN_BINARY_NOT_FOUND",
                suggestions=[
                    "Observed fact: the extracted distribution has no executable bin/mvn",
                    f"Observed candidate MAVEN_HOME: {maven_home}",
                ],
                metadata={"maven_version_floor": floor, "maven_home": maven_home},
            )

        # What the exact binary says about itself, asked by path, before
        # anything is switched. A refusal here leaves the container as found.
        probe_result = self.docker_orchestrator.execute_command(f"{maven_bin} -version 2>&1")
        if probe_result.get("exit_code") != 0:
            return ToolResult.completed_failure(
                output=probe_result.get("output", ""),
                error=f"Apache Maven {distribution} installed but verification failed",
                error_code="MAVEN_CONFIG_FAILED",
                suggestions=[
                    "Observed fact: the installed Maven failed its own post-install probe",
                    f"Observed candidate MAVEN_HOME: {maven_home}",
                ],
                metadata={"maven_version_floor": floor, "maven_home": maven_home},
            )
        contradiction = self._maven_verification_contradiction(
            floor,
            maven_home,
            probe_result.get("output", ""),
        )
        if contradiction is not None:
            return contradiction

        activation_failure = self._activate_maven_runtime(
            maven_home,
            maven_bin,
            parse_maven_version(probe_result.get("output")),
            floor,
        )
        if activation_failure is not None:
            return activation_failure

        verify_result = self.docker_orchestrator.execute_command(MAVEN_DOMAIN_VERIFICATION)
        contradiction = self._maven_verification_contradiction(
            floor,
            maven_home,
            verify_result.get("output", ""),
        )
        if contradiction is not None:
            return contradiction

        observed = parse_maven_version(verify_result.get("output"))
        return ToolResult.completed_success(
            output=f"Successfully installed and activated Apache Maven {observed} "
            f"for floor {floor}\n\n"
            f"MAVEN_HOME: {maven_home}\n"
            f"Verification:\n{verify_result.get('output', '')}",
            metadata={
                "maven_version": observed,
                "maven_version_floor": floor,
                "maven_home": maven_home,
                "distribution_url": url,
                "verified_maven_version": observed,
                "resolved_maven_executable": self._resolved_executable(
                    verify_result.get("output", "")
                ),
                "already_active": False,
            },
        )

    def _maven_verification_contradiction(
        self,
        floor: str,
        maven_home: str,
        verification_output: str,
    ) -> Optional[ToolResult]:
        """Refuse the seal when the verification block does not confirm the floor.

        Two ways it can go unproven, and they are the same refusal: a block
        naming a version below the floor — jackrabbit's 3.8.7 answering for
        `[3.9,)` — and a block naming no Maven at all.
        """
        observed = parse_maven_version(verification_output)
        stored_output = (
            f"Maven {floor} was requested\n\n"
            f"MAVEN_HOME: {maven_home}\n"
            f"Verification:\n{verification_output}"
        )
        metadata = {
            "maven_version_floor": floor,
            "maven_home": maven_home,
            "verified_maven_version": observed,
        }
        if not observed:
            return ToolResult.completed_failure(
                output=stored_output,
                error=(
                    f"Maven {floor} installation is unverified: its own verification "
                    "block names no version"
                ),
                error_code="MAVEN_VERSION_UNVERIFIED",
                suggestions=[
                    "Observed fact: mvn -version named no Apache Maven version",
                    f"Observed candidate MAVEN_HOME: {maven_home}",
                    f"Constraint: the active runtime must be observed to report Maven >= {floor}",
                ],
                metadata=metadata,
            )
        if satisfies_maven_floor(observed, floor):
            return None
        return ToolResult.completed_failure(
            output=stored_output,
            error=f"Maven provisioning claimed {floor} but its verification reports {observed}",
            error_code="MAVEN_VERSION_VERIFICATION_MISMATCH",
            suggestions=[
                f"Observed fact: verification under MAVEN_HOME={maven_home} reported "
                f"Maven {observed}",
                f"Constraint: the requested Maven floor is {floor}",
                "Observed fact: a distribution can be installed while the mvn the domain "
                "resolves is another one — the resolved executable is what a build runs",
            ],
            metadata=metadata,
        )

    def _activate_maven_runtime(
        self,
        maven_home: str,
        maven_bin: str,
        version: Optional[str],
        floor: str,
    ) -> Optional[ToolResult]:
        """Make the proven distribution the Maven this container resolves.

        The overlay is the resolution domain and the registry is the durable
        runtime inventory resolution and reporting read; a registration that
        reaches only one of them cannot reach the build. The registry write is
        best effort — it never fails a switch that landed where it matters.
        """
        try:
            EnvOverlayStore(self.docker_orchestrator).register(
                "maven",
                maven_bin,
                version=version,
                source="system_install",
                env={"MAVEN_HOME": maven_home},
                path_prepend=[f"{maven_home}/bin"],
                activate=True,
            )
        except Exception as exc:
            logger.warning(f"Failed to register Maven env overlay: {exc}")
            return ToolResult.completed_failure(
                output=(
                    f"Apache Maven {version or floor} was installed at {maven_home} "
                    f"and not activated\n\n{exc}"
                ),
                error=(
                    f"Apache Maven {version or floor} was installed but its activation "
                    "did not persist"
                ),
                error_code="MAVEN_RUNTIME_ACTIVATION_FAILED",
                suggestions=[
                    f"Observed fact: the runtime overlay that resolves mvn did not accept "
                    f"{maven_bin}",
                    "Observed fact: until it is activated, every dispatch keeps resolving the "
                    "previously active Maven",
                    f"Constraint: a provisioned Maven {floor} must be the runtime later "
                    "dispatches resolve",
                ],
                metadata={
                    "maven_version_floor": floor,
                    "maven_home": maven_home,
                    "activation_error": str(exc),
                },
            )
        record_registered_runtime(
            self.docker_orchestrator,
            "maven",
            maven_bin,
            version=version,
            source="registered",
        )
        return None

    def _persist_java_home_profile(self, java_home: str) -> None:
        """Write the login-shell files. No runner sources them; they are forensic."""
        for command in (
            f"echo 'export JAVA_HOME={java_home}' >> /etc/profile",
            "echo 'export PATH=$JAVA_HOME/bin:$PATH' >> /etc/profile",
            f"echo 'export JAVA_HOME={java_home}' >> /root/.bashrc",
            "echo 'export PATH=$JAVA_HOME/bin:$PATH' >> /root/.bashrc",
        ):
            result = self.docker_orchestrator.execute_command(command)
            if result["exit_code"] != 0:
                logger.warning(f"Failed to execute: {command} - {result.get('output', '')[:100]}")

    def _set_java_alternatives(self, java_bin: str, javac_bin: str) -> Dict[str, bool]:
        """Point each /usr/bin link at this JDK: java at java, javac at javac."""
        outcome: Dict[str, bool] = {}
        for name, binary in (("java", java_bin), ("javac", javac_bin)):
            listed = self.docker_orchestrator.execute_command(
                f"update-alternatives --list {name} 2>/dev/null"
            )
            if binary in listed.get("output", ""):
                logger.info(f"{name} alternative already registered: {binary}")
            else:
                self.docker_orchestrator.execute_command(
                    f"update-alternatives --install /usr/bin/{name} {name} {binary} 100"
                )
            outcome[name] = self._set_java_alternative(name, binary)
        return outcome

    def _set_java_alternative(self, name: str, binary: str) -> bool:
        """Set one alternative, repairing only the link it belongs to.

        The repair used to rewrite every failing `--set` into an `--install
        /usr/bin/java`, so repairing `javac` registered the compiler against
        the `java` link — the one link a JDK switch exists to point correctly.
        """
        set_command = f"update-alternatives --set {name} {binary}"
        result = self.docker_orchestrator.execute_command(set_command)
        if result["exit_code"] == 0:
            return True
        if "not registered" in result.get("output", ""):
            logger.warning(f"Alternatives not registered properly, attempting to fix: {name}")
            self.docker_orchestrator.execute_command(
                f"update-alternatives --install /usr/bin/{name} {name} {binary} 100"
            )
            result = self.docker_orchestrator.execute_command(set_command)
            if result["exit_code"] == 0:
                logger.info(f"Fixed alternatives registration for: {set_command}")
                return True
        logger.warning(f"Failed to execute: {set_command} - {result.get('output', '')[:100]}")
        return False

    @staticmethod
    def _resolved_executable(verification_output: str) -> str:
        """The binary the domain named when it answered, or an empty string."""
        for line in str(verification_output or "").splitlines():
            candidate = line.strip()
            if candidate.startswith("/"):
                return candidate
        return ""

    def _verification_contradiction(
        self,
        java_version: str,
        java_home: str,
        verification_output: str,
    ) -> Optional[ToolResult]:
        """Refuse the seal when the verification block does not confirm the claim.

        A requested major is a version requirement, so the two ways it can go
        unproven are refusals of the same family: a block naming a different
        major, and a block naming no major at all.
        """
        observed = parse_java_verification(verification_output)
        claimed_major = java_major(java_version)
        # What the stored output must say on its own, months later: which
        # version was asked for, and what the container answered.
        stored_output = (
            f"Java {java_version} was requested\n\n"
            f"JAVA_HOME: {java_home}\n"
            f"Verification:\n{verification_output}"
        )
        verified = {
            name: version
            for name, version in (
                ("java", observed["java_version"]),
                ("javac", observed["javac_version"]),
            )
            if version
        }
        metadata = {
            "claimed_java_version": java_version,
            "java_home": java_home,
            "verified_java_version": observed["java_version"],
            "verified_javac_version": observed["javac_version"],
        }
        if not verified:
            return ToolResult.completed_failure(
                output=stored_output,
                error=(
                    f"Java {java_version} installation is unverified: its own verification "
                    "block names no version"
                ),
                error_code="JAVA_VERSION_UNVERIFIED",
                suggestions=[
                    "Observed fact: neither java -version nor javac -version named a version",
                    f"Observed candidate JAVA_HOME: {java_home}",
                    f"Constraint: the active runtime must be observed to report Java {java_version}",
                ],
                metadata=metadata,
            )
        disagreeing = {
            name: version
            for name, version in verified.items()
            if java_major(version) != claimed_major
        }
        if not disagreeing:
            return None
        stated = ", ".join(f"{name} {version}" for name, version in sorted(disagreeing.items()))
        return ToolResult.completed_failure(
            output=stored_output,
            error=(
                f"Java provisioning claimed {java_version} but its verification reports {stated}"
            ),
            error_code="JAVA_VERSION_VERIFICATION_MISMATCH",
            suggestions=[
                f"Observed fact: verification under JAVA_HOME={java_home} reported {stated}",
                f"Constraint: the requested Java major version is {java_version}",
                "Observed fact: the requested JDK may be installed while the active java "
                "on PATH is another one — verify the exact binary before registering it",
            ],
            metadata=metadata,
        )

    def _activate_java_runtime(self, java_home: str, java_version: str) -> Optional[ToolResult]:
        """Make the proven JDK the runtime this container resolves.

        The overlay is the resolution domain: DockerOrchestrator derives every
        project command's environment from it, so an activation that did not
        persist leaves the verification shell and every later dispatch on the
        JDK this call was asked to replace. A switch that did not land is not
        a configured runtime, and reporting it as one is how a run spends the
        rest of itself building against the wrong JVM.
        """
        java_home = java_home.rstrip("/")
        java_bin = f"{java_home}/bin/java"
        try:
            EnvOverlayStore(self.docker_orchestrator).register(
                "java",
                java_bin,
                version=java_version,
                source="system_install",
                env={"JAVA_HOME": java_home},
                path_prepend=[f"{java_home}/bin"],
                activate=True,
            )
        except Exception as exc:
            logger.warning(f"Failed to register Java env overlay: {exc}")
            return ToolResult.completed_failure(
                output=(
                    f"Java {java_version} was installed at {java_home} and not activated\n\n{exc}"
                ),
                error=f"Java {java_version} was installed but its activation did not persist",
                error_code="JAVA_RUNTIME_ACTIVATION_FAILED",
                suggestions=[
                    "Observed fact: the runtime overlay that resolves java did not accept "
                    f"{java_bin}",
                    "Observed fact: until it is activated, every dispatch keeps resolving the "
                    "previously active runtime",
                    f"Constraint: a provisioned Java {java_version} must be the runtime later "
                    "dispatches resolve",
                ],
                metadata={
                    "claimed_java_version": java_version,
                    "java_home": java_home,
                    "activation_error": str(exc),
                },
            )
        return None

    def _smart_install_commands(self, commands: List[str]) -> ToolResult:
        """
        Intelligently install packages that provide the specified commands.
        This solves issues like trying to install 'javac' when 'default-jdk' is needed.
        """
        logger.info(f"Smart installing commands: {commands}")

        # Common command-to-package mappings
        command_mappings = {
            "javac": ["default-jdk", "openjdk-11-jdk", "openjdk-8-jdk"],
            "java": ["default-jre", "openjdk-11-jre", "openjdk-8-jre"],
            "mvn": ["maven"],
            "node": ["nodejs"],
            "npm": ["npm"],
            "python3": ["python3"],
            "pip3": ["python3-pip"],
            "git": ["git"],
            "curl": ["curl"],
            "wget": ["wget"],
            "gcc": ["build-essential", "gcc"],
            "g++": ["build-essential", "g++"],
            "make": ["build-essential", "make"],
            "cmake": ["cmake"],
            "docker": ["docker.io", "docker-ce"],
            "zip": ["zip"],
            "unzip": ["unzip"],
            "vim": ["vim"],
            "nano": ["nano"],
            "grep": ["grep"],
            "find": ["findutils"],
            "which": ["debianutils"],
            "awk": ["gawk"],
            "sed": ["sed"],
        }

        packages_to_install = []
        search_results = []

        for command in commands:
            command = command.strip()

            # First, check if command already exists
            check_result = self.docker_orchestrator.execute_command(f"which {command}")
            if check_result["exit_code"] == 0:
                search_results.append(
                    f"✅ {command}: already available at {check_result['output'].strip()}"
                )
                continue

            # Check our known mappings first
            if command in command_mappings:
                packages = command_mappings[command]
                packages_to_install.extend(packages)
                search_results.append(f"📦 {command}: mapped to {', '.join(packages)}")
                continue

            # Try to search for package using apt-file or dpkg
            package_search_result = self._search_package_for_command(command)
            if package_search_result:
                packages_to_install.extend(package_search_result)
                search_results.append(f"🔍 {command}: found in {', '.join(package_search_result)}")
            else:
                # Fallback: try installing the command name directly
                packages_to_install.append(command)
                search_results.append(f"⚠️ {command}: trying direct install (fallback)")

        # Remove duplicates while preserving order
        unique_packages = []
        for pkg in packages_to_install:
            if pkg not in unique_packages:
                unique_packages.append(pkg)

        if not unique_packages:
            return ToolResult.completed_success(
                output="All requested commands are already available.\n"
                + "\n".join(search_results),
                metadata={
                    "commands": commands,
                    "packages_installed": [],
                    "search_results": search_results,
                },
            )

        # Install the discovered packages
        logger.info(f"Installing packages for commands: {unique_packages}")
        install_result = self._install_packages(unique_packages)

        # Enhance the output with search information
        if install_result.succeeded:
            enhanced_output = f"Smart installation completed!\n\n"
            enhanced_output += f"Command analysis:\n" + "\n".join(search_results) + "\n\n"
            enhanced_output += f"Installed packages: {', '.join(unique_packages)}\n\n"
            enhanced_output += install_result.output

            return ToolResult.completed_success(
                output=enhanced_output,
                metadata={
                    "commands": commands,
                    "packages_installed": unique_packages,
                    "search_results": search_results,
                    "install_output": install_result.output,
                },
            )
        else:
            return install_result

    def _search_package_for_command(self, command: str) -> List[str]:
        """
        Search for packages that provide a specific command.
        Uses multiple fallback strategies.
        """
        packages = []

        # Strategy 1: Try apt-file if available
        apt_file_result = self.docker_orchestrator.execute_command(f"apt-file search bin/{command}")
        if apt_file_result["exit_code"] == 0 and apt_file_result["output"]:
            # Parse apt-file output: "package: /usr/bin/command"
            for line in apt_file_result["output"].split("\n"):
                if f"bin/{command}" in line and ":" in line:
                    package = line.split(":")[0].strip()
                    if package and package not in packages:
                        packages.append(package)

        # Strategy 2: Try dpkg search if apt-file failed
        if not packages:
            dpkg_result = self.docker_orchestrator.execute_command(
                f"dpkg -S $(which {command} 2>/dev/null) 2>/dev/null"
            )
            if dpkg_result["exit_code"] == 0 and dpkg_result["output"]:
                for line in dpkg_result["output"].split("\n"):
                    if ":" in line:
                        package = line.split(":")[0].strip()
                        if package and package not in packages:
                            packages.append(package)

        # Strategy 3: Try command-not-found if available
        if not packages:
            cnf_result = self.docker_orchestrator.execute_command(
                f"command-not-found {command} 2>&1 | grep 'apt install' || true"
            )
            if cnf_result["exit_code"] == 0 and cnf_result["output"]:
                # Extract package names from "apt install package1 package2" suggestions
                import re

                matches = re.findall(r"apt install\s+([^\n]+)", cnf_result["output"])
                for match in matches:
                    suggested_packages = match.strip().split()
                    packages.extend(suggested_packages)

        # Return up to 3 most relevant packages to avoid installing too much
        return packages[:3] if packages else []

    def _analyze_install_error(self, error_output: str) -> Dict[str, Any]:
        """Classify package-install output without selecting a repair command."""
        suggestions = []
        docs = []

        error_lower = error_output.lower()

        # Common error patterns
        if "package not found" in error_lower or "unable to locate package" in error_lower:
            suggestions.extend(
                [
                    "Observed category: requested package was not found",
                    "Relevant constraints: package spelling and configured repository inventory",
                ]
            )

        if "network" in error_lower or "connection" in error_lower:
            suggestions.extend(
                [
                    "Observed category: package transport network failure",
                    "Relevant constraints: container network and DNS availability",
                ]
            )

        if "permission denied" in error_lower or "access denied" in error_lower:
            suggestions.extend(
                [
                    "Observed category: package operation permission failure",
                    "Constraint: the provisioner requires package-manager privileges",
                ]
            )

        if "disk space" in error_lower or "no space left" in error_lower:
            suggestions.extend(
                [
                    "Observed category: insufficient disk space",
                    "Constraint: package installation requires additional writable capacity",
                ]
            )

        # Add general suggestions if no specific ones found
        if not suggestions:
            suggestions.extend(
                [
                    "Observed category: unclassified package installation failure",
                    "Relevant evidence: raw package-manager output and requested package identities",
                ]
            )

        return {"suggestions": suggestions, "docs": docs}

    def _get_parameters_schema(self) -> Dict[str, Any]:
        """Get the parameters schema for this tool."""
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "install",
                        "update",
                        "detect_missing",
                        "install_missing",
                        "install_java",
                        "install_maven",
                        "verify_java",
                    ],
                    "description": "The system operation to perform",
                },
                "packages": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of packages to install (for 'install' or 'install_missing' actions)",
                    "default": [],
                },
                "java_version": {
                    "type": "string",
                    "description": "Java version to install or verify (for 'install_java' or 'verify_java' actions)",
                    "default": None,
                },
                "maven_version": {
                    "type": "string",
                    "description": (
                        "Apache Maven version floor to install and activate, spelled "
                        "major[.minor[.patch]] (for the 'install_maven' action)"
                    ),
                    "default": None,
                },
            },
            "required": ["action"],
        }
