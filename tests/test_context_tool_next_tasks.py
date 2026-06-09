# tests/test_context_tool_next_tasks.py
from sag.tools.context_tool import ContextTool


class FakeTrunk:
    def __init__(self):
        self.added = []
    def add_task(self, description):
        # Mimic dedup from Task 1.
        norm = " ".join(description.split()).strip().lower()
        for d in self.added:
            if " ".join(d.split()).strip().lower() == norm:
                return "task_existing"
        self.added.append(description)
        return f"task_{len(self.added)}"


def test_apply_next_tasks_adds_unique_and_skips_blank():
    trunk = FakeTrunk()
    added = ContextTool._apply_next_tasks(
        trunk,
        ["Build java-core with gradlew", "  ", "Build java-core with gradlew"],
    )
    # Blank dropped; verbatim duplicate collapses to the existing task.
    assert trunk.added == ["Build java-core with gradlew"]
    assert added == ["task_1"]


class _Task:
    def __init__(self, task_id, description):
        self.id = task_id
        self.description = description


class FakeTrunkOrdered:
    """Mimics TrunkContext.{add_task,insert_task} with a positional todo_list."""

    def __init__(self, descriptions):
        self.todo_list = [_Task(f"task_{i + 1}", d) for i, d in enumerate(descriptions)]

    def _dup(self, description):
        norm = " ".join(description.split()).strip().lower()
        for t in self.todo_list:
            if " ".join(t.description.split()).strip().lower() == norm:
                return t.id
        return None

    def add_task(self, description):
        return self.insert_task(description)

    def insert_task(self, description, index=None):
        existing = self._dup(description)
        if existing:
            return existing
        task = _Task(f"task_{len(self.todo_list) + 1}", description)
        if index is None or index >= len(self.todo_list):
            self.todo_list.append(task)
        else:
            self.todo_list.insert(max(0, index), task)
        return task.id


def test_apply_next_tasks_inserts_after_completed_task_before_pending():
    # build (completed) -> run_tests, report (pending). A follow-up authored on
    # the build completion must run NEXT, i.e. right after 'build' and BEFORE
    # the remaining pending tasks.
    trunk = FakeTrunkOrdered(["build", "run_tests", "report"])
    added = ContextTool._apply_next_tasks(
        trunk, ["fix X then rebuild", "rerun build"], after_task_id="task_1"
    )
    order = [t.description for t in trunk.todo_list]
    assert order == ["build", "fix X then rebuild", "rerun build", "run_tests", "report"]
    # And the follow-ups keep their authored order.
    assert order.index("fix X then rebuild") < order.index("rerun build")
    assert len(added) == 2


def test_apply_next_tasks_without_after_id_appends_at_tail():
    trunk = FakeTrunkOrdered(["build", "run_tests"])
    ContextTool._apply_next_tasks(trunk, ["cleanup"])
    assert [t.description for t in trunk.todo_list] == ["build", "run_tests", "cleanup"]


def test_apply_next_tasks_drops_non_string_elements():
    # The schema says array of strings; a malformed element (int/list/dict)
    # must be dropped, not crash the atomic completion.
    trunk = FakeTrunk()
    added = ContextTool._apply_next_tasks(trunk, ["real task", 123, ["nested"], None, {"a": 1}])
    assert trunk.added == ["real task"]
    assert added == ["task_1"]


def test_apply_next_tasks_caps_the_number_of_followups():
    trunk = FakeTrunk()
    proposed = [f"task number {i}" for i in range(20)]
    added = ContextTool._apply_next_tasks(trunk, proposed)
    # A model cannot flood the trunk with an unbounded number of follow-ups.
    assert len(trunk.added) <= 5
    assert len(added) <= 5


def test_apply_next_tasks_truncates_overlong_description():
    trunk = FakeTrunk()
    ContextTool._apply_next_tasks(trunk, ["x" * 5000])
    assert len(trunk.added) == 1
    assert len(trunk.added[0]) <= 500


def test_next_tasks_is_exposed_in_parameter_schema():
    # Function-calling mode sends the manual schema to the model; next_tasks
    # must be advertised there or the model can never author follow-ups.
    tool = ContextTool(context_manager=None)
    props = tool.get_parameter_schema()["properties"]
    assert "next_tasks" in props
    assert props["next_tasks"]["type"] == "array"
