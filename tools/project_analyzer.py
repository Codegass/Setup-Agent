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
        directory: str = None,  # Support legacy parameter name
        update_context: bool = True
    ) -> ToolResult:
        """
        Analyze project and generate execution plan.
        
        Args:
            action: Action to perform ('analyze' for full analysis)
            project_path: Path to the project directory in container
            directory: Legacy parameter name (mapped to project_path)
            update_context: Whether to update the trunk context with new tasks
        """
        
        # Handle legacy parameter name 'directory'
        if directory is not None:
            project_path = directory
            logger.info(f"⚠️ Using legacy parameter 'directory', mapped to project_path: {project_path}")
        
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
                    output="",
                    error=f"Invalid action '{action}'. Use 'analyze' for project analysis.",
                    suggestions=["Use action='analyze' to perform comprehensive project analysis"]
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

        # Step 5: 生成智能执行计划
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

            # 提取构建命令
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
                documentation["build_commands"].extend(matches)

            # 提取测试命令
            test_patterns = [
                r"mvn.*?test",
                r"gradle.*?test",
                r"npm.*?test",
                r"pytest",
                r"python.*?test"
            ]

            for pattern in test_patterns:
                matches = re.findall(pattern, readme_content, re.IGNORECASE)
                documentation["test_commands"].extend(matches)

        return documentation

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
        """分析Maven配置（pom.xml）"""
        result = self.docker_orchestrator.execute_command(f"cat {project_path}/pom.xml")
        if result.get("success"):
            pom_content = result.get("output", "")
            
            # 提取 Java 版本
            java_version_patterns = [
                r"<java\.version>([^<]+)</java\.version>",
                r"<maven\.compiler\.source>([^<]+)</maven\.compiler\.source>",
                r"<maven\.compiler\.target>([^<]+)</maven\.compiler\.target>"
            ]
            
            for pattern in java_version_patterns:
                match = re.search(pattern, pom_content)
                if match:
                    config["java_version"] = match.group(1).strip()
                    break

            # 提取依赖信息
            dependency_matches = re.findall(r"<groupId>([^<]+)</groupId>.*?<artifactId>([^<]+)</artifactId>", pom_content, re.DOTALL)
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

        plan.append({
            "id": "run_tests",
            "description": test_desc,
            "priority": "critical",
            "type": "test",
            "core_step": "test"
        })

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