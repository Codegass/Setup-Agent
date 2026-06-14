import json
from pathlib import Path

from sag.web.context_trace import ContextTraceBuilder
from sag.web.session_registry import _context_filenames, _is_context_filename

CONTEXTS_DIR = "/workspace/.setup_agent/contexts"


class FakeOrchestrator:
    def __init__(self, files):
        self.files = files
        self.commands = []

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        names = [
            path.removeprefix(f"{CONTEXTS_DIR}/")
            for path in sorted(self.files)
            if path.startswith(f"{CONTEXTS_DIR}/")
        ]
        return {"exit_code": 0, "output": "\n".join(names)}


def test_context_trace_artifact_filenames_include_outputs_and_journals():
    assert _is_context_filename("phase_build.json")
    assert _is_context_filename("trunk_20260612_010101.json")
    assert _is_context_filename("full_outputs.jsonl")
    assert _is_context_filename("journal/phase_build.journal.jsonl")
    assert not _is_context_filename("task_3.json")
    assert not _is_context_filename("../phase_build.json")


def test_container_find_requests_trace_artifacts():
    fake = FakeOrchestrator(
        {
            f"{CONTEXTS_DIR}/trunk_20260612_010101.json": "{}",
            f"{CONTEXTS_DIR}/phase_build.json": "{}",
            f"{CONTEXTS_DIR}/full_outputs.jsonl": "",
            f"{CONTEXTS_DIR}/journal/phase_build.journal.jsonl": "{}",
        }
    )

    names = _context_filenames(fake)

    assert names == [
        "full_outputs.jsonl",
        "journal/phase_build.journal.jsonl",
        "phase_build.json",
        "trunk_20260612_010101.json",
    ]
    assert "-maxdepth 2" in fake.commands[0]
    assert "-name 'phase_*.json'" in fake.commands[0]


def test_context_trace_builder_creates_trunk_phase_task_iteration_trace(tmp_path: Path):
    contexts = tmp_path / ".setup_agent" / "contexts"
    journal = contexts / "journal"
    journal.mkdir(parents=True)
    output = "BUILD SUCCESS\nCompiled 42 classes."
    (contexts / "full_outputs.jsonl").write_text(
        json.dumps(
            {
                "ref_id": "output_build",
                "task_id": "phase_build",
                "tool_name": "build",
                "timestamp": "2026-06-13T21:00:00",
                "output_length": len(output),
                "output": output,
                "metadata": {"iteration": 2},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (contexts / "trunk_commons.json").write_text(
        json.dumps(
            {
                "goal": "Set up commons-cli",
                "todo_list": [
                    {
                        "id": "phase_provision",
                        "description": "Provision the repository",
                        "status": "completed",
                        "key_results": "Repository cloned.",
                    },
                    {
                        "id": "phase_build",
                        "description": "Build the project",
                        "status": "completed",
                        "key_results": "Compilation succeeded.",
                        "evidence_refs": ["output_build"],
                    },
                    {
                        "id": "task_legacy",
                        "description": "Old non-phase task",
                        "status": "completed",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (contexts / "phase_build.json").write_text(
        json.dumps(
            {
                "task_id": "phase_build",
                "task_description": "Build the project",
                "history": [
                    {
                        "type": "thought",
                        "iteration": 1,
                        "content": "Need to compile with the build tool.",
                    },
                    {
                        "type": "action",
                        "iteration": 2,
                        "tool_name": "build",
                        "parameters": {"action": "compile"},
                        "success": True,
                        "output": "Full output ref: output_build",
                        "observation": "build succeeded",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (journal / "phase_build.journal.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "iteration": 1,
                        "phase": "build",
                        "segments": {"intro": 120, "ledger": 0, "steps": 1},
                        "delta": {"added": 1, "compacted": 0},
                        "total_chars": 3000,
                        "intro_text": "=== PHASE: BUILD ===",
                        "step_span": 1,
                    }
                ),
                json.dumps(
                    {
                        "iteration": 2,
                        "phase": "build",
                        "segments": {"intro": 120, "ledger": 0, "steps": 3},
                        "delta": {"added": 2, "compacted": 0},
                        "total_chars": 4200,
                        "step_span": 3,
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    trace = ContextTraceBuilder(contexts).build()

    assert trace.trunk.goal == "Set up commons-cli"
    assert trace.trunk.progress == {"done": 2, "total": 2}
    assert [phase.id for phase in trace.phases] == ["phase_provision", "phase_build"]

    build = trace.phases[1]
    assert build.name == "build"
    assert build.title == "Build the project"
    assert build.key_results == "Compilation succeeded."
    assert [ref.ref for ref in build.refs] == ["output_build"]

    task = build.tasks[0]
    assert task.id == "phase_build/work"
    assert task.title == "Build the project"
    assert [iteration.iteration for iteration in task.iterations] == [1, 2]
    assert task.iterations[0].thoughts == ["Need to compile with the build tool."]
    assert task.iterations[0].window.intro_text == "=== PHASE: BUILD ==="
    action = task.iterations[1].actions[0]
    assert action.tool_name == "build"
    assert action.parameters == {"action": "compile"}
    assert action.observation == "build succeeded"
    assert action.refs[0].content == output


def test_context_trace_builder_tolerates_malformed_journal_and_output_lines(tmp_path: Path):
    contexts = tmp_path / ".setup_agent" / "contexts"
    journal = contexts / "journal"
    journal.mkdir(parents=True)
    (contexts / "full_outputs.jsonl").write_text(
        "\n".join(
            [
                "{bad json",
                json.dumps(
                    {
                        "ref_id": "output_ok",
                        "tool_name": "bash",
                        "output": "complete output",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    (contexts / "trunk_trace.json").write_text(
        json.dumps(
            {
                "goal": "Set up demo",
                "todo_list": [
                    {
                        "id": "phase_build",
                        "description": "Build the project",
                        "status": "in_progress",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (contexts / "phase_build.json").write_text(
        json.dumps(
            {
                "history": [
                    {
                        "type": "action",
                        "iteration": 3,
                        "tool_name": "bash",
                        "output_refs": ["output_ok", "output_missing"],
                        "output": "Full output ref: output_missing",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (journal / "phase_build.journal.jsonl").write_text(
        "\n".join(
            [
                "{bad json",
                json.dumps(
                    {
                        "iteration": 3,
                        "phase": "build",
                        "segments": {"steps": 2},
                        "delta": {"added": 1},
                        "total_chars": 99,
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    trace = ContextTraceBuilder(contexts).build()

    action = trace.phases[0].tasks[0].iterations[0].actions[0]
    assert [ref.ref for ref in action.refs] == ["output_ok", "output_missing"]
    assert action.refs[0].content == "complete output"
    assert action.refs[1].content is None
    assert trace.phases[0].tasks[0].iterations[0].window.total_chars == 99


def test_context_trace_builder_handles_missing_phase_files_and_journals(tmp_path: Path):
    contexts = tmp_path / ".setup_agent" / "contexts"
    contexts.mkdir(parents=True)
    (contexts / "trunk_trace.json").write_text(
        json.dumps(
            {
                "goal": "Set up demo",
                "todo_list": [
                    {
                        "id": "phase_report",
                        "description": "Generate report",
                        "status": "pending",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    trace = ContextTraceBuilder(contexts).build()

    assert trace.trunk.progress == {"done": 0, "total": 1}
    assert trace.phases[0].tasks[0].iterations == []
