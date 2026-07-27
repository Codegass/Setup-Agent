"""Project analyzer tool for physical project surveying."""

import re
from typing import Any, Dict, List, Optional

from loguru import logger

# The filesystem READERS live in the physical observation substrate beside the
# validator (analyzer diet, Category 2); the old names are re-exported here so
# every call site — including project_setup_tool and the tests — is unchanged.
from sag.agent.physical_survey import (
    ENFORCER_JAVA_PATTERN,
    FALLBACK_BUILD_MARKERS,
)
from sag.agent.physical_survey import (  # noqa: E402
    PYTHON_SUBDIR_CANDIDATES as _PYTHON_SUBDIR_CANDIDATES,
)
from sag.agent.physical_survey import (
    detect_python_package_root,
)
from sag.agent.physical_survey import normalize_java_version as _normalize_java_version
from sag.agent.physical_survey import path_exists as _path_exists
from sag.agent.physical_survey import root_has_installable_package as _root_has_installable_package
from sag.project_fact_sheet import (
    project_analysis_error_metadata,
    project_fact_sheet_metadata,
    serialize_project_analysis_error,
    serialize_project_fact_sheet,
)
from sag.testcases.catalog import (
    STATIC_SCAN_EXCLUSION_HELPER,
    TestCaseCatalog,
    build_java_test_catalog,
)

from ..base import BaseTool, ToolResult

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
# v5: the python package LAYOUT (__init__.py paths) joins the fingerprint —
# python_packages derives from those paths and rides the manifest into the
# validator; a package rename with unchanged config served stale names.
# v6: the layout section shares discover_packages' OWN scan machinery
# (arbitrary-depth declared package_dir, symlinks, no pruning) instead of a
# hand-mirrored fixed-depth find that drifted from it.
# v7: the layout listing is SORTED before digesting (find order is
# unspecified) and a probe that fails to execute makes the fingerprint
# CANNOT COMPARE instead of masquerading as an empty layout.
# v8: Python distribution/backend/package paths, direct local providers,
# native artifact roots and grounded smoke coordinates become survey facts;
# .gitmodules and Python test-path layout join the fingerprint domain.
# v9: the survey types each build island as a DOMAIN with the coordinates it
# produces/requires and derives the coordinate edges between them (P0-B), and
# gradle.properties joins the fingerprint domain (a gradle domain's produced
# version is read there). A v8 manifest carries no domains at all, so reusing
# it would hide the very graph the gate now judges independence by.
# v10: the manifest carries the neutral per-domain projection (domain_facts)
# and the edges carry their stable edge_id/support_claim_ids (Plan 6 Stage A).
# A v9 manifest has neither, so reusing it would serve a later stage facts
# whose identities do not exist.
SURVEY_FACTS_VERSION = 10


def _project_recommendation_coordinates(rec):
    """Project internal island redirect state onto shared coordinate facts.

    Per-island ``goal`` remains an internal mechanical input; trunk/model
    consumers receive only system/root coordinates and the typed domain graph.
    """
    if not rec:
        return rec
    projected = {k: v for k, v in rec.items() if k not in ("goal", "rationale")}
    for key in ("build_islands", "test_islands"):
        islands = projected.get(key)
        if islands:
            projected[key] = [
                {k: isl[k] for k in ("root", "system") if k in isl} for isl in islands
            ]
    return projected


def _analysis_failure(code: str, **facts: Any) -> ToolResult:
    """Return a typed analyzer failure; the engine owns its prose projection."""
    metadata = project_analysis_error_metadata(code, **facts)
    return ToolResult.completed_failure(
        output=serialize_project_analysis_error(metadata),
        error=code,
        error_code=code,
        metadata=metadata,
    )


class ProjectAnalyzerTool(BaseTool):
    """Tool for observing and persisting project facts."""

    def __init__(self, docker_orchestrator=None, context_manager=None):
        description = (
            "Survey the cloned project's structure, requirements, and documentation, and persist the machine facts. "
            "This tool reads README files, analyzes build configurations (Maven pom.xml, Gradle build.gradle/build.gradle.kts), "
            "detects Java versions, dependencies, test frameworks (JUnit, TestNG, Spock), and records the survey facts "
            "(manifest + trunk metrics) the build/test phases consume."
        )
        super().__init__(
            name="project_analyzer",
            description=description,
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
                and self._trunk_survey_current(validated, existing_stamp.get("config_fingerprint"))
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
                if not self._update_trunk_context_with_facts(analysis):
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
        Survey project structure and persist observed facts.

        Args:
            action: Action to perform ('analyze' for full analysis)
            project_path: Path to the project directory in container
            update_context: Whether to update the trunk context with new tasks
        """

        # Check for unexpected parameters
        if kwargs:
            invalid_params = list(kwargs.keys())
            return _analysis_failure(
                "ANALYSIS_INVALID_PARAMETERS",
                invalid_parameters=invalid_params,
            )

        logger.info(f"Starting project analysis at: {project_path}")

        try:
            if action == "analyze":
                # Step 1: Validate and discover project path
                validated_path = self._validate_and_discover_project_path(project_path)
                if not validated_path:
                    return _analysis_failure(
                        "PROJECT_NOT_FOUND",
                        requested_path=project_path,
                    )

                logger.info(f"✅ Using validated project path: {validated_path}")

                # Step 2: Perform comprehensive analysis
                analysis_result = self._perform_comprehensive_analysis(validated_path)

                # Step 3: Validate analysis results
                if not self._is_analysis_valid(analysis_result):
                    return _analysis_failure(
                        "ANALYSIS_FAILED",
                        validated_path=validated_path,
                    )

                # Step 4: Update context if requested
                if update_context and self.context_manager:
                    success = self._update_trunk_context_with_facts(analysis_result)
                    if success:
                        analysis_result["context_updated"] = True
                    else:
                        analysis_result["context_updated"] = False
                        analysis_result["context_error"] = "TRUNK_CONTEXT_UPDATE_FAILED"

                fact_sheet = self._facts_projected_metadata(analysis_result)
                return ToolResult.completed_success(
                    output=serialize_project_fact_sheet(fact_sheet),
                    metadata=fact_sheet,
                )
            else:
                return _analysis_failure(
                    "ANALYSIS_INVALID_ACTION",
                    action=action,
                )

        except Exception as e:
            logger.error(f"Failed to analyze project: {e}")
            return _analysis_failure(
                "ANALYSIS_EXCEPTION",
                exception_type=type(e).__name__,
            )

    def _facts_projected_metadata(self, analysis: Dict[str, Any]) -> Dict[str, Any]:
        """Project internal survey state onto the bounded public fact schema.

        Tool-layer installer choices and other policy fields stay in the
        manifest/internal handoff; they never inherit the fact-sheet identity.
        """
        return project_fact_sheet_metadata(analysis)

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
        # A test scanner that has no ecosystem-specific build label must not
        # erase the structure/config scanner's observed build system.
        analysis.update({key: value for key, value in test_config.items() if value is not None})

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

        # dim (a) deleted: no execution plan is generated and the field is
        # absent from the fact sheet (never an empty list).
        analysis.pop("execution_plan", None)

        # dim (c) deleted: no project_brief artifact, ref, or projection is
        # composed — the analyze output and trunk carry the survey facts only.

        return analysis

    def _analyze_project_structure(self, project_path: str) -> Dict[str, Any]:
        from sag.agent.physical_survey import analyze_project_structure

        return analyze_project_structure(self.docker_orchestrator, project_path)

    def _python_subdir_package(self, project_path: str) -> bool:
        from sag.agent.physical_survey import python_subdir_package

        return python_subdir_package(self.docker_orchestrator, project_path)

    def _analyze_documentation(self, project_path: str) -> Dict[str, Any]:
        from sag.agent.physical_survey import analyze_documentation

        # Category 4: documentation facts are returned AS DOCUMENTED. Validity
        # and any later correction belong to the contract/assessment loop, not
        # to the survey result.
        return analyze_documentation(self.docker_orchestrator, project_path)

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
            "python_distribution_name": meta.get("python_distribution_name"),
            "python_build_backend": meta.get("python_build_backend"),
            "python_declared_dependencies": meta.get("python_declared_dependencies") or [],
            "python_package_paths": meta.get("python_package_paths") or [],
            "python_local_providers": meta.get("python_local_providers") or [],
            "python_smoke_candidates": meta.get("python_smoke_candidates") or [],
            "python_venv": f"{python_root.rstrip('/')}/.venv",
            "has_c_extensions": meta["has_c_extensions"],
            # The directory the python package actually installs from (the repo
            # root for a plain project; a python/ subdir for a native-core repo)
            # and whether a native library must be built before it imports.
            "python_root": python_root,
            "has_native_build": meta["has_native_build"],
            "native_build_mode": meta.get("native_build_mode"),
            "native_artifact_roots": meta.get("native_artifact_roots") or [],
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
        """Survey build coordinates so the build phase does not target an empty
        aggregator root.

        Bigtop's root pom is ``packaging=pom`` aggregating Groovy/Gradle modules,
        so ``mvn compile`` at the root returns BUILD SUCCESS with zero
        ``target/classes/*.class``. This inspects the real layout — root packaging,
        root/module main-source dirs (Java AND Groovy), and any Gradle build.

            {build_system, build_root, is_aggregator_only, has_gradle,
             source_modules}
        """
        rec: Dict[str, Any] = {
            "build_system": analysis.get("build_system"),
            "build_root": project_path,
            "is_aggregator_only": False,
            "has_gradle": False,
            "source_modules": [],
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
            # The real install target: a python/ subdir for a native-core repo
            # (TVM), the repo root for a plain project. python_root is set by
            # _analyze_python_project; fall back to the repo root when the
            # python branch did not run (label-only python signal).
            python_root = python_config.get("python_root") or project_path
            rec.update(
                build_system="python",
                build_root=python_root,
                test_root=python_root,
                test_system="pytest",
            )
            # Native-core flag rides the recommendation so the phase-intro
            # guidance can prepend the native-first block (build libtvm.so before
            # the python package can import). False/absent for plain projects.
            if python_config.get("has_native_build"):
                rec["has_native_build"] = True
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
            rec.update(build_system="maven", build_root=project_path)
            return rec

        # 2) Aggregator root (packaging=pom): compiling the root produces nothing.
        if has_pom and packaging == "pom":
            groovy_modules = [m for m in source_modules if m["lang"] == "groovy"]
            if source_modules:
                # If the root pom declares modules, the reactor builds them — build
                # at root. If it does NOT (Bigtop: profile-gated modules), building
                # the root compiles nothing, so target the source module directly.
                if analysis.get("maven_modules"):
                    build_root = project_path
                else:
                    preferred = (groovy_modules or source_modules)[0]
                    build_root = preferred["dir"]
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
                    # P0-B: an island is a DIRECTORY fact and calling it
                    # independent was never grounded (bigtop: data-generators
                    # publishes 3.7.0-SNAPSHOT while transaction-queue/spark
                    # require 3.5.0/3.6.0-SNAPSHOT — 13 attempts died on an
                    # unresolvable dependency). Type each island with the
                    # coordinates it produces/requires and derive the edges, so
                    # independence is a conclusion of the graph. Both keys ride
                    # BESIDE build_islands (untouched) and stay ABSENT on a
                    # single-domain project — there is no graph to speak of.
                    self._attach_build_domains(rec, project_path, source_modules, preferred["dir"])
                rec.update(build_system="maven", build_root=build_root)
                return rec
            if rec["has_gradle"]:
                rec.update(build_system="gradle", build_root=project_path)
                return rec
            # Nothing to compile anywhere and no Gradle: packaging/meta-project.
            rec["is_aggregator_only"] = True
            return rec

        # 3) Gradle-only project.
        if not has_pom and rec["has_gradle"]:
            rec.update(build_system="gradle", build_root=project_path)
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

    def _island_applies_maven_publish(self, root: str) -> bool:
        from sag.agent.physical_survey import island_applies_maven_publish

        return island_applies_maven_publish(self.docker_orchestrator, root)

    def _enumerate_build_islands(
        self, project_path: str, source_modules: List[Dict[str, Any]], preferred_dir: str
    ) -> List[Dict[str, Any]]:
        """Group every source-bearing module into its independent build island
        (pathological-aggregator path only).

        Each island is ``{root, system, goal}``, deduped by root, with
        the preferred module's island FIRST (so build_islands[0]["root"] ==
        build_root for backward compatibility). The surveyor substrate supplies
        the DESCRIPTIVE island facts ({root, system, applies_maven_publish});
        the goal is the one Category-3 survivor consumed mechanically by the
        island redirect/build tool (maven -> 'install',
        gradle-with-maven-publish -> 'publishToMavenLocal', else 'build').
        Category 4 removed the unread rationale fields.
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
                }
            )

        # Preferred module's island leads (matches build_root).
        islands.sort(key=lambda i: 0 if i["root"] == preferred_island_root else 1)
        return islands

    def _attach_build_domains(
        self,
        rec: Dict[str, Any],
        project_path: str,
        source_modules: List[Dict[str, Any]],
        preferred_dir: str,
    ) -> None:
        """Store the typed build domains and their coordinate edges on the
        recommendation (domain schema v1), in the same order as build_islands.

        Facts only — the domains are the surveyor's coordinate reading and the
        edges are derived from them. A single domain is no graph: both keys stay
        ABSENT (not empty) so single-module/healthy-reactor recommendations,
        manifests and intros are byte-identical to before.
        """
        from sag.agent.physical_survey import (
            derive_domain_edges,
            enumerate_build_domains,
            read_policy_claims,
        )

        domains = enumerate_build_domains(self.docker_orchestrator, project_path, source_modules)
        if len(domains) < 2:
            return
        preferred_root = self._island_root_for(project_path, preferred_dir)["root"]
        # Same lead as build_islands (island #1 == build_root) so the two lists
        # stay index-aligned for their readers.
        domains.sort(key=lambda d: 0 if d["root"] == preferred_root else 1)
        rec["build_domains"] = domains
        # Plan 6 Stage A: the persisted policy claims are what can SUPPORT an
        # edge. Absent claims mean absent support keys — the edges themselves
        # are still derived from the coordinates alone.
        edges = derive_domain_edges(domains, claims=read_policy_claims(self.docker_orchestrator))
        if edges:
            rec["domain_edges"] = edges

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
                }
                by_root[root] = island
                test_islands.append(island)
            test_islands.sort(key=lambda i: 0 if i["root"] == dominant_root else 1)
            build_rec["test_islands"] = test_islands

    @staticmethod
    def _survey_native_artifact_fact(
        project_path: str, analysis: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """The survey-time native-artifact fact, in the engine probe's vocabulary.

        The survey knows WHERE a native core would land (``python_root``); it
        cannot know whether the library is BUILT — that is a post-hoc reading,
        so the pre-build status is honestly ``unknown``. Returns None when the
        project declares no native build at all, and the domain then carries no
        ``capability_state`` key (absent fact, absent key).
        """
        python_config = analysis.get("python_config") or {}
        if not python_config.get("has_native_build"):
            return None
        return {
            "status": "unknown",
            "root": python_config.get("python_root") or project_path,
        }

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
        analysis["config_fingerprint"] = config_fingerprint(self.docker_orchestrator, project_path)

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

        # P0-B: the typed domains and their coordinate edges ride the SAME
        # handoff manifest, so every reader downstream judges independence from
        # the graph instead of the directory layout. Absent — not empty — when
        # the survey found no multi-domain decomposition, which keeps
        # single-module and healthy-reactor manifests byte-identical.
        for key in ("build_domains", "domain_edges"):
            if rec.get(key):
                data[key] = rec[key]

        # Plan 6 Stage A (spec §C2): the neutral per-domain projection rides the
        # SAME manifest as the domains it is derived from, so a later stage
        # judges a domain from typed facts instead of re-reading the repository.
        # Facts only: role/environment stay "unknown" until a deterministic rule
        # exists, and documented_actions are claim IDs — never commands. Absent
        # (not empty) without a multi-domain decomposition, which keeps
        # single-module and healthy-reactor manifests byte-identical.
        if rec.get("build_domains"):
            from sag.agent.physical_survey import build_domain_facts

            domain_facts = build_domain_facts(
                self.docker_orchestrator,
                rec["build_domains"],
                rec.get("domain_edges"),
                native_artifact_fact=self._survey_native_artifact_fact(project_path, analysis),
            )
            if domain_facts:
                data["domain_facts"] = domain_facts

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
                    "python_distribution_name": python_config.get("python_distribution_name"),
                    "python_build_backend": python_config.get("python_build_backend"),
                    "python_declared_dependencies": python_config.get(
                        "python_declared_dependencies"
                    )
                    or [],
                    "python_package_paths": python_config.get("python_package_paths") or [],
                    "python_local_providers": python_config.get("python_local_providers") or [],
                    "python_smoke_candidates": python_config.get("python_smoke_candidates") or [],
                    "python_venv": python_config.get("python_venv"),
                    "python_root": python_config.get("python_root"),
                    "has_c_extensions": bool(python_config.get("has_c_extensions")),
                    # Native core (root CMakeLists.txt) that must be built before
                    # the python package imports — read by the validator's native
                    # evidence rung.
                    "has_native_build": bool(python_config.get("has_native_build")),
                    "native_build_mode": python_config.get("native_build_mode"),
                    "native_artifact_roots": python_config.get("native_artifact_roots") or [],
                    "test_hints": python_config.get("test_hints") or {},
                }
            )

        write_build_requirements(self.docker_orchestrator, data)

    def _update_trunk_context_with_facts(self, analysis: Dict[str, Any]) -> bool:
        """Record the survey facts (build system + static test metrics) on the
        trunk and persist them. Facts-only: the plan->todo rewrite (dim a) is
        deleted — a facts-only analysis is a SUCCESS, and the recorded metrics
        ARE the completion."""
        if not self.context_manager:
            logger.warning("No context manager available for updating trunk context")
            return False

        try:
            trunk_context = self.context_manager.load_trunk_context()
            if not trunk_context:
                logger.error("No trunk context found to update")
                return False

            # ALWAYS record environment metrics (like static test count).
            self._record_environment_metrics(trunk_context, analysis)

            # Persist. The trunk survey stamp asserts THESE metrics are
            # PERSISTED — if the save fails, strip it from the (possibly cached)
            # in-memory trunk, or a later fast path would trust an env-summary
            # that never landed.
            try:
                self.context_manager._save_trunk_context(trunk_context)
            except Exception:
                (trunk_context.environment_summary or {}).pop("survey", None)
                raise

            return True

        except Exception as e:
            logger.error(f"Failed to update trunk context: {e}")
            return False

    def _record_environment_metrics(self, trunk_context, analysis: Dict[str, Any]) -> None:
        """Record build system + static test metrics in environment_summary.

        The facts-only completion (dim a deleted): the survey never rewrites the
        todo list — these recorded metrics ARE the analysis result the report/
        test phases consume."""
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
            # The trunk carries coordinate FACTS only (system, roots, islands
            # as {root, system}); the intro line and gates read from here.
            build_recommendation = _project_recommendation_coordinates(build_recommendation)
            trunk_context.environment_summary["build_recommendation"] = build_recommendation
            logger.info(
                "📊 Stored build coordinates: "
                f"{build_recommendation.get('build_system')} "
                f"at {build_recommendation.get('build_root')}"
            )

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
                    "description": "Whether to persist the analysis results to the trunk context",
                    "default": True,
                },
            },
            "required": ["action"],
        }

    def get_usage_example(self) -> str:
        """Describe the public facts-only survey surface."""
        return """
Project survey examples:
  project(action="analyze")
  project(action="analyze", project_path="/workspace/my-project")

The survey records observed project type, build/test coordinates, package and
module layout, tool constraints, test structure, and documentation facts. It
returns a schema-versioned fact sheet and does not generate an execution plan,
choose a build action, repair documented commands, or judge whether a later
build/test succeeded.
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

        # Facts-based validity ONLY (analyzer-diet spec, shared gate rework
        # #1): the superseded plan-length criterion made analysis validity —
        # and therefore ensure_facts — depend on plan GENERATION succeeding.
        # A survey that identified the project and found its files is valid,
        # plan or no plan.
        return True

    # Build files that let the fallback pick a concrete build/test plan
    # (the canonical tuple lives with the surveyor).
    _FALLBACK_BUILD_MARKERS = FALLBACK_BUILD_MARKERS

    def _redetect_build_files(self, project_path: str) -> List[str]:
        from sag.agent.physical_survey import redetect_build_files

        return redetect_build_files(self.docker_orchestrator, project_path)
