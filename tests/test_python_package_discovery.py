# tests/test_python_package_discovery.py
"""Import-rung fallback + package_dir layouts (pyyaml re-probe bug #6).

pyyaml declares ``package_dir={'': 'lib'}``, which defeated the src/flat
probes in discover_packages: the manifest carried python_packages=[] and the
validator's imports rung — the STRONGEST evidence rung — silently vanished
(imports_ok=None with no trace in the report). Covered here:

(a) validation-time fallback: empty manifest packages -> import targets read
    from the PROJECT'S OWN installed record ONLY — the dist-info whose PEP 610
    ``direct_url.json`` points back at the project dir (the ``pip install -e .``
    / ``pip install .`` record), else the record whose distribution name
    PEP 503-matches the project dir name. Third-party dependency records
    (requests, urllib3, Cython, ...) sit in the SAME site-packages and are
    NEVER import-probed as project evidence: a dependency's broken import must
    not BLOCK the project, and a dependency's working import must not fake
    imports_ok=True when the project's own install failed;
(b) discover_packages honors the project's declared package_dir mapping
    (setup.py / setup.cfg / pyproject.toml inline and table forms) and probes
    ``<dir>/<pkg>/__init__.py``;
(c) nothing importable at all -> imports_ok stays None BUT the skip surfaces
    as a visible warning in the build evidence, never silently.

Scripted-orchestrator house style: tests/test_python_verifier.py.
"""

import json

from sag.agent.physical_validator import (
    PhysicalValidator,
    _dist_record_matches,
    _normalize_dist_name,
)
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


_SITE = "/workspace/pyyaml/.venv/lib/python3.12/site-packages"


def _dist(record, top_level="", direct_url=None):
    """One installed distribution record: ``*.dist-info`` / ``*.egg-info``
    dir basename, its top_level.txt content, its direct_url.json (or None
    for an index install — deps from PyPI carry no direct_url.json)."""
    return {"record": record, "top_level": top_level, "direct_url": direct_url}


_PROJECT_DIST = _dist(
    "pyyaml-6.0.dist-info",
    "yaml\n_yaml\n",
    direct_url='{"url": "file:///workspace/pyyaml", "dir_info": {"editable": true}}',
)
# The reviewer's live repro shape: the project's own dependencies and the
# install tooling ALL carry top_level.txt records in the same site-packages.
# None of them is the project; none may be import-probed as project evidence.
_DEP_DISTS = [
    _dist("requests-2.32.0.dist-info", "requests\n"),
    _dist("urllib3-2.2.0.dist-info", "urllib3\n"),
    _dist("idna-3.7.dist-info", "idna\n"),
    _dist("certifi-2026.1.1.dist-info", "certifi\n"),
    _dist("charset_normalizer-3.4.0.dist-info", "charset_normalizer\n"),
    _dist("Cython-3.0.10.dist-info", "Cython\ncython\npyximport\n"),
]
_TOOLING_DISTS = [
    _dist("pip-24.0.dist-info", "pip\n"),
    _dist("setuptools-70.0.0.dist-info", "setuptools\npkg_resources\n_distutils_hack\n"),
    _dist("wheel-0.43.0.dist-info", "wheel\n"),
]
_DEP_TOP_LEVEL = (
    "requests", "urllib3", "idna", "certifi", "charset_normalizer",
    "Cython", "cython", "pyximport",
)


class TopLevelOrch:
    """Evidence-ladder container for a pyyaml-style project: no manifest
    packages, import targets only discoverable from the installed records.
    site-packages realistically holds project + dependency + tooling
    dist-infos side by side."""

    def __init__(self, *, dists=None, import_ok=True, failing_imports=(),
                 manifest=None):
        self.dists = (
            [_PROJECT_DIST] + _DEP_DISTS + _TOOLING_DISTS
            if dists is None
            else dists
        )
        self.import_ok = import_ok
        self.failing_imports = set(failing_imports)
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
        if c.startswith("grep") and "direct_url.json" in c:
            needle = c.split("'")[1]
            hits = [
                f"{_SITE}/{d['record']}/direct_url.json"
                for d in self.dists
                if d["direct_url"] and needle in d["direct_url"]
            ]
            return res(bool(hits), "\n".join(hits))
        if c.startswith("find") and "dist-info" in c:
            return res(
                True, "\n".join(f"{_SITE}/{d['record']}" for d in self.dists)
            )
        if c.startswith("cat") and c.split()[1].endswith("/top_level.txt"):
            record = c.split()[1].rsplit("/", 2)[-2]
            for d in self.dists:
                if d["record"] == record:
                    return res(True, d["top_level"])
            return res(False)
        if c.startswith("test -f "):
            return res(c.endswith("/setup.py"))  # python project via setup.py
        if c.startswith("test -d "):
            return res(c.split()[2].endswith("/.venv"))  # only the venv exists
        if "pip check" in c:
            return res(True, "No broken requirements found.")
        if '-c "import ' in c:
            module = c.split('"import ')[1].split('"')[0]
            ok = self.import_ok and module not in self.failing_imports
            return res(ok, "" if ok else f"ModuleNotFoundError: {module}")
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
# (a) the PROJECT's own top_level.txt record drives the imports rung —
#     third-party dependency records are never project evidence
# ---------------------------------------------------------------------------


def test_top_level_fallback_drives_import_checks_when_manifest_empty():
    orch = TopLevelOrch()
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


def test_third_party_dependency_records_are_never_probed():
    # Reject (a): deps' top_level.txt records sit in the SAME site-packages;
    # none of their names may be import-probed as project evidence.
    orch = TopLevelOrch()
    _validate(orch)
    imports = _import_commands(orch)
    for name in _DEP_TOP_LEVEL:
        assert not any(f'"import {name}"' in c for c in imports), name
    assert len(imports) == 2  # yaml + _yaml, nothing else


def test_broken_third_party_import_does_not_block_the_project():
    # Reviewer's live repro: Cython (and requests) fail to import while the
    # project itself imports fine — the verdict must NOT be BLOCKED on a
    # dependency's name.
    orch = TopLevelOrch(failing_imports={"Cython", "requests"})
    result = _validate(orch)
    details = result["evidence"]["fingerprint_details"]
    assert details["imports_ok"] is True
    assert details["import_failures"] == []
    assert result["success"] is True
    assert result["evidence_status"] != "blocked"


def test_working_dep_imports_are_not_project_evidence_when_install_failed():
    # Symmetric false positive: the project's own install failed (no project
    # record in site-packages), its deps import fine -> imports_ok must stay
    # None with a VISIBLE skip, never True on third-party evidence.
    orch = TopLevelOrch(dists=_DEP_DISTS + _TOOLING_DISTS)
    result = _validate(orch)
    details = result["evidence"]["fingerprint_details"]
    assert details["imports_ok"] is None
    assert _import_commands(orch) == []
    assert any(
        "imports rung skipped" in w for w in result["evidence"]["warnings"]
    )


def test_direct_url_record_wins_even_when_the_dist_name_differs():
    # `pip install -e .` writes a PEP 610 direct_url.json pointing at the
    # project dir; that record IS the project even when the distribution
    # name (mylib) differs from the checkout dir name (pyyaml).
    project = _dist(
        "mylib-1.0.dist-info",
        "mylib\n",
        direct_url='{"url": "file:///workspace/pyyaml", "dir_info": {"editable": true}}',
    )
    orch = TopLevelOrch(dists=_DEP_DISTS + [project] + _TOOLING_DISTS)
    _validate(orch)
    imports = _import_commands(orch)
    assert any('"import mylib"' in c for c in imports)
    assert len(imports) == 1


def test_symlink_resolved_direct_url_still_selects_local_install():
    # pip records the REALPATH in direct_url.json (live repro: /tmp project
    # dir -> file:///private/tmp/... on macOS), so the exact-tree grep can
    # miss. The PEP 610 dir_info marker still identifies the local-directory
    # install as the project — index-installed deps never carry
    # direct_url.json at all.
    project = _dist(
        "mylib-1.0.dist-info",
        "mylib\n",
        direct_url=(
            '{"dir_info": {"editable": true},'
            ' "url": "file:///private/workspace/pyyaml"}'
        ),
    )
    orch = TopLevelOrch(dists=_DEP_DISTS + [project] + _TOOLING_DISTS)
    _validate(orch)
    imports = _import_commands(orch)
    assert any('"import mylib"' in c for c in imports)
    assert len(imports) == 1


def test_name_match_selects_the_project_record_without_direct_url():
    # Legacy install (no direct_url.json anywhere): the record whose
    # distribution name PEP 503-matches the project dir (PyYAML ~ pyyaml)
    # is the project; deps are still never probed.
    project = _dist("PyYAML-6.0.dist-info", "yaml\n_yaml\n")
    orch = TopLevelOrch(dists=_DEP_DISTS + [project] + _TOOLING_DISTS)
    result = _validate(orch)
    imports = _import_commands(orch)
    assert any('"import yaml"' in c for c in imports)
    assert any('"import _yaml"' in c for c in imports)
    assert len(imports) == 2
    assert result["evidence"]["fingerprint_details"]["imports_ok"] is True


def test_name_match_accepts_the_egg_info_record():
    # `python setup.py install` writes *.egg-info instead of *.dist-info.
    project = _dist("PyYAML.egg-info", "yaml\n_yaml\n")
    orch = TopLevelOrch(dists=_DEP_DISTS + [project])
    _validate(orch)
    imports = _import_commands(orch)
    assert any('"import yaml"' in c for c in imports)
    assert len(imports) == 2


def test_top_level_fallback_reads_site_packages_dist_info():
    orch = TopLevelOrch()
    _validate(orch)
    probes = [c for c in orch.commands if "top_level.txt" in c]
    assert probes, "the venv's installed top_level.txt records were never read"
    assert any("site-packages" in c and "/.venv/" in c for c in probes)


def test_top_level_fallback_failed_import_still_blocks():
    # The fallback rung keeps FULL ladder strength: a failed PROJECT import
    # is BLOCKED.
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
    orch = TopLevelOrch(dists=[])
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
    # Second deny-list layer: even when the PROJECT's own record lists only
    # tooling names, pip/setuptools are never probed as project evidence.
    project = _dist(
        "pyyaml-6.0.dist-info",
        "pip\nsetuptools\nwheel\npkg_resources\n_distutils_hack\n",
        direct_url='{"url": "file:///workspace/pyyaml", "dir_info": {}}',
    )
    orch = TopLevelOrch(dists=[project] + _TOOLING_DISTS)
    result = _validate(orch)
    assert result["evidence"]["fingerprint_details"]["imports_ok"] is None
    assert _import_commands(orch) == []
    assert any(
        "imports rung skipped" in w for w in result["evidence"]["warnings"]
    )


# ---------------------------------------------------------------------------
# record-selection helpers (pure functions)
# ---------------------------------------------------------------------------


def test_dist_record_name_matching_is_pep503_normalized():
    assert _normalize_dist_name("PyYAML") == "pyyaml"
    assert _normalize_dist_name("charset_normalizer") == "charset-normalizer"
    assert _normalize_dist_name("my.lib") == "my-lib"
    assert _dist_record_matches(f"{_SITE}/PyYAML-6.0.dist-info", "pyyaml")
    assert _dist_record_matches(f"{_SITE}/PyYAML.egg-info", "pyyaml")
    assert _dist_record_matches(f"{_SITE}/PyYAML-6.0-py3.12.egg-info", "pyyaml")
    assert _dist_record_matches(f"{_SITE}/my_lib-1.0.dist-info", "my-lib")
    assert _dist_record_matches(f"{_SITE}/my.lib-1.0.dist-info", "my_lib")


def test_dist_record_name_matching_rejects_prefixed_dependencies():
    # requests-toolbelt is NOT project requests: after the name segment only
    # a version (leading digit) may follow. A dep's record never matches.
    assert not _dist_record_matches(
        f"{_SITE}/requests_toolbelt-1.0.dist-info", "requests"
    )
    assert not _dist_record_matches(f"{_SITE}/Cython-3.0.10.dist-info", "pyyaml")
    assert not _dist_record_matches(f"{_SITE}/README.txt", "readme")


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


def test_setup_py_commented_package_dir_is_ignored():
    # A commented-out mapping is not the live declaration.
    assert package_dir_from_setup_py("# package_dir={'': 'old'}\n") is None
    assert package_dir_from_setup_py(
        "    # package_dir={'': 'old'}\n    package_dir={'': 'lib'},\n"
    ) == "lib"


def test_package_dir_parsers_ignore_named_package_mappings():
    # A mapping WITHOUT the '' root key relocates single packages, not the
    # import root — nothing to probe as a base dir.
    assert package_dir_from_setup_py("package_dir={'yaml': 'lib/yaml'}") is None
    assert package_dir_from_pyproject(
        '[tool.setuptools]\npackage-dir = {"yaml" = "lib/yaml"}\n'
    ) is None
