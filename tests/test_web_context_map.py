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
