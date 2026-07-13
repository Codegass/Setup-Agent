# tests/test_python_root_package_less.py
"""Reviewer-confirmed (c) defect: subdir-redirect must require the root to be
package-LESS, established positively — not inferred from a bracket-fragile
``[project] ... dependencies =`` regex that any ``[`` (authors/classifiers/
keywords arrays, the standard modern pyproject ordering) truncates.

LIVE EVIDENCE (mirror image of the TVM bug, session 20260713_014403_27874):
a pure-python repo whose root pyproject is a REAL package but declares
``authors``/``classifiers`` before ``dependencies`` was judged a build shell
and silently redirected to a ``python/`` subdir — wrong install/venv/test root,
mis-scoped package discovery.

Every fixture below is a REAL root package (must NOT redirect) paired with a
``python/`` subdir that ships its own package (the redirect trap). The subdir
exists specifically so a false "shell" verdict redirects into it — a correct
detector leaves the root alone.
"""

from sag.tools.internal.project_analyzer import detect_python_package_root
from tests.test_native_build_guidance import _ScriptedRepo


def _detect(root, files):
    orch = _ScriptedRepo(root, files)
    root_files = {
        p[len(root) + 1 :]
        for p in orch.files
        if p.startswith(root + "/") and "/" not in p[len(root) + 1 :]
    }
    root_pyproject = files.get("pyproject.toml", "")
    return detect_python_package_root(orch, root, root_files, root_pyproject)


_ROOT = "/workspace/proj"

# A python/ subdir package that a false "shell" verdict would redirect into.
_SUBDIR_TRAP = {
    "python/setup.py": "from setuptools import setup\nsetup(name='sub')\n",
    "python/sub/__init__.py": "",
}


def test_authors_before_deps_root_is_not_shell():
    """Modern ordering: ``authors=[...]`` before ``dependencies=[...]`` (this
    repo's own pyproject shape). The root is a real package — no redirect."""
    files = {
        "pyproject.toml": (
            '[project]\nname = "proj"\nrequires-python = ">=3.9"\n'
            'authors = [{name = "X"}]\n'
            'classifiers = ["Programming Language :: Python :: 3"]\n'
            'dependencies = ["requests"]\n'
        ),
        "proj/__init__.py": "",
        **_SUBDIR_TRAP,
    }
    result = _detect(_ROOT, files)
    assert result["python_root"] == _ROOT
    assert result["has_native_build"] is False


def test_poetry_root_is_not_shell():
    """Poetry package: real project, deps under ``[tool.poetry.dependencies]``,
    no ``[project]`` table at all — still a root package, no redirect."""
    files = {
        "pyproject.toml": (
            '[tool.poetry]\nname = "proj"\n'
            '[tool.poetry.dependencies]\npython = "^3.9"\nrequests = "*"\n'
        ),
        "proj/__init__.py": "",
        **_SUBDIR_TRAP,
    }
    result = _detect(_ROOT, files)
    assert result["python_root"] == _ROOT


def test_dynamic_dependencies_root_is_not_shell():
    """``[project]`` with ``dynamic = ["dependencies"]`` (deps resolved by the
    build backend). Named package at the root — no redirect."""
    files = {
        "pyproject.toml": (
            '[project]\nname = "proj"\nrequires-python = ">=3.9"\n'
            'dynamic = ["dependencies"]\n'
        ),
        "proj/__init__.py": "",
        **_SUBDIR_TRAP,
    }
    result = _detect(_ROOT, files)
    assert result["python_root"] == _ROOT


def test_setup_py_root_is_not_shell():
    """A classic requirements.txt + setup.py root package (no pyproject at all).
    The root installs from setup.py — no redirect into python/."""
    files = {
        "setup.py": "from setuptools import setup\nsetup(name='proj')\n",
        "requirements.txt": "requests\n",
        "proj/__init__.py": "",
        **_SUBDIR_TRAP,
    }
    result = _detect(_ROOT, files)
    assert result["python_root"] == _ROOT


def test_setup_cfg_root_is_not_shell():
    """A setup.cfg-declarative root package (setup.py is a one-line shim or
    absent). setup.cfg presence establishes a root package — no redirect."""
    files = {
        "setup.cfg": "[metadata]\nname = proj\n[options]\npackages = find:\n",
        "proj/__init__.py": "",
        **_SUBDIR_TRAP,
    }
    result = _detect(_ROOT, files)
    assert result["python_root"] == _ROOT


# --- The redirect MUST still fire for genuine build shells (TVM shape) --------


def test_cmake_shell_with_no_root_package_still_redirects():
    """The real TVM shape: root CMakeLists + a build-shell pyproject with no
    [project] name / no [tool.poetry], real package under python/. This MUST
    still redirect (the fix must not over-correct into never redirecting)."""
    files = {
        "CMakeLists.txt": "project(tvm)\n",
        "pyproject.toml": (
            "[build-system]\n"
            'requires = ["setuptools", "cython"]\n'
            'build-backend = "setuptools.build_meta"\n'
        ),
        **_SUBDIR_TRAP,
    }
    result = _detect(_ROOT, files)
    assert result["python_root"] == f"{_ROOT}/python"
    assert result["has_native_build"] is True


def test_bare_pyproject_shell_with_no_package_still_redirects():
    """A pyproject that is purely a PEP-517 shell (no [project] name, no
    [tool.poetry]) and no root setup.py/setup.cfg, with a python/ package.
    No CMake here — the package-less pyproject alone triggers the redirect."""
    files = {
        "pyproject.toml": (
            "[build-system]\n"
            'requires = ["setuptools"]\n'
            'build-backend = "setuptools.build_meta"\n'
        ),
        **_SUBDIR_TRAP,
    }
    result = _detect(_ROOT, files)
    assert result["python_root"] == f"{_ROOT}/python"
    assert result["has_native_build"] is False
