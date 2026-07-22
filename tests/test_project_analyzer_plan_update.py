"""Analyzer facts recording must feed environment_summary, never the todo list.

Facts-only behavior (Category-3 refactor): the analyzer no longer expands an
execution plan into the trunk todo list. `_update_trunk_context_with_facts`
records survey facts (build system + static test metrics) into the trunk's
environment_summary and persists them; it must never rewrite a phase trunk's
todo list. The report/test phases consume those recorded metrics.
"""

from sag.agent.context_manager import Task, TaskStatus, TrunkContext
from sag.agent.phase_machine import PHASE_NAMES
from sag.tools.internal.project_analyzer import ProjectAnalyzerTool


class FakeContextManager:
    def __init__(self, trunk):
        self.trunk = trunk
        self.save_count = 0

    def load_trunk_context(self):
        return self.trunk

    def _save_trunk_context(self, trunk):
        self.save_count += 1


def _analyzer_with_trunk():
    trunk = TrunkContext(context_id="trunk_t", goal="g", project_url="u", project_name="p")
    trunk.add_task("Clone repository and setup basic environment (use project_setup tool)")
    trunk.add_task("CRITICAL: Run project_analyzer tool with action='analyze'")
    for task in trunk.todo_list:
        task.status = TaskStatus.COMPLETED
    cm = FakeContextManager(trunk)
    analyzer = ProjectAnalyzerTool(None, cm)
    return analyzer, trunk


def test_known_analysis_records_build_system_in_trunk():
    analyzer, trunk = _analyzer_with_trunk()
    analyzer._update_trunk_context_with_facts({"build_system": "Maven", "project_type": "Java"})
    assert trunk.environment_summary.get("build_system") == "Maven"


# --- Stage-2 phase machine (spec §3.1) --------------------------------------
#
# A phase trunk (phase_<name> task ids) is owned by the engine. The analyzer
# records facts into environment_summary but must never touch the todo list.
# Rewriting the trunk deleted pending phase_build/phase_test/phase_report
# entries, turning every later _persist_phase_record into a silent no-op and
# orphaning task_N entries in the webui.


def _phase_trunk():
    trunk = TrunkContext(context_id="trunk_p", goal="g", project_url="u", project_name="p")
    for name in PHASE_NAMES:
        trunk.todo_list.append(Task(id=f"phase_{name}", description=f"{name} objective"))
    trunk.todo_list[0].status = TaskStatus.COMPLETED  # phase_provision
    trunk.todo_list[1].status = TaskStatus.IN_PROGRESS  # phase_analyze
    return trunk


def test_phase_trunk_task_ids_survive_facts_update():
    trunk = _phase_trunk()
    analyzer = ProjectAnalyzerTool(None, FakeContextManager(trunk))

    result = analyzer._update_trunk_context_with_facts(
        {"build_system": "Gradle", "static_test_count": 42}
    )

    assert result is True
    assert [t.id for t in trunk.todo_list] == [f"phase_{name}" for name in PHASE_NAMES], (
        "analyzer must never rewrite a phase trunk: pending phase_* tasks "
        "must stay intact and no task_N entries may be appended"
    )
    # Pending phase tasks remained pending; the in-progress one untouched.
    assert trunk.todo_list[1].status == TaskStatus.IN_PROGRESS
    assert all(t.status == TaskStatus.PENDING for t in trunk.todo_list[2:])


def test_phase_trunk_still_records_analysis_metrics():
    """Build system + static test metrics keep flowing to environment_summary
    (the report/test phases consume them) even though the todo list is
    untouched."""
    trunk = _phase_trunk()
    analyzer = ProjectAnalyzerTool(None, FakeContextManager(trunk))

    analyzer._update_trunk_context_with_facts(
        {"build_system": "Gradle", "static_test_count": 42}
    )

    assert trunk.environment_summary.get("build_system") == "Gradle"
    assert trunk.environment_summary.get("static_test_count") == 42


def test_phase_trunk_record_persists_after_facts_update():
    """End-to-end shape of the original defect: after the analyze phase runs
    the analyzer, the engine must still be able to mark phase_build done."""
    trunk = _phase_trunk()
    analyzer = ProjectAnalyzerTool(None, FakeContextManager(trunk))
    analyzer._update_trunk_context_with_facts({"build_system": "Gradle"})

    assert trunk.update_task_status("phase_build", TaskStatus.COMPLETED, "ok") is True
