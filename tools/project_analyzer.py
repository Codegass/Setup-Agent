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
        update_context: bool = True
    ) -> ToolResult:
        """
        Analyze project and generate execution plan.
        
        Args:
            action: Action to perform ('analyze' for full analysis)
            project_path: Path to the project directory in container
            update_context: Whether to update the trunk context with new tasks
        """
        
        logger.info(f"Starting project analysis at: {project_path}")

        try:
            if action == "analyze":
                analysis_result = self._perform_comprehensive_analysis(project_path)
                
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
                suggestions=["Check if project is properly cloned and accessible"]
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
        """根据分析结果生成智能执行计划"""
        plan = []

        project_type = analysis.get("project_type", "unknown")
        build_system = analysis.get("build_system", "unknown")
        java_version = analysis.get("java_version")
        documentation = analysis.get("documentation", {})

        # 基础环境准备任务
        if java_version:
            plan.append({
                "id": "setup_java_environment",
                "description": f"Verify and setup Java {java_version} environment",
                "priority": "high",
                "type": "environment"
            })

        # 依赖安装任务
        if project_type == "Java" and build_system == "Maven":
            plan.append({
                "id": "install_dependencies",
                "description": "Install Maven dependencies and verify build environment",
                "priority": "high", 
                "type": "dependencies"
            })
        elif project_type == "Java" and build_system == "Gradle":
            plan.append({
                "id": "install_dependencies",
                "description": "Install Gradle dependencies and verify build environment",
                "priority": "high",
                "type": "dependencies"
            })
        elif project_type == "Node.js":
            plan.append({
                "id": "install_dependencies",
                "description": "Install Node.js dependencies using npm/yarn",
                "priority": "high",
                "type": "dependencies"
            })

        # 构建任务
        build_commands = documentation.get("build_commands", [])
        if build_commands:
            plan.append({
                "id": "build_project",
                "description": f"Build project using documented commands: {', '.join(build_commands[:2])}",
                "priority": "high",
                "type": "build"
            })
        elif project_type == "Java" and build_system == "Maven":
            plan.append({
                "id": "build_project", 
                "description": "Compile project using Maven",
                "priority": "high",
                "type": "build"
            })
        elif project_type == "Java" and build_system == "Gradle":
            plan.append({
                "id": "build_project",
                "description": "Compile project using Gradle",
                "priority": "high",
                "type": "build"
            })

        # 测试任务
        test_framework = analysis.get("test_framework", "unknown")
        test_commands = documentation.get("test_commands", [])
        if test_commands:
            plan.append({
                "id": "run_tests",
                "description": f"Run tests using documented commands: {', '.join(test_commands[:2])}",
                "priority": "high",
                "type": "test"
            })
        elif project_type == "Java" and build_system == "Maven":
            test_desc = "Execute project tests using Maven"
            if test_framework != "unknown":
                test_desc += f" (detected: {test_framework})"
            plan.append({
                "id": "run_tests",
                "description": test_desc,
                "priority": "high",
                "type": "test"
            })
        elif project_type == "Java" and build_system == "Gradle":
            test_desc = "Execute project tests using Gradle"
            if test_framework != "unknown":
                test_desc += f" (detected: {test_framework})"
            plan.append({
                "id": "run_tests",
                "description": test_desc,
                "priority": "high",
                "type": "test"
            })

        # 最终报告任务（确保始终存在）
        plan.append({
            "id": "generate_completion_report",
            "description": "Generate comprehensive setup completion report",
            "priority": "high",
            "type": "report"
        })

        return plan

    def _update_trunk_context_with_plan(self, analysis: Dict[str, Any]) -> bool:
        """更新trunk context的todo list"""
        if not self.context_manager:
            return False

        try:
            trunk_context = self.context_manager.load_trunk_context()
            if not trunk_context:
                logger.error("No trunk context found to update")
                return False

            execution_plan = analysis.get("execution_plan", [])
            if not execution_plan:
                logger.warning("No execution plan generated")
                return False

            # 清除现有的pending任务（保留已完成的）
            remaining_tasks = [task for task in trunk_context.todo_list if task.status.value != "pending"]
            trunk_context.todo_list = remaining_tasks

            # 添加新的智能任务
            for plan_item in execution_plan:
                task_description = plan_item.get("description", "Unknown task")
                trunk_context.add_task(task_description)

            # 保存更新后的context
            self.context_manager._save_trunk_context(trunk_context)
            logger.info(f"Successfully updated trunk context with {len(execution_plan)} new tasks")
            return True

        except Exception as e:
            logger.error(f"Failed to update trunk context: {e}")
            return False

    def _format_analysis_output(self, analysis: Dict[str, Any]) -> str:
        """格式化分析输出"""
        output = "🔍 PROJECT ANALYSIS COMPLETED\n\n"
        
        # 基本信息
        output += f"📂 Project Type: {analysis.get('project_type', 'Unknown')}\n"
        output += f"🔧 Build System: {analysis.get('build_system', 'Unknown')}\n"
        
        if analysis.get('java_version'):
            output += f"☕ Java Version: {analysis['java_version']}\n"
        
        # 文档分析
        doc = analysis.get('documentation', {})
        if doc.get('java_version_requirement'):
            output += f"📋 Required Java Version: {doc['java_version_requirement']}\n"
        
        if doc.get('build_commands'):
            output += f"🔨 Build Commands Found: {', '.join(doc['build_commands'][:3])}\n"
        
        if doc.get('test_commands'):
            output += f"🧪 Test Commands Found: {', '.join(doc['test_commands'][:3])}\n"
        
        # 执行计划
        execution_plan = analysis.get('execution_plan', [])
        if execution_plan:
            output += f"\n📋 GENERATED EXECUTION PLAN ({len(execution_plan)} tasks):\n"
            for i, task in enumerate(execution_plan, 1):
                output += f"  {i}. {task.get('description', 'Unknown task')}\n"
        
        # Context更新状态
        if analysis.get('context_updated'):
            output += f"\n✅ Trunk context updated with new intelligent task plan\n"
        elif 'context_error' in analysis:
            output += f"\n⚠️ Context update failed: {analysis['context_error']}\n"
        
        output += f"\n🎯 Ready to execute intelligent project setup plan!"
        
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

WORKFLOW:
1. First clone the repository using project_setup tool
2. Then use project_analyzer to understand the project and generate intelligent plan
3. Execute the dynamically generated tasks

WHAT IT ANALYZES:
- Project type (Java, Node.js, Python, etc.)
- Build system (Maven, Gradle, npm, etc.)
- Java version requirements from README and config files
- Maven dependencies from pom.xml
- Gradle dependencies from build.gradle/build.gradle.kts (supports both Groovy and Kotlin DSL)
- Test frameworks (JUnit, TestNG, Spock) for both Maven and Gradle projects
- Gradle plugins and Maven profiles
- Documentation and setup instructions
- Generates optimized execution plan based on findings

OUTPUT:
- Comprehensive project analysis
- Dynamically generated task list optimized for the specific project
- Updates trunk context with intelligent execution plan
""" 