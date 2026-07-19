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
