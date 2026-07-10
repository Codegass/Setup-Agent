"""Shared Python-environment helpers: requirement parsing, version resolution,
installer-ladder detection. Used by the analyzer, python_tool, and the setup
tool so the ladder exists exactly once (spec Components 1-3)."""

import re
from typing import Any, Dict, List, Optional

SUPPORTED_PYTHONS = ["3.8", "3.9", "3.10", "3.11", "3.12", "3.13"]

_PYPROJECT_RP = re.compile(r'requires-python\s*=\s*["\']([^"\']+)["\']')
_SETUP_PY_RP = re.compile(r'python_requires\s*=\s*["\']([^"\']+)["\']')
_SETUP_CFG_RP = re.compile(r'^\s*python_requires\s*=\s*(.+)$', re.MULTILINE)


def requires_python_from_pyproject(content: str) -> Optional[str]:
    m = _PYPROJECT_RP.search(content or "")
    return m.group(1).strip() if m else None


def requires_python_from_setup_py(content: str) -> Optional[str]:
    m = _SETUP_PY_RP.search(content or "")
    return m.group(1).strip() if m else None


def requires_python_from_setup_cfg(content: str) -> Optional[str]:
    m = _SETUP_CFG_RP.search(content or "")
    return m.group(1).strip() if m else None


def _ver(v: str) -> tuple:
    return tuple(int(p) for p in v.split(".")[:2])


def _satisfies(candidate: str, spec: str) -> bool:
    """Minimal PEP-440 subset for major.minor candidates: >=, <=, ==, !=, ~=, <, >.
    Wildcards (3.9.*) compare on the major.minor prefix. Unknown syntax -> False
    (unresolvable is honest; the caller keeps the container default)."""
    spec = spec.strip()
    m = re.match(r"^(>=|<=|==|!=|~=|<|>)\s*(\d+(?:\.\d+)?)(?:\.\d+|\.\*)?$", spec)
    if not m:
        return False
    op, rhs = m.group(1), _ver(m.group(2))
    c = _ver(candidate)
    if op == ">=":
        return c >= rhs
    if op == "<=":
        return c <= rhs
    if op == "<":
        return c < rhs
    if op == ">":
        return c > rhs
    if op == "==":
        return c == rhs
    if op == "!=":
        return c != rhs
    if op == "~=":  # compatible release on major.minor: same as == at this granularity
        return c == rhs
    return False


def resolve_python_version(
    constraint: Optional[str], candidates: List[str] = SUPPORTED_PYTHONS
) -> Optional[str]:
    """Newest candidate satisfying EVERY comma-separated specifier, or None."""
    if not constraint or "${" in constraint:
        return None
    specs = [s for s in (p.strip() for p in constraint.split(",")) if s]
    if not specs:
        return None
    for candidate in reversed(candidates):
        if all(_satisfies(candidate, s) for s in specs):
            return candidate
    return None


# ---------------------------------------------------------------------------
# Installer ladder + package discovery (spec Components 1 & 3)
# ---------------------------------------------------------------------------

# Directories that carry an __init__.py without being the import package.
_NON_PACKAGE_DIRS = {"tests", "docs", "examples"}

# pip rung for a pyproject without a lock: try the test extra first so test
# dependencies land, then fall back to a plain editable install.
_PYPROJECT_PIP_COMMAND = (
    "{venv}/bin/pip install -e '.[test]' || {venv}/bin/pip install -e ."
)


def detect_installer(files_present) -> Dict[str, Any]:
    """Pick the project's OWN declared installer (faithfulness ladder):
    poetry.lock -> poetry, Pipfile.lock -> pipenv, then the pip rungs
    (pyproject editable, requirements*.txt, bare setup.py). Command strings
    carry ``{venv}`` / ``{dir}`` placeholders the executing caller fills in;
    detection never runs anything."""
    files = set(files_present or ())
    if "poetry.lock" in files:
        return {"installer": "poetry", "commands": ["poetry install"],
                "source": "poetry.lock"}
    if "Pipfile.lock" in files:
        return {"installer": "pipenv", "commands": ["pipenv install --dev"],
                "source": "Pipfile.lock"}
    if "pyproject.toml" in files:
        return {"installer": "pip", "commands": [_PYPROJECT_PIP_COMMAND],
                "source": "pyproject.toml"}
    requirements = sorted(
        name for name in files
        if name.startswith("requirements") and name.endswith(".txt")
    )
    if "requirements.txt" in requirements:  # the plain file installs first
        requirements.remove("requirements.txt")
        requirements.insert(0, "requirements.txt")
    if requirements:
        return {
            "installer": "pip",
            "commands": [f"{{venv}}/bin/pip install -r {name}" for name in requirements],
            "source": requirements[0],
        }
    if "setup.py" in files:
        return {"installer": "pip", "commands": ["{venv}/bin/pip install -e ."],
                "source": "setup.py"}
    # Nothing declared: honest empty ladder (callers narrate, never invent).
    return {"installer": "pip", "commands": [], "source": None}


# package_dir root mapping ({'': '<dir>'}): the project's own declaration of
# where its import packages live (pyyaml's lib/ layout). Only the '' root key
# relocates the probe base; named per-package mappings are ignored.
_SETUP_PY_PKG_DIR = re.compile(
    r"package_dir\s*=\s*\{[^}]*?(['\"])\1\s*:\s*['\"]([^'\"]+)['\"]"
)
_PYPROJECT_PKG_DIR_INLINE = re.compile(
    r"package-dir\s*=\s*\{[^}]*?(['\"])\1\s*=\s*['\"]([^'\"]+)['\"]"
)
_PYPROJECT_PKG_DIR_TABLE = re.compile(
    r"\[tool\.setuptools\.package-dir\]([^\[]*)"
)
_PYPROJECT_PKG_DIR_ROOT_KEY = re.compile(
    r"^\s*(['\"])\1\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE
)


def package_dir_from_setup_py(content: str) -> Optional[str]:
    """The ``package_dir={'': '<dir>'}`` root mapping, or None. Full-line
    ``#`` comments are dropped first so a commented-out mapping is never
    read as the live declaration (docstring mentions remain a heuristic
    limitation)."""
    live = "\n".join(
        line
        for line in (content or "").splitlines()
        if not line.lstrip().startswith("#")
    )
    m = _SETUP_PY_PKG_DIR.search(live)
    return m.group(2).strip() or None if m else None


def package_dir_from_setup_cfg(content: str) -> Optional[str]:
    """The ``[options] package_dir`` root mapping (``= <dir>`` line), or None."""
    for line in _ini_section(content or "", "options").get("package_dir", []):
        stripped = line.strip()
        if stripped.startswith("="):
            value = stripped[1:].strip()
            if value:
                return value
    return None


def package_dir_from_pyproject(content: str) -> Optional[str]:
    """The ``[tool.setuptools] package-dir`` root mapping, inline dict or
    ``[tool.setuptools.package-dir]`` table form, or None."""
    content = content or ""
    m = _PYPROJECT_PKG_DIR_INLINE.search(content)
    if m:
        return m.group(2).strip() or None
    table = _PYPROJECT_PKG_DIR_TABLE.search(content)
    if table:
        m = _PYPROJECT_PKG_DIR_ROOT_KEY.search(table.group(1))
        if m:
            return m.group(2).strip() or None
    return None


# The project's own declared distribution name ([project].name /
# [tool.poetry].name / setup(name=...)), used ONLY to rank flat-layout
# candidates (apache/libcloud bug #8): a top-level dir whose import-normalized
# name matches the project name IS the project package, and sibling
# repo-support dirs (contrib/, demos/, integration/) that happen to carry an
# __init__.py are dropped. A heuristic, never a deny-list — the validator's
# installed-record gate is the guarantee.
_PYPROJECT_NAME_SECTION = re.compile(
    r"^\[(?:project|tool\.poetry)\]\s*$(.*?)(?=^\[|\Z)", re.MULTILINE | re.DOTALL
)
_NAME_KEY = re.compile(r"^\s*name\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
_SETUP_PY_NAME = re.compile(r"\bname\s*=\s*['\"]([^'\"]+)['\"]")


def project_name_from_pyproject(content: str) -> Optional[str]:
    """The ``name`` key of ``[project]`` (PEP 621) or ``[tool.poetry]``, or
    None. A ``name`` key in any other table is never the project name."""
    for section in _PYPROJECT_NAME_SECTION.finditer(content or ""):
        m = _NAME_KEY.search(section.group(1))
        if m:
            return m.group(1).strip() or None
    return None


def project_name_from_setup_py(content: str) -> Optional[str]:
    """The ``setup(name=...)`` distribution name, or None. Full-line ``#``
    comments are dropped first (same limitation notes as
    package_dir_from_setup_py)."""
    live = "\n".join(
        line
        for line in (content or "").splitlines()
        if not line.lstrip().startswith("#")
    )
    m = _SETUP_PY_NAME.search(live)
    return m.group(1).strip() or None if m else None


def _import_normalized(name: str) -> str:
    """Distribution/dir name in import-name form: lowercase, ``-``/``.``
    runs collapsed to ``_`` (apache-libcloud -> apache_libcloud)."""
    return re.sub(r"[-.]+", "_", (name or "").strip().lower())


def _declared_project_name(orchestrator, root: str) -> Optional[str]:
    """The declared project name from pyproject.toml, else setup.py."""
    readers = (
        ("pyproject.toml", project_name_from_pyproject),
        ("setup.py", project_name_from_setup_py),
    )
    for filename, parse in readers:
        result = orchestrator.execute_command(f"cat {root}/{filename} 2>/dev/null")
        name = parse(result.get("output") or "")
        if name:
            return name
    return None


def _declared_package_dir(orchestrator, root: str) -> Optional[str]:
    """The declared package_dir root ('' key) from setup.py / setup.cfg /
    pyproject.toml, normalized ('.'/'' -> None: that IS the flat layout)."""
    readers = (
        ("setup.py", package_dir_from_setup_py),
        ("setup.cfg", package_dir_from_setup_cfg),
        ("pyproject.toml", package_dir_from_pyproject),
    )
    for name, parse in readers:
        result = orchestrator.execute_command(f"cat {root}/{name} 2>/dev/null")
        mapped = parse(result.get("output") or "")
        if mapped:
            mapped = mapped.strip().strip("/")
            if mapped and mapped != ".":
                return mapped
    return None


def discover_packages(orchestrator, project_dir: str) -> List[str]:
    """Top-level import packages: the project's declared package_dir mapping
    (``package_dir={'': 'lib'}`` — pyyaml-style layouts) first, then
    src-layout (``src/<pkg>/__init__.py``), then flat layout
    (``<pkg>/__init__.py``), excluding tests/docs/examples. Empty when
    nothing is found — never invented.

    Flat-layout ranking (apache/libcloud bug #8): a repo root can carry
    __init__.py in dirs that are NOT the project (contrib/, demos/,
    integration/, pylint_plugins/). When a flat candidate's import-normalized
    name matches the declared project name, that match IS the project and the
    non-matching candidates are dropped; without a name-match every candidate
    is kept (heuristic only — the validator's installed-record gate is the
    guarantee). src-layout and package_dir bases are never ranked."""
    root = project_dir.rstrip("/")
    bases = [f"{root}/src", root]
    mapped = _declared_package_dir(orchestrator, root)
    if mapped:
        mapped_base = f"{root}/{mapped}"
        if mapped_base in bases:
            bases.remove(mapped_base)
        bases.insert(0, mapped_base)
    for base in bases:
        result = orchestrator.execute_command(
            f"find {base} -maxdepth 2 -name __init__.py -not -path '*/.*' 2>/dev/null"
        )
        packages = []
        for line in (result.get("output") or "").splitlines():
            line = line.strip()
            if not line.endswith("/__init__.py"):
                continue
            parent, _, name = line[: -len("/__init__.py")].rpartition("/")
            if parent != base or not name or name in _NON_PACKAGE_DIRS:
                continue
            if name not in packages:
                packages.append(name)
        if packages:
            if base == root and len(packages) > 1:
                declared = _declared_project_name(orchestrator, root)
                if declared:
                    wanted = _import_normalized(declared)
                    matches = [
                        name for name in packages
                        if _import_normalized(name) == wanted
                    ]
                    if matches:
                        packages = matches
            return sorted(packages)
    return []


# ---------------------------------------------------------------------------
# Test hints — tox.ini / setup.cfg are READ-ONLY metadata, never executed
# (settled spec decision)
# ---------------------------------------------------------------------------


def _ini_section(content: str, section: str) -> Dict[str, List[str]]:
    """Tiny INI scrape for one section: {key: [value lines]}. Handles the
    indented continuation-line style tox.ini and setup.cfg both use."""
    values: Dict[str, List[str]] = {}
    in_section = False
    current: Optional[str] = None
    for line in (content or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped[1:-1].strip() == section
            current = None
            continue
        if not in_section or not stripped or stripped.startswith(("#", ";")):
            continue
        if line[:1] in (" ", "\t") and current is not None:  # continuation
            values[current].append(stripped)
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        current = key.strip()
        values[current] = [value.strip()] if value.strip() else []
    return values


def tox_test_hints(content: str) -> Dict[str, Any]:
    """Scrape tox.ini ``[testenv]`` for deps and the pytest args of its
    commands. tox substitutions (``{posargs}``) are dropped: the args feed a
    direct pytest run, never a tox one."""
    section = _ini_section(content, "testenv")
    deps = [dep for dep in section.get("deps", []) if dep]
    pytest_args: Optional[str] = None
    for command in section.get("commands", []):
        if command == "pytest" or command.startswith("pytest "):
            raw = re.sub(r"\{[^}]*\}", "", command[len("pytest"):])
            pytest_args = " ".join(raw.split()) or None
            break
    return {"pytest_args": pytest_args, "test_deps": deps}


def setup_cfg_test_deps(content: str) -> List[str]:
    """Test/dev extras from setup.cfg ``[options.extras_require]``."""
    section = _ini_section(content, "options.extras_require")
    deps: List[str] = []
    for extra in ("test", "tests", "dev"):
        for dep in section.get(extra, []):
            if dep and dep not in deps:
                deps.append(dep)
    return deps
