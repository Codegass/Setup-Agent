"""System management tool for package installation and system operations."""

import re
from typing import Any, Dict, List, Optional

from loguru import logger

from .base import BaseTool, ToolResult


class SystemTool(BaseTool):
    """Tool for system management operations like package installation."""

    def __init__(self, docker_orchestrator):
        super().__init__(
            name="system",
            description="Install system packages, manage dependencies, and perform system operations. "
            "Automatically detects missing dependencies and installs them.",
        )
        self.docker_orchestrator = docker_orchestrator

    def execute(self, action: str, packages: Optional[List[str]] = None, java_version: Optional[str] = None) -> ToolResult:
        """Execute system management operations."""
        
        if action not in ["install", "update", "detect_missing", "install_missing", "install_java"]:
            return ToolResult(
                success=False,
                output="",
                error=f"Invalid action '{action}'. Must be 'install', 'update', 'detect_missing', 'install_missing', or 'install_java'",
                error_code="INVALID_ACTION",
                suggestions=[
                    "Use 'install' to install specific packages",
                    "Use 'update' to update package lists",
                    "Use 'detect_missing' to check for missing dependencies",
                    "Use 'install_missing' to automatically install missing dependencies",
                    "Use 'install_java' to install and configure a specific Java version"
                ]
            )

        try:
            if action == "install":
                if not packages:
                    return ToolResult(
                        success=False,
                        output="",
                        error="Packages list is required for 'install' action",
                        error_code="MISSING_PACKAGES",
                        suggestions=["Provide a list of packages to install"]
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
                    return ToolResult(
                        success=False,
                        output="",
                        error="Java version is required for 'install_java' action",
                        error_code="MISSING_VERSION",
                        suggestions=[
                            "Provide Java version to install (e.g., '17', '21')",
                            "Example: system(action='install_java', java_version='17')"
                        ]
                    )
                return self._install_and_configure_java(java_version)
                
        except Exception as e:
            error_msg = f"System operation failed: {str(e)}"
            logger.error(f"System tool error for action '{action}': {error_msg}")
            return ToolResult(
                success=False,
                output="",
                error=error_msg,
                error_code="SYSTEM_ERROR",
                suggestions=[
                    "Check if Docker container is running",
                    "Verify network connectivity",
                    "Try updating package lists first"
                ]
            )

    def _install_packages(self, packages: List[str]) -> ToolResult:
        """Install system packages using apt-get."""
        if not packages:
            return ToolResult(
                success=True,
                output="No packages to install",
                metadata={"packages": []}
            )

        # Update package lists first
        update_result = self._update_packages()
        if not update_result.success:
            logger.warning(f"Failed to update package lists: {update_result.error}")

        # Install packages
        packages_str = " ".join(packages)
        command = f"apt-get install -y {packages_str}"
        
        logger.info(f"Installing packages: {packages_str}")
        
        result = self.docker_orchestrator.execute_command(
            command=command
        )

        if result["exit_code"] == 0:
            return ToolResult(
                success=True,
                output=f"Successfully installed packages: {packages_str}\n\n{result['output']}",
                metadata={
                    "packages": packages,
                    "exit_code": result["exit_code"],
                    "command": command
                }
            )
        else:
            # Try to provide helpful error analysis
            error_analysis = self._analyze_install_error(result["output"])
            
            return ToolResult(
                success=False,
                output=result["output"],
                error=f"Failed to install packages: {packages_str}",
                error_code="INSTALL_FAILED",
                suggestions=error_analysis.get("suggestions", []),
                documentation_links=error_analysis.get("docs", []),
                metadata={
                    "packages": packages,
                    "exit_code": result["exit_code"],
                    "command": command
                }
            )

    def _update_packages(self) -> ToolResult:
        """Update package lists."""
        command = "apt-get update"
        
        logger.info("Updating package lists")
        
        result = self.docker_orchestrator.execute_command(
            command=command
        )

        if result["exit_code"] == 0:
            return ToolResult(
                success=True,
                output=f"Successfully updated package lists\n\n{result['output']}",
                metadata={"exit_code": result["exit_code"], "command": command}
            )
        else:
            return ToolResult(
                success=False,
                output=result["output"],
                error="Failed to update package lists",
                error_code="UPDATE_FAILED",
                suggestions=[
                    "Check network connectivity",
                    "Try running the command again",
                    "Verify repository URLs are accessible"
                ],
                metadata={"exit_code": result["exit_code"], "command": command}
            )

    def _detect_missing_tools(self) -> ToolResult:
        """Detect missing development tools and dependencies."""
        
        # Common development tools to check
        required_tools = [
            "git", "curl", "wget", "python3", "pip", "node", "npm",
            "java", "javac", "mvn", "make", "gcc", "g++"
        ]
        
        missing_tools = []
        
        for tool in required_tools:
            check_result = self.docker_orchestrator.execute_command(
                command=f"which {tool}"
            )
            
            if check_result["exit_code"] != 0:
                missing_tools.append({
                    "tool": tool,
                    "packages": [tool], # Assuming a package name is the tool itself
                    "check_command": f"which {tool}"
                })

        if missing_tools:
            output = "Missing development tools detected:\n\n"
            for tool_info in missing_tools:
                output += f"• {tool_info['tool']}: Install with 'apt-get install {' '.join(tool_info['packages'])}'\n"
            
            return ToolResult(
                success=True,
                output=output,
                metadata={
                    "missing_tools": missing_tools,
                    "total_missing": len(missing_tools)
                }
            )
        else:
            return ToolResult(
                success=True,
                output="All common development tools are available",
                metadata={"missing_tools": [], "total_missing": 0}
            )

    def _install_missing_dependencies(self) -> ToolResult:
        """Detect and install missing dependencies automatically."""
        
        # First detect what's missing
        detect_result = self._detect_missing_tools()
        if not detect_result.success:
            return detect_result
        
        missing_tools = detect_result.metadata.get("missing_tools", [])
        
        if not missing_tools:
            return ToolResult(
                success=True,
                output="No missing dependencies detected",
                metadata={"installed_packages": []}
            )

        # Collect all packages to install
        packages_to_install = []
        for tool_info in missing_tools:
            packages_to_install.extend(tool_info["packages"])
        
        # Remove duplicates
        packages_to_install = list(set(packages_to_install))
        
        # Install all missing packages
        install_result = self._install_packages(packages_to_install)
        
        if install_result.success:
            return ToolResult(
                success=True,
                output=f"Successfully installed missing dependencies: {', '.join(packages_to_install)}\n\n{install_result.output}",
                metadata={
                    "missing_tools": missing_tools,
                    "installed_packages": packages_to_install,
                    "total_installed": len(packages_to_install)
                }
            )
        else:
            return ToolResult(
                success=False,
                output=install_result.output,
                error=f"Failed to install some dependencies: {install_result.error}",
                error_code="INSTALL_MISSING_FAILED",
                suggestions=install_result.suggestions,
                documentation_links=install_result.documentation_links,
                metadata={
                    "missing_tools": missing_tools,
                    "failed_packages": packages_to_install
                }
            )

    def _install_and_configure_java(self, java_version: str) -> ToolResult:
        """Install and configure a specific Java version."""
        logger.info(f"Installing and configuring Java {java_version}")
        
        # Step 1: Check current Java version
        current_java = self.docker_orchestrator.execute_command("java -version 2>&1 | head -1")
        logger.info(f"Current Java: {current_java.get('output', 'Not installed')}")
        
        # Step 2: Update package lists
        update_result = self._update_packages()
        if not update_result.success:
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
                f"java-{java_version}-openjdk-devel"
            ]
            
            for alt_package in alt_packages:
                logger.info(f"Trying alternative package: {alt_package}")
                alt_result = self.docker_orchestrator.execute_command(f"apt-get install -y {alt_package}")
                if alt_result["exit_code"] == 0:
                    install_result = alt_result
                    java_package = alt_package
                    break
            else:
                return ToolResult(
                    success=False,
                    output=install_result["output"],
                    error=f"Failed to install Java {java_version}",
                    error_code="JAVA_INSTALL_FAILED",
                    suggestions=[
                        f"Java {java_version} may not be available in the package repository",
                        "Try a different version (e.g., 11, 17, 21)",
                        "Check available versions: apt-cache search openjdk | grep jdk"
                    ]
                )
        
        # Step 4: Get architecture for Java home path
        arch_result = self.docker_orchestrator.execute_command("dpkg --print-architecture")
        arch = arch_result.get("output", "amd64").strip()
        
        # Step 5: Set JAVA_HOME and update alternatives
        java_home = f"/usr/lib/jvm/java-{java_version}-openjdk-{arch}"
        java_bin = f"{java_home}/bin/java"
        javac_bin = f"{java_home}/bin/javac"
        
        # Check if the Java installation exists
        check_java = self.docker_orchestrator.execute_command(f"test -f {java_bin} && echo 'exists'")
        if "exists" not in check_java.get("output", ""):
            # Try to find the actual Java installation
            find_java = self.docker_orchestrator.execute_command(
                f"find /usr/lib/jvm -name 'java-{java_version}-openjdk*' -type d | head -1"
            )
            if find_java.get("output"):
                java_home = find_java["output"].strip()
                java_bin = f"{java_home}/bin/java"
                javac_bin = f"{java_home}/bin/javac"
        
        # Step 6: Configure environment
        config_commands = [
            # Set JAVA_HOME in profile
            f"echo 'export JAVA_HOME={java_home}' >> /etc/profile",
            f"echo 'export PATH=$JAVA_HOME/bin:$PATH' >> /etc/profile",
            # Set JAVA_HOME in bashrc
            f"echo 'export JAVA_HOME={java_home}' >> /root/.bashrc",
            f"echo 'export PATH=$JAVA_HOME/bin:$PATH' >> /root/.bashrc",
            # Update alternatives
            f"update-alternatives --install /usr/bin/java java {java_bin} 100",
            f"update-alternatives --install /usr/bin/javac javac {javac_bin} 100",
            f"update-alternatives --set java {java_bin}",
            f"update-alternatives --set javac {javac_bin}"
        ]
        
        for cmd in config_commands:
            result = self.docker_orchestrator.execute_command(cmd)
            if result["exit_code"] != 0:
                logger.warning(f"Failed to execute: {cmd}")
        
        # Step 7: Verify installation
        verify_result = self.docker_orchestrator.execute_command(
            f"export JAVA_HOME={java_home} && java -version 2>&1 && echo '---' && javac -version 2>&1"
        )
        
        if verify_result["exit_code"] == 0:
            return ToolResult(
                success=True,
                output=f"Successfully installed and configured Java {java_version}\n\n"
                      f"JAVA_HOME: {java_home}\n"
                      f"Verification:\n{verify_result['output']}",
                metadata={
                    "java_version": java_version,
                    "java_home": java_home,
                    "package": java_package,
                    "architecture": arch
                }
            )
        else:
            return ToolResult(
                success=False,
                output=verify_result["output"],
                error=f"Java {java_version} installed but verification failed",
                error_code="JAVA_CONFIG_FAILED",
                suggestions=[
                    "Java was installed but configuration may be incomplete",
                    f"Try manually setting: export JAVA_HOME={java_home}",
                    "Restart the shell or container to apply changes"
                ]
            )
    
    def _smart_install_commands(self, commands: List[str]) -> ToolResult:
        """
        Intelligently install packages that provide the specified commands.
        This solves issues like trying to install 'javac' when 'default-jdk' is needed.
        """
        logger.info(f"Smart installing commands: {commands}")
        
        # Common command-to-package mappings
        command_mappings = {
            'javac': ['default-jdk', 'openjdk-11-jdk', 'openjdk-8-jdk'],
            'java': ['default-jre', 'openjdk-11-jre', 'openjdk-8-jre'],
            'mvn': ['maven'],
            'node': ['nodejs'],
            'npm': ['npm'],
            'python3': ['python3'],
            'pip3': ['python3-pip'],
            'git': ['git'],
            'curl': ['curl'],
            'wget': ['wget'],
            'gcc': ['build-essential', 'gcc'],
            'g++': ['build-essential', 'g++'],
            'make': ['build-essential', 'make'],
            'cmake': ['cmake'],
            'docker': ['docker.io', 'docker-ce'],
            'zip': ['zip'],
            'unzip': ['unzip'],
            'vim': ['vim'],
            'nano': ['nano'],
            'grep': ['grep'],
            'find': ['findutils'],
            'which': ['debianutils'],
            'awk': ['gawk'],
            'sed': ['sed'],
        }
        
        packages_to_install = []
        search_results = []
        
        for command in commands:
            command = command.strip()
            
            # First, check if command already exists
            check_result = self.docker_orchestrator.execute_command(f"which {command}")
            if check_result["exit_code"] == 0:
                search_results.append(f"✅ {command}: already available at {check_result['output'].strip()}")
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
            return ToolResult(
                success=True,
                output="All requested commands are already available.\n" + "\n".join(search_results),
                metadata={"commands": commands, "packages_installed": [], "search_results": search_results}
            )
        
        # Install the discovered packages
        logger.info(f"Installing packages for commands: {unique_packages}")
        install_result = self._install_packages(unique_packages)
        
        # Enhance the output with search information
        if install_result.success:
            enhanced_output = f"Smart installation completed!\n\n"
            enhanced_output += f"Command analysis:\n" + "\n".join(search_results) + "\n\n"
            enhanced_output += f"Installed packages: {', '.join(unique_packages)}\n\n"
            enhanced_output += install_result.output
            
            return ToolResult(
                success=True,
                output=enhanced_output,
                metadata={
                    "commands": commands,
                    "packages_installed": unique_packages,
                    "search_results": search_results,
                    "install_output": install_result.output
                }
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
            for line in apt_file_result["output"].split('\n'):
                if f'bin/{command}' in line and ':' in line:
                    package = line.split(':')[0].strip()
                    if package and package not in packages:
                        packages.append(package)
        
        # Strategy 2: Try dpkg search if apt-file failed
        if not packages:
            dpkg_result = self.docker_orchestrator.execute_command(f"dpkg -S $(which {command} 2>/dev/null) 2>/dev/null")
            if dpkg_result["exit_code"] == 0 and dpkg_result["output"]:
                for line in dpkg_result["output"].split('\n'):
                    if ':' in line:
                        package = line.split(':')[0].strip()
                        if package and package not in packages:
                            packages.append(package)
        
        # Strategy 3: Try command-not-found if available
        if not packages:
            cnf_result = self.docker_orchestrator.execute_command(f"command-not-found {command} 2>&1 | grep 'apt install' || true")
            if cnf_result["exit_code"] == 0 and cnf_result["output"]:
                # Extract package names from "apt install package1 package2" suggestions
                import re
                matches = re.findall(r'apt install\s+([^\n]+)', cnf_result["output"])
                for match in matches:
                    suggested_packages = match.strip().split()
                    packages.extend(suggested_packages)
        
        # Return up to 3 most relevant packages to avoid installing too much
        return packages[:3] if packages else []

    def _analyze_install_error(self, error_output: str) -> Dict[str, Any]:
        """Analyze installation error output and provide suggestions."""
        suggestions = []
        docs = []
        
        error_lower = error_output.lower()
        
        # Common error patterns
        if "package not found" in error_lower or "unable to locate package" in error_lower:
            suggestions.extend([
                "Update package lists with 'apt-get update'",
                "Check if package name is spelled correctly",
                "Verify the package exists in current repositories"
            ])
        
        if "network" in error_lower or "connection" in error_lower:
            suggestions.extend([
                "Check network connectivity",
                "Try again after a few minutes",
                "Verify DNS resolution is working"
            ])
        
        if "permission denied" in error_lower or "access denied" in error_lower:
            suggestions.extend([
                "Ensure running with appropriate permissions",
                "Check if container has required privileges"
            ])
        
        if "disk space" in error_lower or "no space left" in error_lower:
            suggestions.extend([
                "Free up disk space",
                "Clean package cache with 'apt-get clean'",
                "Remove unused packages with 'apt-get autoremove'"
            ])
        
        # Add general suggestions if no specific ones found
        if not suggestions:
            suggestions.extend([
                "Try updating package lists first",
                "Check container logs for more details",
                "Verify the package name and version"
            ])
        
        return {
            "suggestions": suggestions,
            "docs": docs
        }

    def _get_parameters_schema(self) -> Dict[str, Any]:
        """Get the parameters schema for this tool."""
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["install", "update", "detect_missing", "install_missing"],
                    "description": "The system operation to perform"
                },
                "packages": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of packages to install (required for 'install' action)",
                    "default": []
                }
            },
            "required": ["action"]
        }