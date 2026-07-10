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

Bug #8 (apache/libcloud live probe) extends (a) into an ALWAYS-on gate: the
flat-layout probe listed repo-support dirs (contrib/, demos/, integration/,
pylint_plugins/ — each carries an __init__.py) as manifest packages, none of
them was ever installed, and the imports rung required ALL manifest names ->
false BLOCKED on a good build. Covered at the end of this file: whenever the
project's own installed record is non-empty its FULL name set is the import
target list (never a manifest-narrowed subset — flat-layout ranking can drop
genuine installed siblings, mercurial shape; else manifest as before), junk
names warn instead of blocking, and discover_packages ranks flat-layout
candidates by the declared project name.

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
    project_name_from_pyproject,
    project_name_from_setup_py,
)

_TOOLING_NAMES = ("pip", "setuptools", "wheel", "pkg_resources", "_distutils_hack")


def _manifest(**overrides):
    """pyyaml-shaped manifest: discovery found NO packages (lib/ layout)."""
    data = {
        "python_version": "3.12",
        "python_constraint": ">=3.8",
        "python_installer": "pip",
        "python_install_commands": ["{venv}/bin/python -m pip install -e ."],
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


def test_installed_record_outranks_a_disjoint_manifest():
    # Bug #8 flips the old "manifest wins" rule: the installed record is now
    # ALWAYS consulted. A manifest name with no installed counterpart is a
    # junk discovery; with no intersection at all, the installed names ARE
    # the import targets and the junk surfaces as a visible warning.
    orch = TopLevelOrch(manifest=_manifest(python_packages=["declared"]))
    result = _validate(orch)
    imports = _import_commands(orch)
    assert any('"import yaml"' in c for c in imports)
    assert any('"import _yaml"' in c for c in imports)
    assert not any('"import declared"' in c for c in imports)
    assert any("top_level.txt" in c for c in orch.commands)
    assert any(
        "discovered but not installed" in w and "declared" in w
        for w in result["evidence"]["warnings"]
    )


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


# ---------------------------------------------------------------------------
# Bug #8 (apache/libcloud live probe): junk flat-layout discoveries must
# never turn the imports rung into a false BLOCKED
# ---------------------------------------------------------------------------

# The live repro shape: repo-support dirs each carry an __init__.py, so
# discovery listed them as manifest packages; only the real package was ever
# installed (its record's top_level.txt says so). The direct_url deliberately
# points at /workspace/pyyaml: TopLevelOrch's harness project dir is
# /workspace/pyyaml regardless of the distribution under test, and the PEP
# 610 ladder selects the record whose direct_url targets THAT dir.
_LIBCLOUD_JUNK = ["contrib", "demos", "integration", "pylint_plugins"]
_LIBCLOUD_DIST = _dist(
    "apache_libcloud-3.8.0.dist-info",
    "libcloud\n",
    direct_url='{"url": "file:///workspace/pyyaml", "dir_info": {"editable": true}}',
)


def test_libcloud_junk_discoveries_warn_but_never_block():
    # (a) manifest ∩ installed drives the rung: only libcloud is probed, it
    # imports fine, and the junk names surface as a warning — verdict NOT
    # blocked (the live run said BLOCKED on contrib/demos/integration/
    # pylint_plugins, none of which was ever installed).
    orch = TopLevelOrch(
        dists=[_LIBCLOUD_DIST] + _DEP_DISTS + _TOOLING_DISTS,
        manifest=_manifest(python_packages=_LIBCLOUD_JUNK + ["libcloud"]),
    )
    result = _validate(orch)
    imports = _import_commands(orch)
    assert any('"import libcloud"' in c for c in imports)
    assert len(imports) == 1  # junk names are never import-probed
    details = result["evidence"]["fingerprint_details"]
    assert details["imports_ok"] is True
    assert details["import_failures"] == []
    assert result["success"] is True
    assert result["evidence_status"] != "blocked"
    warning = next(
        w for w in result["evidence"]["warnings"]
        if "discovered but not installed" in w
    )
    for name in _LIBCLOUD_JUNK:
        assert name in warning
    assert "libcloud," not in warning  # the real package is not junk


def test_all_junk_manifest_falls_back_to_installed_names():
    # (b) empty intersection: the installed project names alone are the
    # import targets.
    orch = TopLevelOrch(
        dists=[_LIBCLOUD_DIST] + _DEP_DISTS + _TOOLING_DISTS,
        manifest=_manifest(python_packages=list(_LIBCLOUD_JUNK)),
    )
    result = _validate(orch)
    imports = _import_commands(orch)
    assert any('"import libcloud"' in c for c in imports)
    assert len(imports) == 1
    assert result["evidence"]["fingerprint_details"]["imports_ok"] is True
    assert result["evidence_status"] != "blocked"


def test_nothing_installed_keeps_manifest_import_semantics():
    # (c) no project record in site-packages at all: manifest packages remain
    # the import targets exactly as before — their failure is real evidence
    # (the environment truly is unusable), never masked by the gate.
    orch = TopLevelOrch(
        dists=[],
        manifest=_manifest(python_packages=["libcloud"]),
        import_ok=False,
    )
    result = _validate(orch)
    assert result["success"] is False
    assert result["evidence_status"] == "blocked"
    details = result["evidence"]["fingerprint_details"]
    assert details["imports_ok"] is False
    assert details["import_failures"] == ["libcloud"]


def test_installed_sibling_dropped_by_flat_ranking_is_still_probed():
    # Mercurial shape: flat mercurial/ + hgext/ + hgdemandimport/, ALL in the
    # project's own top_level.txt, setup(name='mercurial'). Discovery's
    # flat-layout ranking narrows the manifest to ['mercurial'] (the declared
    # name matches one candidate) — but the siblings ARE the project: the
    # validator must probe the FULL installed record, so a broken sibling
    # import is a real BLOCKED, never a silent success.
    project = _dist(
        "mercurial-6.5.dist-info",
        "mercurial\nhgext\nhgdemandimport\n",
        direct_url='{"url": "file:///workspace/pyyaml", "dir_info": {"editable": true}}',
    )
    orch = TopLevelOrch(
        dists=[project] + _DEP_DISTS + _TOOLING_DISTS,
        manifest=_manifest(python_packages=["mercurial"]),
        failing_imports={"hgext"},
    )
    result = _validate(orch)
    imports = _import_commands(orch)
    assert any('"import mercurial"' in c for c in imports)
    assert any('"import hgext"' in c for c in imports)
    assert any('"import hgdemandimport"' in c for c in imports)
    assert result["success"] is False
    assert result["evidence_status"] == "blocked"
    details = result["evidence"]["fingerprint_details"]
    assert details["imports_ok"] is False
    assert details["import_failures"] == ["hgext"]


def test_installed_siblings_are_all_probed_on_partial_intersection():
    # Same shape, healthy imports: every installed name is probed (the gate
    # re-expands matched -> installed), and a manifest narrowed by ranking
    # produces no junk warning — nothing was discovered-but-not-installed.
    project = _dist(
        "mercurial-6.5.dist-info",
        "mercurial\nhgext\nhgdemandimport\n",
        direct_url='{"url": "file:///workspace/pyyaml", "dir_info": {"editable": true}}',
    )
    orch = TopLevelOrch(
        dists=[project] + _DEP_DISTS + _TOOLING_DISTS,
        manifest=_manifest(python_packages=["mercurial"]),
    )
    result = _validate(orch)
    assert len(_import_commands(orch)) == 3
    assert result["evidence"]["fingerprint_details"]["imports_ok"] is True
    assert not any(
        "discovered but not installed" in w
        for w in result["evidence"]["warnings"]
    )


def test_fully_installed_manifest_needs_no_junk_warning():
    # Clean intersection (every manifest name installed): no junk warning,
    # both names probed.
    orch = TopLevelOrch(manifest=_manifest(python_packages=["yaml", "_yaml"]))
    result = _validate(orch)
    imports = _import_commands(orch)
    assert len(imports) == 2
    assert not any(
        "discovered but not installed" in w
        for w in result["evidence"]["warnings"]
    )
    assert result["evidence"]["fingerprint_details"]["imports_ok"] is True


def test_failed_import_of_an_installed_package_still_blocks():
    # The gate weakens nothing: an INSTALLED project package that fails to
    # import is still a real BLOCKED.
    orch = TopLevelOrch(
        dists=[_LIBCLOUD_DIST] + _DEP_DISTS + _TOOLING_DISTS,
        manifest=_manifest(python_packages=_LIBCLOUD_JUNK + ["libcloud"]),
        failing_imports={"libcloud"},
    )
    result = _validate(orch)
    assert result["success"] is False
    assert result["evidence_status"] == "blocked"
    assert result["evidence"]["fingerprint_details"]["import_failures"] == [
        "libcloud"
    ]


# ---------------------------------------------------------------------------
# Bug #14 (pyyaml live probe): OPTIONAL extension modules (_yaml) fail the
# imports rung on a perfectly usable environment — false BLOCKED
# ---------------------------------------------------------------------------

# The live repro shape: pyyaml's wheel statically lists top_level.txt as
# "yaml\n_yaml\n" whether or not the OPTIONAL libyaml C-extension was built.
# The suite ran 1287/1287 green and `import yaml` worked, but `import _yaml`
# failed -> "BLOCKED - Top-level package import failed: _yaml". A 1287-green
# suite proves the environment WAS usable: underscore-prefixed top-level
# names are accessory/extension modules and must route to the C-extension
# rung (PARTIAL), never BLOCK — as long as a real (non-underscore) project
# package imported.


def test_optional_extension_import_failure_is_partial_not_blocked():
    # yaml imports, _yaml fails: PARTIAL via the ext rung, never BLOCKED.
    orch = TopLevelOrch(failing_imports={"_yaml"})
    result = _validate(orch)
    details = result["evidence"]["fingerprint_details"]
    assert details["imports_ok"] is True  # the real package imported
    assert details["import_failures"] == []  # no BLOCKING failure
    assert details["ext_modules_ok"] is False  # the optional extension rung
    assert result["success"] is True
    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert result["evidence_status"] != "blocked"
    assert (
        "optional extension module(s) not importable: _yaml" in result["reason"]
    )
    # The environment was never called unusable.
    assert "not usable" not in result["reason"]


def test_non_underscore_import_failure_still_blocks_next_to_optional_one():
    # Rule (b) regression: ANY non-underscore failure keeps today's BLOCKED
    # semantics, and the evidence lists every failing name honestly.
    orch = TopLevelOrch(failing_imports={"yaml", "_yaml"})
    result = _validate(orch)
    assert result["success"] is False
    assert result["evidence_status"] == "blocked"
    details = result["evidence"]["fingerprint_details"]
    assert details["imports_ok"] is False
    assert sorted(details["import_failures"]) == ["_yaml", "yaml"]


def test_all_underscore_names_failing_with_nothing_else_is_blocked():
    # Rule (c): when EVERY installed top-level name is underscore-prefixed
    # and they all fail, nothing usable was verified — BLOCKED, never a
    # soft PARTIAL on zero evidence.
    project = _dist(
        "cffi_backend_only-1.0.dist-info",
        "_cffi_backend\n",
        direct_url='{"url": "file:///workspace/pyyaml", "dir_info": {"editable": true}}',
    )
    orch = TopLevelOrch(
        dists=[project] + _DEP_DISTS + _TOOLING_DISTS,
        failing_imports={"_cffi_backend"},
    )
    result = _validate(orch)
    assert result["success"] is False
    assert result["evidence_status"] == "blocked"
    details = result["evidence"]["fingerprint_details"]
    assert details["imports_ok"] is False
    assert details["import_failures"] == ["_cffi_backend"]


def test_underscore_failure_without_a_green_regular_import_is_blocked():
    # Rule (a)'s guard: the optional-extension demotion requires at least one
    # NON-underscore package to have imported. An underscore failure next to
    # only underscore successes verified nothing usable — BLOCKED.
    project = _dist(
        "underscores_only-1.0.dist-info",
        "_alpha\n_beta\n",
        direct_url='{"url": "file:///workspace/pyyaml", "dir_info": {"editable": true}}',
    )
    orch = TopLevelOrch(
        dists=[project] + _DEP_DISTS + _TOOLING_DISTS,
        failing_imports={"_beta"},
    )
    result = _validate(orch)
    assert result["success"] is False
    assert result["evidence_status"] == "blocked"
    assert result["evidence"]["fingerprint_details"]["import_failures"] == ["_beta"]


# ---------------------------------------------------------------------------
# (d) discover_packages ranks flat-layout candidates by the declared name
# ---------------------------------------------------------------------------

_LIBCLOUD_FLAT_FIND = "".join(
    f"/workspace/libcloud/{name}/__init__.py\n"
    for name in ("contrib", "demos", "integration", "pylint_plugins", "libcloud")
)


def test_discover_packages_flat_name_match_drops_junk_dirs():
    orch = PackageDirOrch(
        files={
            "/workspace/libcloud/setup.py": (
                "setup(\n    name='libcloud',\n    packages=['libcloud'],\n)\n"
            )
        },
        find_outputs={"/workspace/libcloud": _LIBCLOUD_FLAT_FIND},
    )
    assert discover_packages(orch, "/workspace/libcloud") == ["libcloud"]


def test_discover_packages_flat_name_match_is_import_normalized():
    # Distribution names use '-', import dirs use '_': My-Lib ~ my_lib.
    orch = PackageDirOrch(
        files={"/workspace/mylib/pyproject.toml": '[project]\nname = "My-Lib"\n'},
        find_outputs={
            "/workspace/mylib": (
                "/workspace/mylib/my_lib/__init__.py\n"
                "/workspace/mylib/contrib/__init__.py\n"
            )
        },
    )
    assert discover_packages(orch, "/workspace/mylib") == ["my_lib"]


def test_discover_packages_without_name_match_keeps_all_candidates():
    # apache-libcloud normalizes to apache_libcloud — no flat dir matches, so
    # every candidate is kept (a heuristic, never a deny-list; the
    # validator's installed-record gate is the guarantee, see above).
    orch = PackageDirOrch(
        files={
            "/workspace/libcloud/setup.py": "setup(name='apache-libcloud')\n"
        },
        find_outputs={"/workspace/libcloud": _LIBCLOUD_FLAT_FIND},
    )
    assert discover_packages(orch, "/workspace/libcloud") == [
        "contrib", "demos", "integration", "libcloud", "pylint_plugins",
    ]


def test_discover_packages_src_layout_is_never_ranked():
    # src-layout behavior unchanged: no ranking, both packages kept even
    # though only one matches the declared name.
    orch = PackageDirOrch(
        files={"/workspace/proj/pyproject.toml": '[project]\nname = "foo"\n'},
        find_outputs={
            "/workspace/proj/src": (
                "/workspace/proj/src/foo/__init__.py\n"
                "/workspace/proj/src/foo_helpers/__init__.py\n"
            )
        },
    )
    assert discover_packages(orch, "/workspace/proj") == ["foo", "foo_helpers"]


def test_discover_packages_single_flat_candidate_survives_name_mismatch():
    # A lone flat candidate is kept even when it does not match the declared
    # name — ranking only ever DROPS junk next to a name-match, it never
    # empties the discovery.
    orch = PackageDirOrch(
        files={
            "/workspace/proj/pyproject.toml": '[project]\nname = "something-else"\n'
        },
        find_outputs={"/workspace/proj": "/workspace/proj/bar/__init__.py\n"},
    )
    assert discover_packages(orch, "/workspace/proj") == ["bar"]


# ---------------------------------------------------------------------------
# project-name parsers (pure functions)
# ---------------------------------------------------------------------------


def test_project_name_parsers_extract_the_declared_name():
    assert project_name_from_pyproject(
        '[project]\nname = "apache-libcloud"\nversion = "3.8.0"\n'
    ) == "apache-libcloud"
    assert project_name_from_pyproject(
        '[tool.poetry]\nname = "mylib"\n'
    ) == "mylib"
    assert project_name_from_setup_py("setup(\n    name='PyYAML',\n)") == "PyYAML"
    assert project_name_from_setup_py(
        "# name='old'\nsetup(name='new')"
    ) == "new"


def test_project_name_parsers_never_read_unrelated_keys():
    # A name key outside [project]/[tool.poetry] is not the project name.
    assert project_name_from_pyproject('[tool.other]\nname = "nope"\n') is None
    assert project_name_from_pyproject("[project]\nversion = '1.0'\n") is None
    # author_name= is not name= (word boundary), and no name at all is None.
    assert project_name_from_setup_py("setup(author_name='x')") is None
    assert project_name_from_setup_py("setup(packages=['x'])") is None
