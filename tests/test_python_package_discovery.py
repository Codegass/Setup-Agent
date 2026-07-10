# tests/test_python_package_discovery.py
"""Import-rung fallback + package_dir layouts (pyyaml re-probe bug #6).

pyyaml declares ``package_dir={'': 'lib'}``, which defeated the src/flat
probes in discover_packages: the manifest carried python_packages=[] and the
validator's imports rung — the STRONGEST evidence rung — silently vanished
(imports_ok=None with no trace in the report). Covered here:

(a) validation-time fallback: empty manifest packages -> import targets read
    from the venv's installed ``top_level.txt`` records, tooling names
    (pip/setuptools/wheel/pkg_resources/_distutils_hack) filtered and NEVER
    probed;
(b) discover_packages honors the project's declared package_dir mapping
    (setup.py / setup.cfg / pyproject.toml inline and table forms) and probes
    ``<dir>/<pkg>/__init__.py``;
(c) nothing importable at all -> imports_ok stays None BUT the skip surfaces
    as a visible warning in the build evidence, never silently.

Scripted-orchestrator house style: tests/test_python_verifier.py.
"""

import json

from sag.agent.physical_validator import PhysicalValidator
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.python_env import (
    discover_packages,
    package_dir_from_pyproject,
    package_dir_from_setup_cfg,
    package_dir_from_setup_py,
)

_TOOLING_NAMES = ("pip", "setuptools", "wheel", "pkg_resources", "_distutils_hack")


def _manifest(**overrides):
    """pyyaml-shaped manifest: discovery found NO packages (lib/ layout)."""
    data = {
        "python_version": "3.12",
        "python_constraint": ">=3.8",
        "python_installer": "pip",
        "python_install_commands": ["{venv}/bin/pip install -e ."],
        "python_packages": [],
        "python_venv": "/workspace/pyyaml/.venv",
        "has_c_extensions": False,
    }
    data.update(overrides)
    return data


class TopLevelOrch:
    """Evidence-ladder container for a pyyaml-style project: no manifest
    packages, import targets only discoverable via installed top_level.txt."""

    def __init__(self, *, top_level="yaml\n_yaml\n", import_ok=True,
                 manifest=None):
        self.top_level = top_level
        self.import_ok = import_ok
        self.manifest = manifest if manifest is not None else _manifest()
        self.commands = []

    def execute_command(self, cmd, workdir=None, **kwargs):
        self.commands.append(cmd)

        def res(ok, output=""):
            return {"success": ok, "exit_code": 0 if ok else 1, "output": output}

        c = cmd.strip()
        if c == f"cat {REQUIREMENTS_PATH}":
            return res(True, json.dumps(self.manifest))
        if "python3 --version" in c:
            return res(True, "Python 3.12.0")
        if "java -version" in c:
            return res(False, "java: command not found")
        if c.startswith("test -f "):
            return res(c.endswith("/setup.py"))  # python project via setup.py
        if c.startswith("test -d "):
            return res(c.split()[2].endswith("/.venv"))  # only the venv exists
        if "pip check" in c:
            return res(True, "No broken requirements found.")
        if "top_level.txt" in c:
            return res(True, self.top_level)
        if '-c "import ' in c:
            return res(
                self.import_ok,
                "" if self.import_ok else "ModuleNotFoundError",
            )
        if "compileall" in c:
            return res(True)
        if "__pycache__" in c and "wc -l" in c:
            return res(True, "10")
        if "'*.py'" in c and "wc -l" in c:
            return res(True, "10")
        if "'*.jar'" in c or "'*.class'" in c or "'*.so'" in c:
            return res(True, "0")
        return res(True, "")


def _validate(orch):
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace")
    return validator.validate_build_status("pyyaml")


def _import_commands(orch):
    return [c for c in orch.commands if '-c "import ' in c]


# ---------------------------------------------------------------------------
# (a) top_level.txt fallback drives the imports rung
# ---------------------------------------------------------------------------


def test_top_level_fallback_drives_import_checks_when_manifest_empty():
    orch = TopLevelOrch(
        top_level="yaml\n_yaml\npip\nsetuptools\nwheel\npkg_resources\n_distutils_hack\n"
    )
    result = _validate(orch)
    imports = _import_commands(orch)
    assert any('"import yaml"' in c for c in imports)
    assert any('"import _yaml"' in c for c in imports)
    # Tooling names are filtered — never probed as project evidence.
    for name in _TOOLING_NAMES:
        assert not any(f'"import {name}"' in c for c in imports)
    details = result["evidence"]["fingerprint_details"]
    assert details["imports_ok"] is True
    assert details["import_failures"] == []
    # The rung genuinely ran: no skip warning in the evidence.
    assert not any(
        "imports rung skipped" in w for w in result["evidence"]["warnings"]
    )


def test_top_level_fallback_reads_site_packages_dist_info():
    orch = TopLevelOrch()
    _validate(orch)
    probes = [c for c in orch.commands if "top_level.txt" in c]
    assert probes, "the venv's installed top_level.txt records were never read"
    assert any("site-packages" in c and "/.venv/" in c for c in probes)


def test_top_level_fallback_failed_import_still_blocks():
    # The fallback rung keeps FULL ladder strength: a failed import is BLOCKED.
    orch = TopLevelOrch(import_ok=False)
    result = _validate(orch)
    assert result["success"] is False
    assert result["evidence_status"] == "blocked"
    details = result["evidence"]["fingerprint_details"]
    assert details["imports_ok"] is False
    assert sorted(details["import_failures"]) == ["_yaml", "yaml"]


def test_manifest_packages_still_win_over_fallback():
    orch = TopLevelOrch(manifest=_manifest(python_packages=["declared"]))
    _validate(orch)
    imports = _import_commands(orch)
    assert any('"import declared"' in c for c in imports)
    assert not any('"import yaml"' in c for c in imports)
    # No fallback probe needed when the manifest already declares packages.
    assert not any("top_level.txt" in c for c in orch.commands)


# ---------------------------------------------------------------------------
# (c) nothing importable -> imports_ok stays None, skip is VISIBLE
# ---------------------------------------------------------------------------


def test_nothing_importable_keeps_none_with_visible_warning():
    orch = TopLevelOrch(top_level="")
    result = _validate(orch)
    details = result["evidence"]["fingerprint_details"]
    assert details["imports_ok"] is None
    assert _import_commands(orch) == []
    assert any(
        "imports rung skipped: no importable package detected" in w
        for w in result["evidence"]["warnings"]
    )
    # No invented failure: the other rungs still carry the verdict.
    assert result["success"] is True


def test_only_tooling_top_level_is_treated_as_nothing_importable():
    orch = TopLevelOrch(top_level="pip\nsetuptools\nwheel\npkg_resources\n_distutils_hack\n")
    result = _validate(orch)
    assert result["evidence"]["fingerprint_details"]["imports_ok"] is None
    assert _import_commands(orch) == []
    assert any(
        "imports rung skipped" in w for w in result["evidence"]["warnings"]
    )


# ---------------------------------------------------------------------------
# (b) discover_packages honors the declared package_dir mapping
# ---------------------------------------------------------------------------


class PackageDirOrch:
    """find/cat script (house style: test_python_requirements.LayoutOrch)
    for a project whose packages live under a declared package_dir."""

    def __init__(self, files=None, find_outputs=None):
        self.files = files or {}
        self.find_outputs = find_outputs or {}
        self.commands = []

    def execute_command(self, cmd, workdir=None, **kwargs):
        self.commands.append(cmd)
        if cmd.startswith("find "):
            base = cmd.split()[1]
            return {"success": True, "exit_code": 0,
                    "output": self.find_outputs.get(base, "")}
        if cmd.startswith("cat "):
            path = cmd.split()[1]
            content = self.files.get(path)
            if content is None:
                return {"success": False, "exit_code": 1, "output": ""}
            return {"success": True, "exit_code": 0, "output": content}
        return {"success": True, "exit_code": 0, "output": ""}


_LIB_FIND = (
    "/workspace/pyyaml/lib/yaml/__init__.py\n"
    "/workspace/pyyaml/lib/_yaml/__init__.py\n"
)


def test_discover_packages_setup_py_package_dir_lib_layout():
    orch = PackageDirOrch(
        files={
            "/workspace/pyyaml/setup.py": (
                "setup(\n    name='PyYAML',\n"
                "    package_dir={'': 'lib'},\n    packages=['yaml'],\n)\n"
            )
        },
        find_outputs={"/workspace/pyyaml/lib": _LIB_FIND},
    )
    assert discover_packages(orch, "/workspace/pyyaml") == ["_yaml", "yaml"]


def test_discover_packages_setup_cfg_package_dir():
    orch = PackageDirOrch(
        files={
            "/workspace/pyyaml/setup.cfg": (
                "[options]\npackage_dir =\n    = lib\npackages = find:\n"
            )
        },
        find_outputs={"/workspace/pyyaml/lib": _LIB_FIND},
    )
    assert discover_packages(orch, "/workspace/pyyaml") == ["_yaml", "yaml"]


def test_discover_packages_pyproject_inline_package_dir():
    orch = PackageDirOrch(
        files={
            "/workspace/pyyaml/pyproject.toml": (
                '[tool.setuptools]\npackage-dir = {"" = "lib"}\n'
            )
        },
        find_outputs={"/workspace/pyyaml/lib": _LIB_FIND},
    )
    assert discover_packages(orch, "/workspace/pyyaml") == ["_yaml", "yaml"]


def test_discover_packages_pyproject_table_package_dir():
    orch = PackageDirOrch(
        files={
            "/workspace/pyyaml/pyproject.toml": (
                '[tool.setuptools.package-dir]\n"" = "lib"\n'
            )
        },
        find_outputs={"/workspace/pyyaml/lib": _LIB_FIND},
    )
    assert discover_packages(orch, "/workspace/pyyaml") == ["_yaml", "yaml"]


def test_discover_packages_declared_mapping_wins_over_flat_layout():
    orch = PackageDirOrch(
        files={"/workspace/pyyaml/setup.py": "package_dir={'': 'lib'}\n"},
        find_outputs={
            "/workspace/pyyaml/lib": _LIB_FIND,
            "/workspace/pyyaml": "/workspace/pyyaml/stale/__init__.py\n",
        },
    )
    assert discover_packages(orch, "/workspace/pyyaml") == ["_yaml", "yaml"]


def test_discover_packages_without_mapping_keeps_existing_ladder():
    # No package_dir declared anywhere -> src probe still wins as before.
    orch = PackageDirOrch(
        find_outputs={"/workspace/proj/src": "/workspace/proj/src/foo/__init__.py\n"},
    )
    assert discover_packages(orch, "/workspace/proj") == ["foo"]


def test_discover_packages_dot_mapping_is_ignored():
    # package_dir={'': '.'} is the flat layout, not a new base to invent.
    orch = PackageDirOrch(
        files={"/workspace/proj/setup.py": "package_dir={'': '.'}\n"},
        find_outputs={"/workspace/proj": "/workspace/proj/bar/__init__.py\n"},
    )
    assert discover_packages(orch, "/workspace/proj") == ["bar"]


# ---------------------------------------------------------------------------
# package_dir parsers (pure functions)
# ---------------------------------------------------------------------------


def test_package_dir_parsers_extract_the_root_mapping():
    assert package_dir_from_setup_py("package_dir={'': 'lib'},") == "lib"
    assert package_dir_from_setup_py('package_dir = {"": "lib"}') == "lib"
    assert package_dir_from_setup_py("packages=['yaml']") is None
    assert package_dir_from_setup_cfg("[options]\npackage_dir =\n    = lib\n") == "lib"
    assert package_dir_from_setup_cfg("[options]\npackage_dir = =lib\n") == "lib"
    assert package_dir_from_setup_cfg("[options]\npackages = find:\n") is None
    assert package_dir_from_pyproject('[tool.setuptools]\npackage-dir = {"" = "lib"}\n') == "lib"
    assert package_dir_from_pyproject('[tool.setuptools.package-dir]\n"" = "lib"\n') == "lib"
    assert package_dir_from_pyproject("[tool.setuptools]\nzip-safe = false\n") is None


def test_package_dir_parsers_ignore_named_package_mappings():
    # A mapping WITHOUT the '' root key relocates single packages, not the
    # import root — nothing to probe as a base dir.
    assert package_dir_from_setup_py("package_dir={'yaml': 'lib/yaml'}") is None
    assert package_dir_from_pyproject(
        '[tool.setuptools]\npackage-dir = {"yaml" = "lib/yaml"}\n'
    ) is None
