"""Python tool: setup_env / test / build / compile for Python projects.

Manifest-driven and narrated (spec 2026-07-07 Component 3): the venv and the
install commands come from the analyzer's build-requirements manifest; the
PythonPreflight guarantee layer runs first (check-and-fix, NEVER a hard
block); a failed poetry/pipenv install falls back to the pip rung narrated
as a faithfulness deviation; a version-shaped pip failure re-provisions and
reruns exactly once. The test operation records the collect-only denominator
and produces standard JUnit XML for the verifier — one honest run per suite,
never re-run on test failures. The wheel build is extra evidence, never
required for a green verdict.
"""

import hashlib
import json
import posixpath
import re
import shlex
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from loguru import logger

from sag.agent.evidence_assessments import (
    ControlAssessment,
    next_control_event_id,
    write_assessment,
)
from sag.agent.invocation_contracts import (
    CONTRACT_AUTHORITY_MISSING,
    contract_receipt_fields,
    current_contract,
    dispatch_contract,
    ensure_dispatch_contract,
    python_facade_dispatch_matches,
)
from sag.agent.invocation_receipts import (
    producer_observations_sha256,
    python_import_targets,
    record_invocation,
    snapshot_reports,
)
from sag.agent.job_obligations import record_dispatch_obligation_result
from sag.evidence import TestStats
from sag.testcases.compileall_metrics import (
    COMPILEALL_METRICS_UNAVAILABLE_CONFLICT,
    compileall_metrics_command,
    parse_compileall_metrics,
)

from ..base import BaseTool, ToolResult

# The native allowlists are the FACADE's module data (spec §C8). They are
# re-read here rather than trusted from the materialized params so the physical
# command line is derived from the same tables the facade validated against —
# no path into this executor can widen them.
from ..build.backends import (
    NATIVE_DEFINITION_ENV,
    NATIVE_DEFINITION_KEY,
    NATIVE_DEFINITION_VALUES,
    NATIVE_FEATURE_RESOLVER,
    native_cmake_args,
)
from .build_preflight import (
    PythonPreflight,
    active_python_version,
    classify_python_version_error,
    read_live_build_requirements,
)
from .build_utils import (
    DETACHED_HANDOFF_STATUSES,
    detached_handoff_tool_result,
)
from .python_env import (
    detect_installer,
    discover_packages,
    ensure_venv_pip,
    project_name_from_pyproject,
    venv_repair_note,
)

# The verifier (Task 6) reads both: the JUnit XML under PYTEST_REPORT_DIR for
# executed counts, COLLECTED_JSON as the detected-tests denominator feeding
# the tests_not_fully_executed gate.
PYTEST_REPORT_DIR = "/workspace/.setup_agent/pytest-reports"
COLLECTED_JSON = "/workspace/.setup_agent/pytest_collected.json"
# Plan 4 Task 1 (audit 2026-07-26): the CAPABILITY receipt. A native project
# unlocks a full collect only after a bounded smoke actually executed tests on
# this project root — readiness probes never unlock it.
NATIVE_SMOKE_RECEIPT_JSON = "/workspace/.setup_agent/native_smoke_receipt.json"
# Error codes whose attempt executed nothing at all.
_PYTEST_NOTHING_RAN_CODES = frozenset(
    {"PYTEST_COLLECTION_ERROR", "PYTEST_NO_TESTS", "PYTEST_USAGE_ERROR", "PYTEST_MISSING"}
)
BUILD_REQUIREMENTS_UNAVAILABLE = "BUILD_REQUIREMENTS_UNAVAILABLE"

# The pip rung a failed poetry/pipenv install falls back to (narrated).
# Module form (bug #12): plain uv venvs ship no {venv}/bin/pip binary.
_PIP_FALLBACK = "{venv}/bin/python -m pip install -e ."

_PYTHON_IMPORT_PROBE_SCRIPT = """\
# SAG_PYTHON_IMPORT_PROBE
import contextlib
import importlib
import json
import os
import sys

targets = json.loads(sys.argv[1])
importable = []
failures = []
with open(os.devnull, "w") as sink:
    for target in targets:
        try:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                importlib.import_module(target)
        except BaseException as exc:
            failures.append({"target": target, "error_type": type(exc).__name__})
        else:
            importable.append(target)
print(json.dumps(
    {"targets": targets, "importable": importable, "failures": failures},
    separators=(",", ":"),
    sort_keys=True,
))
"""

_WHEEL_SNAPSHOT_PREFIX = "SAG_WHEEL_SNAPSHOT"
_PYTHON_IMPORT_TARGET_CAP = 64
_PYTHON_IMPORT_FAILURE_SAMPLE_CAP = 20


@dataclass
class _ProducerStep:
    ordinal: int
    role: str
    mechanical: bool
    argv: str
    result: Dict[str, Any]


@dataclass
class _ProducerTrace:
    operation: str
    steps: List[_ProducerStep] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)

    def record(self, role: str, mechanical: bool, argv: str, result: Dict[str, Any]) -> None:
        self.steps.append(
            _ProducerStep(
                ordinal=len(self.steps) + 1,
                role=role,
                mechanical=mechanical,
                argv=argv,
                result=dict(result or {}),
            )
        )


class _ProducerDetached(RuntimeError):
    def __init__(self, command: str, result: Dict[str, Any]):
        super().__init__("python producer dispatch handed off")
        self.command = command
        self.result = result


_ACTIVE_PRODUCER_TRACE: ContextVar[Optional[_ProducerTrace]] = ContextVar(
    "sag_python_producer_trace", default=None
)
# The rung after a successful local-provider install whose root retry re-fails
# on the provider's OWN distribution: the in-repo build resolves BELOW the
# declared floor (PEP 440 orders 0.1.13.dev47 < 0.1.13), so resolution can
# never succeed. Install the root project without resolving, then the declared
# dependencies the provider does not supply.
_PIP_NO_DEPS = "{venv}/bin/python -m pip install -e . --no-deps"
# The resolver's package install, in the house form (`project_setup_tool`'s JDK
# install): refresh first, then a non-interactive install so a live run cannot
# hang on a prompt. Every token outside `{packages}` is harness-owned, and
# `{packages}` is the resolver's own allowlisted list — spec §C8's "no
# model-controlled tokens" holds across the whole command line.
_APT_REFRESH = "apt-get update"
_APT_INSTALL = "DEBIAN_FRONTEND=noninteractive apt-get install -y {packages}"

_COLLECTED_RE = re.compile(r"(\d+)\s+tests?\s+collected")
# The SELECTED count of a filtered collection: the X of pytest's
# "X/Y tests collected (Z deselected)". The plain _COLLECTED_RE would match
# Y (the digits touching "tests collected") — the TOTAL, not the selection.
_SELECTED_RE = re.compile(r"(\d+)/\d+\s+tests?\s+collected")
_NO_TESTS_RE = re.compile(r"no tests collected|no tests ran")

# Bug #13 defect 2: install-failure signatures that must redden the result
# even when the wrapper reports exit 0 (live evidence: "No module named pip"
# on a run that claimed success while nothing installed).
_INSTALL_ERROR_RE = re.compile(
    r"No module named pip"
    r"|error: subprocess-exited-with-error"
    r"|ERROR: No matching distribution found"
    r"|ERROR: Could not find a version"
    r"|ERROR: Could not install"
)
_MISSING_DISTRIBUTION_RE = re.compile(
    r"(?:Could not find a version that satisfies the requirement|"
    r"No matching distribution found for)\s+"
    r"(?P<requirement>[A-Za-z0-9][A-Za-z0-9._-]*(?:[<>=!~].*)?)",
    re.IGNORECASE,
)

# Bug #13 defect 6: honest pytest outcome classification.
_FAILED_STATS_RE = re.compile(r"\b\d+ failed\b")
# Pytest's own summary stats line ("1 failed, 5 passed in 0.34s"): when it is
# present the suite RAN — text-signature fallbacks must never override it.
_SUMMARY_STATS_RE = re.compile(r"\b\d+ (?:passed|failed)\b")
# Reviewer-confirmed defect (criterion f): these signatures previously
# substring-matched ANYWHERE in the output — including captured stdout/stderr
# of the tests under test (argparse's 'prog: error: unrecognized arguments'
# on any CLI-heavy project). Anchored to pytest's OWN line shapes: the
# 'ERROR: usage:' prefix only pytest prints at line start, the collection
# ERROR header line, and the '!! Interrupted: N errors during collection !!'
# band. Applied only when the exit code is unreliable (0/None) and no
# summary stats line exists.
_COLLECTION_ERROR_RE = re.compile(
    r"^_*\s*ERROR collecting\b" r"|!!+\s*Interrupted: \d+ errors? during collection",
    re.MULTILINE,
)
_CONFTEST_IMPORT_ERROR_RE = re.compile(
    r"^(?:STDERR:\s*)?ImportError while loading conftest\b",
    re.MULTILINE,
)
_USAGE_ERROR_RE = re.compile(r"^ERROR: usage:", re.MULTILINE)

# Bug #13 defect 7: pytest-plausible flags (simple allowlist heuristic).
# -k/-m/--maxfail take a value token; everything else must fullmatch here or
# be an EXISTING test path — 'make test' never reaches a pytest command line.
_PYTEST_VALUE_FLAGS = ("-k", "-m", "--maxfail")
_PYTEST_FLAG_RE = re.compile(
    r"-x|-q|-s|-v{1,3}|-r[a-zA-Z]+|--lf|--ff|--nf|--maxfail=\d+"
    r"|--tb=(?:auto|long|short|line|native|no)|--durations=\d+|--collect-only|--co"
)

_PYTEST_USAGE_HINT = (
    "Pass pytest-style args only: existing test paths and flags like "
    "-k EXPR, -m MARK, -x, -q, -v, -s, --maxfail=N, --lf, --ff, --tb=STYLE"
)

# Survey provenance is executable only when it comes from one of the two
# descriptive discovery paths.  Keep this as the single allowlist consumed by
# both PythonTool and PhysicalValidator so an unknown/model-authored source
# cannot be accepted by one side and rejected by the other.
PYTHON_SMOKE_CANDIDATE_SOURCES = frozenset(
    {
        "pyproject.toml:tool.cibuildwheel.test-command",
        "filesystem:test-file",
    }
)


def verify_project_owned_path(
    execute_command: Callable[[str], Dict[str, Any]],
    project_root: str,
    candidate: str,
) -> Tuple[bool, Optional[str]]:
    """Re-verify that an existing absolute path is a real child of the project.

    Lexical containment alone is insufficient because a path inside the
    checkout can be a symlink to a host/shared tree.  Callers get a stable
    reason so the model can recover without being shown a runnable unsafe
    selector.
    """
    root = posixpath.normpath(str(project_root or "").strip())
    path = posixpath.normpath(str(candidate or "").strip())
    if not root.startswith("/workspace/"):
        return False, "the surveyed project root is not a scoped /workspace path"
    if not path.startswith("/") or not path.startswith(f"{root}/"):
        return False, "the path is outside the surveyed project"

    realpath = execute_command(
        "realpath -m -- " f"{shlex.quote('/workspace')} {shlex.quote(root)} {shlex.quote(path)}"
    )
    resolved = [
        line.strip() for line in (realpath.get("output") or "").splitlines() if line.strip()
    ]
    if not realpath.get("success") or len(resolved) != 3:
        return False, "the path could not be resolved for project ownership"
    workspace_real, root_real, path_real = resolved
    if workspace_real == "/" or not root_real.startswith(workspace_real.rstrip("/") + "/"):
        return False, "the surveyed project root real path escapes resolved /workspace"
    if not path_real.startswith(root_real.rstrip("/") + "/"):
        return False, "the path real path escapes the surveyed project"

    exists = execute_command(f"test -e {shlex.quote(path)} && echo EXISTS || echo MISSING")
    if "EXISTS" not in (exists.get("output") or ""):
        return False, "the path does not currently exist"
    return True, None


def verify_path_within_smoke_boundary(
    execute_command: Callable[[str], Dict[str, Any]],
    smoke_boundary: str,
    candidate: str,
) -> Tuple[bool, Optional[str]]:
    """Require a pytest coordinate to be no broader than the surveyed smoke."""
    boundary = posixpath.normpath(str(smoke_boundary or "").strip())
    path = posixpath.normpath(str(candidate or "").strip())
    if path != boundary and not path.startswith(f"{boundary}/"):
        return False, "the path is broader than the verified survey smoke coordinate"

    realpath = execute_command("realpath -m -- " f"{shlex.quote(boundary)} {shlex.quote(path)}")
    resolved = [
        line.strip() for line in (realpath.get("output") or "").splitlines() if line.strip()
    ]
    if not realpath.get("success") or len(resolved) != 2:
        return False, "the smoke-bounded path could not be resolved"
    if resolved[1] != resolved[0] and not resolved[1].startswith(resolved[0].rstrip("/") + "/"):
        return False, "the path real path escapes the verified survey smoke coordinate"
    return True, None


def verified_python_smoke_candidate(
    execute_command: Callable[[str], Dict[str, Any]],
    project_root: str,
    candidates: List[Dict[str, Any]],
) -> Optional[Dict[str, str]]:
    """Return the first allowlisted, current, realpath-contained smoke fact."""
    root = posixpath.normpath(str(project_root or "").strip())
    if not root.startswith("/workspace/"):
        return None
    for entry in candidates:
        if not isinstance(entry, dict) or entry.get("source") not in PYTHON_SMOKE_CANDIDATE_SOURCES:
            continue
        relative = str(entry.get("path") or "").strip()
        if not relative or relative.startswith("/"):
            continue
        normalized = posixpath.normpath(relative)
        if normalized in ("", ".") or normalized == ".." or normalized.startswith("../"):
            continue
        full = posixpath.normpath(posixpath.join(root, normalized))
        owned, _ = verify_project_owned_path(execute_command, root, full)
        if not owned:
            continue
        return {
            "path": normalized,
            "source": str(entry["source"]),
            "absolute_path": full,
        }
    return None


_OPERATIONS = ("setup_env", "test", "build", "compile", "native")
_PYTEST_JUNIT_CONFLICT = "pytest_junit_unavailable"
_PYTEST_ATTEMPT_ID_CONFLICT = "pytest_attempt_id_unpersisted"
_PYTEST_JUNIT_MAX_COUNT = (1 << 63) - 1
_PYTEST_JUNIT_SUMMARY_MAX_BYTES = 512
_PYTEST_JUNIT_ERROR_REASONS = frozenset(
    {
        "missing",
        "malformed",
        "unreadable",
        "unsupported",
        "invalid_counts",
        "extract_failed",
    }
)
_PYTEST_JUNIT_EXTRACT_SCRIPT = """\
import json
import sys
import xml.etree.ElementTree as ET

def emit(payload):
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))

def unavailable(reason):
    emit({"error": reason, "ok": False})
    raise SystemExit(0)

try:
    root = ET.parse(sys.argv[1]).getroot()
except FileNotFoundError:
    unavailable("missing")
except ET.ParseError:
    unavailable("malformed")
except OSError:
    unavailable("unreadable")

name = root.tag.rsplit("}", 1)[-1]
if name == "testsuite":
    suites = [root]
elif name == "testsuites":
    suites = [root] if "tests" in root.attrib else [
        child for child in root if child.tag.rsplit("}", 1)[-1] == "testsuite"
    ]
else:
    unavailable("unsupported")

try:
    counts = {
        key: sum(int(suite.attrib.get(key, 0) or 0) for suite in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }
except (TypeError, ValueError, OverflowError):
    unavailable("invalid_counts")

if (
    counts["tests"] <= 0
    or any(value < 0 for value in counts.values())
    or any(value > 9223372036854775807 for value in counts.values())
    or counts["failures"] + counts["errors"] + counts["skipped"] > counts["tests"]
):
    unavailable("invalid_counts")

emit({"ok": True, **counts})
"""

_PYTEST_JUNIT_SKIP_REASONS_SCRIPT = """\
import json
import sys
import xml.etree.ElementTree as ET

try:
    root = ET.parse(sys.argv[1]).getroot()
except Exception:
    print("[]")
    raise SystemExit(0)

reasons = []
for element in root.iter():
    name = element.tag.rsplit("}", 1)[-1]
    if name != "skipped":
        continue
    message = " ".join((element.attrib.get("message") or element.text or "").split())
    if message and message not in reasons:
        reasons.append(message)
    if len(reasons) >= 3:
        break
print(json.dumps(reasons, separators=(",", ":")))
"""

_PYTEST_ATTEMPT_TAG_SCRIPT = """\
import os
import sys
import xml.etree.ElementTree as ET

path = sys.argv[1]
attempt_id = int(sys.argv[2])
if attempt_id < 1:
    raise ValueError("attempt_id must be positive")

tree = ET.parse(path)
root = tree.getroot()

def local_name(element):
    return element.tag.rsplit("}", 1)[-1]

def child_tag(parent, name):
    if parent.tag.startswith("{"):
        namespace = parent.tag.split("}", 1)[0] + "}"
        return namespace + name
    return name

suite = root if local_name(root) == "testsuite" else next(
    element for element in root.iter() if local_name(element) == "testsuite"
)
properties = next(
    (child for child in suite if local_name(child) == "properties"),
    None,
)
if properties is None:
    properties = ET.Element(child_tag(suite, "properties"))
    suite.insert(0, properties)
property_element = next(
    (
        child
        for child in properties
        if local_name(child) == "property"
        and child.attrib.get("name") == "sag.attempt_id"
    ),
    None,
)
if property_element is None:
    property_element = ET.SubElement(properties, child_tag(properties, "property"))
property_element.set("name", "sag.attempt_id")
property_element.set("value", str(attempt_id))

temporary = path + ".attempt.tmp"
tree.write(temporary, encoding="utf-8", xml_declaration=True)
os.replace(temporary, path)
print("SAG_ATTEMPT_TAGGED")
"""

_NATIVE_PROJECT_READY_SCRIPT = """\
import importlib
import importlib.metadata as metadata
import json
import os
import re
import sys
from urllib.parse import unquote, urlsplit

def normalized(value):
    return re.sub(r"[-_.]+", "-", value or "").lower()

distribution_name, install_root, survey_root, package_json, artifact_json, workspace_root = (
    sys.argv[1:7]
)
install_root = os.path.realpath(install_root)
survey_root = os.path.realpath(survey_root)
workspace_root = os.path.realpath(workspace_root)
try:
    if (
        workspace_root == os.path.sep
        or os.path.commonpath((workspace_root, survey_root)) != workspace_root
        or survey_root == workspace_root
    ):
        raise RuntimeError("surveyed checkout escapes workspace")
    if os.path.commonpath((survey_root, install_root)) != survey_root:
        raise RuntimeError("python install root escapes surveyed checkout")
    distribution = metadata.distribution(distribution_name)
    if normalized(distribution.metadata.get("Name", "")) != normalized(distribution_name):
        raise RuntimeError("distribution name mismatch")
    direct_url = json.loads(distribution.read_text("direct_url.json") or "{}")
    parsed = urlsplit(str(direct_url.get("url") or ""))
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
        raise RuntimeError("distribution is not project-owned")
    if os.path.realpath(unquote(parsed.path)) != install_root:
        raise RuntimeError("distribution belongs to another checkout")
    packages = json.loads(package_json)
    if not packages:
        raise RuntimeError("no surveyed import package")
    for package in packages:
        importlib.import_module(package)
    artifact_found = False
    for relative in json.loads(artifact_json):
        candidate = os.path.realpath(os.path.join(survey_root, relative))
        if os.path.commonpath((survey_root, candidate)) != survey_root:
            continue
        if not os.path.isdir(candidate):
            continue
        for directory, _, files in os.walk(candidate):
            for name in files:
                if not name.endswith((".so", ".dylib", ".dll", ".pyd")):
                    continue
                artifact = os.path.realpath(os.path.join(directory, name))
                if (
                    os.path.isfile(artifact)
                    and os.path.commonpath((survey_root, artifact)) == survey_root
                    and os.path.commonpath((candidate, artifact)) == candidate
                ):
                    artifact_found = True
                    break
            if artifact_found:
                break
        if artifact_found:
            break
    if not artifact_found:
        raise RuntimeError("no project-owned native artifact")
except Exception:
    raise SystemExit(1)
print("SAG_NATIVE_PROJECT_READY")
"""


def _parse_pytest_junit_summary(
    output: str,
    discovered: Optional[int],
) -> tuple[Optional[TestStats], Dict[str, int], Optional[str]]:
    encoded = (output or "").strip().encode("utf-8", errors="replace")
    if not encoded or len(encoded) > _PYTEST_JUNIT_SUMMARY_MAX_BYTES:
        return None, {}, "extract_failed"
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, {}, "extract_failed"
    if not isinstance(payload, dict):
        return None, {}, "extract_failed"
    if payload.get("ok") is not True:
        reason = payload.get("error")
        if reason not in _PYTEST_JUNIT_ERROR_REASONS:
            reason = "extract_failed"
        return None, {}, reason

    keys = ("tests", "failures", "errors", "skipped")
    if any(type(payload.get(key)) is not int for key in keys):
        return None, {}, "invalid_counts"
    executed, failures, errors, skipped = (payload[key] for key in keys)
    if (
        executed <= 0
        or min(executed, failures, errors, skipped) < 0
        or max(executed, failures, errors, skipped) > _PYTEST_JUNIT_MAX_COUNT
        or failures + errors + skipped > executed
    ):
        return None, {}, "invalid_counts"

    counts = {
        "tests": executed,
        "failed_tests": failures,
        "error_tests": errors,
        "skipped_tests": skipped,
    }
    return (
        TestStats(
            discovered=discovered,
            executed=executed,
            passed=executed - failures - errors - skipped,
            failed=failures + errors,
            skipped=skipped,
        ),
        counts,
        None,
    )


def _classify_pytest_result(
    exit_code: Optional[int], output: str
) -> Tuple[bool, Optional[str], Optional[str]]:
    """Honest pytest outcome mapping (bug #13 defect 6): usage errors,
    collection errors, a missing pytest and zero collected are NEVER green;
    tests that RAN with failures are an honest green result (stats in the
    output) — a result to report, not an error state."""
    text = output or ""

    def _snippet(pattern: re.Pattern) -> str:
        for line in text.splitlines():
            if pattern.search(line):
                return line.strip()
        return ""

    if "No module named pytest" in text:
        return (
            False,
            "pytest is not importable in the venv (No module named pytest)",
            "PYTEST_MISSING",
        )
    # Pytest's documented exit codes are authoritative when present.
    if exit_code == 4:
        detail = _snippet(_USAGE_ERROR_RE) or f"pytest exited {exit_code}"
        return False, f"pytest usage error — {detail}", "PYTEST_USAGE_ERROR"
    if exit_code == 2:
        detail = (
            _snippet(_COLLECTION_ERROR_RE)
            or _snippet(_CONFTEST_IMPORT_ERROR_RE)
            or f"pytest exited {exit_code}"
        )
        return False, f"pytest collection error — {detail}", "PYTEST_COLLECTION_ERROR"
    if exit_code == 5:
        return (
            False,
            "pytest collected zero tests — nothing was executed",
            "PYTEST_NO_TESTS",
        )
    # An explicit summary stats line at exit 1 WINS over text-signature
    # fallbacks (reviewer-confirmed defect): the suite RAN, some tests failed
    # — an honest result to report, never an error state. Captured argparse/
    # click stderr from the tests under test must not redden it.
    if exit_code == 1 and _FAILED_STATS_RE.search(text):
        return True, None, None
    # Text-only signatures apply ONLY when the exit code is unreliable
    # (a wrapper reporting 0/None) AND pytest printed no summary stats line
    # — the lying-wrapper hole they were built for, nothing wider.
    if exit_code in (0, None) and not _SUMMARY_STATS_RE.search(text):
        if _USAGE_ERROR_RE.search(text):
            return (
                False,
                f"pytest usage error — {_snippet(_USAGE_ERROR_RE)}",
                "PYTEST_USAGE_ERROR",
            )
        if _COLLECTION_ERROR_RE.search(text) or _CONFTEST_IMPORT_ERROR_RE.search(text):
            return (
                False,
                "pytest collection error — "
                f"{_snippet(_COLLECTION_ERROR_RE) or _snippet(_CONFTEST_IMPORT_ERROR_RE)}",
                "PYTEST_COLLECTION_ERROR",
            )
        if _NO_TESTS_RE.search(text):
            return (
                False,
                "pytest collected zero tests — nothing was executed",
                "PYTEST_NO_TESTS",
            )
    if exit_code == 0:
        return True, None, None
    return (
        False,
        f"pytest exited {exit_code} — honest result recorded, no rerun",
        "PYTEST_ERROR",
    )


class PythonTool(BaseTool):
    """Internal python tool; wrapped by the consolidated BuildTool backend."""

    def __init__(self, orchestrator, command_tracker=None):
        super().__init__(
            name="python",
            description=(
                "Python project operations. setup_env installs dependencies into "
                "./.venv via the project's OWN declared tool (poetry/pipenv/pip "
                "ladder from the analyzer manifest); test runs pytest exactly once "
                "with --junitxml after recording the collect-only denominator; "
                "build attempts a wheel (extra evidence, never required for green); "
                "compile byte-compiles the package sources and reports coverage."
            ),
        )
        self.orchestrator = orchestrator
        self.command_tracker = command_tracker
        self._test_attempt_counter = 0
        self._producer_attempt_counter = 0

    def execute(
        self,
        operation: str,
        working_directory: str = "/workspace",
        args: str = None,
        timeout: int = 600,
        native: Optional[Dict[str, Any]] = None,
    ) -> ToolResult:
        op = (operation or "").strip().lower()
        if op not in _OPERATIONS:
            return ToolResult.completed_failure(
                output="",
                error=f"Unknown python operation: {operation!r}",
                error_code="UNKNOWN_PYTHON_OPERATION",
                suggestions=[f"Valid operations: {', '.join(_OPERATIONS)}"],
            )
        contract = current_contract()
        internal_params: Dict[str, Any] = {
            "operation": op,
            "working_directory": working_directory,
            "timeout": timeout,
        }
        if args is not None:
            internal_params["args"] = args
        if native is not None:
            internal_params["native"] = dict(native)
        if contract is None or not python_facade_dispatch_matches(
            contract,
            operation=op,
            working_directory=working_directory,
            internal_params=internal_params,
        ):
            return ToolResult.completed_failure(
                output=(
                    "[contract] Python was not dispatched: the active facade contract "
                    "does not exactly bind this operation and parameter set."
                ),
                error="invocation contract authority missing or mismatched",
                error_code=CONTRACT_AUTHORITY_MISSING,
                metadata={"runner_dispatched": False, "tool": "python", "operation": op},
            )
        manifest_read = read_live_build_requirements(self.orchestrator)
        if (
            not manifest_read.complete
            or manifest_read.conflict is not None
            or manifest_read.payload is None
        ):
            return ToolResult.completed_failure(
                output=(
                    "[evidence] Python was not dispatched: build requirements are not "
                    "a complete current host-published revision."
                ),
                error="live build requirements unavailable",
                error_code=BUILD_REQUIREMENTS_UNAVAILABLE,
                facts={
                    "operation": op,
                    "working_directory": working_directory,
                    "build_requirements_status": manifest_read.conflict or "absent",
                },
                metadata={
                    "runner_dispatched": False,
                    "tool": "python",
                    "operation": op,
                    "blocker_owner": "harness",
                },
            )
        requirements = dict(manifest_read.payload)
        venv = requirements.get("python_venv") or f"{working_directory.rstrip('/')}/.venv"
        if op == "native":
            return self._native(working_directory, timeout, requirements, venv, native)
        handler = {
            "setup_env": self._setup_env,
            "test": self._run_tests,
            "build": self._build_wheel,
            "compile": self._compileall,
        }[op]
        if op in {"setup_env", "build", "compile"}:
            return self._execute_producer(
                op,
                handler,
                working_directory,
                args,
                timeout,
                requirements,
                venv,
            )
        return handler(working_directory, args, timeout, requirements, venv)

    def _execute_producer(
        self,
        operation: str,
        handler: Callable[..., ToolResult],
        working_directory: str,
        args: Optional[str],
        timeout: int,
        requirements: Dict[str, Any],
        venv: str,
    ) -> ToolResult:
        """Run one contract-owned Python action and finalize one receipt.

        The step list is deliberately named ``recorded_*``: `_run` owns these
        project/mechanical commands, while PythonPreflight and the venv repair
        ladder still own their own probes.  A partial trace is never described
        as every physical command the action executed.
        """

        self._producer_attempt_counter += 1
        attempt = self._producer_attempt_counter
        trace = _ProducerTrace(operation=operation)
        token = _ACTIVE_PRODUCER_TRACE.set(trace)
        handoff: Optional[_ProducerDetached] = None
        result: Optional[ToolResult] = None
        try:
            result = handler(working_directory, args, timeout, requirements, venv)
        except _ProducerDetached as detached:
            handoff = detached
        finally:
            _ACTIVE_PRODUCER_TRACE.reset(token)

        if handoff is not None:
            obligation = record_dispatch_obligation_result(
                self.orchestrator.execute_command,
                result=handoff.result,
                tool="python",
                attempt=attempt,
                requested_action=operation,
                effective_action=operation,
                argv=handoff.command,
                working_directory=working_directory,
                before={},
                requirements=requirements,
            )
            handoff.result.update(obligation.metadata())
            pending = detached_handoff_tool_result("python", handoff.command, handoff.result)
            pending.metadata.update(
                {
                    "operation": operation,
                    "producer_observations_status": "pending",
                }
            )
            return pending

        assert result is not None
        if not trace.steps:
            result.metadata.setdefault("runner_dispatched", False)
            return result

        observations = self._producer_observations(
            trace,
            result=result,
            requirements=requirements,
            working_directory=working_directory,
            venv=venv,
        )
        argv = " && ".join(step.argv for step in trace.steps)
        receipt_metadata = record_invocation(
            self.orchestrator.execute_command,
            tool="python",
            attempt=attempt,
            requested_action=operation,
            effective_action=operation,
            argv=argv,
            working_directory=working_directory,
            exit_code=self._producer_exit_code(trace, result),
            before={},
            after={},
            lifecycle_state=(result.metadata or {}).get("lifecycle_state"),
            termination_reason=(result.metadata or {}).get("termination_reason"),
            output=result.raw_output if result.raw_output is not None else result.output,
            requirements=requirements,
            producer_observations=observations,
            **contract_receipt_fields(argv),
        )
        result.metadata.update(receipt_metadata)
        result.metadata["runner_dispatched"] = True
        return result

    @staticmethod
    def _producer_exit_code(trace: _ProducerTrace, result: ToolResult) -> int:
        metadata_exit = (result.metadata or {}).get("exit_code")
        if type(metadata_exit) is int:
            return metadata_exit
        failed = [
            step.result.get("exit_code")
            for step in trace.steps
            if not step.result.get("success") and type(step.result.get("exit_code")) is int
        ]
        if failed:
            return failed[-1]
        terminal = trace.steps[-1].result.get("exit_code")
        if type(terminal) is int:
            return terminal if result.succeeded else (terminal or 1)
        return 0 if result.succeeded else 1

    @staticmethod
    def _producer_step_observation(step: _ProducerStep) -> Dict[str, Any]:
        output = str(step.result.get("full_output", step.result.get("output")) or "")
        observation: Dict[str, Any] = {
            "ordinal": step.ordinal,
            "role": step.role,
            "argv_sha256": hashlib.sha256(step.argv.encode("utf-8", "replace")).hexdigest(),
            "outcome": "completed" if step.result.get("success") else "failed",
            "output_sha256": hashlib.sha256(output.encode("utf-8", "replace")).hexdigest(),
        }
        exit_code = step.result.get("exit_code")
        if type(exit_code) is int:
            observation["exit_code"] = exit_code
        for key in ("lifecycle_state", "termination_reason"):
            value = str(step.result.get(key) or "").strip()
            if value:
                observation[key] = value
        semantic_failure = str(step.result.get("semantic_failure") or "").strip()
        if semantic_failure:
            observation["semantic_failure"] = semantic_failure
        return observation

    @staticmethod
    def _mark_latest_install_signature_failure() -> None:
        """Record a lying-zero installer failure without forging its exit.

        Some wrappers return success/0 while their output contains an
        authoritative installation-error signature. ``_setup_env`` already
        treats that as a semantic failure. Bind the same fact into the latest
        dependency step while preserving its real exit code and output bytes.
        """

        trace = _ACTIVE_PRODUCER_TRACE.get()
        if trace is None:
            return
        for step in reversed(trace.steps):
            if step.role != "dependency_install":
                continue
            if step.result.get("success") is True and step.result.get("exit_code") == 0:
                step.result["success"] = False
                step.result["semantic_failure"] = "install_error_signature"
            return

    def _producer_observations(
        self,
        trace: _ProducerTrace,
        *,
        result: ToolResult,
        requirements: Dict[str, Any],
        working_directory: str,
        venv: str,
    ) -> Dict[str, Any]:
        project_steps = [
            self._producer_step_observation(step) for step in trace.steps if not step.mechanical
        ]
        mechanical_steps = [
            self._producer_step_observation(step) for step in trace.steps if step.mechanical
        ]
        observations: Dict[str, Any] = {
            "schema_version": 1,
            "ecosystem": "python",
            "operation": trace.operation,
            "recorded_project_steps": project_steps,
        }
        if mechanical_steps:
            observations["recorded_mechanical_steps"] = mechanical_steps

        if trace.operation == "setup_env":
            setup = dict(trace.details.get("setup") or {})
            setup.setdefault("venv", venv)
            setup.setdefault("installer", str(requirements.get("python_installer") or "pip"))
            setup.setdefault("install_outcome", "success" if result.succeeded else "failed")
            observations["setup"] = setup
        elif trace.operation == "build":
            observations["build"] = dict(
                trace.details.get("build") or {"artifact_status": "probe_failed", "artifacts": []}
            )
        else:
            observations["compile"] = dict(trace.details.get("compile") or {})
        return observations

    # ------------------------------------------------------------------
    # native (spec §C8 — the resolved capability, then the same deps ladder)
    # ------------------------------------------------------------------

    def _native(
        self,
        working_directory: str,
        timeout: int,
        requirements: Dict[str, Any],
        venv: str,
        native: Optional[Dict[str, Any]],
    ) -> ToolResult:
        """Resolve the capability physically, then re-run the project's install.

        Three steps, in the order that makes each one meaningful:

        1. install the resolver's own `debian_packages` — the model named a
           FEATURE, the harness owns every token on the command line;
        2. run the feature's probe VERBATIM and keep its first line: a package
           that installed is not a capability, an executable that answers is;
        3. re-run the project's OWN editable install through `_setup_env` under
           a `CMAKE_ARGS` overlay composed from the validated definitions —
           which is why the local-provider and `--no-deps` rungs still apply.

        The receipt records the probe lines as `capability_observations`. The
        bounded-smoke capability receipt is deliberately NOT written here: a
        rebuild is not a test, and only an executed test may unlock collection.
        """
        bundle = dict(native or {})
        features, definitions, refusal = self._validated_native(bundle)
        if refusal is not None:
            return refusal

        preamble: List[str] = []
        commands: List[str] = []
        observations: List[Dict[str, str]] = []
        probes_green = True

        packages: List[str] = []
        for feature in features:
            for package in NATIVE_FEATURE_RESOLVER[feature]["debian_packages"]:
                if package not in packages:
                    packages.append(package)
        install_command = _APT_INSTALL.format(packages=" ".join(packages))
        self._run(_APT_REFRESH, working_directory, timeout)
        installed = self._run(install_command, working_directory, timeout)
        commands.append(install_command)
        preamble.append(
            f"[native] resolved {', '.join(features)} to {', '.join(packages)}"
            + ("" if installed.get("success") else " — the package install reported a failure")
        )

        for feature in features:
            probe = NATIVE_FEATURE_RESOLVER[feature]["probe"]
            probed = self._run(probe, working_directory, timeout)
            commands.append(probe)
            line = next(
                (
                    row.strip()
                    for row in str(probed.get("output") or "").splitlines()
                    if row.strip()
                ),
                "",
            )
            observation: Dict[str, str] = {"feature": feature, "probe": probe}
            probe_exit = probed.get("exit_code")
            if type(probe_exit) is int:
                observation["probe_exit_code"] = str(probe_exit)
            if line:
                # Absent facts stay absent: a probe that answered nothing states
                # no version, and inventing "unknown" would be a fact nobody saw.
                observation["observation"] = line
            if not probed.get("success") or probe_exit != 0 or not line:
                probes_green = False
            observations.append(observation)
            preamble.append(f"[native] {probe} -> {line or 'no output'}")

        overlay = {NATIVE_DEFINITION_ENV: native_cmake_args(definitions)}
        preamble.append(
            f"[native] re-running the project's own install with "
            f"{NATIVE_DEFINITION_ENV}={overlay[NATIVE_DEFINITION_ENV]!r}"
        )
        rebuilt = self._setup_env(
            working_directory, None, timeout, requirements, venv, env_overlay=overlay
        )
        commands.extend(
            self._with_env(command, overlay)
            for command in (rebuilt.metadata or {}).get("install_commands") or ()
        )

        succeeded = bool(installed.get("success")) and probes_green and rebuilt.succeeded
        receipt_metadata = record_invocation(
            self.orchestrator.execute_command,
            tool="python",
            attempt="native",
            requested_action="native",
            effective_action="native",
            # The native materialization is a fixed sequence, so the receipt
            # records the sequence it ran rather than one line of it.
            argv=" && ".join(commands),
            working_directory=working_directory,
            exit_code=0 if succeeded else 1,
            # A rebuild writes no test reports; an empty delta on both sides is
            # the honest statement that none were expected.
            before={},
            after={},
            output=rebuilt.raw_output if rebuilt.raw_output is not None else rebuilt.output,
            requirements=requirements,
            capability_observations=observations,
            **contract_receipt_fields(" && ".join(commands)),
        )
        metadata: Dict[str, Any] = dict(rebuilt.metadata or {})
        metadata.update(
            {
                "operation": "native",
                "native_features": features,
                "native_definitions": definitions,
                "native_packages": packages,
                "capability_observations": observations,
                NATIVE_DEFINITION_ENV.lower(): overlay[NATIVE_DEFINITION_ENV],
            }
        )
        metadata.update(receipt_metadata)
        return self._finish(
            ToolResult.completed(
                operation_outcome="success" if succeeded else "failed",
                output=rebuilt.output or "",
                raw_output=rebuilt.raw_output,
                error=(
                    None
                    if succeeded
                    else (
                        rebuilt.error
                        or (
                            "a native capability probe failed or returned no observation"
                            if not probes_green
                            else f"the {', '.join(packages)} install reported a failure"
                        )
                    )
                ),
                error_code=None if succeeded else (rebuilt.error_code or "NATIVE_BUILD_FAILED"),
                metadata=metadata,
            ),
            preamble,
        )

    @staticmethod
    def _validated_native(
        bundle: Dict[str, Any],
    ) -> Tuple[List[str], Dict[str, str], Optional[ToolResult]]:
        """The bundle re-checked against the allowlists, or a refusal.

        The facade already validated this; re-checking costs a dict lookup and
        means no caller — including a future one — can reach a physical apt
        install or an environment overlay through an unvalidated bundle.
        """
        features = [
            str(feature).strip()
            for feature in bundle.get("features") or ()
            if str(feature).strip() in NATIVE_FEATURE_RESOLVER
        ]
        definitions = {
            str(key): str(value)
            for key, value in dict(bundle.get("definitions") or {}).items()
            if NATIVE_DEFINITION_KEY.match(str(key)) and str(value) in NATIVE_DEFINITION_VALUES
        }
        if (
            features
            and definitions
            and len(features) == len(bundle.get("features") or ())
            and len(definitions) == len(bundle.get("definitions") or {})
        ):
            return features, definitions, None
        return (
            [],
            {},
            ToolResult.completed_failure(
                output="",
                error="the native bundle is not an allowlisted feature/definition set",
                error_code="NATIVE_BUNDLE_REJECTED",
                suggestions=[
                    "Constraint: native features and definitions must match the public allowlist"
                ],
                metadata={"operation": "native"},
            ),
        )

    # ------------------------------------------------------------------
    # setup_env
    # ------------------------------------------------------------------

    def _setup_env(
        self,
        working_directory: str,
        args: Optional[str],
        timeout: int,
        requirements: Dict[str, Any],
        venv: str,
        env_overlay: Optional[Dict[str, str]] = None,
    ) -> ToolResult:
        """Install the project's dependencies through its own declared ladder.

        `args`, when present, is an EXACT pin the build facade already matched
        against a stored dependency claim (plan §Stage E item 3); it replaces
        the ladder for that one install rather than being appended to it, and
        it is the only shape this parameter accepts. `env_overlay` is the
        native affordance's validated `CMAKE_ARGS` (spec §C8): it rides every
        rung, so the local-provider and `--no-deps` recoveries rebuild under
        exactly the definitions the contract froze.
        """
        # Pre-flight FIRST (narration prepended, same pattern as the ported
        # maven/gradle tools): check-and-fix, never a hard block.
        preamble: List[str] = []
        outcome = PythonPreflight(self.orchestrator).run(
            requirements.get("python_version"),
            constraint=requirements.get("python_constraint"),
            source=requirements.get("python_version_source") or "requires-python",
            venv_path=venv,
        )
        if outcome.narration:
            preamble.append(outcome.narration)

        # Venv on the pre-flight's interpreter. A provisioning pre-flight has
        # already created the venv (uv venv / pythonX.Y -m venv).
        if not outcome.provisioned and not self._venv_exists(venv):
            made = self._run(
                f"python3 -m venv {venv}",
                working_directory,
                timeout,
                producer_role="venv_create",
                mechanical=True,
            )
            if not made.get("success"):
                return self._finish(
                    ToolResult.completed_failure(
                        output=self._tail(made.get("output") or ""),
                        error=f"could not create venv at {venv}",
                        error_code="VENV_CREATE_FAILED",
                        suggestions=["Check that python3 and the venv module are available"],
                        metadata={"operation": "setup_env", "venv": venv},
                    ),
                    preamble,
                )

        # Bug #13 defect 1: an earlier phase (clone auto-install) can leave a
        # pip-less/broken venv the pre-flight never repairs because the venv
        # already exists. Probe/repair/recreate BEFORE anything installs.
        repair = ensure_venv_pip(
            self.orchestrator, venv, python_version=requirements.get("python_version")
        )
        repair_note = venv_repair_note(repair, venv)
        if repair_note:
            preamble.append(repair_note)

        installer = requirements.get("python_installer") or "pip"
        note = requirements.get("python_install_note")
        # The facade contract forbids every direct deps target. This operation
        # consumes only surveyed project-owned install commands; retaining a
        # hidden pin rung here would create a second authority path.
        commands = [
            c.replace("{venv}", venv).replace("{dir}", working_directory)
            for c in (requirements.get("python_install_commands") or [])
        ]
        if not commands:
            # Bug #13 defect 4: self-healing deps — an empty manifest (the
            # agent skipped project analyze) must not no-op green; the marker
            # files are right there, so detect the ladder inline.
            ladder = self._detect_ladder_inline(working_directory)
            commands = [
                c.replace("{venv}", venv).replace("{dir}", working_directory)
                for c in ladder["commands"]
            ]
            if commands:
                installer = ladder["installer"] or installer
                note = ladder.get("note")
                preamble.append("[setup] manifest empty — detected installer ladder inline")
            else:
                return self._finish(
                    ToolResult.completed_failure(
                        output="",
                        error=(
                            "no python install commands: the manifest is empty and no "
                            "installer markers (poetry.lock/Pipfile.lock/pyproject.toml/"
                            "requirements*.txt/setup.py) were found in "
                            f"{working_directory}"
                        ),
                        error_code="PYTHON_NO_INSTALLER_DETECTED",
                        suggestions=[
                            "Observed fact: no installer declaration was found at the submitted root",
                            "Relevant constraints: current survey epoch and submitted working directory",
                        ],
                        metadata={"operation": "setup_env", "venv": venv},
                    ),
                    preamble,
                )
        if note:
            # Bug #13 defect 3: the missing-test-extras hole is narrated, never silent.
            preamble.append(f"[setup] {note}")

        transcript: List[str] = []
        deviation: Optional[str] = None
        retry_meta: Optional[Dict[str, str]] = None
        provider_recovery_meta: Optional[Dict[str, Any]] = None
        provider_recovery_attempted = False
        provider_no_deps_rung: Optional[bool] = None
        retried = False
        overall_ok = True
        failure_detail: Optional[str] = None
        install_timeout = (
            max(timeout, 2400)
            if requirements.get("native_build_mode") == "pep517-integrated"
            else timeout
        )
        for cmd in commands:
            result_already_recorded = False
            result = self._run(
                cmd,
                working_directory,
                install_timeout,
                env_overlay,
                producer_role="dependency_install",
            )
            install_failed = self._effective_install_failure(result)

            # Bounded retry (spec: exactly once): pip's Requires-Python
            # rejection is authoritative; re-provision from it and rerun ONCE.
            if install_failed and not retried:
                needed = classify_python_version_error(result.get("output") or "")
                active = outcome.active_version or active_python_version(self.orchestrator)
                if needed and needed != active:
                    retried = True
                    retry_outcome = PythonPreflight(self.orchestrator).run(
                        needed,
                        source="install-error",
                        venv_path=venv,
                    )
                    if retry_outcome.provisioned:
                        preamble.append(
                            f"[pre-flight] install error requires Python {needed}, "
                            f"re-provisioned, retry 1/1"
                        )
                        retry_meta = {"from": active or "unknown", "to": needed}
                        result = self._run(
                            cmd,
                            working_directory,
                            install_timeout,
                            env_overlay,
                            producer_role="dependency_install",
                        )
                        install_failed = self._effective_install_failure(result)

            if install_failed and not provider_recovery_attempted:
                recovery = self._recover_local_provider(
                    result.get("output") or "",
                    command=cmd,
                    working_directory=working_directory,
                    timeout=install_timeout,
                    requirements=requirements,
                    venv=venv,
                    env_overlay=env_overlay,
                )
                if recovery is not None:
                    provider_recovery_attempted = True
                    provider_recovery_meta = recovery["metadata"]
                    transcript.append(
                        f"$ {self._with_env(cmd, env_overlay)}\n"
                        f"{self._tail(result.get('output') or '')}"
                    )
                    transcript.append(
                        f"$ {self._with_env(recovery['provider_command'], env_overlay)}\n"
                        f"{self._tail(recovery['provider_result'].get('output') or '')}"
                    )
                    preamble.append(recovery["narration"])
                    provider_failed = self._effective_install_failure(recovery["provider_result"])
                    if not provider_failed:
                        result = self._run(
                            cmd,
                            working_directory,
                            install_timeout,
                            env_overlay,
                            producer_role="dependency_install",
                        )
                        provider_recovery_meta["root_retry"] = True
                    else:
                        result = recovery["provider_result"]
                        result_already_recorded = True
                    install_failed = self._effective_install_failure(result)

                    # The provider installed, yet the retried root install
                    # re-fails naming the provider's OWN distribution: the
                    # in-repo build sits below the declared floor, so no
                    # amount of retrying resolves it. One more narrated rung.
                    # One-shot: guarded by provider_recovery_attempted above.
                    if (
                        install_failed
                        and not provider_failed
                        and self._missing_distribution_name(result.get("output") or "")
                        == provider_recovery_meta["distribution_name"]
                    ):
                        transcript.append(
                            f"$ {self._with_env(cmd, env_overlay)}\n"
                            f"{self._tail(result.get('output') or '')}"
                        )
                        preamble.append(
                            f"[recovery] the local {provider_recovery_meta['distribution_name']} "
                            "build resolves below the declared floor; installed the root "
                            "project with --no-deps, then its remaining declared dependencies"
                        )
                        rung = self._run_no_deps_rung(
                            distribution_name=provider_recovery_meta["distribution_name"],
                            working_directory=working_directory,
                            timeout=install_timeout,
                            requirements=requirements,
                            venv=venv,
                            env_overlay=env_overlay,
                        )
                        transcript.extend(rung["transcript"])
                        result = rung["result"]
                        result_already_recorded = True
                        install_failed = self._effective_install_failure(result)
                        provider_no_deps_rung = not install_failed

            # Faithfulness deviation (spec Component 3): the project's own
            # tool failed; the pip rung keeps setup moving, NARRATED so the
            # generated setup docs reflect what actually ran.
            if install_failed and installer in ("poetry", "pipenv"):
                deviation = (
                    f"[deviation] {installer} install failed; fell back to "
                    f"pip install -e . — setup docs must list the fallback"
                )
                preamble.append(deviation)
                transcript.append(
                    f"$ {self._with_env(cmd, env_overlay)}\n"
                    f"{self._tail(result.get('output') or '')}"
                )
                cmd = _PIP_FALLBACK.replace("{venv}", venv)
                result = self._run(
                    cmd,
                    working_directory,
                    timeout,
                    env_overlay,
                    producer_role="dependency_install",
                )
                result_already_recorded = False
                install_failed = self._effective_install_failure(result)

            if not result_already_recorded:
                transcript.append(
                    f"$ {self._with_env(cmd, env_overlay)}\n"
                    f"{self._tail(result.get('output') or '')}"
                )
            # Bug #13 defect 2: honest failure — a non-zero exit OR an
            # install-error signature in the output (a wrapper reporting
            # exit 0 while stderr said "No module named pip") is a FAILURE,
            # and the observation leads with it instead of burying it.
            masked = self._install_error_line(result.get("output") or "")
            if install_failed:
                if masked:
                    self._mark_latest_install_signature_failure()
                overall_ok = False
                failure_detail = masked or self._failure_tail_line(result)
                preamble.insert(0, f"[setup] dependency install FAILED — {failure_detail}")
                break

        trace = _ACTIVE_PRODUCER_TRACE.get()
        setup_observation: Dict[str, Any] = {
            "venv": venv,
            "installer": installer,
            "install_outcome": "success" if overall_ok else "failed",
        }
        # Only the facade's setup_env action owns these postconditions.  A
        # native action calls this dependency ladder internally and finalizes
        # its own aggregate native receipt; running unreceipted pip/import
        # probes inside that nested call would create a second evidence lane.
        if overall_ok and trace is not None:
            postcondition_timeout = min(max(int(timeout), 1), 120)
            pip_check = self._run(
                f"{venv}/bin/python -m pip check",
                working_directory,
                postcondition_timeout,
                env_overlay,
                producer_role="pip_check",
                monitor_floor=1,
            )
            pip_output = str(pip_check.get("full_output") or pip_check.get("output") or "")
            pip_exit = pip_check.get("exit_code")
            if type(pip_exit) is not int:
                pip_exit = 0 if pip_check.get("success") else 1
            setup_observation["pip_check"] = {
                "status": "clean" if pip_check.get("success") and pip_exit == 0 else "broken",
                "exit_code": pip_exit,
                "output_sha256": hashlib.sha256(pip_output.encode("utf-8", "replace")).hexdigest(),
            }
            setup_observation["imports"] = self._python_import_observation(
                working_directory=working_directory,
                timeout=postcondition_timeout,
                requirements=requirements,
                venv=venv,
                env_overlay=env_overlay,
            )
        if trace is not None:
            trace.details["setup"] = setup_observation

        return self._finish(
            ToolResult.completed(
                operation_outcome="success" if overall_ok else "failed",
                output="\n".join(transcript),
                error=(
                    None if overall_ok else f"dependency installation failed — {failure_detail}"
                ),
                error_code=None if overall_ok else "PYTHON_SETUP_FAILED",
                metadata={
                    "operation": "setup_env",
                    "venv": venv,
                    "installer": installer,
                    "install_commands": commands,
                    **({"deviation": deviation} if deviation else {}),
                    **({"python_retry": retry_meta} if retry_meta else {}),
                    **(
                        {"local_provider_recovery": provider_recovery_meta}
                        if provider_recovery_meta
                        else {}
                    ),
                    **(
                        {"provider_no_deps_rung": provider_no_deps_rung}
                        if provider_no_deps_rung is not None
                        else {}
                    ),
                },
            ),
            preamble,
        )

    @staticmethod
    def _survey_import_targets(requirements: Dict[str, Any]) -> Optional[List[str]]:
        """Exact survey-derived import targets; None means the survey was unsafe.

        No package is discovered or guessed here.  Facts-v8 package mappings
        own the primary list; the older surveyed package list is the explicit
        compatibility fallback.
        """

        return python_import_targets(requirements)

    def _python_import_observation(
        self,
        *,
        working_directory: str,
        timeout: int,
        requirements: Dict[str, Any],
        venv: str,
        env_overlay: Optional[Dict[str, str]],
    ) -> Dict[str, Any]:
        targets = self._survey_import_targets(requirements)
        if targets is None:
            return {"status": "unavailable", "reason_code": "invalid_survey_targets"}
        if not targets:
            return {"status": "unavailable", "reason_code": "no_survey_targets"}
        target_json = json.dumps(targets, separators=(",", ":"), ensure_ascii=False)
        command = (
            f"{venv}/bin/python -c {shlex.quote(_PYTHON_IMPORT_PROBE_SCRIPT)} "
            f"{shlex.quote(target_json)}"
        )
        result = self._run(
            command,
            working_directory,
            timeout,
            env_overlay,
            producer_role="import_probe",
            monitor_floor=1,
        )
        if not result.get("success"):
            return {"status": "unavailable", "reason_code": "probe_failed"}
        output = str(result.get("full_output") or result.get("output") or "")
        try:
            payload = json.loads(output.strip())
            if not isinstance(payload, dict) or set(payload) != {
                "targets",
                "importable",
                "failures",
            }:
                raise ValueError("invalid import probe shape")
            if payload["targets"] != targets:
                raise ValueError("import probe target drift")
            importable = payload["importable"]
            failures = payload["failures"]
            if not isinstance(importable, list) or not isinstance(failures, list):
                raise ValueError("invalid import probe collections")
            if any(target not in targets for target in importable) or len(set(importable)) != len(
                importable
            ):
                raise ValueError("invalid imported targets")
            normalized_failures: List[Dict[str, str]] = []
            failed_targets: List[str] = []
            for raw_failure in failures:
                if not isinstance(raw_failure, dict) or set(raw_failure) != {
                    "target",
                    "error_type",
                }:
                    raise ValueError("invalid import failure")
                target = str(raw_failure.get("target") or "").strip()
                error_type = str(raw_failure.get("error_type") or "").strip()
                if target not in targets or target in failed_targets or not error_type:
                    raise ValueError("invalid import failure identity")
                failed_targets.append(target)
                if len(normalized_failures) < _PYTHON_IMPORT_FAILURE_SAMPLE_CAP:
                    normalized_failures.append({"target": target, "error_type": error_type[:128]})
            if set(importable) | set(failed_targets) != set(targets) or set(importable) & set(
                failed_targets
            ):
                raise ValueError("import probe did not account for every target")
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"status": "unavailable", "reason_code": "invalid_probe_output"}
        observation: Dict[str, Any] = {
            "status": "complete",
            "targets": targets,
            "targets_sha256": producer_observations_sha256(targets),
            "target_count": len(targets),
            "importable_count": len(importable),
            "failed_count": len(failed_targets),
            "failures": normalized_failures,
            "output_sha256": hashlib.sha256(output.encode("utf-8", "replace")).hexdigest(),
        }
        if len(normalized_failures) < len(failed_targets):
            observation["truncated"] = True
        return observation

    @staticmethod
    def _normalized_distribution_name(value: str) -> str:
        return re.sub(r"[-_.]+", "-", str(value or "").strip()).lower()

    @classmethod
    def _requirement_distribution_name(cls, requirement: Any) -> Optional[str]:
        """The normalized distribution name a requirement string names, before
        any version specifier (`apache-tvm-ffi>=0.1.13` -> `apache-tvm-ffi`)."""
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", str(requirement or "").strip())
        return cls._normalized_distribution_name(match.group(0)) if match else None

    @classmethod
    def _missing_distribution_name(cls, output: str) -> Optional[str]:
        match = _MISSING_DISTRIBUTION_RE.search(output or "")
        if not match:
            return None
        return cls._requirement_distribution_name(match.group("requirement"))

    @classmethod
    def _remaining_declared_dependencies(
        cls, requirements: Dict[str, Any], installed: set[str]
    ) -> List[str]:
        """The manifest's declared dependencies minus the ones already supplied
        by an installed local provider (installing those from the index is the
        very resolution that cannot succeed)."""
        remaining: List[str] = []
        for requirement in requirements.get("python_declared_dependencies") or ():
            text = str(requirement or "").strip()
            if not text or cls._requirement_distribution_name(text) in installed:
                continue
            remaining.append(text)
        return remaining

    def _run_no_deps_rung(
        self,
        *,
        distribution_name: str,
        working_directory: str,
        timeout: int,
        requirements: Dict[str, Any],
        venv: str,
        env_overlay: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Install the root project without resolving, then the declared
        dependencies the local provider does not supply. Stops at the first
        failure — the caller renders it as an honest failure, rung narrated."""
        transcript: List[str] = []
        no_deps_command = _PIP_NO_DEPS.replace("{venv}", venv)
        result = self._run(
            no_deps_command,
            working_directory,
            timeout,
            env_overlay,
            producer_role="dependency_install",
        )
        transcript.append(
            f"$ {self._with_env(no_deps_command, env_overlay)}\n"
            f"{self._tail(result.get('output') or '')}"
        )
        if not self._effective_install_failure(result):
            remaining = self._remaining_declared_dependencies(
                requirements, {self._normalized_distribution_name(distribution_name)}
            )
            if remaining:
                deps_command = f"{venv}/bin/python -m pip install " + " ".join(
                    shlex.quote(item) for item in remaining
                )
                result = self._run(
                    deps_command,
                    working_directory,
                    timeout,
                    env_overlay,
                    producer_role="dependency_install",
                )
                transcript.append(
                    f"$ {self._with_env(deps_command, env_overlay)}\n"
                    f"{self._tail(result.get('output') or '')}"
                )
        return {"result": result, "transcript": transcript}

    def _recover_local_provider(
        self,
        output: str,
        *,
        command: str,
        working_directory: str,
        timeout: int,
        requirements: Dict[str, Any],
        venv: str,
        env_overlay: Optional[Dict[str, str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Install one exact in-repo provider, then let the caller retry once."""
        missing = self._missing_distribution_name(output)
        if missing is None or " install " not in f" {command} ":
            return None
        declared = {
            name
            for requirement in requirements.get("python_declared_dependencies") or ()
            if (name := self._requirement_distribution_name(requirement))
        }
        if missing not in declared:
            return None
        providers = [
            provider
            for provider in requirements.get("python_local_providers") or ()
            if isinstance(provider, dict)
            and self._normalized_distribution_name(provider.get("distribution_name")) == missing
        ]
        if len(providers) != 1:
            return None
        provider = providers[0]
        project_root = str(
            (requirements.get("survey") or {}).get("project_path")
            or requirements.get("python_root")
            or working_directory
        ).rstrip("/")
        relative_root = posixpath.normpath(str(provider.get("root") or "").strip())
        if (
            not project_root.startswith("/workspace/")
            or not relative_root
            or relative_root.startswith("../")
            or relative_root.startswith("/")
        ):
            return None
        provider_root = posixpath.normpath(f"{project_root}/{relative_root}")
        if not provider_root.startswith(project_root + "/"):
            return None

        provider_owned, _ = verify_project_owned_path(
            self.orchestrator.execute_command,
            project_root,
            provider_root,
        )
        if not provider_owned:
            return None

        metadata_path = f"{provider_root}/pyproject.toml"
        exists = self.orchestrator.execute_command(
            f"test -f {shlex.quote(metadata_path)} && echo EXISTS || echo MISSING"
        )
        if "EXISTS" not in (exists.get("output") or ""):
            return None
        read = self.orchestrator.execute_command(
            f"cat {shlex.quote(metadata_path)}",
            truncate_output=False,
        )
        actual_name = project_name_from_pyproject(
            (read.get("output") or "") if read.get("success") else ""
        )
        if self._normalized_distribution_name(actual_name or "") != missing:
            return None

        provider_command = f"{venv}/bin/python -m pip install -e {shlex.quote(provider_root)}"
        provider_result = self._run(
            provider_command,
            working_directory,
            timeout,
            env_overlay,
            producer_role="dependency_install",
        )
        return {
            "provider_command": provider_command,
            "provider_result": provider_result,
            "narration": (
                f"[recovery] index lacked {missing}; installed the exact surveyed "
                f"local provider at {relative_root}, then retried the original "
                "root install at most once"
            ),
            "metadata": {
                "distribution_name": missing,
                "provider_root": relative_root,
                "provider_command": provider_command,
                "provider_succeeded": not self._effective_install_failure(provider_result),
                "root_retry": False,
            },
        }

    def _detect_ladder_inline(self, working_directory: str) -> Dict[str, Any]:
        """Bug #13 defect 4: run the shared installer detection against the
        working directory when the manifest declares nothing — same ladder,
        same extras rules (the strings live ONLY in python_env)."""
        listing = self.orchestrator.execute_command(f"ls -A1 {working_directory}")
        files_present = {
            line.strip() for line in (listing.get("output") or "").splitlines() if line.strip()
        }
        contents: Dict[str, str] = {}
        for name in ("pyproject.toml", "setup.cfg"):
            if name in files_present:
                read = self.orchestrator.execute_command(f"cat {working_directory}/{name}")
                contents[name] = (read.get("output") or "") if read.get("success") else ""
        return detect_installer(files_present, contents)

    @staticmethod
    def _install_error_line(output: str) -> Optional[str]:
        """The line carrying an install-error signature, or None."""
        match = _INSTALL_ERROR_RE.search(output or "")
        if not match:
            return None
        for line in (output or "").splitlines():
            if match.group(0) in line:
                return line.strip()
        return match.group(0)

    @classmethod
    def _effective_install_failure(cls, result: Dict[str, Any]) -> bool:
        """Classify one installer result once for every recovery/final branch.

        Some execution wrappers can report a zero/unknown exit while pip
        printed an explicit terminal error.  Treat that observation
        consistently: it cannot be success while choosing recovery and failure
        only later while rendering the result.
        """
        return not bool(result.get("success")) or bool(
            cls._install_error_line(result.get("output") or "")
        )

    @staticmethod
    def _failure_tail_line(result: Dict[str, Any]) -> str:
        """Surface the stderr: the last non-empty output line, with the exit."""
        output = result.get("output") or ""
        tail = next((l.strip() for l in reversed(output.splitlines()) if l.strip()), "")
        exit_code = result.get("exit_code")
        return f"exit {exit_code}: {tail}" if tail else f"install command exited {exit_code}"

    # ------------------------------------------------------------------
    # test
    # ------------------------------------------------------------------

    def _run_tests(
        self,
        working_directory: str,
        args: Optional[str],
        timeout: int,
        requirements: Dict[str, Any],
        venv: str,
    ) -> ToolResult:
        python = f"{venv}/bin/python"
        preamble: List[str] = []
        has_native_build = bool(requirements.get("has_native_build"))
        native_project_root = (
            str(
                (requirements.get("survey") or {}).get("project_path")
                or requirements.get("python_root")
                or working_directory
            ).rstrip("/")
            if has_native_build
            else None
        )
        # Plan 4 Task 1 (audit 2026-07-26): the bounded smoke is CAPABILITY-
        # gated, never readiness-gated. An LLVM-less TVM build satisfied this
        # probe (surveyed distribution + PEP 610 origin + any native artifact),
        # `native_unready` went False, and the bare test collected the full
        # suite — precisely the failure the guard below was written to prevent.
        # Readiness survives as an informational fact; the ONLY thing that
        # unlocks a full collect is a capability receipt: a bounded smoke that
        # actually executed tests on this project root.
        native_ready_probe = has_native_build and self._native_project_ready(
            python=python,
            working_directory=working_directory,
            requirements=requirements,
        )
        smoke_receipt_present = has_native_build and (
            self._native_smoke_receipt(native_project_root) is not None
        )
        native_bounded = has_native_build and not smoke_receipt_present
        native_smoke = (
            self._verified_native_smoke_candidate(
                working_directory=working_directory,
                requirements=requirements,
            )
            if native_bounded
            else None
        )
        native_selection_mode: Optional[str] = None
        # `smoke_receipt_present` is the fact the gate DECIDED on (a receipt
        # written by this very attempt is reported separately). `native_unready`
        # keeps its literal meaning — the readiness probe failed — and is no
        # longer a gate; no consumer may read it as one.
        gate_metadata: Dict[str, Any] = {
            "native_ready_probe": bool(native_ready_probe),
            "smoke_receipt_present": bool(smoke_receipt_present),
            **({"native_bounded": True} if native_bounded else {}),
            **({"native_unready": True} if has_native_build and not native_ready_probe else {}),
        }

        # Bug #13 defect 7: allowlist-sanitize the args BEFORE anything runs —
        # 'make test' was pasted verbatim into 'pytest make test' in the live run.
        hints = requirements.get("test_hints") or {}
        raw_args = (args or "").strip()
        if raw_args:
            pytest_args, rejection = self._sanitize_pytest_args(
                raw_args,
                working_directory,
                required_project_root=native_project_root if native_bounded else None,
                required_smoke_boundary=(native_smoke["absolute_path"] if native_smoke else None),
            )
            if rejection:
                rejection_lines = [f"[test] rejected args {raw_args!r} — {rejection}"]
                if native_bounded and native_smoke:
                    rejection_lines.append(
                        f"[test] no native smoke receipt for {native_project_root} — "
                        "a bounded, project-owned smoke observation is required before a full sweep"
                    )
                self._record_control_assessment("PYTEST_ARGS_REJECTED", rejection)
                return ToolResult.completed_failure(
                    output="\n".join(rejection_lines),
                    error=rejection,
                    error_code="PYTEST_ARGS_REJECTED",
                    failure_signature="pytest_args_rejected:invalid_selector",
                    suggestions=[
                        _PYTEST_USAGE_HINT,
                        "Constraint: a native-unready project may not expand to a full-suite selector",
                        "Neutral affordances: build test or shell execution",
                    ],
                    metadata={
                        "operation": "test",
                        "rejected_args": raw_args,
                        "surveyed_native_smoke_available": native_smoke is not None,
                        **gate_metadata,
                    },
                )
            if native_bounded:
                pytest_args = self._bounded_native_pytest_args(pytest_args or "")
                native_selection_mode = "explicit"
        else:
            if native_bounded:
                if native_smoke is None:
                    unavailable = (
                        "native smoke unavailable — refusing to guess a path or "
                        "collect the full suite"
                    )
                    self._record_control_assessment("NATIVE_SMOKE_UNAVAILABLE", unavailable)
                    return ToolResult.completed_failure(
                        output=(
                            "[test] this native project has no smoke receipt and the "
                            "survey has no current, project-owned smoke target"
                        ),
                        error=unavailable,
                        error_code="NATIVE_SMOKE_UNAVAILABLE",
                        suggestions=[
                            "Observed fact: no current project-owned native smoke candidate is available",
                            "Constraint: the runner refuses an unbounded native-unready test sweep",
                        ],
                        metadata={
                            "operation": "test",
                            "selection_mode": "none",
                            **gate_metadata,
                        },
                    )
                pytest_args = native_smoke["args"]
                native_selection_mode = "survey_candidate"
                preamble.append(
                    "[test] no native smoke receipt yet — selected the surveyed, "
                    "project-owned bounded smoke target"
                )
            else:
                if smoke_receipt_present:
                    preamble.append(
                        "[test] native smoke receipt present — the bounded smoke "
                        "already executed, so the full suite is unlocked"
                    )
                pytest_args = (hints.get("pytest_args") or "").strip()

        # A failed interpreter or broken pytest plugin is not evidence that
        # pytest is absent. Install only the explicitly missing module, and
        # verify both the installer exit and the resulting import before tests.
        probe_command = f"{python} -m pytest --version"
        probe = self.orchestrator.execute_command(probe_command) or {}
        bootstrap_failure = None
        if not probe.get("success") or probe.get("exit_code") != 0:
            missing_pytest = probe.get("exit_code") == 1 and re.search(
                r"(?:^|:\s*)No module named ['\"]?pytest['\"]?(?:\s|$)",
                probe.get("output") or "",
            )
            if not missing_pytest:
                bootstrap_failure = ("probe", probe_command, probe)
            else:
                install_command = f"{python} -m pip install pytest"
                installed = self._run(install_command, working_directory, timeout)
                if installed.get("dispatch_status") in DETACHED_HANDOFF_STATUSES:
                    pending = detached_handoff_tool_result("python", install_command, installed)
                    pending.metadata.update(operation="test", bootstrap_stage="install")
                    return pending
                if installed.get("exit_code") != 0 or self._effective_install_failure(installed):
                    bootstrap_failure = ("install", install_command, installed)
                else:
                    verified = self.orchestrator.execute_command(probe_command) or {}
                    if not verified.get("success") or verified.get("exit_code") != 0:
                        bootstrap_failure = ("verify", probe_command, verified)
                    else:
                        preamble.append("[test] pytest not in venv — installed for the run")
        if bootstrap_failure is not None:
            stage, command, failed = bootstrap_failure
            detail = self._failure_tail_line(failed)
            self._record_control_assessment("PYTEST_BOOTSTRAP_FAILED", detail)
            return ToolResult.completed_failure(
                output="\n".join(
                    (
                        *preamble,
                        f"[test] pytest bootstrap {stage} failed",
                        failed.get("output") or "",
                    )
                ),
                error=detail,
                error_code="PYTEST_BOOTSTRAP_FAILED",
                metadata={
                    "operation": "test",
                    "runner_dispatched": False,
                    "bootstrap_stage": stage,
                    "command": command,
                    "exit_code": failed.get("exit_code"),
                    **gate_metadata,
                },
            )

        # Panel anchor source (Category-3 spec): the SELECTED count for THIS
        # invocation as a STRUCTURED field — never parsed from the run's
        # summary text downstream. A filtered invocation gets ONLY its scoped
        # collect pass: an initial unfiltered collect can itself trigger the
        # native full-suite failure this guard exists to prevent.
        if pytest_args:
            collect_command = f"{python} -m pytest --collect-only -q {pytest_args}"
            collect = self._run(
                collect_command,
                working_directory,
                timeout,
            )
            collected_after_deselection = self._parse_collected_after_deselection(
                collect.get("output") or ""
            )
            collected = None
            collection_scope = "filtered"
        else:
            collect_command = f"{python} -m pytest --collect-only -q"
            collect = self._run(collect_command, working_directory, timeout)
            collected = self._parse_collected(collect.get("output") or "")
            collected_after_deselection = collected
            collection_scope = "full"
        self._write_collected(
            collected,
            scope=collection_scope,
            selected=collected_after_deselection,
        )

        if native_bounded and (
            collected_after_deselection is None or not 1 <= collected_after_deselection <= 50
        ):
            if collected_after_deselection is None:
                code = "NATIVE_SMOKE_COUNT_UNKNOWN"
                detail = "the scoped collection count could not be parsed"
            elif collected_after_deselection == 0:
                code = "NATIVE_SMOKE_EMPTY"
                detail = "the verified target currently selects zero tests"
            else:
                code = "NATIVE_SMOKE_TOO_BROAD"
                detail = (
                    f"the verified target selects {collected_after_deselection} tests; "
                    "the bounded-smoke safety limit is 50"
                )
            # Nothing ran: executed is 0, and the collection-error count is
            # unknown only when the collect command itself failed.
            collection_errors = 0 if collect.get("success") else None
            preamble.append(
                self._collection_line(
                    scope=collection_scope,
                    collected=collected,
                    selected=collected_after_deselection,
                    executed=0,
                    collection_errors=collection_errors,
                )
            )
            return self._finish(
                ToolResult.completed_failure(
                    output=self._tail(collect.get("output") or ""),
                    raw_output=collect.get("output"),
                    error=f"{detail} — test execution was not started",
                    error_code=code,
                    suggestions=[
                        "Observed fact: collection produced no admissible executable test items",
                        "Relevant constraints: current survey coordinates and native capability evidence",
                    ],
                    metadata={
                        "operation": "test",
                        "selection_mode": native_selection_mode,
                        "collection_scope": collection_scope,
                        "collection_command": collect_command,
                        "collected": collected,
                        "collected_after_deselection": collected_after_deselection,
                        "executed": 0,
                        "collection_errors": collection_errors,
                        **gate_metadata,
                        **(
                            {
                                "smoke_candidate": native_smoke["path"],
                                "smoke_candidate_source": native_smoke["source"],
                            }
                            if native_smoke and native_selection_mode == "survey_candidate"
                            else {}
                        ),
                    },
                ),
                preamble,
            )

        self._test_attempt_counter += 1
        attempt_id = self._test_attempt_counter
        report = f"{PYTEST_REPORT_DIR}/pytest-attempt-{attempt_id:06d}.xml"
        self.orchestrator.execute_command(f"mkdir -p {PYTEST_REPORT_DIR}")
        command = f"{python} -m pytest"
        if pytest_args:
            command += f" {pytest_args}"
        command += f" --junitxml={report}"

        # ONE honest run per suite. pytest exit 1 (failures) is a RESULT to
        # report, never an error to retry — no rerun, ever.
        # P0-A: bracket the run with report-XML content hashes so this
        # attempt's JUnit output is attributable to this attempt alone —
        # a rerun overwriting the same path is a content change, not a
        # second report.
        # Revalidate the facade's semantic contract against the exact test
        # operation immediately before the test runner. Internal Python code
        # never freezes fallback authority of its own.
        pytest_contract, _ = ensure_dispatch_contract(
            self.orchestrator.execute_command,
            tool="python",
            effective_action="test",
            expected_cwd=working_directory,
            expected_argv=command,
            requirements=requirements,
            internal_params={
                "operation": "test",
                "working_directory": working_directory,
                "args": args,
                "timeout": timeout,
            },
        )
        if pytest_contract is None:
            return self._finish(
                ToolResult.completed_failure(
                    output=(
                        "[contract] Python tests were not dispatched: the active "
                        "facade contract no longer matches the test operation."
                    ),
                    error="invocation contract authority missing or mismatched",
                    error_code=CONTRACT_AUTHORITY_MISSING,
                    metadata={
                        "runner_dispatched": False,
                        "tool": "python",
                        "operation": "test",
                    },
                ),
                preamble,
            )
        reports_before = snapshot_reports(
            self.orchestrator.execute_command,
            [working_directory, PYTEST_REPORT_DIR],
        )
        with dispatch_contract(pytest_contract):
            result = self._run(command, working_directory, timeout)
            exit_code = result.get("exit_code")
            output = result.get("output") or ""
            # Bind the same exact runner bytes in the receipt and returned
            # result. Monitor summaries and tail truncation are display only;
            # an observed empty stream is still an available raw observation.
            runner_output = result.get("full_output", output)
            attempt_tag_command = (
                f"{shlex.quote(python)} -c {shlex.quote(_PYTEST_ATTEMPT_TAG_SCRIPT)} "
                f"{shlex.quote(report)} {attempt_id}"
            )
            attempt_tag_result = self.orchestrator.execute_command(attempt_tag_command)
            attempt_tagged = attempt_tag_result.get("success")
            if attempt_tagged is None:
                attempt_tagged = attempt_tag_result.get("exit_code") == 0
            # The after-snapshot follows the attempt tagger on purpose: tagging
            # REWRITES the JUnit XML, so hashing before it would bind the receipt
            # to bytes that no longer exist and every consumer would read the
            # report as stale.
            receipt_metadata = record_invocation(
                self.orchestrator.execute_command,
                tool="python",
                attempt=attempt_id,
                requested_action="test",
                effective_action="test",
                argv=command,
                working_directory=working_directory,
                exit_code=exit_code,
                before=reports_before,
                after=snapshot_reports(
                    self.orchestrator.execute_command,
                    [working_directory, PYTEST_REPORT_DIR],
                ),
                output=runner_output,
                requirements=requirements,
                # Bind this dispatch to the semantic contract the build facade
                # froze. Python semantic receipts intentionally carry no argv
                # compliance claim.
                **contract_receipt_fields(command),
            )
        # Bug #13 defect 6: honest mapping — collection/usage errors and zero
        # collected are never green, even when the wrapper showed exit 0.
        success, error, error_code = _classify_pytest_result(exit_code, output)
        extraction_command = (
            f"{shlex.quote(python)} -c {shlex.quote(_PYTEST_JUNIT_EXTRACT_SCRIPT)} "
            f"{shlex.quote(report)}"
        )
        report_result = self.orchestrator.execute_command(extraction_command)
        if report_result.get("success"):
            test_stats, junit_counts, junit_error = _parse_pytest_junit_summary(
                report_result.get("output") or "",
                collected,
            )
        else:
            test_stats, junit_counts, junit_error = None, {}, "extract_failed"
        if self.command_tracker:
            try:
                self.command_tracker.track_test_command(
                    command=command,
                    tool="python",
                    working_dir=working_directory,
                    exit_code=exit_code,
                    output=output,
                )
            except Exception as exc:  # tracking must never mask the honest result
                logger.debug(f"python test tracking skipped: {exc}")

        executed, collection_errors = self._executed_and_collection_errors(
            junit_counts=junit_counts,
            success=success,
            error_code=error_code,
            selected=collected_after_deselection,
        )
        metadata = {
            "operation": "test",
            "command": command,
            "runner_dispatched": result.get("runner_dispatched") is True,
            "exit_code": exit_code,
            "report": report,
            "attempt_id": attempt_id,
            "collected": collected,
            "collected_after_deselection": collected_after_deselection,
            "collection_scope": collection_scope,
            "collected_json": COLLECTED_JSON,
            # Byte-compat: `receipt_id` when the receipt landed,
            # `receipt_persisted: false` when it did not — never both.
            **receipt_metadata,
            **({"selection_mode": native_selection_mode} if native_selection_mode else {}),
            **gate_metadata,
            **(
                {
                    "smoke_candidate": native_smoke["path"],
                    "smoke_candidate_source": native_smoke["source"],
                }
                if native_smoke and native_selection_mode == "survey_candidate"
                else {}
            ),
            **junit_counts,
            # Derived AFTER junit_counts on purpose: `tests` counts JUnit
            # testcase nodes, which a collection failure fills with collection
            # nodes. `executed`/`collection_errors` are the honest projection.
            "executed": executed,
            "collection_errors": collection_errors,
        }
        # Capability receipt: a bounded smoke that actually executed tests
        # cleanly is the ONLY thing that unlocks a later full collect.
        smoke_failed = int(junit_counts.get("failed_tests") or 0)
        smoke_errors = int(junit_counts.get("error_tests") or 0)
        smoke_skipped = int(junit_counts.get("skipped_tests") or 0)
        smoke_clean = (
            native_bounded
            and collection_scope == "filtered"
            and native_smoke is not None
            and success
            and isinstance(executed, int)
            and executed >= 1
            and collection_errors == 0
            and smoke_failed == 0
            and smoke_errors == 0
        )
        # P0-E (ground-truth review 2026-07-26): a clean run is not evidence.
        # The live TVM receipt was minted from 3 selected / 3 skipped, so a
        # smoke that proved NOTHING unlocked the next full collect. Positive
        # evidence — at least one non-skipped pass — is now required.
        smoke_passed = (
            (executed - smoke_failed - smoke_errors - smoke_skipped)
            if isinstance(executed, int)
            else 0
        )
        if smoke_clean and smoke_passed >= 1:
            self._write_native_smoke_receipt(
                project_root=native_project_root,
                candidate=native_smoke["path"],
                stats={
                    "executed": executed,
                    "selected": collected_after_deselection,
                    "passed": smoke_passed,
                    "failed": smoke_failed,
                    "errors": smoke_errors,
                    "skipped": smoke_skipped,
                },
                attempt=attempt_id,
            )
            metadata["smoke_receipt_written"] = True
        elif smoke_clean:
            metadata["smoke_capability_unproven"] = True
        if junit_error:
            metadata["junit_extraction"] = {
                "status": "unavailable",
                "reason": junit_error,
            }
        else:
            metadata["junit_extraction"] = {
                "status": "available",
                "transport": "container_elementtree_json",
            }
        metadata["junit_attempt_id"] = {
            "status": "available" if attempt_tagged else "unavailable",
            "value": attempt_id,
        }
        raw_data = {
            **junit_counts,
            "junit_status": junit_error or "available",
        }
        result_conflicts = [_PYTEST_JUNIT_CONFLICT] if junit_error else []
        # A wholly unavailable JUnit report already carries the stronger
        # pytest_junit_unavailable conflict. Report attempt persistence as a
        # separate conflict only when the XML was otherwise usable.
        if not attempt_tagged and not junit_error:
            result_conflicts.append(_PYTEST_ATTEMPT_ID_CONFLICT)
        # Model-visible collection facts on EVERY pytest attempt: the scope the
        # harness chose and what it actually produced. The audit found the model
        # reading a full-suite collect as a passing test run.
        preamble.append(
            self._collection_line(
                scope=collection_scope,
                collected=collected,
                selected=collected_after_deselection,
                executed=executed,
                collection_errors=collection_errors,
            )
        )
        if metadata.get("smoke_capability_unproven"):
            preamble.append(
                f"[test] bounded smoke: all {executed} selected tests were skipped — "
                "capability NOT proven; no receipt written; the next bare test call "
                "remains bounded"
            )
            # Stage E (P0-D): the live TVM smoke showed the model three bare
            # SKIPPED labels, so it never saw `need llvm` — the fact that names
            # the missing capability. The reasons the run already wrote to its
            # own junit XML are now model-visible.
            skip_reasons = self._junit_skip_reasons(python, report)
            if skip_reasons:
                metadata["smoke_skip_reasons"] = skip_reasons
                preamble.append(f"[test] skip reasons: {'; '.join(skip_reasons)}")
        tail = self._tail(output)
        if success:
            return self._finish(
                ToolResult.completed_success(
                    output=tail,
                    raw_output=runner_output,
                    raw_data=raw_data,
                    metadata=metadata,
                    test_stats=test_stats,
                    evidence_refs=[report],
                    conflicts=result_conflicts,
                ),
                preamble,
            )
        return self._finish(
            ToolResult.completed_failure(
                output=tail,
                raw_output=runner_output,
                error=error,
                error_code=error_code,
                raw_data=raw_data,
                metadata=metadata,
                test_stats=test_stats,
                evidence_refs=[report],
                conflicts=result_conflicts,
            ),
            preamble,
        )

    def _sanitize_pytest_args(
        self,
        raw: str,
        working_directory: str,
        *,
        required_project_root: Optional[str] = None,
        required_smoke_boundary: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Bug #13 defect 7: simple allowlist heuristic — pytest-plausible
        flags and EXISTING test paths pass; everything else is rejected with
        the correct usage named. Returns (cleaned_args, None) on acceptance,
        (None, reason) on rejection.

        A native-unready invocation is stricter: selectors such as ``-k`` and
        ``-m`` refine a concrete project-owned coordinate, but can never
        replace one.  The path is checked through realpath so a symlink below
        the checkout cannot redirect collection outside the project.
        """
        try:
            tokens = shlex.split(raw)
        except ValueError as exc:
            return None, f"args are not shell-parseable: {exc}"
        project_root = posixpath.normpath(required_project_root) if required_project_root else None
        smoke_boundary = (
            posixpath.normpath(required_smoke_boundary) if required_smoke_boundary else None
        )
        normalized_workdir = posixpath.normpath(working_directory)
        if project_root and not (
            normalized_workdir == project_root or normalized_workdir.startswith(f"{project_root}/")
        ):
            return None, (
                f"working directory {working_directory!r} is outside the surveyed "
                f"project {project_root!r}"
            )
        if project_root and smoke_boundary is None:
            return None, (
                "native-unready explicit args require a current allowlisted, "
                "project-owned survey smoke coordinate"
            )
        cleaned: List[str] = []
        pending_flag: Optional[str] = None
        concrete_path_seen = False
        for token in tokens:
            if pending_flag is not None:
                if pending_flag == "--maxfail" and not token.isdigit():
                    return None, f"--maxfail needs a number, got {token!r}"
                cleaned.append(shlex.quote(token))
                pending_flag = None
                continue
            if token in _PYTEST_VALUE_FLAGS:
                cleaned.append(token)
                pending_flag = token
                continue
            if _PYTEST_FLAG_RE.fullmatch(token):
                cleaned.append(token)
                continue
            if token.startswith("-"):
                return None, (f"{token!r} is not an accepted pytest flag. {_PYTEST_USAGE_HINT}")
            path = token.split("::", 1)[0]
            if project_root and posixpath.normpath(path) == ".":
                return None, (
                    f"{token!r} is not a concrete native smoke path. " f"{_PYTEST_USAGE_HINT}"
                )
            full = posixpath.normpath(
                path if path.startswith("/") else posixpath.join(normalized_workdir, path)
            )
            if project_root:
                owned, reason = verify_project_owned_path(
                    self.orchestrator.execute_command,
                    project_root,
                    full,
                )
                if not owned:
                    return None, f"{token!r} is unsafe: {reason}. {_PYTEST_USAGE_HINT}"
                bounded, reason = verify_path_within_smoke_boundary(
                    self.orchestrator.execute_command,
                    smoke_boundary,
                    full,
                )
                if not bounded:
                    return None, f"{token!r} is unsafe: {reason}. {_PYTEST_USAGE_HINT}"
                concrete_path_seen = True
            else:
                probe = self.orchestrator.execute_command(
                    f"test -e {shlex.quote(full)} && echo EXISTS || echo MISSING"
                )
                if "EXISTS" not in (probe.get("output") or ""):
                    return None, (
                        f"{token!r} is not an existing test path under "
                        f"{working_directory} — this is not a make/shell command line. "
                        f"{_PYTEST_USAGE_HINT}"
                    )
            cleaned.append(shlex.quote(token))
        if pending_flag is not None:
            return None, f"{pending_flag} requires a value"
        if project_root and not concrete_path_seen:
            return None, (
                "native-unready pytest args must include a concrete existing path "
                "inside the surveyed project; selector-only -k/-m args are unsafe"
            )
        return " ".join(cleaned), None

    def _native_project_ready(
        self,
        *,
        python: str,
        working_directory: str,
        requirements: Dict[str, Any],
    ) -> bool:
        """Conservative project-owned native readiness probe.

        An arbitrary dependency distribution or shared library must not disable
        the bounded-smoke guard. Readiness requires the exact surveyed
        distribution, a PEP 610 origin resolving to its Python install root,
        all surveyed imports, and a native artifact below a repository-rooted
        surveyed artifact boundary.
        """
        distribution_name = str(requirements.get("python_distribution_name") or "").strip()
        survey_root = str(
            (requirements.get("survey") or {}).get("project_path")
            or requirements.get("python_root")
            or working_directory
        ).rstrip("/")
        install_root = str(
            requirements.get("python_root") or survey_root or working_directory
        ).rstrip("/")
        package_names = [
            str(item.get("import_name") or "").strip()
            for item in requirements.get("python_package_paths") or ()
            if (
                isinstance(item, dict)
                and str(item.get("import_name") or "").strip()
                and all(
                    part.isidentifier()
                    for part in str(item.get("import_name") or "").strip().split(".")
                )
            )
        ]
        if not package_names:
            package_names = [
                str(name).strip()
                for name in requirements.get("python_packages") or ()
                if str(name).strip()
                and all(part.isidentifier() for part in str(name).strip().split("."))
            ]
        artifact_roots = []
        for path in requirements.get("native_artifact_roots") or ():
            raw = str(path or "").strip()
            normalized = posixpath.normpath(raw)
            if (
                not raw
                or raw.startswith("/")
                or normalized in (".", "..")
                or normalized.startswith("../")
            ):
                continue
            artifact_roots.append(normalized)
        if (
            not survey_root.startswith("/workspace/")
            or not (install_root == survey_root or install_root.startswith(f"{survey_root}/"))
            or not distribution_name
            or not package_names
            or not artifact_roots
        ):
            return False
        command = (
            f"{shlex.quote(python)} -c {shlex.quote(_NATIVE_PROJECT_READY_SCRIPT)} "
            f"{shlex.quote(distribution_name)} {shlex.quote(install_root)} "
            f"{shlex.quote(survey_root)} "
            f"{shlex.quote(json.dumps(package_names))} "
            f"{shlex.quote(json.dumps(artifact_roots))} "
            f"{shlex.quote('/workspace')}"
        )
        result = self.orchestrator.execute_command(command, workdir=working_directory)
        return bool(result.get("success")) and "SAG_NATIVE_PROJECT_READY" in (
            result.get("output") or ""
        )

    def _verified_native_smoke_candidate(
        self,
        *,
        working_directory: str,
        requirements: Dict[str, Any],
    ) -> Optional[Dict[str, str]]:
        """Return the first current, realpath-contained surveyed smoke target."""
        project_root = str(
            (requirements.get("survey") or {}).get("project_path")
            or requirements.get("python_root")
            or working_directory
        ).rstrip("/")
        verified = verified_python_smoke_candidate(
            self.orchestrator.execute_command,
            project_root,
            requirements.get("python_smoke_candidates") or [],
        )
        if verified is None:
            return None
        pytest_path = (
            verified["path"]
            if working_directory.rstrip("/") == posixpath.normpath(project_root)
            else verified["absolute_path"]
        )
        return {
            "path": verified["path"],
            "source": verified["source"],
            "absolute_path": verified["absolute_path"],
            "args": f"{shlex.quote(pytest_path)} --maxfail=1",
        }

    @staticmethod
    def _executed_and_collection_errors(
        *,
        junit_counts: Dict[str, int],
        success: bool,
        error_code: Optional[str],
        selected: Optional[int],
    ) -> Tuple[Optional[int], Optional[int]]:
        """Honest (executed, collection_errors) for one pytest attempt.

        A collection failure executes NOTHING: pytest still writes a testcase
        node per uncollectable file (the live TVM artifact: tests="56",
        errors="28", skipped="28"), so the JUnit `tests` attribute must never
        be laundered into an executed count. The structured per-node count
        arrives with Plan 4 Task 2; until it does, a collection failure reports
        an unknown (never a zero) collection-error count.
        """
        structured = junit_counts.get("collection_errors")
        collection_errors = structured if isinstance(structured, int) else None
        if error_code == "PYTEST_COLLECTION_ERROR":
            return 0, collection_errors
        if collection_errors is None:
            collection_errors = 0
        if error_code in _PYTEST_NOTHING_RAN_CODES:
            return 0, collection_errors
        junit_tests = junit_counts.get("tests")
        if isinstance(junit_tests, int):
            return junit_tests - collection_errors, collection_errors
        # No usable JUnit report: a completed green run executed what it
        # selected; anything else leaves the count unknown rather than invented.
        return (selected if success else None), collection_errors

    @staticmethod
    def _collection_line(
        *,
        scope: str,
        collected: Optional[int],
        selected: Optional[int],
        executed: Optional[int],
        collection_errors: Optional[int],
    ) -> str:
        """The one model-visible collection fact line of a pytest attempt."""

        def rendered(value: Optional[int]) -> str:
            return "unknown" if value is None else str(value)

        return (
            f"Collection: {scope} — {rendered(collected)} collected, "
            f"{rendered(selected)} selected, {rendered(executed)} executed, "
            f"{rendered(collection_errors)} collection errors"
        )

    # Up to three reasons, each short enough that the whole projection stays one
    # model-visible line.
    _SKIP_REASON_LIMIT = 3
    _SKIP_REASON_MAX_CHARS = 120

    def _junit_skip_reasons(self, python: str, report: str) -> List[str]:
        """Distinct skip messages from THIS attempt's junit XML, or [].

        Read from the report the run just wrote, so the reasons are the
        project's own words. An unreadable/messageless report yields no
        reasons — never an invented one.
        """
        command = (
            f"{shlex.quote(python)} -c {shlex.quote(_PYTEST_JUNIT_SKIP_REASONS_SCRIPT)} "
            f"{shlex.quote(report)}"
        )
        try:
            result = self.orchestrator.execute_command(command) or {}
        except Exception as exc:  # a missing fact must never mask the result
            logger.debug(f"junit skip-reason extraction unavailable: {exc}")
            return []
        if not result.get("success"):
            return []
        try:
            payload = json.loads((result.get("output") or "").strip() or "[]")
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        reasons: List[str] = []
        for item in payload:
            message = " ".join(str(item).split())[: self._SKIP_REASON_MAX_CHARS]
            if message and message not in reasons:
                reasons.append(message)
            if len(reasons) >= self._SKIP_REASON_LIMIT:
                break
        return reasons

    def _record_control_assessment(self, typed_code: str, detail: str) -> bool:
        """Record a pre-dispatch refusal as a typed control fact (spec §C4).

        A refusal ran no runner, so it mints no invocation receipt — but the
        ToolResult alone leaves no evidence a later reader can consult. The
        assessment says which stage refused and under which typed code, and it
        can never contradict a project-owned claim. Never blocks the refusal.
        """
        return write_assessment(
            self.orchestrator.execute_command,
            ControlAssessment(
                event_or_intent_id=next_control_event_id("python-test"),
                stage="precondition",
                typed_code=typed_code,
                detail=detail,
            ),
        )

    def _target_sha(self, project_root: Optional[str]) -> Optional[str]:
        """The current checkout SHA of `project_root`, or None when unknown."""
        if not project_root:
            return None
        result = self.orchestrator.execute_command(
            f"git -C {shlex.quote(project_root)} rev-parse HEAD"
        )
        if not result.get("success"):
            return None
        return ((result.get("output") or "").strip()) or None

    def _native_smoke_receipt(self, project_root: Optional[str]) -> Optional[Dict[str, Any]]:
        """Return typed live capability authority once that schema exists.

        ``native_smoke_receipt.json`` is retained only as a forensic mirror.
        It is container-writable and therefore cannot unlock a broader runner
        dispatch.  InvocationReceipt v2 does not yet freeze the bounded
        selector/count/pass tuple needed to validate that capability, so the
        only safe live answer is absent and the next attempt stays bounded.
        """
        del project_root
        return None

    def _write_native_smoke_receipt(
        self,
        *,
        project_root: Optional[str],
        candidate: str,
        stats: Dict[str, Any],
        attempt: int,
    ) -> None:
        """Write a forensic smoke summary; never live dispatch authority."""
        target_sha = self._target_sha(project_root)
        body = json.dumps(
            {
                "project_root": (project_root or "").rstrip("/"),
                "candidate": candidate,
                "stats": stats,
                "attempt": attempt,
                **({"target_sha": target_sha} if target_sha else {}),
            },
            sort_keys=True,
        )
        self.orchestrator.execute_command("mkdir -p /workspace/.setup_agent")
        self.orchestrator.execute_command(
            f"cat > {NATIVE_SMOKE_RECEIPT_JSON} <<'SAGEOF'\n{body}\nSAGEOF"
        )

    @staticmethod
    def _bounded_native_pytest_args(pytest_args: str) -> str:
        """Force fail-fast on a native-unready explicit smoke selector."""
        tokens = shlex.split(pytest_args or "")
        if "-x" not in tokens and "--maxfail=1" not in tokens:
            return f"{pytest_args} --maxfail=1".strip()
        return pytest_args

    # ------------------------------------------------------------------
    # build (wheel — extra evidence, never required for green)
    # ------------------------------------------------------------------

    def _build_wheel(
        self,
        working_directory: str,
        args: Optional[str],
        timeout: int,
        requirements: Dict[str, Any],
        venv: str,
    ) -> ToolResult:
        before_wheels = self._wheel_snapshot(working_directory)
        self._run(
            f"{venv}/bin/python -m pip install build",
            working_directory,
            timeout,
            producer_role="build_prerequisite",
            mechanical=True,
        )
        result = self._run(
            f"{venv}/bin/python -m build --wheel",
            working_directory,
            timeout,
            producer_role="wheel_build",
        )
        after_wheels = self._wheel_snapshot(working_directory)
        trace = _ACTIVE_PRODUCER_TRACE.get()
        if before_wheels is None or after_wheels is None:
            build_observation = {"artifact_status": "probe_failed", "artifacts": []}
        else:
            artifacts = []
            for path in sorted(after_wheels):
                digest, size = after_wheels[path]
                previous = before_wheels.get(path)
                if previous == (digest, size):
                    continue
                artifacts.append(
                    {
                        "path": posixpath.relpath(path, working_directory.rstrip("/")),
                        "sha256": digest,
                        "size_bytes": size,
                        "change": "new" if previous is None else "changed",
                    }
                )
            build_observation = {
                "artifact_status": (
                    "produced" if artifacts else ("stale_only" if after_wheels else "missing")
                ),
                "artifacts": artifacts,
            }
        if trace is not None:
            trace.details["build"] = build_observation
        success = bool(result.get("success"))
        metadata = {
            "operation": "build",
            "exit_code": result.get("exit_code"),
            # Settled spec decision: the wheel is EXTRA evidence. Callers must
            # not redden a verdict on this result.
            "evidence_only": True,
        }
        tail = self._tail(result.get("output") or "")
        if success:
            return ToolResult.completed_success(
                output=tail,
                raw_output=result.get("output"),
                metadata=metadata,
            )
        return ToolResult.completed_failure(
            output=tail,
            raw_output=result.get("output"),
            error="wheel build failed (evidence only — never required for green)",
            error_code="WHEEL_BUILD_FAILED",
            metadata=metadata,
        )

    def _wheel_snapshot(self, working_directory: str) -> Optional[Dict[str, Tuple[str, int]]]:
        """Bounded content/size snapshot of wheels directly under this root's dist/."""

        root = working_directory.rstrip("/")
        dist = f"{root}/dist"
        script = (
            "for file do "
            "digest=$(sha256sum -- \"$file\" | cut -d' ' -f1) || exit 1; "
            'size=$(wc -c < "$file") || exit 1; '
            f"printf '{_WHEEL_SNAPSHOT_PREFIX}\\t%s\\t%s\\t%s\\n' "
            '"$size" "$digest" "$file"; '
            "done"
        )
        command = (
            f"find {shlex.quote(dist)} -maxdepth 1 -type f -name '*.whl' "
            f"-exec sh -c {shlex.quote(script)} sh {{}} + 2>/dev/null | sort | head -n 33"
        )
        result = self.orchestrator.execute_command(command, workdir=working_directory) or {}
        if not result.get("success"):
            return None
        lines = [line for line in str(result.get("output") or "").splitlines() if line.strip()]
        if len(lines) > 32:
            return None
        snapshot: Dict[str, Tuple[str, int]] = {}
        for line in lines:
            fields = line.split("\t")
            if len(fields) != 4 or fields[0] != _WHEEL_SNAPSHOT_PREFIX:
                return None
            _, raw_size, digest, path = fields
            raw_size = raw_size.strip()
            digest = digest.strip().lower()
            normalized = posixpath.normpath(path)
            if (
                not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not raw_size.isdigit()
                or not normalized.startswith(dist + "/")
                or "/" in normalized[len(dist) + 1 :]
                or not re.fullmatch(r"[A-Za-z0-9_.+!-]+\.whl", posixpath.basename(normalized))
            ):
                return None
            snapshot[normalized] = (digest, int(raw_size))
        return snapshot

    # ------------------------------------------------------------------
    # compile (the compileall evidence generator)
    # ------------------------------------------------------------------

    def _compileall(
        self,
        working_directory: str,
        args: Optional[str],
        timeout: int,
        requirements: Dict[str, Any],
        venv: str,
    ) -> ToolResult:
        dirs = self._package_dirs(working_directory, requirements)
        target = " ".join(shlex.quote(directory) for directory in dirs)
        result = self._run(
            f"{venv}/bin/python -m compileall -q {target}",
            working_directory,
            timeout,
            producer_role="compileall",
        )
        metric_result = self._run(
            compileall_metrics_command(f"{venv}/bin/python", dirs),
            working_directory,
            timeout,
            producer_role="compile_metrics",
        )
        metric = None
        metric_error = None
        try:
            if not metric_result.get("success"):
                raise ValueError("scanner command failed")
            metric = parse_compileall_metrics(metric_result.get("output") or "")
        except (TypeError, ValueError) as exc:
            metric_error = str(exc)

        if metric is not None:
            py_count = metric.source_count
            pyc_count = metric.compiled_source_count
            failed = metric.missing_source_count
            coverage = metric.coverage
            metric_status = metric.status
            metric_conflicts = list(metric.conflicts)
            foreign_pyc_count = metric.foreign_pyc_count
            cache_tag = metric.cache_tag
        else:
            py_count = None
            pyc_count = None
            failed = None
            coverage = None
            metric_status = "unavailable"
            metric_conflicts = [COMPILEALL_METRICS_UNAVAILABLE_CONFLICT]
            foreign_pyc_count = None
            cache_tag = None

        root = working_directory.rstrip("/")

        def relative(path: str, *, allow_dot: bool = False) -> Optional[str]:
            normalized = posixpath.normpath(str(path or ""))
            if normalized != root and not normalized.startswith(root + "/"):
                return None
            value = posixpath.relpath(normalized, root)
            if value == "." and not allow_dot:
                return None
            return value

        relative_roots = [relative(directory, allow_dot=True) for directory in dirs]
        trace = _ACTIVE_PRODUCER_TRACE.get()
        if any(path is None for path in relative_roots):
            compile_observation: Dict[str, Any] = {
                "status": "unavailable",
                "roots": [path for path in relative_roots if path is not None] or ["."],
                "roots_sha256": producer_observations_sha256(
                    [path for path in relative_roots if path is not None] or ["."]
                ),
                "reason_code": "basis_escaped",
            }
        elif metric is None:
            roots = [str(path) for path in relative_roots]
            compile_observation = {
                "status": "unavailable",
                "roots": roots,
                "roots_sha256": producer_observations_sha256(roots),
                "reason_code": (
                    "metrics_command_failed"
                    if not metric_result.get("success")
                    else "metrics_output_invalid"
                ),
            }
        elif not result.get("success"):
            roots = [str(path) for path in relative_roots]
            compile_observation = {
                "status": "unavailable",
                "roots": roots,
                "roots_sha256": producer_observations_sha256(roots),
                "reason_code": "compileall_failed",
            }
        elif metric.status == "unavailable":
            roots = [str(path) for path in relative_roots]
            compile_observation = {
                "status": "unavailable",
                "roots": roots,
                "roots_sha256": producer_observations_sha256(roots),
                "reason_code": "no_sources",
            }
        else:
            missing_paths = [relative(path) for path in metric.missing_sources]
            foreign_paths = [relative(path) for path in metric.foreign_pycs]
            if any(path is None for path in missing_paths + foreign_paths):
                roots = [str(path) for path in relative_roots]
                compile_observation = {
                    "status": "unavailable",
                    "roots": roots,
                    "roots_sha256": producer_observations_sha256(roots),
                    "reason_code": "basis_escaped",
                }
            else:
                roots = [str(path) for path in relative_roots]
                compile_observation = {
                    "status": metric.status,
                    "roots": roots,
                    "roots_sha256": producer_observations_sha256(roots),
                    "source_count": metric.source_count,
                    "compiled_source_count": metric.compiled_source_count,
                    "missing_source_count": metric.missing_source_count,
                    "foreign_pyc_count": metric.foreign_pyc_count,
                    "cache_tag": metric.cache_tag,
                    "source_basis_sha256": metric.source_basis_sha256,
                    "pyc_basis_sha256": metric.pyc_basis_sha256,
                    "source_basis_entry_count": metric.source_basis_entry_count,
                    "pyc_basis_entry_count": metric.pyc_basis_entry_count,
                    "missing_sources": [str(path) for path in missing_paths],
                    "foreign_pycs": [str(path) for path in foreign_paths],
                    "conflicts": list(metric.conflicts),
                }
                if metric.coverage is not None:
                    compile_observation["coverage"] = metric.coverage
        if trace is not None:
            trace.details["compile"] = compile_observation

        if metric_status == "unavailable" and py_count == 0:
            # Bug #13 defect 8: 0/0 compiled is VACUOUS evidence — say so
            # instead of a misleading green ('0/0 sources compiled').
            return ToolResult.completed_success(
                output=f"no sources found under {target} — nothing verified",
                raw_output=result.get("output"),
                metadata={
                    "operation": "compile",
                    "dirs": dirs,
                    "py_count": 0,
                    "pyc_count": pyc_count,
                    "failed": None,
                    "coverage": None,
                    "compileall_metric_status": metric_status,
                    "metrics_conflicts": metric_conflicts,
                    "foreign_pyc_count": foreign_pyc_count,
                    "cache_tag": cache_tag,
                    "exit_code": result.get("exit_code"),
                    "vacuous": True,
                },
                conflicts=metric_conflicts,
            )
        summary = f"compileall over {target}: "
        if metric_status == "invalid":
            summary += (
                "invalid (source/PYC basis mismatch; " f"{foreign_pyc_count or 0} foreign pyc)"
            )
        elif py_count is not None and pyc_count is not None:
            summary += f"{pyc_count}/{py_count} sources compiled, {failed} failed"
            if coverage is not None:
                summary += f" (coverage {coverage:.2f})"
        else:
            summary += f"source/bytecode counts unavailable ({metric_error or 'unknown reason'})"
        success = bool(result.get("success"))
        errors = self._tail(result.get("output") or "", lines=20)
        return ToolResult.completed(
            operation_outcome="success" if success else "failed",
            output=summary + (f"\n{errors}" if errors else ""),
            raw_output=result.get("output"),
            error=None if success else "compileall reported errors",
            error_code=None if success else "COMPILEALL_ERRORS",
            metadata={
                "operation": "compile",
                "dirs": dirs,
                "py_count": py_count,
                "pyc_count": pyc_count,
                "failed": failed,
                "coverage": coverage,
                "compileall_metric_status": metric_status,
                "metrics_conflicts": metric_conflicts,
                "foreign_pyc_count": foreign_pyc_count,
                "cache_tag": cache_tag,
                "exit_code": result.get("exit_code"),
            },
            conflicts=metric_conflicts,
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _with_env(command: str, env_overlay: Optional[Dict[str, str]]) -> str:
        """`NAME=value command`, in sorted name order; the command when empty.

        Sorted so the same overlay always renders the same prefix — the
        environment a dispatch ran under is a fact the receipt records, and a
        fact whose spelling depends on dict order is not one.
        """
        if not env_overlay:
            return command
        prefix = " ".join(
            f"{name}={shlex.quote(str(env_overlay[name]))}" for name in sorted(env_overlay)
        )
        return f"{prefix} {command}"

    def _run(
        self,
        command: str,
        workdir: str,
        timeout: int,
        env_overlay: Optional[Dict[str, str]] = None,
        *,
        producer_role: Optional[str] = None,
        mechanical: bool = False,
        monitor_floor: int = 600,
    ) -> Dict[str, Any]:
        """One container command; monitored path when the orchestrator has it
        (installs and test runs are long), plain execute_command otherwise.

        `env_overlay` is prepended as a shell assignment prefix rather than
        exported: it applies to THIS command and cannot leak into the next one.
        """
        command = self._with_env(command, env_overlay)
        trace = _ACTIVE_PRODUCER_TRACE.get()
        if trace is not None and not producer_role:
            raise RuntimeError(
                f"contract-owned python {trace.operation} command has no producer role"
            )
        if hasattr(self.orchestrator, "execute_command_with_monitoring"):
            result = self.orchestrator.execute_command_with_monitoring(
                command,
                workdir=workdir,
                silent_timeout=max(timeout, monitor_floor),
                absolute_timeout=max(timeout, monitor_floor),
                optimize_for_maven=False,
            )
        else:
            result = self.orchestrator.execute_command(command, workdir=workdir)
        normalized = dict(result or {})
        if trace is not None:
            trace.record(str(producer_role), mechanical, command, normalized)
            if normalized.get("dispatch_status") in DETACHED_HANDOFF_STATUSES:
                raise _ProducerDetached(command, normalized)
        return normalized

    def _venv_exists(self, venv: str) -> bool:
        probe = self.orchestrator.execute_command(
            f"test -x {venv}/bin/python && echo EXISTS || echo MISSING"
        )
        return "EXISTS" in (probe.get("output") or "")

    def _package_dirs(self, working_directory: str, requirements: Dict[str, Any]) -> List[str]:
        """Package source dirs: manifest packages (src-layout probed first),
        shared discovery as fallback, the project dir as the last resort."""
        root = working_directory.rstrip("/")
        package_root = str(requirements.get("python_root") or root).rstrip("/")
        surveyed_dirs: List[str] = []
        for item in requirements.get("python_package_paths") or ():
            if not isinstance(item, dict):
                continue
            relative = posixpath.normpath(str(item.get("path") or "").strip())
            if (
                not relative
                or relative == "."
                or relative.startswith("../")
                or relative.startswith("/")
            ):
                continue
            candidate = posixpath.normpath(f"{package_root}/{relative}")
            if not candidate.startswith(package_root + "/"):
                continue
            probe = self.orchestrator.execute_command(
                f"test -d {shlex.quote(candidate)} && echo EXISTS || echo MISSING"
            )
            if "EXISTS" in (probe.get("output") or ""):
                surveyed_dirs.append(candidate)
        if surveyed_dirs:
            return list(dict.fromkeys(surveyed_dirs))
        packages = requirements.get("python_packages") or discover_packages(
            self.orchestrator, package_root
        )
        dirs: List[str] = []
        for package in packages:
            for candidate in (
                f"{package_root}/src/{package}",
                f"{package_root}/{package}",
            ):
                probe = self.orchestrator.execute_command(
                    f"test -d {candidate} && echo EXISTS || echo MISSING"
                )
                if "EXISTS" in (probe.get("output") or ""):
                    dirs.append(candidate)
                    break
        return dirs or [package_root]

    def _parse_collected(self, output: str) -> Optional[int]:
        """Trailing `N tests collected` from pytest --collect-only -q; a `no
        tests collected` suite records an honest 0 — never invented."""
        matches = _COLLECTED_RE.findall(output or "")
        if matches:
            return int(matches[-1])
        if _NO_TESTS_RE.search(output or ""):
            return 0
        return None

    def _parse_collected_after_deselection(self, output: str) -> Optional[int]:
        """The SELECTED count of a scoped `--collect-only -q` run: the X of
        `X/Y tests collected (Z deselected)`; a plain `N tests collected`
        (nothing deselected) IS the selection; `no tests collected` is an
        honest 0; unparseable is None — never invented."""
        matches = _SELECTED_RE.findall(output or "")
        if matches:
            return int(matches[-1])
        return self._parse_collected(output)

    def _write_collected(
        self,
        collected: Optional[int],
        *,
        scope: str,
        selected: Optional[int],
    ) -> None:
        body = json.dumps(
            {
                "collected": collected,
                "scope": scope,
                "selected": selected,
            }
        )
        self.orchestrator.execute_command("mkdir -p /workspace/.setup_agent")
        self.orchestrator.execute_command(f"cat > {COLLECTED_JSON} <<'SAGEOF'\n{body}\nSAGEOF")

    @staticmethod
    def _tail(output: str, lines: int = 60) -> str:
        rows = (output or "").strip().splitlines()
        if len(rows) <= lines:
            return "\n".join(rows)
        return "\n".join([f"... [{len(rows) - lines} lines omitted] ..."] + rows[-lines:])

    @staticmethod
    def _finish(tool_result: ToolResult, preamble: List[str]) -> ToolResult:
        """Prepend display narration without changing bound runner evidence."""
        if preamble:
            head = "\n".join(preamble) + "\n"
            tool_result.output = head + (tool_result.output or "")
        return tool_result
