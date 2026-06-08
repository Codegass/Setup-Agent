# tests/test_trunk_context_dedup.py
from sag.agent.context_manager import TrunkContext, TaskStatus


def _trunk():
    return TrunkContext(context_id="trunk_test", goal="g", project_url="u", project_name="p")


def test_add_task_dedupes_identical_descriptions():
    trunk = _trunk()
    first = trunk.add_task("Manually explore and identify project structure at /workspace/beam")
    second = trunk.add_task("manually explore and identify project structure at /workspace/beam  ")
    # Same normalized description -> no second task, returns the existing id.
    assert first == second
    assert len(trunk.todo_list) == 1


def test_add_task_keeps_distinct_descriptions():
    trunk = _trunk()
    trunk.add_task("Compile project using Gradle")
    trunk.add_task("Execute Gradle project tests")
    assert len(trunk.todo_list) == 2
