# tests/test_trunk_context_dedup.py
from sag.agent.context_manager import TaskStatus, TrunkContext


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


def test_task_ids_unique_after_removal():
    """Reproduces the beam 2026-06-10 collision: after pending tasks are
    removed the list is non-contiguous (task_1..task_8 minus a few); a
    length-based id would collide with surviving task_7/task_8."""
    trunk = _trunk()
    for n in range(8):
        trunk.add_task(f"original task number {n}")
    # Simulate the analyzer clearing some mid-list tasks (e.g. stale pending)
    trunk.todo_list = [t for t in trunk.todo_list if t.id not in ("task_3", "task_4")]
    assert len(trunk.todo_list) == 6  # ids: 1,2,5,6,7,8

    new_id = trunk.add_task("a brand new follow-up task")
    existing_ids = [t.id for t in trunk.todo_list]
    assert existing_ids.count(new_id) == 1, f"duplicate id {new_id} in {existing_ids}"
    assert new_id == "task_9"


def test_insert_task_dedupes_task_id_prefixed_copy():
    """The model authored next_tasks copying the plan with a 'task_4: ' prefix;
    dedup must catch it as the same task."""
    trunk = _trunk()
    original = trunk.add_task("Compile project using Gradle")
    duplicate = trunk.insert_task("task_4: Compile project using Gradle", index=0)
    assert duplicate == original
    assert len(trunk.todo_list) == 1


def test_insert_task_strips_bogus_task_id_prefix_from_description():
    """Ids are assigned by the manager; a 'task_N:' prefix in the description
    is always bogus and must not be stored (it desyncs id and description)."""
    trunk = _trunk()
    new_id = trunk.add_task("task_3: Install Gradle dependencies and verify build environment")
    task = next(t for t in trunk.todo_list if t.id == new_id)
    assert task.description == "Install Gradle dependencies and verify build environment"


def test_completed_task_with_prefixed_duplicate_not_readded():
    trunk = _trunk()
    first = trunk.add_task("Execute Gradle project tests")
    for task in trunk.todo_list:
        task.status = TaskStatus.COMPLETED
    again = trunk.add_task("task_5: Execute Gradle project tests")
    assert again == first
    assert len(trunk.todo_list) == 1
