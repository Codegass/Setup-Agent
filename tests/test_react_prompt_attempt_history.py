# tests/test_react_prompt_attempt_history.py
from sag.agent.react_prompt_builder import _format_attempt_history


class _Task:
    def __init__(self, id, description, status):
        self.id = id
        self.description = description
        self.status = type("S", (), {"value": status})()


def test_attempt_history_lists_all_tasks_with_status_and_flags_repeats():
    tasks = [
        _Task("task_1", "Clone repository", "completed"),
        _Task("task_2", "Manually explore project", "completed"),
        _Task("task_3", "Manually explore project", "completed"),
        _Task("task_4", "Manually explore project", "in_progress"),
    ]
    text = _format_attempt_history(tasks)
    assert "task_1" in text and "completed" in text
    # The repeated description must be flagged so the model notices the loop.
    assert "repeat" in text.lower() or "×" in text or "x3" in text.lower()
