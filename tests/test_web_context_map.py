import json
from pathlib import Path

from sag.web.context_map import ContextMapBuilder


def test_context_map_builder_creates_trunk_branch_abstraction(tmp_path: Path):
    contexts = tmp_path / ".setup_agent" / "contexts"
    contexts.mkdir(parents=True)
    (contexts / "trunk_commons.json").write_text(
        json.dumps(
            {
                "goal": "Set up commons-cli",
                "overall_status": "In progress",
                "summary": "Build succeeds; tests are partial.",
                "todo_list": [
                    {
                        "id": "T1",
                        "task": "Clone repository",
                        "status": "completed",
                        "summary": "Cloned.",
                    },
                    {
                        "id": "T2",
                        "task": "Run tests",
                        "status": "active",
                        "summary": "312/320 passing.",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (contexts / "task_T2.json").write_text(
        json.dumps(
            {
                "task": "Run tests",
                "why": "Build is green.",
                "memory": ["mvn test -> 312/320"],
                "last_refs": [{"label": "maven_test.log", "ref": "logs/maven_test.log"}],
                "context_pressure": 0.42,
            }
        ),
        encoding="utf-8",
    )

    ctx = ContextMapBuilder(contexts).build()

    assert ctx.trunk.goal == "Set up commons-cli"
    assert ctx.trunk.progress == {"done": 1, "total": 2}
    assert ctx.tasks[1].status == "active"
    assert ctx.active_branch.task == "Run tests"
    assert ctx.debug["trunk"].endswith("trunk_commons.json")


def test_context_map_builder_exposes_task_evidence_state_and_refs(tmp_path: Path):
    contexts = tmp_path / ".setup_agent" / "contexts"
    contexts.mkdir(parents=True)
    (contexts / "full_outputs.jsonl").write_text(
        json.dumps(
            {
                "ref_id": "output_validator",
                "task_id": "T2",
                "tool_name": "pytest",
                "timestamp": "2026-06-07T00:00:00",
                "output_length": 28,
                "output": "312 passed, 8 failed",
                "metadata": {},
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
                        "id": "T2",
                        "task": "Run tests",
                        "status": "completed",
                        "summary": "312/320 passing.",
                        "evidence_status": "partial",
                        "evidence_refs": ["output_validator"],
                        "conflicts": ["Expected all tests green; observed 8 failures."],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    ctx = ContextMapBuilder(contexts).build()

    task = ctx.tasks[0]
    assert task.evidence_status == "partial"
    assert [ref.ref for ref in task.evidence_refs] == ["output_validator"]
    assert [ref.ref for ref in task.refs] == ["output_validator"]
    assert task.refs[0].content == "312 passed, 8 failed"
    assert task.conflicts == ["Expected all tests green; observed 8 failures."]
    alias_data = task.model_dump(mode="json", by_alias=True)
    assert alias_data["evidenceStatus"] == "partial"
    assert alias_data["evidenceRefs"][0]["ref"] == "output_validator"


def test_context_map_builder_hydrates_dict_evidence_refs_from_full_outputs(tmp_path: Path):
    contexts = tmp_path / ".setup_agent" / "contexts"
    contexts.mkdir(parents=True)
    (contexts / "full_outputs.jsonl").write_text(
        json.dumps(
            {
                "ref_id": "output_validator",
                "task_id": "T2",
                "tool_name": "pytest",
                "timestamp": "2026-06-07T00:00:00",
                "output_length": 18,
                "output": "rich output preview",
                "metadata": {},
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
                        "id": "T2",
                        "task": "Run tests",
                        "status": "completed",
                        "evidence_refs": [
                            {
                                "ref": "output_validator",
                                "label": "validator evidence",
                            }
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (contexts / "task_T2.json").write_text(
        json.dumps(
            {
                "history": [
                    {
                        "type": "action",
                        "tool_name": "pytest",
                        "success": True,
                        "output": "Full output ref: output_validator",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    ctx = ContextMapBuilder(contexts).build()

    task = ctx.tasks[0]
    assert task.refs[0].label == "validator evidence"
    assert task.refs[0].tool == "pytest"
    assert task.refs[0].content == "rich output preview"
    assert task.refs[0].content_length == 18


def test_context_map_builder_uses_real_sag_in_progress_description_fields(tmp_path: Path):
    contexts = tmp_path / ".setup_agent" / "contexts"
    contexts.mkdir(parents=True)
    (contexts / "trunk_commons.json").write_text(
        json.dumps(
            {
                "project_goal": "Set up pytest plugin",
                "state": "running",
                "tasks": [
                    {
                        "id": "T1",
                        "description": "Prepare repository",
                        "status": "completed",
                    },
                    {
                        "id": "T2",
                        "description": "Run integration tests",
                        "status": "in_progress",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (contexts / "task_T2.json").write_text(
        json.dumps(
            {
                "task": "Run integration tests",
                "why": "Validate setup against the real project.",
                "memory": ["uv run pytest tests/integration"],
                "last_refs": [{"label": "integration.log", "ref": "logs/integration.log"}],
                "pressure": 0.7,
            }
        ),
        encoding="utf-8",
    )

    ctx = ContextMapBuilder(contexts).build()

    assert ctx.tasks[1].status == "in_progress"
    assert ctx.tasks[1].title == "Run integration tests"
    assert ctx.active_branch.task == "Run integration tests"


def test_context_map_builder_enriches_task_rows_from_branch_context(tmp_path: Path):
    contexts = tmp_path / ".setup_agent" / "contexts"
    contexts.mkdir(parents=True)
    (contexts / "trunk_commons.json").write_text(
        json.dumps(
            {
                "goal": "Set up commons-cli",
                "todo_list": [
                    {
                        "id": "task_4",
                        "description": "Compile project using Maven",
                        "status": "completed",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (contexts / "task_4.json").write_text(
        json.dumps(
            {
                "task_description": "Compile project using Maven",
                "previous_task_summary": "Previous task: Java 8 verified.",
                "history": [
                    {
                        "type": "action",
                        "tool_name": "maven",
                        "success": False,
                        "output": "Maven 3.8.7 below requirement.\n... [Full output ref: output_old_maven] ...",
                    },
                    {
                        "type": "action",
                        "tool_name": "maven",
                        "success": True,
                        "output": "Maven Build Summary:\nBUILD SUCCESS\n... [Full output ref: output_build_success] ...",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    ctx = ContextMapBuilder(contexts).build()

    assert ctx.tasks[0].summary.startswith("Previous task: Java 8 verified.")
    assert "maven succeeded" in ctx.tasks[0].summary
    assert [ref.ref for ref in ctx.tasks[0].refs] == ["output_old_maven", "output_build_success"]


def test_context_map_builder_preserves_full_branch_summary(tmp_path: Path):
    contexts = tmp_path / ".setup_agent" / "contexts"
    contexts.mkdir(parents=True)
    long_tail = " ".join(f"detail_{index}" for index in range(80))
    (contexts / "trunk_commons.json").write_text(
        json.dumps(
            {
                "goal": "Set up commons-lang",
                "todo_list": [
                    {
                        "id": "task_5",
                        "description": "Generate comprehensive setup completion report",
                        "status": "completed",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (contexts / "task_5.json").write_text(
        json.dumps(
            {
                "task_description": "Generate comprehensive setup completion report",
                "previous_task_summary": "Previous task (task_4): tests passed.",
                "history": [
                    {
                        "type": "action",
                        "tool_name": "report",
                        "success": True,
                        "output": f"Report generated successfully. {long_tail}",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    ctx = ContextMapBuilder(contexts).build()

    assert "detail_79" in ctx.tasks[0].summary
    assert "..." not in ctx.tasks[0].summary


def test_context_map_builder_resolves_full_output_refs(tmp_path: Path):
    contexts = tmp_path / ".setup_agent" / "contexts"
    contexts.mkdir(parents=True)
    full_output = "\n".join(
        [
            "PROJECT_SETUP SUCCEEDED",
            "Repository cloned successfully.",
            "Java Version Required: 17",
            *[f"deep_detail_{index}" for index in range(80)],
        ]
    )
    (contexts / "full_outputs.jsonl").write_text(
        json.dumps(
            {
                "ref_id": "output_project_setup",
                "task_id": "task_1",
                "tool_name": "project_setup",
                "timestamp": "2026-06-07T00:00:00",
                "output_length": len(full_output),
                "output": full_output,
                "metadata": {},
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
                        "id": "task_1",
                        "description": "Clone repository and setup basic environment",
                        "status": "completed",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (contexts / "task_1.json").write_text(
        json.dumps(
            {
                "task_description": "Clone repository and setup basic environment",
                "history": [
                    {
                        "type": "action",
                        "tool_name": "project_setup",
                        "success": True,
                        "output": (
                            "PROJECT_SETUP SUCCEEDED\n"
                            "Java Version Required:...\n"
                            "... [Full output ref: output_project_setup] ..."
                        ),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    ctx = ContextMapBuilder(contexts).build()

    assert "deep_detail_79" in ctx.tasks[0].summary
    assert "Java Version Required: 17" in ctx.tasks[0].summary
    assert "Java Version Required:..." not in ctx.tasks[0].summary
    assert ctx.tasks[0].refs[0].ref == "output_project_setup"
    assert ctx.tasks[0].refs[0].content == full_output
    assert ctx.tasks[0].refs[0].content_length == len(full_output)


def test_context_map_builder_resolves_output_refs_from_previous_task_summary(tmp_path: Path):
    contexts = tmp_path / ".setup_agent" / "contexts"
    contexts.mkdir(parents=True)
    full_output = "PROJECT_SETUP SUCCEEDED\nRepository cloned successfully.\nJava 8 detected."
    (contexts / "full_outputs.jsonl").write_text(
        json.dumps(
            {
                "ref_id": "output_prev_task",
                "task_id": "task_1",
                "tool_name": "project_setup",
                "timestamp": "2026-06-07T00:00:00",
                "output_length": len(full_output),
                "output": full_output,
                "metadata": {},
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
                        "id": "task_2",
                        "description": "Analyze project structure",
                        "status": "completed",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (contexts / "task_2.json").write_text(
        json.dumps(
            {
                "task_description": "Analyze project structure",
                "previous_task_summary": (
                    "Previous task (task_1): PROJECT_SETUP SUCCEEDED\n"
                    "... [Output truncated: showing 325 of 941 chars] ...\n"
                    "... [Full output ref: output_prev_task] ..."
                ),
                "history": [],
            }
        ),
        encoding="utf-8",
    )

    ctx = ContextMapBuilder(contexts).build()

    assert [ref.ref for ref in ctx.tasks[0].refs] == ["output_prev_task"]
    assert ctx.tasks[0].refs[0].content == full_output
    assert "Output truncated" not in ctx.tasks[0].summary
    assert "Search with" not in ctx.tasks[0].summary
    assert "output_prev_task" in ctx.tasks[0].summary


def test_context_map_builder_handles_non_object_trunk_json(tmp_path: Path):
    for index, trunk_root in enumerate((["not", "a", "dict"], "not a dict"), start=1):
        contexts = tmp_path / f"case_{index}" / ".setup_agent" / "contexts"
        contexts.mkdir(parents=True)
        (contexts / "trunk_commons.json").write_text(json.dumps(trunk_root), encoding="utf-8")

        ctx = ContextMapBuilder(contexts).build()

        assert ctx.trunk.goal == "Unknown goal"
        assert ctx.trunk.progress == {"done": 0, "total": 0}
        assert ctx.tasks == []


def test_context_map_builder_handles_malformed_branch_fields(tmp_path: Path):
    contexts = tmp_path / ".setup_agent" / "contexts"
    contexts.mkdir(parents=True)
    (contexts / "trunk_commons.json").write_text(
        json.dumps(
            {
                "goal": "Set up commons-cli",
                "todo_list": [
                    {"id": "T2", "task": "Run tests", "status": "active"},
                ],
            }
        ),
        encoding="utf-8",
    )
    (contexts / "task_T2.json").write_text(
        json.dumps(
            {
                "task": "Run tests",
                "memory": "pytest output",
                "last_refs": ["logs/test.log"],
                "context_pressure": "bad",
            }
        ),
        encoding="utf-8",
    )

    ctx = ContextMapBuilder(contexts).build()

    assert ctx.active_branch.task == "Run tests"
    assert ctx.active_branch.memory == []
    assert ctx.active_branch.last_refs == []
    assert ctx.active_branch.pressure == 0.0


def test_context_map_builder_handles_non_list_tasks(tmp_path: Path):
    for index, raw_tasks in enumerate(({"id": "T1"}, "T1"), start=1):
        contexts = tmp_path / f"case_{index}" / ".setup_agent" / "contexts"
        contexts.mkdir(parents=True)
        (contexts / "trunk_commons.json").write_text(
            json.dumps({"goal": "Set up commons-cli", "todo_list": raw_tasks}),
            encoding="utf-8",
        )

        ctx = ContextMapBuilder(contexts).build()

        assert ctx.tasks == []
        assert ctx.trunk.progress == {"done": 0, "total": 0}
