from sag.web.models import (
    BuildSummary,
    DockerSummary,
    ExecutionSessionSummary,
    TestSummary,
    WorkspaceSummary,
)


def test_workspace_summary_serializes_demo_shape():
    summary = WorkspaceSummary(
        id="sag-commons-cli",
        project="apache/commons-cli",
        container="sag-commons-cli",
        stack="Java · Maven",
        docker=DockerSummary(status="running", image="sag/base:24.04"),
        task="Build project and run full test suite",
        build=BuildSummary(state="success", tool="Maven", time="47.2s"),
        test=TestSummary(state="partial", pass_count=312, fail_count=8, skip_count=0, total=320),
        report="ready",
        changed=7,
        active_session="CC-3",
        latest_session="CC-3",
        updated="just now",
    )

    data = summary.model_dump(mode="json")
    assert data["id"] == "sag-commons-cli"
    assert data["docker"]["status"] == "running"
    assert data["test"]["pass_count"] == 312
    assert data["latest_session"] == "CC-3"


def test_session_summary_uses_workspace_task_entry_semantics():
    session = ExecutionSessionSummary(
        id="CC-3",
        workspace="sag-commons-cli",
        title="Build project and execute full test suite",
        status="running",
        entry="CLI",
        start="02:14:08",
        duration="running · 2m 11s",
        build="success",
        test=TestSummary(state="partial", pass_count=312, fail_count=8, skip_count=0, total=320),
        report="ready",
        files=7,
        evidence=18,
    )

    assert session.workspace == "sag-commons-cli"
    assert session.entry == "CLI"
