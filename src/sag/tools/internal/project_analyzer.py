"""Project analyzer tool for intelligent project setup planning."""

import json
import re
from typing import Any, Dict, List, Optional

from loguru import logger

from sag.testcases.catalog import (
    STATIC_SCAN_EXCLUSION_HELPER,
    TestCaseCatalog,
    build_java_test_catalog,
)

from ..base import BaseTool, ToolResult

# Enforcer version accepts range syntax ([1.8,), [11,17)); capture the lower
# bound including a legacy "1.x" form (the old \d+ captured "1" from "1.8").
ENFORCER_JAVA_PATTERN = r"<requireJavaVersion>.*?<version>\s*\[?\s*(\d+(?:\.\d+)?)"
PROJECT_ANALYZER_VERSION = "project-analyzer-v1"


def _normalize_java_version(raw) -> Optional[str]:
    """Normalize a detected Java version to a plain major string, or None.

    Rejects unresolved property indirection (``${...}``) and non-numeric
    junk; maps legacy ``1.x`` to ``x`` (1.8 -> 8).
    """
    if not raw:
        return None
    value = str(raw).strip()
    if not value or "${" in value:
        return None
    if value.startswith("1.") and value[2:].isdigit():
        return value[2:]
    if value.isdigit():
        return value
    return None


# Bumped when the survey's fact semantics change: an older-version manifest is
# re-surveyed instead of reused (review 2026-07-19: existence-as-no-op would
# happily serve stale facts across analyzer upgrades).
SURVEY_FACTS_VERSION = 1


def _path_exists(orch, path: str) -> bool:
    result = orch.execute_command(f"test -e {path} && echo yes || echo no")
    return "yes" in (result.get("output") or "")


# Subdirectories a python-primary repo conventionally uses to hold the real
# installable python package when the repo ROOT is a build shell (native-core
# projects such as TVM: root CMakeLists.txt + python/setup.py). Order is the
# search order — the first that ships its own setup.py/pyproject.toml wins.
_PYTHON_SUBDIR_CANDIDATES = ("python", "bindings/python")


def _root_has_installable_package(root_files: set, root_pyproject: str) -> bool:
    """True when the repo ROOT itself declares an installable python package.

    Established POSITIVELY, not inferred from a bracket-fragile deps regex:

      * ``setup.py`` at the root — a classic (requirements.txt +) setup.py
        package; OR
      * ``setup.cfg`` at the root — a declarative setuptools package (setup.py
        is often a one-line shim or absent); OR
      * ``pyproject.toml`` that names a package — a ``[project]`` table with a
        ``name`` (PEP 621) or a ``[tool.poetry]`` table. This uses the same
        section-scoped parser as package discovery, so it is immune to the
        ``[`` characters in ``authors``/``classifiers``/``keywords`` arrays (the
        standard modern ordering, which this repo's own pyproject uses),
        recognizes Poetry roots (deps under ``[tool.poetry.dependencies]``,
        no ``[project]`` table), and recognizes ``dynamic = ["dependencies"]``
        packages (deps resolved by the backend, still a real root package).

    A bare PEP-517 build-shell pyproject (only ``[build-system]`` /
    ``[build-backend]``, no package name) is NOT a root package — that is the
    TVM shape whose real package lives under ``python/``.
    """
    from .python_env import project_name_from_pyproject

    if "setup.py" in root_files or "setup.cfg" in root_files:
        return True
    return project_name_from_pyproject(root_pyproject or "") is not None


def detect_python_package_root(
    orch,
    project_path: str,
    root_files: set,
    root_pyproject: str,
) -> Dict[str, Any]:
    """Where the REAL python package lives, and whether a native core precedes it.

    Live TVM regression (session 20260713_014403): the repo root carried a
    ``CMakeLists.txt`` (native ``libtvm.so``) and a build-shell pyproject with
    no root package, while the actual installable python package lived in
    ``python/`` (``python/setup.py``). A root ``pip install -e .`` therefore
    targeted the wrong thing, and nothing said the native library had to be
    built first.

    Detection is GUIDANCE-level and conservative — it only redirects the python
    root when BOTH hold:

      * the root ships NO installable package of its own — no root
        ``setup.py``/``setup.cfg`` and no package-naming ``pyproject.toml``
        (``[project]`` name or ``[tool.poetry]``); see
        ``_root_has_installable_package``. Package-less-ness is established
        POSITIVELY, so a real root package with the standard modern pyproject
        ordering (``authors``/``classifiers`` before ``dependencies``), a
        Poetry root, a ``dynamic = ["dependencies"]`` root, or a plain
        setup.py/setup.cfg root is never mistaken for a shell and redirected —
        the mirror image of the TVM bug. AND
      * a conventional subdirectory (``python/`` or ``bindings/python/``) ships
        its OWN ``setup.py``/``pyproject.toml`` (a real package there).

    Returns ``{"python_root": <dir>, "has_native_build": <bool>}``. When no
    subdir package is found the python_root stays the repo root (a plain-python
    repo is byte-identical to before). ``has_native_build`` is True purely on a
    root-level ``CMakeLists.txt`` — the native core the python package needs
    built first — independent of whether the root redirected.
    """
    root = project_path.rstrip("/")
    has_native_build = "CMakeLists.txt" in root_files
    root_is_shell = not _root_has_installable_package(root_files, root_pyproject)

    python_root = root
    if root_is_shell:
        for candidate in _PYTHON_SUBDIR_CANDIDATES:
            sub = f"{root}/{candidate}"
            if _path_exists(orch, f"{sub}/setup.py") or _path_exists(orch, f"{sub}/pyproject.toml"):
                python_root = sub
                break

    return {"python_root": python_root, "has_native_build": has_native_build}


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
        self._java_annotation_cache: Dict[str, Dict[str, int]] = {}

    def ensure_facts(self, project_path: str = "/workspace") -> str:
        """Framework-owned survey guarantee: compute + persist the machine
        facts (manifest, trunk env metrics).

        Eight mechanical readers (preflight, build tools, gates, finalizer)
        depend on the manifest, but it was written only when the agent chose
        to call ``project(action='analyze')`` — live 2026-07-13 pyyaml: the
        agent skipped analyze and the install chain starved. The engine calls
        this at build/test entry; zero LLM tokens (container commands only).
        Never raises.

        Returns ``"created"`` only after (a) the trunk env metrics saved and
        (b) the re-read manifest carries THIS survey's stamp (version AND
        project path — a stale file left on disk keeps the readback non-empty
        when a replacement write is dropped); ``"present"`` for an agent-era
        stampless manifest or a current-version same-project stamp;
        ``"failed"`` otherwise. Older-version or other-project stamps
        re-survey.
        """
        orchestrator = getattr(self, "docker_orchestrator", None) or getattr(
            self, "orchestrator", None
        )
        if orchestrator is None:
            return "failed"
        try:
            from .build_preflight import read_build_requirements

            existing = read_build_requirements(orchestrator) or {}
            existing_stamp = (existing.get("survey") or {}) if existing else {}
            if existing and not existing_stamp:
                # Agent-era manifest (pre-stamp): still authoritative — the
                # zero-behavior-change promise when the agent DID analyze.
                return "present"

            validated = self._validate_and_discover_project_path(project_path)
            if not validated:
                return "failed"
            if (
                existing_stamp.get("analyzer_version") == SURVEY_FACTS_VERSION
                and existing_stamp.get("project_path") == validated
            ):
                # Current survey for THIS project (re-review 2026-07-19: a
                # same-version manifest from another workspace project must
                # not pass as present).
                return "present"

            analysis = self._perform_comprehensive_analysis(validated)
            if not self._is_analysis_valid(analysis):
                return "failed"
            if self.context_manager is not None:
                # The guarantee is manifest AND trunk env metrics — a stale
                # env would still pick the wrong phase objective (re-review
                # 2026-07-19: ignoring this return let 'created' stand over a
                # failed trunk save).
                if not self._update_trunk_context_with_plan(analysis):
                    return "failed"
            # Success is what the READERS can see: the re-read manifest must
            # carry THIS survey's stamp — a stale file left on disk keeps the
            # readback non-empty even when the replacement write was dropped
            # (re-review 2026-07-19).
            persisted = (read_build_requirements(orchestrator) or {}).get("survey") or {}
            if (
                persisted.get("analyzer_version") != SURVEY_FACTS_VERSION
                or persisted.get("project_path") != validated
            ):
                return "failed"
            return "created"
        except Exception as exc:
            logger.warning(f"framework survey failed: {exc}")
            return "failed"

    def execute(
        self,
        action: str = "analyze",
        project_path: str = "/workspace",
        update_context: bool = True,
        **kwargs,
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
            return ToolResult.completed_failure(
                output=(
                    f"❌ Invalid parameters for project analysis: {invalid_params}\n\n"
                    f"✅ Valid parameters:\n"
                    f"  - action (optional): 'analyze' (default: 'analyze')\n"
                    f"  - project_path (optional): Path to project directory (default: '/workspace')\n"
                    f"  - update_context (optional): Update trunk context (default: True)\n\n"
                    f"Example: project(action='analyze', project_path='/workspace/myproject')\n"
                    f"Example: project(action='analyze')"  # Uses all defaults
                ),
                error=f"Invalid parameters: {invalid_params}",
            )

        logger.info(f"Starting project analysis at: {project_path}")

        try:
            if action == "analyze":
                # Step 1: Validate and discover project path
                validated_path = self._validate_and_discover_project_path(project_path)
                if not validated_path:
                    return ToolResult.completed_failure(
                        output="",
                        error=f"No valid project found at {project_path} or in common subdirectories",
                        suggestions=[
                            "Ensure the project has been cloned successfully",
                            "Check if the project contains build files (pom.xml, build.gradle, package.json, etc.)",
                            "Try specifying the exact project directory path",
                            "Use bash tool to list directory contents: bash(command='ls -la /workspace')",
                        ],
                        error_code="PROJECT_NOT_FOUND",
                    )

                logger.info(f"✅ Using validated project path: {validated_path}")

                # Step 2: Perform comprehensive analysis
                analysis_result = self._perform_comprehensive_analysis(validated_path)

                # Step 3: Validate analysis results
                if not self._is_analysis_valid(analysis_result):
                    return ToolResult.completed_failure(
                        output="",
                        error="Project analysis failed to detect valid project structure",
                        suggestions=[
                            "Verify the project is properly structured",
                            "Check if build files are accessible",
                            "Ensure the project directory is correct",
                            "Try manual analysis with bash tool",
                        ],
                        error_code="ANALYSIS_FAILED",
                    )

                # Step 4: Update context if requested
                if update_context and self.context_manager:
                    success = self._update_trunk_context_with_plan(analysis_result)
                    if success:
                        analysis_result["context_updated"] = True
                    else:
                        analysis_result["context_updated"] = False
                        analysis_result["context_error"] = "Failed to update trunk context"

                return ToolResult.completed_success(
                    output=self._format_analysis_output(analysis_result),
                    metadata=analysis_result,
                )
            else:
                return ToolResult.completed_failure(
                    output=(
                        f"❌ Invalid action for project analysis: '{action}'\n\n"
                        f"✅ Valid actions:\n"
                        f"  - analyze: Perform comprehensive project analysis and generate setup plan\n\n"
                        f"Examples:\n"
                        f"  project(action='analyze')\n"
                        f"  project(action='analyze', project_path='/workspace/myproject')"
                    ),
                    error=f"Invalid action: {action}",
                    suggestions=[
                        "Use action='analyze' to perform comprehensive project analysis",
                        "Check the tool documentation for valid actions",
                    ],
                )

        except Exception as e:
            logger.error(f"Failed to analyze project: {e}")
            return ToolResult.completed_failure(
                output="",
                error=f"Project analysis failed: {str(e)}",
                suggestions=[
                    "Check if project is properly cloned and accessible",
                    "Verify Docker container has access to the project directory",
                    "Try using bash tool to manually inspect the project structure",
                ],
                error_code="ANALYSIS_EXCEPTION",
            )

    def _perform_comprehensive_analysis(self, project_path: str) -> Dict[str, Any]:
        """Perform comprehensive project analysis."""
        analysis = {
            "analyzer_version": PROJECT_ANALYZER_VERSION,
            "project_path": project_path,
            "project_type": "unknown",
            "build_system": "unknown",
            "java_version": None,
            "dependencies": [],
            "test_framework": "unknown",
            "documentation": {},
            "special_requirements": [],
            "execution_plan": [],
            "static_test_count": None,  # Add static test count field
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

        # Step 4.5: Build test catalog for Java projects
        # This provides structured test discovery with full metadata
        if analysis["project_type"] == "Java":
            test_catalog = build_java_test_catalog(project_path, self.docker_orchestrator)
            test_count = test_catalog.count()

            if test_count > 0:
                # Store catalog and metrics
                analysis["test_catalog"] = test_catalog.to_dict()
                analysis["static_test_count"] = test_count
                analysis["method_count"] = test_count  # For now, same as static count
                analysis["test_count_method"] = "catalog_based_discovery"

                # Extract module information if multi-module
                by_module = test_catalog.to_dict()["by_module"]
                if by_module:
                    analysis["test_modules"] = by_module

                logger.info(f"📊 Test catalog built:")
                logger.info(f"   - Total test methods discovered: {test_count}")
                if by_module and len(by_module) > 1:
                    logger.info(f"   - Multi-module distribution: {by_module}")

                # For backward compatibility, still get annotation counts
                test_count_result = self._count_java_test_with_expansions(project_path)
                if test_count_result.get("parameterized_info"):
                    analysis["parameterized_info"] = test_count_result.get("parameterized_info", {})
            else:
                logger.debug("No test methods discovered in Java project")

        # Step 4.6: Recommend where/how to build so the build phase targets the
        # real reactor/module instead of an empty aggregator root.
        try:
            analysis["build_recommendation"] = self._recommend_build_approach(
                project_path, analysis
            )
            # Tests can live in different modules / a different build system than
            # the main build (Bigtop: Maven build module, Gradle test modules).
            self._recommend_test_approach(project_path, analysis["build_recommendation"])
            # Persist the phase-1 -> build-tool handoff into the container so
            # MavenTool/GradleTool (which only hold an orchestrator) can run
            # the JDK pre-flight against the analyzed requirements.
            self._persist_build_requirements(project_path, analysis)
        except Exception as exc:
            logger.warning(f"Build-approach recommendation failed: {exc}")

        # Step 5: 生成智能执行计划
        execution_plan = self._generate_execution_plan(analysis)
        analysis["execution_plan"] = execution_plan

        # One deterministic role-typed composition pass owns all build/test
        # planner guidance. Persistence failure is evidence, not a reason to
        # discard an otherwise valid project analysis.
        try:
            self._compose_project_brief(project_path, analysis)
        except Exception as exc:
            analysis["project_brief_error"] = type(exc).__name__
            logger.warning(f"Project brief composition failed: {exc}")

        return analysis

    def _analyze_project_structure(self, project_path: str) -> Dict[str, Any]:
        """分析项目结构，检测项目类型和构建系统"""
        if not self.docker_orchestrator:
            return {"project_type": "unknown", "build_system": "unknown"}

        # 检查关键文件存在性
        files_to_check = [
            "pom.xml",  # Maven
            "build.gradle",  # Gradle (Groovy DSL)
            "build.gradle.kts",  # Gradle (Kotlin DSL — e.g. apache/beam root)
            "settings.gradle",  # Gradle multi-project marker
            "settings.gradle.kts",  # Gradle multi-project marker (Kotlin DSL)
            "gradlew",  # Gradle wrapper — strong gradle signal even without root build file
            "package.json",  # Node.js
            "requirements.txt",  # Python
            "pyproject.toml",  # Python Poetry
            "Cargo.toml",  # Rust
            "go.mod",  # Go
            "CMakeLists.txt",  # CMake
            "Makefile",  # Make
            "README.md",
            "README.txt",
            "README",
        ]

        existing_files = []
        for file in files_to_check:
            result = self.docker_orchestrator.execute_command(
                f"test -f {project_path}/{file} && echo 'exists' || echo 'missing'"
            )
            if result.get("success") and "exists" in result.get("output", ""):
                existing_files.append(file)

        gradle_markers = (
            "build.gradle",
            "build.gradle.kts",
            "settings.gradle",
            "settings.gradle.kts",
            "gradlew",
        )

        # 检测项目类型
        project_type = "unknown"
        build_system = "unknown"

        if "pom.xml" in existing_files:
            project_type = "Java"
            build_system = "Maven"
        elif any(marker in existing_files for marker in gradle_markers):
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
        elif "CMakeLists.txt" in existing_files and self._python_subdir_package(project_path):
            # Native-core python repo (live TVM): the root is a CMake build shell
            # with NO root python marker, but the real installable python package
            # lives in python/ (or bindings/python/). Classify as Python so the
            # python analysis + native-first guidance run — this branch is reached
            # ONLY after every root marker above missed, so it can never reclassify
            # a Java/Node/Rust/Go repo, and it requires an actual subdir package.
            project_type = "Python"
            build_system = "pip/poetry"

        logger.info(f"Detected project type: {project_type}, build system: {build_system}")

        structure = {
            "project_type": project_type,
            "build_system": build_system,
            "existing_files": existing_files,
        }

        # An "unknown" verdict must carry its evidence: which markers were
        # checked and what the project root actually contains — so the model
        # can see WHY detection failed and correct course, instead of
        # receiving a bare authoritative "unknown".
        if project_type == "unknown":
            structure["detection_checked"] = [
                f for f in files_to_check if not f.startswith("README")
            ]
            listing = self.docker_orchestrator.execute_command(
                f"ls -1 {project_path} 2>/dev/null | head -30"
            )
            if listing.get("success"):
                structure["root_listing"] = (listing.get("output") or "").strip()

        return structure

    def _python_subdir_package(self, project_path: str) -> bool:
        """True when a conventional python subdir ships its own package metadata.

        Native-core repos (TVM) keep the installable python package in
        ``python/`` (or ``bindings/python/``) beside a CMake build shell at the
        root. Used only as the LAST classification fallback — a CMake root with
        no root python marker is Python iff such a subdir package exists."""
        orch = self.docker_orchestrator
        if not orch:
            return False
        root = project_path.rstrip("/")
        for candidate in _PYTHON_SUBDIR_CANDIDATES:
            sub = f"{root}/{candidate}"
            if _path_exists(orch, f"{sub}/setup.py") or _path_exists(orch, f"{sub}/pyproject.toml"):
                return True
        return False

    def _analyze_documentation(self, project_path: str) -> Dict[str, Any]:
        """分析项目文档，提取关键信息"""
        documentation = {
            "source_path": None,
            "readme_content": "",
            "setup_instructions": [],
            "build_commands": [],
            "test_commands": [],
            "requirements": [],
            "java_version_requirement": None,
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
                documentation["source_path"] = readme_file
                logger.info(f"Successfully read {readme_file}")
                break

        documentation["readme_content"] = readme_content

        if readme_content:
            # 提取 Java 版本要求
            java_patterns = [
                r"Java\s+(\d+)",
                r"JDK\s+(\d+)",
                r"java\.version.*?(\d+)",
                r"requires.*Java\s+(\d+)",
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
                r"python setup\.py",
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
                r"python.*?test",
            ]

            for pattern in test_patterns:
                matches = re.findall(pattern, readme_content, re.IGNORECASE)
                # 清理提取的命令
                for match in matches:
                    clean_cmd = self._clean_markdown_command(match)
                    # Filter out invalid test commands
                    if clean_cmd and clean_cmd not in documentation["test_commands"]:
                        # Skip commands with -Dtest without a value (invalid Maven syntax)
                        if "-Dtest" in clean_cmd and not re.search(r"-Dtest=\S+", clean_cmd):
                            # Fix the command by removing invalid -Dtest
                            clean_cmd = clean_cmd.replace("-Dtest", "").strip()
                            # If it becomes just 'mvn clean install', change to 'mvn clean test'
                            if clean_cmd == "mvn clean install -Dossindex.skip":
                                clean_cmd = "mvn clean test -Dossindex.skip"
                        documentation["test_commands"].append(clean_cmd)

        return documentation

    def _clean_markdown_command(self, command: str) -> str:
        """清理从markdown中提取的命令，移除格式化字符"""
        if not command:
            return ""

        clean_cmd = command.strip()

        # 移除markdown代码块标记
        clean_cmd = re.sub(r"^```[a-z]*\s*", "", clean_cmd)  # 移除开始的```bash等
        clean_cmd = re.sub(r"\s*```$", "", clean_cmd)  # 移除结束的```

        # 移除反引号
        clean_cmd = re.sub(r"^`+|`+$", "", clean_cmd)  # 移除首尾反引号

        # 移除shell提示符
        clean_cmd = re.sub(r"^[>$#]\s*", "", clean_cmd)  # 移除常见的shell提示符

        # 移除多余的空白字符
        clean_cmd = " ".join(clean_cmd.split())

        # 如果命令被截断或包含省略号，标记为需要验证
        if "..." in clean_cmd or clean_cmd.endswith("."):
            # 移除省略号
            clean_cmd = clean_cmd.replace("...", "").rstrip(".")

        return clean_cmd.strip()

    def _analyze_build_configuration(self, project_path: str, project_type: str) -> Dict[str, Any]:
        """分析构建配置文件"""
        config = {
            "java_version": None,
            "dependencies": [],
            "plugins": [],
            "profiles": [],
            "build_system": None,
        }

        if not self.docker_orchestrator:
            return config

        if project_type == "Java":
            # 首先检查是Maven还是Gradle项目
            maven_exists = self.docker_orchestrator.execute_command(
                f"test -f {project_path}/pom.xml && echo 'exists'"
            )
            gradle_exists = self.docker_orchestrator.execute_command(
                f"test -f {project_path}/build.gradle && echo 'exists'"
            )
            gradle_kts_exists = self.docker_orchestrator.execute_command(
                f"test -f {project_path}/build.gradle.kts && echo 'exists'"
            )

            if maven_exists.get("success") and "exists" in maven_exists.get("output", ""):
                config["build_system"] = "Maven"
                self._analyze_maven_configuration(project_path, config)
            elif (gradle_exists.get("success") and "exists" in gradle_exists.get("output", "")) or (
                gradle_kts_exists.get("success") and "exists" in gradle_kts_exists.get("output", "")
            ):
                config["build_system"] = "Gradle"
                self._analyze_gradle_configuration(project_path, config)
        elif project_type == "Python":
            # Keep the structure-detection label (this dict overwrites the
            # analysis via update(), so a None here would erase it) and add
            # the Python analysis depth (spec Component 1).
            config["build_system"] = "pip/poetry"
            self._analyze_python_project(project_path, config)

        return config

    def _analyze_python_project(self, project_path: str, analysis: Dict[str, Any]) -> None:
        """Python analysis depth (spec Component 1): interpreter constraint ->
        concrete version (newest satisfying), installer faithfulness ladder,
        top-level package discovery, and READ-ONLY test hints (tox/nox are
        metadata only, never executed). Fills ``analysis["python_config"]``,
        which _persist_build_requirements merges into the handoff manifest.
        """
        from .python_env import (
            detect_installer,
            discover_packages,
            requires_python_from_pyproject,
            requires_python_from_setup_cfg,
            requires_python_from_setup_py,
            resolve_python_version,
            setup_cfg_test_deps,
            tox_test_hints,
        )

        orch = self.docker_orchestrator
        if not orch:
            return

        def list_dir(directory: str) -> set:
            listing = orch.execute_command(f"ls -1 {directory} 2>/dev/null")
            return {
                line.strip() for line in (listing.get("output") or "").splitlines() if line.strip()
            }

        def read_from(directory: str, name: str, present: set) -> str:
            if name not in present:
                return ""
            # Untruncated like the pom reads: this content is parsed
            # internally by regex and never reaches the model's context.
            result = orch.execute_command(f"cat {directory}/{name}", truncate_output=False)
            return result.get("output", "") if result.get("success") else ""

        # Native-core detection (live TVM regression): when the repo ROOT is a
        # build shell (root CMakeLists.txt, or a pyproject with no [project]
        # deps) and the real python package lives in python/ (or
        # bindings/python/), redirect ALL python analysis to that subdir root —
        # constraint/installer/C-extension parsing, package discovery, and the
        # venv path — so the recommendation and manifest target the package that
        # actually installs, not the CMake shell. has_native_build rides along.
        root_files = list_dir(project_path)
        root_pyproject = read_from(project_path, "pyproject.toml", root_files)
        native = detect_python_package_root(orch, project_path, root_files, root_pyproject)
        python_root = native["python_root"]
        has_native_build = native["has_native_build"]

        # All metadata reads now come from the DETECTED python root (identical to
        # the repo root for a plain-python project).
        files_present = root_files if python_root == project_path else list_dir(python_root)

        def read(name: str) -> str:
            return read_from(python_root, name, files_present)

        pyproject = read("pyproject.toml")
        setup_py = read("setup.py")
        setup_cfg = read("setup.cfg")
        tox_ini = read("tox.ini")

        # Constraint precedence mirrors packaging reality: pyproject is
        # authoritative when present, setup.py/setup.cfg are the legacy forms.
        constraint = None
        constraint_source = None
        for source, value in (
            ("pyproject.toml", requires_python_from_pyproject(pyproject)),
            ("setup.py", requires_python_from_setup_py(setup_py)),
            ("setup.cfg", requires_python_from_setup_cfg(setup_cfg)),
        ):
            if value:
                constraint, constraint_source = value, source
                break

        # Bug #13 defect 3: the editable pip rungs install the extras the
        # project ACTUALLY declares — pass the metadata contents through.
        installer = detect_installer(
            files_present,
            {"pyproject.toml": pyproject, "setup.cfg": setup_cfg},
        )

        hints = tox_test_hints(tox_ini)
        for dep in setup_cfg_test_deps(setup_cfg):
            if dep not in hints["test_deps"]:
                hints["test_deps"].append(dep)

        # C-extension markers: ext_modules in setup.py, the [tool.setuptools]
        # ext-modules table in pyproject, or cython anywhere in either. The
        # bare [tool.setuptools] table is NOT a marker — every modern
        # setuptools project has one, and flagging it would demand .so
        # evidence from pure-Python builds.
        has_c_extensions = bool(
            re.search(r"\bext_modules\b", setup_py)
            or re.search(r"\bext[-_]modules\b", pyproject)
            or re.search(r"(?i)\bcython\b", pyproject + setup_py)
        )

        analysis["python_config"] = {
            "python_constraint": constraint,
            "python_constraint_source": constraint_source,
            "python_version": resolve_python_version(constraint),
            "python_installer": installer["installer"],
            "python_install_commands": installer["commands"],
            "python_install_source": installer["source"],
            # Bug #13 defect 3: no-test-extras rides the manifest so
            # setup_env narrates the hole instead of failing silently.
            "python_install_note": installer.get("note"),
            "python_packages": discover_packages(orch, python_root),
            "python_venv": f"{python_root.rstrip('/')}/.venv",
            "has_c_extensions": has_c_extensions,
            # The directory the python package actually installs from (the repo
            # root for a plain project; a python/ subdir for a native-core repo)
            # and whether a native library must be built before it imports.
            "python_root": python_root,
            "has_native_build": has_native_build,
            "test_hints": hints,
        }

    def _analyze_maven_configuration(self, project_path: str, config: Dict[str, Any]):
        """分析Maven配置（pom.xml）- 包括多模块项目和父POM"""
        # First, read the main pom.xml. Read it UNTRUNCATED: the default XML-aware
        # truncation protects the model's context window, but this content is parsed
        # internally by regex (java version, <modules>, <packaging>, dependencies) and
        # never reaches the model. Truncation drops <modules>/enforcer blocks on large
        # poms (httpcomponents-client: <modules> at line 260), which mis-scoped builds.
        result = self.docker_orchestrator.execute_command(
            f"cat {project_path}/pom.xml", truncate_output=False
        )
        if not result.get("success"):
            return

        main_pom_content = result.get("output", "")

        # Check if this is a multi-module project and look for parent POMs
        all_pom_contents = [main_pom_content]
        pom_locations = [f"{project_path}/pom.xml"]

        # Check for parent module reference (e.g., tika-parent)
        parent_match = re.search(
            r"<parent>.*?<artifactId>([^<]+)</artifactId>.*?</parent>", main_pom_content, re.DOTALL
        )
        if parent_match:
            parent_artifact = parent_match.group(1)
            # Try to find the parent POM in common locations
            potential_parent_paths = [
                f"{project_path}/{parent_artifact}/pom.xml",
                f"{project_path}/../{parent_artifact}/pom.xml",
                f"{project_path}/parent/pom.xml",
            ]

            for parent_path in potential_parent_paths:
                # First check if parent POM exists
                check_result = self.docker_orchestrator.execute_command(
                    f"test -f {parent_path} && echo 'exists' 2>/dev/null"
                )
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
            enforcer_match = re.search(
                ENFORCER_JAVA_PATTERN, pom_content, re.DOTALL | re.IGNORECASE
            )
            if enforcer_match:
                normalized = _normalize_java_version(enforcer_match.group(1))
                if normalized:
                    java_version = normalized
                    java_version_source = "maven-enforcer"
                    java_version_enforced = True
                    logger.info(
                        f"Found Java version from Maven Enforcer in {pom_locations[idx]}: {java_version}"
                    )
                    break

            # 2. Check standard properties, then the maven-compiler-plugin
            # <configuration> form. Many poms (e.g. cassandra-java-driver) declare the
            # Java level only as <source>/<target>/<release> inside the compiler
            # plugin config rather than as maven.compiler.* properties; without this
            # the analyzer detects nothing and the wrong JDK gets provisioned.
            java_version_patterns = [
                r"<maven\.compiler\.release>([^<]+)</maven\.compiler\.release>",  # Highest priority
                r"<maven\.compiler\.target>([^<]+)</maven\.compiler\.target>",
                r"<maven\.compiler\.source>([^<]+)</maven\.compiler\.source>",
                r"<java\.version>([^<]+)</java\.version>",
                r"<release>\s*(1\.\d+|\d+)\s*</release>",  # compiler-plugin config
                r"<target>\s*(1\.\d+|\d+)\s*</target>",
                r"<source>\s*(1\.\d+|\d+)\s*</source>",
            ]

            for pattern in java_version_patterns:
                match = re.search(pattern, pom_content)
                if match:
                    normalized = _normalize_java_version(match.group(1))
                    if not normalized:
                        # Rejected capture (e.g. ${...} indirection): fall
                        # through to the next pattern instead of accepting it.
                        continue
                    java_version = normalized
                    java_version_source = "maven-compiler"
                    logger.info(
                        f"Found Java version from {pattern} in {pom_locations[idx]}: {java_version}"
                    )
                    break

        if java_version:
            config["java_version"] = java_version
            config["java_version_source"] = java_version_source
            config["java_version_enforced"] = java_version_enforced
        else:
            logger.warning(f"No Java version found in Maven configuration for {project_path}")

        # Check for multi-module project. The pom is read untruncated above, so the
        # <modules> block is intact even on large poms.
        modules_match = re.search(r"<modules>(.*?)</modules>", main_pom_content, re.DOTALL)
        if modules_match:
            modules = re.findall(r"<module>([^<]+)</module>", modules_match.group(1))
            config["maven_modules"] = modules
            config["is_multi_module"] = True
            logger.info(f"Found multi-module Maven project with {len(modules)} modules: {modules}")
        else:
            config["maven_modules"] = []
            config["is_multi_module"] = False

        # Extract dependencies from main POM only
        dependency_matches = re.findall(
            r"<groupId>([^<]+)</groupId>.*?<artifactId>([^<]+)</artifactId>",
            main_pom_content,
            re.DOTALL,
        )
        config["dependencies"] = [
            f"{group}:{artifact}" for group, artifact in dependency_matches[:10]
        ]  # 限制输出

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
            result = self.docker_orchestrator.execute_command(
                f"cat {project_path}/build.gradle.kts"
            )
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
                version = _normalize_java_version(match.group(1))
                if not version:
                    # Rejected capture: fall through to the next pattern.
                    continue
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
            "build_system": None,
        }

        if not self.docker_orchestrator:
            return test_config

        # 检查测试目录
        test_dirs = ["src/test", "test", "tests", "__tests__"]
        for test_dir in test_dirs:
            result = self.docker_orchestrator.execute_command(
                f"test -d {project_path}/{test_dir} && echo 'exists'"
            )
            if result.get("success") and "exists" in result.get("output", ""):
                test_config["test_directories"].append(test_dir)

        # 根据项目类型检测测试框架
        if project_type == "Java":
            # 检查是Maven还是Gradle项目
            maven_exists = self.docker_orchestrator.execute_command(
                f"test -f {project_path}/pom.xml && echo 'exists'"
            )
            gradle_exists = self.docker_orchestrator.execute_command(
                f"test -f {project_path}/build.gradle && echo 'exists'"
            )
            gradle_kts_exists = self.docker_orchestrator.execute_command(
                f"test -f {project_path}/build.gradle.kts && echo 'exists'"
            )

            if maven_exists.get("success") and "exists" in maven_exists.get("output", ""):
                test_config["build_system"] = "Maven"
                self._detect_maven_test_framework(project_path, test_config)
            elif (gradle_exists.get("success") and "exists" in gradle_exists.get("output", "")) or (
                gradle_kts_exists.get("success") and "exists" in gradle_kts_exists.get("output", "")
            ):
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
        result = self.docker_orchestrator.execute_command(
            f"grep -r 'testng' {project_path}/pom.xml"
        )
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
            result = self.docker_orchestrator.execute_command(
                f"cat {project_path}/build.gradle.kts"
            )
            if result.get("success"):
                gradle_content = result.get("output", "")

        if gradle_content:
            # 检测测试框架
            test_frameworks = self._parse_gradle_test_frameworks(gradle_content)
            if test_frameworks:
                test_config["test_framework"] = ", ".join(test_frameworks)
                logger.info(f"Found Gradle test frameworks: {test_frameworks}")

    def _estimate_total_test_cases(
        self, project_path: str, project_type: str, build_system: str
    ) -> Optional[int]:
        """(Deprecated) Test estimation disabled."""
        return None

    def _get_java_test_annotation_counts(self, project_path: str) -> Optional[Dict[str, int]]:
        """Collect counts for key JUnit annotations inside src/test/* Java sources."""
        if not self.docker_orchestrator:
            return None

        if project_path in self._java_annotation_cache:
            return self._java_annotation_cache[project_path]

        command = f"""cd {project_path} && python3 - <<'PY'
import json
import re
from collections import Counter
from pathlib import Path

{STATIC_SCAN_EXCLUSION_HELPER}

ANNOTATION_PATTERN = re.compile(r'@([A-Za-z_][A-Za-z0-9_]*)')


def strip_comments(source: str) -> str:
    source = re.sub(r'/\\*.*?\\*/', '', source, flags=re.S)
    source = re.sub(r'//.*', '', source)
    return source


counts = Counter()
project_root = Path('.')

test_dirs = []
for candidate in project_root.rglob('src'):
    if candidate.name != 'src':
        continue
    test_dir = candidate / 'test'
    if not test_dir.is_dir():
        continue
    if is_excluded(test_dir):
        continue
    test_dirs.append(test_dir)

for test_dir in test_dirs:
    for java_file in test_dir.rglob('*.java'):
        if is_excluded(java_file.parent):
            continue
        try:
            text = java_file.read_text(encoding='utf-8')
        except Exception:
            try:
                text = java_file.read_text(encoding='latin-1')
            except Exception:
                continue
        cleaned = strip_comments(text)
        counts.update(ANNOTATION_PATTERN.findall(cleaned))

result = {{
    'Test': counts.get('Test', 0),
    'ParameterizedTest': counts.get('ParameterizedTest', 0),
    'RepeatedTest': counts.get('RepeatedTest', 0),
    'TestFactory': counts.get('TestFactory', 0),
    'TestTemplate': counts.get('TestTemplate', 0),
    'DynamicTest': counts.get('DynamicTest', 0),
    'Disabled': counts.get('Disabled', 0),
}}
print(json.dumps(result))
PY"""

        response = self.docker_orchestrator.execute_command(command)
        if not response.get("success"):
            return None

        output = (response.get("output") or "").strip()
        if not output:
            return None

        try:
            counts = json.loads(output.splitlines()[-1])
        except json.JSONDecodeError:
            logger.debug("Unable to parse Java test annotation counts from output")
            return None

        self._java_annotation_cache[project_path] = counts
        return counts

    def _count_java_test_with_expansions(self, project_path: str) -> Dict[str, Any]:
        """
        Count Java test annotations and capture metadata about parameterized usage.

        Returns:
            Dict with:
            - 'method_count': Number of test method annotations
            - 'total_test_count': Total test cases based on annotations (deduplicated)
            - 'parameterized_info': Details about parameterized tests
        """
        if not self.docker_orchestrator:
            return {"method_count": None, "total_test_count": None}

        # Always calculate the raw annotation total first so we have a baseline
        # even if the per-annotation breakdown command fails.
        method_count = self._count_java_test_annotations(project_path)

        counts = self._get_java_test_annotation_counts(project_path)
        if counts is None and method_count is None:
            return {"method_count": None, "total_test_count": None}

        # When both approaches succeed use the scripted breakdown so we can
        # populate the parameterized metadata, but prefer the streaming grep
        # total as a guard against bugs in either implementation.
        if counts is None:
            counts = {
                "Test": 0,
                "ParameterizedTest": 0,
                "RepeatedTest": 0,
                "TestFactory": 0,
                "TestTemplate": 0,
                "DynamicTest": 0,
            }

        regular_tests = counts.get("Test", 0)
        parameterized_methods = counts.get("ParameterizedTest", 0)
        repeated_tests = counts.get("RepeatedTest", 0)
        factory_methods = counts.get("TestFactory", 0)
        template_methods = counts.get("TestTemplate", 0)
        dynamic_tests = counts.get("DynamicTest", 0)

        breakdown_total = (
            regular_tests
            + parameterized_methods
            + repeated_tests
            + factory_methods
            + template_methods
            + dynamic_tests
        )

        if method_count is None:
            method_count = breakdown_total
        elif breakdown_total and breakdown_total != method_count:
            logger.debug(
                "Mismatch between streaming annotation total ({}) and breakdown total ({})",
                method_count,
                breakdown_total,
            )
            method_count = max(method_count, breakdown_total)

        total_test_count = method_count

        result = {
            "method_count": method_count,
            "total_test_count": total_test_count,
            "parameterized_info": {
                "regular_tests": regular_tests,
                "parameterized_methods": parameterized_methods,
                "parameterized_expansions": parameterized_methods,
                "repeated_tests": repeated_tests,
                "test_factory_methods": factory_methods,
                "test_template_methods": template_methods,
                "dynamic_tests": dynamic_tests,
            },
        }

        logger.info("📊 Test count analysis:")
        logger.info(f"   - Regular @Test methods: {regular_tests}")
        logger.info(f"   - @ParameterizedTest methods: {parameterized_methods}")
        if repeated_tests:
            logger.info(f"   - @RepeatedTest methods: {repeated_tests}")
        if factory_methods or template_methods or dynamic_tests:
            logger.info(
                "   - Additional test annotations (factory/template/dynamic): "
                f"{factory_methods}/{template_methods}/{dynamic_tests}"
            )
        logger.info(f"   - Total annotated test methods: {method_count}")

        return result

    def _count_java_test_annotations(self, project_path: str) -> Optional[int]:
        """Count all test annotations across Java test sources for a project.

        Includes:
        - @Test (standard JUnit 4/5 tests)
        - @ParameterizedTest (JUnit 5 - runs multiple times with different parameters)
        - @RepeatedTest (JUnit 5 - runs multiple times)
        - @TestFactory (JUnit 5 - generates tests dynamically)
        - @TestTemplate (JUnit 5 - template for tests)

        Note: This counts test METHOD declarations, not test EXECUTIONS.
        Parameterized tests will execute multiple times but are counted once here.
        """
        if not self.docker_orchestrator:
            return None

        counts = self._get_java_test_annotation_counts(project_path)
        if counts is None:
            return None

        total = (
            counts.get("Test", 0)
            + counts.get("ParameterizedTest", 0)
            + counts.get("RepeatedTest", 0)
            + counts.get("TestFactory", 0)
            + counts.get("TestTemplate", 0)
            + counts.get("DynamicTest", 0)
        )

        if total > 0:
            logger.info(
                f"📊 Found {total} test method annotations (Test/Parameterized/Repeated/Factory/Template)."
            )
            param_methods = counts.get("ParameterizedTest", 0)
            if param_methods:
                logger.info(f"   - Includes {param_methods} parameterized test methods")

        return total if total > 0 else None

    def _count_actual_test_executions(self, project_path: str) -> Optional[int]:
        """Count actual test executions (including parameterized test expansions).

        This method attempts to get the true count of test cases that will execute,
        including all parameter variations of parameterized tests.

        Approaches:
        1. Check existing surefire-reports XML files for test counts
        2. Run tests with minimal overhead to generate reports
        3. Fall back to annotation counting if execution counting fails
        """
        if not self.docker_orchestrator:
            return None

        # First, try to get counts from existing test reports if available
        xml_count_cmd = (
            "if [ -d {project}/target/surefire-reports ]; then "
            "grep -h 'tests=' {project}/target/surefire-reports/TEST-*.xml 2>/dev/null | "
            "sed -n 's/.*tests=\"\\([0-9]*\\)\".*/\\1/p' | "
            "awk '{{sum += $1}} END {{if (sum > 0) print sum; else print \"0\"}}'; "
            "else echo '0'; fi"
        ).format(project=project_path)

        result = self.docker_orchestrator.execute_command(xml_count_cmd)
        if result.get("success"):
            output = (result.get("output") or "").strip()
            try:
                count = int(output)
                if count > 0:
                    logger.info(f"📊 Found {count} actual test executions from surefire reports")
                    return count
            except ValueError:
                pass

        # If no existing reports, try to run tests in list-only mode (if supported)
        # Note: This is a lightweight operation that discovers tests without executing them
        # However, JUnit 5's console launcher or Maven's test discovery might be needed

        # For now, we'll check if we can get a quick test count by running with failfast
        # This is still not ideal as it runs tests, but we limit the time
        discover_cmd = (
            "cd {project} && timeout 30 mvn test -DskipTests=false -Dmaven.test.failure.ignore=true "
            "-DtrimStackTrace=false 2>&1 | grep -E '^\\[INFO\\] Tests run: [0-9]+' | "
            "tail -1 | sed 's/.*Tests run: \\([0-9]*\\).*/\\1/'"
        ).format(project=project_path)

        # Actually, let's not run tests here - that's too expensive
        # Instead, return None to indicate we couldn't get actual execution count
        logger.debug("No existing test reports found, actual execution count unavailable")
        return None

    def _parse_gradle_test_frameworks(self, gradle_content: str) -> List[str]:
        """从Gradle配置中解析测试框架"""
        frameworks = []

        # JUnit 检测模式
        junit_patterns = [
            r"junit['\"]?\s*:\s*['\"]?[0-9]",  # junit: '5.8.2'
            r"['\"]junit['\"]",  # 'junit'
            r"org\.junit\.jupiter",  # JUnit 5
            r"junit-jupiter",  # JUnit 5
            r"junit-vintage",  # JUnit 4 via JUnit 5
            r"useJUnitPlatform\(\)",  # JUnit Platform configuration
        ]

        # TestNG 检测模式
        testng_patterns = [
            r"testng['\"]?\s*:\s*['\"]?[0-9]",  # testng: '7.4.0'
            r"['\"]testng['\"]",  # 'testng'
            r"org\.testng",  # TestNG package
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

    def _recommend_build_approach(
        self, project_path: str, analysis: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Recommend WHERE and HOW to build so the build phase does not compile an
        empty aggregator root.

        Bigtop's root pom is ``packaging=pom`` aggregating Groovy/Gradle modules,
        so ``mvn compile`` at the root returns BUILD SUCCESS with zero
        ``target/classes/*.class``. This inspects the real layout — root packaging,
        root/module main-source dirs (Java AND Groovy), and any Gradle build — and
        returns a concrete recommendation the build phase can target:

            {build_system, build_root, goal, is_aggregator_only, has_gradle,
             source_modules, rationale}
        """
        rec: Dict[str, Any] = {
            "build_system": analysis.get("build_system"),
            "build_root": project_path,
            "goal": "compile",
            "is_aggregator_only": False,
            "has_gradle": False,
            "source_modules": [],
            "rationale": "",
        }
        # Python project: a missing Java compile target is EXPECTED, never a
        # block signal. Make the recommendation REAL — live probes (paramiko,
        # pyyaml) showed an empty/None-build_system rec left the trunk's
        # environment_summary without any python signal, so the phase intros
        # carried neither the rec line nor the python guidance and agents
        # improvised (bash pip against the system python, blocked build
        # phases, unrun tests). Key off the same signal _analyze_python_project
        # produces (python_config), with the structure label as fallback, and
        # store the CANONICAL ecosystem label — the runtime phase intros key
        # their python guidance off rec["build_system"]
        # (react_engine._detected_build_system).
        python_config = analysis.get("python_config") or {}
        if python_config or str(analysis.get("project_type", "")).strip().lower() == "python":
            installer = python_config.get("python_installer") or "pip"
            # The real install target: a python/ subdir for a native-core repo
            # (TVM), the repo root for a plain project. python_root is set by
            # _analyze_python_project; fall back to the repo root when the
            # python branch did not run (label-only python signal).
            python_root = python_config.get("python_root") or project_path
            rec.update(
                build_system="python",
                build_root=python_root,
                goal="deps",
                test_root=python_root,
                test_system="pytest",
            )
            # Native-core flag rides the recommendation so the phase-intro
            # guidance can prepend the native-first block (build libtvm.so before
            # the python package can import). False/absent for plain projects.
            if python_config.get("has_native_build"):
                rec["has_native_build"] = True
            rec["rationale"] = (
                f"Python project ({installer}): create the venv and install "
                "with build(action='deps'), verify with build(action='compile'), "
                "test with build(action='test')."
            )
            return rec

        orch = self.docker_orchestrator
        if not orch:
            return rec

        has_pom = _path_exists(orch, f"{project_path}/pom.xml")
        has_gradlew = _path_exists(orch, f"{project_path}/gradlew")
        has_build_gradle = _path_exists(orch, f"{project_path}/build.gradle") or _path_exists(
            orch, f"{project_path}/build.gradle.kts"
        )
        rec["has_gradle"] = has_gradlew or has_build_gradle

        root_main_java = _path_exists(orch, f"{project_path}/src/main/java")
        root_main_groovy = _path_exists(orch, f"{project_path}/src/main/groovy")
        root_main_scala = _path_exists(orch, f"{project_path}/src/main/scala")
        root_main_kotlin = _path_exists(orch, f"{project_path}/src/main/kotlin")

        packaging = None
        if has_pom:
            pkg = orch.execute_command(f"grep -m1 '<packaging>' {project_path}/pom.xml 2>/dev/null")
            match = re.search(r"<packaging>\s*([^<\s]+)\s*</packaging>", pkg.get("output") or "")
            packaging = match.group(1).strip().lower() if match else "jar"

        # Find source-bearing modules DIRECTLY rather than trusting the root
        # pom's <modules> — Bigtop declares its modules inside a profile, so the
        # parsed list is empty and the Groovy iTest framework was missed. Scan for
        # Java, Groovy, Scala AND Kotlin main-source dirs, excluding build output.
        # (Live re-probe: bigpetstore-spark's only sources are src/main/scala with
        # its own build.gradle; a java/groovy-only find never enumerated it, so
        # the real archipelago produced 3 islands where the fixture had 4. Kotlin
        # is the same class of gap.)
        source_modules = []
        find_cmd = (
            f"find {project_path} -maxdepth 5 -type d "
            f"\\( -path '*/src/main/java' -o -path '*/src/main/groovy' "
            f"-o -path '*/src/main/scala' -o -path '*/src/main/kotlin' \\) "
            f"-not -path '*/target/*' -not -path '*/build/*' 2>/dev/null"
        )
        found = orch.execute_command(find_cmd)
        seen_dirs = set()
        for line in (found.get("output") or "").splitlines():
            line = line.strip()
            if not line or "/src/main/" not in line:
                continue
            suffix = line.rsplit("/src/main/", 1)[1]
            lang = suffix if suffix in ("groovy", "scala", "kotlin") else "java"
            module_dir = line.rsplit("/src/main/", 1)[0]
            if module_dir == project_path or module_dir in seen_dirs:
                continue
            seen_dirs.add(module_dir)
            source_modules.append(
                {
                    "module": module_dir[len(project_path) :].lstrip("/"),
                    "dir": module_dir,
                    "lang": lang,
                }
            )
        rec["source_modules"] = source_modules

        # 1) Plain Maven module with its own sources: compile at the root.
        if has_pom and (root_main_java or root_main_groovy or root_main_scala or root_main_kotlin):
            rec.update(build_system="maven", build_root=project_path, goal="compile")
            rec["rationale"] = "Root Maven module has main sources; compile at the root."
            return rec

        # 2) Aggregator root (packaging=pom): compiling the root produces nothing.
        if has_pom and packaging == "pom":
            groovy_modules = [m for m in source_modules if m["lang"] == "groovy"]
            if source_modules:
                # Groovy is compiled by a plugin bound to a later phase, so a bare
                # `compile` frequently yields no target/classes; `install` runs it.
                goal = "install" if groovy_modules else "compile"
                # If the root pom declares modules, the reactor builds them — build
                # at root. If it does NOT (Bigtop: profile-gated modules), building
                # the root compiles nothing, so target the source module directly.
                if analysis.get("maven_modules"):
                    build_root = project_path
                    scope = "the reactor at the root"
                    # Reactor modules can depend on siblings' produced artifacts
                    # (shaded jars, code-gen), not just their .class files — those
                    # exist only after a module is built and installed, which
                    # `compile` never does. Install so the test phase resolves them
                    # (cassandra-java-driver: core needs the shaded-guava jar).
                    goal = "install"
                else:
                    preferred = (groovy_modules or source_modules)[0]
                    build_root = preferred["dir"]
                    scope = f"module {preferred['module']} directly"
                    # PATHOLOGICAL-AGGREGATOR PATH ONLY: this repo is an
                    # archipelago (Bigtop: a maven island + several INDEPENDENT
                    # gradle islands, each with real sources). Picking ONE
                    # preferred module leaves the others UNKNOWN (live evidence:
                    # bigpetstore-spark + bigpetstore-transaction-queue never
                    # built). Enumerate EVERY independent island so the agent's
                    # guidance can cover each. build_root stays = island #1 for
                    # backward compatibility; the recommendation is guidance,
                    # not orchestration — the agent remains in charge.
                    rec["build_islands"] = self._enumerate_build_islands(
                        project_path, source_modules, preferred["dir"]
                    )
                rec.update(build_system="maven", build_root=build_root, goal=goal)
                rec["rationale"] = (
                    f"Aggregator root over {len(source_modules)} source module(s) "
                    f"({len(groovy_modules)} Groovy); build {scope} with '{goal}'."
                )
                return rec
            if rec["has_gradle"]:
                rec.update(build_system="gradle", build_root=project_path, goal="build")
                rec["rationale"] = (
                    "Maven root is an aggregator with no compilable modules, but a "
                    "Gradle build is present and is likely the primary build."
                )
                return rec
            # Nothing to compile anywhere and no Gradle: packaging/meta-project.
            rec["is_aggregator_only"] = True
            rec["rationale"] = (
                "Root is a Maven aggregator with no module main sources and no Gradle "
                "build — there is no standard Java compile target (packaging/meta-project)."
            )
            return rec

        # 3) Gradle-only project.
        if not has_pom and rec["has_gradle"]:
            rec.update(build_system="gradle", build_root=project_path, goal="build")
            rec["rationale"] = "Gradle build detected (no root pom)."
            return rec

        return rec

    def _island_root_for(self, project_path: str, source_dir: str) -> Dict[str, Any]:
        """Map one source/test-bearing dir to its nearest INDEPENDENT build
        island: the build root that owns it, plus that root's build system.

        Walk up from ``source_dir`` toward ``project_path`` (never above it),
        recording the first ancestor with a build marker (pom.xml /
        build.gradle(.kts)). Independence is defined by settings.gradle: a
        Gradle multi-project (settings.gradle at its root) is ONE island and its
        subprojects are NOT separate islands, so the OUTERMOST settings-gradle
        ancestor wins over a nearer subproject build.gradle. The root aggregator
        itself is skipped (walking stops one level below project_path) — it is
        the pathological root we are decomposing, not an island.

        Returns ``{root, system}`` when an owning build root exists (root = the
        island dir, system = maven/gradle), or ``{"root": None, "system": None}``
        when NO build file sits between the source dir and the aggregator root.
        An island REQUIRES its own build root: a source dir with no build marker
        above it (an example / vendored copy) is NOT an island — callers must
        exclude it, never promote it (doing so manufactured a bogus system=null
        island for examples/demo that the manifest persisted and the agent
        guidance rendered as "build unknown in .../examples/demo").
        """
        orch = self.docker_orchestrator
        root = project_path.rstrip("/")
        cur = source_dir.rstrip("/")

        nearest_build = None  # first ancestor with any build marker
        nearest_system = None
        settings_root = None  # OUTERMOST ancestor carrying settings.gradle

        # Ascend from the module dir up to (but not including) the project root.
        while cur.startswith(root + "/"):
            if _path_exists(orch, f"{cur}/settings.gradle") or _path_exists(
                orch, f"{cur}/settings.gradle.kts"
            ):
                settings_root = cur  # keep ascending -> ends on the outermost
            has_pom = _path_exists(orch, f"{cur}/pom.xml")
            has_gradle_build = _path_exists(orch, f"{cur}/build.gradle") or _path_exists(
                orch, f"{cur}/build.gradle.kts"
            )
            if nearest_build is None and (has_pom or has_gradle_build):
                nearest_build = cur
                nearest_system = "maven" if has_pom else "gradle"
            parent = cur.rsplit("/", 1)[0]
            if parent == cur:
                break
            cur = parent

        if settings_root is not None:
            # The gradle multi-project root is the island; its subprojects fold in.
            return {"root": settings_root, "system": "gradle"}
        if nearest_build is not None:
            return {"root": nearest_build, "system": nearest_system}
        # No build file above the source dir: it has no build root of its own, so
        # it is NOT an island (vendored/example sources). Signal exclusion.
        return {"root": None, "system": None}

    def _island_build_goal(self, root: str, system: Optional[str]) -> str:
        """The recommended build action (GOAL) for one independent island.

        LIVE EVIDENCE (bigtop re-probe): the transaction-queue gradle island died
        13x resolving org.apache.bigtop:bigpetstore-data-generator:3.5.0-SNAPSHOT
        from file:/root/.m2/... — an artifact the data-generators island PRODUCES
        but only if it PUBLISHES to the local maven repo, which a bare `build`
        never does. This is the gradle-island version of the reactor-install
        lesson (a maven island `install`s so siblings resolve its artifact).

        So: maven island -> 'install'; gradle island whose build.gradle(.kts)
        applies the maven-publish plugin -> 'publishToMavenLocal' (it publishes a
        SNAPSHOT other islands consume); every other gradle island -> 'build'.
        """
        if system == "maven":
            return "install"
        if system == "gradle" and self._island_applies_maven_publish(root):
            return "publishToMavenLocal"
        return "build"

    def _island_applies_maven_publish(self, root: str) -> bool:
        """True iff the island's own build.gradle(.kts) applies the maven-publish
        plugin — the signal that it publishes an artifact to the local maven repo
        that a cross-island SNAPSHOT dependency can resolve."""
        orch = self.docker_orchestrator
        if not orch:
            return False
        root = root.rstrip("/")
        cmd = (
            f"grep -lE 'maven-publish' {root}/build.gradle {root}/build.gradle.kts " f"2>/dev/null"
        )
        found = orch.execute_command(cmd)
        return bool((found.get("output") or "").strip())

    def _enumerate_build_islands(
        self, project_path: str, source_modules: List[Dict[str, Any]], preferred_dir: str
    ) -> List[Dict[str, Any]]:
        """Group every source-bearing module into its independent build island
        (pathological-aggregator path only).

        Each island is ``{root, system, goal, rationale}``, deduped by root, with
        the preferred module's island FIRST (so build_islands[0]["root"] ==
        build_root for backward compatibility). ``goal`` is the recommended build
        action for that island (maven -> 'install', gradle-with-maven-publish ->
        'publishToMavenLocal', else 'build') so a cross-island SNAPSHOT
        dependency resolves from the local maven repo.
        """
        islands: List[Dict[str, Any]] = []
        by_root: Dict[str, Dict[str, Any]] = {}
        preferred_island_root = self._island_root_for(project_path, preferred_dir)["root"]

        for mod in source_modules:
            info = self._island_root_for(project_path, mod["dir"])
            root = info["root"]
            if root is None:
                # No build root above this source dir -> not an island
                # (vendored/example copy); exclude it rather than manufacture a
                # bogus system=null island.
                continue
            existing = by_root.get(root)
            if existing is None:
                goal = self._island_build_goal(root, info["system"])
                island = {
                    "root": root,
                    "system": info["system"],
                    "goal": goal,
                    "rationale": (
                        f"Independent {info['system'] or 'unknown'} build island "
                        f"under the aggregator; build it on its own with '{goal}'."
                    ),
                }
                by_root[root] = island
                islands.append(island)
            elif existing.get("system") is None and info["system"]:
                existing["system"] = info["system"]
                # System resolved late -> recompute the goal now that we know it.
                existing["goal"] = self._island_build_goal(root, info["system"])

        # Preferred module's island leads (matches build_root).
        islands.sort(key=lambda i: 0 if i["root"] == preferred_island_root else 1)
        return islands

    def _recommend_test_approach(self, project_path: str, build_rec: Dict[str, Any]) -> None:
        """Recommend WHERE to run tests — they often live in different modules (and
        a different build system) than the main build.

        Bigtop: the 6 compiled classes are the Maven/Groovy bigtop-test-framework,
        but ~49 of 57 tests are in the Gradle bigtop-data-generators modules — so
        `mvn test` in the build module ran zero tests. This finds the test-bearing
        modules, picks the dominant cluster, and records test_root/test_system on
        the recommendation (falling back to the build target when tests are
        co-located).
        """
        orch = self.docker_orchestrator
        build_rec.setdefault("test_root", build_rec.get("build_root", project_path))
        build_rec.setdefault("test_system", build_rec.get("build_system"))
        build_rec.setdefault("test_modules", [])
        # A python recommendation already carries its real test target (pytest
        # at the project root); the Java/Groovy test-dir scan below must not
        # override it (a stray src/test/java dir would relabel it maven).
        if str(build_rec.get("build_system", "")).strip().lower() == "python":
            return
        if not orch:
            return

        find_cmd = (
            f"find {project_path} -maxdepth 6 -type d "
            f"\\( -path '*/src/test/java' -o -path '*/src/test/groovy' "
            f"-o -path '*/src/test/scala' -o -path '*/src/test/kotlin' \\) "
            f"-not -path '*/target/*' -not -path '*/build/*' 2>/dev/null"
        )
        found = orch.execute_command(find_cmd)
        test_module_dirs = []
        for line in (found.get("output") or "").splitlines():
            line = line.strip()
            if "/src/test/" not in line:
                continue
            module_dir = line.rsplit("/src/test/", 1)[0]
            if module_dir not in test_module_dirs:
                test_module_dirs.append(module_dir)
        if not test_module_dirs:
            return
        build_rec["test_modules"] = [
            d[len(project_path) :].lstrip("/") or "." for d in test_module_dirs
        ]

        # Group test modules by their first path segment under the project root and
        # pick the segment that owns the most test modules (where the tests cluster).
        seg_counts: Dict[str, int] = {}
        for module_dir in test_module_dirs:
            rel = module_dir[len(project_path) :].lstrip("/")
            top = rel.split("/")[0] if rel else ""
            seg_counts[top] = seg_counts.get(top, 0) + 1
        top_seg = max(seg_counts.items(), key=lambda kv: kv[1])[0]
        test_root = f"{project_path}/{top_seg}" if top_seg else project_path

        # The test cluster's own build system can differ from the main build's.
        if _path_exists(orch, f"{test_root}/settings.gradle") or _path_exists(
            orch, f"{test_root}/build.gradle"
        ):
            test_system = "gradle"
        elif _path_exists(orch, f"{test_root}/pom.xml"):
            test_system = "maven"
        else:
            test_system = build_rec.get("build_system")

        build_rec["test_root"] = test_root
        build_rec["test_system"] = test_system

        # A Maven reactor built at its root must also be TESTED at its root so
        # `mvn test` runs across every module. The dominant-cluster heuristic above
        # exists for tests that live in a foreign subtree / build system (Bigtop's
        # Gradle tests beside a Maven build); when the build is already the reactor
        # root and the tests are the same system, a single leaf segment is the wrong
        # target (httpcomponents-client: 5 sibling modules tie at 1 test dir each,
        # so the heuristic picked an arbitrary leaf and ran 16 of 1856 tests).
        if build_rec.get("build_root") == project_path and test_system == build_rec.get(
            "build_system"
        ):
            build_rec["test_root"] = project_path

        # PATHOLOGICAL-AGGREGATOR PATH ONLY: an archipelago has independent test
        # islands too. The dominant-cluster heuristic above picks ONE (Bigtop's
        # Gradle bigtop-data-generators); the maven bigtop-test-framework's OWN
        # unit tests then never ran. Enumerate EVERY test island (test-bearing
        # dir -> its build island) so the agent's test-phase guidance targets
        # each; dominant cluster (test_root) leads for backward compatibility.
        if build_rec.get("build_islands"):
            test_islands: List[Dict[str, Any]] = []
            by_root: Dict[str, Dict[str, Any]] = {}
            # test_root (resolved above) is the dominant cluster root and always
            # truthy here — it leads for backward compatibility.
            dominant_root = build_rec.get("test_root")
            for module_dir in test_module_dirs:
                info = self._island_root_for(project_path, module_dir)
                root = info["root"]
                if root is None:
                    # No build root above this test dir -> not a test island
                    # (vendored/example copy); exclude it.
                    continue
                if root in by_root:
                    if by_root[root].get("system") is None and info["system"]:
                        by_root[root]["system"] = info["system"]
                    continue
                island = {
                    "root": root,
                    "system": info["system"],
                    "rationale": (
                        f"Independent {info['system'] or 'unknown'} test island; "
                        "run its tests on its own."
                    ),
                }
                by_root[root] = island
                test_islands.append(island)
            test_islands.sort(key=lambda i: 0 if i["root"] == dominant_root else 1)
            build_rec["test_islands"] = test_islands

    def _persist_build_requirements(self, project_path: str, analysis: Dict[str, Any]) -> None:
        """Persist the analyzer's build/test requirements manifest (spec §2).

        The root shape is DERIVED from the recommendation the analyzer already
        computed — it is a classification of the chosen targeting, not a second
        classifier that could disagree with it:

        - build target IS the project root and the root pom declares reactor
          modules -> ``healthy_reactor``: install/test with fail-at-end so one
          broken module cannot hide the rest (the tri-state verdict absorbs
          partial reactor failures).
        - build target is a subdirectory -> ``pathological_aggregator``: the
          PR #9 leaf targeting was chosen because building the root compiles
          nothing (Bigtop: profile-gated modules).
        - anything else -> ``single_module``.
        """
        from .build_preflight import write_build_requirements

        rec = analysis.get("build_recommendation") or {}
        build_root = rec.get("build_root") or project_path
        root = project_path.rstrip("/")
        if build_root.rstrip("/") == root and analysis.get("maven_modules"):
            root_shape = "healthy_reactor"
        elif build_root.startswith(f"{root}/"):
            root_shape = "pathological_aggregator"
        else:
            root_shape = "single_module"

        fail_at_end = root_shape == "healthy_reactor"
        # Fail-at-end testing only makes sense at reactor scope; when the test
        # cluster lives elsewhere (Bigtop's Gradle subtree) leave it alone.
        test_fail_at_end = fail_at_end and (rec.get("test_root") or "").rstrip("/") == root

        data = {
            "survey": {
                "project_path": project_path,
                "analyzer_version": SURVEY_FACTS_VERSION,
            },
            "java_version": analysis.get("java_version"),
            "java_version_source": analysis.get("java_version_source"),
            "java_version_enforced": bool(analysis.get("java_version_enforced")),
            "root_shape": root_shape,
            "build_root": build_root,
            "build_goal": rec.get("goal"),
            "fail_at_end": fail_at_end,
            "test_root": rec.get("test_root"),
            "test_system": rec.get("test_system"),
            "test_fail_at_end": test_fail_at_end,
            # Multi-island coverage on pathological aggregators: the full
            # archipelago the agent must build/test EACH of. Empty lists on
            # healthy reactors / single modules (the single build_root/test_root
            # fields above already fully describe those).
            "build_islands": rec.get("build_islands") or [],
            "test_islands": rec.get("test_islands") or [],
        }

        # Python requirements ride along on the SAME handoff manifest (spec
        # Component 1): java keys stay, python keys are added when the
        # analyzer's Python branch ran.
        python_config = analysis.get("python_config") or {}
        if python_config:
            data.update(
                {
                    "python_version": python_config.get("python_version"),
                    "python_constraint": python_config.get("python_constraint"),
                    "python_constraint_source": python_config.get("python_constraint_source"),
                    "python_installer": python_config.get("python_installer"),
                    "python_install_commands": python_config.get("python_install_commands") or [],
                    "python_install_note": python_config.get("python_install_note"),
                    "python_install_source": python_config.get("python_install_source"),
                    "python_packages": python_config.get("python_packages") or [],
                    "python_venv": python_config.get("python_venv"),
                    "has_c_extensions": bool(python_config.get("has_c_extensions")),
                    # Native core (root CMakeLists.txt) that must be built before
                    # the python package imports — read by the validator's native
                    # evidence rung.
                    "has_native_build": bool(python_config.get("has_native_build")),
                    "test_hints": python_config.get("test_hints") or {},
                }
            )

        write_build_requirements(self.docker_orchestrator, data)

    def _compose_project_brief(
        self,
        project_path: str,
        analysis: Dict[str, Any],
    ):
        """Compose and atomically publish the complete role-typed brief."""
        from sag.agent.project_brief import ProjectBriefAdapter

        artifact = ProjectBriefAdapter(
            self.docker_orchestrator,
            analyzer_version=str(
                analysis.get("analyzer_version") or PROJECT_ANALYZER_VERSION
            ),
        ).compose(analysis, project_path=project_path)
        analysis["project_brief"] = artifact.brief.model_dump(mode="json")
        analysis["project_brief_ref"] = artifact.artifact_ref
        analysis["project_brief_projection"] = artifact.planner_projection
        analysis["project_brief_cache_hit"] = artifact.cache_hit
        return artifact

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

        logger.info(
            f"Generating three-step execution plan for {project_type} project with {build_system}"
        )

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
                plan.append(
                    {
                        "id": "setup_java_environment",
                        "description": f"Install and configure Java {java_version} (Required by Maven Enforcer)",
                        "priority": "critical",
                        "type": "environment",
                        "core_step": "preparation",
                        "commands": [
                            f'bash(command=\'java -version 2>&1 | grep "version" || echo "Java not found"\')',
                            f"bash(command='apt-get update && apt-get install -y openjdk-{java_version}-jdk')",
                            f"bash(command='update-alternatives --set java /usr/lib/jvm/java-{java_version}-openjdk-$(dpkg --print-architecture)/bin/java')",
                            f"bash(command='export JAVA_HOME=/usr/lib/jvm/java-{java_version}-openjdk-$(dpkg --print-architecture) && java -version')",
                        ],
                    }
                )
            else:
                plan.append(
                    {
                        "id": "setup_environment",
                        "description": f"Verify Java {java_version} environment and install dependencies",
                        "priority": "high",
                        "type": "environment",
                        "core_step": "preparation",
                    }
                )

        # STEP 2: BUILD - Compile/package the project
        if project_type == "Java" and build_system == "Maven":
            plan.append(
                {
                    "id": "build_project",
                    "description": "Compile project using Maven",
                    "priority": "critical",
                    "type": "build",
                    "core_step": "build",
                }
            )
        elif project_type == "Java" and build_system == "Gradle":
            plan.append(
                {
                    "id": "build_project",
                    "description": "Compile project using Gradle",
                    "priority": "critical",
                    "type": "build",
                    "core_step": "build",
                }
            )
        elif project_type == "Node.js":
            plan.append(
                {
                    "id": "build_project",
                    "description": "Build project using npm/yarn",
                    "priority": "critical",
                    "type": "build",
                    "core_step": "build",
                }
            )
        elif project_type == "Python":
            plan.append(
                {
                    "id": "build_project",
                    "description": "Setup and validate Python project",
                    "priority": "critical",
                    "type": "build",
                    "core_step": "build",
                }
            )
        else:
            # Generic build step
            plan.append(
                {
                    "id": "build_project",
                    "description": f"Build {project_type} project using {build_system}",
                    "priority": "critical",
                    "type": "build",
                    "core_step": "build",
                }
            )

        # STEP 3: TEST - Run project tests
        test_framework = analysis.get("test_framework", "unknown")
        test_commands = documentation.get("test_commands", [])

        if test_commands:
            # The documented command is REFERENCE ONLY: the task must prescribe
            # the build tool, which resolves the registered toolchain. Round-4
            # eval: a task saying "documented commands: mvn" steered the model
            # into raw bash mvn with a stale PATH (50 wrong-path failures).
            test_desc = (
                "Run tests with build(action='test') "
                f"(documented command for reference: {', '.join(test_commands[:2])})"
            )
        elif project_type == "Java" and build_system == "Maven":
            # Check if this is a multi-module project
            is_multi_module = analysis.get("is_multi_module", False)
            if is_multi_module:
                test_desc = "Run tests for all modules using Maven (multi-module project)"
                # Add specific command recommendation
                test_commands = ["build(action='test')"]
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
            "core_step": "test",
        }

        # Add specific commands for multi-module Maven projects
        if (
            project_type == "Java"
            and build_system == "Maven"
            and analysis.get("is_multi_module", False)
        ):
            test_step["commands"] = [
                "maven(command='test', fail_at_end=True)",
                "# This ensures all modules are tested even if some have failures",
            ]
            test_step["notes"] = "Multi-module project: use fail_at_end=True to test all modules"

        plan.append(test_step)

        # STEP 4: REPORT - Generate completion report
        plan.append(
            {
                "id": "generate_completion_report",
                "description": "Generate comprehensive setup completion report",
                "priority": "high",
                "type": "report",
                "core_step": "report",
            }
        )

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
            plan.extend(
                [
                    {
                        "id": "analyze_maven_project",
                        "description": "Analyze Maven project structure and dependencies",
                        "priority": "high",
                        "type": "analysis",
                    },
                    {
                        "id": "setup_maven_environment",
                        "description": "Setup Maven build environment and install dependencies",
                        "priority": "high",
                        "type": "environment",
                    },
                    {
                        "id": "build_maven_project",
                        "description": "Compile Maven project",
                        "priority": "high",
                        "type": "build",
                    },
                    {
                        "id": "test_maven_project",
                        "description": "Execute Maven project tests",
                        "priority": "high",
                        "type": "test",
                    },
                ]
            )
        elif any(f in existing_files for f in ["build.gradle", "build.gradle.kts"]):
            plan.extend(
                [
                    {
                        "id": "analyze_gradle_project",
                        "description": "Analyze Gradle project structure and dependencies",
                        "priority": "high",
                        "type": "analysis",
                    },
                    {
                        "id": "setup_gradle_environment",
                        "description": "Setup Gradle build environment and install dependencies",
                        "priority": "high",
                        "type": "environment",
                    },
                    {
                        "id": "build_gradle_project",
                        "description": "Compile Gradle project",
                        "priority": "high",
                        "type": "build",
                    },
                    {
                        "id": "test_gradle_project",
                        "description": "Execute Gradle project tests",
                        "priority": "high",
                        "type": "test",
                    },
                ]
            )
        elif "package.json" in existing_files:
            plan.extend(
                [
                    {
                        "id": "analyze_nodejs_project",
                        "description": "Analyze Node.js project dependencies and scripts",
                        "priority": "high",
                        "type": "analysis",
                    },
                    {
                        "id": "install_npm_dependencies",
                        "description": "Install Node.js dependencies using npm/yarn",
                        "priority": "high",
                        "type": "dependencies",
                    },
                    {
                        "id": "build_nodejs_project",
                        "description": "Build Node.js project",
                        "priority": "high",
                        "type": "build",
                    },
                    {
                        "id": "test_nodejs_project",
                        "description": "Execute Node.js project tests",
                        "priority": "high",
                        "type": "test",
                    },
                ]
            )
        else:
            # 完全未知的项目，使用通用方法
            plan.extend(
                [
                    {
                        "id": "manual_project_exploration",
                        "description": f"Manually explore project structure at {project_path}",
                        "priority": "high",
                        "type": "exploration",
                    },
                    {
                        "id": "identify_build_system",
                        "description": "Identify project build system and requirements",
                        "priority": "high",
                        "type": "analysis",
                    },
                    {
                        "id": "setup_development_environment",
                        "description": "Setup appropriate development environment",
                        "priority": "high",
                        "type": "environment",
                    },
                    {
                        "id": "attempt_project_build",
                        "description": "Attempt to build project using identified tools",
                        "priority": "medium",
                        "type": "build",
                    },
                ]
            )

        return plan

    def _generate_basic_setup_plan(self, analysis: Dict[str, Any]) -> List[Dict[str, str]]:
        """生成基本的setup计划作为最后的fallback"""
        return [
            {
                "id": "verify_project_structure",
                "description": "Verify project structure and identify key components",
                "priority": "high",
                "type": "verification",
            },
            {
                "id": "setup_basic_environment",
                "description": "Setup basic development environment",
                "priority": "high",
                "type": "environment",
            },
            {
                "id": "manual_build_attempt",
                "description": "Attempt manual project build",
                "priority": "medium",
                "type": "build",
            },
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

            # ALWAYS record environment metrics (like static test count) unconditionally
            # This ensures we don't lose test counts if the execution plan is rejected
            self._record_environment_metrics(trunk_context, analysis)

            # Save the metrics immediately in case we return early
            self.context_manager._save_trunk_context(trunk_context)

            # Stage-2 phase machine (spec §3.1): a phase trunk (phase_<name>
            # task ids) is owned by the engine — the analyzer's execution plan
            # is phase-internal advice surfaced in the tool output, never trunk
            # tasks. Rewriting here deleted the pending phase_build/phase_test/
            # phase_report entries, turning every later _persist_phase_record
            # into a silent no-op and orphaning task_N entries in the webui.
            if any(str(task.id).startswith("phase_") for task in trunk_context.todo_list):
                logger.info(
                    "Phase trunk detected: preserved phase_* tasks (analyzer plan "
                    "stays phase-internal advice; recorded analysis metrics only)"
                )
                return True

            execution_plan = analysis.get("execution_plan", [])
            if not execution_plan:
                logger.warning("No execution plan generated, trunk context unchanged")
                return False

            # Evidence hierarchy: a derived re-analysis must not overwrite a
            # plan grounded in stronger evidence. If THIS analysis failed to
            # identify the build system while a previous one succeeded (the
            # trunk remembers it), keep the existing plan — re-planning from
            # "unknown" is exactly the loop that burned beam's 06-10 run
            # (25 re-plans driven by an analyzer blind to the Kotlin DSL).
            incoming_unknown = str(analysis.get("build_system", "unknown")).lower() in (
                "unknown",
                "none",
                "",
            )
            known_system = (trunk_context.environment_summary or {}).get("build_system")
            if incoming_unknown and known_system:
                logger.warning(
                    f"Analyzer returned unknown build system but trunk already has "
                    f"evidence of '{known_system}'; preserving the existing plan"
                )
                return True

            # 验证执行计划的质量
            if not self._is_execution_plan_valid(execution_plan):
                logger.warning(
                    "Generated execution plan appears invalid, preserving existing tasks"
                )
                return False

            # 获取当前pending任务数量
            current_pending = len(
                [task for task in trunk_context.todo_list if task.status.value == "pending"]
            )
            logger.info(
                f"Current pending tasks: {current_pending}, new plan has {len(execution_plan)} tasks"
            )

            # 只有在新计划看起来合理时才替换现有任务
            if len(execution_plan) >= 3:  # 至少3个任务才认为是合理的计划
                # Idempotent plan application: keep completed/in-progress tasks
                # AND pending tasks that are part of the new plan (so their ids
                # stay stable across analyzer re-runs); drop only stale pending
                # tasks the new plan no longer contains. A full clear+re-add
                # renumbered the same tasks on every re-run (beam 2026-06-10:
                # plan re-applied 3x in 90s, churning ids and orphaning the
                # branch contexts/outputs joined on them).
                normalize = trunk_context._normalize_task_description
                plan_descriptions = {
                    normalize(item.get("description", "Unknown task")) for item in execution_plan
                }
                trunk_context.todo_list = [
                    task
                    for task in trunk_context.todo_list
                    if task.status.value != "pending"
                    or normalize(task.description) in plan_descriptions
                ]

                # 添加新的智能任务 (add_task dedup keeps already-present ones)
                for plan_item in execution_plan:
                    task_description = plan_item.get("description", "Unknown task")
                    task_type = plan_item.get("type", "general")
                    logger.debug(f"Adding task: {task_description} (type: {task_type})")
                    trunk_context.add_task(task_description)

                # Remember the identified build system + test metrics so weaker
                # future analyses cannot regress the plan (see guard above).
                # (Metrics already recorded unconditionally at the top of method)

                # 保存更新后的context
                self.context_manager._save_trunk_context(trunk_context)
                logger.info(
                    f"✅ Successfully updated trunk context with {len(execution_plan)} new intelligent tasks"
                )
                return True
            else:
                logger.warning(
                    f"Execution plan too short ({len(execution_plan)} tasks), preserving existing tasks"
                )
                return False

        except Exception as e:
            logger.error(f"Failed to update trunk context: {e}")
            return False

    def _record_environment_metrics(self, trunk_context, analysis: Dict[str, Any]) -> None:
        """Record build system + static test metrics in environment_summary.

        Shared by the legacy plan-rewrite path and the phase-trunk path (which
        never touches the todo list but still feeds the report/test phases)."""
        incoming_unknown = str(analysis.get("build_system", "unknown")).lower() in (
            "unknown",
            "none",
            "",
        )
        if not incoming_unknown:
            trunk_context.environment_summary["build_system"] = analysis.get("build_system")

        build_recommendation = analysis.get("build_recommendation")
        if build_recommendation:
            trunk_context.environment_summary["build_recommendation"] = build_recommendation
            logger.info(
                "📊 Stored build recommendation: "
                f"{build_recommendation.get('build_system')} '{build_recommendation.get('goal')}' "
                f"at {build_recommendation.get('build_root')}"
            )

        project_brief = analysis.get("project_brief") or {}
        project_brief_projection = analysis.get("project_brief_projection")
        project_brief_ref = analysis.get("project_brief_ref")
        if project_brief and project_brief_projection and project_brief_ref:
            trunk_context.environment_summary["project_brief_fingerprint"] = project_brief.get(
                "input_fingerprint"
            )
            trunk_context.environment_summary["project_brief_projection"] = (
                project_brief_projection
            )
            trunk_context.environment_summary["project_brief_ref"] = project_brief_ref

        static_test_count = analysis.get("static_test_count")
        if static_test_count is not None:
            trunk_context.environment_summary["static_test_count"] = static_test_count
            logger.info(
                f"📊 Stored total test count in trunk context: {static_test_count} test cases"
            )

            # Also store method count and parameterized info for detailed reporting
            method_count = analysis.get("method_count")
            if method_count is not None:
                trunk_context.environment_summary["method_count"] = method_count
                trunk_context.environment_summary["test_count_method"] = analysis.get(
                    "test_count_method", "unknown"
                )

            parameterized_info = analysis.get("parameterized_info")
            if parameterized_info:
                trunk_context.environment_summary["parameterized_info"] = parameterized_info

            # Store test catalog summary if available
            test_catalog = analysis.get("test_catalog")
            if test_catalog:
                trunk_context.environment_summary["test_catalog_summary"] = {
                    "total_tests": test_catalog.get("total_count", 0),
                    "by_module": test_catalog.get("by_module", {}),
                }

    def _is_execution_plan_valid(self, execution_plan: List[Dict[str, str]]) -> bool:
        """验证执行计划是否有效"""
        if not execution_plan or len(execution_plan) < 2:
            logger.debug("Execution plan too short")
            return False

        # 检查是否只有报告任务（这通常意味着分析失败）
        non_report_tasks = [
            task
            for task in execution_plan
            if task.get("type") != "report" and "report" not in task.get("description", "").lower()
        ]

        if len(non_report_tasks) < 2:
            logger.debug("Execution plan contains mostly report tasks")
            return False

        # 检查是否有实际的构建/测试任务
        has_build_or_test = any(
            task.get("type") in ["build", "test", "dependencies", "environment"]
            or any(
                keyword in task.get("description", "").lower()
                for keyword in ["build", "compile", "test", "install", "setup"]
            )
            for task in execution_plan
        )

        if not has_build_or_test:
            logger.debug("Execution plan lacks build/test tasks")
            return False

        logger.debug("Execution plan validation passed")
        return True

    def _render_recommended_build_output(self, analysis: Dict[str, Any]) -> str:
        """The 🧭 Recommended Build block of the analysis output.

        With MULTIPLE build islands the island list IS the recommendation —
        the pathological branch's single-target sentence must not co-render
        (live bigtop 2026-07-18: the agent followed 'build module
        bigtop-test-framework directly' from the rationale, hammered the one
        upstream-broken island for 7 calls, and never touched three healthy
        ones the island line named). One authority per fact.
        """
        rec = analysis.get("build_recommendation") or {}
        output = ""
        if rec.get("is_aggregator_only"):
            return (
                f"🧭 Recommended Build: NONE — {rec['rationale']} "
                f"Consider phase(action='blocked', outcome='unknown', ...) with this "
                f"evidence rather than forcing a compile.\n"
            )
        build_islands = rec.get("build_islands") or []
        if len(build_islands) > 1:
            isles = "; ".join(
                f"{i}) {isl.get('system') or 'unknown'} '{isl.get('goal') or 'build'}' "
                f"in {isl['root']}"
                for i, isl in enumerate(build_islands, start=1)
            )
            output += (
                f"🧭 Recommended Build: {len(build_islands)} independent build islands "
                f"— build EACH: {isles}. Islands may depend on each other through the "
                f"local maven repo: publish/install provider islands first.\n"
            )
        else:
            output += (
                f"🧭 Recommended Build: {rec.get('build_system')} "
                f"'{rec.get('goal')}' in {rec.get('build_root')} — {rec['rationale']}\n"
            )
        if rec.get("source_modules"):
            mods = ", ".join(f"{m['module']}({m['lang']})" for m in rec["source_modules"][:6])
            output += f"   • Source modules: {mods}\n"
        return output

    def _format_analysis_output(self, analysis: Dict[str, Any]) -> str:
        """格式化分析输出"""
        output = "🔍 PROJECT ANALYSIS COMPLETED\n\n"

        # 分析路径信息
        project_path = analysis.get("project_path", "Unknown")
        output += f"📁 Analyzed Path: {project_path}\n"

        # 基本信息
        project_type = analysis.get("project_type", "Unknown")
        build_system = analysis.get("build_system", "Unknown")
        output += f"📂 Project Type: {project_type}\n"
        output += f"🔧 Build System: {build_system}\n"
        if analysis.get("project_brief_ref"):
            fingerprint = (analysis.get("project_brief") or {}).get(
                "input_fingerprint", "unknown"
            )
            output += (
                f"🧾 Project Brief: {analysis['project_brief_ref']} "
                f"(fingerprint {str(fingerprint)[:16]})\n"
            )

        # Recommended build target — steer the build phase away from compiling an
        # empty aggregator root (e.g. Bigtop's packaging=pom over Groovy/Gradle).
        rec = analysis.get("build_recommendation") or {}
        if rec.get("rationale"):
            output += self._render_recommended_build_output(analysis)
            # Tests may live in a different module / build system than the build.
            # (Python recs are pytest-at-the-build-root by construction — their
            # differing labels must not render the "not in the build module"
            # call-out; mirrors react_engine._recommended_build_line.)
            test_root = rec.get("test_root")
            if test_root and (
                test_root != rec.get("build_root")
                or (
                    rec.get("test_system") != rec.get("build_system")
                    and str(rec.get("build_system", "")).strip().lower() != "python"
                )
            ):
                output += (
                    f"🧪 Recommended Tests: {rec.get('test_system')} test in {test_root} "
                    f"— the test suite lives here, not in the build module.\n"
                )

        # 显示发现的文件
        existing_files = analysis.get("existing_files", [])
        if existing_files:
            output += f"📄 Project Files Found: {', '.join(existing_files[:5])}\n"
            if len(existing_files) > 5:
                output += f"    ... and {len(existing_files) - 5} more files\n"
        else:
            output += f"⚠️ No project files detected\n"

        # An unknown verdict shows its evidence so the model can judge it
        # (and override with its own observations) instead of trusting a
        # bare "unknown" as authoritative.
        if str(project_type).lower() == "unknown":
            checked = analysis.get("detection_checked") or []
            if checked:
                output += (
                    f"🔎 Detection evidence: checked for {', '.join(checked)} — none present\n"
                )
            root_listing = analysis.get("root_listing")
            if root_listing:
                output += f"📁 Project root contains:\n{root_listing}\n"
            output += (
                "⚠️ This 'unknown' verdict is a detection result, not ground truth — "
                "if build evidence exists (wrapper scripts, compiled artifacts), trust that instead.\n"
            )

        if analysis.get("java_version"):
            output += f"☕ Java Version: {analysis['java_version']}\n"

        # 依赖信息
        dependencies = analysis.get("dependencies", [])
        if dependencies:
            output += (
                f"📦 Dependencies: {len(dependencies)} found ({', '.join(dependencies[:3])}...)\n"
            )

        # 文档分析
        doc = analysis.get("documentation", {})
        if doc.get("java_version_requirement"):
            output += f"📋 Required Java Version: {doc['java_version_requirement']}\n"

        if doc.get("build_commands"):
            output += f"🔨 Build Commands Found: {', '.join(doc['build_commands'][:3])}\n"

        if doc.get("test_commands"):
            output += f"🧪 Test Commands Found: {', '.join(doc['test_commands'][:3])}\n"

        # 测试框架
        test_framework = analysis.get("test_framework", "unknown")
        if test_framework != "unknown":
            output += f"🧪 Test Framework: {test_framework}\n"

        # Test count analysis - now with accurate parameterized expansion
        static_test_count = analysis.get("static_test_count")
        method_count = analysis.get("method_count")
        test_count_method = analysis.get("test_count_method", "unknown")

        if static_test_count is not None:
            if test_count_method == "accurate_expansion_counting":
                output += f"📊 Test Count Analysis (Accurate with Expansions):\n"
                output += f"   • Total Test Cases: {static_test_count} (includes parameterized expansions)\n"
                if method_count and method_count != static_test_count:
                    output += f"   • Method Annotations: {method_count} (@Test, @ParameterizedTest, etc.)\n"
                    expansion = static_test_count / method_count if method_count > 0 else 1
                    output += (
                        f"   • Expansion Factor: {expansion:.1f}x (from parameterized tests)\n"
                    )

                # Show breakdown if available
                param_info = analysis.get("parameterized_info", {})
                if param_info:
                    regular = param_info.get("regular_tests", 0)
                    param_expansions = param_info.get("parameterized_expansions", 0)
                    if regular or param_expansions:
                        output += f"   • Breakdown: {regular} regular tests + {param_expansions} parameterized expansions\n"
            elif test_count_method == "actual_executions":
                output += f"📊 Test Count: {static_test_count} actual test executions (from test reports)\n"
                output += f"   ℹ️ This includes all parameterized test expansions\n"
            else:
                output += f"📊 Test Count: {static_test_count} test method annotations found\n"
                output += f"   ℹ️ Note: Parameterized tests will execute multiple times\n"

        # 执行计划
        execution_plan = analysis.get("execution_plan", [])
        if execution_plan:
            # 分析计划类型
            plan_types = [task.get("type", "general") for task in execution_plan]
            type_counts = {}
            for t in plan_types:
                type_counts[t] = type_counts.get(t, 0) + 1

            output += f"\n📋 GENERATED EXECUTION PLAN ({len(execution_plan)} tasks):\n"
            for i, task in enumerate(execution_plan, 1):
                task_type = task.get("type", "general")
                task_desc = task.get("description", "Unknown task")
                priority = task.get("priority", "medium")
                type_emoji = {
                    "environment": "🔧",
                    "dependencies": "📦",
                    "build": "🔨",
                    "test": "🧪",
                    "report": "📊",
                    "analysis": "🔍",
                    "exploration": "🗺️",
                }.get(task_type, "📋")
                output += f"  {i}. {type_emoji} {task_desc} [{priority}]\n"

            # 显示计划质量指标
            non_report_tasks = [t for t in execution_plan if t.get("type") != "report"]
            if len(non_report_tasks) >= 3:
                output += f"\n✅ Plan Quality: Good ({len(non_report_tasks)} actionable tasks)\n"
            else:
                output += f"\n⚠️ Plan Quality: Limited ({len(non_report_tasks)} actionable tasks)\n"
        else:
            output += f"\n❌ No execution plan generated\n"

        # Context更新状态
        if analysis.get("context_updated"):
            output += f"\n✅ Trunk context updated with new intelligent task plan\n"
        elif analysis.get("context_updated") == False:
            context_error = analysis.get("context_error", "Unknown error")
            output += f"\n⚠️ Context update failed: {context_error}\n"

        # 最终状态
        if project_type != "Unknown" and build_system != "Unknown" and execution_plan:
            output += f"\n🎯 Ready to execute intelligent project setup plan!"
        elif project_type == "Unknown" or build_system == "Unknown":
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
                subdirs = [
                    line.strip()
                    for line in result.get("output", "").split("\n")
                    if line.strip() and line.strip() != "/workspace"
                ]
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
            "pom.xml",  # Maven
            "build.gradle",  # Gradle (Groovy)
            "build.gradle.kts",  # Gradle (Kotlin)
            "package.json",  # Node.js
            "requirements.txt",  # Python
            "pyproject.toml",  # Python Poetry
            "Cargo.toml",  # Rust
            "go.mod",  # Go
            "CMakeLists.txt",  # CMake
            "Makefile",  # Make
            "composer.json",  # PHP
            "Gemfile",  # Ruby
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

    # Build files that let the fallback pick a concrete build/test plan.
    _FALLBACK_BUILD_MARKERS = (
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        "package.json",
        "requirements.txt",
        "pyproject.toml",
    )

    def _redetect_build_files(self, project_path: str) -> List[str]:
        """Re-scan the project root for build files.

        The main analysis can fail to record build files (it errored out, or it
        only checked ``build.gradle`` and missed a Kotlin-DSL ``build.gradle.kts``
        like apache/beam's root). Without this, the fallback treats a known
        Maven/Gradle project as "completely unknown" and tells the agent to
        manually explore, which can loop. Re-detecting here keeps the fallback
        anchored to the real build system.
        """
        if not self.docker_orchestrator:
            return []

        found: List[str] = []
        for marker in self._FALLBACK_BUILD_MARKERS:
            try:
                result = self.docker_orchestrator.execute_command(
                    f"test -f {project_path}/{marker} && echo 'exists' || echo 'missing'"
                )
            except Exception as exc:  # never let detection crash the fallback
                logger.debug(f"Build-file re-detection failed for {marker}: {exc}")
                continue
            if result.get("success") and "exists" in result.get("output", ""):
                found.append(marker)
        return found

    def _generate_three_step_fallback_plan(self, analysis: Dict[str, Any]) -> List[Dict[str, str]]:
        """Generate three-step fallback plan for unknown project types."""
        plan = []
        existing_files = analysis.get("existing_files", [])
        project_path = analysis.get("project_path", "/workspace")

        # If the analysis recorded no recognizable build file, re-scan the root
        # before giving up. This stops a known build system (e.g. a Gradle repo
        # whose root is build.gradle.kts) from being mislabeled "unknown" and
        # sending the agent into a manual-exploration loop.
        if not any(marker in existing_files for marker in self._FALLBACK_BUILD_MARKERS):
            redetected = self._redetect_build_files(project_path)
            if redetected:
                existing_files = list(dict.fromkeys([*existing_files, *redetected]))
                logger.info(f"Fallback re-detected build files: {redetected}")

        logger.info("Generating three-step fallback execution plan for unknown project type")

        # STEP 1: Environment/Dependencies
        if "pom.xml" in existing_files:
            plan.append(
                {
                    "id": "setup_environment",
                    "description": "Install Maven dependencies and verify build environment",
                    "priority": "high",
                    "type": "environment",
                    "core_step": "preparation",
                }
            )
            plan.append(
                {
                    "id": "build_project",
                    "description": "Compile project using Maven",
                    "priority": "critical",
                    "type": "build",
                    "core_step": "build",
                }
            )
            plan.append(
                {
                    "id": "run_tests",
                    "description": "Execute Maven project tests",
                    "priority": "critical",
                    "type": "test",
                    "core_step": "test",
                }
            )
        elif any(
            f in existing_files
            for f in [
                "build.gradle",
                "build.gradle.kts",
                "settings.gradle",
                "settings.gradle.kts",
            ]
        ):
            plan.append(
                {
                    "id": "setup_environment",
                    "description": "Install Gradle dependencies and verify build environment",
                    "priority": "high",
                    "type": "environment",
                    "core_step": "preparation",
                }
            )
            plan.append(
                {
                    "id": "build_project",
                    "description": "Compile project using Gradle",
                    "priority": "critical",
                    "type": "build",
                    "core_step": "build",
                }
            )
            plan.append(
                {
                    "id": "run_tests",
                    "description": "Execute Gradle project tests",
                    "priority": "critical",
                    "type": "test",
                    "core_step": "test",
                }
            )
        elif "package.json" in existing_files:
            plan.append(
                {
                    "id": "setup_environment",
                    "description": "Install Node.js dependencies using npm/yarn",
                    "priority": "high",
                    "type": "environment",
                    "core_step": "preparation",
                }
            )
            plan.append(
                {
                    "id": "build_project",
                    "description": "Build Node.js project",
                    "priority": "critical",
                    "type": "build",
                    "core_step": "build",
                }
            )
            plan.append(
                {
                    "id": "run_tests",
                    "description": "Execute Node.js project tests",
                    "priority": "critical",
                    "type": "test",
                    "core_step": "test",
                }
            )
        else:
            # Completely unknown project
            plan.extend(
                [
                    {
                        "id": "explore_project",
                        "description": f"Manually explore and identify project structure at {project_path}",
                        "priority": "high",
                        "type": "exploration",
                        "core_step": "preparation",
                    },
                    {
                        "id": "attempt_build",
                        "description": "Attempt to build project using identified tools",
                        "priority": "critical",
                        "type": "build",
                        "core_step": "build",
                    },
                    {
                        "id": "attempt_tests",
                        "description": "Attempt to run project tests",
                        "priority": "critical",
                        "type": "test",
                        "core_step": "test",
                    },
                ]
            )

        # STEP 4: Always add report
        plan.append(
            {
                "id": "generate_completion_report",
                "description": "Generate comprehensive setup completion report",
                "priority": "high",
                "type": "report",
                "core_step": "report",
            }
        )

        return plan
