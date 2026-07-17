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

import json
import re
import shlex
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from sag.evidence import TestStats
from sag.testcases.compileall_metrics import (
    COMPILEALL_METRICS_UNAVAILABLE_CONFLICT,
    compileall_metrics_command,
    parse_compileall_metrics,
)

from ..base import BaseTool, ToolResult
from .build_preflight import (
    PythonPreflight,
    active_python_version,
    classify_python_version_error,
    read_build_requirements,
)
from .python_env import (
    detect_installer,
    discover_packages,
    ensure_venv_pip,
    venv_repair_note,
)

# The verifier (Task 6) reads both: the JUnit XML under PYTEST_REPORT_DIR for
# executed counts, COLLECTED_JSON as the detected-tests denominator feeding
# the tests_not_fully_executed gate.
PYTEST_REPORT_DIR = "/workspace/.setup_agent/pytest-reports"
COLLECTED_JSON = "/workspace/.setup_agent/pytest_collected.json"

# The pip rung a failed poetry/pipenv install falls back to (narrated).
# Module form (bug #12): plain uv venvs ship no {venv}/bin/pip binary.
_PIP_FALLBACK = "{venv}/bin/python -m pip install -e ."

_COLLECTED_RE = re.compile(r"(\d+)\s+tests?\s+collected")
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

_OPERATIONS = ("setup_env", "test", "build", "compile")
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
        detail = _snippet(_COLLECTION_ERROR_RE) or f"pytest exited {exit_code}"
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
        if _COLLECTION_ERROR_RE.search(text):
            return (
                False,
                f"pytest collection error — {_snippet(_COLLECTION_ERROR_RE)}",
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

    def execute(
        self,
        operation: str,
        working_directory: str = "/workspace",
        args: str = None,
        timeout: int = 600,
    ) -> ToolResult:
        op = (operation or "").strip().lower()
        if op not in _OPERATIONS:
            return ToolResult.completed_failure(
                output="",
                error=f"Unknown python operation: {operation!r}",
                error_code="UNKNOWN_PYTHON_OPERATION",
                suggestions=[f"Valid operations: {', '.join(_OPERATIONS)}"],
            )
        requirements = read_build_requirements(self.orchestrator)
        venv = requirements.get("python_venv") or f"{working_directory.rstrip('/')}/.venv"
        handler = {
            "setup_env": self._setup_env,
            "test": self._run_tests,
            "build": self._build_wheel,
            "compile": self._compileall,
        }[op]
        return handler(working_directory, args, timeout, requirements, venv)

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
    ) -> ToolResult:
        # Pre-flight FIRST (narration prepended, same pattern as the ported
        # maven/gradle tools): check-and-fix, never a hard block.
        preamble: List[str] = []
        outcome = PythonPreflight(self.orchestrator).run(
            requirements.get("python_version"),
            constraint=requirements.get("python_constraint"),
            source=requirements.get("python_version_source") or "requires-python",
        )
        if outcome.narration:
            preamble.append(outcome.narration)

        # Venv on the pre-flight's interpreter. A provisioning pre-flight has
        # already created the venv (uv venv / pythonX.Y -m venv).
        if not outcome.provisioned and not self._venv_exists(venv):
            made = self._run(f"python3 -m venv {venv}", working_directory, timeout)
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
                            "Run project(action='analyze') to (re)generate the "
                            "build-requirements manifest",
                            "Check that working_directory points at the project root",
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
        retried = False
        overall_ok = True
        failure_detail: Optional[str] = None
        for cmd in commands:
            result = self._run(cmd, working_directory, timeout)

            # Bounded retry (spec: exactly once): pip's Requires-Python
            # rejection is authoritative; re-provision from it and rerun ONCE.
            if not result.get("success") and not retried:
                needed = classify_python_version_error(result.get("output") or "")
                active = outcome.active_version or active_python_version(self.orchestrator)
                if needed and needed != active:
                    retried = True
                    retry_outcome = PythonPreflight(self.orchestrator).run(
                        needed, source="install-error"
                    )
                    if retry_outcome.provisioned:
                        preamble.append(
                            f"[pre-flight] install error requires Python {needed}, "
                            f"re-provisioned, retry 1/1"
                        )
                        retry_meta = {"from": active or "unknown", "to": needed}
                        result = self._run(cmd, working_directory, timeout)

            # Faithfulness deviation (spec Component 3): the project's own
            # tool failed; the pip rung keeps setup moving, NARRATED so the
            # generated setup docs reflect what actually ran.
            if not result.get("success") and installer in ("poetry", "pipenv"):
                deviation = (
                    f"[deviation] {installer} install failed; fell back to "
                    f"pip install -e . — setup docs must list the fallback"
                )
                preamble.append(deviation)
                transcript.append(f"$ {cmd}\n{self._tail(result.get('output') or '')}")
                cmd = _PIP_FALLBACK.replace("{venv}", venv)
                result = self._run(cmd, working_directory, timeout)

            transcript.append(f"$ {cmd}\n{self._tail(result.get('output') or '')}")
            # Bug #13 defect 2: honest failure — a non-zero exit OR an
            # install-error signature in the output (a wrapper reporting
            # exit 0 while stderr said "No module named pip") is a FAILURE,
            # and the observation leads with it instead of burying it.
            masked = self._install_error_line(result.get("output") or "")
            if not result.get("success") or masked:
                overall_ok = False
                failure_detail = masked or self._failure_tail_line(result)
                preamble.insert(0, f"[setup] dependency install FAILED — {failure_detail}")
                break

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
                },
            ),
            preamble,
        )

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

        # Bug #13 defect 7: allowlist-sanitize the args BEFORE anything runs —
        # 'make test' was pasted verbatim into 'pytest make test' in the live run.
        hints = requirements.get("test_hints") or {}
        raw_args = (args or "").strip()
        if raw_args:
            pytest_args, rejection = self._sanitize_pytest_args(raw_args, working_directory)
            if rejection:
                return ToolResult.completed_failure(
                    output=f"[test] rejected args {raw_args!r} — {rejection}",
                    error=rejection,
                    error_code="PYTEST_ARGS_REJECTED",
                    suggestions=[
                        _PYTEST_USAGE_HINT,
                        "For make targets or shell commands use the bash tool instead",
                    ],
                    metadata={"operation": "test", "rejected_args": raw_args},
                )
        else:
            pytest_args = (hints.get("pytest_args") or "").strip()

        # Bug #13 defect 5: pytest bootstrap — ensure pytest is importable in
        # the venv first; live evidence: 5 test calls failed with 'No module
        # named pytest' and still looked successful.
        probe = self.orchestrator.execute_command(f"{python} -m pytest --version")
        if not probe.get("success"):
            self._run(f"{python} -m pip install pytest", working_directory, timeout)
            preamble.append("[test] pytest not in venv — installed for the run")

        # Detected-tests denominator FIRST (spec Component 3): the verifier
        # compares executed counts against it (tests_not_fully_executed).
        collect = self._run(f"{python} -m pytest --collect-only -q", working_directory, timeout)
        collected = self._parse_collected(collect.get("output") or "")
        self._write_collected(collected)

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
        result = self._run(command, working_directory, timeout)
        exit_code = result.get("exit_code")
        output = result.get("output") or ""
        attempt_tag_command = (
            f"{shlex.quote(python)} -c {shlex.quote(_PYTEST_ATTEMPT_TAG_SCRIPT)} "
            f"{shlex.quote(report)} {attempt_id}"
        )
        attempt_tag_result = self.orchestrator.execute_command(attempt_tag_command)
        attempt_tagged = attempt_tag_result.get("success")
        if attempt_tagged is None:
            attempt_tagged = attempt_tag_result.get("exit_code") == 0
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

        metadata = {
            "operation": "test",
            "command": command,
            "exit_code": exit_code,
            "report": report,
            "attempt_id": attempt_id,
            "collected": collected,
            "collected_json": COLLECTED_JSON,
            **junit_counts,
        }
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
        tail = self._tail(output)
        if success:
            return self._finish(
                ToolResult.completed_success(
                    output=tail,
                    raw_output=output,
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
                raw_output=output,
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
        self, raw: str, working_directory: str
    ) -> Tuple[Optional[str], Optional[str]]:
        """Bug #13 defect 7: simple allowlist heuristic — pytest-plausible
        flags and EXISTING test paths pass; everything else is rejected with
        the correct usage named. Returns (cleaned_args, None) on acceptance,
        (None, reason) on rejection."""
        try:
            tokens = shlex.split(raw)
        except ValueError as exc:
            return None, f"args are not shell-parseable: {exc}"
        cleaned: List[str] = []
        pending_flag: Optional[str] = None
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
            full = path if path.startswith("/") else f"{working_directory.rstrip('/')}/{path}"
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
        return " ".join(cleaned), None

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
        self._run(f"{venv}/bin/python -m pip install build", working_directory, timeout)
        result = self._run(f"{venv}/bin/python -m build --wheel", working_directory, timeout)
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
            f"{venv}/bin/python -m compileall -q {target}", working_directory, timeout
        )
        metric_result = self._run(
            compileall_metrics_command(f"{venv}/bin/python", dirs),
            working_directory,
            timeout,
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

    def _run(self, command: str, workdir: str, timeout: int) -> Dict[str, Any]:
        """One container command; monitored path when the orchestrator has it
        (installs and test runs are long), plain execute_command otherwise."""
        if hasattr(self.orchestrator, "execute_command_with_monitoring"):
            return self.orchestrator.execute_command_with_monitoring(
                command,
                workdir=workdir,
                silent_timeout=max(timeout, 600),
                absolute_timeout=max(timeout, 600),
                optimize_for_maven=False,
            )
        return self.orchestrator.execute_command(command, workdir=workdir)

    def _venv_exists(self, venv: str) -> bool:
        probe = self.orchestrator.execute_command(
            f"test -x {venv}/bin/python && echo EXISTS || echo MISSING"
        )
        return "EXISTS" in (probe.get("output") or "")

    def _package_dirs(self, working_directory: str, requirements: Dict[str, Any]) -> List[str]:
        """Package source dirs: manifest packages (src-layout probed first),
        shared discovery as fallback, the project dir as the last resort."""
        root = working_directory.rstrip("/")
        packages = requirements.get("python_packages") or discover_packages(self.orchestrator, root)
        dirs: List[str] = []
        for package in packages:
            for candidate in (f"{root}/src/{package}", f"{root}/{package}"):
                probe = self.orchestrator.execute_command(
                    f"test -d {candidate} && echo EXISTS || echo MISSING"
                )
                if "EXISTS" in (probe.get("output") or ""):
                    dirs.append(candidate)
                    break
        return dirs or [root]

    def _parse_collected(self, output: str) -> Optional[int]:
        """Trailing `N tests collected` from pytest --collect-only -q; a `no
        tests collected` suite records an honest 0 — never invented."""
        matches = _COLLECTED_RE.findall(output or "")
        if matches:
            return int(matches[-1])
        if _NO_TESTS_RE.search(output or ""):
            return 0
        return None

    def _write_collected(self, collected: Optional[int]) -> None:
        body = json.dumps({"collected": collected})
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
        """Prepend the pre-flight/deviation narration (transparency-by-
        construction, same pattern as the ported maven/gradle tools)."""
        if preamble:
            head = "\n".join(preamble) + "\n"
            tool_result.output = head + (tool_result.output or "")
            tool_result.raw_output = head + (tool_result.raw_output or "")
        return tool_result
