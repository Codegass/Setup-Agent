# tests/test_python_requirements.py
"""requires-python parsing + newest-satisfying resolution (spec Component 1)."""

from sag.tools.internal.python_env import (
    SUPPORTED_PYTHONS,
    requires_python_from_pyproject,
    requires_python_from_setup_cfg,
    requires_python_from_setup_py,
    resolve_python_version,
)


def test_pyproject_requires_python():
    content = '[project]\nname = "x"\nrequires-python = ">=3.9,<3.13"\n'
    assert requires_python_from_pyproject(content) == ">=3.9,<3.13"


def test_setup_py_python_requires():
    content = 'setup(name="x", python_requires=">=3.8", packages=[])'
    assert requires_python_from_setup_py(content) == ">=3.8"


def test_setup_cfg_python_requires():
    content = "[options]\npython_requires = >=3.7,!=3.9.*\n"
    assert requires_python_from_setup_cfg(content) == ">=3.7,!=3.9.*"


def test_resolution_policy_is_newest_satisfying():
    assert resolve_python_version(">=3.9,<3.13") == "3.12"
    assert resolve_python_version(">=3.8") == SUPPORTED_PYTHONS[-1]
    assert resolve_python_version("<3.10") == "3.9"
    assert resolve_python_version("~=3.10.0") == "3.10"
    assert resolve_python_version(">=3.7,!=3.9.*") != "3.9"


def test_unresolvable_returns_none():
    assert resolve_python_version(None) is None
    assert resolve_python_version("") is None
    assert resolve_python_version(">=4.0") is None      # nothing satisfies
    assert resolve_python_version("${py.version}") is None  # templated garbage
