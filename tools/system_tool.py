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

    def execute(self, action: str, packages: Optional[List[str]] = None, 
                container_name: Optional[str] = None) -> ToolResult:
        """Execute system management operations."""
        
        if action not in ["install", "update", "detect_missing", "install_missing"]:
            return ToolResult(
                success=False,
                output="",
                error=f"Invalid action '{action}'. Must be 'install', 'update', 'detect_missing', or 'install_missing'",
                error_code="INVALID_ACTION",
                suggestions=[
                    "Use 'install' to install specific packages",
                    "Use 'update' to update package lists",
                    "Use 'detect_missing' to check for missing dependencies",
                    "Use 'install_missing' to automatically install missing dependencies"
                ]
            )

        try:
            if action == "install":
                return self._install_packages(packages or [], container_name)
            elif action == "update":
                return self._update_packages(container_name)
            elif action == "detect_missing":
                return self._detect_missing_dependencies(container_name)
            elif action == "install_missing":
                return self._install_missing_dependencies(container_name)
                
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

    def _install_packages(self, packages: List[str], container_name: Optional[str] = None) -> ToolResult:
        """Install system packages using apt-get."""
        if not packages:
            return ToolResult(
                success=False,
                output="",
                error="No packages specified for installation",
                error_code="NO_PACKAGES"
            )

        # Update package lists first
        update_result = self._update_packages(container_name)
        if not update_result.success:
            logger.warning(f"Failed to update package lists: {update_result.error}")

        # Install packages
        packages_str = " ".join(packages)
        command = f"apt-get install -y {packages_str}"
        
        logger.info(f"Installing packages: {packages_str}")
        
        result = self.docker_orchestrator.execute_command(
            command=command,
            container_name=container_name,
            timeout=300  # 5 minutes timeout for installations
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

    def _update_packages(self, container_name: Optional[str] = None) -> ToolResult:
        """Update package lists."""
        command = "apt-get update"
        
        logger.info("Updating package lists")
        
        result = self.docker_orchestrator.execute_command(
            command=command,
            container_name=container_name,
            timeout=120
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

    def _detect_missing_dependencies(self, container_name: Optional[str] = None) -> ToolResult:
        """Detect missing dependencies by checking common tools."""
        missing_tools = []
        
        # Common development tools to check
        tools_to_check = {
            "maven": ["mvn", "maven"],
            "gradle": ["gradle"],
            "node": ["nodejs", "npm"],
            "python": ["python3", "pip3"],
            "git": ["git"],
            "curl": ["curl"],
            "wget": ["wget"],
            "unzip": ["unzip"],
            "zip": ["zip"]
        }
        
        for tool, packages in tools_to_check.items():
            # Check if tool is available
            check_result = self.docker_orchestrator.execute_command(
                command=f"which {tool}",
                container_name=container_name,
                timeout=10
            )
            
            if check_result["exit_code"] != 0:
                missing_tools.append({
                    "tool": tool,
                    "packages": packages,
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

    def _install_missing_dependencies(self, container_name: Optional[str] = None) -> ToolResult:
        """Automatically install missing dependencies."""
        
        # First detect what's missing
        detect_result = self._detect_missing_dependencies(container_name)
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
        install_result = self._install_packages(packages_to_install, container_name)
        
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
                },
                "container_name": {
                    "type": "string",
                    "description": "Name of the container to operate on (optional)",
                    "default": None
                }
            },
            "required": ["action"]
        }