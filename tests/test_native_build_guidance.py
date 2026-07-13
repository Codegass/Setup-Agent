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
  B. guidance: the build-phase intro of a native repo PREPENDS the native-first
     block (build the native library first, then install from the detected
     python root); a plain-python repo's intro is byte-identical to before.
  C. validator: has_native_build + no built .so caps the build at PARTIAL with
     the reason "native core not built" (never BLOCKED — pure-python parts may
     still run); with a .so present the ladder is unchanged.
"""

import json
import re
from types import SimpleNamespace

from sag.agent.physical_validator import PhysicalValidator
from sag.agent.phase_machine import PhaseMachine
from sag.agent.react_engine import (
    PYTHON_BUILD_PHASE_GUIDANCE,
    ReActEngine,
)
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
            names = sorted(
                {p[len(base):].split("/")[0] for p in self.files if p.startswith(base)}
            )
            return {"success": True, "output": "\n".join(names)}
        if cmd.startswith("mkdir"):
            return {"success": True, "output": ""}
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
                hits = sorted(
                    d for d in self.dirs if any(d.endswith(s) for s in suffixes)
                )
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
        "from setuptools import setup\n"
        "setup(name='tvm', python_requires='>=3.8')\n"
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


_NATIVE_FIRST_MARKERS = (
    "NATIVE core",
    "CMakeLists.txt at the repo root",
    "build the native library FIRST",
    "will not import without it",
    f"{_TVM_ROOT}/python",  # the detected python root, named in the guidance
    "Long native builds detach; poll with search",
)


def test_native_build_intro_prepends_native_first_block():
    env = _env_from(_analyzed(_TVM_ROOT, _TVM_FILES)[0])
    intro = _engine_at(2, env)._phase_intro_step().content
    for marker in _NATIVE_FIRST_MARKERS:
        assert marker in intro, f"missing native-first marker: {marker!r}"
    # The native-first block PRECEDES the ordinary python build guidance (it is
    # a prepend, not a replacement — the deps/compile advice still follows).
    assert PYTHON_BUILD_PHASE_GUIDANCE in intro
    assert intro.index("build the native library FIRST") < intro.index(
        PYTHON_BUILD_PHASE_GUIDANCE
    )


def test_plain_python_intro_has_no_native_text_and_is_byte_identical():
    """A plain-python repo must carry ZERO native text and its build intro must
    be byte-identical to the same repo analyzed with the native path never
    firing (snapshot below, captured from the plain-repo chain)."""
    env = _env_from(_analyzed(_PLAIN_ROOT, _PLAIN_FILES)[0])
    intro = _engine_at(2, env)._phase_intro_step().content
    assert "NATIVE core" not in intro
    assert "native library FIRST" not in intro
    assert intro == _PLAIN_BUILD_INTRO_SNAPSHOT


# Captured VERBATIM from the plain-python chain (no native path). If this
# fails, the plain-python intro changed — out of scope for T5 and must be an
# intentional, separate change.
_PLAIN_BUILD_INTRO_SNAPSHOT = (
    "=== PHASE: BUILD ===\n"
    "Run picture so far:\n"
    "✓ provision: repo cloned; toolchain installed\n"
    "✓ analyze: python project analyzed\n"
    "→ current: build\n"
    "\n"
    "Objective: Set up the environment and install dependencies: "
    "build(action='deps'), then verify byte-compilation with "
    "build(action='compile'). A Python project has no Java compile target — "
    "that is NOT grounds for phase(action='blocked'). Block only when the "
    "environment or dependency install itself genuinely fails, with that "
    "evidence. Never run pip/python via bash — build resolves the registered "
    "toolchain. Long installs detach; poll the job ref with search.\n"
    "This is a Python project — there is no Java compile target and that is "
    "NOT grounds for phase(action='blocked'). Do: build(action='deps') to "
    "create the venv and install dependencies with the project's own tool, "
    "then build(action='compile') to verify byte-compilation. Never run "
    "pip/pytest via bash — the build tool resolves the project venv.\n"
    "Recommended Build: python 'deps' in /workspace/pyproj — Python project "
    "(pip): create the venv and install with build(action='deps'), verify with "
    "build(action='compile'), test with build(action='test').\n"
    "Budget: flexible — up to ~132 iterations available (a small reserve is "
    "kept for later phases). When finished, call phase(action='done', "
    "key_results=..., evidence=[refs]). If it cannot be finished, "
    "phase(action='blocked', reason=..., evidence=[refs])."
)


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
        if "compileall" in c:
            return res(True)
        if "__pycache__" in c and "wc -l" in c:
            return res(True, "10")
        if "'*.py'" in c and "wc -l" in c:
            return res(True, "10")
        if "'*.so'" in c or "'*.dylib'" in c:
            return res(True, "/workspace/tvm/python/tvm/libtvm.so" if self.so_present else "")
        if "'*.jar'" in c or "'*.class'" in c:
            return res(True, "0")
        return res(True, "")


def _validate(orch):
    validator = PhysicalValidator(
        docker_orchestrator=orch, project_path="/workspace/tvm/python"
    )
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
    orch = NativeLadderOrch(
        so_present=False, manifest=_native_manifest(has_native_build=False)
    )
    result = _validate(orch)
    assert result["success"] is True
    assert result["build_complete"] is True
    assert result["evidence_status"] == "success"
    assert "native core not built" not in result["reason"]
