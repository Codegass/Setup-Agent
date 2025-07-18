"""Project setup tool for handling repository cloning and dependency installation."""

import os
import re
from typing import Dict, Any, Optional, List

from loguru import logger

from .base import BaseTool, ToolResult, ToolError


class ProjectSetupTool(BaseTool):
    """Tool for project setup tasks like cloning repositories and installing dependencies."""
    
    def __init__(self, orchestrator):
        super().__init__(
            name="project_setup",
            description="Handle project setup tasks including repository cloning, dependency detection, "
                       "and build environment preparation. Automatically detects project type and suggests next steps."
        )
        self.orchestrator = orchestrator
    
    def execute(
        self,
        action: str,
        repository_url: Optional[str] = None,
        target_directory: Optional[str] = None,
        branch: Optional[str] = None,
        auto_install_deps: bool = True,
        working_directory: str = "/workspace"
    ) -> ToolResult:
        """
        Execute project setup actions.
        
        Args:
            action: Action to perform ('clone', 'detect_project_type', 'install_dependencies', 'analyze_structure')
            repository_url: Git repository URL (required for 'clone')
            target_directory: Directory to clone into (optional, auto-generated if not provided)
            branch: Git branch to clone (optional, uses default branch if not provided)
            auto_install_deps: Whether to automatically install dependencies after cloning
            working_directory: Base directory for operations
        """
        
        valid_actions = ["clone", "detect_project_type", "install_dependencies", "analyze_structure"]
        
        if action not in valid_actions:
            raise ToolError(
                message=f"Invalid action '{action}'. Must be one of: {', '.join(valid_actions)}",
                suggestions=[
                    f"Use one of the valid actions: {', '.join(valid_actions)}",
                    "• clone: Clone a repository",
                    "• detect_project_type: Detect project type and build system",
                    "• install_dependencies: Install project dependencies",
                    "• analyze_structure: Analyze project structure and suggest setup steps"
                ],
                error_code="INVALID_ACTION"
            )
        
        try:
            if action == "clone":
                return self._clone_repository(repository_url, target_directory, branch, auto_install_deps, working_directory)
            elif action == "detect_project_type":
                return self._detect_project_type(working_directory)
            elif action == "install_dependencies":
                return self._install_dependencies(working_directory)
            elif action == "analyze_structure":
                return self._analyze_structure(working_directory)
                
        except Exception as e:
            raise ToolError(
                message=f"Project setup failed: {str(e)}",
                suggestions=[
                    "Check network connectivity for repository cloning",
                    "Verify repository URL is accessible",
                    "Ensure sufficient disk space in the container",
                    "Check if required tools are installed"
                ],
                error_code="PROJECT_SETUP_ERROR"
            )
    
    def _clone_repository(self, repository_url: str, target_directory: str, branch: str, auto_install_deps: bool, working_directory: str) -> ToolResult:
        """Clone a repository with comprehensive error handling."""
        
        if not repository_url:
            raise ToolError(
                message="repository_url is required for clone action",
                suggestions=[
                    "Provide a repository URL: project_setup(action='clone', repository_url='https://github.com/user/repo.git')",
                    "Ensure the URL is accessible and correct",
                    "Use HTTPS URLs for public repositories"
                ],
                error_code="MISSING_REPOSITORY_URL"
            )
        
        # Generate target directory if not provided
        if not target_directory:
            target_directory = repository_url.split('/')[-1].replace('.git', '')
        
        # Build git clone command
        clone_cmd = f"git clone"
        if branch:
            clone_cmd += f" -b {branch}"
        clone_cmd += f" {repository_url} {target_directory}"
        
        # Check if git is installed
        git_check = self.orchestrator.execute_command("which git", workdir=working_directory)
        if git_check["exit_code"] != 0:
            raise ToolError(
                message="Git is not installed in the container",
                suggestions=[
                    "Install Git first: bash(command='apt update && apt install -y git')",
                    "Verify Git installation: bash(command='git --version')",
                    "Check if the container has package management tools"
                ],
                documentation_links=[
                    "https://git-scm.com/book/en/v2/Getting-Started-Installing-Git"
                ],
                error_code="GIT_NOT_INSTALLED"
            )
        
        # Execute clone command
        logger.info(f"Cloning repository: {repository_url}")
        result = self.orchestrator.execute_command(clone_cmd, workdir=working_directory)
        
        if result["exit_code"] != 0:
            return self._handle_clone_error(result["output"], repository_url, target_directory, clone_cmd)
        
        # Verify clone was successful
        clone_path = os.path.join(working_directory, target_directory)
        verify_result = self.orchestrator.execute_command(f"ls -la {clone_path}", workdir=working_directory)
        
        if verify_result["exit_code"] != 0:
            raise ToolError(
                message="Repository clone verification failed",
                suggestions=[
                    "Check if the repository was cloned correctly",
                    "Verify disk space and permissions",
                    "Try cloning manually with bash tool"
                ],
                error_code="CLONE_VERIFICATION_FAILED"
            )
        
        output = f"✅ Repository cloned successfully!\n\n"
        output += f"📂 Repository: {repository_url}\n"
        output += f"📁 Directory: {clone_path}\n"
        if branch:
            output += f"🌿 Branch: {branch}\n"
        
        # Detect project type
        project_type = self._detect_project_type_in_directory(clone_path)
        output += f"🔍 Project Type: {project_type['type']}\n"
        
        if project_type['build_files']:
            output += f"📋 Build Files: {', '.join(project_type['build_files'])}\n"
        
        metadata = {
            "repository_url": repository_url,
            "target_directory": target_directory,
            "clone_path": clone_path,
            "project_type": project_type,
            "branch": branch
        }
        
        # Auto-install dependencies if requested
        if auto_install_deps and project_type['type'] != 'unknown':
            output += f"\n🔧 Installing dependencies automatically...\n"
            
            try:
                deps_result = self._install_dependencies_for_project_type(project_type, clone_path)
                if deps_result['success']:
                    output += f"✅ Dependencies installed successfully!\n"
                    output += f"📦 Installed: {deps_result['installed']}\n"
                    metadata['dependencies_installed'] = deps_result
                else:
                    output += f"⚠️ Dependency installation had issues:\n{deps_result['error']}\n"
                    output += f"💡 You can install manually using the appropriate tool\n"
                    metadata['dependencies_error'] = deps_result
            except Exception as e:
                output += f"⚠️ Auto-dependency installation failed: {str(e)}\n"
                output += f"💡 You can install manually using the appropriate tool\n"
        
        # Suggest next steps
        output += f"\n📝 Suggested next steps:\n"
        if project_type['type'] == 'maven':
            output += f"• Use maven tool: maven(command='clean compile')\n"
            output += f"• Run tests: maven(command='test')\n"
        elif project_type['type'] == 'gradle':
            output += f"• Use gradle tool: gradle(task='build')\n"
        elif project_type['type'] == 'npm':
            output += f"• Use npm tool: npm(command='install')\n"
            output += f"• Run build: npm(command='run build')\n"
        elif project_type['type'] == 'python':
            output += f"• Use uv tool: uv(command='sync')\n"
            output += f"• Run tests: uv(command='run pytest')\n"
        else:
            output += f"• Analyze project structure: project_setup(action='analyze_structure')\n"
            output += f"• Use bash tool for custom setup commands\n"
        
        return ToolResult(
            success=True,
            output=output,
            metadata=metadata
        )
    
    def _detect_project_type(self, working_directory: str) -> ToolResult:
        """Detect project type based on files in the directory."""
        
        project_type = self._detect_project_type_in_directory(working_directory)
        
        output = f"🔍 Project Type Detection Results\n\n"
        output += f"📋 Project Type: {project_type['type']}\n"
        
        if project_type['build_files']:
            output += f"📄 Build Files Found:\n"
            for file in project_type['build_files']:
                output += f"  • {file}\n"
        
        if project_type['language']:
            output += f"💻 Primary Language: {project_type['language']}\n"
        
        if project_type['dependencies']:
            output += f"📦 Dependencies: {', '.join(project_type['dependencies'])}\n"
        
        if project_type['suggested_tools']:
            output += f"\n🔧 Recommended Tools:\n"
            for tool in project_type['suggested_tools']:
                output += f"  • {tool}\n"
        
        return ToolResult(
            success=True,
            output=output,
            metadata=project_type
        )
    
    def _detect_project_type_in_directory(self, directory: str) -> Dict[str, Any]:
        """Detect project type in a specific directory."""
        
        # Check for common build files
        result = self.orchestrator.execute_command(f"find {directory} -maxdepth 2 -name 'pom.xml' -o -name 'build.gradle' -o -name 'package.json' -o -name 'pyproject.toml' -o -name 'requirements.txt' -o -name 'Cargo.toml' -o -name 'go.mod'", workdir=directory)
        
        if result["exit_code"] != 0:
            return {
                'type': 'unknown',
                'build_files': [],
                'language': None,
                'dependencies': [],
                'suggested_tools': ['bash']
            }
        
        build_files = [f.strip() for f in result["output"].split('\n') if f.strip()]
        
        # Determine project type
        project_type = 'unknown'
        language = None
        dependencies = []
        suggested_tools = []
        
        for file in build_files:
            if 'pom.xml' in file:
                project_type = 'maven'
                language = 'java'
                suggested_tools = ['maven', 'bash']
                break
            elif 'build.gradle' in file:
                project_type = 'gradle'
                language = 'java'
                suggested_tools = ['gradle', 'bash']
                break
            elif 'package.json' in file:
                project_type = 'npm'
                language = 'javascript'
                suggested_tools = ['npm', 'bash']
                break
            elif 'pyproject.toml' in file:
                project_type = 'python'
                language = 'python'
                suggested_tools = ['uv', 'bash']
                break
            elif 'requirements.txt' in file:
                project_type = 'python'
                language = 'python'
                suggested_tools = ['uv', 'bash']
                break
            elif 'Cargo.toml' in file:
                project_type = 'rust'
                language = 'rust'
                suggested_tools = ['cargo', 'bash']
                break
            elif 'go.mod' in file:
                project_type = 'go'
                language = 'go'
                suggested_tools = ['go', 'bash']
                break
        
        return {
            'type': project_type,
            'build_files': build_files,
            'language': language,
            'dependencies': dependencies,
            'suggested_tools': suggested_tools
        }
    
    def _install_dependencies_for_project_type(self, project_type: Dict[str, Any], directory: str) -> Dict[str, Any]:
        """Install dependencies based on project type."""
        
        if project_type['type'] == 'maven':
            # Install Java and Maven
            install_cmd = "apt update && apt install -y default-jdk maven"
            result = self.orchestrator.execute_command(install_cmd, workdir=directory)
            
            if result["exit_code"] == 0:
                return {
                    'success': True,
                    'installed': 'Java JDK and Maven',
                    'output': result["output"]
                }
            else:
                return {
                    'success': False,
                    'error': result["output"],
                    'exit_code': result["exit_code"]
                }
        
        # Add more project types as needed
        return {
            'success': False,
            'error': f"Auto-installation not implemented for project type: {project_type['type']}"
        }
    
    def _handle_clone_error(self, output: str, repository_url: str, target_directory: str, command: str) -> ToolResult:
        """Handle git clone errors with specific suggestions."""
        
        error_suggestions = []
        documentation_links = []
        error_code = "CLONE_ERROR"
        
        if "fatal: repository" in output and "not found" in output:
            error_code = "REPOSITORY_NOT_FOUND"
            error_suggestions.extend([
                "Verify the repository URL is correct",
                "Check if the repository exists and is accessible",
                "Ensure you have permission to access the repository",
                "Try the repository URL in a browser to verify it exists"
            ])
        
        elif "fatal: could not read" in output or "Connection refused" in output:
            error_code = "CONNECTION_ERROR"
            error_suggestions.extend([
                "Check network connectivity from the container",
                "Verify DNS resolution is working",
                "Try using HTTPS instead of SSH URLs",
                "Check if firewall is blocking the connection"
            ])
        
        elif "already exists" in output:
            error_code = "DIRECTORY_EXISTS"
            error_suggestions.extend([
                f"Directory '{target_directory}' already exists",
                "Use a different target directory name",
                "Remove the existing directory first: bash(command='rm -rf {target_directory}')",
                "Or use project_setup with a different target_directory parameter"
            ])
        
        elif "Permission denied" in output:
            error_code = "PERMISSION_ERROR"
            error_suggestions.extend([
                "Check if you have write permissions to the target directory",
                "Use sudo if necessary (though not recommended in containers)",
                "Check directory ownership and permissions"
            ])
        
        else:
            error_suggestions = [
                "Check the git clone output for specific error details",
                "Verify the repository URL format",
                "Try cloning manually with bash tool for more control",
                "Check if git is properly installed and configured"
            ]
        
        return ToolResult(
            success=False,
            output="",
            error=f"Failed to clone repository: {repository_url}",
            error_code=error_code,
            suggestions=error_suggestions,
            documentation_links=documentation_links,
            raw_output=output,
            metadata={
                "repository_url": repository_url,
                "target_directory": target_directory,
                "command": command
            }
        )
    
    def _install_dependencies(self, working_directory: str) -> ToolResult:
        """Install dependencies based on detected project type."""
        
        project_type = self._detect_project_type_in_directory(working_directory)
        
        if project_type['type'] == 'unknown':
            return ToolResult(
                success=False,
                output="",
                error="Cannot install dependencies: Unknown project type",
                suggestions=[
                    "Use detect_project_type action first",
                    "Install dependencies manually using appropriate tools",
                    "Check if the project has build files in the correct location"
                ],
                error_code="UNKNOWN_PROJECT_TYPE"
            )
        
        # Install dependencies based on project type
        try:
            result = self._install_dependencies_for_project_type(project_type, working_directory)
            
            if result['success']:
                return ToolResult(
                    success=True,
                    output=f"✅ Dependencies installed successfully for {project_type['type']} project!\n\nInstalled: {result['installed']}",
                    metadata=result
                )
            else:
                return ToolResult(
                    success=False,
                    output="",
                    error=result['error'],
                    suggestions=[
                        "Install dependencies manually using the appropriate tool",
                        "Check if the container has package management tools",
                        "Verify network connectivity for package downloads"
                    ],
                    error_code="DEPENDENCY_INSTALLATION_FAILED",
                    metadata=result
                )
        
        except Exception as e:
            raise ToolError(
                message=f"Dependency installation failed: {str(e)}",
                suggestions=[
                    "Try installing dependencies manually",
                    "Check container permissions and network access",
                    "Use the appropriate language-specific tool"
                ],
                error_code="DEPENDENCY_INSTALLATION_ERROR"
            )
    
    def _analyze_structure(self, working_directory: str) -> ToolResult:
        """Analyze project structure and suggest setup steps."""
        
        # Get directory structure
        structure_result = self.orchestrator.execute_command(f"find {working_directory} -type f -name '*.java' -o -name '*.py' -o -name '*.js' -o -name '*.ts' -o -name '*.go' -o -name '*.rs' | head -20", workdir=working_directory)
        
        # Get project type
        project_type = self._detect_project_type_in_directory(working_directory)
        
        output = f"📊 Project Structure Analysis\n\n"
        output += f"📁 Working Directory: {working_directory}\n"
        output += f"🔍 Project Type: {project_type['type']}\n\n"
        
        if structure_result["exit_code"] == 0 and structure_result["output"].strip():
            output += f"📄 Source Files Found:\n"
            for file in structure_result["output"].strip().split('\n'):
                if file.strip():
                    output += f"  • {file.strip()}\n"
        else:
            output += f"📄 No source files found in common locations\n"
        
        # Suggest setup steps
        output += f"\n📝 Suggested Setup Steps:\n"
        
        if project_type['type'] == 'maven':
            output += f"1. Install Java and Maven: project_setup(action='install_dependencies')\n"
            output += f"2. Compile project: maven(command='compile')\n"
            output += f"3. Run tests: maven(command='test')\n"
            output += f"4. Package application: maven(command='package')\n"
        elif project_type['type'] == 'gradle':
            output += f"1. Install Java and Gradle: project_setup(action='install_dependencies')\n"
            output += f"2. Build project: gradle(task='build')\n"
            output += f"3. Run tests: gradle(task='test')\n"
        elif project_type['type'] == 'npm':
            output += f"1. Install Node.js and npm: project_setup(action='install_dependencies')\n"
            output += f"2. Install packages: npm(command='install')\n"
            output += f"3. Run build: npm(command='run build')\n"
        elif project_type['type'] == 'python':
            output += f"1. Install Python and uv: project_setup(action='install_dependencies')\n"
            output += f"2. Install packages: uv(command='sync')\n"
            output += f"3. Run tests: uv(command='run pytest')\n"
        else:
            output += f"1. Examine build files manually\n"
            output += f"2. Use bash tool for custom setup commands\n"
            output += f"3. Check project documentation for setup instructions\n"
        
        return ToolResult(
            success=True,
            output=output,
            metadata={
                "project_type": project_type,
                "working_directory": working_directory,
                "structure_analysis": structure_result["output"]
            }
        )
    
    def get_usage_example(self) -> str:
        """Get usage examples for project setup tool."""
        return """
Project Setup Tool Usage Examples:

1. Clone a repository:
   project_setup(action="clone", repository_url="https://github.com/apache/commons-cli.git")

2. Clone with specific options:
   project_setup(action="clone", repository_url="https://github.com/user/repo.git", 
                 target_directory="my-project", branch="develop", auto_install_deps=True)

3. Detect project type:
   project_setup(action="detect_project_type")

4. Install dependencies:
   project_setup(action="install_dependencies")

5. Analyze project structure:
   project_setup(action="analyze_structure")

Common Workflow:
1. Clone repository
2. Detect project type (done automatically after clone)
3. Install dependencies (done automatically if auto_install_deps=True)
4. Use language-specific tools (maven, gradle, npm, etc.)
"""