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
                       "and build environment preparation. Automatically detects project type, Java version requirements "
                       "from Maven/Gradle configurations, and installs the correct Java version. Supports Maven Enforcer "
                       "plugin detection and parent POM analysis for accurate Java version setup."
        )
        self.orchestrator = orchestrator
    
    def _extract_key_info(self, output: str, tool_name: str) -> str:
        """Override to use project-setup-specific extraction."""
        if tool_name == "project_setup" or tool_name == self.name:
            return self._extract_project_setup_key_info(output)
        return output

    def _extract_project_setup_key_info(self, output: str) -> str:
        """Extract key information from project setup output."""
        if not output or len(output) <= self.max_output_length:
            return output
        
        lines = output.split('\n')
        
        # Check if this is git clone output
        if self._is_git_clone_output(lines):
            return self._extract_git_clone_info(lines, output)
        
        # Check if this is project analysis output
        elif self._is_project_analysis_output(lines):
            return self._extract_project_analysis_info(lines, output)
        
        # For other cases, use general truncation
        return output

    def _is_git_clone_output(self, lines: list) -> bool:
        """Check if output looks like git clone output."""
        git_patterns = [
            'cloning into',
            'remote: counting objects',
            'remote: compressing objects',
            'receiving objects',
            'resolving deltas',
            'checking out files'
        ]
        
        content_lower = '\n'.join(lines).lower()
        return any(pattern in content_lower for pattern in git_patterns)

    def _is_project_analysis_output(self, lines: list) -> bool:
        """Check if output looks like project analysis output."""
        analysis_patterns = [
            'pom.xml',
            'build.gradle',
            'package.json',
            'requirements.txt',
            'detected project type',
            'dependencies found'
        ]
        
        content_lower = '\n'.join(lines).lower()
        return any(pattern in content_lower for pattern in analysis_patterns)

    def _extract_git_clone_info(self, lines: list, original_output: str) -> str:
        """Extract key info from git clone output."""
        summary = []
        
        # Look for key git clone indicators
        clone_target = ""
        progress_info = []
        completion_info = []
        
        for line in lines:
            line_lower = line.lower()
            
            if 'cloning into' in line_lower:
                clone_target = line.strip()
                
            elif any(progress in line_lower for progress in ['receiving objects', 'resolving deltas', 'counting objects']):
                if len(progress_info) < 5:  # Limit progress lines
                    progress_info.append(line.strip())
                    
            elif any(completion in line_lower for completion in ['done', 'completed', 'checking out']):
                completion_info.append(line.strip())
        
        summary.append("🔄 Git Clone Summary:")
        
        if clone_target:
            summary.append(f"📂 {clone_target}")
        
        if progress_info:
            summary.append(f"\n📊 Progress indicators:")
            summary.extend(progress_info)
        
        if completion_info:
            summary.append(f"\n✅ Completion status:")
            summary.extend(completion_info[:3])  # Show first 3 completion messages
        
        # Show first and last lines for full context
        if len(lines) > 20:
            summary.append(f"\nFirst 10 lines:")
            summary.extend(lines[:10])
            summary.append(f"\n... [middle output truncated] ...")
            summary.append(f"\nLast 10 lines:")
            summary.extend(lines[-10:])
        else:
            summary.append(f"\nFull output:")
            summary.extend(lines)
        
        summary.append(f"\n💡 Use 'file_io' to list directory contents or 'bash' to verify clone status.")
        
        return '\n'.join(summary)

    def _extract_project_analysis_info(self, lines: list, original_output: str) -> str:
        """Extract key info from project analysis output."""
        summary = []
        
        # Look for project type indicators
        project_types = []
        config_files = []
        dependencies = []
        structure_info = []
        
        for line in lines:
            line_lower = line.lower().strip()
            
            # Project type detection
            if 'maven' in line_lower or 'pom.xml' in line_lower:
                project_types.append("Maven")
                if 'pom.xml' in line_lower:
                    config_files.append(line.strip())
                    
            elif 'gradle' in line_lower or 'build.gradle' in line_lower:
                project_types.append("Gradle")
                if 'build.gradle' in line_lower:
                    config_files.append(line.strip())
                    
            elif 'npm' in line_lower or 'package.json' in line_lower:
                project_types.append("Node.js")
                if 'package.json' in line_lower:
                    config_files.append(line.strip())
                    
            elif 'python' in line_lower or 'requirements.txt' in line_lower:
                project_types.append("Python")
                if 'requirements.txt' in line_lower:
                    config_files.append(line.strip())
            
            # Dependencies
            elif any(dep_indicator in line_lower for dep_indicator in ['dependency', 'dependencies', 'import', 'require']):
                if len(dependencies) < 5:
                    dependencies.append(line.strip())
            
            # Structure info
            elif any(structure_word in line_lower for structure_word in ['src/', 'lib/', 'target/', 'build/', 'node_modules/']):
                if len(structure_info) < 10:
                    structure_info.append(line.strip())
        
        summary.append("🔍 Project Analysis Summary:")
        
        if project_types:
            unique_types = list(set(project_types))
            summary.append(f"📋 Project Types: {', '.join(unique_types)}")
        
        if config_files:
            summary.append(f"⚙️ Configuration Files:")
            summary.extend(config_files[:5])  # Show first 5 config files
            
        if dependencies:
            summary.append(f"\n📦 Dependencies Found:")
            summary.extend(dependencies[:5])  # Show first 5 dependencies
            
        if structure_info:
            summary.append(f"\n📁 Project Structure:")
            summary.extend(structure_info[:8])  # Show first 8 structure items
        
        # Show sample of full output
        if len(lines) > 30:
            summary.append(f"\nAnalysis Output (first 15 lines):")
            summary.extend(lines[:15])
            summary.append(f"\n... [analysis truncated, {len(lines)} total lines] ...")
            summary.append(f"\nAnalysis Output (last 10 lines):")
            summary.extend(lines[-10:])
        else:
            summary.append(f"\nFull Analysis Output:")
            summary.extend(lines)
        
        summary.append(f"\n💡 Use 'file_io' to examine specific config files or 'bash' to explore project structure.")
        
        return '\n'.join(summary)
    
    def execute(
        self,
        action: str,
        repository_url: Optional[str] = None,
        target_directory: Optional[str] = None,
        branch: Optional[str] = None,
        auto_install_deps: bool = True,
        working_directory: str = "/workspace",
        **kwargs
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
        
        # Detect Java version requirement for Java projects
        java_version_required = None
        if project_type['type'] in ['maven', 'gradle']:
            java_version_required = self._detect_java_version_requirement(clone_path, project_type)
            if java_version_required:
                output += f"☕ Java Version Required: {java_version_required}\n"
        
        metadata = {
            "repository_url": repository_url,
            "target_directory": target_directory,
            "clone_path": clone_path,
            "project_type": project_type,
            "java_version_required": java_version_required,
            "branch": branch
        }
        
        # Auto-install dependencies if requested
        if auto_install_deps and project_type['type'] != 'unknown':
            output += f"\n🔧 Installing dependencies automatically...\n"
            
            try:
                deps_result = self._install_dependencies_for_project_type(project_type, clone_path, java_version_required)
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
    
    def _detect_java_version_requirement(self, project_path: str, project_type: Dict[str, Any]) -> Optional[str]:
        """
        Detect the required Java version for Maven/Gradle projects.
        
        Returns:
            str: Java version (e.g., "17", "11", "8") or None if not detected
        """
        if project_type.get('type') not in ['maven', 'gradle'] or not self.orchestrator:
            return None
        
        logger.info(f"Detecting Java version requirement for {project_type.get('type')} project at {project_path}")
        
        if project_type.get('type') == 'maven':
            return self._detect_maven_java_version(project_path)
        elif project_type.get('type') == 'gradle':
            return self._detect_gradle_java_version(project_path)
        
        return None
    
    def _detect_maven_java_version(self, project_path: str) -> Optional[str]:
        """Detect Java version from Maven pom.xml files."""
        # Read main pom.xml
        main_pom_result = self.orchestrator.execute_command(f"cat {project_path}/pom.xml")
        if main_pom_result["exit_code"] != 0 or not main_pom_result.get("output"):
            logger.warning(f"Could not read main pom.xml at {project_path}")
            return None
        
        main_pom_content = main_pom_result["output"]
        all_pom_contents = [main_pom_content]
        pom_locations = [f"{project_path}/pom.xml"]
        
        # Check for parent POM (multi-module projects)
        parent_pattern = r"<parent>.*?<artifactId>([^<]+)</artifactId>.*?</parent>"
        parent_match = re.search(parent_pattern, main_pom_content, re.DOTALL)
        if parent_match:
            parent_artifact_id = parent_match.group(1)
            # Look for parent POM in common locations
            possible_parent_paths = [
                f"{project_path}/{parent_artifact_id}/pom.xml",
                f"{project_path}/../{parent_artifact_id}/pom.xml"
            ]
            
            for parent_path in possible_parent_paths:
                # Extract properties section from parent POM to avoid large content
                props_result = self.orchestrator.execute_command(
                    f"sed -n '/<properties>/,/<\\/properties>/p' {parent_path} 2>/dev/null || echo ''"
                )
                if props_result.get("success") and props_result.get("output"):
                    minimal_parent = f"<project>{props_result.get('output', '')}</project>"
                    all_pom_contents.append(minimal_parent)
                    pom_locations.append(parent_path)
                    logger.info(f"Found parent POM at: {parent_path}")
                    break
        
        # Analyze all POM contents for Java version
        java_version = None
        java_version_source = None
        java_version_enforced = False
        
        for idx, pom_content in enumerate(all_pom_contents):
            if java_version:
                break  # Already found
                
            # 1. First check Maven Enforcer plugin for RequireJavaVersion (highest priority)
            enforcer_pattern = r"<requireJavaVersion>.*?<version>\[?(\d+),?\)?</version>.*?</requireJavaVersion>"
            enforcer_match = re.search(enforcer_pattern, pom_content, re.DOTALL | re.IGNORECASE)
            if enforcer_match:
                java_version = enforcer_match.group(1).strip()
                java_version_source = "maven-enforcer"
                java_version_enforced = True
                logger.info(f"Found Java version from Maven Enforcer in {pom_locations[idx]}: {java_version}")
                break
            
            # 2. Check standard properties
            java_version_patterns = [
                r"<maven\.compiler\.release>([^<]+)</maven\.compiler\.release>",  # Highest priority
                r"<maven\.compiler\.target>([^<]+)</maven\.compiler\.target>",
                r"<maven\.compiler\.source>([^<]+)</maven\.compiler\.source>",
                r"<java\.version>([^<]+)</java\.version>"
            ]
            
            for pattern in java_version_patterns:
                match = re.search(pattern, pom_content)
                if match:
                    java_version = match.group(1).strip()
                    java_version_source = "maven-compiler"
                    logger.info(f"Found Java version from {pattern} in {pom_locations[idx]}: {java_version}")
                    break
        
        if java_version:
            # Normalize version (e.g., "1.8" -> "8")
            if java_version.startswith("1."):
                java_version = java_version[2:]
            logger.info(f"Detected Java version: {java_version} (source: {java_version_source}, enforced: {java_version_enforced})")
        else:
            logger.warning(f"No Java version found in Maven configuration for {project_path}")
        
        return java_version
    
    def _detect_gradle_java_version(self, project_path: str) -> Optional[str]:
        """Detect Java version from Gradle build files."""
        # Try build.gradle first, then build.gradle.kts
        gradle_files = ["build.gradle", "build.gradle.kts"]
        gradle_content = None
        
        for gradle_file in gradle_files:
            result = self.orchestrator.execute_command(f"cat {project_path}/{gradle_file}")
            if result.get("success") and result.get("output"):
                gradle_content = result.get("output", "")
                logger.info(f"Reading Gradle configuration from {gradle_file}")
                break
        
        if not gradle_content:
            logger.warning(f"Could not read Gradle build files at {project_path}")
            return None
        
        # Extract Java version from Gradle configuration
        java_version_patterns = [
            # Java toolchain configuration (highest priority)
            r"java\s*\{\s*toolchain\s*\{\s*languageVersion\s*=\s*JavaLanguageVersion\.of\((\d+)\)",
            r"languageVersion\.set\(JavaLanguageVersion\.of\((\d+)\)\)",
            r"java\.toolchain\.languageVersion\s*=\s*JavaLanguageVersion\.of\((\d+)\)",
            
            # Source/Target compatibility
            r"sourceCompatibility\s*=\s*['\"]?(\d+(?:\.\d+)?)['\"]?",
            r"targetCompatibility\s*=\s*['\"]?(\d+(?:\.\d+)?)['\"]?",
            r"sourceCompatibility\s*=\s*JavaVersion\.VERSION_(\d+)",
            r"targetCompatibility\s*=\s*JavaVersion\.VERSION_(\d+)",
            
            # Kotlin DSL style
            r"java\.sourceCompatibility\s*=\s*JavaVersion\.VERSION_(\d+)",
            r"java\.targetCompatibility\s*=\s*JavaVersion\.VERSION_(\d+)",
        ]
        
        for pattern in java_version_patterns:
            match = re.search(pattern, gradle_content, re.IGNORECASE | re.MULTILINE)
            if match:
                version = match.group(1).strip()
                # Normalize version format (1.8 -> 8)
                if version.startswith("1."):
                    version = version[2:]
                logger.info(f"Found Java version from Gradle: {version}")
                return version
        
        logger.warning(f"No Java version found in Gradle configuration for {project_path}")
        return None
    
    def _install_dependencies_for_project_type(self, project_type: Dict[str, Any], directory: str, java_version: Optional[str] = None) -> Dict[str, Any]:
        """Install dependencies based on project type and detected Java version."""
        
        if project_type['type'] == 'maven':
            # Determine which Java package to install
            if java_version and java_version.isdigit():
                java_package = f"openjdk-{java_version}-jdk"
                logger.info(f"Installing dependencies for Maven project with Java {java_version}: {java_package}, maven")
            else:
                java_package = "default-jdk"
                logger.info("Installing dependencies for Maven project: default-jdk, maven")
            
            # 1. Update package lists
            logger.info("Updating package lists with apt-get update...")
            update_result = self.orchestrator.execute_command("apt-get update")
            if not update_result["success"]:
                logger.warning(f"apt-get update failed, but proceeding with install: {update_result['output']}")

            # 2. Install packages
            install_cmd = f"DEBIAN_FRONTEND=noninteractive apt-get install -y {java_package} maven"
            result = self.orchestrator.execute_command(install_cmd, workdir=directory)
            
            if result["success"]:
                # Setup Java environment after installation
                java_home = self._setup_java_environment(java_version)
                
                installed_description = f"Java JDK {java_version or 'default'} and Maven with environment setup"
                return {
                    'success': True,
                    'installed': installed_description,
                    'java_version': java_version,
                    'java_home': java_home,
                    'output': result["output"]
                }
            else:
                # Try alternative packages if specific version fails
                if java_version and java_package != "default-jdk":
                    logger.warning(f"Failed to install {java_package}, trying alternative packages")
                    alt_packages = [
                        f"openjdk-{java_version}-jdk-headless",
                        f"java-{java_version}-openjdk",
                        "default-jdk"  # Final fallback
                    ]
                    
                    for alt_package in alt_packages:
                        logger.info(f"Trying alternative package: {alt_package}")
                        alt_cmd = f"DEBIAN_FRONTEND=noninteractive apt-get install -y {alt_package} maven"
                        alt_result = self.orchestrator.execute_command(alt_cmd, workdir=directory)
                        if alt_result["success"]:
                            java_home = self._setup_java_environment(java_version if alt_package != "default-jdk" else None)
                            return {
                                'success': True,
                                'installed': f"{alt_package} and Maven with environment setup",
                                'java_version': java_version if alt_package != "default-jdk" else None,
                                'java_home': java_home,
                                'output': alt_result["output"]
                            }
                
                return {
                    'success': False,
                    'error': result["output"],
                    'exit_code': result["exit_code"],
                    'java_version_requested': java_version
                }
        
        # Add more project types as needed
        return {
            'success': False,
            'error': f"Auto-installation not implemented for project type: {project_type['type']}"
        }

    def _setup_java_environment(self, java_version: Optional[str] = None):
        """Setup JAVA_HOME environment variable and alternatives for specific Java version."""
        try:
            # Determine which Java installation to use
            if java_version and java_version.isdigit():
                # Look for specific Java version first
                specific_java_cmds = [
                    # Try different possible architectures
                    f"ls -d /usr/lib/jvm/java-{java_version}-openjdk-* 2>/dev/null | head -1",
                    f"find /usr/lib/jvm -name 'java-{java_version}-openjdk*' -type d | head -1",
                ]
                
                java_home = None
                for cmd in specific_java_cmds:
                    find_java_result = self.orchestrator.execute_command(cmd)
                    if find_java_result["exit_code"] == 0 and find_java_result["output"].strip():
                        java_home = find_java_result["output"].strip()
                        logger.info(f"Found Java {java_version} installation at: {java_home}")
                        break
                
                if not java_home:
                    logger.warning(f"Could not find Java {java_version} installation, falling back to latest")
                    find_java_result = self.orchestrator.execute_command("find /usr/lib/jvm -name 'java-*-openjdk*' -type d | sort -V | tail -1")
                    if find_java_result["exit_code"] == 0 and find_java_result["output"].strip():
                        java_home = find_java_result["output"].strip()
                        logger.info(f"Found fallback Java installation at: {java_home}")
            else:
                # Find the latest available JDK
                find_java_result = self.orchestrator.execute_command("find /usr/lib/jvm -name 'java-*-openjdk*' -type d | sort -V | tail -1")
                if find_java_result["exit_code"] == 0 and find_java_result["output"].strip():
                    java_home = find_java_result["output"].strip()
                    logger.info(f"Found Java installation at: {java_home}")
                else:
                    java_home = None
            
            if not java_home:
                logger.warning("Could not find any Java installation directory")
                return None
            
            # Set up Java binaries paths
            java_bin = f"{java_home}/bin/java"
            javac_bin = f"{java_home}/bin/javac"
            
            # Verify binaries exist
            verify_java = self.orchestrator.execute_command(f"test -f {java_bin} && test -f {javac_bin} && echo 'verified'")
            if "verified" not in verify_java.get("output", ""):
                logger.warning(f"Java binaries not found at {java_home}")
                return None
            
            # Set JAVA_HOME system-wide
            java_env_commands = [
                f"echo 'export JAVA_HOME={java_home}' >> /etc/profile",
                f"echo 'export JAVA_HOME={java_home}' >> /root/.bashrc",
                f"echo 'export PATH=$JAVA_HOME/bin:$PATH' >> /etc/profile",
                f"echo 'export PATH=$JAVA_HOME/bin:$PATH' >> /root/.bashrc"
            ]
            
            for cmd in java_env_commands:
                result = self.orchestrator.execute_command(cmd)
                if not result["success"]:
                    logger.warning(f"Failed to execute: {cmd}")
            
            # Set up alternatives for java and javac
            alternatives_commands = [
                f"update-alternatives --install /usr/bin/java java {java_bin} 100",
                f"update-alternatives --install /usr/bin/javac javac {javac_bin} 100",
                f"update-alternatives --set java {java_bin}",
                f"update-alternatives --set javac {javac_bin}"
            ]
            
            for cmd in alternatives_commands:
                result = self.orchestrator.execute_command(cmd)
                if not result["success"]:
                    logger.warning(f"Failed to set alternative: {cmd} - {result.get('output', '')}")
                else:
                    logger.info(f"Successfully executed: {cmd}")
            
            # Verify the setup
            verify_result = self.orchestrator.execute_command("java -version 2>&1 && echo '---' && javac -version 2>&1")
            if verify_result["exit_code"] == 0:
                logger.info(f"Java environment setup completed successfully")
                logger.info(f"Verification output: {verify_result['output'][:200]}...")
            else:
                logger.warning(f"Java environment verification failed: {verify_result.get('output', '')}")
            
            logger.info(f"Set JAVA_HOME to {java_home}")
            return java_home
                
        except Exception as e:
            logger.warning(f"Failed to setup Java environment: {e}")
            return None
    
    def _handle_clone_error(self, output: str, repository_url: str, target_directory: str, command: str) -> ToolResult:
        """Handle git clone errors with specific suggestions."""
        
        error_suggestions = []
        documentation_links = []
        error_code = "CLONE_ERROR"
        
        # CRITICAL FIX: Before treating as error, verify if clone actually succeeded
        # Sometimes git clone outputs warnings but still succeeds
        if "already exists" in output:
            # Check if the target directory contains a valid git repository
            verification_result = self._verify_clone_success(target_directory)
            if verification_result["success"]:
                logger.info(f"✅ Clone succeeded despite 'already exists' warning")
                logger.info(f"✅ Verified git repository at: {target_directory}")
                
                # Return success with the verification info
                return ToolResult(
                    success=True,
                    output=f"Repository cloned successfully to {target_directory}",
                    metadata={
                        "repository_url": repository_url,
                        "target_directory": target_directory,
                        "verification_details": verification_result["details"],
                        "clone_warning": "Directory already existed but clone succeeded"
                    }
                )
            else:
                # Actually failed due to directory conflict
                error_code = "DIRECTORY_EXISTS"
                error_suggestions.extend([
                    f"Directory '{target_directory}' already exists and contains conflicting content",
                    "Use a different target directory name",
                    "Remove the existing directory first: bash(command='rm -rf {target_directory}')",
                    "Or use project_setup with a different target_directory parameter"
                ])
        
        elif "fatal: repository" in output and "not found" in output:
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

    def _verify_clone_success(self, target_directory: str) -> Dict[str, Any]:
        """
        Verify if a git clone operation actually succeeded by checking the target directory.
        
        Returns:
            Dict with 'success' (bool) and 'details' (str) keys
        """
        try:
            # CRITICAL FIX: Properly handle path construction for verification
            # The target_directory might be:
            # 1. Just the repo name (e.g., "commons-cli") - relative to working directory
            # 2. An absolute path (e.g., "/workspace/commons-cli")
            # 3. Incorrectly set to working directory itself (e.g., "/workspace")
            
            if target_directory.startswith('/'):
                # Absolute path - use as is, but check if it's actually the working directory
                if target_directory == "/workspace":
                    # CRITICAL: If target_directory is just "/workspace", this is a bug!
                    # The actual cloned repo should be in a subdirectory
                    # Try to find the actual repository directory
                    logger.warning(f"🐛 BUG DETECTED: target_directory is '{target_directory}' which is the working directory itself")
                    
                    # Look for git repositories in the workspace
                    find_git_cmd = "find /workspace -maxdepth 1 -type d -name '.git' -exec dirname {} \\;"
                    find_result = self.orchestrator.execute_command(find_git_cmd, workdir=None)
                    
                    if find_result.get("success") and find_result.get("output", "").strip():
                        # Found git repositories, use the first one
                        git_dirs = [d.strip() for d in find_result["output"].strip().split('\n') if d.strip()]
                        if git_dirs:
                            check_path = git_dirs[0]
                            logger.info(f"🔧 AUTOFIX: Found git repository at {check_path}, using as check_path")
                        else:
                            check_path = target_directory
                    else:
                        # Fallback: Look for any subdirectories that might be the repo
                        find_dirs_cmd = "find /workspace -maxdepth 1 -type d ! -name '.*' ! -name 'workspace' | grep -v '^/workspace$'"
                        dirs_result = self.orchestrator.execute_command(find_dirs_cmd, workdir=None)
                        
                        if dirs_result.get("success") and dirs_result.get("output", "").strip():
                            dirs = [d.strip() for d in dirs_result["output"].strip().split('\n') if d.strip()]
                            if dirs:
                                # Check if any of these directories contains .git
                                for potential_repo in dirs:
                                    git_test = self.orchestrator.execute_command(f"test -d '{potential_repo}/.git' && echo 'FOUND'", workdir=None)
                                    if git_test.get("success") and "FOUND" in git_test.get("output", ""):
                                        check_path = potential_repo
                                        logger.info(f"🔧 AUTOFIX: Found git repository at {check_path}")
                                        break
                                else:
                                    check_path = target_directory
                            else:
                                check_path = target_directory
                        else:
                            check_path = target_directory
                else:
                    check_path = target_directory
            else:
                # Relative path - assume it's relative to /workspace (the standard working directory)
                check_path = f"/workspace/{target_directory}"
            
            logger.debug(f"🔍 Verifying clone success at path: {check_path}")
            
            # Check if directory exists and contains .git
            git_check = self.orchestrator.execute_command(f"test -d '{check_path}/.git' && echo 'HAS_GIT' || echo 'NO_GIT'", workdir=None)
            
            if git_check.get("success") and "HAS_GIT" in git_check.get("output", ""):
                # Further verify it's a valid git repository
                verify_cmd = f"cd '{check_path}' && git status 2>/dev/null && echo 'VALID_REPO' || echo 'INVALID_REPO'"
                verify_result = self.orchestrator.execute_command(verify_cmd, workdir=None)
                
                if verify_result.get("success") and "VALID_REPO" in verify_result.get("output", ""):
                    # Get some details about the repository
                    details_cmd = f"cd '{check_path}' && git remote -v | head -1"
                    details_result = self.orchestrator.execute_command(details_cmd, workdir=None)
                    
                    logger.info(f"✅ VERIFICATION SUCCESS: Valid git repository at {check_path}")
                    
                    return {
                        "success": True,
                        "details": f"Valid git repository found at {check_path}. " + 
                                 (details_result.get("output", "").strip() if details_result.get("success") else "")
                    }
                else:
                    logger.warning(f"❌ VERIFICATION FAILED: Directory {check_path} exists but git status failed")
                    logger.debug(f"Git status output: {verify_result.get('output', 'no output')}")
                    return {
                        "success": False,
                        "details": f"Directory {check_path} exists but is not a valid git repository"
                    }
            else:
                logger.warning(f"❌ VERIFICATION FAILED: No .git directory found at {check_path}")
                logger.debug(f"Git check output: {git_check.get('output', 'no output')}")
                return {
                    "success": False,
                    "details": f"Directory {check_path} does not exist or does not contain .git"
                }
                
        except Exception as e:
            logger.warning(f"Clone verification failed with exception: {e}")
            return {
                "success": False,
                "details": f"Verification failed due to exception: {str(e)}"
            }
    
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
        
        # Detect Java version if it's a Java project
        java_version = None
        if project_type['type'] in ['maven', 'gradle']:
            java_version = self._detect_java_version_requirement(working_directory, project_type)
        
        # Install dependencies based on project type
        try:
            result = self._install_dependencies_for_project_type(project_type, working_directory, java_version)
            
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

1. Clone a repository (with automatic Java version detection):
   project_setup(action="clone", repository_url="https://github.com/apache/tika.git")
   # Automatically detects Java 17 requirement and installs openjdk-17-jdk

2. Clone with specific options:
   project_setup(action="clone", repository_url="https://github.com/user/repo.git", 
                 target_directory="my-project", branch="develop", auto_install_deps=True)

3. Detect project type:
   project_setup(action="detect_project_type")

4. Install dependencies (with Java version detection):
   project_setup(action="install_dependencies")
   # Detects required Java version from pom.xml/build.gradle and installs correct JDK

5. Analyze project structure:
   project_setup(action="analyze_structure")

Java Version Detection Features:
- Maven Enforcer plugin RequireJavaVersion detection (highest priority)
- maven.compiler.release/target/source property analysis  
- Parent POM support for multi-module projects
- Gradle toolchain and compatibility settings
- Automatic update-alternatives configuration
- JAVA_HOME environment setup

Common Workflow:
1. Clone repository
2. Detect project type and Java version (done automatically after clone)
3. Install correct Java version and dependencies (done automatically if auto_install_deps=True)
4. Use language-specific tools (maven, gradle, npm, etc.) with correct Java environment
"""