"""Phase histories and journals reach the webui (spec §8.3): registry stops
filtering phase_*.json; new endpoints expose phases + journal timelines."""

import json

from fastapi.testclient import TestClient

from sag.web.app import create_app
from sag.web.read_model import ReadModelBuilder
from sag.web.session_registry import (
    ContainerSessionRegistry,
    _context_filenames,
    _is_context_filename,
)

CONTEXTS_DIR = "/workspace/.setup_agent/contexts"

TRUNK = {
    "context_id": "trunk_20260612_010101",
    "goal": "Set up commons-cli",
    "todo_list": [
        {
            "id": "phase_provision",
            "description": "Provision the toolchain",
            "status": "completed",
            "notes": "",
            "key_results": "JDK 17 ready",
        },
        {
            "id": "phase_build",
            "description": "Build the project",
            "status": "failed",
            "notes": "blocked: enforcer violations",
            "key_results": "",
        },
        {"id": "task_1", "description": "legacy task", "status": "completed"},
    ],
}

JOURNAL_LINES = "\n".join(
    [
        json.dumps(
            {
                "iteration": 1,
                "phase": "build",
                "segments": {},
                "delta": {"added": 1, "compacted": 0},
                "total_chars": 4000,
                "intro_text": "=== PHASE: BUILD ===",
                "step_span": 1,
            }
        ),
        "{torn append, not valid json",
        json.dumps(
            {
                "iteration": 2,
                "phase": "build",
                "segments": {},
                "delta": {"added": 2, "compacted": 0},
                "total_chars": 5200,
                "step_span": 3,
            }
        ),
    ]
)


class FakeOrchestrator:
    """Serves an in-memory container filesystem; records every command."""

    def __init__(self, files):
        self.files = files
        self.commands = []

    def execute_command(self, command, **kwargs):
        self.commands.append(command)

        if command.startswith("cat "):
            path = command.removeprefix("cat ").split(" ", 1)[0].strip("'")
            if path in self.files:
                return {"exit_code": 0, "output": self.files[path]}
            return {"exit_code": 1, "output": ""}

        if command.startswith(f"find {CONTEXTS_DIR}"):
            # Mirrors -maxdepth 1: direct children only (journal/ excluded).
            names = [
                path.removeprefix(f"{CONTEXTS_DIR}/")
                for path in sorted(self.files)
                if path.startswith(f"{CONTEXTS_DIR}/")
                and "/" not in path.removeprefix(f"{CONTEXTS_DIR}/")
            ]
            return {"exit_code": 0, "output": "\n".join(names)}

        return {"exit_code": 0, "output": ""}


def files_with_phases():
    return {
        f"{CONTEXTS_DIR}/trunk_20260612_010101.json": json.dumps(TRUNK),
        f"{CONTEXTS_DIR}/phase_build.json": json.dumps({"task_id": "phase_build"}),
        f"{CONTEXTS_DIR}/journal/phase_build.journal.jsonl": JOURNAL_LINES,
    }


def phase_setup(files):
    fake = FakeOrchestrator(files)
    registry = ContainerSessionRegistry(orchestrator_factory=lambda workspace_id: fake)
    app = create_app(ReadModelBuilder(demo_mode=True), phase_registry=registry)
    return TestClient(app), registry, fake


# --- registry filters -------------------------------------------------------


def test_phase_context_files_are_included():
    assert _is_context_filename("phase_build.json")
    assert _is_context_filename("task_3.json")
    assert _is_context_filename("trunk_20260612_010101.json")
    assert not _is_context_filename("full_outputs.jsonl")


def test_container_find_requests_phase_files():
    fake = FakeOrchestrator(files_with_phases())

    names = _context_filenames(fake)

    assert "phase_build.json" in names
    assert "-name 'phase_*.json'" in fake.commands[0]


# --- phases endpoint --------------------------------------------------------


def test_phases_endpoint_lists_trunk_phase_tasks():
    client, _, _ = phase_setup(files_with_phases())

    response = client.get("/api/workspaces/sag-commons-cli/phases")

    assert response.status_code == 200
    phases = response.json()["phases"]
    assert [p["name"] for p in phases] == ["provision", "build"]
    assert phases[0]["status"] == "completed"
    assert phases[0]["key_results"] == "JDK 17 ready"
    assert phases[1]["status"] == "failed"
    assert phases[1]["notes"] == "blocked: enforcer violations"


def test_phases_endpoint_404_when_no_trunk():
    client, _, _ = phase_setup({})

    response = client.get("/api/workspaces/sag-ghost/phases")

    assert response.status_code == 404


def test_phases_endpoint_404_when_trunk_has_no_phase_history():
    legacy_trunk = {
        "context_id": "trunk_20260101_000000",
        "todo_list": [{"id": "task_1", "description": "legacy", "status": "completed"}],
    }
    client, _, _ = phase_setup(
        {f"{CONTEXTS_DIR}/trunk_20260101_000000.json": json.dumps(legacy_trunk)}
    )

    response = client.get("/api/workspaces/sag-legacy/phases")

    assert response.status_code == 404


# --- journal endpoint -------------------------------------------------------


def test_journal_endpoint_parses_jsonl_and_skips_bad_lines():
    client, _, _ = phase_setup(files_with_phases())

    response = client.get("/api/workspaces/sag-commons-cli/phases/build/journal")

    assert response.status_code == 200
    records = response.json()["records"]
    assert [r["iteration"] for r in records] == [1, 2]
    assert records[0]["intro_text"] == "=== PHASE: BUILD ==="
    assert records[1]["step_span"] == 3


def test_journal_endpoint_404_when_absent():
    client, _, _ = phase_setup(files_with_phases())

    response = client.get("/api/workspaces/sag-commons-cli/phases/test/journal")

    assert response.status_code == 404


def test_journal_rejects_unsafe_phase_names_without_touching_container():
    _, registry, fake = phase_setup(files_with_phases())

    assert registry.get_phase_journal("sag-commons-cli", "../etc/passwd") is None
    assert fake.commands == []
