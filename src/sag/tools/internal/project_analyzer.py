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

# The filesystem READERS live in the physical observation substrate beside the
# validator (analyzer diet, Category 2); the old names are re-exported here so
# every call site — including project_setup_tool and the tests — is unchanged.
from sag.agent.physical_survey import (  # noqa: E402
    ENFORCER_JAVA_PATTERN,
    FALLBACK_BUILD_MARKERS,
    PYTHON_SUBDIR_CANDIDATES as _PYTHON_SUBDIR_CANDIDATES,
    detect_python_package_root,
    normalize_java_version as _normalize_java_version,
    path_exists as _path_exists,
    root_has_installable_package as _root_has_installable_package,
)

PROJECT_ANALYZER_VERSION = "project-analyzer-v1"


# Bumped when the survey's fact semantics change: an older-version manifest is
# re-surveyed instead of reused (review 2026-07-19: existence-as-no-op would
# happily serve stale facts across analyzer upgrades).
# v2: the stamp carries the config source fingerprint (Category 2) — v1
# stamps predate it and re-survey once to gain the staleness contract.
# v3: the fingerprint is recursive-by-name with per-file digests (parent
# POMs, nested island configs, lockfiles), and the trunk stamp carries it
# too — the fast path requires fingerprint agreement on BOTH persisted ends
# (final Category-2 review: a failed trunk save after a config edit left an
# old-fingerprint trunk that still matched on version+path alone).
# v4: the fingerprint domain covers EVERYTHING the survey reads (Cargo/Go/
# Make markers, READMEs, outside-root parent POMs, test sources, the
# module-dir layout), and 'created' verifies THIS survey's fingerprint on
# the re-read manifest — version+path cannot distinguish two surveys of the
# same project, so a dropped rewrite after a config edit passed as created.
SURVEY_FACTS_VERSION = 4


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
        (b) the re-read manifest carries THIS survey's stamp (version,
        project path AND this survey's config fingerprint — a stale file
        left on disk keeps the readback non-empty when a replacement write
        is dropped, and version+path alone cannot tell two surveys of the
        same project apart); ``"present"`` for an agent-era
        stampless manifest, or a current same-project stamp on BOTH persisted
        ends (manifest and trunk env-summary — they fail independently, and a
        manifest-only partial survey must retry the trunk save, not skip it);
        ``"failed"`` otherwise. Older-version or other-project stamps
        re-survey, and so does a current stamp whose config source
        fingerprint no longer matches the files on disk (the staleness
        contract: facts follow the config they were derived from).
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
                and self._trunk_survey_current(
                    validated, existing_stamp.get("config_fingerprint")
                )
                and not self._config_changed_since(orchestrator, existing_stamp, validated)
            ):
                # Current survey for THIS project, on BOTH persisted ends,
                # derived from the config still on disk (re-review 2026-07-19:
                # a same-version manifest from another workspace project must
                # not pass; final review: a failed trunk save left a
                # current-stamp manifest behind, and this fast path then
                # skipped the env-summary retry forever).
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
                or persisted.get("config_fingerprint") != analysis.get("config_fingerprint")
            ):
                # The fingerprint term is what catches a dropped rewrite after
                # a CONFIG EDIT: the old manifest matches on version+path (same
                # project, same analyzer), and only THIS survey's fingerprint
                # tells the readback apart (final Category-2 review P1). Both
                # None (probe down) is equality — a non-None mismatch in either
                # direction means the readback is not this survey's write.
                return "failed"
            return "created"
        except Exception as exc:
            logger.warning(f"framework survey failed: {exc}")
            return "failed"

    def _trunk_survey_current(self, validated: str, manifest_fingerprint) -> bool:
        """Whether the trunk env-summary carries THIS survey's stamp —
        version, project path AND config fingerprint.

        The manifest and the env-summary are persisted by different stores
        that fail independently; the fast path may only skip the survey when
        both ends describe the SAME survey. Fingerprint equality is what
        catches the config-edit re-survey whose trunk save failed: the old
        trunk still matches on version+path, but its metrics were derived
        from the config before the edit (final Category-2 review P1). A load
        failure propagates to ``ensure_facts``'s handler ('failed') — the
        guarantee is manifest AND trunk metrics.
        """
        if self.context_manager is None:
            return True  # no trunk store in play — nothing to keep in sync
        trunk = self.context_manager.load_trunk_context()
        stamp = ((getattr(trunk, "environment_summary", None) or {}).get("survey")) or {}
        return (
            stamp.get("analyzer_version") == SURVEY_FACTS_VERSION
            and stamp.get("project_path") == validated
            and stamp.get("config_fingerprint") == manifest_fingerprint
        )

    def _config_changed_since(self, orchestrator, stamp: Dict[str, Any], validated: str) -> bool:
        """Whether the build-config files changed since the stamped survey.

        Completes the staleness contract (Category 2): a survey's facts
        follow the config they were derived from — editing pom.xml or
        pyproject.toml invalidates the fast path and re-surveys. Comparison
        requires BOTH fingerprints readable; an unavailable probe (either
        end) means CANNOT COMPARE and must not thrash re-surveys.
        """
        from sag.agent.physical_survey import config_fingerprint

        stored = stamp.get("config_fingerprint")
        if not stored:
            return False
        current = config_fingerprint(orchestrator, validated)
        return bool(current) and current != stored

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
        from sag.agent.physical_survey import analyze_project_structure

        return analyze_project_structure(self.docker_orchestrator, project_path)

    def _python_subdir_package(self, project_path: str) -> bool:
        from sag.agent.physical_survey import python_subdir_package

        return python_subdir_package(self.docker_orchestrator, project_path)

    def _analyze_documentation(self, project_path: str) -> Dict[str, Any]:
        from sag.agent.physical_survey import analyze_documentation

        documentation = analyze_documentation(self.docker_orchestrator, project_path)
        # The surveyor extracts commands AS DOCUMENTED; repairing broken ones
        # is a prescription and happens here (final Category-2 review). Skip
        # commands with -Dtest without a value (invalid Maven syntax).
        fixed = []
        for clean_cmd in documentation.get("test_commands", []):
            if "-Dtest" in clean_cmd and not re.search(r"-Dtest=\S+", clean_cmd):
                # Fix the command by removing invalid -Dtest
                clean_cmd = clean_cmd.replace("-Dtest", "").strip()
                # If it becomes just 'mvn clean install', change to 'mvn clean test'
                if clean_cmd == "mvn clean install -Dossindex.skip":
                    clean_cmd = "mvn clean test -Dossindex.skip"
            fixed.append(clean_cmd)
        documentation["test_commands"] = fixed
        return documentation

    def _clean_markdown_command(self, command: str) -> str:
        from sag.agent.physical_survey import clean_markdown_command

        return clean_markdown_command(command)

    def _analyze_build_configuration(self, project_path: str, project_type: str) -> Dict[str, Any]:
        from sag.agent.physical_survey import analyze_build_configuration

        config = analyze_build_configuration(self.docker_orchestrator, project_path, project_type)
        meta = config.pop("python_metadata", None)
        if meta is not None:
            self._compose_python_config(config, meta)
        return config

    def _analyze_python_project(self, project_path: str, analysis: Dict[str, Any]) -> None:
        from sag.agent.physical_survey import read_python_metadata

        meta = read_python_metadata(self.docker_orchestrator, project_path)
        if meta is not None:
            self._compose_python_config(analysis, meta)

    def _compose_python_config(self, analysis: Dict[str, Any], meta: Dict[str, Any]) -> None:
        """Compose the install PLAN from the surveyor's descriptive metadata.

        The installer faithfulness ladder is a prescription — it belongs at
        the tool layer beside setup/python tools' own detect_installer calls,
        not in the surveyor (final Category-2 review). Bug #13 defect 3: the
        editable pip rungs install the extras the project ACTUALLY declares —
        the surveyed metadata contents feed the ladder.
        """
        from .python_env import detect_installer, resolve_python_version

        installer = detect_installer(meta["files_present"], meta["metadata_contents"])
        python_root = meta["python_root"]
        analysis["python_config"] = {
            "python_constraint": meta["python_constraint"],
            "python_constraint_source": meta["python_constraint_source"],
            # The constraint is the surveyed fact; the concrete version that
            # satisfies it (newest from OUR supported list) is a policy pick
            # made here at the tool layer (final Category-2 review).
            "python_version": resolve_python_version(meta["python_constraint"]),
            "python_installer": installer["installer"],
            "python_install_commands": installer["commands"],
            "python_install_source": installer["source"],
            # Bug #13 defect 3: no-test-extras rides the manifest so
            # setup_env narrates the hole instead of failing silently.
            "python_install_note": installer.get("note"),
            "python_packages": meta["python_packages"],
            "python_venv": f"{python_root.rstrip('/')}/.venv",
            "has_c_extensions": meta["has_c_extensions"],
            # The directory the python package actually installs from (the repo
            # root for a plain project; a python/ subdir for a native-core repo)
            # and whether a native library must be built before it imports.
            "python_root": python_root,
            "has_native_build": meta["has_native_build"],
            "test_hints": meta["test_hints"],
        }

    def _analyze_maven_configuration(self, project_path: str, config: Dict[str, Any]):
        from sag.agent.physical_survey import analyze_maven_configuration

        analyze_maven_configuration(self.docker_orchestrator, project_path, config)

    def _analyze_gradle_configuration(self, project_path: str, config: Dict[str, Any]):
        from sag.agent.physical_survey import analyze_gradle_configuration

        analyze_gradle_configuration(self.docker_orchestrator, project_path, config)

    def _extract_gradle_java_version(self, gradle_content: str, config: Dict[str, Any]):
        from sag.agent.physical_survey import extract_gradle_java_version

        extract_gradle_java_version(gradle_content, config)

    def _extract_gradle_dependencies(self, gradle_content: str, config: Dict[str, Any]):
        from sag.agent.physical_survey import extract_gradle_dependencies

        extract_gradle_dependencies(gradle_content, config)

    def _extract_gradle_plugins(self, gradle_content: str, config: Dict[str, Any]):
        from sag.agent.physical_survey import extract_gradle_plugins

        extract_gradle_plugins(gradle_content, config)

    def _analyze_test_configuration(self, project_path: str, project_type: str) -> Dict[str, Any]:
        from sag.agent.physical_survey import analyze_test_configuration

        return analyze_test_configuration(self.docker_orchestrator, project_path, project_type)

    def _detect_maven_test_framework(self, project_path: str, test_config: Dict[str, Any]):
        from sag.agent.physical_survey import detect_maven_test_framework

        detect_maven_test_framework(self.docker_orchestrator, project_path, test_config)

    def _detect_gradle_test_framework(self, project_path: str, test_config: Dict[str, Any]):
        from sag.agent.physical_survey import detect_gradle_test_framework

        detect_gradle_test_framework(self.docker_orchestrator, project_path, test_config)

    def _estimate_total_test_cases(
        self, project_path: str, project_type: str, build_system: str
    ) -> Optional[int]:
        """(Deprecated) Test estimation disabled."""
        return None

    def _get_java_test_annotation_counts(self, project_path: str) -> Optional[Dict[str, int]]:
        from sag.agent.physical_survey import get_java_test_annotation_counts

        return get_java_test_annotation_counts(
            self.docker_orchestrator, project_path, self._java_annotation_cache
        )

    def _count_java_test_with_expansions(self, project_path: str) -> Dict[str, Any]:
        from sag.agent.physical_survey import count_java_test_with_expansions

        return count_java_test_with_expansions(
            self.docker_orchestrator, project_path, self._java_annotation_cache
        )

    def _count_java_test_annotations(self, project_path: str) -> Optional[int]:
        from sag.agent.physical_survey import count_java_test_annotations

        return count_java_test_annotations(
            self.docker_orchestrator, project_path, self._java_annotation_cache
        )

    def _parse_gradle_test_frameworks(self, gradle_content: str) -> List[str]:
        from sag.agent.physical_survey import parse_gradle_test_frameworks

        return parse_gradle_test_frameworks(gradle_content)

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

        from sag.agent.physical_survey import scan_root_build_markers, scan_source_modules

        markers = scan_root_build_markers(orch, project_path)
        has_pom = markers["has_pom"]
        rec["has_gradle"] = markers["has_gradlew"] or markers["has_build_gradle"]

        root_main_java = markers["root_main"]["java"]
        root_main_groovy = markers["root_main"]["groovy"]
        root_main_scala = markers["root_main"]["scala"]
        root_main_kotlin = markers["root_main"]["kotlin"]
        packaging = markers["packaging"]

        source_modules = scan_source_modules(orch, project_path)
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
        from sag.agent.physical_survey import island_root_for

        return island_root_for(self.docker_orchestrator, project_path, source_dir)

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
        from sag.agent.physical_survey import island_applies_maven_publish

        return island_applies_maven_publish(self.docker_orchestrator, root)

    def _enumerate_build_islands(
        self, project_path: str, source_modules: List[Dict[str, Any]], preferred_dir: str
    ) -> List[Dict[str, Any]]:
        """Group every source-bearing module into its independent build island
        (pathological-aggregator path only).

        Each island is ``{root, system, goal, rationale}``, deduped by root, with
        the preferred module's island FIRST (so build_islands[0]["root"] ==
        build_root for backward compatibility). The surveyor substrate supplies
        the DESCRIPTIVE island facts ({root, system, applies_maven_publish});
        the goal composed here from those facts is a prescription (maven ->
        'install', gradle-with-maven-publish -> 'publishToMavenLocal', else
        'build' — so a cross-island SNAPSHOT dependency resolves from the local
        maven repo) and stays at the tool layer until Category 3's A/B gate.
        """
        from sag.agent.physical_survey import enumerate_build_islands

        preferred_island_root = self._island_root_for(project_path, preferred_dir)["root"]

        islands: List[Dict[str, Any]] = []
        for fact in enumerate_build_islands(self.docker_orchestrator, project_path, source_modules):
            goal = (
                "install"
                if fact["system"] == "maven"
                else ("publishToMavenLocal" if fact["applies_maven_publish"] else "build")
            )
            islands.append(
                {
                    "root": fact["root"],
                    "system": fact["system"],
                    "goal": goal,
                    "rationale": (
                        f"Independent {fact['system'] or 'unknown'} build island "
                        f"under the aggregator; build it on its own with '{goal}'."
                    ),
                }
            )

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

        from sag.agent.physical_survey import build_system_at, scan_test_module_dirs

        test_module_dirs = scan_test_module_dirs(orch, project_path)
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
        test_system = build_system_at(orch, test_root) or build_rec.get("build_system")

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

        from sag.agent.physical_survey import config_fingerprint

        # Computed once, stamped on BOTH persisted ends: the manifest here and
        # the trunk env-summary via _record_environment_metrics (the fast path
        # requires agreement — a manifest-only fingerprint let a stale trunk
        # pass on version+path alone).
        analysis["config_fingerprint"] = config_fingerprint(
            self.docker_orchestrator, project_path
        )

        data = {
            "survey": {
                "project_path": project_path,
                "analyzer_version": SURVEY_FACTS_VERSION,
                # Staleness contract: the facts follow the config they were
                # derived from. None when the probe is unavailable — the fast
                # path then skips the comparison rather than thrash.
                "config_fingerprint": analysis["config_fingerprint"],
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

            # Save the metrics immediately in case we return early. The trunk
            # survey stamp asserts THESE metrics are PERSISTED — if the save
            # fails, strip it from the (possibly cached) in-memory trunk, or a
            # later fast path would trust an env-summary that never landed.
            try:
                self.context_manager._save_trunk_context(trunk_context)
            except Exception:
                (trunk_context.environment_summary or {}).pop("survey", None)
                raise

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

        # Mirror the manifest's survey stamp on the trunk end: the fast path
        # in ensure_facts requires the CURRENT stamp on BOTH persisted stores
        # before it may skip the survey (final review 2026-07-19).
        survey_path = analysis.get("project_path")
        if survey_path:
            trunk_context.environment_summary["survey"] = {
                "project_path": survey_path,
                "analyzer_version": SURVEY_FACTS_VERSION,
                # The SAME fingerprint the manifest stamp carries (persisted
                # by _persist_build_requirements earlier in this analysis) —
                # both ends must describe the same survey or the fast path
                # re-surveys (final Category-2 review P1).
                "config_fingerprint": analysis.get("config_fingerprint"),
            }

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
        from sag.agent.physical_survey import validate_and_discover_project_path

        return validate_and_discover_project_path(self.docker_orchestrator, initial_path)

    def _is_valid_project_directory(self, path: str) -> bool:
        from sag.agent.physical_survey import is_valid_project_directory

        return is_valid_project_directory(self.docker_orchestrator, path)

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

    # Build files that let the fallback pick a concrete build/test plan
    # (the canonical tuple lives with the surveyor).
    _FALLBACK_BUILD_MARKERS = FALLBACK_BUILD_MARKERS

    def _redetect_build_files(self, project_path: str) -> List[str]:
        from sag.agent.physical_survey import redetect_build_files

        return redetect_build_files(self.docker_orchestrator, project_path)

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
