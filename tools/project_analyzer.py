"""Project analyzer tool for intelligent project setup planning."""

import json
import re
from typing import Dict, Any, List, Optional
from loguru import logger

from .base import BaseTool, ToolResult


class ProjectAnalyzerTool(BaseTool):
    """Tool for analyzing project structure and generating intelligent execution plans."""

    def __init__(self, docker_orchestrator=None, context_manager=None):
        super().__init__(
            name="project_analyzer",
            description="Analyze cloned project structure, requirements, and documentation to generate intelligent execution plan. "
            "This tool reads README files, analyzes build configurations (Maven pom.xml, Gradle build.gradle/build.gradle.kts), "
            "detects Java versions, dependencies, test frameworks (JUnit, TestNG, Spock), and creates optimized task lists for "
            "Maven and Gradle projects. Essential for intelligent project setup planning.",
        )
        self.docker_orchestrator = docker_orchestrator
        self.context_manager = context_manager

    def execute(
        self,
        action: str = "analyze",
        project_path: str = "/workspace",
        update_context: bool = True,
        **kwargs
    ) -> ToolResult:
        """
        Analyze project and generate execution plan.
        
        Args:
            action: Action to perform ('analyze' for full analysis)
            project_path: Path to the project directory in container
            update_context: Whether to update the trunk context with new tasks
        """
        
        # Check for unexpected parameters
        if kwargs:
            invalid_params = list(kwargs.keys())
            return ToolResult(
                success=False,
                output=(
                    f"❌ Invalid parameters for project_analyzer tool: {invalid_params}\n\n"
                    f"✅ Valid parameters:\n"
                    f"  - action (optional): 'analyze' (default: 'analyze')\n"
                    f"  - project_path (optional): Path to project directory (default: '/workspace')\n"
                    f"  - update_context (optional): Update trunk context (default: True)\n\n"
                    f"Example: project_analyzer(action='analyze', project_path='/workspace/myproject')\n"
                    f"Example: project_analyzer()"  # Uses all defaults
                ),
                error=f"Invalid parameters: {invalid_params}"
            )
        
        logger.info(f"Starting project analysis at: {project_path}")

        try:
            if action == "analyze":
                # Step 1: Validate and discover project path
                validated_path = self._validate_and_discover_project_path(project_path)
                if not validated_path:
                    return ToolResult(
                        success=False,
                        output="",
                        error=f"No valid project found at {project_path} or in common subdirectories",
                        suggestions=[
                            "Ensure the project has been cloned successfully",
                            "Check if the project contains build files (pom.xml, build.gradle, package.json, etc.)",
                            "Try specifying the exact project directory path",
                            "Use bash tool to list directory contents: bash(command='ls -la /workspace')"
                        ],
                        error_code="PROJECT_NOT_FOUND"
                    )
                
                logger.info(f"✅ Using validated project path: {validated_path}")
                
                # Step 2: Perform comprehensive analysis
                analysis_result = self._perform_comprehensive_analysis(validated_path)
                
                # Step 3: Validate analysis results
                if not self._is_analysis_valid(analysis_result):
                    return ToolResult(
                        success=False,
                        output="",
                        error="Project analysis failed to detect valid project structure",
                        suggestions=[
                            "Verify the project is properly structured",
                            "Check if build files are accessible",
                            "Ensure the project directory is correct",
                            "Try manual analysis with bash tool"
                        ],
                        error_code="ANALYSIS_FAILED"
                    )
                
                # Step 4: Update context if requested
                if update_context and self.context_manager:
                    success = self._update_trunk_context_with_plan(analysis_result)
                    if success:
                        analysis_result["context_updated"] = True
                    else:
                        analysis_result["context_updated"] = False
                        analysis_result["context_error"] = "Failed to update trunk context"
                
                return ToolResult(
                    success=True,
                    output=self._format_analysis_output(analysis_result),
                    metadata=analysis_result
                )
            else:
                return ToolResult(
                    success=False,
                    output=(
                        f"❌ Invalid action for project_analyzer tool: '{action}'\n\n"
                        f"✅ Valid actions:\n"
                        f"  - analyze: Perform comprehensive project analysis and generate setup plan\n\n"
                        f"Examples:\n"
                        f"  project_analyzer(action='analyze')\n"
                        f"  project_analyzer(action='analyze', project_path='/workspace/myproject')\n"
                        f"  project_analyzer()  # Uses default action='analyze'"
                    ),
                    error=f"Invalid action: {action}",
                    suggestions=[
                        "Use action='analyze' to perform comprehensive project analysis",
                        "Check the tool documentation for valid actions"
                    ]
                )
                
        except Exception as e:
            logger.error(f"Failed to analyze project: {e}")
            return ToolResult(
                success=False,
                output="",
                error=f"Project analysis failed: {str(e)}",
                suggestions=[
                    "Check if project is properly cloned and accessible",
                    "Verify Docker container has access to the project directory",
                    "Try using bash tool to manually inspect the project structure"
                ],
                error_code="ANALYSIS_EXCEPTION"
            )

    def _perform_comprehensive_analysis(self, project_path: str) -> Dict[str, Any]:
        """Perform comprehensive project analysis."""
        analysis = {
            "project_path": project_path,
            "project_type": "unknown",
            "build_system": "unknown",
            "java_version": None,
            "dependencies": [],
            "test_framework": "unknown",
            "documentation": {},
            "special_requirements": [],
            "execution_plan": []
        }

        # Step 1: 检测项目基本结构
        project_structure = self._analyze_project_structure(project_path)
        analysis.update(project_structure)

        # Step 2: 读取并分析文档
        documentation = self._analyze_documentation(project_path)
        analysis["documentation"] = documentation

        # Step 3: 分析构建配置
        build_config = self._analyze_build_configuration(project_path, analysis["project_type"])
        analysis.update(build_config)

        # Step 4: 检测测试配置
        test_config = self._analyze_test_configuration(project_path, analysis["project_type"])
        analysis.update(test_config)

        # Step 5: 估算测试总数
        tests_expected_total = self._estimate_total_test_cases(
            project_path, 
            analysis["project_type"], 
            analysis.get("build_system", "unknown")
        )
        if tests_expected_total is not None:
            analysis["tests_expected_total"] = tests_expected_total
            logger.info(f"Estimated {tests_expected_total} total test cases for project")

        # Step 6: 生成智能执行计划
        execution_plan = self._generate_execution_plan(analysis)
        analysis["execution_plan"] = execution_plan

        return analysis

    def _analyze_project_structure(self, project_path: str) -> Dict[str, Any]:
        """分析项目结构，检测项目类型和构建系统"""
        if not self.docker_orchestrator:
            return {"project_type": "unknown", "build_system": "unknown"}

        # 检查关键文件存在性
        files_to_check = [
            "pom.xml",           # Maven
            "build.gradle",      # Gradle  
            "package.json",      # Node.js
            "requirements.txt",  # Python
            "pyproject.toml",    # Python Poetry
            "Cargo.toml",        # Rust
            "go.mod",           # Go
            "CMakeLists.txt",   # CMake
            "Makefile",         # Make
            "README.md",
            "README.txt",
            "README"
        ]

        existing_files = []
        for file in files_to_check:
            result = self.docker_orchestrator.execute_command(
                f"test -f {project_path}/{file} && echo 'exists' || echo 'missing'"
            )
            if result.get("success") and "exists" in result.get("output", ""):
                existing_files.append(file)

        # 检测项目类型
        project_type = "unknown"
        build_system = "unknown"

        if "pom.xml" in existing_files:
            project_type = "Java"
            build_system = "Maven"
        elif "build.gradle" in existing_files:
            project_type = "Java"
            build_system = "Gradle"
        elif "package.json" in existing_files:
            project_type = "Node.js"
            build_system = "npm/yarn"
        elif "requirements.txt" in existing_files or "pyproject.toml" in existing_files:
            project_type = "Python"
            build_system = "pip/poetry"
        elif "Cargo.toml" in existing_files:
            project_type = "Rust"
            build_system = "Cargo"
        elif "go.mod" in existing_files:
            project_type = "Go"
            build_system = "Go modules"

        logger.info(f"Detected project type: {project_type}, build system: {build_system}")

        return {
            "project_type": project_type,
            "build_system": build_system,
            "existing_files": existing_files
        }

    def _analyze_documentation(self, project_path: str) -> Dict[str, Any]:
        """分析项目文档，提取关键信息"""
        documentation = {
            "readme_content": "",
            "setup_instructions": [],
            "build_commands": [],
            "test_commands": [],
            "requirements": [],
            "java_version_requirement": None
        }

        if not self.docker_orchestrator:
            return documentation

        # 尝试读取 README 文件
        readme_files = ["README.md", "README.txt", "README", "docs/README.md"]
        readme_content = ""

        for readme_file in readme_files:
            result = self.docker_orchestrator.execute_command(f"cat {project_path}/{readme_file}")
            if result.get("success"):
                readme_content = result.get("output", "")
                logger.info(f"Successfully read {readme_file}")
                break

        documentation["readme_content"] = readme_content

        if readme_content:
            # 提取 Java 版本要求
            java_patterns = [
                r"Java\s+(\d+)",
                r"JDK\s+(\d+)",
                r"java\.version.*?(\d+)",
                r"requires.*Java\s+(\d+)"
            ]
            
            for pattern in java_patterns:
                match = re.search(pattern, readme_content, re.IGNORECASE)
                if match:
                    documentation["java_version_requirement"] = match.group(1)
                    break

            # 提取构建命令 - 清理markdown格式
            build_patterns = [
                r"mvn.*?compile",
                r"mvn.*?install",
                r"mvn.*?package",
                r"gradle.*?build",
                r"npm.*?build",
                r"pip install",
                r"python setup\.py"
            ]

            for pattern in build_patterns:
                matches = re.findall(pattern, readme_content, re.IGNORECASE)
                # 清理提取的命令
                for match in matches:
                    clean_cmd = self._clean_markdown_command(match)
                    if clean_cmd and clean_cmd not in documentation["build_commands"]:
                        documentation["build_commands"].append(clean_cmd)

            # 提取测试命令 - 清理markdown格式
            test_patterns = [
                r"mvn.*?test",
                r"gradle.*?test",
                r"npm.*?test",
                r"pytest",
                r"python.*?test"
            ]

            for pattern in test_patterns:
                matches = re.findall(pattern, readme_content, re.IGNORECASE)
                # 清理提取的命令
                for match in matches:
                    clean_cmd = self._clean_markdown_command(match)
                    # Filter out invalid test commands
                    if clean_cmd and clean_cmd not in documentation["test_commands"]:
                        # Skip commands with -Dtest without a value (invalid Maven syntax)
                        if '-Dtest' in clean_cmd and not re.search(r'-Dtest=\S+', clean_cmd):
                            # Fix the command by removing invalid -Dtest
                            clean_cmd = clean_cmd.replace('-Dtest', '').strip()
                            # If it becomes just 'mvn clean install', change to 'mvn clean test'
                            if clean_cmd == 'mvn clean install -Dossindex.skip':
                                clean_cmd = 'mvn clean test -Dossindex.skip'
                        documentation["test_commands"].append(clean_cmd)

        return documentation
    
    def _clean_markdown_command(self, command: str) -> str:
        """清理从markdown中提取的命令，移除格式化字符"""
        if not command:
            return ""
        
        clean_cmd = command.strip()
        
        # 移除markdown代码块标记
        clean_cmd = re.sub(r'^```[a-z]*\s*', '', clean_cmd)  # 移除开始的```bash等
        clean_cmd = re.sub(r'\s*```$', '', clean_cmd)        # 移除结束的```
        
        # 移除反引号
        clean_cmd = re.sub(r'^`+|`+$', '', clean_cmd)        # 移除首尾反引号
        
        # 移除shell提示符
        clean_cmd = re.sub(r'^[>$#]\s*', '', clean_cmd)      # 移除常见的shell提示符
        
        # 移除多余的空白字符
        clean_cmd = ' '.join(clean_cmd.split())
        
        # 如果命令被截断或包含省略号，标记为需要验证
        if '...' in clean_cmd or clean_cmd.endswith('.'):
            # 移除省略号
            clean_cmd = clean_cmd.replace('...', '').rstrip('.')
        
        return clean_cmd.strip()

    def _analyze_build_configuration(self, project_path: str, project_type: str) -> Dict[str, Any]:
        """分析构建配置文件"""
        config = {
            "java_version": None,
            "dependencies": [],
            "plugins": [],
            "profiles": [],
            "build_system": None
        }

        if not self.docker_orchestrator:
            return config

        if project_type == "Java":
            # 首先检查是Maven还是Gradle项目
            maven_exists = self.docker_orchestrator.execute_command(f"test -f {project_path}/pom.xml && echo 'exists'")
            gradle_exists = self.docker_orchestrator.execute_command(f"test -f {project_path}/build.gradle && echo 'exists'")
            gradle_kts_exists = self.docker_orchestrator.execute_command(f"test -f {project_path}/build.gradle.kts && echo 'exists'")
            
            if maven_exists.get("success") and "exists" in maven_exists.get("output", ""):
                config["build_system"] = "Maven"
                self._analyze_maven_configuration(project_path, config)
            elif (gradle_exists.get("success") and "exists" in gradle_exists.get("output", "")) or \
                 (gradle_kts_exists.get("success") and "exists" in gradle_kts_exists.get("output", "")):
                config["build_system"] = "Gradle"
                self._analyze_gradle_configuration(project_path, config)

        return config

    def _analyze_maven_configuration(self, project_path: str, config: Dict[str, Any]):
        """分析Maven配置（pom.xml）- 包括多模块项目和父POM"""
        # First, read the main pom.xml
        result = self.docker_orchestrator.execute_command(f"cat {project_path}/pom.xml")
        if not result.get("success"):
            return
            
        main_pom_content = result.get("output", "")
        
        # Check if this is a multi-module project and look for parent POMs
        all_pom_contents = [main_pom_content]
        pom_locations = [f"{project_path}/pom.xml"]
        
        # Check for parent module reference (e.g., tika-parent)
        parent_match = re.search(r"<parent>.*?<artifactId>([^<]+)</artifactId>.*?</parent>", main_pom_content, re.DOTALL)
        if parent_match:
            parent_artifact = parent_match.group(1)
            # Try to find the parent POM in common locations
            potential_parent_paths = [
                f"{project_path}/{parent_artifact}/pom.xml",
                f"{project_path}/../{parent_artifact}/pom.xml",
                f"{project_path}/parent/pom.xml"
            ]
            
            for parent_path in potential_parent_paths:
                # First check if parent POM exists
                check_result = self.docker_orchestrator.execute_command(f"test -f {parent_path} && echo 'exists' 2>/dev/null")
                if check_result.get("success") and "exists" in check_result.get("output", ""):
                    # Extract just the properties section to avoid truncation
                    props_result = self.docker_orchestrator.execute_command(
                        f"sed -n '/<properties>/,/<\\/properties>/p' {parent_path} 2>/dev/null | head -200"
                    )
                    if props_result.get("success") and props_result.get("output"):
                        # Get a minimal version of parent POM with just properties
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
                
            # 1. First check Maven Enforcer plugin for RequireJavaVersion
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
            config["java_version"] = java_version
            config["java_version_source"] = java_version_source
            config["java_version_enforced"] = java_version_enforced
        else:
            logger.warning(f"No Java version found in Maven configuration for {project_path}")

        # Check for multi-module project
        modules_match = re.search(r"<modules>(.*?)</modules>", main_pom_content, re.DOTALL)
        if modules_match:
            module_content = modules_match.group(1)
            modules = re.findall(r"<module>([^<]+)</module>", module_content)
            config["maven_modules"] = modules
            config["is_multi_module"] = True
            logger.info(f"Found multi-module Maven project with {len(modules)} modules: {modules}")
        else:
            config["maven_modules"] = []
            config["is_multi_module"] = False

        # Extract dependencies from main POM only
        dependency_matches = re.findall(r"<groupId>([^<]+)</groupId>.*?<artifactId>([^<]+)</artifactId>", main_pom_content, re.DOTALL)
        config["dependencies"] = [f"{group}:{artifact}" for group, artifact in dependency_matches[:10]]  # 限制输出

    def _analyze_gradle_configuration(self, project_path: str, config: Dict[str, Any]):
        """分析Gradle配置（build.gradle 或 build.gradle.kts）"""
        # 首先尝试读取 build.gradle
        gradle_content = ""
        gradle_file = ""
        
        result = self.docker_orchestrator.execute_command(f"cat {project_path}/build.gradle")
        if result.get("success"):
            gradle_content = result.get("output", "")
            gradle_file = "build.gradle"
        else:
            # 尝试读取 build.gradle.kts
            result = self.docker_orchestrator.execute_command(f"cat {project_path}/build.gradle.kts")
            if result.get("success"):
                gradle_content = result.get("output", "")
                gradle_file = "build.gradle.kts"

        if gradle_content:
            logger.info(f"Analyzing Gradle configuration from {gradle_file}")
            
            # 提取 Java 版本
            self._extract_gradle_java_version(gradle_content, config)
            
            # 提取依赖信息
            self._extract_gradle_dependencies(gradle_content, config)
            
            # 提取插件信息
            self._extract_gradle_plugins(gradle_content, config)

    def _extract_gradle_java_version(self, gradle_content: str, config: Dict[str, Any]):
        """从Gradle配置中提取Java版本"""
        java_version_patterns = [
            # Java toolchain configuration
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
                # 处理版本号格式（比如 1.8 -> 8）
                if version.startswith("1."):
                    version = version[2:]
                config["java_version"] = version
                logger.info(f"Found Java version: {version}")
                break

    def _extract_gradle_dependencies(self, gradle_content: str, config: Dict[str, Any]):
        """从Gradle配置中提取依赖信息"""
        # 匹配各种依赖声明格式
        dependency_patterns = [
            # implementation 'group:artifact:version'
            r"implementation\s+['\"]([^:]+):([^:]+):[^'\"]+['\"]",
            # api 'group:artifact:version'
            r"api\s+['\"]([^:]+):([^:]+):[^'\"]+['\"]",
            # testImplementation 'group:artifact:version'
            r"testImplementation\s+['\"]([^:]+):([^:]+):[^'\"]+['\"]",
            # compile 'group:artifact:version' (legacy)
            r"compile\s+['\"]([^:]+):([^:]+):[^'\"]+['\"]",
            # Kotlin DSL style
            r"implementation\(['\"]([^:]+):([^:]+):[^'\"]+['\"]\)",
            r"api\(['\"]([^:]+):([^:]+):[^'\"]+['\"]\)",
        ]
        
        dependencies = []
        for pattern in dependency_patterns:
            matches = re.findall(pattern, gradle_content, re.MULTILINE)
            for group, artifact in matches:
                dep = f"{group}:{artifact}"
                if dep not in dependencies:
                    dependencies.append(dep)
        
        # 限制输出数量并去重
        config["dependencies"] = dependencies[:15]
        if dependencies:
            logger.info(f"Found {len(dependencies)} Gradle dependencies")

    def _extract_gradle_plugins(self, gradle_content: str, config: Dict[str, Any]):
        """从Gradle配置中提取插件信息"""
        plugin_patterns = [
            # plugins { id 'plugin-name' }
            r"id\s+['\"]([^'\"]+)['\"]",
            # apply plugin: 'plugin-name'
            r"apply\s+plugin:\s+['\"]([^'\"]+)['\"]",
            # Kotlin DSL: id("plugin-name")
            r"id\(['\"]([^'\"]+)['\"]\)",
        ]
        
        plugins = []
        for pattern in plugin_patterns:
            matches = re.findall(pattern, gradle_content, re.MULTILINE)
            for plugin in matches:
                if plugin not in plugins:
                    plugins.append(plugin)
        
        config["plugins"] = plugins[:10]  # 限制输出
        if plugins:
            logger.info(f"Found Gradle plugins: {', '.join(plugins)}")

    def _analyze_test_configuration(self, project_path: str, project_type: str) -> Dict[str, Any]:
        """分析测试配置"""
        test_config = {
            "test_framework": "unknown",
            "test_directories": [],
            "test_patterns": [],
            "build_system": None
        }

        if not self.docker_orchestrator:
            return test_config

        # 检查测试目录
        test_dirs = ["src/test", "test", "tests", "__tests__"]
        for test_dir in test_dirs:
            result = self.docker_orchestrator.execute_command(f"test -d {project_path}/{test_dir} && echo 'exists'")
            if result.get("success") and "exists" in result.get("output", ""):
                test_config["test_directories"].append(test_dir)

        # 根据项目类型检测测试框架
        if project_type == "Java":
            # 检查是Maven还是Gradle项目
            maven_exists = self.docker_orchestrator.execute_command(f"test -f {project_path}/pom.xml && echo 'exists'")
            gradle_exists = self.docker_orchestrator.execute_command(f"test -f {project_path}/build.gradle && echo 'exists'")
            gradle_kts_exists = self.docker_orchestrator.execute_command(f"test -f {project_path}/build.gradle.kts && echo 'exists'")
            
            if maven_exists.get("success") and "exists" in maven_exists.get("output", ""):
                test_config["build_system"] = "Maven"
                self._detect_maven_test_framework(project_path, test_config)
            elif (gradle_exists.get("success") and "exists" in gradle_exists.get("output", "")) or \
                 (gradle_kts_exists.get("success") and "exists" in gradle_kts_exists.get("output", "")):
                test_config["build_system"] = "Gradle"
                self._detect_gradle_test_framework(project_path, test_config)

        return test_config

    def _detect_maven_test_framework(self, project_path: str, test_config: Dict[str, Any]):
        """检测Maven项目的测试框架"""
        # 检查是否使用 JUnit
        result = self.docker_orchestrator.execute_command(f"grep -r 'junit' {project_path}/pom.xml")
        if result.get("success") and result.get("output"):
            test_config["test_framework"] = "JUnit"
        
        # 检查是否使用 TestNG
        result = self.docker_orchestrator.execute_command(f"grep -r 'testng' {project_path}/pom.xml")
        if result.get("success") and result.get("output"):
            test_config["test_framework"] = "TestNG"

    def _detect_gradle_test_framework(self, project_path: str, test_config: Dict[str, Any]):
        """检测Gradle项目的测试框架"""
        # 尝试读取build.gradle文件
        gradle_content = ""
        result = self.docker_orchestrator.execute_command(f"cat {project_path}/build.gradle")
        if result.get("success"):
            gradle_content = result.get("output", "")
        else:
            # 尝试读取build.gradle.kts文件
            result = self.docker_orchestrator.execute_command(f"cat {project_path}/build.gradle.kts")
            if result.get("success"):
                gradle_content = result.get("output", "")

        if gradle_content:
            # 检测测试框架
            test_frameworks = self._parse_gradle_test_frameworks(gradle_content)
            if test_frameworks:
                test_config["test_framework"] = ", ".join(test_frameworks)
                logger.info(f"Found Gradle test frameworks: {test_frameworks}")

    def _estimate_total_test_cases(self, project_path: str, project_type: str, build_system: str) -> Optional[int]:
        """
        Estimate the total number of test cases in the project.
        Uses build tool signals first, then falls back to filesystem scanning.
        
        Returns:
            Estimated number of test cases, or None if unable to estimate
        """
        if not self.docker_orchestrator:
            return None
            
        logger.info(f"Estimating test cases for {project_type} project with {build_system}")
        
        # Prefer direct annotation counting for Java-based projects
        if project_type and project_type.lower() == "java" or build_system in {"Maven", "Gradle"}:
            annotation_total = self._count_java_test_annotations(project_path)
            if annotation_total:
                logger.info(f"Detected {annotation_total} test methods via @Test annotation scan")
                return annotation_total

        # Strategy 1: Maven - Static analysis of test files only
        if build_system == "Maven":
            # First, try to find all test directories in multi-module projects
            # Look for any src/test/java directories recursively
            test_dirs_result = self.docker_orchestrator.execute_command(
                f"find {project_path} -type d -path '*/src/test/java' 2>/dev/null"
            )
            
            test_file_count = 0
            
            if test_dirs_result.get("success") and test_dirs_result.get("output"):
                # Count test files in all test directories found (handles multi-module projects)
                # Use proper find syntax with parentheses for OR conditions
                result = self.docker_orchestrator.execute_command(
                    f"find {project_path} -path '*/src/test/java/*' \\( -name '*Test.java' -o -name '*Tests.java' -o -name 'Test*.java' -o -name '*TestCase.java' -o -name '*IT.java' \\) -type f 2>/dev/null | wc -l"
                )
                
                # Also log which modules have tests for debugging
                modules_with_tests = self.docker_orchestrator.execute_command(
                    f"find {project_path} -type d -path '*/src/test/java' 2>/dev/null | wc -l"
                )
                if modules_with_tests.get("success"):
                    module_count = int(modules_with_tests.get("output", "0").strip())
                    if module_count > 0:
                        logger.debug(f"Found {module_count} Maven modules with test directories")
            else:
                # Fallback: Look for test files in standard single-module Maven structure
                result = self.docker_orchestrator.execute_command(
                    f"find {project_path}/src/test -type f \\( -name '*Test.java' -o -name '*Tests.java' -o -name 'Test*.java' -o -name '*TestCase.java' -o -name '*IT.java' \\) 2>/dev/null | wc -l"
                )
            if result.get("success"):
                try:
                    file_count = int(result["output"].strip())
                    if file_count > 0:
                        # Count actual @Test annotations in all test files
                        # This gives us the exact number of test methods, not an estimate
                        
                        # Count JUnit 4/5 @Test annotations
                        test_count_result = self.docker_orchestrator.execute_command(
                            f"find {project_path} -path '*/src/test/java/*' \\( -name '*Test.java' -o -name '*Tests.java' -o -name 'Test*.java' -o -name '*TestCase.java' -o -name '*IT.java' \\) -type f -exec grep -h '@Test' {{}} \\; 2>/dev/null | wc -l"
                        )
                        
                        if test_count_result.get("success"):
                            try:
                                test_count = int(test_count_result["output"].strip())
                                if test_count > 0:
                                    logger.info(f"Found {file_count} test files containing {test_count} @Test annotations")
                                    return test_count
                            except ValueError:
                                pass
                        
                        # Fallback: If grep fails or no annotations found, try alternative patterns
                        # TestNG uses @Test, JUnit 3 uses test* method names
                        testng_result = self.docker_orchestrator.execute_command(
                            f"find {project_path} -path '*/src/test/java/*' -name '*.java' -type f -exec grep -h '@Test.*testng' {{}} \\; 2>/dev/null | wc -l"
                        )
                        junit3_result = self.docker_orchestrator.execute_command(
                            f"find {project_path} -path '*/src/test/java/*' -name '*.java' -type f -exec grep -h 'public.*void test' {{}} \\; 2>/dev/null | wc -l"
                        )
                        
                        total_tests = 0
                        if testng_result.get("success"):
                            try:
                                total_tests += int(testng_result["output"].strip())
                            except ValueError:
                                pass
                        
                        if junit3_result.get("success"):
                            try:
                                total_tests += int(junit3_result["output"].strip())
                            except ValueError:
                                pass
                        
                        if total_tests > 0:
                            logger.info(f"Found {total_tests} test methods using alternative patterns")
                            return total_tests
                        
                        # Last resort: If we found test files but couldn't count annotations
                        # This might happen if files are not readable or use unusual patterns
                        logger.warning(f"Found {file_count} test files but couldn't count test methods, using conservative estimate")
                        return file_count * 3  # Conservative estimate if annotation counting fails
                        
                except ValueError:
                    pass
        
        # Strategy 2: Gradle - Count actual test annotations
        elif build_system == "Gradle":
            # First find test files (Java, Kotlin, Groovy)
            result = self.docker_orchestrator.execute_command(
                f"find {project_path} -path '*/src/test/*' \\( -name '*.java' -o -name '*.kt' -o -name '*.groovy' \\) -type f 2>/dev/null | wc -l"
            )
            
            if result.get("success"):
                try:
                    file_count = int(result["output"].strip())
                    if file_count > 0:
                        # Count @Test annotations for JUnit/TestNG
                        test_count_result = self.docker_orchestrator.execute_command(
                            f"find {project_path} -path '*/src/test/*' \\( -name '*.java' -o -name '*.kt' -o -name '*.groovy' \\) -type f -exec grep -h '@Test' {{}} \\; 2>/dev/null | wc -l"
                        )
                        
                        if test_count_result.get("success"):
                            try:
                                test_count = int(test_count_result["output"].strip())
                                if test_count > 0:
                                    logger.info(f"Found {file_count} test files containing {test_count} @Test annotations")
                                    return test_count
                            except ValueError:
                                pass
                        
                        # For Spock tests (Groovy), look for def "test name"() patterns
                        spock_result = self.docker_orchestrator.execute_command(
                            f"find {project_path} -path '*/src/test/*' -name '*.groovy' -type f -exec grep -h 'def \".*\"()' {{}} \\; 2>/dev/null | wc -l"
                        )
                        
                        if spock_result.get("success"):
                            try:
                                spock_count = int(spock_result["output"].strip())
                                if spock_count > 0:
                                    logger.info(f"Found {spock_count} Spock test methods")
                                    return spock_count
                            except ValueError:
                                pass
                        
                        # Fallback for Gradle
                        logger.warning(f"Found {file_count} test files but couldn't count test methods")
                        return file_count * 3  # Conservative estimate
                        
                except ValueError:
                    pass
        
        # Strategy 3: Node.js projects - Count actual test definitions
        elif project_type in ["Node.js", "JavaScript", "TypeScript"]:
            patterns = [
                "*.test.js", "*.spec.js", "*.test.ts", "*.spec.ts",
                "*.test.jsx", "*.spec.jsx", "*.test.tsx", "*.spec.tsx"
            ]
            pattern_str = " -o ".join([f"-name '{p}'" for p in patterns])
            result = self.docker_orchestrator.execute_command(
                f"find {project_path} -type f \\( {pattern_str} \\) 2>/dev/null | grep -v node_modules | wc -l"
            )
            if result.get("success"):
                try:
                    file_count = int(result["output"].strip())
                    if file_count > 0:
                        # Count actual test definitions: it(), test(), describe().it, etc.
                        test_count_result = self.docker_orchestrator.execute_command(
                            f"find {project_path} -type f \\( {pattern_str} \\) -not -path '*/node_modules/*' -exec grep -h -E '\\s+(it|test)\\s*\\(' {{}} \\; 2>/dev/null | wc -l"
                        )
                        
                        if test_count_result.get("success"):
                            try:
                                test_count = int(test_count_result["output"].strip())
                                if test_count > 0:
                                    logger.info(f"Found {file_count} test files containing {test_count} test cases")
                                    return test_count
                            except ValueError:
                                pass
                        
                        # Fallback if grep fails
                        logger.warning(f"Found {file_count} test files but couldn't count test cases")
                        return file_count * 5  # Conservative estimate
                        
                except ValueError:
                    pass
        
        # Strategy 4: Python projects - Count test files and functions
        elif project_type == "Python":
            result = self.docker_orchestrator.execute_command(
                f"find {project_path} -name 'test_*.py' -o -name '*_test.py' 2>/dev/null | wc -l"
            )
            if result.get("success"):
                try:
                    count = int(result["output"].strip())
                    if count > 0:
                        # Try to count actual test functions
                        func_result = self.docker_orchestrator.execute_command(
                            f"find {project_path} -name 'test_*.py' -o -name '*_test.py' | xargs grep -h 'def test_' 2>/dev/null | wc -l"
                        )
                        if func_result.get("success"):
                            try:
                                func_count = int(func_result["output"].strip())
                                if func_count > 0:
                                    logger.info(f"Found {func_count} test functions in {count} files")
                                    return func_count
                            except ValueError:
                                pass
                        # Fallback: estimate based on files
                        estimated = count * 6
                        logger.info(f"Found {count} test files, estimating {estimated} test cases")
                        return estimated
                except ValueError:
                    pass
        
        # Generic fallback: Count any test-like files
        result = self.docker_orchestrator.execute_command(
            f"find {project_path} -type f -name '*test*' -o -name '*spec*' 2>/dev/null | grep -v -E '(node_modules|.git|target|build|dist)' | wc -l"
        )
        if result.get("success"):
            try:
                count = int(result["output"].strip())
                if count > 0:
                    # Conservative estimate for unknown projects
                    estimated = count * 3
                    logger.info(f"Found {count} test-like files, estimating {estimated} test cases")
                    return estimated
            except ValueError:
                pass
        
        logger.warning("Unable to estimate test count for project")
        return None

    def _count_java_test_annotations(self, project_path: str) -> Optional[int]:
        """Count @Test annotations across Java test sources for a project."""
        if not self.docker_orchestrator:
            return None

        count_cmd = (
            "cd {project} && "
            "find . -path '*/src/test/*' -name '*.java' -print0 2>/dev/null | "
            "while IFS= read -r -d '' file; do "
            "count=$(grep -c '@Test' \"$file\" 2>/dev/null || echo 0); "
            "count=${count##*:}; "
            "echo $count; "
            "done | awk '{sum += $1} END {print sum+0}'"
        ).format(project=project_path)

        result = self.docker_orchestrator.execute_command(count_cmd)
        if not result.get("success"):
            return None

        output = (result.get("output") or "").strip()
        if not output:
            return None

        try:
            total = int(float(output))
            return total if total > 0 else None
        except ValueError:
            logger.debug(f"Unable to parse test annotation count from output: {output}")
            return None

    def _parse_gradle_test_frameworks(self, gradle_content: str) -> List[str]:
        """从Gradle配置中解析测试框架"""
        frameworks = []
        
        # JUnit 检测模式
        junit_patterns = [
            r"junit['\"]?\s*:\s*['\"]?[0-9]",  # junit: '5.8.2'
            r"['\"]junit['\"]",                # 'junit'
            r"org\.junit\.jupiter",            # JUnit 5
            r"junit-jupiter",                  # JUnit 5
            r"junit-vintage",                  # JUnit 4 via JUnit 5
            r"useJUnitPlatform\(\)",          # JUnit Platform configuration
        ]
        
        # TestNG 检测模式
        testng_patterns = [
            r"testng['\"]?\s*:\s*['\"]?[0-9]", # testng: '7.4.0'
            r"['\"]testng['\"]",               # 'testng'
            r"org\.testng",                    # TestNG package
        ]
        
        # Spock 检测模式（Groovy测试框架）
        spock_patterns = [
            r"spock-core",
            r"['\"]spock['\"]",
            r"org\.spockframework",
        ]
        
        # 检测各种测试框架
        if any(re.search(pattern, gradle_content, re.IGNORECASE) for pattern in junit_patterns):
            frameworks.append("JUnit")
        
        if any(re.search(pattern, gradle_content, re.IGNORECASE) for pattern in testng_patterns):
            frameworks.append("TestNG")
        
        if any(re.search(pattern, gradle_content, re.IGNORECASE) for pattern in spock_patterns):
            frameworks.append("Spock")
        
        # 检测Kotlin测试相关
        if re.search(r"kotlin.*test", gradle_content, re.IGNORECASE):
            frameworks.append("Kotlin Test")
        
        return frameworks

    def _generate_execution_plan(self, analysis: Dict[str, Any]) -> List[Dict[str, str]]:
        """
        Generate intelligent execution plan based on THREE CORE STEPS:
        1. Clone repository (assumed already done by project_setup)
        2. Build project (compile/package)  
        3. Test project (run tests)
        4. Generate report
        """
        plan = []

        project_type = analysis.get("project_type", "unknown")
        build_system = analysis.get("build_system", "unknown")
        java_version = analysis.get("java_version")
        documentation = analysis.get("documentation", {})

        logger.info(f"Generating three-step execution plan for {project_type} project with {build_system}")

        # Handle unknown projects with fallback strategies
        if project_type == "unknown" or build_system == "unknown":
            logger.warning("Project type or build system unknown, generating fallback plan")
            return self._generate_three_step_fallback_plan(analysis)

        # STEP 1: Environment setup (if needed)
        if java_version:
            # Check if Java version is enforced (stricter requirement)
            is_enforced = analysis.get("java_version_enforced", False)
            version_source = analysis.get("java_version_source", "unknown")
            
            if is_enforced:
                plan.append({
                    "id": "setup_java_environment",
                    "description": f"Install and configure Java {java_version} (Required by Maven Enforcer)",
                    "priority": "critical",
                    "type": "environment",
                    "core_step": "preparation",
                    "commands": [
                        f"bash(command='java -version 2>&1 | grep \"version\" || echo \"Java not found\"')",
                        f"bash(command='apt-get update && apt-get install -y openjdk-{java_version}-jdk')",
                        f"bash(command='update-alternatives --set java /usr/lib/jvm/java-{java_version}-openjdk-$(dpkg --print-architecture)/bin/java')",
                        f"bash(command='export JAVA_HOME=/usr/lib/jvm/java-{java_version}-openjdk-$(dpkg --print-architecture) && java -version')"
                    ]
                })
            else:
                plan.append({
                    "id": "setup_environment",
                    "description": f"Verify Java {java_version} environment and install dependencies",
                    "priority": "high",
                    "type": "environment",
                    "core_step": "preparation"
                })

        # STEP 2: BUILD - Compile/package the project
        if project_type == "Java" and build_system == "Maven":
            plan.append({
                "id": "build_project",
                "description": "Compile project using Maven",
                "priority": "critical",
                "type": "build", 
                "core_step": "build"
            })
        elif project_type == "Java" and build_system == "Gradle":
            plan.append({
                "id": "build_project", 
                "description": "Compile project using Gradle",
                "priority": "critical",
                "type": "build",
                "core_step": "build"
            })
        elif project_type == "Node.js":
            plan.append({
                "id": "build_project",
                "description": "Build project using npm/yarn",
                "priority": "critical", 
                "type": "build",
                "core_step": "build"
            })
        elif project_type == "Python":
            plan.append({
                "id": "build_project",
                "description": "Setup and validate Python project",
                "priority": "critical",
                "type": "build", 
                "core_step": "build"
            })
        else:
            # Generic build step
            plan.append({
                "id": "build_project",
                "description": f"Build {project_type} project using {build_system}",
                "priority": "critical",
                "type": "build",
                "core_step": "build"
            })

        # STEP 3: TEST - Run project tests
        test_framework = analysis.get("test_framework", "unknown")
        test_commands = documentation.get("test_commands", [])
        
        if test_commands:
            test_desc = f"Run tests using documented commands: {', '.join(test_commands[:2])}"
        elif project_type == "Java" and build_system == "Maven":
            # Check if this is a multi-module project
            is_multi_module = analysis.get("is_multi_module", False)
            if is_multi_module:
                test_desc = "Run tests for all modules using Maven (multi-module project)"
                # Add specific command recommendation
                test_commands = ["maven(command='test', fail_at_end=True)"]
            else:
                test_desc = "Run tests using Maven"
            if test_framework != "unknown":
                test_desc += f" ({test_framework})"
        elif project_type == "Java" and build_system == "Gradle":
            test_desc = "Run tests using Gradle"
            if test_framework != "unknown":
                test_desc += f" ({test_framework})"
        elif project_type == "Node.js":
            test_desc = "Execute tests using npm/yarn test"
        elif project_type == "Python":
            test_desc = "Run Python tests (pytest/unittest)"
        else:
            test_desc = f"Execute {project_type} project tests"

        test_step = {
            "id": "run_tests",
            "description": test_desc,
            "priority": "critical",
            "type": "test",
            "core_step": "test"
        }

        # Add specific commands for multi-module Maven projects
        if project_type == "Java" and build_system == "Maven" and analysis.get("is_multi_module", False):
            test_step["commands"] = [
                "maven(command='test', fail_at_end=True)",
                "# This ensures all modules are tested even if some have failures"
            ]
            test_step["notes"] = "Multi-module project: use fail_at_end=True to test all modules"

        plan.append(test_step)

        # STEP 4: REPORT - Generate completion report
        plan.append({
            "id": "generate_completion_report",
            "description": "Generate comprehensive setup completion report",
            "priority": "high",
            "type": "report",
            "core_step": "report"
        })

        logger.info(f"Generated {len(plan)} tasks in three-step execution plan")
        logger.info(f"Core steps: {[task.get('core_step') for task in plan]}")
        
        return plan

    def _generate_fallback_execution_plan(self, analysis: Dict[str, Any]) -> List[Dict[str, str]]:
        """为未知项目类型生成fallback执行计划"""
        plan = []
        existing_files = analysis.get("existing_files", [])
        project_path = analysis.get("project_path", "/workspace")

        logger.info("Generating fallback execution plan for unknown project type")

        # 检查是否有任何构建文件
        if "pom.xml" in existing_files:
            plan.extend([
                {
                    "id": "analyze_maven_project",
                    "description": "Analyze Maven project structure and dependencies",
                    "priority": "high",
                    "type": "analysis"
                },
                {
                    "id": "setup_maven_environment",
                    "description": "Setup Maven build environment and install dependencies",
                    "priority": "high",
                    "type": "environment"
                },
                {
                    "id": "build_maven_project",
                    "description": "Compile Maven project",
                    "priority": "high",
                    "type": "build"
                },
                {
                    "id": "test_maven_project",
                    "description": "Execute Maven project tests",
                    "priority": "high",
                    "type": "test"
                }
            ])
        elif any(f in existing_files for f in ["build.gradle", "build.gradle.kts"]):
            plan.extend([
                {
                    "id": "analyze_gradle_project",
                    "description": "Analyze Gradle project structure and dependencies",
                    "priority": "high",
                    "type": "analysis"
                },
                {
                    "id": "setup_gradle_environment",
                    "description": "Setup Gradle build environment and install dependencies",
                    "priority": "high",
                    "type": "environment"
                },
                {
                    "id": "build_gradle_project",
                    "description": "Compile Gradle project",
                    "priority": "high",
                    "type": "build"
                },
                {
                    "id": "test_gradle_project",
                    "description": "Execute Gradle project tests",
                    "priority": "high",
                    "type": "test"
                }
            ])
        elif "package.json" in existing_files:
            plan.extend([
                {
                    "id": "analyze_nodejs_project",
                    "description": "Analyze Node.js project dependencies and scripts",
                    "priority": "high",
                    "type": "analysis"
                },
                {
                    "id": "install_npm_dependencies",
                    "description": "Install Node.js dependencies using npm/yarn",
                    "priority": "high",
                    "type": "dependencies"
                },
                {
                    "id": "build_nodejs_project",
                    "description": "Build Node.js project",
                    "priority": "high",
                    "type": "build"
                },
                {
                    "id": "test_nodejs_project",
                    "description": "Execute Node.js project tests",
                    "priority": "high",
                    "type": "test"
                }
            ])
        else:
            # 完全未知的项目，使用通用方法
            plan.extend([
                {
                    "id": "manual_project_exploration",
                    "description": f"Manually explore project structure at {project_path}",
                    "priority": "high",
                    "type": "exploration"
                },
                {
                    "id": "identify_build_system",
                    "description": "Identify project build system and requirements",
                    "priority": "high",
                    "type": "analysis"
                },
                {
                    "id": "setup_development_environment",
                    "description": "Setup appropriate development environment",
                    "priority": "high",
                    "type": "environment"
                },
                {
                    "id": "attempt_project_build",
                    "description": "Attempt to build project using identified tools",
                    "priority": "medium",
                    "type": "build"
                }
            ])

        return plan

    def _generate_basic_setup_plan(self, analysis: Dict[str, Any]) -> List[Dict[str, str]]:
        """生成基本的setup计划作为最后的fallback"""
        return [
            {
                "id": "verify_project_structure",
                "description": "Verify project structure and identify key components",
                "priority": "high",
                "type": "verification"
            },
            {
                "id": "setup_basic_environment",
                "description": "Setup basic development environment",
                "priority": "high",
                "type": "environment"
            },
            {
                "id": "manual_build_attempt",
                "description": "Attempt manual project build",
                "priority": "medium",
                "type": "build"
            }
        ]

    def _update_trunk_context_with_plan(self, analysis: Dict[str, Any]) -> bool:
        """更新trunk context的todo list（安全版本）"""
        if not self.context_manager:
            logger.warning("No context manager available for updating trunk context")
            return False

        try:
            trunk_context = self.context_manager.load_trunk_context()
            if not trunk_context:
                logger.error("No trunk context found to update")
                return False

            execution_plan = analysis.get("execution_plan", [])
            if not execution_plan:
                logger.warning("No execution plan generated, trunk context unchanged")
                return False

            # 验证执行计划的质量
            if not self._is_execution_plan_valid(execution_plan):
                logger.warning("Generated execution plan appears invalid, preserving existing tasks")
                return False

            # 获取当前pending任务数量
            current_pending = len([task for task in trunk_context.todo_list if task.status.value == "pending"])
            logger.info(f"Current pending tasks: {current_pending}, new plan has {len(execution_plan)} tasks")

            # 只有在新计划看起来合理时才替换现有任务
            if len(execution_plan) >= 3:  # 至少3个任务才认为是合理的计划
                # 清除现有的pending任务（保留已完成的和进行中的）
                remaining_tasks = [task for task in trunk_context.todo_list 
                                 if task.status.value not in ["pending"]]
                trunk_context.todo_list = remaining_tasks

                # 添加新的智能任务
                for plan_item in execution_plan:
                    task_description = plan_item.get("description", "Unknown task")
                    task_type = plan_item.get("type", "general")
                    logger.debug(f"Adding task: {task_description} (type: {task_type})")
                    trunk_context.add_task(task_description)

                # 保存更新后的context
                self.context_manager._save_trunk_context(trunk_context)
                logger.info(f"✅ Successfully updated trunk context with {len(execution_plan)} new intelligent tasks")
                return True
            else:
                logger.warning(f"Execution plan too short ({len(execution_plan)} tasks), preserving existing tasks")
                return False

        except Exception as e:
            logger.error(f"Failed to update trunk context: {e}")
            return False

    def _is_execution_plan_valid(self, execution_plan: List[Dict[str, str]]) -> bool:
        """验证执行计划是否有效"""
        if not execution_plan or len(execution_plan) < 2:
            logger.debug("Execution plan too short")
            return False

        # 检查是否只有报告任务（这通常意味着分析失败）
        non_report_tasks = [task for task in execution_plan 
                           if task.get("type") != "report" and 
                           "report" not in task.get("description", "").lower()]
        
        if len(non_report_tasks) < 2:
            logger.debug("Execution plan contains mostly report tasks")
            return False

        # 检查是否有实际的构建/测试任务
        has_build_or_test = any(
            task.get("type") in ["build", "test", "dependencies", "environment"] or
            any(keyword in task.get("description", "").lower() 
                for keyword in ["build", "compile", "test", "install", "setup"])
            for task in execution_plan
        )

        if not has_build_or_test:
            logger.debug("Execution plan lacks build/test tasks")
            return False

        logger.debug("Execution plan validation passed")
        return True

    def _format_analysis_output(self, analysis: Dict[str, Any]) -> str:
        """格式化分析输出"""
        output = "🔍 PROJECT ANALYSIS COMPLETED\n\n"
        
        # 分析路径信息
        project_path = analysis.get('project_path', 'Unknown')
        output += f"📁 Analyzed Path: {project_path}\n"
        
        # 基本信息
        project_type = analysis.get('project_type', 'Unknown')
        build_system = analysis.get('build_system', 'Unknown')
        output += f"📂 Project Type: {project_type}\n"
        output += f"🔧 Build System: {build_system}\n"
        
        # 显示发现的文件
        existing_files = analysis.get('existing_files', [])
        if existing_files:
            output += f"📄 Project Files Found: {', '.join(existing_files[:5])}\n"
            if len(existing_files) > 5:
                output += f"    ... and {len(existing_files) - 5} more files\n"
        else:
            output += f"⚠️ No project files detected\n"
        
        if analysis.get('java_version'):
            output += f"☕ Java Version: {analysis['java_version']}\n"
        
        # 依赖信息
        dependencies = analysis.get('dependencies', [])
        if dependencies:
            output += f"📦 Dependencies: {len(dependencies)} found ({', '.join(dependencies[:3])}...)\n"
        
        # 文档分析
        doc = analysis.get('documentation', {})
        if doc.get('java_version_requirement'):
            output += f"📋 Required Java Version: {doc['java_version_requirement']}\n"
        
        if doc.get('build_commands'):
            output += f"🔨 Build Commands Found: {', '.join(doc['build_commands'][:3])}\n"
        
        if doc.get('test_commands'):
            output += f"🧪 Test Commands Found: {', '.join(doc['test_commands'][:3])}\n"
        
        # 测试框架
        test_framework = analysis.get('test_framework', 'unknown')
        if test_framework != 'unknown':
            output += f"🧪 Test Framework: {test_framework}\n"

        if analysis.get('tests_expected_total'):
            output += f"🧮 Estimated Tests: {analysis['tests_expected_total']}\n"

        # 执行计划
        execution_plan = analysis.get('execution_plan', [])
        if execution_plan:
            # 分析计划类型
            plan_types = [task.get('type', 'general') for task in execution_plan]
            type_counts = {}
            for t in plan_types:
                type_counts[t] = type_counts.get(t, 0) + 1
            
            output += f"\n📋 GENERATED EXECUTION PLAN ({len(execution_plan)} tasks):\n"
            for i, task in enumerate(execution_plan, 1):
                task_type = task.get('type', 'general')
                task_desc = task.get('description', 'Unknown task')
                priority = task.get('priority', 'medium')
                type_emoji = {
                    'environment': '🔧',
                    'dependencies': '📦',
                    'build': '🔨',
                    'test': '🧪',
                    'report': '📊',
                    'analysis': '🔍',
                    'exploration': '🗺️'
                }.get(task_type, '📋')
                output += f"  {i}. {type_emoji} {task_desc} [{priority}]\n"
            
            # 显示计划质量指标
            non_report_tasks = [t for t in execution_plan if t.get('type') != 'report']
            if len(non_report_tasks) >= 3:
                output += f"\n✅ Plan Quality: Good ({len(non_report_tasks)} actionable tasks)\n"
            else:
                output += f"\n⚠️ Plan Quality: Limited ({len(non_report_tasks)} actionable tasks)\n"
        else:
            output += f"\n❌ No execution plan generated\n"
        
        # Context更新状态
        if analysis.get('context_updated'):
            output += f"\n✅ Trunk context updated with new intelligent task plan\n"
        elif analysis.get('context_updated') == False:
            context_error = analysis.get('context_error', 'Unknown error')
            output += f"\n⚠️ Context update failed: {context_error}\n"
        
        # 最终状态
        if project_type != 'Unknown' and build_system != 'Unknown' and execution_plan:
            output += f"\n🎯 Ready to execute intelligent project setup plan!"
        elif project_type == 'Unknown' or build_system == 'Unknown':
            output += f"\n⚠️ Project analysis incomplete - manual investigation may be needed"
        else:
            output += f"\n❌ Analysis failed - please check project structure and try again"
        
        return output

    def _get_parameters_schema(self) -> Dict[str, Any]:
        """Get the parameters schema for this tool."""
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["analyze"],
                    "description": "Action to perform (always 'analyze' for project analysis)",
                    "default": "analyze",
                },
                "project_path": {
                    "type": "string",
                    "description": "Path to the project directory in container",
                    "default": "/workspace",
                },
                "directory": {
                    "type": "string",
                    "description": "Legacy parameter name for project_path (automatically mapped)",
                    "default": None,
                },
                "update_context": {
                    "type": "boolean",
                    "description": "Whether to update trunk context with generated plan",
                    "default": True,
                },
            },
            "required": ["action"],
        }

    def get_usage_example(self) -> str:
        """Get usage examples for the project analyzer tool."""
        return """
Project Analyzer Tool Usage Examples:

1. Analyze project in workspace (most common):
   project_analyzer(action="analyze")

2. Analyze project in specific directory:
   project_analyzer(action="analyze", project_path="/workspace/my-project")

3. Analyze without updating context:
   project_analyzer(action="analyze", update_context=False)

4. Legacy parameter support (automatically mapped):
   project_analyzer(action="analyze", directory="/workspace/project")

🎯 THREE-STEP EXECUTION STRATEGY:
✅ STEP 1: Clone repository (handled by project_setup tool)
✅ STEP 2: Build project (compile/package - CRITICAL)
✅ STEP 3: Test project (run tests - CRITICAL) 
✅ STEP 4: Generate report

SUCCESS CRITERIA:
- SUCCESS: All three core steps (clone + build + test) succeed
- FAILED: Clone or build fails
- PARTIAL: Clone + build succeed, but tests fail

ENHANCED FEATURES:
✅ Smart path discovery - automatically finds project in subdirectories
✅ Three-step plan generation - creates clear clone → build → test → report workflow
✅ Multi-platform support - Maven, Gradle, npm, Python, Rust, Go
✅ Parameter compatibility - supports both 'project_path' and 'directory'
✅ Intelligent fallback plans - generates meaningful tasks even for unknown projects
✅ Context safety - preserves existing tasks if analysis fails
✅ Plan validation - ensures generated plans follow three-step pattern

WORKFLOW:
1. First clone the repository using project_setup tool
2. Then use project_analyzer to understand the project and generate three-step plan
3. Execute the generated tasks: build → test → report
4. Report tool will evaluate success based on all three core steps

WHAT IT ANALYZES:
- Project type (Java, Node.js, Python, Rust, Go, etc.)
- Build system (Maven, Gradle, npm, pip, Cargo, etc.)
- Java version requirements from README and config files
- Maven/Gradle dependencies and build configuration
- Test frameworks (JUnit, TestNG, Spock, Jest, pytest)
- Documentation and build/test commands
- Source code structure and organization

GENERATED PLAN FORMAT:
Each task includes a 'core_step' field indicating its role:
- core_step: "preparation" - Environment setup
- core_step: "build" - Project compilation/packaging  
- core_step: "test" - Test execution
- core_step: "report" - Final status report

ROBUST ERROR HANDLING:
- Validates project path and discovers actual project location
- Handles parameter name variations (project_path vs directory)
- Generates three-step fallback plans for unknown project types
- Preserves existing context if analysis fails
- Provides detailed diagnostic information

OUTPUT:
- Comprehensive project analysis with path validation
- Three-step execution plan: build → test → report
- Plan quality assessment and validation
- Safe context updates with rollback protection
- Clear core step identification for each task
""" 

    def _validate_and_discover_project_path(self, initial_path: str) -> Optional[str]:
        """Validate project path and discover actual project location if needed."""
        if not self.docker_orchestrator:
            logger.warning("No orchestrator available for path validation")
            return initial_path
        
        # List of paths to check (in order of preference)
        candidate_paths = [initial_path]
        
        # If initial path is /workspace, also check common subdirectories
        if initial_path == "/workspace":
            # Get list of subdirectories in workspace
            result = self.docker_orchestrator.execute_command("find /workspace -maxdepth 1 -type d")
            if result.get("success"):
                subdirs = [line.strip() for line in result.get("output", "").split('\n') 
                          if line.strip() and line.strip() != "/workspace"]
                candidate_paths.extend(subdirs)
        
        # Check each candidate path for project indicators
        for path in candidate_paths:
            if self._is_valid_project_directory(path):
                logger.info(f"✅ Found valid project at: {path}")
                return path
            else:
                logger.debug(f"❌ No project found at: {path}")
        
        return None
    
    def _is_valid_project_directory(self, path: str) -> bool:
        """Check if a directory contains valid project indicators."""
        if not self.docker_orchestrator:
            return False
        
        # Check if directory exists
        result = self.docker_orchestrator.execute_command(f"test -d {path}")
        if result.get("exit_code") != 0:
            logger.debug(f"Directory does not exist: {path}")
            return False
        
        # Check for common project files
        project_indicators = [
            "pom.xml",           # Maven
            "build.gradle",      # Gradle (Groovy)
            "build.gradle.kts",  # Gradle (Kotlin)
            "package.json",      # Node.js
            "requirements.txt",  # Python
            "pyproject.toml",    # Python Poetry
            "Cargo.toml",        # Rust
            "go.mod",           # Go
            "CMakeLists.txt",   # CMake
            "Makefile",         # Make
            "composer.json",    # PHP
            "Gemfile",          # Ruby
        ]
        
        for indicator in project_indicators:
            result = self.docker_orchestrator.execute_command(f"test -f {path}/{indicator}")
            if result.get("exit_code") == 0:
                logger.debug(f"Found project indicator {indicator} in {path}")
                return True
        
        # Check for source code directories as secondary indicators
        source_dirs = ["src", "lib", "app", "source"]
        for src_dir in source_dirs:
            result = self.docker_orchestrator.execute_command(f"test -d {path}/{src_dir}")
            if result.get("exit_code") == 0:
                # Check if it contains actual source files
                result = self.docker_orchestrator.execute_command(
                    f"find {path}/{src_dir} -name '*.java' -o -name '*.py' -o -name '*.js' -o -name '*.ts' -o -name '*.go' -o -name '*.rs' | head -1"
                )
                if result.get("success") and result.get("output", "").strip():
                    logger.debug(f"Found source files in {path}/{src_dir}")
                    return True
        
        return False
    
    def _is_analysis_valid(self, analysis: Dict[str, Any]) -> bool:
        """Validate that the analysis produced meaningful results."""
        # Check if we detected a valid project type
        if analysis.get("project_type") == "unknown" and analysis.get("build_system") == "unknown":
            logger.warning("Analysis failed to detect project type and build system")
            return False
        
        # Check if we found any project files
        existing_files = analysis.get("existing_files", [])
        if not existing_files:
            logger.warning("Analysis found no project files")
            return False
        
        # Check if execution plan was generated
        execution_plan = analysis.get("execution_plan", [])
        if not execution_plan or len(execution_plan) < 2:
            logger.warning("Analysis generated insufficient execution plan")
            return False
        
        return True 

    def _generate_three_step_fallback_plan(self, analysis: Dict[str, Any]) -> List[Dict[str, str]]:
        """Generate three-step fallback plan for unknown project types."""
        plan = []
        existing_files = analysis.get("existing_files", [])
        project_path = analysis.get("project_path", "/workspace")

        logger.info("Generating three-step fallback execution plan for unknown project type")

        # STEP 1: Environment/Dependencies
        if "pom.xml" in existing_files:
            plan.append({
                "id": "setup_environment",
                "description": "Install Maven dependencies and verify build environment",
                "priority": "high",
                "type": "environment",
                "core_step": "preparation"
            })
            plan.append({
                "id": "build_project",
                "description": "Compile project using Maven", 
                "priority": "critical",
                "type": "build",
                "core_step": "build"
            })
            plan.append({
                "id": "run_tests",
                "description": "Execute Maven project tests",
                "priority": "critical", 
                "type": "test",
                "core_step": "test"
            })
        elif any(f in existing_files for f in ["build.gradle", "build.gradle.kts"]):
            plan.append({
                "id": "setup_environment",
                "description": "Install Gradle dependencies and verify build environment",
                "priority": "high",
                "type": "environment",
                "core_step": "preparation" 
            })
            plan.append({
                "id": "build_project",
                "description": "Compile project using Gradle",
                "priority": "critical",
                "type": "build",
                "core_step": "build"
            })
            plan.append({
                "id": "run_tests", 
                "description": "Execute Gradle project tests",
                "priority": "critical",
                "type": "test",
                "core_step": "test"
            })
        elif "package.json" in existing_files:
            plan.append({
                "id": "setup_environment",
                "description": "Install Node.js dependencies using npm/yarn",
                "priority": "high",
                "type": "environment",
                "core_step": "preparation"
            })
            plan.append({
                "id": "build_project",
                "description": "Build Node.js project",
                "priority": "critical",
                "type": "build", 
                "core_step": "build"
            })
            plan.append({
                "id": "run_tests",
                "description": "Execute Node.js project tests",
                "priority": "critical",
                "type": "test",
                "core_step": "test"
            })
        else:
            # Completely unknown project  
            plan.extend([
                {
                    "id": "explore_project",
                    "description": f"Manually explore and identify project structure at {project_path}",
                    "priority": "high",
                    "type": "exploration",
                    "core_step": "preparation"
                },
                {
                    "id": "attempt_build",
                    "description": "Attempt to build project using identified tools",
                    "priority": "critical",
                    "type": "build",
                    "core_step": "build"
                },
                {
                    "id": "attempt_tests",
                    "description": "Attempt to run project tests",
                    "priority": "critical", 
                    "type": "test",
                    "core_step": "test"
                }
            ])

        # STEP 4: Always add report
        plan.append({
            "id": "generate_completion_report",
            "description": "Generate comprehensive setup completion report",
            "priority": "high",
            "type": "report",
            "core_step": "report"
        })

        return plan 
