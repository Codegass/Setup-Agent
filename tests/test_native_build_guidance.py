# tests/test_native_build_guidance.py
"""Native-core detection for python-subdir repos (T5, guidance-level).

LIVE EVIDENCE (TVM, session 20260713_014403_27874): the analysis listed
CMakeLists.txt in "Project Files Found" yet the recommendation railroaded a
plain `pip install -e .` at the repo ROOT. Two live gaps:

  1. TVM's real python package lives in the ``python/`` subdirectory
     (``python/setup.py``) — the root has no installable python package, so a
     root ``pip install -e .`` targets the wrong thing.
  2. TVM's native core (``libtvm.so`` via the root ``CMakeLists.txt``) must be
     built BEFORE the python package can import — and nothing told the agent.

This suite drives the REAL analyzer chain (real detection, real
_recommend_build_approach, real manifest write), the REAL phase-intro guidance
seam, and the REAL python evidence ladder — no fabricated recommendation
anywhere. It is the failing reproduction that T5's fix must turn green.

Sections:
  A. analyzer: a tvm-shaped repo (root CMakeLists + python/setup.py + jvm/pom)
     recommends build_root=/workspace/tvm/python, has_native_build=True; the
     manifest carries has_native_build=True; discovery/install target the
     python subdir. A plain-pyproject repo carries no native signal and its
     recommendation/manifest are byte-identical to the pre-change shape.
  B. guidance: dim (e) of the Category-3 analyzer diet DELETED the pre-hoc
     native-first prose block. The native repo's build intro now carries the
     python FACTS objective + coordinates only (no "build the native library
     first" prose, no project brief); a plain-python repo likewise. The native
     FACT still drives the REACTIVE smoke steer and the validator cap.
  C. validator: has_native_build + no built .so caps the build at PARTIAL with
     the reason "native core not built" (never BLOCKED — pure-python parts may
     still run); with a .so present the ladder is unchanged.
"""

import json
import re
import shlex
from types import SimpleNamespace

from sag.agent.physical_survey import _smoke_candidates_from_pyproject
from sag.agent.physical_validator import PhysicalValidator
from sag.agent.phase_machine import PhaseMachine
from sag.agent.react_engine import ReActEngine
from sag.tools.internal.build_preflight import REQUIREMENTS_PATH
from sag.tools.internal.project_analyzer import ProjectAnalyzerTool

# ---------------------------------------------------------------------------
# Scripted repo (mirrors tests/test_python_phase_guidance.py::_ScriptedRepo,
# extended with a maxdepth-N `find setup.py/pyproject.toml` shape the package-
# root detector needs).
# ---------------------------------------------------------------------------


class _ScriptedRepo:
    """Answers the analyzer's shell probes from an in-memory file map."""

    def __init__(self, root, files):
        self.root = root.rstrip("/")
        self.files = {f"{self.root}/{path}": body for path, body in files.items()}
        self.dirs = {self.root}
        for path in self.files:
            parts = path.split("/")
            for i in range(2, len(parts)):
                self.dirs.add("/".join(parts[:i]))
        self.written = {}  # REQUIREMENTS_PATH heredoc capture

    def execute_command(self, command, **kwargs):
        cmd = command.strip()
        # Manifest heredoc write: capture the JSON body.
        if cmd.startswith("cat >") or cmd.startswith("cat > "):
            m = re.search(r"<<'SAGEOF'\n(.*)\nSAGEOF", command, re.DOTALL)
            target = command.split()[2]
            if m:
                self.written[target] = m.group(1)
            return {"success": True, "output": ""}
        m = re.match(r"test -f (\S+)", cmd)
        if m:
            exists = m.group(1) in self.files
            if "echo 'missing'" in cmd:
                return {"success": True, "output": "exists" if exists else "missing"}
            return {"success": exists, "output": "exists" if exists else ""}
        m = re.match(r"test -d (\S+)", cmd)
        if m:
            hit = m.group(1).rstrip("/") in self.dirs
            return {"success": True, "output": "exists" if hit else ""}
        m = re.match(r"test -e (\S+)", cmd)
        if m:
            path = m.group(1).rstrip("/")
            hit = path in self.files or path in self.dirs
            return {"success": True, "output": "yes" if hit else "no"}
        m = re.match(r"cat (\S+)", cmd)
        if m and not cmd.startswith("cat >"):
            path = m.group(1)
            if path in self.files:
                return {"success": True, "output": self.files[path]}
            return {"success": False, "output": ""}
        m = re.match(r"ls -1 (\S+)", cmd)
        if m:
            base = m.group(1).rstrip("/") + "/"
            names = sorted({p[len(base) :].split("/")[0] for p in self.files if p.startswith(base)})
            return {"success": True, "output": "\n".join(names)}
        if cmd.startswith("mkdir"):
            return {"success": True, "output": ""}
        if cmd.startswith("realpath -m -- "):
            return {
                "success": True,
                "exit_code": 0,
                "output": "\n".join(shlex.split(cmd)[3:]),
            }
        if cmd.startswith("find "):
            base = cmd.split()[1]
            if "__init__.py" in cmd:
                hits = sorted(
                    p
                    for p in self.files
                    if p.startswith(base + "/")
                    and p.endswith("/__init__.py")
                    and p[len(base) + 1 :].count("/") <= 1
                )
                return {"success": True, "output": "\n".join(hits)}
            # `find <root> ... -name setup.py -o -name pyproject.toml` (package-
            # root detection): match by basename under the root, honoring an
            # optional -maxdepth.
            names = re.findall(r"-name ['\"]?([\w.]+)['\"]?", cmd)
            if names:
                depth = None
                dm = re.search(r"-maxdepth (\d+)", cmd)
                if dm:
                    depth = int(dm.group(1))
                hits = []
                for p in self.files:
                    if not p.startswith(base + "/"):
                        continue
                    rel = p[len(base) + 1 :]
                    if depth is not None and rel.count("/") + 1 > depth:
                        continue
                    if p.rsplit("/", 1)[-1] in names:
                        hits.append(p)
                return {"success": True, "output": "\n".join(sorted(hits))}
            suffixes = re.findall(r"-path '\*(/src/(?:main|test)/\w+)'", cmd)
            if suffixes:
                hits = sorted(d for d in self.dirs if any(d.endswith(s) for s in suffixes))
                return {"success": True, "output": "\n".join(hits)}
            return {"success": True, "output": ""}
        return {"success": True, "output": ""}


# ---------------------------------------------------------------------------
# Fixtures: a tvm-shaped repo and a plain-pyproject repo.
# ---------------------------------------------------------------------------

_TVM_ROOT = "/workspace/tvm"
_TVM_FILES = {
    # Root: CMakeLists.txt (native core) + a pyproject WITHOUT [project] deps
    # (TVM's root is a build shell, the real package is python/).
    "CMakeLists.txt": "cmake_minimum_required(VERSION 3.18)\nproject(tvm)\n",
    # The real python package lives here.
    "python/setup.py": (
        "from setuptools import setup\n" "setup(name='tvm', python_requires='>=3.8')\n"
    ),
    "python/tvm/__init__.py": "",
    "python/tvm/relay.py": "X = 1\n",
    "python/tests/test_relay.py": "def test_x():\n    assert True\n",
    # A JVM binding subdirectory (must not flip the primary python targeting).
    "jvm/pom.xml": (
        "<project><modelVersion>4.0.0</modelVersion>"
        "<groupId>tvm</groupId><artifactId>tvm4j</artifactId>"
        "<version>1.0</version></project>"
    ),
}

_PLAIN_ROOT = "/workspace/pyproj"
_PLAIN_FILES = {
    # Pure-python repo: a REAL root [project] package, src layout, no
    # CMakeLists, no python/ subdir package. Nothing native anywhere.
    #
    # The pyproject deliberately uses the STANDARD modern ordering —
    # authors/classifiers arrays BEFORE dependencies (this repo's own
    # pyproject shape). A bracket-fragile "[project] ... dependencies ="
    # regex truncates at the first '[' inside authors=[...] and mis-reads
    # this real package as a build shell, redirecting install/venv/test into
    # a python/ subdir (the mirror image of the TVM bug). This fixture is the
    # regression guard: package-less-ness must be established positively, so
    # this root stays the install/test root and the intro below is byte-
    # identical to the pre-change shape.
    "pyproject.toml": (
        '[project]\nname = "pypkg"\nrequires-python = ">=3.9"\n'
        'authors = [{name = "Plain Author"}]\n'
        'classifiers = ["Programming Language :: Python :: 3"]\n'
        'dependencies = ["requests"]\n'
    ),
    "src/pypkg/__init__.py": "",
    "src/pypkg/core.py": "X = 1\n",
    "tests/test_core.py": "def test_x():\n    assert True\n",
}

_CURRENT_TVM_FILES = {
    "CMakeLists.txt": "cmake_minimum_required(VERSION 3.18)\nproject(tvm)\n",
    ".gitmodules": (
        '[submodule "3rdparty/tvm-ffi"]\n'
        "\tpath = 3rdparty/tvm-ffi\n"
        "\turl = https://github.com/apache/tvm-ffi.git\n"
    ),
    "pyproject.toml": (
        "[build-system]\n"
        'requires = ["scikit-build-core>=0.11", "setuptools-scm>=8"]\n'
        'build-backend = "scikit_build_core.build"\n\n'
        "[project]\n"
        'name = "apache-tvm"\n'
        'requires-python = ">=3.10"\n'
        'dependencies = ["apache-tvm-ffi>=0.1.13", "numpy"]\n\n'
        "[tool.scikit-build]\n"
        'build-dir = "build"\n'
        'wheel.packages = ["python/tvm"]\n\n'
        "[tool.cibuildwheel]\n"
        'test-command = "pytest -vvs {project}/tests/python/all-platform-minimal-test"\n'
    ),
    "python/tvm/__init__.py": "",
    "python/tvm/runtime.py": "X = 1\n",
    "tests/python/all-platform-minimal-test/test_runtime.py": (
        "def test_runtime():\n    assert True\n"
    ),
    "3rdparty/tvm-ffi/pyproject.toml": (
        "[build-system]\n"
        'requires = ["scikit-build-core"]\n'
        'build-backend = "scikit_build_core.build"\n\n'
        "[project]\n"
        'name = "apache-tvm-ffi"\n'
        'dynamic = ["version"]\n'
    ),
}

_SUBDIR_SCIKIT_FILES = {
    "CMakeLists.txt": "cmake_minimum_required(VERSION 3.18)\nproject(native_pkg)\n",
    "python/pyproject.toml": (
        "[build-system]\n"
        'requires = ["scikit-build-core>=0.11"]\n'
        'build-backend = "scikit_build_core.build"\n\n'
        "[project]\n"
        'name = "native-pkg"\n'
        'requires-python = ">=3.10"\n\n'
        "[tool.scikit-build]\n"
        'build-dir = "_native-build"\n'
        'wheel.packages = ["src/native_pkg"]\n\n'
        "[tool.cibuildwheel]\n"
        'test-command = "pytest {project}/tests/smoke"\n'
    ),
    "python/src/native_pkg/__init__.py": "",
    "python/tests/smoke/test_import.py": "def test_import():\n    assert True\n",
}


def _analyzed(root, files):
    orch = _ScriptedRepo(root, files)
    analyzer = ProjectAnalyzerTool(docker_orchestrator=orch, context_manager=None)
    analysis = analyzer._perform_comprehensive_analysis(root)
    return analysis, orch


def _env_from(analysis):
    trunk = SimpleNamespace(environment_summary={}, todo_list=[])
    ProjectAnalyzerTool(docker_orchestrator=None, context_manager=None)._record_environment_metrics(
        trunk, analysis
    )
    return trunk.environment_summary


# ---------------------------------------------------------------------------
# A. analyzer: package-root detection + has_native_build
# ---------------------------------------------------------------------------


def test_tvm_recommendation_targets_python_subdir_root():
    analysis, _ = _analyzed(_TVM_ROOT, _TVM_FILES)
    rec = analysis["build_recommendation"]
    assert rec["build_system"] == "python"
    assert rec["build_root"] == f"{_TVM_ROOT}/python"
    assert rec["test_root"] == f"{_TVM_ROOT}/python"
    assert rec["has_native_build"] is True


def test_tvm_manifest_records_python_root_and_native_flag():
    analysis, orch = _analyzed(_TVM_ROOT, _TVM_FILES)
    body = orch.written.get(REQUIREMENTS_PATH)
    assert body, "manifest was never written"
    manifest = json.loads(body)
    assert manifest["has_native_build"] is True
    assert manifest["build_root"] == f"{_TVM_ROOT}/python"
    # The venv + install target the python subdir, not the CMake shell root.
    assert manifest["python_venv"] == f"{_TVM_ROOT}/python/.venv"


def test_tvm_discovery_finds_package_under_python_subdir():
    analysis, _ = _analyzed(_TVM_ROOT, _TVM_FILES)
    # discover_packages ran against the python/ root, so it finds tvm — not an
    # empty list from probing the CMake shell root.
    assert analysis["python_config"]["python_packages"] == ["tvm"]
    assert analysis["python_config"]["python_root"] == f"{_TVM_ROOT}/python"


def test_current_tvm_pep517_facts_are_grounded_in_the_root_pyproject():
    analysis, orch = _analyzed(_TVM_ROOT, _CURRENT_TVM_FILES)
    python = analysis["python_config"]

    assert python["python_root"] == _TVM_ROOT
    assert python["python_distribution_name"] == "apache-tvm"
    assert python["python_build_backend"] == "scikit_build_core.build"
    assert python["python_packages"] == ["tvm"]
    assert python["python_package_paths"] == [
        {
            "import_name": "tvm",
            "path": "python/tvm",
            "source": "pyproject.toml:tool.scikit-build.wheel.packages",
        }
    ]
    assert python["native_build_mode"] == "pep517-integrated"
    assert python["native_artifact_roots"] == ["build", "python/tvm/lib"]
    assert python["python_local_providers"] == [
        {
            "distribution_name": "apache-tvm-ffi",
            "root": "3rdparty/tvm-ffi",
            "requirement": "apache-tvm-ffi>=0.1.13",
            "build_backend": "scikit_build_core.build",
        }
    ]
    assert python["python_smoke_candidates"] == [
        {
            "path": "tests/python/all-platform-minimal-test",
            "source": "pyproject.toml:tool.cibuildwheel.test-command",
        }
    ]

    manifest = json.loads(orch.written[REQUIREMENTS_PATH])
    for key in (
        "python_distribution_name",
        "python_build_backend",
        "python_package_paths",
        "python_local_providers",
        "python_smoke_candidates",
        "native_build_mode",
        "native_artifact_roots",
    ):
        assert manifest[key] == python[key]


def test_subdir_pep517_facts_keep_install_and_repository_coordinates_distinct():
    """Package paths are install-root relative; smoke/artifact paths are
    repository-root relative. A subdir PEP 517 project therefore must prefix
    only the latter instead of accidentally validating paths at repo root."""
    analysis, _ = _analyzed(_TVM_ROOT, _SUBDIR_SCIKIT_FILES)
    python = analysis["python_config"]

    assert python["python_root"] == f"{_TVM_ROOT}/python"
    assert python["python_distribution_name"] == "native-pkg"
    assert python["python_package_paths"] == [
        {
            "import_name": "native_pkg",
            "path": "src/native_pkg",
            "source": "pyproject.toml:tool.scikit-build.wheel.packages",
        }
    ]
    assert python["native_artifact_roots"] == [
        "python/_native-build",
        "python/src/native_pkg/lib",
    ]
    assert python["python_smoke_candidates"] == [
        {
            "path": "python/tests/smoke",
            "source": "pyproject.toml:tool.cibuildwheel.test-command",
        }
    ]


def test_filesystem_smoke_ranking_has_no_project_named_special_case():
    """Generic evidence wins by generic traits, not a TVM fixture basename."""

    class SmokeRepo:
        def execute_command(self, command, **kwargs):
            if command.startswith("find "):
                return {
                    "success": True,
                    "output": "\n".join(
                        [
                            (
                                "/workspace/proj/tests/python/"
                                "all-platform-minimal-test/test_long_runtime_name.py"
                            ),
                            "/workspace/proj/smoke/test_a.py",
                        ]
                    ),
                }
            return {"success": True, "output": ""}

    candidates = _smoke_candidates_from_pyproject(
        SmokeRepo(),
        "/workspace/proj",
        {},
    )

    assert candidates[0] == {
        "path": "smoke/test_a.py",
        "source": "filesystem:test-file",
    }


def test_plain_pyproject_has_no_native_signal():
    analysis, orch = _analyzed(_PLAIN_ROOT, _PLAIN_FILES)
    rec = analysis["build_recommendation"]
    # Byte-identical to the pre-change plain-python recommendation shape:
    # build_root stays at the repo root and there is no native flag.
    assert rec["build_root"] == _PLAIN_ROOT
    assert rec["test_root"] == _PLAIN_ROOT
    assert rec.get("has_native_build") in (False, None)
    manifest = json.loads(orch.written[REQUIREMENTS_PATH])
    assert manifest.get("has_native_build") in (False, None)
    assert manifest["build_root"] == _PLAIN_ROOT


# ---------------------------------------------------------------------------
# B. guidance: native-first prepend on the build-phase intro
# ---------------------------------------------------------------------------


def _engine_at(phase_done_count, environment_summary):
    engine = ReActEngine.__new__(ReActEngine)
    machine = PhaseMachine()
    results = [
        "repo cloned; toolchain installed",
        "python project analyzed",
        "deps installed",
    ]
    for i in range(phase_done_count):
        machine.mark_done(results[i], [])
    engine.phase_machine = machine
    engine.config = SimpleNamespace(phase_min_floors={}, max_iterations=150)
    engine.current_iteration = 10

    class FakeCM:
        def load_trunk_context(self):
            return SimpleNamespace(environment_summary=environment_summary)

    engine.context_manager = FakeCM()
    return engine


# dim (e) deleted (Category-3 analyzer diet, 2026-07-20): the PRE-HOC
# native-first guidance block is a prescription and is gone. The native FACT
# (has_native_build) is retained on the recommendation/manifest and drives the
# REACTIVE smoke steer and the validator's PARTIAL cap (sections A and C); it no
# longer emits a pre-hoc "build the native library FIRST" prose block.
_NATIVE_FIRST_MARKERS = (
    "NATIVE core",
    "CMakeLists.txt at the repo root",
    "Build the native library FIRST",
    "will not import without it",
)


def test_native_build_intro_has_no_prehoc_native_first_block():
    env = _env_from(_analyzed(_TVM_ROOT, _TVM_FILES)[0])
    intro = _engine_at(2, env)._phase_intro_step().content
    # dim (e) deleted: no pre-hoc native-first prose.
    for marker in _NATIVE_FIRST_MARKERS:
        assert marker not in intro, f"native-first prose should be gone: {marker!r}"
    # The python FACTS objective + coordinates remain (physical substrate).
    assert "build(action='deps')" in intro
    assert "build(action='compile')" in intro
    assert f"Build coordinates: python at {_TVM_ROOT}/python." in intro


def test_plain_python_intro_carries_facts_objective_and_coordinates():
    """Plain Python keeps its semantics — the FACTS objective and coordinates,
    no native prose, no project brief."""
    env = _env_from(_analyzed(_PLAIN_ROOT, _PLAIN_FILES)[0])
    intro = _engine_at(2, env)._phase_intro_step().content
    assert "NATIVE core" not in intro
    assert "native library FIRST" not in intro
    assert "=== PROJECT BRIEF v1 ===" not in intro  # dim (c) deleted
    assert "build(action='deps')" in intro
    assert "build(action='compile')" in intro
    assert f"Build coordinates: python at {_PLAIN_ROOT}." in intro


# ---------------------------------------------------------------------------
# C. validator: native core not built -> PARTIAL, never BLOCKED
# ---------------------------------------------------------------------------


def _native_manifest(**overrides):
    data = {
        "python_version": "3.12",
        "python_constraint": ">=3.8",
        "python_installer": "pip",
        "python_install_commands": ["{venv}/bin/python -m pip install -e ."],
        "python_packages": ["tvm"],
        "python_venv": "/workspace/tvm/python/.venv",
        "build_root": "/workspace/tvm/python",
        "has_c_extensions": False,
        "has_native_build": True,
    }
    data.update(overrides)
    return data


class NativeLadderOrch:
    """Python evidence-ladder container for a native-core repo: package tvm
    imports (pure-python parts present), but the native .so may be absent."""

    def __init__(self, *, so_present=False, import_ok=True, manifest=None):
        self.so_present = so_present
        self.import_ok = import_ok
        self.manifest = manifest if manifest is not None else _native_manifest()
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
            return res("pyproject.toml" in c)
        if c.startswith("test -d "):
            path = c.split()[2]
            if path.endswith("/.venv"):
                return res(True)
            if path.endswith("/src/tvm"):
                return res(False)
            if path.endswith("/tvm"):
                return res(True)
            return res(False)
        if "pip check" in c:
            return res(True, "No broken requirements found.")
        if "import tvm" in c:
            return res(
                self.import_ok,
                "" if self.import_ok else "ImportError: cannot find libtvm.so",
            )
        if "cache_from_source" in c:
            # The validator's scripted metrics probe (compileall_metrics_command
            # runs an inline python script — the word 'compileall' never appears
            # in it) expects a JSON payload; the fixture predated the probe and
            # its empty default parsed as 'metrics unavailable', capping an
            # all-green ladder below build_complete.
            return res(
                True,
                json.dumps(
                    {
                        "status": "valid",
                        "source_count": 10,
                        "compiled_source_count": 10,
                        "missing_source_count": 0,
                        "foreign_pyc_count": 0,
                        "coverage": 1.0,
                        "missing_sources": [],
                        "foreign_pycs": [],
                    }
                ),
            )
        if "__pycache__" in c and "wc -l" in c:
            return res(True, "10")
        if "-m compileall" in c:
            return res(True)
        if "'*.py'" in c and "wc -l" in c:
            return res(True, "10")
        if "'*.so'" in c or "'*.dylib'" in c:
            return res(True, "/workspace/tvm/python/tvm/libtvm.so" if self.so_present else "")
        if "'*.jar'" in c or "'*.class'" in c:
            return res(True, "0")
        return res(True, "")


def _validate(orch):
    validator = PhysicalValidator(docker_orchestrator=orch, project_path="/workspace/tvm/python")
    return validator.validate_build_status("python")


def test_native_core_not_built_caps_at_partial():
    """has_native_build True + NO built .so under the package or build/ -> the
    build evidence caps at PARTIAL with reason 'native core not built' — never
    BLOCKED (pure-python parts and tests may still run)."""
    orch = NativeLadderOrch(so_present=False)
    result = _validate(orch)
    assert result["success"] is True  # never a hard block
    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert "native core not built" in result["reason"]


def test_native_core_built_leaves_ladder_unchanged():
    """With the native .so present, the native rung is satisfied and the ladder
    is the ordinary all-green SUCCESS — the native cap adds nothing."""
    orch = NativeLadderOrch(so_present=True)
    result = _validate(orch)
    assert result["success"] is True
    assert result["build_complete"] is True
    assert result["evidence_status"] == "success"
    assert "native core not built" not in result["reason"]


def test_native_flag_absent_never_adds_native_cap():
    """Without has_native_build the native cap never fires even if no .so is
    present (a pure-python project has no native core to build)."""
    orch = NativeLadderOrch(so_present=False, manifest=_native_manifest(has_native_build=False))
    result = _validate(orch)
    assert result["success"] is True
    assert result["build_complete"] is True
    assert result["evidence_status"] == "success"
    assert "native core not built" not in result["reason"]


# ---------------------------------------------------------------------------
# D. facts-v8 validator ownership: exact root dist + grounded artifact roots
# ---------------------------------------------------------------------------


def _strict_native_manifest(**overrides):
    data = {
        "survey": {
            "project_path": "/workspace/tvm",
            "analyzer_version": 8,
            "config_fingerprint": "fixture",
        },
        "python_version": "3.12",
        "python_constraint": ">=3.8",
        "python_installer": "pip",
        "python_install_commands": ["{venv}/bin/python -m pip install -e ."],
        "python_distribution_name": "apache-tvm",
        "python_packages": ["tvm"],
        "python_package_paths": [
            {
                "import_name": "tvm",
                "path": "python/tvm",
                "source": "pyproject.toml:tool.scikit-build.wheel.packages",
            }
        ],
        "python_smoke_candidates": [
            {
                "path": "tests/python/all-platform-minimal-test",
                "source": "pyproject.toml:tool.cibuildwheel.test-command",
            }
        ],
        "native_artifact_roots": ["build", "python/tvm/lib"],
        "python_venv": "/workspace/tvm/.venv",
        "python_root": "/workspace/tvm",
        "build_root": "/workspace/tvm",
        "has_c_extensions": False,
        "has_native_build": True,
    }
    data.update(overrides)
    return data


def _record(name, top_level, direct_url):
    return {
        "path": f"/workspace/tvm/.venv/lib/python3.12/site-packages/{name}",
        "top_level": top_level,
        "direct_url": direct_url,
    }


_ROOT_RECORD = _record(
    "apache_tvm-0.22.dist-info",
    "tvm\n",
    '{"url": "file:///workspace/tvm", "dir_info": {"editable": true}}',
)
_FFI_RECORD = _record(
    "apache_tvm_ffi-0.1.13.dist-info",
    "tvm_ffi\n",
    '{"url": "file:///workspace/tvm/3rdparty/tvm-ffi", ' '"dir_info": {"editable": true}}',
)


class StrictNativeLadderOrch:
    """Current-TVM physical evidence with realistic sibling dist records."""

    def __init__(
        self,
        *,
        records=None,
        artifact="",
        smoke_exists=True,
        smoke_realpath=None,
        origin_realpath="/workspace/tvm",
        install_realpath=None,
        artifact_realpath=None,
        artifact_realpaths=None,
        manifest=None,
        venv_exists=True,
        workspace_realpath="/workspace",
        survey_realpath=None,
    ):
        self.records = list(records if records is not None else [_ROOT_RECORD, _FFI_RECORD])
        self.artifact = artifact
        self.smoke_exists = smoke_exists
        self.smoke_realpath = smoke_realpath
        self.origin_realpath = origin_realpath
        self.install_realpath = install_realpath
        self.artifact_realpath = artifact_realpath
        self.artifact_realpaths = dict(artifact_realpaths or {})
        self.manifest = manifest if manifest is not None else _strict_native_manifest()
        self.venv_exists = venv_exists
        self.workspace_realpath = workspace_realpath
        self.survey_realpath = survey_realpath
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
            return res(c.endswith("/pyproject.toml"))
        if c.startswith("test -d "):
            return res(self.venv_exists and c.split()[2] == self.manifest.get("python_venv"))
        if c.startswith("test -e "):
            exists = (
                self.smoke_exists
                and c.split()[2] == "/workspace/tvm/tests/python/all-platform-minimal-test"
            )
            if "echo EXISTS" in c:
                return res(True, "EXISTS" if exists else "MISSING")
            return res(exists)
        if "pip check" in c:
            return res(True, "No broken requirements found.")
        if c.startswith("find ") and "site-packages" in c and ("dist-info" in c or "egg-info" in c):
            return res(True, "\n".join(item["path"] for item in self.records))
        if c.startswith("cat ") and c.split()[1].endswith("/direct_url.json"):
            record_path = c.split()[1][: -len("/direct_url.json")]
            record = next((item for item in self.records if item["path"] == record_path), None)
            return res(record is not None, record["direct_url"] if record else "")
        if c.startswith("cat ") and c.split()[1].endswith("/top_level.txt"):
            record_path = c.split()[1][: -len("/top_level.txt")]
            record = next((item for item in self.records if item["path"] == record_path), None)
            return res(record is not None, record["top_level"] if record else "")
        if c.startswith("realpath -m -- "):
            paths = c.split()[3:]
            if len(paths) == 3 and paths[0] == "/workspace":
                paths[0] = self.workspace_realpath
                if self.survey_realpath:
                    paths[1] = self.survey_realpath
                if self.smoke_realpath:
                    paths[2] = self.smoke_realpath
            elif len(paths) == 4 and paths[0] == "/workspace":
                original_survey = paths[3]
                paths[0] = self.workspace_realpath
                paths[1] = self.origin_realpath
                if self.install_realpath:
                    paths[2] = self.install_realpath
                elif self.survey_realpath and paths[2] == original_survey:
                    paths[2] = self.survey_realpath
                if self.survey_realpath:
                    paths[3] = self.survey_realpath
            elif len(paths) >= 5 and paths[0] == "/workspace":
                original_project = paths[2]
                paths[0] = self.workspace_realpath
                paths[1] = self.artifact_realpaths.get(
                    paths[1],
                    self.artifact_realpath or paths[1],
                )
                if self.survey_realpath:
                    paths[2] = self.survey_realpath
                    paths[3:] = [
                        (
                            self.survey_realpath + path[len(original_project) :]
                            if path.startswith(f"{original_project}/")
                            else path
                        )
                        for path in paths[3:]
                    ]
            elif len(paths) == 2 and paths[1].endswith("/tests/python/all-platform-minimal-test"):
                if self.smoke_realpath:
                    paths[1] = self.smoke_realpath
            elif len(paths) == 3:
                paths[0] = self.origin_realpath
                if self.install_realpath:
                    paths[1] = self.install_realpath
            elif len(paths) == 2:
                paths[0] = self.origin_realpath
            elif paths:
                paths[0] = self.artifact_realpaths.get(
                    paths[0],
                    self.artifact_realpath or paths[0],
                )
            return res(True, "\n".join(paths))
        if '-c "import ' in c:
            return res(True)
        if "-m compileall" in c:
            return res(True)
        if "cache_from_source" in c:
            return res(
                True,
                json.dumps(
                    {
                        "status": "valid",
                        "source_count": 10,
                        "compiled_source_count": 10,
                        "missing_source_count": 0,
                        "foreign_pyc_count": 0,
                        "coverage": 1.0,
                        "missing_sources": [],
                        "foreign_pycs": [],
                    }
                ),
            )
        if "'*.so'" in c and "'*.dylib'" in c:
            return res(True, self.artifact)
        if "'*.jar'" in c or "'*.class'" in c:
            return res(True, "0")
        return res(True, "")


def _validate_strict_native(orch):
    validator = PhysicalValidator(
        docker_orchestrator=orch,
        project_path="/workspace",
    )
    return validator.validate_build_status("tvm")


def test_v8_provider_record_cannot_substitute_for_root_distribution():
    orch = StrictNativeLadderOrch(records=[_FFI_RECORD])
    result = _validate_strict_native(orch)
    details = result["evidence"]["fingerprint_details"]

    assert result["success"] is False
    assert result["evidence_status"] == "blocked"
    assert details["distribution_record_ok"] is False
    assert "apache-tvm" in result["reason"]
    # Build verification and test entry are separate facts: the bounded,
    # re-verified smoke coordinate remains runnable during native recovery.
    assert result["test_entry_ready"] is True
    assert "tvm_ffi" not in "\n".join(
        command for command in orch.commands if '-c "import ' in command
    )


def test_v8_mismatched_editable_origin_cannot_verify_root_distribution():
    orch = StrictNativeLadderOrch(origin_realpath="/workspace/other-checkout")
    result = _validate_strict_native(orch)

    assert result["success"] is False
    assert result["evidence"]["fingerprint_details"]["distribution_origin_ok"] is False
    assert "does not resolve to surveyed Python install root" in result["reason"]


def test_v8_symlinked_checkout_root_cannot_turn_owned_artifact_green():
    outside_root = "/opt/shared/tvm"
    orch = StrictNativeLadderOrch(
        artifact="/workspace/tvm/build/libtvm.so",
        origin_realpath=outside_root,
        survey_realpath=outside_root,
        artifact_realpath=f"{outside_root}/build/libtvm.so",
    )

    result = _validate_strict_native(orch)
    details = result["evidence"]["fingerprint_details"]

    assert result["success"] is False
    assert result["build_complete"] is False
    assert result["test_entry_ready"] is False
    assert details["distribution_origin_ok"] is False
    # The exact project distribution fails closed first, so later ladder
    # rungs are deliberately not evaluated in this end-to-end path.
    assert details["native_artifact_ok"] is None


def test_v8_native_artifact_helper_rejects_symlinked_checkout_root():
    outside_root = "/opt/shared/tvm"
    artifact = "/workspace/tvm/build/libtvm.so"
    orch = StrictNativeLadderOrch(
        artifact=artifact,
        survey_realpath=outside_root,
        artifact_realpath=f"{outside_root}/build/libtvm.so",
    )
    validator = PhysicalValidator(
        docker_orchestrator=orch,
        project_path="/workspace",
    )

    assert (
        validator._verified_native_artifact(
            [artifact],
            {
                "/workspace/tvm/build",
                "/workspace/tvm/python/tvm/lib",
            },
            "/workspace/tvm",
        )
        is False
    )


def test_v8_remote_host_file_url_cannot_claim_local_project_origin():
    remote = _record(
        "apache_tvm-0.22.dist-info",
        "tvm\n",
        '{"url": "file://other-host/workspace/tvm", ' '"dir_info": {"editable": true}}',
    )
    orch = StrictNativeLadderOrch(records=[remote])

    result = _validate_strict_native(orch)

    assert result["success"] is False
    assert result["evidence"]["fingerprint_details"]["distribution_origin_ok"] is False


def test_v8_matching_registry_record_without_pep610_origin_is_not_project_owned():
    registry_record = _record(
        "apache_tvm-0.22.dist-info",
        "tvm\n",
        "",
    )
    orch = StrictNativeLadderOrch(records=[registry_record])

    result = _validate_strict_native(orch)

    assert result["success"] is False
    details = result["evidence"]["fingerprint_details"]
    assert details["distribution_record_ok"] is False
    assert details["distribution_origin_ok"] is False
    assert "no PEP 610 local-origin record" in result["reason"]


def test_v8_dependency_so_cannot_satisfy_native_artifact_rung():
    orch = StrictNativeLadderOrch(
        artifact="/workspace/tvm/.venv/lib/python3.12/site-packages/numpy/core.so"
    )
    result = _validate_strict_native(orch)
    details = result["evidence"]["fingerprint_details"]

    assert result["success"] is True
    assert result["build_complete"] is False
    assert details["native_artifact_ok"] is False
    assert "native core not built" in result["reason"]
    native_find = next(
        command for command in orch.commands if "'*.so'" in command and "'*.dylib'" in command
    )
    assert "/workspace/tvm/build" in native_find
    assert "/workspace/tvm/python/tvm/lib" in native_find
    assert "/workspace/tvm/.venv" not in native_find


def test_v8_root_record_artifact_and_package_paths_verify_exact_scope():
    orch = StrictNativeLadderOrch(
        artifact="/workspace/tvm/build/libtvm.so",
        smoke_exists=False,
    )
    result = _validate_strict_native(orch)
    details = result["evidence"]["fingerprint_details"]

    assert result["build_complete"] is True
    assert details["distribution_record_ok"] is True
    assert details["distribution_origin_ok"] is True
    assert details["native_artifact_ok"] is True
    assert result["test_entry_ready"] is True
    compileall = next(command for command in orch.commands if "-m compileall" in command)
    assert compileall.split("-q ", 1)[1] == "/workspace/tvm/python/tvm"


def test_v8_subdir_install_origin_and_package_paths_use_python_root():
    manifest = _strict_native_manifest(
        python_distribution_name="native-pkg",
        python_packages=["native_pkg"],
        python_package_paths=[
            {
                "import_name": "native_pkg",
                "path": "src/native_pkg",
                "source": "pyproject.toml:tool.scikit-build.wheel.packages",
            }
        ],
        python_smoke_candidates=[],
        native_artifact_roots=[
            "python/_native-build",
            "python/src/native_pkg/lib",
        ],
        python_venv="/workspace/tvm/python/.venv",
        python_root="/workspace/tvm/python",
        build_root="/workspace/tvm/python",
    )
    root_record = {
        "path": (
            "/workspace/tvm/python/.venv/lib/python3.12/site-packages/" "native_pkg-1.0.dist-info"
        ),
        "top_level": "native_pkg\n",
        "direct_url": (
            '{"url": "file:///workspace/tvm/python", ' '"dir_info": {"editable": true}}'
        ),
    }
    artifact = "/workspace/tvm/python/_native-build/native_pkg.so"
    orch = StrictNativeLadderOrch(
        records=[root_record],
        artifact=artifact,
        smoke_exists=False,
        origin_realpath="/workspace/tvm/python",
        manifest=manifest,
    )

    result = _validate_strict_native(orch)

    assert result["build_complete"] is True
    details = result["evidence"]["fingerprint_details"]
    assert details["distribution_origin_ok"] is True
    assert details["native_artifact_ok"] is True
    compileall = next(command for command in orch.commands if "-m compileall" in command)
    assert compileall.split("-q ", 1)[1] == "/workspace/tvm/python/src/native_pkg"
    native_find = next(
        command for command in orch.commands if "'*.so'" in command and "'*.dylib'" in command
    )
    assert "/workspace/tvm/python/_native-build" in native_find
    assert "/workspace/tvm/python/src/native_pkg/lib" in native_find


def test_v8_subdir_install_symlink_cannot_escape_repository_root():
    manifest = _strict_native_manifest(
        python_venv="/workspace/tvm/python/.venv",
        python_root="/workspace/tvm/python",
        build_root="/workspace/tvm/python",
    )
    root_record = _record(
        "apache_tvm-0.22.dist-info",
        "tvm\n",
        '{"url": "file:///workspace/tvm/python", "dir_info": {"editable": true}}',
    )
    orch = StrictNativeLadderOrch(
        records=[root_record],
        origin_realpath="/outside/python",
        install_realpath="/outside/python",
        manifest=manifest,
    )

    result = _validate_strict_native(orch)

    assert result["success"] is False
    assert result["evidence"]["fingerprint_details"]["distribution_origin_ok"] is False


def test_v8_native_artifact_realpath_cannot_escape_surveyed_roots():
    orch = StrictNativeLadderOrch(
        artifact="/workspace/tvm/build/libtvm.so",
        artifact_realpath="/outside/build/libtvm.so",
    )
    result = _validate_strict_native(orch)

    assert result["build_complete"] is False
    assert result["evidence"]["fingerprint_details"]["native_artifact_ok"] is False
    assert "native core not built" in result["reason"]


def test_v8_unsafe_first_artifact_does_not_hide_later_owned_artifact():
    unsafe = "/workspace/tvm/build/unsafe.so"
    owned = "/workspace/tvm/python/tvm/lib/libtvm.so"
    orch = StrictNativeLadderOrch(
        artifact=f"{unsafe}\n{owned}",
        artifact_realpaths={unsafe: "/outside/build/unsafe.so"},
    )
    result = _validate_strict_native(orch)

    assert result["build_complete"] is True
    assert result["evidence"]["fingerprint_details"]["native_artifact_ok"] is True
    native_find = next(
        command for command in orch.commands if "'*.so'" in command and "'*.dylib'" in command
    )
    assert "head -1" not in native_find


def test_v8_native_test_entry_requires_artifact_or_existing_smoke_candidate():
    no_entry = StrictNativeLadderOrch(
        artifact="",
        smoke_exists=False,
    )
    no_entry_result = _validate_strict_native(no_entry)
    assert no_entry_result["test_entry_ready"] is False
    assert no_entry_result["evidence"]["test_entry_ready"] is False

    smoke = StrictNativeLadderOrch(
        artifact="",
        smoke_exists=True,
    )
    smoke_result = _validate_strict_native(smoke)
    assert smoke_result["test_entry_ready"] is True
    assert smoke_result["evidence"]["test_entry_ready"] is True
    assert smoke_result["evidence"]["test_entry_candidate"] == (
        "/workspace/tvm/tests/python/all-platform-minimal-test"
    )


def test_v8_missing_distribution_name_fails_closed_without_legacy_record_scan():
    manifest = _strict_native_manifest(python_distribution_name=None)
    orch = StrictNativeLadderOrch(manifest=manifest)
    validator = PhysicalValidator(
        docker_orchestrator=orch,
        project_path="/workspace",
    )

    def legacy_scan_forbidden(*args, **kwargs):
        raise AssertionError("facts-v8 must not enter the legacy distribution scan")

    validator._installed_top_level_packages = legacy_scan_forbidden

    result = validator.validate_build_status("tvm")

    assert result["success"] is True
    assert result["build_complete"] is False
    assert result["evidence_status"] == "partial"
    assert "python_distribution_name" in result["reason"]
    details = result["evidence"]["fingerprint_details"]
    assert details["distribution_record_ok"] is None
    assert details["distribution_origin_ok"] is None
    assert not any("site-packages" in command for command in orch.commands)


def test_v8_missing_distribution_name_with_no_venv_remains_blocked():
    manifest = _strict_native_manifest(python_distribution_name=None)
    orch = StrictNativeLadderOrch(manifest=manifest, venv_exists=False)

    result = _validate_strict_native(orch)

    assert result["success"] is False
    assert result["build_complete"] is False
    assert result["evidence_status"] == "blocked"
    assert "No venv" in result["reason"]
    assert not any("site-packages" in command for command in orch.commands)


def test_v8_unknown_smoke_source_is_not_test_ready():
    manifest = _strict_native_manifest(
        python_smoke_candidates=[
            {
                "path": "tests/python/all-platform-minimal-test",
                "source": "model:guessed-path",
            }
        ]
    )
    orch = StrictNativeLadderOrch(manifest=manifest)

    result = _validate_strict_native(orch)

    assert result["test_entry_ready"] is False
    assert result["evidence"]["test_entry_ready"] is False
    assert result["evidence"].get("test_entry_candidate") is None
    assert not any(
        "realpath -m -- /workspace/tvm "
        "/workspace/tvm/tests/python/all-platform-minimal-test" in command
        for command in orch.commands
    )


def test_v8_native_smoke_symlink_escape_is_not_test_ready():
    orch = StrictNativeLadderOrch(
        artifact="",
        smoke_exists=True,
        smoke_realpath="/outside/tests/test_runtime.py",
    )

    result = _validate_strict_native(orch)

    assert result["test_entry_ready"] is False
    assert result["evidence"]["test_entry_ready"] is False
    assert result["evidence"].get("test_entry_candidate") is None
