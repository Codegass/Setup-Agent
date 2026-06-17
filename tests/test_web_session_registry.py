import json
import os
import shlex
from datetime import datetime
from pathlib import Path

from sag.web.models import DockerSummary, WorkspaceSummary
from sag.web.session_registry import (
    ContainerSessionRegistry,
    ContainerSessionStore,
    SessionRegistry,
    _build_payload_from_metrics,
    _matching_log_session_dir,
    _setup_logs,
)


def _make_session_dir(logs: Path, name: str, project: str, agent_line: str, mtime: float) -> Path:
    session_dir = logs / name
    session_dir.mkdir(parents=True)
    command_log = session_dir / f"command_project_{project}.log"
    command_log.write_text("setup command output\n", encoding="utf-8")
    (session_dir / "agent_execution.log").write_text(agent_line + "\n", encoding="utf-8")
    os.utime(command_log, (mtime, mtime))
    return session_dir


def test_setup_logs_matches_latest_run_by_mtime_ignoring_dir_name_timezone(tmp_path: Path):
    # Two runs of the same project. The "newer" dir's NAME uses a host-local time
    # that would look "in the future" versus a container-UTC created_at, which the
    # old name-vs-created filter wrongly skipped. mtime-based matching must still
    # pick the most recent run regardless of the dir-name timezone.
    logs = tmp_path / "logs"
    _make_session_dir(logs, "session_20260101_000000_111", "commons-cli", "OLD run", 1_000_000)
    newer = _make_session_dir(logs, "session_20260616_134219_222", "commons-cli", "NEW run", 2_000_000)

    assert _matching_log_session_dir(logs, "commons-cli") == newer
    assert _setup_logs(logs, "commons-cli") == ["NEW run"]


def test_setup_logs_empty_without_matching_command_log(tmp_path: Path):
    logs = tmp_path / "logs"
    session_dir = logs / "session_20260616_000000_333"
    session_dir.mkdir(parents=True)
    # agent_execution.log present, but no command_project_<project>.log → no match.
    (session_dir / "agent_execution.log").write_text("orphan\n", encoding="utf-8")

    assert _matching_log_session_dir(logs, "commons-cli") is None
    assert _setup_logs(logs, "commons-cli") == []


def test_setup_logs_empty_when_logs_root_missing(tmp_path: Path):
    assert _setup_logs(tmp_path / "nope", "commons-cli") == []


class FakeOrchestrator:
    def __init__(self, files: dict[str, str]):
        self.files = files

    def execute_command(self, command, **kwargs):
        if command.startswith("cat "):
            path = command.removeprefix("cat ").split(" ", 1)[0].strip("'")
            if path in self.files:
                return {"exit_code": 0, "output": self.files[path]}
            return {"exit_code": 1, "output": ""}

        if "printf %s" in command and ">" in command:
            tokens = shlex.split(command)
            try:
                value = tokens[tokens.index("printf") + 2]
                path = tokens[tokens.index(">") + 1]
            except (ValueError, IndexError):
                return {"exit_code": 1, "output": "bad write command"}
            self.files[path] = value
            return {"exit_code": 0, "output": ""}

        if command.startswith("find /workspace/.setup_agent/contexts"):
            filenames = [
                path.removeprefix("/workspace/.setup_agent/contexts/")
                for path in sorted(self.files)
                if path.startswith("/workspace/.setup_agent/contexts/")
            ]
            return {"exit_code": 0, "output": "\n".join(filenames)}

        if command.startswith("find /workspace -maxdepth 1 -name 'setup-report-*.md'"):
            reports = [
                path
                for path in sorted(self.files)
                if path.startswith("/workspace/setup-report-") and path.endswith(".md")
            ]
            return {"exit_code": 0, "output": "\n".join(reports)}

        return {"exit_code": 0, "output": ""}


def workspace_summary() -> WorkspaceSummary:
    return WorkspaceSummary(
        id="sag-commons-cli",
        project="commons-cli",
        container="sag-commons-cli",
        docker=DockerSummary(status="running"),
    )


def test_session_registry_reads_local_session_index(tmp_path: Path):
    setup_agent = tmp_path / ".setup_agent" / "sessions"
    setup_agent.mkdir(parents=True)
    (setup_agent / "index.json").write_text(
        json.dumps(
            {
                "sessions": [
                    {
                        "id": "CC-3",
                        "workspace": "sag-commons-cli",
                        "title": "Build project",
                        "status": "running",
                        "entry": "CLI",
                        "start": "02:14:08",
                        "duration": "running · 2m 11s",
                        "build": "success",
                        "test": {
                            "state": "partial",
                            "pass": 312,
                            "fail": 8,
                            "skip": 0,
                            "total": 320,
                        },
                        "report": "ready",
                        "files": 7,
                        "evidence": 18,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    rows = SessionRegistry().read_index(tmp_path, "sag-commons-cli")

    assert rows[0].id == "CC-3"
    assert rows[0].workspace == "sag-commons-cli"
    assert rows[0].test.pass_count == 312


def test_session_registry_returns_empty_for_missing_index(tmp_path: Path):
    assert SessionRegistry().read_index(tmp_path, "sag-commons-cli") == []


def test_session_registry_returns_empty_for_unreadable_or_invalid_indexes(tmp_path: Path):
    setup_agent = tmp_path / ".setup_agent" / "sessions"
    setup_agent.mkdir(parents=True)

    for payload in ["{bad json", "[]", json.dumps({"sessions": {}})]:
        (setup_agent / "index.json").write_text(payload, encoding="utf-8")

        assert SessionRegistry().read_index(tmp_path, "sag-commons-cli") == []


def test_session_registry_skips_bad_rows_and_defaults_bad_numbers(tmp_path: Path):
    setup_agent = tmp_path / ".setup_agent" / "sessions"
    setup_agent.mkdir(parents=True)
    (setup_agent / "index.json").write_text(
        json.dumps(
            {
                "sessions": [
                    "bad row",
                    {
                        "id": None,
                        "workspace": None,
                        "title": None,
                        "status": None,
                        "entry": None,
                        "start": None,
                        "finish": 17,
                        "duration": None,
                        "build": None,
                        "test": {"pass": "", "fail": None, "skip": "nope", "total": "12x"},
                        "report": None,
                        "files": "many",
                        "evidence": "",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    rows = SessionRegistry().read_index(tmp_path, "sag-commons-cli")

    assert len(rows) == 1
    assert rows[0].id == "unknown"
    assert rows[0].workspace == "sag-commons-cli"
    assert rows[0].title == "Untitled task"
    assert rows[0].entry == "external"
    assert rows[0].start == "—"
    assert rows[0].finish == "17"
    assert rows[0].duration == "—"
    assert rows[0].test.pass_count == 0
    assert rows[0].test.fail_count == 0
    assert rows[0].test.skip_count == 0
    assert rows[0].test.total == 0
    assert rows[0].files == 0
    assert rows[0].evidence == 0


def test_session_registry_strips_text_and_defaults_blank_optional_fields(tmp_path: Path):
    setup_agent = tmp_path / ".setup_agent" / "sessions"
    setup_agent.mkdir(parents=True)
    (setup_agent / "index.json").write_text(
        json.dumps(
            {
                "sessions": [
                    {
                        "id": "  CC-4  ",
                        "workspace": "  sag-commons-cli  ",
                        "title": "   ",
                        "entry": "",
                        "start": "\t",
                        "finish": "   ",
                        "duration": "\n",
                        "build": " success ",
                        "test": {},
                        "report": " ready ",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    rows = SessionRegistry().read_index(tmp_path, "sag-commons-cli")

    assert rows[0].id == "CC-4"
    assert rows[0].workspace == "sag-commons-cli"
    assert rows[0].title == "Untitled task"
    assert rows[0].entry == "external"
    assert rows[0].start == "—"
    assert rows[0].finish is None
    assert rows[0].duration == "—"
    assert rows[0].build == "success"
    assert rows[0].report == "ready"


def test_container_session_store_finalizes_default_unknown_evidence_status():
    files = {}
    orchestrator = FakeOrchestrator(files)
    store = ContainerSessionStore(
        orchestrator_factory=lambda workspace_id: orchestrator,
        clock=lambda: datetime(2026, 6, 8, 6, 30, 0),
    )

    store.mark_started(
        workspace_id="sag-commons-cli",
        session_id="UI-success",
        task="Run tests",
        source_session=None,
    )
    store.mark_finished(
        workspace_id="sag-commons-cli",
        session_id="UI-success",
        success=True,
        outcome="Task completed.",
    )

    store.mark_started(
        workspace_id="sag-commons-cli",
        session_id="UI-failed",
        task="Run tests",
        source_session=None,
    )
    store.mark_finished(
        workspace_id="sag-commons-cli",
        session_id="UI-failed",
        success=False,
        outcome="Task failed.",
    )

    payload = json.loads(files["/workspace/.setup_agent/sessions/index.json"])
    by_id = {item["id"]: item for item in payload["sessions"]}
    assert by_id["UI-success"]["evidence_status"] == "success"
    assert by_id["UI-failed"]["evidence_status"] == "blocked"


def test_container_session_store_preserves_explicit_evidence_status_on_finish():
    files = {}
    orchestrator = FakeOrchestrator(files)
    store = ContainerSessionStore(
        orchestrator_factory=lambda workspace_id: orchestrator,
        clock=lambda: datetime(2026, 6, 8, 6, 30, 0),
    )

    store.mark_started(
        workspace_id="sag-commons-cli",
        session_id="UI-partial",
        task="Run tests",
        source_session=None,
    )
    payload = json.loads(files["/workspace/.setup_agent/sessions/index.json"])
    payload["sessions"][0]["evidence_status"] = "partial"
    payload["sessions"][0]["evidence_status_source"] = "tool"
    files["/workspace/.setup_agent/sessions/index.json"] = json.dumps(payload)

    store.mark_finished(
        workspace_id="sag-commons-cli",
        session_id="UI-partial",
        success=True,
        outcome="Task completed with unresolved test evidence.",
    )

    payload = json.loads(files["/workspace/.setup_agent/sessions/index.json"])
    assert payload["sessions"][0]["evidence_status"] == "partial"


def test_container_session_registry_falls_back_to_last_comment_without_index():
    files = {
        "/workspace/.sag_last_comment.json": json.dumps(
            {
                "comment": "Task completed: give me a report of all the test in the workspace",
                "timestamp": "2026-06-06T21:14:09.715549",
                "project": "commons-cli",
            }
        )
    }
    registry = ContainerSessionRegistry(
        orchestrator_factory=lambda workspace_id: FakeOrchestrator(files)
    )

    rows = registry.list_workspace_sessions(workspace_summary())

    assert rows[0].id == "LEGACY-20260606-211409"
    assert rows[0].title == "give me a report of all the test in the workspace"
    assert rows[0].status == "completed"
    assert rows[0].finish == "2026-06-06T21:14:09.715549"


def test_container_session_registry_returns_legacy_last_comment_detail():
    files = {
        "/workspace/.sag_last_comment.json": json.dumps(
            {
                "comment": "Task completed: give me a report of all the test in the workspace",
                "timestamp": "2026-06-06T21:14:09.715549",
                "project": "commons-cli",
            }
        )
    }
    registry = ContainerSessionRegistry(
        orchestrator_factory=lambda workspace_id: FakeOrchestrator(files)
    )

    detail = registry.get_workspace_session_detail(
        workspace_summary(),
        "LEGACY-20260606-211409",
    )

    assert detail is not None
    assert detail.id == "LEGACY-20260606-211409"
    assert detail.outcome.startswith("Task completed")
    assert detail.evidence[0].source == "SAG session"


def test_container_session_registry_falls_back_to_setup_artifacts_without_index_or_comment():
    files = {
        "/workspace/.setup_agent/project_meta.json": json.dumps(
            {
                "project_name": "commons-cli",
                "project_url": "https://github.com/apache/commons-cli.git",
                "goal": "Setup and configure the commons-cli project to be runnable",
            }
        ),
        "/workspace/.setup_agent/contexts/trunk_20260606_213241.json": json.dumps(
            {
                "context_id": "trunk_20260606_213241",
                "created_at": "2026-06-06 21:32:41.079329",
                "last_updated": "2026-06-06 21:35:09.305137",
                "goal": "Setup and configure the commons-cli project to be runnable",
                "todo_list": [
                    {"id": "task_1", "description": "Clone repository", "status": "completed"},
                    {"id": "task_2", "description": "Run tests", "status": "completed"},
                    {
                        "id": "task_3",
                        "description": "Generate final report",
                        "status": "pending",
                    },
                ],
            }
        ),
        "/workspace/setup-report-20260606-213509.md": (
            "# Project Setup Report\n\n"
            "**Generated:** 2026-06-06 21:35:09\n"
            "**Result:** SUCCESS\n\n"
            "| **Tests Executed** | 430 |\n"
            "| **Tests Passed** | 420 |\n"
            "| **Pass Rate** | 97.7% |\n"
        ),
    }
    registry = ContainerSessionRegistry(
        orchestrator_factory=lambda workspace_id: FakeOrchestrator(files)
    )

    rows = registry.list_workspace_sessions(workspace_summary())

    assert rows[0].id == "SETUP-commons-cli-20260606-213241"
    assert rows[0].title == "Setup and configure the commons-cli project to be runnable"
    assert rows[0].status == "completed"
    assert rows[0].entry == "CLI"
    assert rows[0].report == "ready"
    assert rows[0].test.pass_count == 420
    assert rows[0].test.total == 430


def test_container_session_registry_merges_web_sessions_with_setup_artifacts():
    files = {
        "/workspace/.setup_agent/sessions/index.json": json.dumps(
            {
                "sessions": [
                    {
                        "id": "UI-12345678",
                        "workspace": "sag-commons-cli",
                        "title": "Run formatter tests",
                        "status": "running",
                        "entry": "Web UI",
                        "start": "2026-06-06T21:48:30",
                        "duration": "running",
                        "build": "none",
                        "test": {"state": "none", "pass": 0, "fail": 0, "skip": 0, "total": 0},
                        "report": "none",
                        "files": 0,
                        "evidence": 1,
                    }
                ]
            }
        ),
        "/workspace/.setup_agent/contexts/trunk_20260606_213241.json": json.dumps(
            {
                "context_id": "trunk_20260606_213241",
                "created_at": "2026-06-06 21:32:41.079329",
                "last_updated": "2026-06-06 21:35:09.305137",
                "goal": "Setup and configure the commons-cli project to be runnable",
                "todo_list": [],
            }
        ),
        "/workspace/setup-report-20260606-213509.md": (
            "# Project Setup Report\n\n"
            "**Generated:** 2026-06-06 21:35:09\n"
            "**Result:** SUCCESS\n"
        ),
    }
    registry = ContainerSessionRegistry(
        orchestrator_factory=lambda workspace_id: FakeOrchestrator(files)
    )

    rows = registry.list_workspace_sessions(workspace_summary())

    assert [row.id for row in rows] == ["SETUP-commons-cli-20260606-213241", "UI-12345678"]


def test_container_session_registry_parses_setup_report_breakdown_table():
    files = {
        "/workspace/.setup_agent/contexts/trunk_20260606_213241.json": json.dumps(
            {
                "context_id": "trunk_20260606_213241",
                "created_at": "2026-06-06 21:32:41.079329",
                "last_updated": "2026-06-06 21:35:09.305137",
                "goal": "Set up commons-cli",
                "todo_list": [],
            }
        ),
        "/workspace/setup-report-20260606-213509.md": (
            "| Total Available | Executed | Passed | Failed | Errors | Skipped |\n"
            "|-----------------|----------|--------|--------|---------|---------|\n"
            "| 460 | 430 | 420 | 0 | 0 | 10 |\n"
        ),
    }
    registry = ContainerSessionRegistry(
        orchestrator_factory=lambda workspace_id: FakeOrchestrator(files)
    )

    rows = registry.list_workspace_sessions(workspace_summary())

    assert rows[0].test.total == 430
    assert rows[0].test.pass_count == 420
    assert rows[0].test.fail_count == 0
    assert rows[0].test.skip_count == 10


def test_setup_artifact_build_state_reads_checkmark_marker():
    # Real reports mark the build with a ✅ in the Build row and write
    # "**Result:** ✅ SUCCESS"; they never contain the literal phrase
    # "build success". The dashboard must still report success, not "unknown".
    files = {
        "/workspace/.setup_agent/contexts/trunk_20260606_213241.json": json.dumps(
            {
                "context_id": "trunk_20260606_213241",
                "created_at": "2026-06-06 21:32:41.079329",
                "last_updated": "2026-06-06 21:35:09.305137",
                "goal": "Set up commons-cli",
                "todo_list": [
                    {"id": "task_1", "description": "Compile", "status": "completed"},
                ],
            }
        ),
        "/workspace/setup-report-20260606-213509.md": (
            "# 🎯 Project Setup Report\n\n"
            "**Result:** ✅ SUCCESS\n\n"
            "### Build & Test Overview\n"
            "│ Build           │ ✅ 115 classes, 0 JARs            │\n"
        ),
    }
    registry = ContainerSessionRegistry(
        orchestrator_factory=lambda workspace_id: FakeOrchestrator(files)
    )

    rows = registry.list_workspace_sessions(workspace_summary())

    assert rows[0].build == "success"


def test_setup_artifact_build_state_reads_failure_marker():
    # A failed build is marked with ❌ in the Build row.
    files = {
        "/workspace/.setup_agent/contexts/trunk_20260606_213241.json": json.dumps(
            {
                "context_id": "trunk_20260606_213241",
                "created_at": "2026-06-06 21:32:41.079329",
                "last_updated": "2026-06-06 21:35:09.305137",
                "goal": "Set up commons-cli",
                "todo_list": [
                    {"id": "task_1", "description": "Compile", "status": "failed"},
                ],
            }
        ),
        "/workspace/setup-report-20260606-213509.md": (
            "# 🎯 Project Setup Report\n\n"
            "**Result:** ❌ INCOMPLETE\n\n"
            "### Build & Test Overview\n"
            "│ Build           │ ❌ compilation failed             │\n"
        ),
    }
    registry = ContainerSessionRegistry(
        orchestrator_factory=lambda workspace_id: FakeOrchestrator(files)
    )

    rows = registry.list_workspace_sessions(workspace_summary())

    assert rows[0].build == "failed"


def test_container_session_registry_returns_setup_artifact_detail():
    files = {
        "/workspace/.setup_agent/contexts/trunk_20260606_213241.json": json.dumps(
            {
                "context_id": "trunk_20260606_213241",
                "created_at": "2026-06-06 21:32:41.079329",
                "last_updated": "2026-06-06 21:35:09.305137",
                "goal": "Setup and configure the commons-cli project to be runnable",
                "todo_list": [
                    {
                        "id": "phase_provision",
                        "description": "Provision repository",
                        "status": "completed",
                    },
                ],
            }
        ),
        "/workspace/setup-report-20260606-213509.md": (
            "# Project Setup Report\n\n"
            "**Commons-Cli** | Maven Java Project | Maven\n"
            "**Generated:** 2026-06-06 21:35:09\n"
            "**Result:** SUCCESS\n"
            "\u2502 Build \u2502 115 classes, 0 JARs \u2502\n"
        ),
    }
    registry = ContainerSessionRegistry(
        orchestrator_factory=lambda workspace_id: FakeOrchestrator(files)
    )

    detail = registry.get_workspace_session_detail(
        workspace_summary(),
        "SETUP-commons-cli-20260606-213241",
    )

    assert detail is not None
    assert detail.id == "SETUP-commons-cli-20260606-213241"
    assert detail.report == "ready"
    assert detail.report_doc is not None
    assert detail.report_doc.title == "setup-report-20260606-213509.md"
    assert [block["type"] for block in detail.report_doc.blocks[:4]] == [
        "h1",
        "p",
        "meta",
        "status",
    ]
    assert detail.build.state == "success"
    assert detail.build.tool == "Maven"
    assert detail.build.note == "115 classes, 0 JARs"
    assert detail.context is not None
    assert detail.context.trunk.progress == {"done": 1, "total": 1}
    assert detail.context.phases[0].id == "phase_provision"


def test_setup_artifact_detail_uses_trunk_report_phase_without_backfill():
    files = {
        "/workspace/.setup_agent/contexts/trunk_20260606_213241.json": json.dumps(
            {
                "context_id": "trunk_20260606_213241",
                "created_at": "2026-06-06 21:32:41.079329",
                "last_updated": "2026-06-06 21:35:09.305137",
                "goal": "Setup and configure the commons-cli project to be runnable",
                "todo_list": [
                    {"id": "phase_test", "description": "Run tests", "status": "completed"},
                    {
                        "id": "phase_report",
                        "description": "Generate final setup report",
                        "status": "completed",
                        "notes": "",
                        "key_results": "Final setup report generated.",
                    },
                ],
            }
        ),
        "/workspace/setup-report-20260606-213509.md": (
            "# Project Setup Report\n\n"
            "**Generated:** 2026-06-06 21:35:09\n"
            "**Result:** SUCCESS\n"
        ),
    }
    registry = ContainerSessionRegistry(
        orchestrator_factory=lambda workspace_id: FakeOrchestrator(files)
    )

    detail = registry.get_workspace_session_detail(
        workspace_summary(),
        "SETUP-commons-cli-20260606-213241",
    )

    assert detail is not None
    assert detail.context is not None
    report_phase = detail.context.phases[1]
    assert report_phase.id == "phase_report"
    assert report_phase.status == "completed"
    assert report_phase.key_results == "Final setup report generated."
    assert report_phase.refs == []
    assert detail.context.trunk.progress == {"done": 2, "total": 2}


def test_container_session_registry_recovers_setup_logs_from_host_session(tmp_path: Path):
    session_dir = tmp_path / "session_20260606_213207"
    session_dir.mkdir()
    (session_dir / "command_project_commons-cli.log").write_text(
        "2026-06-06 21:32:07 | Starting project setup: commons-cli\n",
        encoding="utf-8",
    )
    (session_dir / "agent_execution.log").write_text(
        "first line\nsecond line\n",
        encoding="utf-8",
    )
    files = {
        "/workspace/.setup_agent/contexts/trunk_20260606_213241.json": json.dumps(
            {
                "context_id": "trunk_20260606_213241",
                "created_at": "2026-06-06 21:32:41.079329",
                "goal": "Setup commons-cli",
                "project_name": "commons-cli",
                "todo_list": [],
            }
        ),
        "/workspace/setup-report-20260606-213509.md": "**Result:** SUCCESS\n",
    }
    registry = ContainerSessionRegistry(
        orchestrator_factory=lambda workspace_id: FakeOrchestrator(files),
        logs_root=tmp_path,
    )

    detail = registry.get_workspace_session_detail(
        workspace_summary(),
        "SETUP-commons-cli-20260606-213241",
    )

    assert detail is not None
    assert detail.logs == ["first line", "second line"]


def test_container_session_registry_keeps_complete_setup_report_blocks():
    files = {
        "/workspace/.setup_agent/contexts/trunk_20260606_213241.json": json.dumps(
            {
                "context_id": "trunk_20260606_213241",
                "created_at": "2026-06-06 21:32:41.079329",
                "goal": "Setup commons-cli",
                "todo_list": [],
            }
        ),
        "/workspace/setup-report-20260606-213509.md": "\n".join(
            [
                "# Project Setup Report",
                "**Generated:** 2026-06-06 21:35:09",
                "**Result:** SUCCESS",
                *[f"### Section {index}" for index in range(1, 18)],
                "### Final Notes",
                "The setup report should not be truncated.",
            ]
        ),
    }
    registry = ContainerSessionRegistry(
        orchestrator_factory=lambda workspace_id: FakeOrchestrator(files)
    )

    detail = registry.get_workspace_session_detail(
        workspace_summary(),
        "SETUP-commons-cli-20260606-213241",
    )

    assert detail is not None
    assert detail.report_doc is not None
    assert any(block.get("text") == "Final Notes" for block in detail.report_doc.blocks)
    assert any(
        block.get("text") == "The setup report should not be truncated."
        for block in detail.report_doc.blocks
    )


def test_same_second_setup_sessions_resolve_to_their_own_workspace():
    # Two batch launches can start in the same second and create trunk contexts
    # with identical timestamp stems. Session ids must stay unique per workspace
    # or the global detail lookup returns the wrong project's context map.
    def files_for(project: str) -> dict[str, str]:
        return {
            "/workspace/.setup_agent/contexts/trunk_20260607_173324.json": json.dumps(
                {
                    "context_id": "trunk_20260607_173324",
                    "created_at": "2026-06-07 17:33:24.000000",
                    "last_updated": "2026-06-07 17:35:09.000000",
                    "goal": f"Setup {project}",
                    "todo_list": [],
                }
            ),
        }

    def workspace_for(workspace_id: str, project: str) -> WorkspaceSummary:
        return WorkspaceSummary(
            id=workspace_id,
            project=project,
            container=workspace_id,
            docker=DockerSummary(status="running"),
        )

    workspaces = {
        "sag-commons-vfs": workspace_for("sag-commons-vfs", "commons-vfs"),
        "sag-dubbo": workspace_for("sag-dubbo", "dubbo"),
    }
    orchestrators = {
        "sag-commons-vfs": FakeOrchestrator(files_for("commons-vfs")),
        "sag-dubbo": FakeOrchestrator(files_for("dubbo")),
    }

    class FakeWorkspaceRegistry:
        def list_workspaces(self):
            return list(workspaces.values())

    registry = ContainerSessionRegistry(
        orchestrator_factory=lambda workspace_id: orchestrators[workspace_id],
        workspace_registry_factory=lambda: FakeWorkspaceRegistry(),
    )

    vfs_id = registry.list_workspace_sessions(workspaces["sag-commons-vfs"])[0].id
    dubbo_id = registry.list_workspace_sessions(workspaces["sag-dubbo"])[0].id

    assert vfs_id != dubbo_id

    detail = registry.get_session_detail(dubbo_id)
    assert detail.workspace == "sag-dubbo"
    assert detail.title == "Setup dubbo"


def test_build_payload_surfaces_time_note_artifact():
    payload = _build_payload_from_metrics({
        "build": {
            "state": "success", "system": "maven", "tool": "Maven 3.9.6",
            "class_count": 115, "jar_count": 1,
            "time": "47.2s", "note": "clean package", "artifact": "target/x.jar",
        }
    })
    assert payload is not None
    assert payload["time"] == "47.2s"
    assert payload["note"] == "clean package"
    assert payload["artifact"] == "target/x.jar"


def test_build_payload_time_falls_back_to_dash():
    payload = _build_payload_from_metrics({"build": {"state": "success", "tool": "maven"}})
    assert payload is not None
    assert payload["time"] == "—"
    assert payload.get("note") in (None, "", "—")


def test_resolve_logs_root_walks_up_from_subdir(tmp_path, monkeypatch):
    from sag.web.session_registry import _resolve_logs_root

    (tmp_path / "logs" / "session_20260101_000000").mkdir(parents=True)
    sub = tmp_path / "webui"
    sub.mkdir()
    monkeypatch.delenv("SAG_LOG_DIR", raising=False)
    monkeypatch.chdir(sub)
    assert _resolve_logs_root() == tmp_path / "logs"


def test_resolve_logs_root_honors_env(tmp_path, monkeypatch):
    from sag.web.session_registry import _resolve_logs_root

    monkeypatch.setenv("SAG_LOG_DIR", str(tmp_path / "custom"))
    assert _resolve_logs_root() == tmp_path / "custom"


def test_resolve_logs_root_falls_back_to_relative(tmp_path, monkeypatch):
    from sag.web.session_registry import _resolve_logs_root

    monkeypatch.delenv("SAG_LOG_DIR", raising=False)
    empty = tmp_path / "nowhere"
    empty.mkdir()
    monkeypatch.chdir(empty)
    assert _resolve_logs_root() == Path("logs")


def test_resolve_logs_root_skips_empty_subdir_logs(tmp_path, monkeypatch):
    from sag.web.session_registry import _resolve_logs_root

    # Real project build log one level up.
    (tmp_path / "logs" / "session_A").mkdir(parents=True)
    (tmp_path / "logs" / "session_A" / "command_project_demo.log").write_text("x")
    # The UI process's own empty session dir in the launch subdir must not win.
    (tmp_path / "webui" / "logs" / "session_B").mkdir(parents=True)
    monkeypatch.delenv("SAG_LOG_DIR", raising=False)
    monkeypatch.chdir(tmp_path / "webui")
    assert _resolve_logs_root() == tmp_path / "logs"
