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
