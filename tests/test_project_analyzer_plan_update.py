"""Analyzer plan application must be idempotent and preserve task ids.

Beam 2026-06-10 evidence: the analyzer plan was applied at 09:56:37 and
re-applied at 10:11:06, 10:11:39, 10:12:26 — each re-run cleared the pending
tasks and re-added the same plan under fresh ids, churning ids and orphaning
branch contexts/outputs in the webui.
"""

from sag.agent.context_manager import TaskStatus, TrunkContext
from sag.tools.project_analyzer import ProjectAnalyzerTool


PLAN = [
    {"description": "Install Gradle dependencies and verify build environment", "type": "environment"},
    {"description": "Compile project using Gradle", "type": "build"},
    {"description": "Execute Gradle project tests", "type": "test"},
    {"description": "Generate comprehensive setup completion report", "type": "report"},
]


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


def _ids_and_descriptions(trunk):
    return [(t.id, t.description) for t in trunk.todo_list]


def test_plan_application_adds_tasks_once():
    analyzer, trunk = _analyzer_with_trunk()

    assert analyzer._update_trunk_context_with_plan({"execution_plan": PLAN}) is True

    descriptions = [t.description for t in trunk.todo_list]
    for item in PLAN:
        assert item["description"] in descriptions
    assert len(trunk.todo_list) == 2 + len(PLAN)


def test_plan_reapplication_is_idempotent_and_preserves_ids():
    analyzer, trunk = _analyzer_with_trunk()
    analyzer._update_trunk_context_with_plan({"execution_plan": PLAN})
    snapshot = _ids_and_descriptions(trunk)

    # Re-running the analyzer with the same plan must not renumber or
    # duplicate anything.
    analyzer._update_trunk_context_with_plan({"execution_plan": PLAN})
    analyzer._update_trunk_context_with_plan({"execution_plan": PLAN})

    assert _ids_and_descriptions(trunk) == snapshot


def test_plan_reapplication_keeps_in_progress_and_completed_tasks():
    analyzer, trunk = _analyzer_with_trunk()
    analyzer._update_trunk_context_with_plan({"execution_plan": PLAN})
    # First plan task is being worked on
    first_plan_task = trunk.todo_list[2]
    first_plan_task.status = TaskStatus.IN_PROGRESS

    analyzer._update_trunk_context_with_plan({"execution_plan": PLAN})

    assert first_plan_task in trunk.todo_list
    assert first_plan_task.status == TaskStatus.IN_PROGRESS


def test_stale_pending_tasks_not_in_new_plan_are_removed():
    analyzer, trunk = _analyzer_with_trunk()
    stale_id = trunk.add_task("Manually explore and identify project structure")

    analyzer._update_trunk_context_with_plan({"execution_plan": PLAN})

    assert all(t.id != stale_id for t in trunk.todo_list)


def test_unknown_analysis_cannot_overwrite_known_plan():
    """Evidence hierarchy: once a plan from a KNOWN build system is applied,
    a later analysis that fails detection (unknown/none) must not replace it
    (beam 06-10: 25 'unknown'-driven re-plans churned the trunk)."""
    analyzer, trunk = _analyzer_with_trunk()
    analyzer._update_trunk_context_with_plan(
        {"execution_plan": PLAN, "build_system": "Gradle", "project_type": "Java"}
    )
    snapshot = _ids_and_descriptions(trunk)
    assert trunk.environment_summary.get("build_system") == "Gradle"

    fallback_plan = [
        {"description": "Manually explore and identify project structure", "type": "analysis"},
        {"description": "Setup environment", "type": "environment"},
        {"description": "Attempt generic build", "type": "build"},
    ]
    result = analyzer._update_trunk_context_with_plan(
        {"execution_plan": fallback_plan, "build_system": "unknown", "project_type": "unknown"}
    )

    assert result is True
    assert _ids_and_descriptions(trunk) == snapshot


def test_known_analysis_records_build_system_in_trunk():
    analyzer, trunk = _analyzer_with_trunk()
    analyzer._update_trunk_context_with_plan(
        {"execution_plan": PLAN, "build_system": "Maven", "project_type": "Java"}
    )
    assert trunk.environment_summary.get("build_system") == "Maven"
