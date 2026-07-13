"""JDK/Python pre-flight + build-requirements manifest.

The pre-flight CONSUMES the phase-1 analysis (it is a guarantee layer, not a
second analyzer): the analyzer persists requirements into the container at
REQUIREMENTS_PATH; MavenTool/GradleTool call JdkPreflight at the top of every
build/test execution. When the environment already matches, the pre-flight is
a single `java -version` no-op. See
docs/superpowers/specs/2026-07-06-java-execution-strategy-fixes-design.md.

PythonPreflight is the same contract for Python interpreters (uv -> apt
provision ladder), per
docs/superpowers/specs/2026-07-07-python-project-support-design.md Component 2.
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from loguru import logger

from sag.tools.internal.python_env import (
    _REPAIR_ACTION_PHRASE,
    ensure_venv_pip,
    resolve_python_version,
)

REQUIREMENTS_PATH = "/workspace/.setup_agent/build_requirements.json"


def write_build_requirements(orchestrator, data: Dict[str, Any]) -> bool:
    """Persist the analyzer's build requirements into the container."""
    try:
        body = json.dumps(data, indent=2, sort_keys=True)
        orchestrator.execute_command("mkdir -p /workspace/.setup_agent")
        result = orchestrator.execute_command(
            f"cat > {REQUIREMENTS_PATH} <<'SAGEOF'\n{body}\nSAGEOF"
        )
        return bool(result.get("success"))
    except Exception as exc:
        logger.warning(f"Failed to write build requirements: {exc}")
        return False


def read_build_requirements(orchestrator) -> Dict[str, Any]:
    """Read the manifest; {} when absent or corrupt (callers degrade gracefully)."""
    try:
        result = orchestrator.execute_command(f"cat {REQUIREMENTS_PATH}")
        if not result.get("success"):
            return {}
        return json.loads(result.get("output") or "")
    except Exception:
        return {}


_JAVA_VERSION_RE = re.compile(r'version "(?:1\.)?(\d+)')

# Adoptium/Temurin apt repo for JDKs missing from the base image's Debian
# release (e.g. JDK 8 on bookworm). One-shot, idempotent.
_TEMURIN_SETUP = (
    "apt-get install -y wget apt-transport-https gnupg >/dev/null 2>&1; "
    "wget -qO- https://packages.adoptium.net/artifactory/api/gpg/key/public "
    "| gpg --dearmor -o /usr/share/keyrings/adoptium.gpg 2>/dev/null; "
    'echo "deb [signed-by=/usr/share/keyrings/adoptium.gpg] '
    'https://packages.adoptium.net/artifactory/deb '
    '$(. /etc/os-release && echo $VERSION_CODENAME) main" '
    "> /etc/apt/sources.list.d/adoptium.list && apt-get update"
)


def active_java_major(orchestrator) -> Optional[str]:
    """Major version of the currently active `java`, or None."""
    result = orchestrator.execute_command("java -version 2>&1")
    match = _JAVA_VERSION_RE.search(result.get("output") or "")
    return match.group(1) if match else None


def _register_overlay(orchestrator, java_home: str, version: str) -> bool:
    """Register the provisioned JDK in the shared env overlay (report-visible)."""
    try:
        from sag.runtime.env_overlay import EnvOverlayStore

        EnvOverlayStore(orchestrator).register(
            "java",
            f"{java_home}/bin/java",
            version=version,
            source="build_preflight",
            env={"JAVA_HOME": java_home},
            path_prepend=[f"{java_home}/bin"],
            activate=True,
        )
        return True
    except Exception as exc:
        logger.warning(f"Pre-flight overlay registration failed: {exc}")
        return False


@dataclass
class PreflightOutcome:
    matched: bool
    active_version: Optional[str]
    required_version: Optional[str]
    provisioned: bool = False
    mismatch: bool = False
    narration: str = ""


class JdkPreflight:
    """Check-and-fix JDK guarantee. Never raises; never blocks (spec §1b)."""

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator

    def run(self, required_version: Optional[str], source: str = "unknown") -> PreflightOutcome:
        try:
            return self._run(required_version, source)
        except Exception as exc:  # never let the pre-flight kill a build
            logger.warning(f"JDK pre-flight error (continuing): {exc}")
            return PreflightOutcome(True, None, required_version)

    def _run(self, required: Optional[str], source: str) -> PreflightOutcome:
        if not required:
            return PreflightOutcome(True, None, None)
        active = active_java_major(self.orchestrator)
        if active == required:
            logger.debug(f"JDK pre-flight: active Java {active} matches requirement")
            return PreflightOutcome(True, active, required)

        header = (
            f"[pre-flight] Required: Java {required} (source: {source}). "
            f"Active: Java {active or 'unknown'}."
        )
        java_home = self._provision(required)
        if java_home:
            _register_overlay(self.orchestrator, java_home, required)
            return PreflightOutcome(
                matched=False, active_version=active, required_version=required,
                provisioned=True,
                narration=(
                    f"{header}\n→ installed JDK {required}, "
                    f"JAVA_HOME={java_home} (overlay registered)"
                ),
            )
        return PreflightOutcome(
            matched=False, active_version=active, required_version=required,
            provisioned=False, mismatch=True,
            narration=(
                f"{header}\n→ could not provision JDK {required} "
                f"(apt + Temurin exhausted); continuing on Java {active or 'unknown'} — "
                "the verdict will record jdk_mismatch"
            ),
        )

    def _provision(self, version: str) -> Optional[str]:
        """apt -> Temurin ladder; returns JAVA_HOME on success, None on failure."""
        apt = self.orchestrator.execute_command(
            f"DEBIAN_FRONTEND=noninteractive apt-get update >/dev/null 2>&1; "
            f"DEBIAN_FRONTEND=noninteractive apt-get install -y openjdk-{version}-jdk"
        )
        if not apt.get("success"):
            self.orchestrator.execute_command(_TEMURIN_SETUP)
            temurin = self.orchestrator.execute_command(
                f"DEBIAN_FRONTEND=noninteractive apt-get install -y temurin-{version}-jdk"
            )
            if not temurin.get("success"):
                return None
        home = self.orchestrator.execute_command(
            f"ls -d /usr/lib/jvm/java-{version}-openjdk-* "
            f"/usr/lib/jvm/temurin-{version}-jdk* 2>/dev/null | head -1"
        )
        java_home = (home.get("output") or "").strip().splitlines()
        java_home = java_home[0].strip() if java_home else ""
        if not java_home:
            return None
        self.orchestrator.execute_command(
            f"update-alternatives --install /usr/bin/java java {java_home}/bin/java 100 "
            f"&& update-alternatives --set java {java_home}/bin/java; "
            f"test -x {java_home}/bin/javac && "
            f"update-alternatives --install /usr/bin/javac javac {java_home}/bin/javac 100 "
            f"&& update-alternatives --set javac {java_home}/bin/javac"
        )
        return java_home


# Version-shaped build failures, in match priority. Each pattern captures the
# JDK major the build ACTUALLY needs (the honest, authoritative signal that
# static pom analysis cannot always see — spec §1c).
_VERSION_ERROR_PATTERNS = [
    # enforcer: "... allowed range [17,)" / "allowed version range [11,17)"
    re.compile(r"RequireJavaVersion.*?allowed(?:\s+version)?\s+range\s*\[?(\d+)", re.DOTALL | re.IGNORECASE),
    # javac: "invalid target release: 21" / "release version 17 not supported"
    re.compile(r"invalid (?:target|source) release:?\s*(?:1\.)?(\d+)", re.IGNORECASE),
    re.compile(r"release version (\d+) not supported", re.IGNORECASE),
]
_CLASS_FILE_VERSION = re.compile(r"class file version (\d+)\.")

# Old-Groovy vs new-JDK compiler transform incompatibility (live bigtop R3):
# bigtop's Groovy AST-transforming plugins emit this on JDK >= 11 while the
# maven modules are meant to build on JDK 8. Unlike the other signatures the
# error text names no target version — the remediation is the classic
# groovy-on-jdk8 downgrade, so this is a fixed "8" SENTINEL. classify_version_error
# is pure-text; the caller's `needed != active` gate makes it a no-op when the
# build is already on JDK 8 (needed == active), so the retry stays bounded and
# never fires spuriously.
_GROOVY_TRANSFORM_TYPERESOLVER = re.compile(
    r"wrong descriptors and a potential NullPointerException in TypeResolver"
    r"|Groovy:A transform used a generics containing ClassNode",
    re.IGNORECASE,
)


def classify_version_error(output: str) -> Optional[str]:
    """Extract the JDK major a failed build says it needs, else None."""
    if not output:
        return None
    for pattern in _VERSION_ERROR_PATTERNS:
        match = pattern.search(output)
        if match:
            return match.group(1)
    match = _CLASS_FILE_VERSION.search(output)
    if match:
        # Class-file major 52 = JDK 8, 61 = JDK 17: major - 44.
        return str(int(match.group(1)) - 44)
    if _GROOVY_TRANSFORM_TYPERESOLVER.search(output):
        # Old-Groovy AST transform breaks on JDK >= 11: remediate to JDK 8.
        return "8"
    return None


# ---------------------------------------------------------------------------
# Python pre-flight (spec 2026-07-07 Component 2): same PreflightOutcome
# contract as JdkPreflight — check-and-fix, never raises, never blocks.
# ---------------------------------------------------------------------------

_PYTHON_VERSION_RE = re.compile(r"Python\s+(\d+\.\d+)")

_UV_INSTALL = "curl -LsSf https://astral.sh/uv/install.sh | sh"
# uv lands in ~/.local/bin; every uv invocation prepends it so the install
# rung and the provisioning calls agree on PATH.
_UV_PATH = 'export PATH="$HOME/.local/bin:$PATH"'


def active_python_version(orchestrator) -> Optional[str]:
    """major.minor of the currently active `python3`, or None."""
    result = orchestrator.execute_command("python3 --version 2>&1")
    match = _PYTHON_VERSION_RE.search(result.get("output") or "")
    return match.group(1) if match else None


def _register_python_overlay(orchestrator, venv: str, version: str) -> bool:
    """Register the provisioned interpreter/venv in the shared env overlay."""
    try:
        from sag.runtime.env_overlay import EnvOverlayStore

        EnvOverlayStore(orchestrator).register(
            "python",
            f"{venv}/bin/python",
            version=version,
            source="python_preflight",
            env={"VIRTUAL_ENV": venv},
            path_prepend=[f"{venv}/bin"],
            activate=True,
        )
        return True
    except Exception as exc:
        logger.warning(f"Python pre-flight overlay registration failed: {exc}")
        return False


class PythonPreflight:
    """Check-and-fix Python interpreter guarantee (uv -> apt ladder).

    Mirrors JdkPreflight: satisfied (or no) requirement -> no-op; mismatch ->
    provision + narrate; ladder exhausted -> mismatch=True, which the verifier
    maps to the python_version_mismatch conflict. Never raises, never blocks.
    """

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator

    def run(
        self,
        required_version: Optional[str],
        constraint: Optional[str] = None,
        source: str = "unknown",
    ) -> PreflightOutcome:
        try:
            return self._run(required_version, constraint, source)
        except Exception as exc:  # never let the pre-flight kill a build
            logger.warning(f"Python pre-flight error (continuing): {exc}")
            return PreflightOutcome(True, None, required_version)

    def _run(self, required: Optional[str], constraint: Optional[str], source: str) -> PreflightOutcome:
        if not required:
            return PreflightOutcome(True, None, None)
        active = active_python_version(self.orchestrator)
        if active == required or self._constraint_satisfied(active, constraint):
            logger.debug(f"Python pre-flight: active {active} satisfies requirement")
            # A version MATCH is necessary but not sufficient: the live TVM run
            # (session 20260713_014403_27874) had system python 3.12.3 satisfy
            # '>=3.10' yet ship NO ensurepip, so it could not create a working
            # venv and the broken toolchain sailed through to fail later in
            # deps. Verify FUNCTION, not just version — cheaply (module presence
            # probe, no side effects), and repair via the SAME rungs a mismatch
            # would use. Never flips the (correct) version match to a mismatch.
            if self._can_create_venvs():
                return PreflightOutcome(True, active, required)
            return self._repair_matched_but_broken(active, required, source)

        header = (
            f"[pre-flight] Required: Python {required} (source: {source}). "
            f"Active: {active or 'unknown'}."
        )
        venv = self._venv_path()
        rung = self._provision(required, venv)
        if rung:
            pip_note = self._ensure_venv_pip(venv, required)
            _register_python_overlay(self.orchestrator, venv, required)
            narration = (
                f"{header}\n→ {rung}-provisioned {required}, "
                f"venv at {venv} (overlay registered)"
            )
            if pip_note:
                narration += f"\n{pip_note}"
            return PreflightOutcome(
                matched=False, active_version=active, required_version=required,
                provisioned=True, narration=narration,
            )
        return PreflightOutcome(
            matched=False, active_version=active, required_version=required,
            provisioned=False, mismatch=True,
            narration=(
                f"{header}\n→ could not provision Python {required} "
                f"(uv + apt exhausted); continuing on Python {active or 'unknown'} — "
                "the verdict will record python_version_mismatch"
            ),
        )

    @staticmethod
    def _constraint_satisfied(active: Optional[str], constraint: Optional[str]) -> bool:
        """The active interpreter may satisfy the raw constraint without being
        the resolved newest (e.g. 3.12 for '>=3.9'): the pre-flight guarantees
        requirements, it does not chase the newest interpreter."""
        if not active or not constraint:
            return False
        return resolve_python_version(constraint, [active]) == active

    def _can_create_venvs(self) -> bool:
        """Cheaply verify the ACTIVE interpreter can create a working venv:
        probe for the ``ensurepip`` module (Debian splits it out, so a
        version-matching python can still yield a pip-less venv — the live TVM
        trap). ``python3 -m ensurepip --version`` is a pure module-presence
        check with no side effects; the ``importlib.util.find_spec`` fallback
        covers interpreters whose ensurepip lacks ``--version``.

        Cached PER RUN on the shared orchestrator: multiple tools each build
        their own PythonPreflight in one run, but the probe touches the
        container exactly once (the interpreter does not change mid-run)."""
        cached = getattr(self.orchestrator, "_python_ensurepip_ok", None)
        if cached is not None:
            return cached
        probe = self.orchestrator.execute_command(
            "python3 -m ensurepip --version 2>/dev/null "
            "|| python3 -c \"import importlib.util,sys; "
            "sys.exit(0 if importlib.util.find_spec('ensurepip') else 1)\""
        )
        ok = bool(probe.get("success"))
        try:
            self.orchestrator._python_ensurepip_ok = ok
        except Exception:  # a read-only/exotic orchestrator: skip the cache
            pass
        return ok

    def _repair_matched_but_broken(
        self, active: Optional[str], required: str, source: str
    ) -> PreflightOutcome:
        """Version matched but the interpreter cannot create venvs (no
        ensurepip). Narrate the honest defect and run the SAME apt/uv repair
        rungs a mismatch uses (shared ``ensure_venv_pip`` via
        ``_ensure_venv_pip`` — never duplicated). Provisioning failure keeps the
        existing degrade semantics: narrated, never a hard block, and — because
        the VERSION genuinely matched — never a version mismatch flag."""
        venv = self._venv_path()
        header = (
            f"[pre-flight] Python {active} matches the constraint but cannot "
            f"create venvs (no ensurepip) — repairing (source: {source})"
        )
        pip_note = self._ensure_venv_pip(venv, required)
        # A successful repair means later tools in this run need neither probe
        # nor repair: flip the per-run cache to healthy. A still-broken pip
        # leaves the cache False so the honest narration recurs (never silent).
        if pip_note is None or "still missing" not in pip_note:
            try:
                self.orchestrator._python_ensurepip_ok = True
            except Exception:
                pass
        _register_python_overlay(self.orchestrator, venv, required)
        narration = header
        if pip_note:
            narration += f"\n{pip_note}"
        return PreflightOutcome(
            matched=True, active_version=active, required_version=required,
            provisioned=True, mismatch=False, narration=narration,
        )

    def _venv_path(self) -> str:
        """Project venv from the analyzer manifest; /workspace/.venv fallback."""
        return read_build_requirements(self.orchestrator).get("python_venv") or "/workspace/.venv"

    def _provision(self, version: str, venv: str) -> Optional[str]:
        """uv -> apt ladder; returns the rung name on success, None on failure."""
        if self._uv_available() and self._uv_provision(version, venv):
            return "uv"
        if self._apt_provision(version, venv):
            return "apt"
        return None

    def _uv_available(self) -> bool:
        probe = self.orchestrator.execute_command(f"{_UV_PATH}; command -v uv")
        if probe.get("success"):
            return True
        install = self.orchestrator.execute_command(_UV_INSTALL)
        if not install.get("success"):
            return False
        probe = self.orchestrator.execute_command(f"{_UV_PATH}; command -v uv")
        return bool(probe.get("success"))

    def _uv_provision(self, version: str, venv: str) -> bool:
        install = self.orchestrator.execute_command(
            f"{_UV_PATH}; uv python install {version}"
        )
        if not install.get("success"):
            return False
        # --seed: a plain `uv venv` ships NO pip inside the venv (bug #12),
        # which broke every `{venv}/bin/python -m pip ...` rung downstream.
        made = self.orchestrator.execute_command(
            f"{_UV_PATH}; uv venv --seed --python {version} {venv}"
        )
        return bool(made.get("success"))

    def _ensure_venv_pip(self, venv: str, version: Optional[str] = None) -> Optional[str]:
        """Verify pip exists inside the fresh venv; repair via the shared
        python_env.ensure_venv_pip ladder (bug #12 / bug #13 defect 1, live
        TVM failure): probe -> ensurepip -> recreate -> apt python3-venv ->
        uv venv --seed -> re-probe between each. Never blocks: a still-missing
        pip returns a narration line naming every rung and the run continues."""
        repair = ensure_venv_pip(self.orchestrator, venv, python_version=version)
        action = repair.get("action")
        ladder = repair.get("ladder") or []
        if repair.get("ok"):
            if action is None:
                return None
            phrase = _REPAIR_ACTION_PHRASE.get(action, "repaired")
            return f"→ venv had no pip; {phrase} at {venv}"
        tried = "; ".join(ladder) if ladder else "ensurepip and recreation"
        return (
            f"→ pip still missing in {venv} after the repair ladder "
            f"(tried: {tried}) — pip-based install commands will fail; "
            f"continuing (never blocks)"
        )

    def _apt_provision(self, version: str, venv: str) -> bool:
        apt = self.orchestrator.execute_command(
            f"DEBIAN_FRONTEND=noninteractive apt-get update >/dev/null 2>&1; "
            f"DEBIAN_FRONTEND=noninteractive apt-get install -y "
            f"python{version}-venv python{version}"
        )
        if not apt.get("success"):
            return False
        made = self.orchestrator.execute_command(f"python{version} -m venv {venv}")
        return bool(made.get("success"))


# pip's version-shaped rejections, in match priority. Each captures the
# Requires-Python constraint; the interpreter to provision is the newest
# supported CPython satisfying it (spec Component 1 policy, via
# resolve_python_version). A bare SyntaxError is deliberately NOT classified:
# it cannot distinguish "needs a newer interpreter" from "broken source".
_PIP_REQUIRES_PYTHON_PATTERNS = [
    # "requires a different Python: 3.8.10 not in '>=3.10'"
    re.compile(r"requires a different Python:\s*[\d.]+\s+not in\s+['\"]([^'\"]+)['\"]"),
    # "... 24.2 Requires-Python >=3.10; ..." / metadata "Requires-Python: >=3.10"
    re.compile(r"Requires-Python[:\s]\s*([^;\n]+)"),
]


def classify_python_version_error(output: str) -> Optional[str]:
    """Extract the Python major.minor a failed pip install says it needs.

    Only pip's explicit Requires-Python rejections are actionable; anything
    non-version-shaped (including bare SyntaxErrors) returns None so the
    bounded retry never fires on ambiguity."""
    if not output:
        return None
    for pattern in _PIP_REQUIRES_PYTHON_PATTERNS:
        match = pattern.search(output)
        if match:
            return resolve_python_version(match.group(1).strip())
    return None
