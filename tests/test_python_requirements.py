# tests/test_python_requirements.py
"""requires-python parsing + newest-satisfying resolution (spec Component 1)."""

import shlex

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
    assert resolve_python_version(">=4.0") is None  # nothing satisfies
    assert resolve_python_version("${py.version}") is None  # templated garbage


# ---------------------------------------------------------------------------
# Task 2: installer ladder + package discovery + analyzer manifest keys
# (spec Components 1 & 3 — detection only; nothing here executes an installer)
# ---------------------------------------------------------------------------

from sag.tools.internal.python_env import detect_installer, discover_packages


def test_installer_ladder_poetry_rung():
    result = detect_installer({"poetry.lock", "pyproject.toml", "README.md"})
    assert result["installer"] == "poetry"
    assert result["commands"] == ["poetry install"]
    assert result["source"] == "poetry.lock"


def test_installer_ladder_pipenv_rung():
    result = detect_installer({"Pipfile.lock", "Pipfile", "setup.py"})
    assert result["installer"] == "pipenv"
    assert result["commands"] == ["pipenv install --dev"]
    assert result["source"] == "Pipfile.lock"


def test_installer_ladder_pyproject_rung_without_metadata_is_plain_editable():
    # Bug #13 defect 3: never invent a '.[test]' extra — without metadata to
    # verify extras against, the rung is a plain editable install, noted.
    result = detect_installer({"pyproject.toml", "setup.py"})
    assert result["installer"] == "pip"
    assert result["commands"] == ["{venv}/bin/python -m pip install -e ."]
    assert result["source"] == "pyproject.toml"
    assert result["note"] == "no test extras declared — test deps may be missing"


def test_installer_ladder_requirements_rung_orders_requirements_txt_first():
    result = detect_installer({"requirements-dev.txt", "requirements.txt", "setup.py"})
    assert result["installer"] == "pip"
    assert result["commands"] == [
        "{venv}/bin/python -m pip install -r requirements.txt",
        "{venv}/bin/python -m pip install -r requirements-dev.txt",
    ]
    assert result["source"] == "requirements.txt"


def test_installer_ladder_bare_setup_py_rung():
    result = detect_installer({"setup.py"})
    assert result["installer"] == "pip"
    assert result["commands"] == ["{venv}/bin/python -m pip install -e ."]
    assert result["source"] == "setup.py"


def test_installer_ladder_pip_rungs_are_module_form_only():
    """Bug #12: a plain `uv venv` ships NO {venv}/bin/pip binary, so every
    pip rung must be module-form ('{venv}/bin/python -m pip ...') — the module
    invocation survives seeding differences. String-level guarantee over every
    pip-rung shape; poetry/pipenv rungs stay the project's own tool."""
    pip_rung_file_sets = [
        {"pyproject.toml"},
        {"requirements.txt", "requirements-dev.txt", "requirements-test.txt"},
        {"setup.py"},
    ]
    for files in pip_rung_file_sets:
        for command in detect_installer(files)["commands"]:
            assert "{venv}/bin/pip" not in command, (files, command)
            assert command.count("pip install") == command.count(
                "{venv}/bin/python -m pip install"
            ), (files, command)


def test_installer_precedence_locks_beat_pyproject():
    # Faithfulness order: the project's own lock wins over the generic rungs.
    assert detect_installer({"pyproject.toml", "poetry.lock"})["installer"] == "poetry"
    assert detect_installer({"pyproject.toml", "Pipfile.lock"})["installer"] == "pipenv"
    assert detect_installer({"requirements.txt", "pyproject.toml"})["source"] == "pyproject.toml"


def test_installer_nothing_declared_is_an_honest_empty_ladder():
    result = detect_installer(set())
    assert result["installer"] == "pip"
    assert result["commands"] == []
    assert result["source"] is None


# ---------------------------------------------------------------------------
# Bug #13 defect 3: REAL extras detection — the pip rung must install the
# extras the project ACTUALLY declares ([project.optional-dependencies] /
# setup.cfg [options.extras_require]), never a hardcoded '.[test]'.
# paramiko evidence: no 'test' extra exists, pip printed "does not provide
# the extra 'test'", and pytest/icecream silently never installed.
# ---------------------------------------------------------------------------

from sag.tools.internal.python_env import declared_extras

PARAMIKO_SHAPED_PYPROJECT = """\
[project]
name = "paramiko"

[project.optional-dependencies]
gssapi = ["pyasn1>=0.1.7"]
invoke = ["invoke>=2.0"]
all = ["pyasn1>=0.1.7", "invoke>=2.0"]
"""

PARAMIKO_PEP735_PYPROJECT = """\
[project]
name = "paramiko"

[dependency-groups]
test-core = [
    "pytest-relaxed>=2",
    "icecream>=2.1",
]
dev = [
    "invoke>=2.2.1",
    {include-group = "test-core"},
    "pytest-xdist>=3",
]
"""


def test_declared_extras_reads_pyproject_and_setup_cfg():
    assert declared_extras(PARAMIKO_SHAPED_PYPROJECT, "") == ["gssapi", "invoke", "all"]
    cfg = "[options.extras_require]\ntests =\n    mock\n    pytest\n"
    assert declared_extras("", cfg) == ["tests"]


def test_declared_extras_ignores_dependency_pins_inside_arrays():
    content = (
        "[project.optional-dependencies]\n"
        'test = [\n    "pytest==7.4",\n    "coverage>=6",\n]\n'
        'lint = ["ruff"]\n'
    )
    assert declared_extras(content, "") == ["test", "lint"]


def test_pyproject_rung_without_test_extras_is_plain_editable_with_note():
    # Legacy Paramiko shape: extras exist, but none of them are test extras
    # and there is no PEP 735 dependency group to fall back to.
    result = detect_installer(
        {"pyproject.toml"}, contents={"pyproject.toml": PARAMIKO_SHAPED_PYPROJECT}
    )
    assert result["commands"] == ["{venv}/bin/python -m pip install -e ."]
    assert result["test_extras"] == []
    assert result["note"] == "no test extras declared — test deps may be missing"


def test_pyproject_rung_installs_pep735_dev_group_requirements():
    # Current Paramiko moved its test stack to [dependency-groups].dev. pip
    # 24 has no --group support, so flatten the declared group into a second,
    # safely quoted pip command after the editable install.
    result = detect_installer(
        {"pyproject.toml"}, contents={"pyproject.toml": PARAMIKO_PEP735_PYPROJECT}
    )

    assert result["commands"][0] == "{venv}/bin/python -m pip install -e ."
    assert shlex.split(result["commands"][1]) == [
        "{venv}/bin/python",
        "-m",
        "pip",
        "install",
        "invoke>=2.2.1",
        "pytest-relaxed>=2",
        "icecream>=2.1",
        "pytest-xdist>=3",
    ]
    assert result["test_extras"] == []
    assert result["test_dependency_groups"] == ["dev"]
    assert result.get("note") is None


def test_pep735_group_cycles_do_not_duplicate_or_recurse_forever():
    content = """\
[project]
name = "cycle"
[dependency-groups]
test = ["pytest", {include-group = "dev"}]
dev = ["coverage", {include-group = "test"}, "pytest"]
"""
    result = detect_installer({"pyproject.toml"}, contents={"pyproject.toml": content})

    assert shlex.split(result["commands"][1])[-2:] == ["pytest", "coverage"]
    assert result["test_dependency_groups"] == ["test", "dev"]


def test_pyproject_rung_combines_the_test_extras_that_exist():
    content = (
        '[project]\nname = "x"\n\n'
        "[project.optional-dependencies]\n"
        'tests = ["pytest"]\ndev = ["tox"]\ndocs = ["sphinx"]\n'
    )
    result = detect_installer({"pyproject.toml"}, contents={"pyproject.toml": content})
    # preference order test/tests/dev/develop, restricted to what EXISTS
    assert result["commands"] == ["{venv}/bin/python -m pip install -e '.[tests,dev]'"]
    assert result["test_extras"] == ["tests", "dev"]
    assert result.get("note") is None


def test_setup_cfg_extras_feed_the_editable_rung():
    cfg = "[options.extras_require]\ntest =\n    mock\n    pytest\n"
    result = detect_installer(
        {"pyproject.toml", "setup.cfg"},
        contents={"pyproject.toml": '[project]\nname = "x"\n', "setup.cfg": cfg},
    )
    assert result["commands"] == ["{venv}/bin/python -m pip install -e '.[test]'"]
    assert result["test_extras"] == ["test"]


class LayoutOrch:
    """Scripted orchestrator for package discovery: answers `find <base> ...`
    from a base-dir -> output table (house style: test_build_preflight.py)."""

    def __init__(self, find_outputs):
        self.find_outputs = find_outputs
        self.commands = []

    def execute_command(self, cmd, workdir=None, **kwargs):
        self.commands.append(cmd)
        if cmd.startswith("find "):
            base = cmd.split()[1]
            return {"success": True, "exit_code": 0, "output": self.find_outputs.get(base, "")}
        return {"success": True, "exit_code": 0, "output": ""}


def test_discover_packages_src_layout_wins_over_flat():
    orch = LayoutOrch(
        {
            "/workspace/proj/src": "/workspace/proj/src/foo/__init__.py\n",
            "/workspace/proj": "/workspace/proj/stale/__init__.py\n",
        }
    )
    assert discover_packages(orch, "/workspace/proj") == ["foo"]


def test_discover_packages_flat_layout_excludes_non_packages():
    orch = LayoutOrch(
        {
            "/workspace/proj": (
                "/workspace/proj/bar/__init__.py\n"
                "/workspace/proj/tests/__init__.py\n"
                "/workspace/proj/docs/__init__.py\n"
                "/workspace/proj/examples/__init__.py\n"
            ),
        }
    )
    assert discover_packages(orch, "/workspace/proj") == ["bar"]


def test_discover_packages_none_found_is_empty_not_invented():
    assert discover_packages(LayoutOrch({}), "/workspace/proj") == []


# ---------------------------------------------------------------------------
# Analyzer wiring -> manifest keys (java keys stay; python keys ride along)
# ---------------------------------------------------------------------------


class PythonProjectOrch:
    """Scripted project filesystem: ls -1 root listing, cat of root files,
    find for package discovery."""

    def __init__(self, files, find_outputs=None):
        self.files = files  # root name -> content
        self.find_outputs = find_outputs or {}  # find base -> output
        self.commands = []

    def execute_command(self, cmd, workdir=None, **kwargs):
        self.commands.append(cmd)
        if cmd.startswith("ls -1"):
            return {"success": True, "exit_code": 0, "output": "\n".join(sorted(self.files))}
        if cmd.startswith("cat "):
            name = cmd.split("cat ", 1)[1].split()[0].rsplit("/", 1)[-1]
            if name in self.files:
                return {"success": True, "exit_code": 0, "output": self.files[name]}
            return {"success": False, "exit_code": 1, "output": "No such file"}
        if cmd.startswith("find "):
            base = cmd.split()[1]
            return {"success": True, "exit_code": 0, "output": self.find_outputs.get(base, "")}
        return {"success": True, "exit_code": 0, "output": ""}


def _analyzer(orch):
    from sag.tools.internal.project_analyzer import ProjectAnalyzerTool

    tool = ProjectAnalyzerTool.__new__(ProjectAnalyzerTool)  # skip __init__ wiring
    tool.docker_orchestrator = orch
    return tool


def test_analyzer_persists_python_manifest_keys(monkeypatch):
    import sag.tools.internal.build_preflight as bp

    captured = {}
    monkeypatch.setattr(
        bp,
        "write_build_requirements",
        lambda orch, data: (captured.update(data), True)[1],
    )
    orch = PythonProjectOrch(
        files={"pyproject.toml": '[project]\nname = "foo"\nrequires-python = ">=3.9"\n'},
        find_outputs={"/workspace/proj/src": "/workspace/proj/src/foo/__init__.py\n"},
    )
    tool = _analyzer(orch)
    analysis = {}
    tool._analyze_python_project("/workspace/proj", analysis)
    tool._persist_build_requirements("/workspace/proj", analysis)

    assert captured["python_version"] == "3.13"  # newest satisfying >=3.9
    assert captured["python_constraint"] == ">=3.9"
    assert captured["python_installer"] == "pip"
    # Bug #13 defect 3: no extras declared -> plain editable install, and the
    # missing-test-extras note rides the manifest so setup_env narrates it.
    assert captured["python_install_commands"] == ["{venv}/bin/python -m pip install -e ."]
    assert captured["python_install_note"] == ("no test extras declared — test deps may be missing")
    assert captured["python_packages"] == ["foo"]
    assert captured["python_venv"].endswith("/.venv")
    assert captured["has_c_extensions"] is False
    assert captured["test_hints"] == {"pytest_args": None, "test_deps": []}
    # java keys stay on the same handoff manifest (spec Component 1: same file)
    assert "java_version" in captured


def test_analyzer_persists_pep735_test_dependency_command():
    orch = PythonProjectOrch(
        files={"pyproject.toml": PARAMIKO_PEP735_PYPROJECT},
        find_outputs={"/workspace/paramiko": "/workspace/paramiko/paramiko/__init__.py\n"},
    )
    tool = _analyzer(orch)
    analysis = {}

    tool._analyze_python_project("/workspace/paramiko", analysis)

    config = analysis["python_config"]
    assert config["python_install_commands"][0] == ("{venv}/bin/python -m pip install -e .")
    assert shlex.split(config["python_install_commands"][1])[-4:] == [
        "invoke>=2.2.1",
        "pytest-relaxed>=2",
        "icecream>=2.1",
        "pytest-xdist>=3",
    ]
    assert config["python_install_note"] is None


def test_analyzer_test_hints_are_metadata_only():
    orch = PythonProjectOrch(
        files={
            "setup.py": 'setup(name="legacy", python_requires=">=3.8")',
            "tox.ini": (
                "[tox]\nenvlist = py38\n\n"
                "[testenv]\ndeps =\n    pytest\n    pytest-cov\n"
                "commands = pytest {posargs} -q tests/\n"
            ),
            "setup.cfg": "[options.extras_require]\ntest =\n    mock\n",
        },
        find_outputs={"/workspace/legacy": "/workspace/legacy/legacy/__init__.py\n"},
    )
    tool = _analyzer(orch)
    analysis = {}
    tool._analyze_python_project("/workspace/legacy", analysis)

    config = analysis["python_config"]
    assert config["test_hints"]["test_deps"] == ["pytest", "pytest-cov", "mock"]
    assert config["test_hints"]["pytest_args"] == "-q tests/"  # {posargs} stripped
    assert config["python_installer"] == "pip"  # bare setup.py rung
    # Bug #13 defect 3: the setup.cfg-declared 'test' extra IS real — install it.
    assert config["python_install_commands"] == ["{venv}/bin/python -m pip install -e '.[test]'"]
    assert config["python_packages"] == ["legacy"]
    # tox is READ-ONLY metadata (settled decision): never executed
    assert not any(c.strip().startswith(("tox", "nox")) for c in orch.commands)


def test_analyzer_detects_c_extension_markers():
    orch = PythonProjectOrch(
        files={
            "setup.py": (
                "from setuptools import setup, Extension\n"
                "setup(ext_modules=[Extension('x._x', ['x/_x.c'])])\n"
            ),
        },
    )
    tool = _analyzer(orch)
    analysis = {}
    tool._analyze_python_project("/workspace/cext", analysis)
    assert analysis["python_config"]["has_c_extensions"] is True
