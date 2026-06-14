#!/usr/bin/env python3
"""Code-based acceptance gate for the SAG Workbench implementation."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE_SCRIPT_TEST = Path("tests/test_web_acceptance_script.py")


BACKEND_FILES = [
    "src/sag/web/__init__.py",
    "src/sag/web/paths.py",
    "src/sag/web/models.py",
    "src/sag/web/status.py",
    "src/sag/web/workspace_registry.py",
    "src/sag/web/session_registry.py",
    "src/sag/web/read_model.py",
    "src/sag/web/evidence.py",
    "src/sag/web/context_trace.py",
    "src/sag/web/file_tracker.py",
    "src/sag/web/task_runner.py",
    "src/sag/web/terminal.py",
    "src/sag/web/app.py",
    "src/sag/web/server.py",
]


FRONTEND_FILES = [
    "webui/package.json",
    "webui/vite.config.ts",
    "webui/src/App.tsx",
    "webui/src/pages/Dashboard.tsx",
    "webui/src/pages/Workspace.tsx",
    "webui/src/pages/SessionDetail.tsx",
    "webui/src/components/session/BuildCard.tsx",
    "webui/src/components/session/TestCard.tsx",
    "webui/src/components/session/EvidenceTimeline.tsx",
    "webui/src/components/session/ContextTrace.tsx",
    "webui/src/components/session/FilesDigest.tsx",
    "webui/src/components/session/ReportDoc.tsx",
    "webui/src/components/session/LogsView.tsx",
    "webui/src/components/terminal/TerminalPanel.tsx",
]

FRONTEND_SESSION_FILES = [
    "webui/package.json",
    "webui/vite.config.ts",
    "webui/src/App.tsx",
    "webui/src/pages/Dashboard.tsx",
    "webui/src/pages/Workspace.tsx",
    "webui/src/pages/SessionDetail.tsx",
    "webui/src/components/session/BuildCard.tsx",
    "webui/src/components/session/TestCard.tsx",
    "webui/src/components/session/EvidenceTimeline.tsx",
    "webui/src/components/session/ContextTrace.tsx",
    "webui/src/components/session/FilesDigest.tsx",
    "webui/src/components/session/ReportDoc.tsx",
    "webui/src/components/session/LogsView.tsx",
]

TERMINAL_FILES = [
    "src/sag/web/terminal.py",
    "src/sag/web/app.py",
    "webui/package.json",
    "webui/src/pages/Workspace.tsx",
    "webui/src/components/terminal/TerminalPanel.tsx",
]


PRODUCT_BOUNDARY_PATTERNS = {
    "workspace task route": ("src/sag/web/app.py", "/api/workspaces/{workspace_id}/tasks"),
    "terminal websocket route": ("src/sag/web/app.py", "/api/workspaces/{workspace_id}/terminal"),
    "xterm import": ("webui/src/components/terminal/TerminalPanel.tsx", "@xterm/xterm"),
    "session detail tabs": ("webui/src/pages/SessionDetail.tsx", "Evidence"),
    "context trace trunk": ("webui/src/components/session/ContextTrace.tsx", "Trunk"),
    "file digest snapshot": ("webui/src/components/session/FilesDigest.tsx", "snapshot"),
}

WORKSPACE_SESSION_PATTERNS = {
    "session status tab": ("webui/src/pages/SessionDetail.tsx", "Status"),
    "session evidence tab": ("webui/src/pages/SessionDetail.tsx", "Evidence"),
    "session context tab": ("webui/src/pages/SessionDetail.tsx", "Context"),
    "session files tab": ("webui/src/pages/SessionDetail.tsx", "Files"),
    "session report tab": ("webui/src/pages/SessionDetail.tsx", "Report"),
    "session logs tab": ("webui/src/pages/SessionDetail.tsx", "Logs"),
    "session starts new task": ("webui/src/pages/SessionDetail.tsx", "New task from this"),
    "workspace overview tab": ("webui/src/pages/Workspace.tsx", "Overview"),
    "workspace sessions tab": ("webui/src/pages/Workspace.tsx", "Sessions"),
    "workspace terminal tab": ("webui/src/pages/Workspace.tsx", "Terminal"),
    "workspace settings tab": ("webui/src/pages/Workspace.tsx", "Settings"),
    "workspace creates tasks": ("webui/src/pages/Workspace.tsx", "New task"),
    "app fetches session detail": ("webui/src/App.tsx", "fetchSession"),
    "app submits workspace tasks": ("webui/src/App.tsx", "submitTask"),
}

TERMINAL_PATTERNS = {
    "terminal websocket route": ("src/sag/web/app.py", "/api/workspaces/{workspace_id}/terminal"),
    "terminal app injection": ("src/sag/web/app.py", "terminal_adapter"),
    "terminal exec options": ("src/sag/web/terminal.py", "build_exec_options"),
    "terminal panel xterm": ("webui/src/components/terminal/TerminalPanel.tsx", "@xterm/xterm"),
    "terminal panel fit addon": (
        "webui/src/components/terminal/TerminalPanel.tsx",
        "@xterm/addon-fit",
    ),
    "terminal panel websocket": (
        "webui/src/components/terminal/TerminalPanel.tsx",
        "new WebSocket",
    ),
    "workspace uses terminal panel": ("webui/src/pages/Workspace.tsx", "TerminalPanel"),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--phase",
        choices=["skeleton", "backend", "workspace-session", "terminal", "frontend", "final"],
        required=True,
    )
    parser.add_argument(
        "--skip-commands",
        action="store_true",
        help="Only run structural checks; used by unit tests for this script.",
    )
    args = parser.parse_args()

    failures: list[str] = []
    print(f"accept_web_ui phase={args.phase}")

    check_phase_files(args.phase, failures)
    check_phase_patterns(args.phase, failures)
    if args.phase == "final":
        require_patterns(PRODUCT_BOUNDARY_PATTERNS, failures)

    if not args.skip_commands:
        if args.phase == "terminal":
            run(
                [
                    "uv",
                    "run",
                    "pytest",
                    "tests/test_web_terminal.py",
                    "tests/test_web_api.py",
                    "-v",
                ],
                failures,
            )
        if args.phase in {"backend", "final"}:
            backend_tests = backend_web_test_paths(failures)
            if backend_tests:
                run(["uv", "run", "pytest", *backend_tests, "-v"], failures)
        if (
            args.phase in {"terminal", "frontend", "final"}
            and (ROOT / "webui/package.json").exists()
        ):
            run(["npm", "test"], failures, cwd=ROOT / "webui")
            run(["npm", "run", "build"], failures, cwd=ROOT / "webui")

    if failures:
        print("ACCEPTANCE FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("ACCEPTANCE PASS")
    return 0


def require_files(paths: list[str], failures: list[str]) -> None:
    for path in paths:
        if not (ROOT / path).exists():
            failures.append(f"missing required file: {path}")


def check_phase_files(phase: str, failures: list[str]) -> None:
    if phase in {"backend", "frontend", "final"}:
        require_files(BACKEND_FILES, failures)
    if phase == "workspace-session":
        require_files(FRONTEND_SESSION_FILES, failures)
    if phase == "terminal":
        require_files(TERMINAL_FILES, failures)
    if phase in {"frontend", "final"}:
        require_files(FRONTEND_FILES, failures)
    if phase == "final":
        require_files(["src/sag/web/static/index.html"], failures)


def check_phase_patterns(phase: str, failures: list[str]) -> None:
    if phase == "workspace-session":
        require_patterns(WORKSPACE_SESSION_PATTERNS, failures)
    if phase == "terminal":
        require_patterns(TERMINAL_PATTERNS, failures)


def require_patterns(patterns: dict[str, tuple[str, str]], failures: list[str]) -> None:
    for label, (path, pattern) in patterns.items():
        target = ROOT / path
        if not target.exists():
            failures.append(f"missing file for pattern check {label}: {path}")
            continue
        text = target.read_text(encoding="utf-8")
        if pattern not in text:
            failures.append(f"missing product-boundary pattern {label}: {pattern}")


def backend_web_test_paths(failures: list[str]) -> list[str]:
    paths = [
        path
        for path in sorted(ROOT.glob("tests/test_web_*.py"))
        if path.relative_to(ROOT) != ACCEPTANCE_SCRIPT_TEST
    ]
    if not paths:
        failures.append("no backend web tests found")
    return [str(path.relative_to(ROOT)) for path in paths]


def run(command: list[str], failures: list[str], cwd: Path | None = None) -> None:
    print("+ " + " ".join(command))
    result = subprocess.run(command, cwd=cwd or ROOT, text=True, check=False)
    if result.returncode != 0:
        failures.append(f"command failed ({result.returncode}): {' '.join(command)}")


if __name__ == "__main__":
    sys.exit(main())
