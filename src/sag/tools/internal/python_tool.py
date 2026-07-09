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
import time
from typing import Any, Dict, List, Optional

from loguru import logger

from ..base import BaseTool, ToolResult
from .build_preflight import (
    PythonPreflight,
    active_python_version,
    classify_python_version_error,
    read_build_requirements,
)
from .python_env import discover_packages

# The verifier (Task 6) reads both: the JUnit XML under PYTEST_REPORT_DIR for
# executed counts, COLLECTED_JSON as the detected-tests denominator feeding
# the tests_not_fully_executed gate.
PYTEST_REPORT_DIR = "/workspace/.setup_agent/pytest-reports"
COLLECTED_JSON = "/workspace/.setup_agent/pytest_collected.json"

# The pip rung a failed poetry/pipenv install falls back to (narrated).
_PIP_FALLBACK = "{venv}/bin/pip install -e ."

_COLLECTED_RE = re.compile(r"(\d+)\s+tests?\s+collected")
_NO_TESTS_RE = re.compile(r"no tests collected|no tests ran")

_OPERATIONS = ("setup_env", "test", "build", "compile")


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

    def execute(
        self,
        operation: str,
        working_directory: str = "/workspace",
        args: str = None,
        timeout: int = 600,
    ) -> ToolResult:
        op = (operation or "").strip().lower()
        if op not in _OPERATIONS:
            return ToolResult(
                success=False,
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
        self, working_directory: str, args: Optional[str], timeout: int,
        requirements: Dict[str, Any], venv: str,
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
                    ToolResult(
                        success=False,
                        output=self._tail(made.get("output") or ""),
                        error=f"could not create venv at {venv}",
                        error_code="VENV_CREATE_FAILED",
                        suggestions=["Check that python3 and the venv module are available"],
                        metadata={"operation": "setup_env", "venv": venv},
                    ),
                    preamble,
                )

        installer = requirements.get("python_installer") or "pip"
        commands = [
            c.replace("{venv}", venv).replace("{dir}", working_directory)
            for c in (requirements.get("python_install_commands") or [])
        ]
        if not commands:
            preamble.append(
                "[setup] no declared install commands in the manifest — nothing installed"
            )

        transcript: List[str] = []
        deviation: Optional[str] = None
        retry_meta: Optional[Dict[str, str]] = None
        retried = False
        overall_ok = True
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
            if not result.get("success"):
                overall_ok = False
                break

        return self._finish(
            ToolResult(
                success=overall_ok,
                output="\n".join(transcript),
                error=None if overall_ok else "dependency installation failed",
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

    # ------------------------------------------------------------------
    # test
    # ------------------------------------------------------------------

    def _run_tests(
        self, working_directory: str, args: Optional[str], timeout: int,
        requirements: Dict[str, Any], venv: str,
    ) -> ToolResult:
        python = f"{venv}/bin/python"

        # Detected-tests denominator FIRST (spec Component 3): the verifier
        # compares executed counts against it (tests_not_fully_executed).
        collect = self._run(
            f"{python} -m pytest --collect-only -q", working_directory, timeout
        )
        collected = self._parse_collected(collect.get("output") or "")
        self._write_collected(collected)

        hints = requirements.get("test_hints") or {}
        pytest_args = (args or hints.get("pytest_args") or "").strip()
        report = f"{PYTEST_REPORT_DIR}/pytest-{int(time.time())}.xml"
        self.orchestrator.execute_command(f"mkdir -p {PYTEST_REPORT_DIR}")
        command = f"{python} -m pytest"
        if pytest_args:
            command += f" {pytest_args}"
        command += f" --junitxml={report}"

        # ONE honest run per suite. pytest exit 1 (failures) is a RESULT to
        # report, never an error to retry — no rerun, ever.
        result = self._run(command, working_directory, timeout)
        exit_code = result.get("exit_code")
        success = exit_code == 0
        if self.command_tracker:
            try:
                self.command_tracker.track_test_command(
                    command=command, tool="python", working_dir=working_directory,
                    exit_code=exit_code, output=result.get("output") or "",
                )
            except Exception as exc:  # tracking must never mask the honest result
                logger.debug(f"python test tracking skipped: {exc}")

        metadata = {
            "operation": "test",
            "command": command,
            "exit_code": exit_code,
            "report": report,
            "collected": collected,
            "collected_json": COLLECTED_JSON,
        }
        tail = self._tail(result.get("output") or "")
        if success:
            return ToolResult(
                success=True, output=tail,
                raw_output=result.get("output"), metadata=metadata,
            )
        return ToolResult(
            success=False, output=tail,
            raw_output=result.get("output"),
            error=f"pytest exited {exit_code} — honest result recorded, no rerun",
            error_code="PYTEST_FAILURES" if exit_code == 1 else "PYTEST_ERROR",
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # build (wheel — extra evidence, never required for green)
    # ------------------------------------------------------------------

    def _build_wheel(
        self, working_directory: str, args: Optional[str], timeout: int,
        requirements: Dict[str, Any], venv: str,
    ) -> ToolResult:
        self._run(f"{venv}/bin/pip install build", working_directory, timeout)
        result = self._run(
            f"{venv}/bin/python -m build --wheel", working_directory, timeout
        )
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
            return ToolResult(
                success=True, output=tail,
                raw_output=result.get("output"), metadata=metadata,
            )
        return ToolResult(
            success=False, output=tail,
            raw_output=result.get("output"),
            error="wheel build failed (evidence only — never required for green)",
            error_code="WHEEL_BUILD_FAILED",
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # compile (the compileall evidence generator)
    # ------------------------------------------------------------------

    def _compileall(
        self, working_directory: str, args: Optional[str], timeout: int,
        requirements: Dict[str, Any], venv: str,
    ) -> ToolResult:
        dirs = self._package_dirs(working_directory, requirements)
        target = " ".join(dirs)
        result = self._run(
            f"{venv}/bin/python -m compileall -q {target}", working_directory, timeout
        )
        py_count = self._count(
            f"find {target} -name '*.py' -not -path '*/.*' | wc -l", working_directory
        )
        pyc_count = self._count(
            f"find {target} -path '*/__pycache__/*.pyc' | wc -l", working_directory
        )
        failed = (
            max(py_count - pyc_count, 0)
            if py_count is not None and pyc_count is not None
            else None
        )
        coverage = (pyc_count / py_count) if py_count else None
        summary = f"compileall over {target}: "
        if py_count is not None and pyc_count is not None:
            summary += f"{pyc_count}/{py_count} sources compiled, {failed} failed"
            if coverage is not None:
                summary += f" (coverage {coverage:.2f})"
        else:
            summary += "source/bytecode counts unavailable"
        success = bool(result.get("success"))
        errors = self._tail(result.get("output") or "", lines=20)
        return ToolResult(
            success=success,
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
                "exit_code": result.get("exit_code"),
            },
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
        packages = requirements.get("python_packages") or discover_packages(
            self.orchestrator, root
        )
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

    def _count(self, command: str, workdir: str) -> Optional[int]:
        result = self.orchestrator.execute_command(command, workdir=workdir)
        try:
            return int((result.get("output") or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            return None

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
        self.orchestrator.execute_command(
            f"cat > {COLLECTED_JSON} <<'SAGEOF'\n{body}\nSAGEOF"
        )

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
