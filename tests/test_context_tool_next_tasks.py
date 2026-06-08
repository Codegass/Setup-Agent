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
