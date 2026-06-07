import json
from pathlib import Path

from sag.web.models import DockerSummary, WorkspaceSummary
from sag.web.session_registry import ContainerSessionRegistry, SessionRegistry


class FakeOrchestrator:
    def __init__(self, files: dict[str, str]):
        self.files = files

    def execute_command(self, command, **kwargs):
        if command.startswith("cat "):
            path = command.removeprefix("cat ").split(" ", 1)[0].strip("'")
            if path in self.files:
                return {"exit_code": 0, "output": self.files[path]}
            return {"exit_code": 1, "output": ""}

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

    assert rows[0].id == "SETUP-20260606-213241"
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

    assert [row.id for row in rows] == ["SETUP-20260606-213241", "UI-12345678"]


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


def test_container_session_registry_returns_setup_artifact_detail():
    files = {
        "/workspace/.setup_agent/contexts/trunk_20260606_213241.json": json.dumps(
            {
                "context_id": "trunk_20260606_213241",
                "created_at": "2026-06-06 21:32:41.079329",
                "last_updated": "2026-06-06 21:35:09.305137",
                "goal": "Setup and configure the commons-cli project to be runnable",
                "todo_list": [
                    {"id": "task_1", "description": "Clone repository", "status": "completed"},
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
        "SETUP-20260606-213241",
    )

    assert detail is not None
    assert detail.id == "SETUP-20260606-213241"
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
        "SETUP-20260606-213241",
    )

    assert detail is not None
    assert detail.logs == ["first line", "second line"]
