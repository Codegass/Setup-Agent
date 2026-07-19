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
