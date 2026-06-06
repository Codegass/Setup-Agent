"""Deterministic demo read models for the SAG Workbench skeleton UI."""

from __future__ import annotations

from sag.web.models import (
    ActiveBranchSummary,
    BuildSummary,
    ContextMap,
    ContextTask,
    DashboardResponse,
    DockerSummary,
    EvidenceGroup,
    EvidenceRecord,
    ExecutionSessionDetail,
    FileChangeCounts,
    FileChangeDigest,
    FileChangeItem,
    FileSnapshotRef,
    ReportDocument,
    TerminalConnectionState,
    TestSummary,
    TrunkSummary,
    WorkspaceSummary,
)


_COMMONS_WORKSPACE_ID = "sag-commons-cli"
_COMMONS_SESSION_ID = "CC-3"


def _commons_test_summary() -> TestSummary:
    return TestSummary(
        state="partial",
        pass_count=312,
        fail_count=8,
        skip_count=0,
        total=320,
        note="312 passing, 8 HelpFormatter line-wrapping failures in the local Maven suite.",
    )


def _commons_build_summary() -> BuildSummary:
    return BuildSummary(
        state="success",
        tool="Maven 3.9.6",
        time="47.2s",
        artifact="target/commons-cli-1.6.0.jar",
        note="Compiled with Maven 3.9.6 and JDK 11 inside the workspace container.",
    )


def _commons_evidence() -> list[EvidenceGroup]:
    return [
        EvidenceGroup(
            source="Project analyzer",
            status="complete",
            counts="4 refs",
            time="02:16:04",
            summary="Detected commons-cli 1.6.0 Maven project layout and HelpFormatter test entrypoints.",
            records=[
                EvidenceRecord(
                    time="02:14:20",
                    status="ok",
                    title="Project model",
                    detail="Read pom.xml and identified commons-cli 1.6.0 packaging metadata.",
                    ref="pom.xml",
                ),
                EvidenceRecord(
                    time="02:14:27",
                    status="ok",
                    title="Test layout",
                    detail="Mapped HelpFormatter tests in src/test/java as the primary validation target.",
                    ref="src/test/java",
                ),
            ],
        ),
        EvidenceGroup(
            source="Test validator",
            status="partial",
            counts="320 tests",
            time="02:16:41",
            summary="Maven 3.9.6 test run completed with 8 HelpFormatter line-wrapping failures.",
            records=[
                EvidenceRecord(
                    time="02:16:23",
                    status="pass",
                    title="Maven test suite",
                    detail="Executed mvn test with JDK 11 in the workspace container.",
                    ref="target/surefire-reports",
                ),
                EvidenceRecord(
                    time="02:16:39",
                    status="fail",
                    title="HelpFormatter line-wrapping failures",
                    detail="Eight HelpFormatter assertions expected width 74 but observed wrapping at width 80.",
                    ref="target/surefire-reports/TEST-org.apache.commons.cli.HelpFormatterTest.xml",
                ),
            ],
        ),
    ]


def _commons_context() -> ContextMap:
    return ContextMap(
        trunk=TrunkSummary(
            goal="Prepare commons-cli workspace for reproducible setup and validation.",
            state="in_progress",
            progress={"complete": 2, "active": 1, "blocked": 0},
            summary="Workspace analysis and environment setup are complete; test validation is active.",
        ),
        tasks=[
            ContextTask(
                id="CC-1",
                title="Analyze project and dependency graph",
                status="complete",
                summary="Captured commons-cli 1.6.0 coordinates and JDK 11 toolchain requirements.",
                refs=["pom.xml", ".setup_agent/env_overlay.json"],
            ),
            ContextTask(
                id="CC-3",
                title="Run full test suite and summarize HelpFormatter failures",
                status="active",
                summary="Maven tests ran with HelpFormatter line wrapping at width 80 instead of expected width 74.",
                refs=["target/surefire-reports"],
            ),
        ],
        active_branch=ActiveBranchSummary(
            task="CC-3",
            why="The current evidence bundle is centered on the latest validation run.",
            memory=[
                "Use JDK 11 for local Maven commands.",
                "Maven 3.9.6 produced the current HelpFormatter failure evidence.",
                "Keep generated setup state under .setup_agent.",
            ],
            last_refs=[
                {"label": "Project descriptor", "path": "pom.xml"},
                {"label": "Environment overlay", "path": ".setup_agent/env_overlay.json"},
            ],
            pressure=0.42,
        ),
        debug={"container": _COMMONS_WORKSPACE_ID, "entry": "CLI"},
    )


def _commons_files() -> FileChangeDigest:
    return FileChangeDigest(
        snapshot=FileSnapshotRef(
            base="HEAD",
            head="workspace-scan-2026-06-06T0216",
            mode="demo",
        ),
        counts=FileChangeCounts(modified=1, added=1, deleted=0, renamed=0),
        items=[
            FileChangeItem(
                path="pom.xml",
                change="modified",
                size="18 KB",
                mtime="02:15:02",
                note="commons-cli 1.6.0 dependency and plugin metadata inspected for setup.",
            ),
            FileChangeItem(
                path=".setup_agent/env_overlay.json",
                change="added",
                size="2 KB",
                mtime="02:15:36",
                note="Container environment overlay generated for reproducible commands.",
            ),
        ],
    )


def _commons_report() -> ReportDocument:
    return ReportDocument(
        title="setup-report-2026-06-06T0216.md",
        path=".setup_agent/reports/setup-report-2026-06-06T0216.md",
        generated="2026-06-06T02:16:44Z",
        blocks=[
            {
                "type": "summary",
                "heading": "Validation result",
                "body": "commons-cli 1.6.0 setup is usable, with 8 HelpFormatter line-wrapping tests still failing.",
            },
            {
                "type": "evidence",
                "heading": "Evidence sources",
                "body": "Project analyzer and Test validator captured width 74 vs 80 HelpFormatter evidence.",
            },
        ],
    )


def build_demo_dashboard() -> DashboardResponse:
    docker = DockerSummary(
        status="connected",
        image="setup-agent/workbench:demo",
        version="26.06",
        endpoint="unix:///var/run/docker.sock",
    )
    workspace = WorkspaceSummary(
        id=_COMMONS_WORKSPACE_ID,
        project="apache/commons-cli",
        container=_COMMONS_WORKSPACE_ID,
        stack="Java · Maven",
        tag="rel/commons-cli-1.6.0",
        release="1.6.0",
        commit="rel/commons-cli-1.6.0",
        docker=docker,
        task="Run full test suite and summarize HelpFormatter failures",
        build=_commons_build_summary(),
        test=_commons_test_summary(),
        report="ready",
        changed=2,
        active_session=_COMMONS_SESSION_ID,
        latest_session=_COMMONS_SESSION_ID,
        updated="2026-06-06 02:16",
    )
    return DashboardResponse(docker=docker, workspaces=[workspace])


def get_demo_session(session_id: str) -> ExecutionSessionDetail:
    if session_id != _COMMONS_SESSION_ID:
        raise KeyError(session_id)

    return ExecutionSessionDetail(
        id=_COMMONS_SESSION_ID,
        workspace=_COMMONS_WORKSPACE_ID,
        title="Run full test suite and summarize HelpFormatter failures",
        status="partial",
        entry="CLI",
        start="02:14:08",
        duration="2m 36s",
        outcome="Test suite completed with HelpFormatter line-wrapping failures: expected width 74, observed width 80.",
        build=_commons_build_summary(),
        test=_commons_test_summary(),
        report="ready",
        report_doc=_commons_report(),
        evidence=_commons_evidence(),
        files=_commons_files(),
        context=_commons_context(),
        logs=[
            "02:14:08 workspace sag-commons-cli attached",
            "02:14:18 project analyzer started",
            "02:16:41 mvn test completed: 312 passed, 8 HelpFormatter width failures",
        ],
        partial=True,
    )


def get_demo_terminal(workspace_id: str) -> TerminalConnectionState:
    return TerminalConnectionState(
        container=workspace_id,
        cwd="/workspace/commons-cli",
        status="connected",
        tty="120 × 32",
        lines=[
            {"time": "02:14:08", "text": f"connected to {workspace_id}"},
            {"time": "02:16:41", "text": "mvn test completed with HelpFormatter width failures"},
        ],
    )
