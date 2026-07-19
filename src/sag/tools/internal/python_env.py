"""Shared Python-environment helpers: requirement parsing, version resolution,
installer-ladder detection. Used by the analyzer, python_tool, and the setup
tool so the ladder exists exactly once (spec Components 1-3)."""

import re
import shlex
from typing import Any, Dict, List, Optional

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]

SUPPORTED_PYTHONS = ["3.8", "3.9", "3.10", "3.11", "3.12", "3.13"]

_PYPROJECT_RP = re.compile(r'requires-python\s*=\s*["\']([^"\']+)["\']')
_SETUP_PY_RP = re.compile(r'python_requires\s*=\s*["\']([^"\']+)["\']')
_SETUP_CFG_RP = re.compile(r"^\s*python_requires\s*=\s*(.+)$", re.MULTILINE)


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

# Bug #13 defect 3: the pip rung must install the extras the project ACTUALLY
# declares — a hardcoded '.[test]' made pip warn "does not provide the extra
# 'test'" while the tool claimed success (paramiko: pytest never installed).
NO_TEST_EXTRAS_NOTE = "no test extras declared — test deps may be missing"

# Test-shaped extras, in preference order; only names that EXIST are used.
_TEST_EXTRA_PREFERENCE = ("test", "tests", "dev", "develop")

_PYPROJECT_OPTDEPS_TABLE = re.compile(
    r"^\[project\.optional-dependencies\]\s*$(.*?)(?=^\[|\Z)",
    re.MULTILINE | re.DOTALL,
)
# Extras keys are `name = [` assignments; requiring the `[` keeps dependency
# pins inside multi-line arrays ("pytest==7.4") from reading as keys.
_EXTRA_KEY_RE = re.compile(r"""^\s*["']?([A-Za-z0-9][A-Za-z0-9._-]*)["']?\s*=\s*\[""", re.MULTILINE)


def _normalized_group_name(name: str) -> str:
    """PEP 735 group-name comparison normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _dependency_groups(pyproject_content: str) -> Dict[str, List[Any]]:
    """Return well-shaped PEP 735 dependency groups, or an honest empty map.

    A malformed project file must not crash installer detection. The later
    editable install remains authoritative and will surface its own parse
    error with the project's original text.
    """
    try:
        document = tomllib.loads(pyproject_content or "")
    except (TypeError, tomllib.TOMLDecodeError):
        return {}
    raw_groups = document.get("dependency-groups", {})
    if not isinstance(raw_groups, dict):
        return {}
    return {
        str(name): entries
        for name, entries in raw_groups.items()
        if isinstance(name, str) and isinstance(entries, list)
    }


def preferred_test_dependency_groups(pyproject_content: str) -> List[str]:
    """Declared PEP 735 test-shaped groups in stable preference order."""
    declared = _dependency_groups(pyproject_content)
    by_normalized_name = {_normalized_group_name(name): name for name in reversed(list(declared))}
    return [
        by_normalized_name[name] for name in _TEST_EXTRA_PREFERENCE if name in by_normalized_name
    ]


def dependency_group_requirements(pyproject_content: str, selected_groups: List[str]) -> List[str]:
    """Flatten selected PEP 735 groups without shell interpolation.

    ``include-group`` entries are expanded recursively. Cycles and repeated
    requirements are ignored after their first declaration, preserving the
    selected groups' order for deterministic manifests and run pins.
    """
    groups = _dependency_groups(pyproject_content)
    by_normalized_name = {_normalized_group_name(name): name for name in reversed(list(groups))}
    requirements: List[str] = []
    seen_requirements = set()

    def expand(group_name: str, stack: set[str]) -> None:
        normalized_name = _normalized_group_name(group_name)
        if normalized_name in stack:
            return
        actual_name = by_normalized_name.get(normalized_name)
        if actual_name is None:
            return
        next_stack = {*stack, normalized_name}
        for entry in groups[actual_name]:
            if isinstance(entry, str):
                if entry not in seen_requirements:
                    requirements.append(entry)
                    seen_requirements.add(entry)
                continue
            if not isinstance(entry, dict):
                continue
            included = entry.get("include-group")
            if isinstance(included, str):
                expand(included, next_stack)

    for selected_group in selected_groups:
        expand(selected_group, set())
    return requirements


def declared_extras(pyproject_content: str = "", setup_cfg_content: str = "") -> List[str]:
    """Extras names the project ACTUALLY declares: pyproject
    ``[project.optional-dependencies]`` keys plus setup.cfg
    ``[options.extras_require]`` keys, declaration order, de-duplicated."""
    extras: List[str] = []
    table = _PYPROJECT_OPTDEPS_TABLE.search(pyproject_content or "")
    if table:
        for match in _EXTRA_KEY_RE.finditer(table.group(1)):
            name = match.group(1)
            if name not in extras:
                extras.append(name)
    for name in _ini_section(setup_cfg_content or "", "options.extras_require"):
        if name not in extras:
            extras.append(name)
    return extras


def preferred_test_extras(extras) -> List[str]:
    """The test-shaped extras that EXIST, in preference order (bug #13
    defect 3): test/tests/dev/develop — never an invented name."""
    present = set(extras or ())
    return [name for name in _TEST_EXTRA_PREFERENCE if name in present]


def _editable_pip_rung(contents: Optional[Dict[str, str]]) -> Dict[str, Any]:
    """Editable-install rung with REAL test dependency declarations only.

    Optional-dependency extras remain the first choice. When they do not
    exist, flatten a declared PEP 735 test/dev group into a second pip command
    because pip 24 (used by the locked Paramiko image) has no ``--group``.
    With neither surface, use plain ``-e .`` plus NO_TEST_EXTRAS_NOTE so the
    caller narrates the hole instead of silently skipping test deps.
    Module form ('{venv}/bin/python -m pip') everywhere (bug #12): a plain
    `uv venv` ships no {venv}/bin/pip binary."""
    contents = contents or {}
    pyproject_content = contents.get("pyproject.toml", "")
    extras = preferred_test_extras(
        declared_extras(pyproject_content, contents.get("setup.cfg", ""))
    )
    if extras:
        joined = ",".join(extras)
        return {
            "commands": [f"{{venv}}/bin/python -m pip install -e '.[{joined}]'"],
            "test_extras": extras,
            "note": None,
        }
    dependency_groups = preferred_test_dependency_groups(pyproject_content)
    group_requirements = dependency_group_requirements(pyproject_content, dependency_groups)
    if group_requirements:
        return {
            "commands": [
                "{venv}/bin/python -m pip install -e .",
                "{venv}/bin/python -m pip install " + shlex.join(group_requirements),
            ],
            "test_extras": [],
            "test_dependency_groups": dependency_groups,
            "note": None,
        }
    return {
        "commands": ["{venv}/bin/python -m pip install -e ."],
        "test_extras": [],
        "note": NO_TEST_EXTRAS_NOTE,
    }


def detect_installer(files_present, contents: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Pick the project's OWN declared installer (faithfulness ladder):
    poetry.lock -> poetry, Pipfile.lock -> pipenv, then the pip rungs
    (pyproject editable, requirements*.txt, bare setup.py). Command strings
    carry ``{venv}`` / ``{dir}`` placeholders the executing caller fills in;
    detection never runs anything.

    ``contents`` optionally maps pyproject.toml/setup.cfg names to their text
    so the editable rungs install the extras that ACTUALLY exist (bug #13
    defect 3); without it no extra is ever invented."""
    files = set(files_present or ())
    if "poetry.lock" in files:
        return {"installer": "poetry", "commands": ["poetry install"], "source": "poetry.lock"}
    if "Pipfile.lock" in files:
        return {
            "installer": "pipenv",
            "commands": ["pipenv install --dev"],
            "source": "Pipfile.lock",
        }
    if "pyproject.toml" in files:
        return {"installer": "pip", "source": "pyproject.toml", **_editable_pip_rung(contents)}
    requirements = sorted(
        name for name in files if name.startswith("requirements") and name.endswith(".txt")
    )
    if "requirements.txt" in requirements:  # the plain file installs first
        requirements.remove("requirements.txt")
        requirements.insert(0, "requirements.txt")
    if requirements:
        return {
            "installer": "pip",
            "commands": [f"{{venv}}/bin/python -m pip install -r {name}" for name in requirements],
            "source": requirements[0],
        }
    if "setup.py" in files:
        return {"installer": "pip", "source": "setup.py", **_editable_pip_rung(contents)}
    # Nothing declared: honest empty ladder (callers narrate, never invent).
    return {"installer": "pip", "commands": [], "source": None}


# ---------------------------------------------------------------------------
# Venv pip guarantee (bug #13 defect 1): an earlier phase can leave a
# pip-less/broken venv that the pre-flight never repairs because the venv
# already exists. Shared by python_tool, the setup tool and the pre-flight.
# ---------------------------------------------------------------------------

# uv lands in ~/.local/bin (same PATH note as build_preflight).
_LOCAL_BIN_PATH = 'export PATH="$HOME/.local/bin:$PATH"'
_UV_INSTALL = "curl -LsSf https://astral.sh/uv/install.sh | sh"

_ACTIVE_PY_RE = re.compile(r"Python\s+(\d+\.\d+)")


def _active_python_minor(orchestrator) -> Optional[str]:
    """major.minor of the container's system ``python3``, or None."""
    result = orchestrator.execute_command("python3 --version 2>&1")
    match = _ACTIVE_PY_RE.search(result.get("output") or "")
    return match.group(1) if match else None


def ensure_venv_pip(
    orchestrator, venv: str, python_version: Optional[str] = None
) -> Dict[str, Any]:
    """Guarantee ``{venv}/bin/python -m pip`` works before anything installs.

    Straight-line repair ladder, ONE attempt per rung, re-probing pip between
    each (live TVM failure, session 20260713_014403_27874 — Debian splits
    ``ensurepip`` out of the system python, so a plain ``python3 -m venv``
    yields a venv with only symlinks: no pip, no ensurepip module):

      1. probe   ``{venv}/bin/python -m pip --version``
      2. ensurepip
      3. recreate with the current interpreter (``python3 -m venv --clear``)
      4. apt-get install ``python3-venv python3-pip`` + the versioned
         ``python3.<minor>-venv`` for the ACTIVE minor, then recreate
      5. install uv (``curl | sh``, PATH=$HOME/.local/bin) then
         ``uv venv --seed``
      6. all exhausted -> ``ok=False``.

    Returns ``{"ok": bool, "action": None | "ensurepip" | "recreated" |
    "apt-venv" | "uv", "ladder": [<rungs tried, narrated>]}``. Check-and-fix
    only: callers narrate the ladder; a still-broken pip never blocks here —
    the install commands fail honestly downstream. No loop: each rung fires at
    most once, in order."""

    ladder: List[str] = []

    def pip_ok() -> bool:
        probe = orchestrator.execute_command(f"{venv}/bin/python -m pip --version")
        return bool(probe.get("success"))

    # Rung 1: probe. A healthy venv issues zero repair commands.
    if pip_ok():
        return {"ok": True, "action": None, "ladder": []}

    # Rung 2: ensurepip (fails outright when Debian split the module out —
    # 'No module named ensurepip').
    ladder.append("ensurepip")
    orchestrator.execute_command(f"{venv}/bin/python -m ensurepip --upgrade")
    if pip_ok():
        return {"ok": True, "action": "ensurepip", "ladder": ladder}

    # Rung 3: recreate with the CURRENT interpreter. If that interpreter is the
    # ensurepip-less system python, the fresh venv is pip-less again (the TVM
    # trap the old ladder dead-ended on).
    ladder.append("recreate (current interpreter, python3 -m venv --clear)")
    orchestrator.execute_command(f"python3 -m venv --clear {venv}")
    if pip_ok():
        return {"ok": True, "action": "recreated", "ladder": ladder}

    # Rung 4: install the split-out venv/pip packages — the generic ones plus
    # the versioned python3.<minor>-venv that actually ships ensurepip for the
    # active interpreter — then recreate. This is the rung the live TVM run
    # needed and never had.
    minor = _active_python_minor(orchestrator)
    versioned = f" python3.{minor.split('.')[1]}-venv" if minor else ""
    ladder.append(
        "apt python3-venv/python3-pip"
        + (f" (+python3.{minor.split('.')[1]}-venv)" if minor else "")
        + " then recreate"
    )
    apt = orchestrator.execute_command(
        "DEBIAN_FRONTEND=noninteractive apt-get update >/dev/null 2>&1; "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y "
        f"python3-venv python3-pip{versioned}"
    )
    if apt.get("success"):
        orchestrator.execute_command(f"python3 -m venv --clear {venv}")
        if pip_ok():
            return {"ok": True, "action": "apt-venv", "ladder": ladder}

    # Rung 5: install uv, then a seeded venv (ships pip inside — bug #12).
    ladder.append("install uv then uv venv --seed")
    orchestrator.execute_command(f"{_LOCAL_BIN_PATH}; {_UV_INSTALL}")
    python_arg = f" --python {python_version}" if python_version else ""
    orchestrator.execute_command(f"{_LOCAL_BIN_PATH}; uv venv --seed{python_arg} {venv}")
    if pip_ok():
        return {"ok": True, "action": "uv", "ladder": ladder}

    # Rung 6: exhausted. Honest failure; the full ladder is narrated by callers.
    return {"ok": False, "action": "exhausted", "ladder": ladder}


# The winning rung -> the short human phrase callers prepend. Kept beside the
# ladder so the narration vocabulary lives exactly once.
_REPAIR_ACTION_PHRASE = {
    "ensurepip": "repaired with ensurepip",
    "recreated": "recreated with the current interpreter",
    "apt-venv": "recreated after apt-installing python3-venv/python3-pip",
    "uv": "recreated with a seeded uv venv",
}


def venv_repair_note(repair: Dict[str, Any], venv: str) -> Optional[str]:
    """One ``[env]`` narration line for an ``ensure_venv_pip`` result, or None
    when no repair ran. A successful repair names the winning rung; an
    exhausted ladder is an HONEST failure that names every rung tried (no
    silent success)."""
    action = repair.get("action")
    if not action:
        return None
    ladder = repair.get("ladder") or []
    if repair.get("ok"):
        phrase = _REPAIR_ACTION_PHRASE.get(action, "repaired")
        return f"[env] existing venv was missing pip — {phrase} at {venv}"
    tried = "; ".join(ladder) if ladder else "ensurepip and recreation"
    return (
        f"[env] venv at {venv} still has no working pip after the repair ladder "
        f"(tried: {tried}) — pip installs will fail honestly"
    )


# package_dir root mapping ({'': '<dir>'}): the project's own declaration of
# where its import packages live (pyyaml's lib/ layout). Only the '' root key
# relocates the probe base; named per-package mappings are ignored.
_SETUP_PY_PKG_DIR = re.compile(r"package_dir\s*=\s*\{[^}]*?(['\"])\1\s*:\s*['\"]([^'\"]+)['\"]")
_PYPROJECT_PKG_DIR_INLINE = re.compile(
    r"package-dir\s*=\s*\{[^}]*?(['\"])\1\s*=\s*['\"]([^'\"]+)['\"]"
)
_PYPROJECT_PKG_DIR_TABLE = re.compile(r"\[tool\.setuptools\.package-dir\]([^\[]*)")
_PYPROJECT_PKG_DIR_ROOT_KEY = re.compile(r"^\s*(['\"])\1\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)


def package_dir_from_setup_py(content: str) -> Optional[str]:
    """The ``package_dir={'': '<dir>'}`` root mapping, or None. Full-line
    ``#`` comments are dropped first so a commented-out mapping is never
    read as the live declaration (docstring mentions remain a heuristic
    limitation)."""
    live = "\n".join(
        line for line in (content or "").splitlines() if not line.lstrip().startswith("#")
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
        line for line in (content or "").splitlines() if not line.lstrip().startswith("#")
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


# Echoed after each layout find: its presence in the output proves the
# probe EXECUTED (a missing base still echoes it; a container failure does
# not). Distinguishing the two is load-bearing — see package_layout_listing.
LAYOUT_SCAN_SENTINEL = "__SAG_LAYOUT_SCAN_DONE__"


def _package_layout_scan(orchestrator, root: str):
    """The ordered ``(base, find lines, executed)`` scan that BOTH package
    discovery and the survey fingerprint consume — ONE set of bases
    (declared package_dir first, then src-layout, then flat) and ONE find
    predicate (maxdepth 2 from each base, hidden dirs excluded, symlinks
    accepted, no build-output pruning). Category-2 review: a hand-mirrored
    find in the fingerprint drifted from discovery on declared-package_dir
    depth, symlinks, and pruning — sharing the machinery makes the fact and
    its staleness domain inseparable.

    ``executed`` is True iff the trailing sentinel came back: a MISSING base
    (find fails, sentinel echoes) is a legitimately empty listing, while a
    probe that never ran (no sentinel) is unknowable. Discovery treats both
    as empty (its historical behavior); the fingerprint must not.

    Lazy: ``discover_packages`` stops at the first base with packages,
    preserving its historical command sequence; the fingerprint drains it.
    """
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
            f"; echo {LAYOUT_SCAN_SENTINEL}"
        )
        raw = [line.strip() for line in (result.get("output") or "").splitlines() if line.strip()]
        executed = LAYOUT_SCAN_SENTINEL in raw
        lines = [line for line in raw if line != LAYOUT_SCAN_SENTINEL]
        yield base, lines, executed


def package_layout_listing(orchestrator, project_dir: str) -> Optional[List[str]]:
    """Every ``__init__.py`` path the package-discovery fact can derive
    from, across ALL bases, each tagged with its base and SORTED (find
    order is unspecified; the fingerprint's digest is order-sensitive).
    Discovery stops at the first productive base, but WHICH base is
    productive can change — so the staleness domain is the union.

    Returns None when any base probe failed to EXECUTE (no sentinel) —
    callers must treat that as CANNOT COMPARE, never as an empty layout
    (Category-2 review: a transient find failure over a real package
    layout produced an empty-layout digest, spuriously re-surveyed, and
    the re-survey could write python_packages=[] over good facts)."""
    root = project_dir.rstrip("/")
    listing: List[str] = []
    for base, lines, executed in _package_layout_scan(orchestrator, root):
        if not executed:
            return None
        for line in lines:
            entry = f"{base}::{line}"
            if entry not in listing:
                listing.append(entry)
    return sorted(listing)


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
    is kept. Either way the ranking is a heuristic only: the validator's
    imports rung probes the project's FULL installed record whenever one
    exists, so a genuine sibling dropped here (mercurial shape: hgext/ and
    hgdemandimport/ next to the name-matched mercurial/) is still
    import-probed, and a junk dir kept here still cannot block. src-layout
    and package_dir bases are never ranked."""
    root = project_dir.rstrip("/")
    for base, lines, _executed in _package_layout_scan(orchestrator, root):
        packages = []
        for line in lines:
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
                    matches = [name for name in packages if _import_normalized(name) == wanted]
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
            raw = re.sub(r"\{[^}]*\}", "", command[len("pytest") :])
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
