# tests/test_python_package_discovery.py
"""Read-only distribution ownership + package_dir layouts.

pyyaml declares ``package_dir={'': 'lib'}``, which defeated the src/flat
probes in discover_packages: the manifest carried python_packages=[] and the
validator's legacy imports rung. The judge now uses installed records only as
ownership facts and leaves importability unknown without a producer receipt.
Covered here:

(a) validation-time fallback: empty manifest packages -> ownership names read
    from the PROJECT'S OWN installed record ONLY — the dist-info whose PEP 610
    ``direct_url.json`` points back at the project dir (the ``pip install -e .``
    / ``pip install .`` record), else the record whose distribution name
    PEP 503-matches the project dir name. Third-party dependency records
    (requests, urllib3, Cython, ...) sit in the SAME site-packages and are
    NEVER executed as project evidence;
(b) discover_packages honors the project's declared package_dir mapping
    (setup.py / setup.cfg / pyproject.toml inline and table forms) and probes
    ``<dir>/<pkg>/__init__.py``;
(c) nothing importable at all -> imports_ok stays None BUT the skip surfaces
    as a visible warning in the build evidence, never silently.

Bug #8 (apache/libcloud live probe) extends (a) into an ownership gate: the
flat-layout probe listed repo-support dirs (contrib/, demos/, integration/,
pylint_plugins/ — each carries an __init__.py) as manifest packages, none of
them was ever installed, and the imports rung required ALL manifest names ->
false BLOCKED on a good build. Covered at the end of this file: the project's
installed record identifies its full owned name set, junk names warn without
being executed, and discover_packages ranks flat-layout candidates by the
declared project name.

Scripted-orchestrator house style: tests/test_python_verifier.py.
"""

import json

from sag.agent.evidence_publications import BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID
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
from tests.container_evidence_fakes import (
    add_published_mutable_json,
    strict_published_evidence,
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
    "requests",
    "urllib3",
    "idna",
    "certifi",
    "charset_normalizer",
    "Cython",
    "cython",
    "pyximport",
)


class TopLevelOrch:
    """Evidence-ladder container for a pyyaml-style project: no manifest
    packages, import targets only discoverable from the installed records.
    site-packages realistically holds project + dependency + tooling
    dist-infos side by side."""

    def __init__(self, *, dists=None, import_ok=True, failing_imports=(), manifest=None):
        self.dists = [_PROJECT_DIST] + _DEP_DISTS + _TOOLING_DISTS if dists is None else dists
        self.import_ok = import_ok
        self.failing_imports = set(failing_imports)
        self.manifest = manifest if manifest is not None else _manifest()
        self.commands = []
        self.evidence_store = strict_published_evidence(
            self,
            run_id="run-python-package-discovery",
            target_sha="a" * 40,
            run_pin=False,
        )
        add_published_mutable_json(
            self,
            self.evidence_store,
            path=REQUIREMENTS_PATH,
            record_kind="build_requirements",
            record_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            logical_artifact_id=BUILD_REQUIREMENTS_LOGICAL_ARTIFACT_ID,
            payload=self.manifest,
        )

    def execute_command(self, cmd, workdir=None, **kwargs):
        self.commands.append(cmd)

        def res(ok, output=""):
            return {"success": ok, "exit_code": 0 if ok else 1, "output": output}

        c = cmd.strip()
        if "SAG_NAMED_JSON_RECORD_V1" in c:
            return self.evidence_store(cmd, **kwargs)
        if c in (f"cat {REQUIREMENTS_PATH}", f"cat -- {REQUIREMENTS_PATH}"):
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
            return res(True, "\n".join(f"{_SITE}/{d['record']}" for d in self.dists))
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
# (a) the PROJECT's own top_level.txt record is read-only ownership evidence;
#     third-party dependency records are never project evidence
# ---------------------------------------------------------------------------


def test_project_distribution_ownership_is_read_without_import_execution():
    orch = TopLevelOrch()
    result = _validate(orch)

    probes = [command for command in orch.commands if "top_level.txt" in command]
    assert probes
    assert all("requests" not in command and "Cython" not in command for command in probes)
    assert _import_commands(orch) == []
    details = result["evidence"]["fingerprint_details"]
    assert details["imports_ok"] is None
    assert details["import_failures"] == []
    assert any(
        "package importability unknown: no producer receipt" in warning
        for warning in result["evidence"]["warnings"]
    )


def test_direct_url_selection_remains_a_read_only_ownership_probe():
    project = _dist(
        "mylib-1.0.dist-info",
        "mylib\n",
        direct_url='{"url": "file:///workspace/pyyaml", "dir_info": {"editable": true}}',
    )
    orch = TopLevelOrch(dists=_DEP_DISTS + [project] + _TOOLING_DISTS)

    result = _validate(orch)

    assert any("mylib-1.0.dist-info/top_level.txt" in command for command in orch.commands)
    assert _import_commands(orch) == []
    assert result["evidence"]["fingerprint_details"]["imports_ok"] is None


def test_name_matched_egg_info_selection_remains_read_only():
    project = _dist("PyYAML.egg-info", "yaml\n_yaml\n")
    orch = TopLevelOrch(dists=_DEP_DISTS + [project])

    _validate(orch)

    assert any("PyYAML.egg-info/top_level.txt" in command for command in orch.commands)
    assert _import_commands(orch) == []


def test_top_level_fallback_reads_site_packages_dist_info():
    orch = TopLevelOrch()
    _validate(orch)
    probes = [c for c in orch.commands if "top_level.txt" in c]
    assert probes, "the venv's installed top_level.txt records were never read"
    assert any("site-packages" in c and "/.venv/" in c for c in probes)


def test_installed_record_still_identifies_disjoint_manifest_names_as_junk():
    orch = TopLevelOrch(manifest=_manifest(python_packages=["declared"]))
    result = _validate(orch)
    assert _import_commands(orch) == []
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
        "package importability unknown" in w and "no project-owned names recorded" in w
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
        "package importability unknown" in w and "no project-owned names recorded" in w
        for w in result["evidence"]["warnings"]
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
    assert not _dist_record_matches(f"{_SITE}/requests_toolbelt-1.0.dist-info", "requests")
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
            return {"success": True, "exit_code": 0, "output": self.find_outputs.get(base, "")}
        if cmd.startswith("cat "):
            path = cmd.split()[1]
            content = self.files.get(path)
            if content is None:
                return {"success": False, "exit_code": 1, "output": ""}
            return {"success": True, "exit_code": 0, "output": content}
        return {"success": True, "exit_code": 0, "output": ""}


_LIB_FIND = "/workspace/pyyaml/lib/yaml/__init__.py\n" "/workspace/pyyaml/lib/_yaml/__init__.py\n"


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
            "/workspace/pyyaml/pyproject.toml": ('[tool.setuptools]\npackage-dir = {"" = "lib"}\n')
        },
        find_outputs={"/workspace/pyyaml/lib": _LIB_FIND},
    )
    assert discover_packages(orch, "/workspace/pyyaml") == ["_yaml", "yaml"]


def test_discover_packages_pyproject_table_package_dir():
    orch = PackageDirOrch(
        files={"/workspace/pyyaml/pyproject.toml": ('[tool.setuptools.package-dir]\n"" = "lib"\n')},
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
    assert (
        package_dir_from_setup_py("    # package_dir={'': 'old'}\n    package_dir={'': 'lib'},\n")
        == "lib"
    )


def test_package_dir_parsers_ignore_named_package_mappings():
    # A mapping WITHOUT the '' root key relocates single packages, not the
    # import root — nothing to probe as a base dir.
    assert package_dir_from_setup_py("package_dir={'yaml': 'lib/yaml'}") is None
    assert (
        package_dir_from_pyproject('[tool.setuptools]\npackage-dir = {"yaml" = "lib/yaml"}\n')
        is None
    )


# ---------------------------------------------------------------------------
# Bug #8 (apache/libcloud live probe): junk flat-layout discoveries remain
# distinguishable from project-owned distribution names without execution
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


def test_libcloud_junk_discoveries_warn_without_import_execution():
    # Installed ownership still distinguishes the project's package from
    # survey junk, but the judge does not execute either group. Importability
    # remains unknown until a producer receipt is available.
    orch = TopLevelOrch(
        dists=[_LIBCLOUD_DIST] + _DEP_DISTS + _TOOLING_DISTS,
        manifest=_manifest(python_packages=_LIBCLOUD_JUNK + ["libcloud"]),
    )
    result = _validate(orch)
    assert _import_commands(orch) == []
    details = result["evidence"]["fingerprint_details"]
    assert details["imports_ok"] is None
    assert details["import_failures"] == []
    assert result["success"] is True
    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    warning = next(w for w in result["evidence"]["warnings"] if "discovered but not installed" in w)
    for name in _LIBCLOUD_JUNK:
        assert name in warning
    assert "libcloud," not in warning  # the real package is not junk


def test_all_junk_manifest_uses_installed_names_as_ownership_only():
    orch = TopLevelOrch(
        dists=[_LIBCLOUD_DIST] + _DEP_DISTS + _TOOLING_DISTS,
        manifest=_manifest(python_packages=list(_LIBCLOUD_JUNK)),
    )
    result = _validate(orch)
    assert _import_commands(orch) == []
    assert result["evidence"]["fingerprint_details"]["imports_ok"] is None
    assert any(
        "package importability unknown" in warning and "libcloud" in warning
        for warning in result["evidence"]["warnings"]
    )


def test_nothing_installed_keeps_manifest_names_but_does_not_import_them():
    orch = TopLevelOrch(
        dists=[],
        manifest=_manifest(python_packages=["libcloud"]),
        import_ok=False,
    )
    result = _validate(orch)
    assert _import_commands(orch) == []
    assert result["success"] is True
    assert result["evidence_status"] == "partial"
    details = result["evidence"]["fingerprint_details"]
    assert details["imports_ok"] is None
    assert details["import_failures"] == []


def test_installed_siblings_are_reported_without_runtime_imports():
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
    assert _import_commands(orch) == []
    details = result["evidence"]["fingerprint_details"]
    assert details["imports_ok"] is None
    assert details["import_failures"] == []
    importability_warning = next(
        warning
        for warning in result["evidence"]["warnings"]
        if "package importability unknown" in warning
    )
    for name in ("mercurial", "hgext", "hgdemandimport"):
        assert name in importability_warning


def test_fully_installed_manifest_needs_no_junk_warning_or_execution():
    orch = TopLevelOrch(manifest=_manifest(python_packages=["yaml", "_yaml"]))
    result = _validate(orch)
    assert _import_commands(orch) == []
    assert not any("discovered but not installed" in w for w in result["evidence"]["warnings"])
    assert result["evidence"]["fingerprint_details"]["imports_ok"] is None


def test_optional_extension_claim_cannot_change_judge_without_producer_receipt():
    orch = TopLevelOrch(failing_imports={"_yaml"})
    result = _validate(orch)
    assert _import_commands(orch) == []
    details = result["evidence"]["fingerprint_details"]
    assert details["imports_ok"] is None
    assert details["ext_modules_ok"] is None
    assert result["success"] is True
    assert result["evidence_status"] == "partial"


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
                "/workspace/mylib/my_lib/__init__.py\n" "/workspace/mylib/contrib/__init__.py\n"
            )
        },
    )
    assert discover_packages(orch, "/workspace/mylib") == ["my_lib"]


def test_discover_packages_without_name_match_keeps_all_candidates():
    # apache-libcloud normalizes to apache_libcloud — no flat dir matches, so
    # every candidate is kept (a heuristic, never a deny-list; the
    # validator's installed-record gate is the guarantee, see above).
    orch = PackageDirOrch(
        files={"/workspace/libcloud/setup.py": "setup(name='apache-libcloud')\n"},
        find_outputs={"/workspace/libcloud": _LIBCLOUD_FLAT_FIND},
    )
    assert discover_packages(orch, "/workspace/libcloud") == [
        "contrib",
        "demos",
        "integration",
        "libcloud",
        "pylint_plugins",
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
        files={"/workspace/proj/pyproject.toml": '[project]\nname = "something-else"\n'},
        find_outputs={"/workspace/proj": "/workspace/proj/bar/__init__.py\n"},
    )
    assert discover_packages(orch, "/workspace/proj") == ["bar"]


# ---------------------------------------------------------------------------
# project-name parsers (pure functions)
# ---------------------------------------------------------------------------


def test_project_name_parsers_extract_the_declared_name():
    assert (
        project_name_from_pyproject('[project]\nname = "apache-libcloud"\nversion = "3.8.0"\n')
        == "apache-libcloud"
    )
    assert project_name_from_pyproject('[tool.poetry]\nname = "mylib"\n') == "mylib"
    assert project_name_from_setup_py("setup(\n    name='PyYAML',\n)") == "PyYAML"
    assert project_name_from_setup_py("# name='old'\nsetup(name='new')") == "new"


def test_project_name_parsers_never_read_unrelated_keys():
    # A name key outside [project]/[tool.poetry] is not the project name.
    assert project_name_from_pyproject('[tool.other]\nname = "nope"\n') is None
    assert project_name_from_pyproject("[project]\nversion = '1.0'\n") is None
    # author_name= is not name= (word boundary), and no name at all is None.
    assert project_name_from_setup_py("setup(author_name='x')") is None
    assert project_name_from_setup_py("setup(packages=['x'])") is None
