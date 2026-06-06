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
