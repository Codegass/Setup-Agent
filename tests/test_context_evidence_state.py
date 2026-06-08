from sag.agent.context_manager import ContextManager, TaskStatus
from sag.evidence import EvidenceStatus
from sag.tools.context_tool import ContextTool


def test_trunk_task_records_narrative_and_evidence(tmp_path):
    manager = ContextManager(workspace_path=str(tmp_path))
    trunk = manager.create_trunk_context(
        goal="Set up project",
        project_url="https://example.test/demo",
        project_name="demo",
    )
    task_id = trunk.add_task("Run tests")
    manager._save_trunk_context(trunk)

    updated = manager.update_task_evidence(
        task_id,
        evidence_status="partial",
        evidence_refs=["output_abc", "surefire_report"],
        conflicts=["maven_success_vs_surefire_failures"],
        validator_findings=[
            {"type": "contradiction", "reason": "surefire failures", "status": "partial"}
        ],
    )

    assert updated is True
    reloaded = manager.load_trunk_context()
    task = reloaded.todo_list[0]
    assert task.key_results == ""
    assert task.evidence_status == EvidenceStatus.PARTIAL
    assert task.evidence_refs == ["output_abc", "surefire_report"]
    assert task.conflicts == ["maven_success_vs_surefire_failures"]


def test_branch_receives_previous_summary_and_evidence_digest(tmp_path):
    manager = ContextManager(workspace_path=str(tmp_path))
    trunk = manager.create_trunk_context(
        goal="Set up project",
        project_url="https://example.test/demo",
        project_name="demo",
    )
    task_1 = trunk.add_task("Run build")
    task_2 = trunk.add_task("Run tests")
    trunk.update_task_status(task_1, TaskStatus.COMPLETED)
    trunk.update_task_key_results(task_1, "Build completed but test reports were not checked.")
    manager._save_trunk_context(trunk)
    manager.update_task_evidence(task_1, evidence_status="partial", evidence_refs=["output_build"], conflicts=[])

    result = manager.start_new_branch(task_2)
    branch = manager.load_branch_history(task_2)

    assert "Previous task (task_1)" in result["previous_summary"]
    assert "task_1 evidence_status: partial" in branch.previous_task_evidence_digest
    assert "output_build" in branch.previous_task_evidence_digest


def test_branch_receives_previous_evidence_digest_without_key_results(tmp_path):
    manager = ContextManager(workspace_path=str(tmp_path))
    trunk = manager.create_trunk_context(
        goal="Set up project",
        project_url="https://example.test/demo",
        project_name="demo",
    )
    task_1 = trunk.add_task("Run build")
    task_2 = trunk.add_task("Run tests")
    trunk.update_task_status(task_1, TaskStatus.COMPLETED)
    manager._save_trunk_context(trunk)
    manager.update_task_evidence(
        task_1,
        evidence_status="partial",
        evidence_refs=["output_build_only"],
        conflicts=[],
    )

    result = manager.start_new_branch(task_2)
    branch = manager.load_branch_history(task_2)

    assert result["previous_summary"] == ""
    assert "task_1 evidence_status: partial" in branch.previous_task_evidence_digest
    assert "output_build_only" in branch.previous_task_evidence_digest


def test_get_current_context_info_includes_task_evidence_fields(tmp_path):
    manager = ContextManager(workspace_path=str(tmp_path))
    trunk = manager.create_trunk_context(
        goal="Set up project",
        project_url="https://example.test/demo",
        project_name="demo",
    )
    task_id = trunk.add_task("Run tests")
    manager._save_trunk_context(trunk)
    manager.update_task_evidence(
        task_id,
        evidence_status="partial",
        evidence_refs=["output_abc"],
        conflicts=["report_mismatch"],
        validator_findings=[
            {"type": "contradiction", "reason": "report mismatch", "status": "partial"}
        ],
    )

    info = manager.get_current_context_info()
    task = info["todo_list"][0]

    assert task["evidence_status"] == "partial"
    assert task["evidence_refs"] == ["output_abc"]
    assert task["conflicts"] == ["report_mismatch"]
    assert task["validator_findings"][0]["reason"] == "report mismatch"


def test_get_current_context_info_branch_includes_evidence_fields(tmp_path):
    manager = ContextManager(workspace_path=str(tmp_path))
    trunk = manager.create_trunk_context(
        goal="Set up project",
        project_url="https://example.test/demo",
        project_name="demo",
    )
    task_1 = trunk.add_task("Run build")
    task_2 = trunk.add_task("Run tests")
    trunk.update_task_status(task_1, TaskStatus.COMPLETED)
    trunk.update_task_key_results(task_1, "Build completed.")
    manager._save_trunk_context(trunk)
    manager.update_task_evidence(
        task_1,
        evidence_status="partial",
        evidence_refs=["output_build"],
        conflicts=[],
    )

    manager.start_new_branch(task_2)
    branch = manager.load_branch_history(task_2)
    branch.current_task_evidence_refs = ["output_test"]
    manager._save_branch_history(
        branch, str(manager.contexts_dir / f"{task_2}.json")
    )
    manager.current_task_id = task_2

    info = manager.get_current_context_info()

    assert "task_1 evidence_status: partial" in info["previous_task_evidence_digest"]
    assert info["current_task_evidence_refs"] == ["output_test"]


def test_complete_with_results_preserves_narrative_and_evidence(tmp_path):
    manager = ContextManager(workspace_path=str(tmp_path))
    trunk = manager.create_trunk_context(
        goal="Set up project",
        project_url="https://example.test/demo",
        project_name="demo",
    )
    task_id = trunk.add_task("Record setup evidence")
    manager._save_trunk_context(trunk)
    manager.start_new_branch(task_id)
    tool = ContextTool(manager)

    result = tool.execute(
        action="complete_with_results",
        summary="Maven test command exited zero after ignoring failures.",
        key_results="Tests: 206 / 214 passed, 96.3% pass rate, 3 failed, 5 skipped.",
        evidence_status="partial",
        evidence_refs=["output_abc", "surefire_xml"],
    )

    assert result.success is True
    assert result.metadata["evidence_status"] == "partial"
    assert result.metadata["evidence_refs"] == ["output_abc", "surefire_xml"]
    assert result.metadata["conflicts"] == []
    reloaded = manager.load_trunk_context()
    task = reloaded.todo_list[0]
    assert task.status.value == "completed"
    assert task.key_results.startswith("Tests: 206 / 214 passed")
    assert task.evidence_status.value == "partial"
    assert task.evidence_refs == ["output_abc", "surefire_xml"]


def test_complete_with_results_preserves_existing_evidence_when_omitted(tmp_path):
    manager = ContextManager(workspace_path=str(tmp_path))
    trunk = manager.create_trunk_context(
        goal="Set up project",
        project_url="https://example.test/demo",
        project_name="demo",
    )
    task_id = trunk.add_task("Record setup evidence")
    manager._save_trunk_context(trunk)
    manager.start_new_branch(task_id)
    manager.update_task_evidence(
        task_id,
        evidence_status="partial",
        evidence_refs=["output_abc", "surefire_xml"],
        conflicts=["report_mismatch"],
    )
    tool = ContextTool(manager)

    result = tool.execute(
        action="complete_with_results",
        summary="Recorded the final setup evidence state.",
        key_results="Evidence preserved for later review.",
    )

    assert result.success is True
    assert result.metadata["evidence_status"] == "partial"
    assert result.metadata["evidence_refs"] == ["output_abc", "surefire_xml"]
    assert result.metadata["conflicts"] == ["report_mismatch"]
    reloaded = manager.load_trunk_context()
    task = reloaded.todo_list[0]
    assert task.status.value == "completed"
    assert task.evidence_status.value == "partial"
    assert task.evidence_refs == ["output_abc", "surefire_xml"]
    assert task.conflicts == ["report_mismatch"]
