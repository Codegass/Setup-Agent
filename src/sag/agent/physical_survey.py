"""Surveyor role of the physical observation substrate (analyzer diet, Category 2).

One substrate, two roles, beside the validator's reading machinery:

* the SURVEYOR (this module) reads the filesystem pre-hoc and DESCRIBES what
  exists — structure, config, islands, counts. It never prescribes an action.
* the JUDGE (``physical_validator``) reads post-hoc and VERDICTS what
  happened. It never recommends.

Functions here are pure readers/parsers relocated from the analyzer tool
(``sag.tools.internal.project_analyzer``): they take the container
orchestrator explicitly where they read, hold no tool state, and import
nothing heavy — the same dependency posture as ``module_coverage``. The
analyzer keeps thin delegating wrappers so call sites (and the agent-facing
tool surface) are unchanged; prescriptive composition (goals, plans,
recommendations) stays at the tool layer until Category 3's A/B gate.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from loguru import logger

# Enforcer version accepts range syntax ([1.8,), [11,17)); capture the lower
# bound including a legacy "1.x" form (the old \d+ captured "1" from "1.8").
ENFORCER_JAVA_PATTERN = r"<requireJavaVersion>.*?<version>\s*\[?\s*(\d+(?:\.\d+)?)"


def normalize_java_version(raw) -> Optional[str]:
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


def path_exists(orch, path: str) -> bool:
    result = orch.execute_command(f"test -e {path} && echo yes || echo no")
    return "yes" in (result.get("output") or "")


# Subdirectories a python-primary repo conventionally uses to hold the real
# installable python package when the repo ROOT is a build shell (native-core
# projects such as TVM: root CMakeLists.txt + python/setup.py). Order is the
# search order — the first that ships its own setup.py/pyproject.toml wins.
PYTHON_SUBDIR_CANDIDATES = ("python", "bindings/python")


def root_has_installable_package(root_files: set, root_pyproject: str) -> bool:
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
    from sag.tools.internal.python_env import project_name_from_pyproject

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
        ``root_has_installable_package``. Package-less-ness is established
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
    root_is_shell = not root_has_installable_package(root_files, root_pyproject)

    python_root = root
    if root_is_shell:
        for candidate in PYTHON_SUBDIR_CANDIDATES:
            sub = f"{root}/{candidate}"
            if path_exists(orch, f"{sub}/setup.py") or path_exists(orch, f"{sub}/pyproject.toml"):
                python_root = sub
                break

    return {"python_root": python_root, "has_native_build": has_native_build}


def python_subdir_package(orch, project_path: str) -> bool:
    """True when a conventional python subdir ships its own package metadata.

    Native-core repos (TVM) keep the installable python package in
    ``python/`` (or ``bindings/python/``) beside a CMake build shell at the
    root. Used only as the LAST classification fallback — a CMake root with
    no root python marker is Python iff such a subdir package exists."""
    if not orch:
        return False
    root = project_path.rstrip("/")
    for candidate in PYTHON_SUBDIR_CANDIDATES:
        sub = f"{root}/{candidate}"
        if path_exists(orch, f"{sub}/setup.py") or path_exists(orch, f"{sub}/pyproject.toml"):
            return True
    return False


def analyze_project_structure(orch, project_path: str) -> Dict[str, Any]:
    """分析项目结构，检测项目类型和构建系统"""
    if not orch:
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
        result = orch.execute_command(
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
    elif "CMakeLists.txt" in existing_files and python_subdir_package(orch, project_path):
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
        structure["detection_checked"] = [f for f in files_to_check if not f.startswith("README")]
        listing = orch.execute_command(f"ls -1 {project_path} 2>/dev/null | head -30")
        if listing.get("success"):
            structure["root_listing"] = (listing.get("output") or "").strip()

    return structure


def analyze_documentation(orch, project_path: str) -> Dict[str, Any]:
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

    if not orch:
        return documentation

    # 尝试读取 README 文件
    readme_files = ["README.md", "README.txt", "README", "docs/README.md"]
    readme_content = ""

    for readme_file in readme_files:
        result = orch.execute_command(f"cat {project_path}/{readme_file}")
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
                clean_cmd = clean_markdown_command(match)
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
                clean_cmd = clean_markdown_command(match)
                # AS DOCUMENTED (markdown formatting stripped, deduped): the
                # prescriptive repair of broken documented commands happens at
                # the tool layer, not in the surveyor.
                if clean_cmd and clean_cmd not in documentation["test_commands"]:
                    documentation["test_commands"].append(clean_cmd)

    return documentation


def analyze_build_configuration(orch, project_path: str, project_type: str) -> Dict[str, Any]:
    """分析构建配置文件"""
    config = {
        "java_version": None,
        "dependencies": [],
        "plugins": [],
        "profiles": [],
        "build_system": None,
    }

    if not orch:
        return config

    if project_type == "Java":
        # 首先检查是Maven还是Gradle项目
        maven_exists = orch.execute_command(f"test -f {project_path}/pom.xml && echo 'exists'")
        gradle_exists = orch.execute_command(
            f"test -f {project_path}/build.gradle && echo 'exists'"
        )
        gradle_kts_exists = orch.execute_command(
            f"test -f {project_path}/build.gradle.kts && echo 'exists'"
        )

        if maven_exists.get("success") and "exists" in maven_exists.get("output", ""):
            config["build_system"] = "Maven"
            analyze_maven_configuration(orch, project_path, config)
        elif (gradle_exists.get("success") and "exists" in gradle_exists.get("output", "")) or (
            gradle_kts_exists.get("success") and "exists" in gradle_kts_exists.get("output", "")
        ):
            config["build_system"] = "Gradle"
            analyze_gradle_configuration(orch, project_path, config)
    elif project_type == "Python":
        # Keep the structure-detection label (this dict overwrites the
        # analysis via update(), so a None here would erase it) and add
        # the Python survey depth (spec Component 1). DESCRIPTIVE metadata
        # only — the analyzer composes python_config (installer ladder) from
        # it at the tool layer.
        config["build_system"] = "pip/poetry"
        config["python_metadata"] = read_python_metadata(orch, project_path)

    return config


def read_python_metadata(orch, project_path: str) -> Optional[Dict[str, Any]]:
    """Python survey depth, DESCRIPTIVELY: interpreter constraint -> concrete
    version (newest satisfying — a derived fact, not an action), native-core
    root redirection, top-level package discovery, C-extension markers, and
    READ-ONLY test hints (tox/nox are metadata only, never executed).

    Returns the raw metadata the tool layer composes its install plan from
    (the installer LADDER is a prescription — detect_installer runs at the
    analyzer/setup/python-tool layer, never here; final Category-2 review:
    the surveyor describes, it never prescribes). None when no orchestrator.
    """
    from sag.tools.internal.python_env import (
        discover_packages,
        requires_python_from_pyproject,
        requires_python_from_setup_cfg,
        requires_python_from_setup_py,
        resolve_python_version,
        setup_cfg_test_deps,
        tox_test_hints,
    )

    if not orch:
        return None

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

    return {
        "python_constraint": constraint,
        "python_constraint_source": constraint_source,
        "python_version": resolve_python_version(constraint),
        "python_packages": discover_packages(orch, python_root),
        "has_c_extensions": has_c_extensions,
        # The directory the python package actually installs from (the repo
        # root for a plain project; a python/ subdir for a native-core repo)
        # and whether a native library must be built before it imports.
        "python_root": python_root,
        "has_native_build": has_native_build,
        "test_hints": hints,
        # Raw material for the tool layer's install-plan composition (Bug #13
        # defect 3: the editable pip rungs install the extras the project
        # ACTUALLY declares — the contents ride along for detect_installer).
        "files_present": files_present,
        "metadata_contents": {"pyproject.toml": pyproject, "setup.cfg": setup_cfg},
    }


def analyze_maven_configuration(orch, project_path: str, config: Dict[str, Any]) -> None:
    """分析Maven配置（pom.xml）- 包括多模块项目和父POM"""
    # First, read the main pom.xml. Read it UNTRUNCATED: the default XML-aware
    # truncation protects the model's context window, but this content is parsed
    # internally by regex (java version, <modules>, <packaging>, dependencies) and
    # never reaches the model. Truncation drops <modules>/enforcer blocks on large
    # poms (httpcomponents-client: <modules> at line 260), which mis-scoped builds.
    result = orch.execute_command(f"cat {project_path}/pom.xml", truncate_output=False)
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
            check_result = orch.execute_command(
                f"test -f {parent_path} && echo 'exists' 2>/dev/null"
            )
            if check_result.get("success") and "exists" in check_result.get("output", ""):
                # Extract just the properties section to avoid truncation
                props_result = orch.execute_command(
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
        enforcer_match = re.search(ENFORCER_JAVA_PATTERN, pom_content, re.DOTALL | re.IGNORECASE)
        if enforcer_match:
            normalized = normalize_java_version(enforcer_match.group(1))
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
                normalized = normalize_java_version(match.group(1))
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


def analyze_gradle_configuration(orch, project_path: str, config: Dict[str, Any]) -> None:
    """分析Gradle配置（build.gradle 或 build.gradle.kts）"""
    # 首先尝试读取 build.gradle
    gradle_content = ""
    gradle_file = ""

    result = orch.execute_command(f"cat {project_path}/build.gradle")
    if result.get("success"):
        gradle_content = result.get("output", "")
        gradle_file = "build.gradle"
    else:
        # 尝试读取 build.gradle.kts
        result = orch.execute_command(f"cat {project_path}/build.gradle.kts")
        if result.get("success"):
            gradle_content = result.get("output", "")
            gradle_file = "build.gradle.kts"

    if gradle_content:
        logger.info(f"Analyzing Gradle configuration from {gradle_file}")

        # 提取 Java 版本
        extract_gradle_java_version(gradle_content, config)

        # 提取依赖信息
        extract_gradle_dependencies(gradle_content, config)

        # 提取插件信息
        extract_gradle_plugins(gradle_content, config)


def analyze_test_configuration(orch, project_path: str, project_type: str) -> Dict[str, Any]:
    """分析测试配置"""
    test_config = {
        "test_framework": "unknown",
        "test_directories": [],
        "test_patterns": [],
        "build_system": None,
    }

    if not orch:
        return test_config

    # 检查测试目录
    test_dirs = ["src/test", "test", "tests", "__tests__"]
    for test_dir in test_dirs:
        result = orch.execute_command(f"test -d {project_path}/{test_dir} && echo 'exists'")
        if result.get("success") and "exists" in result.get("output", ""):
            test_config["test_directories"].append(test_dir)

    # 根据项目类型检测测试框架
    if project_type == "Java":
        # 检查是Maven还是Gradle项目
        maven_exists = orch.execute_command(f"test -f {project_path}/pom.xml && echo 'exists'")
        gradle_exists = orch.execute_command(
            f"test -f {project_path}/build.gradle && echo 'exists'"
        )
        gradle_kts_exists = orch.execute_command(
            f"test -f {project_path}/build.gradle.kts && echo 'exists'"
        )

        if maven_exists.get("success") and "exists" in maven_exists.get("output", ""):
            test_config["build_system"] = "Maven"
            detect_maven_test_framework(orch, project_path, test_config)
        elif (gradle_exists.get("success") and "exists" in gradle_exists.get("output", "")) or (
            gradle_kts_exists.get("success") and "exists" in gradle_kts_exists.get("output", "")
        ):
            test_config["build_system"] = "Gradle"
            detect_gradle_test_framework(orch, project_path, test_config)

    return test_config


def detect_maven_test_framework(orch, project_path: str, test_config: Dict[str, Any]) -> None:
    """检测Maven项目的测试框架"""
    # 检查是否使用 JUnit
    result = orch.execute_command(f"grep -r 'junit' {project_path}/pom.xml")
    if result.get("success") and result.get("output"):
        test_config["test_framework"] = "JUnit"

    # 检查是否使用 TestNG
    result = orch.execute_command(f"grep -r 'testng' {project_path}/pom.xml")
    if result.get("success") and result.get("output"):
        test_config["test_framework"] = "TestNG"


def detect_gradle_test_framework(orch, project_path: str, test_config: Dict[str, Any]) -> None:
    """检测Gradle项目的测试框架"""
    # 尝试读取build.gradle文件
    gradle_content = ""
    result = orch.execute_command(f"cat {project_path}/build.gradle")
    if result.get("success"):
        gradle_content = result.get("output", "")
    else:
        # 尝试读取build.gradle.kts文件
        result = orch.execute_command(f"cat {project_path}/build.gradle.kts")
        if result.get("success"):
            gradle_content = result.get("output", "")

    if gradle_content:
        # 检测测试框架
        test_frameworks = parse_gradle_test_frameworks(gradle_content)
        if test_frameworks:
            test_config["test_framework"] = ", ".join(test_frameworks)
            logger.info(f"Found Gradle test frameworks: {test_frameworks}")


def get_java_test_annotation_counts(
    orch, project_path: str, cache: Optional[Dict[str, Dict[str, int]]] = None
) -> Optional[Dict[str, int]]:
    """Collect counts for key JUnit annotations inside src/test/* Java sources."""
    import json

    from sag.testcases.catalog import STATIC_SCAN_EXCLUSION_HELPER

    if not orch:
        return None

    if cache is not None and project_path in cache:
        return cache[project_path]

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

    response = orch.execute_command(command)
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

    if cache is not None:
        cache[project_path] = counts
    return counts


def count_java_test_annotations(
    orch, project_path: str, cache: Optional[Dict[str, Dict[str, int]]] = None
) -> Optional[int]:
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
    if not orch:
        return None

    counts = get_java_test_annotation_counts(orch, project_path, cache)
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


def count_java_test_with_expansions(
    orch, project_path: str, cache: Optional[Dict[str, Dict[str, int]]] = None
) -> Dict[str, Any]:
    """
    Count Java test annotations and capture metadata about parameterized usage.

    Returns:
        Dict with:
        - 'method_count': Number of test method annotations
        - 'total_test_count': Total test cases based on annotations (deduplicated)
        - 'parameterized_info': Details about parameterized tests
    """
    if not orch:
        return {"method_count": None, "total_test_count": None}

    # Always calculate the raw annotation total first so we have a baseline
    # even if the per-annotation breakdown command fails.
    method_count = count_java_test_annotations(orch, project_path, cache)

    counts = get_java_test_annotation_counts(orch, project_path, cache)
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


def island_root_for(orch, project_path: str, source_dir: str) -> Dict[str, Any]:
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
    root = project_path.rstrip("/")
    cur = source_dir.rstrip("/")

    nearest_build = None  # first ancestor with any build marker
    nearest_system = None
    settings_root = None  # OUTERMOST ancestor carrying settings.gradle

    # Ascend from the module dir up to (but not including) the project root.
    while cur.startswith(root + "/"):
        if path_exists(orch, f"{cur}/settings.gradle") or path_exists(
            orch, f"{cur}/settings.gradle.kts"
        ):
            settings_root = cur  # keep ascending -> ends on the outermost
        has_pom = path_exists(orch, f"{cur}/pom.xml")
        has_gradle_build = path_exists(orch, f"{cur}/build.gradle") or path_exists(
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


def island_applies_maven_publish(orch, root: str) -> bool:
    """True iff the island's own build.gradle(.kts) applies the maven-publish
    plugin — the signal that it publishes an artifact to the local maven repo
    that a cross-island SNAPSHOT dependency can resolve."""
    if not orch:
        return False
    root = root.rstrip("/")
    cmd = f"grep -lE 'maven-publish' {root}/build.gradle {root}/build.gradle.kts " f"2>/dev/null"
    found = orch.execute_command(cmd)
    return bool((found.get("output") or "").strip())


def enumerate_build_islands(
    orch, project_path: str, source_modules: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Group every source-bearing module into its independent build island
    (pathological-aggregator path only) — DESCRIPTIVELY.

    Each island is ``{root, system, applies_maven_publish}``, deduped by root:
    what exists on disk, nothing about what to do with it. The recommended
    action (goal) is a prescription and stays with the analyzer tool layer —
    the surveyor describes, it never prescribes.
    """
    islands: List[Dict[str, Any]] = []
    by_root: Dict[str, Dict[str, Any]] = {}

    for mod in source_modules:
        info = island_root_for(orch, project_path, mod["dir"])
        root = info["root"]
        if root is None:
            # No build root above this source dir -> not an island
            # (vendored/example copy); exclude it rather than manufacture a
            # bogus system=null island.
            continue
        existing = by_root.get(root)
        if existing is None:
            island = {
                "root": root,
                "system": info["system"],
                "applies_maven_publish": (
                    info["system"] == "gradle" and island_applies_maven_publish(orch, root)
                ),
            }
            by_root[root] = island
            islands.append(island)
        elif existing.get("system") is None and info["system"]:
            existing["system"] = info["system"]
            # System resolved late -> the publish fact becomes knowable now.
            existing["applies_maven_publish"] = (
                info["system"] == "gradle" and island_applies_maven_publish(orch, root)
            )

    return islands


def scan_root_build_markers(orch, project_path: str) -> Dict[str, Any]:
    """Root build markers + declared packaging, as facts.

    The probe order matches the recommendation's historical inline reads:
    pom, gradlew, build.gradle(.kts), the four main-source language dirs,
    then the packaging grep (Maven roots only; absent <packaging> defaults
    to jar, packaging reality).
    """
    has_pom = path_exists(orch, f"{project_path}/pom.xml")
    has_gradlew = path_exists(orch, f"{project_path}/gradlew")
    has_build_gradle = path_exists(orch, f"{project_path}/build.gradle") or path_exists(
        orch, f"{project_path}/build.gradle.kts"
    )

    root_main = {
        "java": path_exists(orch, f"{project_path}/src/main/java"),
        "groovy": path_exists(orch, f"{project_path}/src/main/groovy"),
        "scala": path_exists(orch, f"{project_path}/src/main/scala"),
        "kotlin": path_exists(orch, f"{project_path}/src/main/kotlin"),
    }

    packaging = None
    if has_pom:
        pkg = orch.execute_command(f"grep -m1 '<packaging>' {project_path}/pom.xml 2>/dev/null")
        match = re.search(r"<packaging>\s*([^<\s]+)\s*</packaging>", pkg.get("output") or "")
        packaging = match.group(1).strip().lower() if match else "jar"

    return {
        "has_pom": has_pom,
        "has_gradlew": has_gradlew,
        "has_build_gradle": has_build_gradle,
        "root_main": root_main,
        "packaging": packaging,
    }


def scan_source_modules(orch, project_path: str) -> List[Dict[str, Any]]:
    """Find source-bearing modules DIRECTLY rather than trusting the root
    pom's <modules> — Bigtop declares its modules inside a profile, so the
    parsed list is empty and the Groovy iTest framework was missed. Scan for
    Java, Groovy, Scala AND Kotlin main-source dirs, excluding build output.
    (Live re-probe: bigpetstore-spark's only sources are src/main/scala with
    its own build.gradle; a java/groovy-only find never enumerated it, so
    the real archipelago produced 3 islands where the fixture had 4. Kotlin
    is the same class of gap.)

    Each module is ``{module, dir, lang}`` (module = root-relative path);
    the aggregator root itself is never a module.
    """
    source_modules: List[Dict[str, Any]] = []
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
    return source_modules


def scan_test_module_dirs(orch, project_path: str) -> List[str]:
    """Find every test-bearing module dir (Java/Groovy/Scala/Kotlin test
    sources, build output excluded), deduped in find order. Bigtop: the
    compiled classes are the Maven/Groovy test framework, but ~49 of 57
    tests live in the Gradle data-generators modules — the test scan must
    see the whole tree, not the build target."""
    find_cmd = (
        f"find {project_path} -maxdepth 6 -type d "
        f"\\( -path '*/src/test/java' -o -path '*/src/test/groovy' "
        f"-o -path '*/src/test/scala' -o -path '*/src/test/kotlin' \\) "
        f"-not -path '*/target/*' -not -path '*/build/*' 2>/dev/null"
    )
    found = orch.execute_command(find_cmd)
    test_module_dirs: List[str] = []
    for line in (found.get("output") or "").splitlines():
        line = line.strip()
        if "/src/test/" not in line:
            continue
        module_dir = line.rsplit("/src/test/", 1)[0]
        if module_dir not in test_module_dirs:
            test_module_dirs.append(module_dir)
    return test_module_dirs


def build_system_at(orch, root: str) -> Optional[str]:
    """The build system marked at ``root`` ITSELF (no ancestor walk):
    gradle when settings.gradle/build.gradle sits there, maven on pom.xml,
    else None. Probe order preserved from the historical inline read."""
    if path_exists(orch, f"{root}/settings.gradle") or path_exists(orch, f"{root}/build.gradle"):
        return "gradle"
    if path_exists(orch, f"{root}/pom.xml"):
        return "maven"
    return None


def validate_and_discover_project_path(orch, initial_path: str) -> Optional[str]:
    """Validate project path and discover actual project location if needed."""
    if not orch:
        logger.warning("No orchestrator available for path validation")
        return initial_path

    # List of paths to check (in order of preference)
    candidate_paths = [initial_path]

    # If initial path is /workspace, also check common subdirectories
    if initial_path == "/workspace":
        # Get list of subdirectories in workspace
        result = orch.execute_command("find /workspace -maxdepth 1 -type d")
        if result.get("success"):
            subdirs = [
                line.strip()
                for line in result.get("output", "").split("\n")
                if line.strip() and line.strip() != "/workspace"
            ]
            candidate_paths.extend(subdirs)

    # Check each candidate path for project indicators
    for path in candidate_paths:
        if is_valid_project_directory(orch, path):
            logger.info(f"✅ Found valid project at: {path}")
            return path
        else:
            logger.debug(f"❌ No project found at: {path}")

    return None


def is_valid_project_directory(orch, path: str) -> bool:
    """Check if a directory contains valid project indicators."""
    if not orch:
        return False

    # Check if directory exists
    result = orch.execute_command(f"test -d {path}")
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
        result = orch.execute_command(f"test -f {path}/{indicator}")
        if result.get("exit_code") == 0:
            logger.debug(f"Found project indicator {indicator} in {path}")
            return True

    # Check for source code directories as secondary indicators
    source_dirs = ["src", "lib", "app", "source"]
    for src_dir in source_dirs:
        result = orch.execute_command(f"test -d {path}/{src_dir}")
        if result.get("exit_code") == 0:
            # Check if it contains actual source files
            result = orch.execute_command(
                f"find {path}/{src_dir} -name '*.java' -o -name '*.py' -o -name '*.js' -o -name '*.ts' -o -name '*.go' -o -name '*.rs' | head -1"
            )
            if result.get("success") and result.get("output", "").strip():
                logger.debug(f"Found source files in {path}/{src_dir}")
                return True

    return False


# Build files that let the fallback pick a concrete build/test plan.
FALLBACK_BUILD_MARKERS = (
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "package.json",
    "requirements.txt",
    "pyproject.toml",
)

# The config files the survey derives its facts from — the staleness domain
# of the survey stamp's source fingerprint. Recursive by NAME (final
# Category-2 review P1: parent POMs, nested island build files, lockfiles
# and wrapper markers all feed the facts — a root-only cat missed them):
# java build files at every depth, the gradle wrapper marker, the python
# metadata/lockfiles the installer/constraint/test-hint parsing reads, and
# the native-core marker.
SURVEY_FINGERPRINT_SOURCES = (
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "gradlew",
    "package.json",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "tox.ini",
    "requirements*.txt",
    "poetry.lock",
    "Pipfile",
    "Pipfile.lock",
    "CMakeLists.txt",
)

# Never fingerprint build OUTPUT or vendored trees: configs copied into
# target/build by a build run would churn the fingerprint after every build
# and thrash re-surveys (the same exclusions the source-module scan uses).
FINGERPRINT_PRUNE_DIRS = (".git", "node_modules", ".venv", "target", "build", "dist", ".tox")


def config_fingerprint(orch, project_path: str) -> Optional[str]:
    """Digest of the build-config files under ``project_path``, or None.

    One container command: ``find`` enumerates every fingerprint source by
    name at any depth (pruning build output), ``sort`` fixes the order, and
    per-file ``cksum`` lines — checksum, size AND file name — collapse
    through a final ``cksum``. Per-file digests encode what a bare
    concatenation cannot (final Category-2 review P1): file NAMES, file
    EXISTENCE (adding/deleting a config changes the listing), and content
    BOUNDARIES (bytes moving between files changes the line set).

    Returns None when the probe is unavailable — callers must treat None as
    CANNOT COMPARE, never as a mismatch, or a flaky container would thrash
    re-surveys.
    """
    if not orch:
        return None
    prunes = " -o ".join(f"-name {d}" for d in FINGERPRINT_PRUNE_DIRS)
    names = " -o ".join(f"-name '{n}'" for n in SURVEY_FINGERPRINT_SOURCES)
    command = (
        f"cd {project_path} && find . \\( {prunes} \\) -prune -o "
        f"-type f \\( {names} \\) -print 2>/dev/null | sort | "
        f"xargs -r cksum 2>/dev/null | cksum"
    )
    try:
        result = orch.execute_command(command)
    except Exception as exc:
        logger.debug(f"config fingerprint unavailable: {exc}")
        return None
    if not result.get("success"):
        return None
    return (result.get("output") or "").strip() or None


def redetect_build_files(orch, project_path: str) -> List[str]:
    """Re-scan the project root for build files.

    The main analysis can fail to record build files (it errored out, or it
    only checked ``build.gradle`` and missed a Kotlin-DSL ``build.gradle.kts``
    like apache/beam's root). Without this, the fallback treats a known
    Maven/Gradle project as "completely unknown" and tells the agent to
    manually explore, which can loop. Re-detecting here keeps the fallback
    anchored to the real build system.
    """
    if not orch:
        return []

    found: List[str] = []
    for marker in FALLBACK_BUILD_MARKERS:
        try:
            result = orch.execute_command(
                f"test -f {project_path}/{marker} && echo 'exists' || echo 'missing'"
            )
        except Exception as exc:  # never let detection crash the fallback
            logger.debug(f"Build-file re-detection failed for {marker}: {exc}")
            continue
        if result.get("success") and "exists" in result.get("output", ""):
            found.append(marker)
    return found


def clean_markdown_command(command: str) -> str:
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


def extract_gradle_java_version(gradle_content: str, config: Dict[str, Any]) -> None:
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
            version = normalize_java_version(match.group(1))
            if not version:
                # Rejected capture: fall through to the next pattern.
                continue
            config["java_version"] = version
            logger.info(f"Found Java version: {version}")
            break


def extract_gradle_dependencies(gradle_content: str, config: Dict[str, Any]) -> None:
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


def extract_gradle_plugins(gradle_content: str, config: Dict[str, Any]) -> None:
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


def parse_gradle_test_frameworks(gradle_content: str) -> List[str]:
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
